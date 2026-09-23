"""
放量突破选股策略（Volume Breakout）

与 bottom_fishing_strategy.py 并列的第二支日线选股信号，捕捉「横盘整理末端 →
放量突破关键阻力位」的趋势启动点。整体骨架、数据层、基本面防雷、市场环境、
决赛圈周线确认、漏斗日志、时间预算、看门狗全部沿用抄底策略的实现，
只替换「日线信号 + 评估」层。

核心思路（七层漏斗）：
1. 基本面防雷（继承 check_fundamentals）
2. 流动性 & 数据完整性（近 20 日日均成交额 ≥ MIN_AMOUNT）
3. 日线技术面（本策略核心）：
   3.1 三级阻力位识别：L1=近 60 日最高、L2=近 20 日最高、L3=MA60
   3.2 放量确认：量比 1.8–4.0，成交额 ≥ 1 亿，并通过历史量能分位数检查
   3.3 K 线形态：阳线实体占比 ≥ 0.5、收盘/最高 ≥ 0.97、涨幅 2%–7%、跳空 ≤ 2%（继承 MAX_GAP_UP_PCT）
   3.4 突破前平台整理：近 20 日振幅 ≤ 18%、收盘离散度 ≤ 4%
   3.5 趋势背景：MA20 上行、MA60 走平或上行、现价站上 MA20
   3.6 近期假突破过滤：近 10 日无「突破 L1 后跌回」记录
   3.7 突破前一日 RSI ≤ 80（突破日天然推高 RSI）
4. 波动率风控：ATR ≤ 4.0% 现价（比抄底 3.33% 略放宽）
5. 综合评分与等级（B 级 ≥60 准入；熊市 +15 门槛）
6. 决赛圈周线确认（继承 check_weekly_trend + check_weekly_macd）
7. 排序截取（牛 5 / 中性 3 / 熊 0）

交易计划：
- 止损 = max(突破位 Lk − 1×ATR, 现价 × (1 − 6%))，突破失败即撤
- 止盈 = 现价 × (1 + 15%)，趋势启动目标更大
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

# 复用抄底策略的数据层、市场环境、基本面、周线确认、Baostock 生命周期、并发筛选骨架
from src.bottom_fishing_strategy import (
    StrategyConfig,
    CacheManager,
    Signal,
    _bs_login,
    _bs_logout,
    _bs_state,
    _AK_AVAILABLE,
    _beijing_now,
    _effective_regime,
    _fund_verify_state,
    _fill_optional_fundamentals,
    _drop_incomplete_weekly_bar,
    _weekly_trend_data_ok,
    _weekly_macd_data_ok,
    _weekly_fresh_enough,
    get_stock_industry,
    market_crash_halt,
    evaluate_quality_value,
    _screen_quality_pool,
    get_index_daily,
    _build_volatile_row,
    _fetch_weekly_dual,
    _fmt_cell,
    _num_or_none,
    describe_trade_plan,
    _GRADE_ORDER,
    _grade_from_score,
    check_fundamentals,
    check_weekly_macd,
    check_weekly_trend,
    fetch_stats,
    get_daily_data,
    get_fundamentals,
    get_market_environment,
    get_stock_list,
    has_halt_gap,
    kdj_not_overheated,
    log_volatile_rejects,
    macd_not_deeply_weak,
    run_concurrent_screen,
    sort_volatile,
)

logger = logging.getLogger("strategy.breakout")

# ===========================================================================
# 模块级元数据
# ===========================================================================

DISPLAY_COLS = [
    ("code", "代码"), ("name", "名称"), ("date", "日期"), ("close", "收盘"),
    ("score", "评分"), ("grade", "等级"), ("daily_score", "日线分"),
    ("breakout_level", "突破级别"), ("breakout_margin", "突破幅度%"),
    ("rsi", "RSI"), ("vol_ratio", "量比"), ("turnover_ratio", "换手比"),
    ("avg_amount", "日均额(万)"), ("platform_range", "平台振幅%"),
    ("stop_loss", "止损"), ("take_profit", "止盈"), ("rr_ratio", "收益比"),
    ("market_env", "市场"),
]

TITLE = "放量突破选股结果"
PREFIX = "vb"
# 确定性排序：评分↓ → 突破级别↓（L1>L2>L3） → 20日均成交额↓ → 代码↑
SORT_BY = ["score", "breakout_level", "avg_amount", "code"]
SORT_ASC = [False, False, False, True]


# ===========================================================================
# VolumeBreakoutConfig
# ===========================================================================

@dataclass
class VolumeBreakoutConfig(StrategyConfig):
    """放量突破策略配置。继承抄底策略全部字段，覆盖/新增突破专用阈值。

    继承的字段中，以下几项语义在本策略中被重新解释或收紧：
    - MAX_ENTRY_PCT_CHG → 用 MAX_BREAKOUT_PCT 覆盖（同为防追高上限）
    - MA20_TREND_MIN_SLOPE → 用 MA20_TREND_MIN_SLOPE_BREAKOUT 覆盖（突破要求 MA20 上行）
    - DAILY_RSI_ENTRY_MAX → 用 DAILY_RSI_ENTRY_MAX_BREAKOUT 覆盖（放宽到 80，检查突破前一日）
    - MAX_ATR_PCT → 用 MAX_ATR_PCT_BREAKOUT 覆盖（放宽到 4.0%）
    - BEAR_MAX_PICKS → 用 BEAR_MAX_PICKS_BREAKOUT 覆盖（熊市直接空仓）
    - BEAR_GRADE_BOOST → 用 BEAR_GRADE_BOOST_BREAKOUT 覆盖（+15，比抄底严）
    """

    # ===== 关键阻力位 =====
    BREAKOUT_LOOKBACK_HIGH: int = 60         # L1 长期新高回看窗口
    BREAKOUT_LOOKBACK_PLATFORM: int = 20     # L2 平台突破回看窗口
    BREAKOUT_MA_LONG: int = 60               # L3 长期均线周期
    BREAKOUT_MIN_MARGIN: float = 0.005       # 突破关键位的最小超越幅度（0.5%）
    # 关闭 L3 可避免「低位整理股反弹站上 MA60」被误判为突破，代价是信号量减少
    REQUIRE_L1_OR_L2: bool = True

    # ===== 放量确认 =====
    VOLUME_BREAKOUT_MIN: float = 1.8         # 突破日量比下限
    VOLUME_BREAKOUT_PEAK: float = 2.5        # 量能评分峰值
    VOLUME_BREAKOUT_MAX: float = 4.0         # 量能评分归零点（>4 视为过度放量）
    MIN_BREAKOUT_AMOUNT: float = 1e8         # 突破日成交额下限（元），大资金介入门槛

    # ===== K 线形态（防假突破）=====
    MIN_BODY_RATIO: float = 0.5              # 阳线实体占全天振幅比例下限
    MIN_CLOSE_TO_HIGH: float = 0.97          # 收盘/最高下限（防尾盘跳水）
    MIN_BREAKOUT_PCT: float = 2.0            # 突破日最小涨幅（%）
    MAX_BREAKOUT_PCT: float = 7.0            # 突破日最大涨幅（%，防追高/涨停）

    # ===== 突破前平台整理 =====
    PLATFORM_LOOKBACK: int = 20              # 平台整理判定窗口
    PLATFORM_SHIFT: int = 3                  # 平台度量右端点相对当日的偏移（避开突破 bar 污染）
    MAX_PLATFORM_RANGE: float = 0.18         # 平台期最大振幅（18%）
    MAX_PLATFORM_TIGHTNESS: float = 0.04     # 平台期收盘价 std/mean 上限（4%，越小整理越充分）
    ATR_COMPRESSION_LOOKBACK_SHORT: int = 10 # 短期 ATR 均值窗口（仅诊断展示，不参与否决）
    ATR_COMPRESSION_LOOKBACK_LONG: int = 30  # 长期 ATR 均值窗口（仅诊断展示）
    ATR_COMPRESSION_RATIO: float = 1.5       # 短期/长期 ATR 上限（放宽到 1.5，突破本身会抬高 ATR）

    # ===== 趋势背景（比抄底严）=====
    MA20_TREND_MIN_SLOPE_BREAKOUT: float = 0.0   # MA20 斜率下限（必须上行）
    MA60_TREND_MIN_SLOPE: float = -0.01          # MA60 斜率下限（走平或上行）
    MA60_TREND_LOOKBACK: int = 20               # MA60 独立趋势回看窗口
    MA20_STAND_TOLERANCE: float = 0.02           # 现价站上 MA20 容忍度

    # ===== 近期假突破过滤 =====
    FAILED_BREAKOUT_LOOKBACK: int = 10       # 近 N 日有「突破 L1 后跌回」则否决
    FAILED_BREAKOUT_CONFIRM_DAYS: int = 3   # 突破后确认失败的窗口

    # ===== RSI / 波动率（比抄底放宽）=====
    # 突破日 RSI 天然偏高（单日 3-5% 涨幅即可把 RSI14 从 50 推到 70+），
    # 75 上限会误杀正常突破（合成数据测试：L1 突破、评分 85.5 被 FAIL_RSI_HIGH 拦截）。
    # 放宽到 80，仅拦截真正超买（多日连续大涨后的 RSI>80）。
    DAILY_RSI_ENTRY_MAX_BREAKOUT: float = 80.0
    MAX_ATR_PCT_BREAKOUT: float = 4.0

    # ===== 交易计划 =====
    FIXED_STOP_LOSS_PCT_BREAKOUT: float = 6.0
    FIXED_TAKE_PROFIT_PCT_BREAKOUT: float = 15.0
    ATR_STOP_MULT_BREAKOUT: float = 1.0
    # （已删除 MIN_RR_RATIO_BREAKOUT：止损≤6%/止盈15% 下 RR≥2.5 恒成立，检查永不触发；
    #   rr_ratio 仅作展示与落库，波动率风控由 MAX_ATR_PCT_BREAKOUT 承担）
    ATR_TAKE_PROFIT_MULT: float = 5.0        # ATR 目标与固定目标取较近者
    TIME_STOP_DAYS: int = 5                 # 回测中无延续的时间止损
    TRAILING_ATR_MULT: float = 2.0          # 回测移动止损倍数
    ADAPTIVE_VOLUME_LOOKBACK: int = 60       # 自身历史量能分位数窗口
    MIN_VOLUME_PERCENTILE: float = 0.80     # 当日成交量至少处于历史80%分位
    MIN_AMOUNT_RATIO: float = 1.5           # 当日成交额/前20日成交额中位数

    # ===== 推荐数量 =====
    BEAR_MAX_PICKS_BREAKOUT: int = 0         # 熊市直接空仓
    NEUTRAL_MAX_PICKS: int = 3               # 中性市
    # MAX_PICKS 继承（牛市 5 只）

    # ===== 评分门槛 =====
    BEAR_GRADE_BOOST_BREAKOUT: float = 15.0  # 熊市评分门槛提升（比抄底 10 严）

    # ===== 评分权重（合计 100）=====
    W_BREAKOUT: float = 35.0                 # 突破强度（0.55×级别 + 0.45×幅度 加法混合）
    W_VOLUME_BR: float = 25.0                # 量能质量
    W_PATTERN: float = 15.0                  # 平台整理
    W_TREND_BR: float = 15.0                 # 趋势背景
    W_MOMENTUM_BR: float = 10.0              # 动能确认

    # 动能组参数（突破版）
    KDJ_K_MAX_BREAKOUT: float = 80.0         # 突破日 KDJ K 值上限（比抄底 60 放宽）

    # ===== 层 3.8：KDJ / MACD 下限否决（荐股控制）=====
    # 原实现里 KDJ/MACD 只贡献 W_MOMENTUM_BR=10 分（占满分 10%），分档失守仍可守住
    # B 级 60 分准入线——动能已转弱的高位突破照样进正式名单，是本策略唯一的控制漏洞。
    # 本组开关把两个指标升级为硬闸门，阈值与实现与 quality_value 模式共用
    # （MACD_WEAK_DAYS / KDJ_K_HARD_MAX / KDJ_DEAD_CROSS_K 定义在 StrategyConfig）。
    REQUIRE_BR_MACD_NOT_WEAK: bool = True    # MACD 柱连续走弱 → FAIL_MACD_WEAK
    REQUIRE_BR_KDJ_NOT_HIGH: bool = True     # KDJ 高位或高位死叉 → FAIL_KDJ_HIGH


# ===========================================================================
# BreakoutSignal：与 Signal 字段兼容，额外携带突破维度用于排序与展示
# ===========================================================================

@dataclass
class BreakoutSignal(Signal):
    breakout_level: int = 0        # 3=L1(60日新高) / 2=L2(20日新高) / 1=L3(MA60)
    breakout_margin: float = 0.0   # 突破幅度（%）
    platform_range: float = 0.0    # 突破前 20 日振幅（%）
    # 突破策略排序/展示需要 20 日均成交额；基础 Signal（抄底策略）没有该字段，
    # 在子类补充并给默认值以保持两套信号构造接口兼容。
    avg_amount: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ===========================================================================
# 辅助函数：smoothstep 归一化
# ===========================================================================

def _smoothstep(x: pd.Series | float, lo: float, hi: float) -> pd.Series | float:
    """平滑阶跃：x ≤ lo → 0，x ≥ hi → 1，中间 3t²−2t³。用于评分归一化避免硬阈值。"""
    t = ((x - lo) / (hi - lo)).clip(0, 1) if isinstance(x, pd.Series) else max(0.0, min(1.0, (x - lo) / (hi - lo)))
    return t * t * (3 - 2 * t)


def _inv_smoothstep(x: pd.Series | float, lo: float, hi: float) -> pd.Series | float:
    """反向 smoothstep：x ≤ lo → 1，x ≥ hi → 0。用于"越小越好"的评分。"""
    return 1 - _smoothstep(x, lo, hi)


# ===========================================================================
# 核心信号计算
# ===========================================================================

_BREAKOUT_NEED_COLS = {"close", "open", "high", "low", "volume", "amount", "date", "pct_chg"}


def compute_breakout_signals(df: pd.DataFrame, config: VolumeBreakoutConfig) -> Optional[pd.DataFrame]:
    """计算放量突破所需的全部技术指标与评分维度。

    输入：近 DAILY_BARS 根日 K 线（至少 MIN_DAYS=60 根）
    输出：附加了 ma20/ma60/atr/rsi14/macd/kdj/量比/突破位/评分等列的 DataFrame；
         数据不足或列缺失返回 None。
    """
    if df is None or df.empty or not _BREAKOUT_NEED_COLS.issubset(df.columns):
        return None
    # The adaptive volume percentile and the L1 resistance both need more
    # history than the generic MIN_DAYS floor.  Reject short windows here so
    # callers cannot reach the evaluator with silently unavailable volume
    # history (or with partially formed resistance levels).
    _min_history = max(
        config.MIN_DAYS,
        config.ADAPTIVE_VOLUME_LOOKBACK + 2,
        config.BREAKOUT_LOOKBACK_HIGH + 1,
    )
    if len(df) < _min_history:
        return None

    out = df.copy().reset_index(drop=True)

    # 盘中运行时，数据源可能已经返回当天尚未收盘的日 bar。该 bar 的
    # high/low/volume/pct_chg 会继续变化，若参与突破判断会产生盘中漂移。
    # 15:00 前剔除日期为今天的最后一根；收盘后保留已完成 bar。
    # 注意必须用北京时间：GitHub Actions runner 为 UTC，若用本地时区，
    # UTC 15:00（= 北京 23:00）前的所有运行都会把「当日已收盘 bar」误剔除，
    # 导致信号系统性滞后一天（北京 15:25 = UTC 07:25 的定时运行曾中招）。
    if "date" in out.columns and not out.empty:
        _dates = pd.to_datetime(out["date"], errors="coerce")
        _now = _beijing_now()
        if (_now.hour < 15 and pd.notna(_dates.iloc[-1])
                and _dates.iloc[-1].date() == _now.date()):
            out = out.iloc[:-1].reset_index(drop=True)
    if len(out) < _min_history:
        return None

    # ----- 均线 -----
    out["ma20"] = out["close"].rolling(config.DAILY_MA20).mean()
    out["ma60"] = out["close"].rolling(config.BREAKOUT_MA_LONG).mean()

    # ----- RSI14（复用抄底算法：Wilder 平滑）-----
    delta = out["close"].diff()
    gain, loss = delta.clip(lower=0), (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=config.DAILY_RSI_PERIOD - 1, min_periods=config.DAILY_RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(com=config.DAILY_RSI_PERIOD - 1, min_periods=config.DAILY_RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi14"] = 100 - (100 / (1 + rs))
    out.loc[(avg_loss == 0) & (avg_gain > 0), "rsi14"] = 100.0
    out.loc[(avg_loss == 0) & (avg_gain == 0), "rsi14"] = 50.0

    # ----- MACD -----
    exp1 = out["close"].ewm(span=config.DAILY_MACD_FAST, adjust=False).mean()
    exp2 = out["close"].ewm(span=config.DAILY_MACD_SLOW, adjust=False).mean()
    out["macd_diff"] = exp1 - exp2
    out["macd_dea"] = out["macd_diff"].ewm(span=config.DAILY_MACD_SIGNAL, adjust=False).mean()
    out["macd_histogram"] = out["macd_diff"] - out["macd_dea"]
    out["macd_golden_cross"] = (out["macd_diff"] > out["macd_dea"]) & \
                                (out["macd_diff"].shift(1) <= out["macd_dea"].shift(1))

    # ----- KDJ(9,3,3) -----
    _low9 = out["low"].rolling(9).min()
    _high9 = out["high"].rolling(9).max()
    _rsv = (out["close"] - _low9) / (_high9 - _low9).replace(0, np.nan) * 100
    out["kdj_k"] = _rsv.ewm(com=2, adjust=False).mean()
    out["kdj_d"] = out["kdj_k"].ewm(com=2, adjust=False).mean()

    # ----- ATR（Wilder 平滑）-----
    prev_close = out["close"].shift(1)
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - prev_close).abs(),
        (out["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr"] = tr.ewm(com=config.ATR_PERIOD - 1, min_periods=config.ATR_PERIOD, adjust=False).mean()

    # ----- 量比 / 换手比 -----
    out["daily_vol_base"] = out["volume"].shift(1).rolling(20).mean()
    out["daily_vol_ratio"] = out["volume"] / out["daily_vol_base"].replace(0, np.nan)
    # 自身历史分位数与相对成交额补充固定量比，避免只看绝对规模。
    # 向量化实现（sliding_window_view）：等价于 rolling(w).apply(np.mean(x[:-1] <= x[-1]))，
    # 但避免逐窗口 Python 回调——全市场 5000+ 股票 × 120 根 × 61 窗的回调是主要 CPU 热点。
    # NaN 语义与原实现一致（NaN 参与比较恒为 False）。
    _vol_arr = out["volume"].to_numpy(dtype=float)
    _w = config.ADAPTIVE_VOLUME_LOOKBACK + 1
    if len(_vol_arr) >= _w:
        _win = sliding_window_view(_vol_arr, _w)
        _pct = (_win[:, :-1] <= _win[:, -1:]).mean(axis=1)
        # 与 pandas rolling 语义平价：min_periods 按非 NaN 观测数计数，
        # 窗口内含任意 NaN（有效观测 < 窗口长度）→ 结果为 NaN
        _pct = np.where((~np.isnan(_win)).all(axis=1), _pct, np.nan)
        out["volume_percentile"] = np.concatenate([np.full(_w - 1, np.nan), _pct])
    else:
        out["volume_percentile"] = np.nan
    out["amount_ratio"] = out["amount"] / out["amount"].shift(1).rolling(20).median().replace(0, np.nan)
    if "turnover" in out.columns:
        out["daily_to_base"] = out["turnover"].shift(1).rolling(config.DAILY_TURNOVER_LOOKBACK).mean()
        out["daily_turnover_ratio"] = out["turnover"] / out["daily_to_base"].replace(0, np.nan)
    else:
        out["daily_turnover_ratio"] = np.nan

    # ----- 三级阻力位（不含当日）-----
    # L1: 近 BREAKOUT_LOOKBACK_HIGH 日最高价（shift(1) 排除当日）
    out["level_l1"] = out["high"].shift(1).rolling(config.BREAKOUT_LOOKBACK_HIGH, min_periods=config.BREAKOUT_LOOKBACK_HIGH // 2).max()
    # L2: 近 BREAKOUT_LOOKBACK_PLATFORM 日最高价
    out["level_l2"] = out["high"].shift(1).rolling(config.BREAKOUT_LOOKBACK_PLATFORM, min_periods=config.BREAKOUT_LOOKBACK_PLATFORM // 2).max()
    # L3: MA60
    out["level_l3"] = out["ma60"]

    # ----- 突破判定（各级独立，取最高级别作为 breakout_level）-----
    margin = config.BREAKOUT_MIN_MARGIN
    above_l1 = (out["close"] > out["level_l1"] * (1 + margin)) & out["level_l1"].notna()
    above_l2 = (out["close"] > out["level_l2"] * (1 + margin)) & out["level_l2"].notna()
    above_l3 = (out["close"] > out["level_l3"] * (1 + margin)) & out["level_l3"].notna()
    brk_l1 = above_l1 & ~above_l1.shift(1, fill_value=False)
    brk_l2 = above_l2 & ~above_l2.shift(1, fill_value=False)
    brk_l3 = above_l3 & ~above_l3.shift(1, fill_value=False)
    if config.REQUIRE_L1_OR_L2:
        brk_l3 = pd.Series(False, index=out.index)

    out["breakout_l1"] = brk_l1
    out["breakout_l2"] = brk_l2 & ~brk_l1  # L2 仅在未同时突破 L1 时计为 L2
    out["breakout_l3"] = brk_l3 & ~brk_l1 & ~brk_l2
    out["breakout_any"] = brk_l1 | brk_l2 | brk_l3

    # breakout_level：3=L1 / 2=L2 / 1=L3 / 0=未突破
    out["breakout_level"] = np.select(
        [brk_l1, brk_l2 & ~brk_l1, brk_l3 & ~brk_l1 & ~brk_l2],
        [3, 2, 1],
        default=0,
    )

    # 突破幅度（%）：相对所突破级别关键位的超越幅度
    breakout_ref = np.select(
        [brk_l1, brk_l2 & ~brk_l1, brk_l3 & ~brk_l1 & ~brk_l2],
        [out["level_l1"], out["level_l2"], out["level_l3"]],
        default=np.nan,
    )
    out["breakout_margin"] = np.where(
        pd.notna(breakout_ref) & (breakout_ref > 0),
        (out["close"] / breakout_ref - 1) * 100,
        0.0,
    )

    # ----- 近期假突破过滤：近 N 日是否曾「突破 L1/L2 后 3 日内收盘跌回关键位下方」-----
    # 事件锚定在突破日对应级别的关键位（而非后续交易日的动态关键位）：突破日 j
    # 之后 1~CONFIRM_DAYS 个交易日内收盘价跌回 anchor 下方 → 记一次失败事件（发生于 k）；
    # 该事件在区间 [k, j+LOOKBACK] 内抑制新信号（含两端，与原三重循环逐日口径等价：
    # 原条件 j∈[i-LOOKBACK, i) 且 k≤i 且 i-k<LOOKBACK ⟺ i∈[k, j+LOOKBACK]）。
    # L2 失败也必须纳入冷却窗口：否则平台突破失败后仍可能在短期内重复发出信号。
    # 向量化实现：突破日逐一定位失败日（通常 <10 个），用差分数组标记抑制区间，
    # 替代原 O(n×LOOKBACK×CONFIRM_DAYS) 三重 Python 循环（全市场扫描的主要 CPU 热点）。
    _broke_l1 = brk_l1.to_numpy(dtype=bool)
    _broke_l2 = (brk_l2 & ~brk_l1).to_numpy(dtype=bool)
    _broke = _broke_l1 | _broke_l2
    _anchor_l1 = out["level_l1"].to_numpy(dtype=float)
    _anchor_l2 = out["level_l2"].to_numpy(dtype=float)
    _close_arr = out["close"].to_numpy(dtype=float)
    _n = len(out)
    _lookback = config.FAILED_BREAKOUT_LOOKBACK
    _confirm = config.FAILED_BREAKOUT_CONFIRM_DAYS
    _diff = np.zeros(_n + 1, dtype=float)
    for _j in np.flatnonzero(_broke):
        _a = _anchor_l1[_j] if _broke_l1[_j] else _anchor_l2[_j]
        if np.isnan(_a):
            continue
        _end = min(_n, _j + _confirm + 1)
        for _k in range(_j + 1, _end):
            if _close_arr[_k] < _a:
                _diff[_k] += 1
                _diff[min(_n, _j + _lookback + 1)] -= 1
                break
    out["recent_failed_breakout"] = pd.Series(np.cumsum(_diff)[:_n] > 0, index=out.index)

    # ----- 平台整理判定 -----
    # 近 PLATFORM_LOOKBACK 日振幅（不含当日，用 shift(1) 避免当日突破被计入平台）
    platform_high = out["high"].shift(config.PLATFORM_SHIFT).rolling(config.PLATFORM_LOOKBACK).max()
    platform_low = out["low"].shift(config.PLATFORM_SHIFT).rolling(config.PLATFORM_LOOKBACK).min()
    out["platform_range"] = np.where(
        platform_low > 0,
        (platform_high - platform_low) / platform_low * 100,
        np.nan,
    )

    # 平台期收盘价离散度（tightness）：std/mean，越小表示整理越充分。
    # 用 shift(PLATFORM_SHIFT=3) 避开突破 bar 污染：突破本身会抬高近几日波动，
    # 若只用 shift(1)，多日突破行情下「短窗 ATR / 长窗 ATR」恒 >1，compression_quality
    # 归零会把 volume_br_score 整体乘零（2026-09 合成数据测试暴露的问题）。
    # 改为对平台期收盘价取 std/mean，且窗口右端点固定在 shift(3) 处，
    # 确保度量的是「突破发生前」的整理质量，而非突破后的波动。
    _platform_close = out["close"].shift(config.PLATFORM_SHIFT)
    _platform_mean = _platform_close.rolling(config.PLATFORM_LOOKBACK, min_periods=config.PLATFORM_LOOKBACK // 2).mean()
    _platform_std = _platform_close.rolling(config.PLATFORM_LOOKBACK, min_periods=config.PLATFORM_LOOKBACK // 2).std()
    out["platform_tightness"] = np.where(_platform_mean > 0, _platform_std / _platform_mean, np.nan)
    # 保留 atr_compression 字段供诊断展示，但不再参与评分/否决
    atr_short = out["atr"].shift(config.PLATFORM_SHIFT).rolling(config.ATR_COMPRESSION_LOOKBACK_SHORT, min_periods=config.ATR_COMPRESSION_LOOKBACK_SHORT // 2).mean()
    atr_long = out["atr"].shift(config.PLATFORM_SHIFT).rolling(config.ATR_COMPRESSION_LOOKBACK_LONG, min_periods=config.ATR_COMPRESSION_LOOKBACK_LONG // 2).mean()
    out["atr_compression"] = np.where(atr_long > 0, atr_short / atr_long, np.nan)

    # ----- 趋势背景 -----
    ma20_prev = out["ma20"].shift(config.MA20_TREND_LOOKBACK)
    out["ma20_slope"] = (out["ma20"] - ma20_prev) / ma20_prev.replace(0, np.nan)
    ma60_prev = out["ma60"].shift(config.MA60_TREND_LOOKBACK)
    out["ma60_slope"] = (out["ma60"] - ma60_prev) / ma60_prev.replace(0, np.nan)
    out["ma20_rising"] = out["ma20_slope"] >= config.MA20_TREND_MIN_SLOPE_BREAKOUT
    out["ma60_ok"] = out["ma60_slope"] >= config.MA60_TREND_MIN_SLOPE
    out["close_above_ma20"] = out["close"] >= out["ma20"] * (1 - config.MA20_STAND_TOLERANCE)
    out["bull_alignment"] = (out["close"] > out["ma20"]) & (out["ma20"] > out["ma60"])

    # ----- K 线形态 -----
    body = (out["close"] - out["open"]).abs()
    full_range = (out["high"] - out["low"]).replace(0, np.nan)
    out["body_ratio"] = body / full_range
    out["is_bullish_candle"] = out["close"] > out["open"]
    out["close_to_high"] = out["close"] / out["high"].replace(0, np.nan)

    # =========================================================================
    # 评分维度（合计 100）
    # =========================================================================

    # ----- W_BREAKOUT=35：突破级别与突破幅度（加法混合，替代旧纯乘法）-----
    # 旧实现 level_factor × margin_factor：L2/L3 的级别系数（0.75/0.5）把幅度分压得
    # 极低（常规 1-2% 幅度下 L2 仅 ~4 分），B 级 60 准入下 L2/L3 事实上无法通过，
    # 等于只推 L1，信号稀疏。改为 0.55×级别 + 0.45×幅度 的加法混合：
    # L1 深度突破仍最高（~30 分），L2 常规突破 ~18 分，配合其他维度可达成 B 级准入。
    level_factor = out["breakout_level"].map({3: 1.0, 2: 0.75, 1: 0.5}).fillna(0.0)
    margin_factor = _smoothstep(out["breakout_margin"], config.BREAKOUT_MIN_MARGIN * 100, 5.0)
    out["breakout_score"] = config.W_BREAKOUT * (0.55 * level_factor + 0.45 * margin_factor)

    # ----- W_VOLUME_BR=25：放量倍数 + 突破前整理充分度（加权加法，避免单维度归零拖垮整体）-----
    # 旧实现用乘法 vol_quality × compression_quality，多日突破行情下 compression_quality
    # 恒为 0（突破本身抬高 ATR），导致 volume_br_score 整体归零（合成数据测试暴露）。
    # 改为 0.7×放量质量 + 0.3×整理质量，任一维度缺失时另一维度仍可贡献分数。
    vol_ratio = out["daily_vol_ratio"]
    vol_up = _smoothstep(vol_ratio, config.VOLUME_BREAKOUT_MIN, config.VOLUME_BREAKOUT_PEAK)
    vol_down = _smoothstep(vol_ratio, config.VOLUME_BREAKOUT_PEAK, config.VOLUME_BREAKOUT_MAX)
    vol_quality = pd.Series(np.where(vol_ratio <= config.VOLUME_BREAKOUT_PEAK, vol_up, 1 - vol_down), index=out.index).fillna(0)
    # 整理质量：平台期收盘价 std/mean 越小分越高（能量聚集充分）
    tightness_quality = _inv_smoothstep(out["platform_tightness"], 0.005, config.MAX_PLATFORM_TIGHTNESS).fillna(0.5)
    out["volume_br_score"] = config.W_VOLUME_BR * (0.7 * vol_quality + 0.3 * tightness_quality)

    # ----- W_PATTERN=15：平台振幅越小分越高 -----
    out["pattern_score"] = config.W_PATTERN * _inv_smoothstep(out["platform_range"], 5.0, config.MAX_PLATFORM_RANGE * 100).fillna(0)

    # ----- W_TREND_BR=15：MA20 斜率 + MA60 斜率 + 多头排列 -----
    ma20_quality = _smoothstep(out["ma20_slope"], 0.0, 0.03).fillna(0)
    ma60_quality = _smoothstep(out["ma60_slope"], -0.01, 0.02).fillna(0)
    alignment_quality = out["bull_alignment"].astype(float)
    out["trend_br_score"] = config.W_TREND_BR * (0.5 * ma20_quality + 0.3 * ma60_quality + 0.2 * alignment_quality)

    # ----- W_MOMENTUM_BR=10：MACD 金叉/柱体放大 + KDJ 未高位 + RSI 未超买 -----
    macd_mom = (out["macd_histogram"].diff() > 0).astype(float)
    macd_gold = out["macd_golden_cross"].astype(float)
    kdj_ok = ((out["kdj_k"] > out["kdj_d"]) & (out["kdj_k"] <= config.KDJ_K_MAX_BREAKOUT)).astype(float)
    rsi_ok = (out["rsi14"].shift(1) <= config.DAILY_RSI_ENTRY_MAX_BREAKOUT).astype(float)
    out["momentum_br_score"] = config.W_MOMENTUM_BR * (
        0.4 * np.maximum(macd_mom, macd_gold) + 0.3 * kdj_ok + 0.3 * rsi_ok
    )

    # ----- 日线综合评分 -----
    out["daily_score"] = (
        out["breakout_score"] + out["volume_br_score"] + out["pattern_score"] +
        out["trend_br_score"] + out["momentum_br_score"]
    ).fillna(0).clip(0, 100).round(1)

    return out


# ===========================================================================
# 交易计划：止损取技术位与固定风险上限中更高者；止盈按固定目标与 ATR 目标取较近者
# ===========================================================================

def compute_breakout_risk_reward(
    entry_price: float,
    breakout_ref: float,
    config: VolumeBreakoutConfig,
    atr: Optional[float] = None,
) -> dict:
    """生成突破交易计划，并保证止损不会扩大到固定风险上限之外。"""
    # 技术位止损：突破位 − 1×ATR
    if atr is not None and atr > 0 and breakout_ref > 0:
        tech_stop = breakout_ref - config.ATR_STOP_MULT_BREAKOUT * atr
    else:
        tech_stop = None
    # 固定止损：现价 × (1 − 6%)
    fixed_stop = entry_price * (1 - config.FIXED_STOP_LOSS_PCT_BREAKOUT / 100)
    # 取较大值才是更紧的止损，并且保证固定止损所表达的最大亏损上限。
    # 原先取 min 会在突破位远低于现价时把止损放到固定止损下方，
    # 实际风险超过配置的 6%。
    # 技术位若已高于/等于执行价，说明突破位距离执行价不足以形成有效
    # 的下方保护，不能把止损夹在入场价附近，否则四舍五入后会出现
    # “止损=入场价”和虚假的超高风险收益比；此时回退固定止损。
    if tech_stop is not None and tech_stop < entry_price:
        stop_loss = max(tech_stop, fixed_stop)
    else:
        stop_loss = fixed_stop
    fixed_take = entry_price * (1 + config.FIXED_TAKE_PROFIT_PCT_BREAKOUT / 100)
    atr_take = entry_price + (atr * config.ATR_TAKE_PROFIT_MULT if atr is not None and atr > 0 else float("inf"))
    take_profit = min(fixed_take, atr_take)
    stop_loss = max(0.01, min(stop_loss, entry_price * (1 - 1e-6)))
    stop_loss = round(stop_loss, 2)
    # 保证展示值在两位小数精度下仍严格低于入场价。
    if stop_loss >= round(entry_price, 2):
        stop_loss = max(0.01, round(entry_price, 2) - 0.01)
    risk = entry_price - stop_loss
    reward = take_profit - entry_price
    rr_ratio = reward / risk if risk > 0 else 0.0
    return {
        "stop_loss": stop_loss,
        "take_profit": round(take_profit, 2),
        "rr_ratio": round(rr_ratio, 2),
    }


# ===========================================================================
# 评估函数：七层漏斗，返回 (Signal, reason_code)
# ===========================================================================

def evaluate_breakout(
    daily_df: Optional[pd.DataFrame],
    code: str = "",
    name: str = "",
    config: Optional[VolumeBreakoutConfig] = None,
    market_env: Optional[dict] = None,
    fund_data: Optional[dict] = None,
    volatile_out: Optional[list] = None,
    latest_trade_date: Optional[str] = None,
    val_context: Optional[dict] = None,
    index_df: Optional[pd.DataFrame] = None,
) -> tuple[Optional[BreakoutSignal], str]:
    """评估单只股票是否满足放量突破入场条件。

    返回 (signal, reason)：
    - reason == "PASS"：signal 为 BreakoutSignal 实例
    - reason 以 "FAIL_" 开头：signal 为 None，reason 为失败归因码

    volatile_out：可选 list，FAIL_VOLATILE 的个股会以明细 dict 追加进去（供 CI 日志
    逐只留档，**不发送飞书**）。注意本策略的波动率检查位于评分定级之前，
    因此其中的标的只保证「突破/量能/形态/趋势」各层已过，评分等级尚未校验。

    val_context：行业相对估值上下文，仅在 quality_value 委派路径下透传给
    evaluate_quality_value；technical（独立突破）路径忽略。
    index_df：沪深300日线，仅在 quality_value 委派路径下透传给
    evaluate_quality_value（用于近60日相对强度）；technical 路径忽略。
    """
    if config is None:
        config = VolumeBreakoutConfig()
    if config.RECOMMENDATION_MODE == "quality_value":
        base, reason = evaluate_quality_value(daily_df, code, name, config, market_env,
                                              fund_data, latest_trade_date, val_context=val_context,
                                              index_df=index_df, volatile_out=volatile_out)
        if base is None:
            return None, reason
        # 突破仅作为统一候选池内的标签，不另设荐股资格或排序权重。
        technical_config = VolumeBreakoutConfig(**{**asdict(config), "RECOMMENDATION_MODE": "technical"})
        breakout, _ = evaluate_breakout(daily_df, code, name, technical_config, market_env,
                                        None, latest_trade_date=latest_trade_date)
        extra = {}
        if breakout is not None:
            if "放量突破" not in base.signals_hit.split(","):
                base.signals_hit += ",放量突破"
            extra = {k: getattr(breakout, k) for k in ("breakout_level", "breakout_margin", "platform_range", "avg_amount")}
        return BreakoutSignal(**base.to_dict(), **extra), "PASS"
    regime = (market_env or {}).get("regime", "unknown")
    eff_regime = _effective_regime(regime, config)
    grade_boost = config.BEAR_GRADE_BOOST_BREAKOUT if eff_regime == "bear" else 0.0

    # 层 1：基本面防雷
    if not check_fundamentals(fund_data, config, code=code, name=name):
        return None, "FAIL_FUND"

    # 层 2：数据 & 流动性
    if daily_df is None or daily_df.empty:
        return None, "FAIL_DATA"
    if "amount" in daily_df.columns and len(daily_df) >= 20:
        _avg_amt = pd.to_numeric(daily_df["amount"], errors="coerce").tail(20).mean()
        if pd.isna(_avg_amt):
            return None, "FAIL_DATA"
        if float(_avg_amt) < config.MIN_AMOUNT:
            return None, "FAIL_LIQUIDITY"

    # 计算全部信号
    out = compute_breakout_signals(daily_df, config)
    if out is None or out.empty:
        return None, "FAIL_DATA"

    # 层 2.5：停牌缺口（与抄底策略共用 has_halt_gap）。突破判定依赖 level_l1/l2（60/20 日
    # 最高价）、platform_range（20 日振幅）与 daily_vol_ratio（20 日均量），K 线跨停牌区间时
    # 这些窗口全部混入停牌前数据——「突破 60 日新高」可能是突破 5 个月前的复权高点，
    # 量比也可能把复牌爆量与停牌前死量做比值，判定失去意义。
    if has_halt_gap(out, config):
        return None, "FAIL_HALT_GAP"

    d_last = out.iloc[-1]
    # 核心行情字段缺失时不应继续以默认值计算并放行。尤其是 amount、
    # pct_chg 缺失会绕过成交额/涨幅过滤，造成“无量突破”或无法复核的信号。
    _required_last = ("date", "open", "high", "low", "close", "volume", "amount", "pct_chg")
    if any(col not in d_last.index or pd.isna(d_last[col]) for col in _required_last):
        return None, "FAIL_DATA"
    day = pd.to_datetime(d_last["date"], errors="coerce")
    if pd.isna(day):
        return None, "FAIL_DATA"
    if config.REQUIRE_FRESH_DAILY and latest_trade_date is not None:
        benchmark = pd.to_datetime(latest_trade_date, errors="coerce")
        if pd.isna(benchmark) or day.date() != benchmark.date():
            return None, "FAIL_STALE"
    values = pd.to_numeric(d_last[list(_required_last[1:])], errors="coerce")
    if not np.isfinite(values.to_numpy(dtype=float)).all():
        return None, "FAIL_DATA"
    if any(float(d_last[c]) <= 0 for c in ("open", "high", "low", "close", "volume", "amount")):
        return None, "FAIL_DATA"
    if "tradestatus" in d_last and str(d_last["tradestatus"]) not in ("1", "1.0"):
        return None, "FAIL_DATA"
    if len(out) < 2 or not np.isfinite(pd.to_numeric(out.iloc[-2].get("close"), errors="coerce")):
        return None, "FAIL_DATA"
    last_close = float(d_last["close"])

    # 层 3.1：突破判定
    if not bool(d_last.get("breakout_any", False)):
        return None, "FAIL_NO_BREAKOUT"
    breakout_level = int(d_last.get("breakout_level", 0))
    breakout_margin = float(d_last.get("breakout_margin", 0))

    # 层 3.2：放量确认
    vol_ratio = d_last.get("daily_vol_ratio")
    if vol_ratio is None or pd.isna(vol_ratio) or float(vol_ratio) < config.VOLUME_BREAKOUT_MIN:
        return None, "FAIL_VOL_INSUFFICIENT"
    if float(vol_ratio) > config.VOLUME_BREAKOUT_MAX:
        return None, "FAIL_CLIMAX_VOL"
    amount_today = d_last.get("amount")
    if amount_today is None or pd.isna(amount_today) or float(amount_today) < config.MIN_BREAKOUT_AMOUNT:
        return None, "FAIL_AMOUNT_INSUFFICIENT"
    # 自适应量能指标依赖完整的历史窗口。历史不足或指标缺失时必须失败关闭，
    # 不能因为 NaN 绕过过滤并把不可复核的突破升级为正式信号。
    volume_percentile = d_last.get("volume_percentile")
    amount_ratio = d_last.get("amount_ratio")
    if volume_percentile is None or pd.isna(volume_percentile):
        return None, "FAIL_VOL_INSUFFICIENT"
    if amount_ratio is None or pd.isna(amount_ratio):
        return None, "FAIL_VOL_INSUFFICIENT"
    volume_percentile = _num_or_none(volume_percentile)
    amount_ratio = _num_or_none(amount_ratio)
    if volume_percentile is None or amount_ratio is None:
        return None, "FAIL_DATA"
    if volume_percentile < config.MIN_VOLUME_PERCENTILE:
        return None, "FAIL_VOL_INSUFFICIENT"
    if amount_ratio < config.MIN_AMOUNT_RATIO:
        return None, "FAIL_VOL_INSUFFICIENT"

    # 层 3.3：K 线形态
    pct_chg = d_last.get("pct_chg")
    if pct_chg is not None and not pd.isna(pct_chg):
        pct_chg_val = float(pct_chg)
        if pct_chg_val > config.MAX_BREAKOUT_PCT:
            return None, "FAIL_CHASE"
        if pct_chg_val < config.MIN_BREAKOUT_PCT:
            return None, "FAIL_BREAKOUT_WEAK"
    open_today = d_last.get("open")
    prev_close = float(out.iloc[-2]["close"]) if len(out) >= 2 else None
    if (open_today is not None and not pd.isna(open_today) and prev_close and prev_close > 0
            and (float(open_today) / prev_close - 1) * 100 > config.MAX_GAP_UP_PCT):
        return None, "FAIL_GAP"
    body_ratio = d_last.get("body_ratio")
    is_bullish = bool(d_last.get("is_bullish_candle", False))
    if not is_bullish or (body_ratio is not None and not pd.isna(body_ratio) and float(body_ratio) < config.MIN_BODY_RATIO):
        return None, "FAIL_FAKE_BREAKOUT"
    close_to_high = d_last.get("close_to_high")
    if close_to_high is not None and not pd.isna(close_to_high) and float(close_to_high) < config.MIN_CLOSE_TO_HIGH:
        return None, "FAIL_FAKE_BREAKOUT"

    # 层 3.4：平台整理
    platform_range = d_last.get("platform_range")
    if platform_range is not None and not pd.isna(platform_range) and float(platform_range) > config.MAX_PLATFORM_RANGE * 100:
        return None, "FAIL_NO_PLATFORM"
    # 平台期收盘价离散度过大 → 整理不充分，否决。
    # 旧实现用 atr_compression ≤ 1.0 做硬门槛，但突破本身会抬高 ATR，多日突破行情下
    # 该比值恒 >1，正常突破被误杀（合成数据测试暴露）。改为 platform_tightness（std/mean），
    # 窗口右端点固定在 shift(PLATFORM_SHIFT) 处，度量的是「突破发生前」的整理质量。
    platform_tightness = d_last.get("platform_tightness")
    if platform_tightness is not None and not pd.isna(platform_tightness) and float(platform_tightness) > config.MAX_PLATFORM_TIGHTNESS:
        return None, "FAIL_PLATFORM_LOOSE"

    # 层 3.5：趋势背景
    if not bool(d_last.get("ma20_rising", False)):
        return None, "FAIL_TREND_DOWN"
    if not bool(d_last.get("ma60_ok", False)):
        return None, "FAIL_TREND_DOWN"
    if not bool(d_last.get("close_above_ma20", False)):
        return None, "FAIL_BELOW_MA20"

    # 层 3.6：近期假突破过滤
    if bool(d_last.get("recent_failed_breakout", False)):
        return None, "FAIL_RECENT_FAILED_BREAKOUT"

    # 层 3.7：RSI 上限（检查突破前一日，而非突破日）
    # 突破日单日大涨会把 RSI14 从 ~50 推到 80+（极度窄幅整理后甚至到 95+），
    # 这是突破信号的预期行为，不应拦截。改为检查 shift(1) 的 RSI：
    # 若突破前 RSI 已 >75，说明股票已连涨多日、突破偏晚（追高风险）；
    # 若突破前 RSI 正常、仅因突破日单日大涨而飙高，放行。
    rsi_prev = out.iloc[-2].get("rsi14") if len(out) >= 2 else None
    if rsi_prev is not None and not pd.isna(rsi_prev) and float(rsi_prev) > config.DAILY_RSI_ENTRY_MAX_BREAKOUT:
        return None, "FAIL_RSI_HIGH"

    # 层 3.8：KDJ / MACD 下限否决（荐股控制）
    # 这两个指标原先是纯评分项（W_MOMENTUM_BR=10），丢分不淘汰，对推荐与否没有
    # 约束力；此处补上硬闸门，只拦「动能连续走弱」与「KDJ 高位滞涨」，不要求金叉。
    # 刻意放在 RSI 层之后、波动率层之前：归因上属「日线技术入场质量」，
    # 与 FAIL_RSI_HIGH 同层可比，也与 volatile 观察池的语义（各层已过、仅 ATR 超限）不冲突。
    if getattr(config, "REQUIRE_BR_MACD_NOT_WEAK", True) \
            and not macd_not_deeply_weak(out, config.MACD_WEAK_DAYS, config.MACD_WEAK_HIST_PCT):
        return None, "FAIL_MACD_WEAK"
    if getattr(config, "REQUIRE_BR_KDJ_NOT_HIGH", True) \
            and not kdj_not_overheated(out, config.KDJ_K_HARD_MAX, config.KDJ_DEAD_CROSS_K):
        return None, "FAIL_KDJ_HIGH"

    # 层 4：波动率风控
    atr_val = d_last.get("atr")
    atr_val = float(atr_val) if atr_val is not None and not pd.isna(atr_val) else None
    if atr_val is not None and last_close > 0 and (atr_val / last_close * 100) > config.MAX_ATR_PCT_BREAKOUT:
        if volatile_out is not None:
            # 明细留档（并发 worker 线程直接 append，与抄底策略同一约定）。
            # 注意本策略的波动率层位于评分定级（层 5）之前，故其评分/等级仅作参考。
            _lvl_zh = {3: "L1·60日新高", 2: "L2·20日新高", 1: "L3·MA60"}.get(
                breakout_level, f"L{breakout_level}")
            _score = float(d_last.get("daily_score", 0))
            volatile_out.append(_build_volatile_row(
                code=code, name=name, date=d_last["date"], close=last_close,
                atr=atr_val, atr_pct=atr_val / last_close * 100,
                limit=config.MAX_ATR_PCT_BREAKOUT,
                score=_score, grade=_grade_from_score(_score - grade_boost, config),
                hits=(f"突破 {_lvl_zh} +{breakout_margin:.2f}%、放量 {float(vol_ratio):.2f}×、"
                      f"K线形态与平台整理达标、MA20 上行"),
                rsi=d_last.get("rsi14"), vol_ratio=vol_ratio,
            ))
        return None, "FAIL_VOLATILE"

    # 层 5：综合评分与等级
    daily_score = float(d_last.get("daily_score", 0))
    base_grade = _grade_from_score(daily_score - grade_boost, config)
    min_grade = config.MIN_PASS_GRADE if config.MIN_PASS_GRADE in _GRADE_ORDER else "B"
    if _GRADE_ORDER.index(base_grade) < _GRADE_ORDER.index(min_grade):
        return None, "FAIL_TECH"

    # 交易计划（rr_ratio 仅展示与落库：止损≤6%/止盈15% 下 RR≥2.5 恒成立，
    # 旧 MIN_RR_RATIO_BREAKOUT≥1.5 检查永不触发，已删除；波动率风控见层 4）
    breakout_ref_map = {3: d_last.get("level_l1"), 2: d_last.get("level_l2"), 1: d_last.get("level_l3")}
    breakout_ref = breakout_ref_map.get(breakout_level)
    breakout_ref = float(breakout_ref) if breakout_ref is not None and not pd.isna(breakout_ref) else last_close
    rr = compute_breakout_risk_reward(last_close, breakout_ref, config, atr_val)

    _amt = pd.to_numeric(out["amount"], errors="coerce").tail(20).mean() if "amount" in out.columns else float("nan")

    fund_status, fund_tags = _fund_verify_state(fund_data)
    if latest_trade_date is None:
        fund_tags.append("market_date")
    sig = BreakoutSignal(
        tier="pending", fund_status=fund_status, weekly_status="unverified",
        missing_tags=",".join(fund_tags),
        code=code, name=name,
        date=pd.to_datetime(d_last["date"]).strftime("%Y-%m-%d"),
        close=round(last_close, 2),
        score=round(daily_score, 1),
        grade=base_grade,
        daily_score=round(daily_score, 1),
        rsi=round(float(d_last.get("rsi14", 50)), 1),
        rsi7=0.0,  # 突破策略不使用 RSI7，占位保持 Signal 字段兼容
        rsi21=0.0,
        vol_ratio=round(float(d_last.get("daily_vol_ratio", 0)), 2),
        turnover_ratio=round(float(d_last.get("daily_turnover_ratio", 0)), 2),
        avg_amount=round(float(_amt) / 1e4, 1) if pd.notna(_amt) else 0.0,
        stop_loss=rr["stop_loss"], take_profit=rr["take_profit"], rr_ratio=rr["rr_ratio"],
        market_env=regime,
        has_divergence=False,  # 突破策略不使用底背离字段，占位保持 Signal 字段兼容
        breakout_level=breakout_level,
        breakout_margin=round(breakout_margin, 2),
        platform_range=round(float(d_last.get("platform_range", 0)), 2),
    )
    return sig, "PASS"


# ===========================================================================
# 编排函数：main_breakout
# ===========================================================================

def describe_breakout(row: dict) -> str:
    """把一条放量突破推荐格式化为飞书卡片文本（notify/feishu.py 按 strategy 选择调用）。

    止损/止盈不再挤在指标行里，统一由 describe_trade_plan 渲染成「操作计划」块，
    与优质低估低位入口共用同一格式（含建仓区间与建议持有周期）。标题用「正式推荐」
    而非「候选」——能进卡片的必然已是 tier=formal（feishu 渲染前已过滤）。
    """
    if row.get("weekly_status") == "not_required":
        from src.bottom_fishing_strategy import describe
        return describe(row)
    lvl = {3: "L1·60日新高", 2: "L2·20日新高", 1: "L3·MA60"}.get(
        int(row.get("breakout_level") or 0), "-")
    lines = [
        f"**{row.get('name', '')} {row.get('code', '')}** · 正式推荐 · 放量突破",
        f"评分: {_fmt_cell(row.get('score'))} ({row.get('grade') or '-'}级)"
        f" | 突破: {lvl} +{_fmt_cell(row.get('breakout_margin'))}%"
        f" | 收盘: {_fmt_cell(row.get('close'))}",
        f"量比: {_fmt_cell(row.get('vol_ratio'))}"
        f" | RSI14: {_fmt_cell(row.get('rsi'))}"
        f" | 平台振幅: {_fmt_cell(row.get('platform_range'))}%"
        f" | 日均额: {_fmt_cell(row.get('avg_amount'))}万"
        f" | 市场: {row.get('market_env') or '-'}",
    ]
    lines.extend(describe_trade_plan(row))
    lines.extend(_breakout_brief_lines(row, lvl))
    return "\n".join(lines)


def _breakout_brief_lines(row: dict, lvl: str) -> list[str]:
    """P5 决策简报（突破版）：由已有的突破/量能/形态字段就地组装，纯展示不落库。

    突破策略走 technical 模式，不经过 evaluate_quality_value 的简报组装；这里用 row 里
    已有的 breakout_level/margin/vol_ratio/platform_range/stop_loss 拼一份等价简报，
    让两套策略的卡片都具备「看多/风险/失效/信心/今日触发」五段结构。
    """
    margin = _num_or_none(row.get("breakout_margin"))
    vol_ratio = _num_or_none(row.get("vol_ratio"))
    platform = _num_or_none(row.get("platform_range"))
    stop = _num_or_none(row.get("stop_loss"))
    close = _num_or_none(row.get("close"))
    rsi = _num_or_none(row.get("rsi"))
    grade = str(row.get("grade") or "")

    bull = [f"放量突破 {lvl}" + (f" +{margin:.2f}%" if margin is not None else "")]
    if vol_ratio is not None:
        bull.append(f"量比 {vol_ratio:.2f}×（资金介入）")
    if platform is not None:
        bull.append(f"突破前平台振幅 {platform:.1f}%（整理充分）")
    bull.append("MA20 上行、站上均线（趋势背景多头）")

    bear: list[str] = []
    if rsi is not None and rsi >= 75:
        bear.append(f"RSI14 {rsi:.0f} 偏高，突破日追高、次日回踩风险大")
    if margin is not None and margin >= 5:
        bear.append("单日突破幅度偏大，注意消息驱动/一日游")
    bear.append("突破次日若跌回关键位即为假突破，需按失效价果断离场")
    if str(row.get("market_env") or "") == "bear":
        bear.append("市场偏空，突破胜率下降（熊市本策略默认空仓）")

    if stop is not None and close is not None and close > 0:
        invalidation = f"止损 {stop:.2f}（约 {(stop / close - 1) * 100:.1f}%）；跌回突破位下方视为假突破离场"
    else:
        invalidation = "跌回突破关键位下方视为假突破离场"

    conviction = {"A": "高", "B": "中", "C": "中低", "D": "低（观察）"}.get(grade, "中")
    why_today = f"今日放量突破 {lvl}" + (f"，涨幅 {margin:.2f}%" if margin is not None else "")

    return [
        f"**信心:** {conviction}",
        f"**今日触发:** {why_today}",
        f"**看多:** {'；'.join(bull)}",
        f"**风险:** {'；'.join(bear)}",
        f"**失效:** {invalidation}",
    ]


def main_breakout(
    config: Optional[VolumeBreakoutConfig] = None,
    cache: Optional[CacheManager] = None,
    volatile_out: Optional[list] = None,
    pending_out: Optional[list] = None,
) -> Optional[pd.DataFrame]:
    """放量突破选股主流程。骨架与 bottom_fishing_strategy.main 对齐：
    初始化数据源 → 市场环境 → 股票池 → 并发筛选（漏斗日志） → 决赛圈周线确认 → 排序截取。

    volatile_out：可选 list，传出「波动率风控否决」明细（突破/量能/形态/趋势各层已过、
    仅 ATR 超限），仅用于 CI 日志留档（**不发送飞书**），不落库、不参与追踪与归因。
    """
    if config is None:
        config = VolumeBreakoutConfig()
    if cache is None:
        cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)

    logger.info("放量突破策略启动 | Python %s | AkShare备用: %s | 缓存目录: %s",
                sys.version.split()[0], "可用" if _AK_AVAILABLE else "未安装", config.CACHE_DIR)

    if not _bs_login(max_retry=5):
        if _AK_AVAILABLE:
            logger.warning("Baostock 登录失败（已重试 5 次），本次运行降级为 AkShare 备用数据源")
            _bs_state["circuit_open"] = True
        else:
            logger.error("Baostock 登录失败，且未安装 AkShare，无可用数据源")
            return None
    else:
        logger.info("数据源: Baostock（备用: %s）", "AkShare" if _AK_AVAILABLE else "未安装 akshare，无备用")

    try:
        market_env = get_market_environment(config, cache)
        logger.info("市场环境: %s", market_env.get("description", "unknown"))

        index = get_index_daily(config, cache)
        latest_day = (pd.to_datetime(index.iloc[-1]["date"], errors="coerce")
                      if index is not None and not index.empty and "date" in index else pd.NaT)
        latest = latest_day.strftime("%Y-%m-%d") if pd.notna(latest_day) else None
        if market_crash_halt(index, config) is not None:
            logger.warning("指数急跌触发市场熔断，突破策略暂停推荐")
            return None
        if config.RECOMMENDATION_MODE == "quality_value":
            return _screen_quality_pool(config, cache, market_env, latest,
                                         pending_out, evaluator=evaluate_breakout,
                                         index_df=index, volatile_out=volatile_out)

        regime_raw = market_env.get("regime", "unknown")
        regime_eff = _effective_regime(regime_raw, config)
        if regime_eff != regime_raw:
            logger.warning("市场环境 unknown（指数数据缺失），按熊市保守处理：推荐上限收缩为 %d",
                           config.BEAR_MAX_PICKS_BREAKOUT)

        # 推荐数量：牛 5 / 中性 3 / 熊 0（直接空仓）
        if regime_eff == "bull":
            max_picks = config.MAX_PICKS
        elif regime_eff == "neutral":
            max_picks = config.NEUTRAL_MAX_PICKS
        else:  # bear
            max_picks = config.BEAR_MAX_PICKS_BREAKOUT
        logger.info("市场环境 %s：推荐数量上限 %d", regime_eff, max_picks)

        if max_picks == 0:
            logger.info("熊市环境，突破策略直接空仓（BEAR_MAX_PICKS_BREAKOUT=0），本次运行跳过筛选")
            return None

        stock_list = get_stock_list(config, cache)
        if not stock_list:
            logger.warning("无法获取股票列表，本次运行终止")
            return None
        logger.info("待筛选股票数: %d", len(stock_list))

        fetch_stats["bs_ok"] = fetch_stats["ak_ok"] = fetch_stats["fail"] = 0
        signals: list[dict] = []
        total = len(stock_list)
        stats = {
            "total": total, "error": 0, "fail_data": 0, "fail_liq": 0, "fail_halt": 0,
            "fail_fund": 0,
            "fail_breakout": 0, "fail_vol": 0, "fail_pattern": 0, "fail_trend": 0,
            "fail_fake": 0, "fail_chase": 0, "fail_rsi": 0, "fail_volatile": 0,
            "fail_tech": 0, "pass": 0,
            # 层 3.8 新增：KDJ/MACD 下限否决。独立计数便于复核「动能闸门是否已成
            # 日线淘汰主因」——两个指标原先只扣分不淘汰，现在能淘汰了，必须可观测。
            "fail_macd_weak": 0, "fail_kdj_high": 0,
        }

        def _screen_one(stock: dict) -> tuple[Optional[BreakoutSignal], str]:
            code, name = stock["code"], stock["name"]
            try:
                daily_df = get_daily_data(code, config, cache)
                if daily_df is None:
                    return None, "FAIL_DATA"
                fund_data = get_fundamentals(code, cache, config)
                return evaluate_breakout(daily_df, code, name, config, market_env, fund_data,
                                         volatile_out=volatile_out, latest_trade_date=latest)
            except Exception as e:
                logger.debug("%s(%s) 筛选异常: %s", name, code, e)
                return None, "ERROR"

        # 并发筛选（共享骨架：线程池 + 心跳看门狗 + 时间预算，与抄底策略同一模板）
        screen_start = time.time()
        results, processed, time_budget_hit = run_concurrent_screen(stock_list, _screen_one, config, logger)
        screen_minutes = (time.time() - screen_start) / 60

        for sig, reason in results:
            if reason == "PASS" and sig is not None:
                stats["pass"] += 1
                signals.append(sig.to_dict())
                logger.info("[通过] %s(%s) 评分 %.1f %s级 L%d 突破幅度 %.2f%%",
                            sig.name, sig.code, sig.score, sig.grade, sig.breakout_level, sig.breakout_margin)
            elif reason == "FAIL_FUND": stats["fail_fund"] += 1
            elif reason in ("FAIL_DATA", "FAIL_STALE"): stats["fail_data"] += 1
            elif reason == "FAIL_LIQUIDITY": stats["fail_liq"] += 1
            elif reason == "FAIL_HALT_GAP": stats["fail_halt"] += 1
            elif reason == "FAIL_NO_BREAKOUT": stats["fail_breakout"] += 1
            elif reason in ("FAIL_VOL_INSUFFICIENT", "FAIL_CLIMAX_VOL", "FAIL_AMOUNT_INSUFFICIENT"): stats["fail_vol"] += 1
            elif reason in ("FAIL_NO_PLATFORM", "FAIL_PLATFORM_LOOSE"): stats["fail_pattern"] += 1
            elif reason in ("FAIL_TREND_DOWN", "FAIL_BELOW_MA20"): stats["fail_trend"] += 1
            elif reason == "FAIL_FAKE_BREAKOUT": stats["fail_fake"] += 1
            elif reason in ("FAIL_CHASE", "FAIL_GAP", "FAIL_BREAKOUT_WEAK"): stats["fail_chase"] += 1
            elif reason == "FAIL_RECENT_FAILED_BREAKOUT": stats["fail_chase"] += 1
            elif reason == "FAIL_RSI_HIGH": stats["fail_rsi"] += 1
            elif reason == "FAIL_MACD_WEAK": stats["fail_macd_weak"] += 1
            elif reason == "FAIL_KDJ_HIGH": stats["fail_kdj_high"] += 1
            elif reason == "FAIL_VOLATILE": stats["fail_volatile"] += 1
            elif reason == "FAIL_TECH": stats["fail_tech"] += 1
            elif reason == "ERROR": stats["error"] += 1

        # 漏斗日志
        logger.info("=" * 50)
        logger.info("[FUNNEL] 放量突破漏斗数据分析   筛选耗时 %.1f 分钟", screen_minutes)
        logger.info("=" * 50)
        if time_budget_hit:
            logger.warning("[WARN] 因达到时间预算，以下漏斗仅统计已完成的 %d/%d 只", processed, total)
        logger.info("1. 初始有效股票池: %d 只 (完成处理 %d 只)", stats["total"], processed)
        _pass_data = processed - stats["fail_data"] - stats["fail_liq"] - stats["fail_halt"] - stats["error"]
        logger.info("2. 数据 & 流动性达标: %d 只 (数据缺失 %d, 僵尸股 %d, 停牌缺口 %d, 异常 %d)",
                    _pass_data,
                    stats["fail_data"], stats["fail_liq"], stats["fail_halt"], stats["error"])
        logger.info("3. 基本面防雷通过: %d 只 (淘汰 %d)",
                    _pass_data - stats["fail_fund"],
                    stats["fail_fund"])
        # 各层通过数 = pass + 该层之后所有层的淘汰数（层序见 evaluate_breakout）：
        # 3.1 突破 → 3.2 量能 → 3.3 K线形态 → 3.4 平台 → 3.5 趋势 → 3.6 假突破
        # → 3.7 RSI → 3.8 KDJ/MACD 动能下限 → 4 波动率 → 5 评分定级
        _mom = stats["fail_macd_weak"] + stats["fail_kdj_high"]
        logger.info("4. 突破 & 量能确认: %d 只 (未突破 %d, 量能不足/天量/额不足 %d)",
                    stats["pass"] + stats["fail_pattern"] + stats["fail_trend"] + stats["fail_fake"] +
                    stats["fail_chase"] + stats["fail_rsi"] + _mom + stats["fail_volatile"] + stats["fail_tech"],
                    stats["fail_breakout"], stats["fail_vol"])
        logger.info("5. 平台整理 & 趋势背景: %d 只 (无平台 %d, 趋势下行 %d)",
                    stats["pass"] + stats["fail_fake"] + stats["fail_chase"] + stats["fail_rsi"] +
                    _mom + stats["fail_volatile"] + stats["fail_tech"],
                    stats["fail_pattern"], stats["fail_trend"])
        logger.info("6. K线形态 & 假突破过滤: %d 只 (假突破 %d, 追高/跳空 %d)",
                    stats["pass"] + stats["fail_rsi"] + _mom + stats["fail_volatile"] + stats["fail_tech"],
                    stats["fail_fake"], stats["fail_chase"])
        logger.info("7. RSI & 动能下限 & 波动率 & 评分: %d 只 "
                    "(RSI过高 %d, MACD走弱 %d, KDJ高位 %d, 波动率超限 %d, 评分不达 %d)",
                    stats["pass"] + _mom + stats["fail_volatile"] + stats["fail_tech"],
                    stats["fail_rsi"], stats["fail_macd_weak"], stats["fail_kdj_high"],
                    stats["fail_volatile"], stats["fail_tech"])
        logger.info("日线取数来源: Baostock %d 只，AkShare 兜底 %d 只，双源均失败 %d 只",
                    fetch_stats["bs_ok"], fetch_stats["ak_ok"], fetch_stats["fail"])
        logger.info("=" * 50)

        # 波动率风控否决明细：逐只留档便于复核「为什么今天没推荐」（**不发送飞书**，
        # 通知只发通过全部筛选的正式推荐；不落库、不参与追踪）。
        if volatile_out:
            # 就地定序（技术分降序 → ATR% 降序），保证日志 Top-N 可复现
            volatile_out[:] = sort_volatile(volatile_out)
            log_volatile_rejects(volatile_out, logger)
        elif volatile_out is not None and not stats["fail_volatile"] and not stats["pass"]:
            # 波动率层零淘汰 + 无通过信号：拦截在上游层，直接点出主要拦截层
            _upstream = {
                "数据/流动性/停牌缺口": stats["fail_data"] + stats["fail_liq"] + stats["fail_halt"] + stats["error"],
                "基本面防雷": stats["fail_fund"],
                "突破形态/量能/趋势/RSI/动能下限/评分": (
                    stats["fail_breakout"] + stats["fail_vol"] + stats["fail_pattern"]
                    + stats["fail_trend"] + stats["fail_fake"] + stats["fail_chase"]
                    + stats["fail_rsi"] + stats["fail_macd_weak"] + stats["fail_kdj_high"]
                    + stats["fail_tech"]),
            }
            _layer = max(_upstream, key=lambda k: _upstream[k])
            if _upstream[_layer] > 0:
                logger.info("[VOLATILE] 波动率层本轮未淘汰任何标的（拦截发生在上游）："
                            "无通过信号，主要拦截层为「%s」%d 只", _layer, _upstream[_layer])

        if not signals:
            logger.info("未发现符合条件的放量突破信号")
            return None

        # 确定性排序
        df = pd.DataFrame(signals).sort_values(SORT_BY, ascending=SORT_ASC).reset_index(drop=True)

        # 候选只有完成终审才能升级 formal；缺项不占名额，继续向后补足。
        weekly_enabled = config.REQUIRE_WEEKLY_TREND or config.REQUIRE_WEEKLY_MACD_STABLE
        industry_map = get_stock_industry(config, cache) if config.USE_INDUSTRY_DEDUP else {}
        industry_used: dict[str, int] = {}
        confirmed = []
        for _, candidate in df.iterrows():
            if len(confirmed) >= max_picks:
                break
            row = candidate.to_dict()
            code = str(row["code"])
            row["tier"] = "pending"
            missing = [] if latest is not None else ["market_date"]
            fund = get_fundamentals(code, cache, config)
            fund = _fill_optional_fundamentals(code, dict(fund or {}), config)
            if not check_fundamentals(fund, config, code=code, name=str(row["name"])):
                continue
            row["fund_status"], fund_tags = _fund_verify_state(fund)
            missing.extend(fund_tags)
            verified = row["fund_status"] == "verified" and latest is not None
            row["weekly_status"] = "disabled"
            if weekly_enabled:
                wk = _fetch_weekly_dual(code, config)
                time.sleep(config.FETCH_DELAY)
                # 在数据充分性判断前去掉半成品周线，防止检查器缺数自动放行。
                wk = _drop_incomplete_weekly_bar(wk, config)
                if wk is not None and not wk.empty and "date" in wk:
                    dates = pd.to_datetime(wk["date"], errors="coerce")
                    if latest is not None:
                        cutoff = pd.Timestamp(latest)
                        if config.WEEKLY_REQUIRE_CLOSED_BAR:
                            cutoff -= pd.Timedelta(days=(cutoff.weekday() - 4) % 7)
                        wk = wk.loc[dates.notna() & (dates <= cutoff)].copy()
                data_ok = wk is not None and not wk.empty and "close" in wk and "date" in wk
                if data_ok:
                    closes = pd.to_numeric(wk["close"], errors="coerce")
                    dates = pd.to_datetime(wk["date"], errors="coerce")
                    data_ok = bool(np.isfinite(closes).all() and (closes > 0).all()
                                   and dates.notna().all() and not dates.duplicated().any())
                if data_ok and config.REQUIRE_WEEKLY_TREND:
                    data_ok = _weekly_trend_data_ok(wk, config)
                if data_ok and config.REQUIRE_WEEKLY_MACD_STABLE:
                    data_ok = _weekly_macd_data_ok(wk, config)
                if data_ok and latest is not None:
                    expected = pd.Timestamp(latest)
                    if config.WEEKLY_REQUIRE_CLOSED_BAR:
                        expected -= pd.Timedelta(days=(expected.weekday() - 4) % 7)
                    data_ok = _weekly_fresh_enough(wk, expected.strftime("%Y-%m-%d"))
                if data_ok:
                    if config.REQUIRE_WEEKLY_TREND and not check_weekly_trend(wk, config):
                        continue
                    if config.REQUIRE_WEEKLY_MACD_STABLE and not check_weekly_macd(wk, config):
                        continue
                    row["weekly_status"] = "confirmed"
                else:
                    row["weekly_status"] = "unverified"
                    missing.append("weekly")
                    verified = False
            row["missing_tags"] = ",".join(dict.fromkeys(missing))
            if not verified:
                if pending_out is not None:
                    pending_out.append(row)
                continue
            industry = industry_map.get(code, "") or ""
            if industry and industry_used.get(industry, 0) >= config.MAX_PICKS_PER_INDUSTRY:
                continue
            if industry:
                industry_used[industry] = industry_used.get(industry, 0) + 1
            row["tier"] = "formal"
            confirmed.append(row)
        df = pd.DataFrame(confirmed).reset_index(drop=True) if confirmed else df.iloc[0:0]
        logger.info("筛选完成，最终推荐 %d 只放量突破股票", len(df))
        return df

    finally:
        _bs_logout()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout, force=True)
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode == "screen":
        result = main_breakout()
        print(result.to_string() if result is not None else "未发现信号")
    elif mode == "full":
        result = main_breakout()
        if result is not None:
            print(f"共 {len(result)} 只放量突破信号")
    else:
        print(f"未知模式: {mode}，可选: screen / full")
