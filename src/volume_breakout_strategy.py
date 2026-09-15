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
   3.3 K 线形态：阳线实体占比 ≥ 0.5、收盘/最高 ≥ 0.97、涨幅 2%–7%、跳空 ≤ 3%
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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

# 复用抄底策略的数据层、市场环境、基本面、周线确认、Baostock 生命周期
from src.bottom_fishing_strategy import (
    StrategyConfig,
    CacheManager,
    Signal,
    _bs_login,
    _bs_logout,
    _bs_state,
    _AK_AVAILABLE,
    _effective_regime,
    _fetch_weekly_dual,
    _GRADE_ORDER,
    _grade_from_score,
    check_fundamentals,
    check_weekly_macd,
    check_weekly_trend,
    get_daily_data,
    get_fundamentals,
    get_market_environment,
    get_stock_list,
)

# Older bottom-fishing revisions do not expose the shared fetch counters. Keep
# the breakout module importable for offline backtests in that case.
try:
    from src.bottom_fishing_strategy import fetch_stats  # type: ignore
except ImportError:
    fetch_stats = {"bs_ok": 0, "ak_ok": 0, "fail": 0}

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
    MIN_RR_RATIO_BREAKOUT: float = 1.5       # 计划收益风险比下限
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
    W_BREAKOUT: float = 35.0                 # 突破强度
    W_VOLUME_BR: float = 25.0                # 量能质量
    W_PATTERN: float = 15.0                  # 平台整理
    W_TREND_BR: float = 15.0                 # 趋势背景
    W_MOMENTUM_BR: float = 10.0              # 动能确认

    # 动能组参数（突破版）
    KDJ_K_MAX_BREAKOUT: float = 80.0         # 突破日 KDJ K 值上限（比抄底 60 放宽）


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
    if len(df) < config.MIN_DAYS:
        return None

    out = df.copy().reset_index(drop=True)

    # 盘中运行时，数据源可能已经返回当天尚未收盘的日 bar。该 bar 的
    # high/low/volume/pct_chg 会继续变化，若参与突破判断会产生盘中漂移。
    # 15:00 前剔除日期为今天的最后一根；收盘后保留已完成 bar。
    if "date" in out.columns and not out.empty:
        _dates = pd.to_datetime(out["date"], errors="coerce")
        _now = datetime.now()
        if (_now.hour < 15 and pd.notna(_dates.iloc[-1])
                and _dates.iloc[-1].date() == _now.date()):
            out = out.iloc[:-1].reset_index(drop=True)
    if len(out) < config.MIN_DAYS:
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
    out["volume_percentile"] = out["volume"].rolling(config.ADAPTIVE_VOLUME_LOOKBACK + 1).apply(
        lambda x: float(np.mean(x[:-1] <= x[-1])), raw=True)
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

    # ----- 近期假突破过滤：近 N 日是否曾「突破 L1 后 3 日内收盘跌回 L1 下方」-----
    # 触发条件：某日 close > L1 × (1+margin)，但随后 3 日内出现 close < L1
    broke_l1 = brk_l1.astype(int)
    # 滚动窗口：近 FAILED_BREAKOUT_LOOKBACK 日内，存在「突破日 + 之后跌回日」配对
    failed_pattern = pd.Series(False, index=out.index)
    lookback = config.FAILED_BREAKOUT_LOOKBACK
    # 事件锚定在突破日的 L1，而不是把每个后续交易日的动态 L1
    # 当作回测基准。只有突破后 3 个交易日内收盘跌回该突破日 L1 下方，
    # 才算一次失败突破；该事件在随后 lookback 日内抑制新的信号。
    for i in range(len(out)):
        start = max(0, i - lookback)
        for j in range(start, i):
            if not broke_l1.iloc[j]:
                continue
            anchor = out["level_l1"].iloc[j]
            if pd.isna(anchor):
                continue
            # 仅检查突破后第 1~3 个交易日，且失败事件发生后 lookback
            # 天内才抑制；这样不会因“突破日距今很近”而缩短冷却期。
            end = min(len(out), j + config.FAILED_BREAKOUT_CONFIRM_DAYS + 1)
            for k in range(j + 1, end):
                if k <= i and out["close"].iloc[k] < float(anchor) and i - k < lookback:
                    failed_pattern.iloc[i] = True
                    break
            if failed_pattern.iloc[i]:
                break
    out["recent_failed_breakout"] = failed_pattern

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

    # ----- W_BREAKOUT=35：突破级别 × 突破幅度 -----
    level_factor = out["breakout_level"].map({3: 1.0, 2: 0.75, 1: 0.5}).fillna(0.0)
    margin_factor = _smoothstep(out["breakout_margin"], config.BREAKOUT_MIN_MARGIN * 100, 5.0)
    out["breakout_score"] = config.W_BREAKOUT * level_factor * margin_factor

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
) -> tuple[Optional[BreakoutSignal], str]:
    """评估单只股票是否满足放量突破入场条件。

    返回 (signal, reason)：
    - reason == "PASS"：signal 为 BreakoutSignal 实例
    - reason 以 "FAIL_" 开头：signal 为 None，reason 为失败归因码
    """
    if config is None:
        config = VolumeBreakoutConfig()
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

    d_last = out.iloc[-1]
    # 核心行情字段缺失时不应继续以默认值计算并放行。尤其是 amount、
    # pct_chg 缺失会绕过成交额/涨幅过滤，造成“无量突破”或无法复核的信号。
    _required_last = ("date", "open", "high", "low", "close", "volume", "amount", "pct_chg")
    if any(col not in d_last.index or pd.isna(d_last[col]) for col in _required_last):
        return None, "FAIL_DATA"
    if len(out) < 2 or pd.isna(out.iloc[-2].get("close")):
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
    if pd.notna(d_last.get("volume_percentile")) and float(d_last["volume_percentile"]) < config.MIN_VOLUME_PERCENTILE:
        return None, "FAIL_VOL_INSUFFICIENT"
    if pd.notna(d_last.get("amount_ratio")) and float(d_last["amount_ratio"]) < config.MIN_AMOUNT_RATIO:
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

    # 层 4：波动率风控
    atr_val = d_last.get("atr")
    atr_val = float(atr_val) if atr_val is not None and not pd.isna(atr_val) else None
    if atr_val is not None and last_close > 0 and (atr_val / last_close * 100) > config.MAX_ATR_PCT_BREAKOUT:
        return None, "FAIL_VOLATILE"

    # 层 5：综合评分与等级
    daily_score = float(d_last.get("daily_score", 0))
    base_grade = _grade_from_score(daily_score - grade_boost, config)
    min_grade = config.MIN_PASS_GRADE if config.MIN_PASS_GRADE in _GRADE_ORDER else "B"
    if _GRADE_ORDER.index(base_grade) < _GRADE_ORDER.index(min_grade):
        return None, "FAIL_TECH"

    # 交易计划
    breakout_ref_map = {3: d_last.get("level_l1"), 2: d_last.get("level_l2"), 1: d_last.get("level_l3")}
    breakout_ref = breakout_ref_map.get(breakout_level)
    breakout_ref = float(breakout_ref) if breakout_ref is not None and not pd.isna(breakout_ref) else last_close
    rr = compute_breakout_risk_reward(last_close, breakout_ref, config, atr_val)
    if rr["rr_ratio"] < config.MIN_RR_RATIO_BREAKOUT:
        return None, "FAIL_TECH"

    _amt = pd.to_numeric(out["amount"], errors="coerce").tail(20).mean() if "amount" in out.columns else float("nan")

    sig = BreakoutSignal(
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

def main_breakout(
    config: Optional[VolumeBreakoutConfig] = None,
    cache: Optional[CacheManager] = None,
) -> Optional[pd.DataFrame]:
    """放量突破选股主流程。骨架与 bottom_fishing_strategy.main 对齐：
    初始化数据源 → 市场环境 → 股票池 → 并发筛选（漏斗日志） → 决赛圈周线确认 → 排序截取。
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

        signals: list[dict] = []
        processed, total = 0, len(stock_list)
        stats = {
            "total": total, "error": 0, "fail_data": 0, "fail_liq": 0, "fail_fund": 0,
            "fail_breakout": 0, "fail_vol": 0, "fail_pattern": 0, "fail_trend": 0,
            "fail_fake": 0, "fail_chase": 0, "fail_rsi": 0, "fail_volatile": 0,
            "fail_tech": 0, "pass": 0,
        }

        def _screen_one(stock: dict) -> tuple[Optional[BreakoutSignal], str]:
            code, name = stock["code"], stock["name"]
            try:
                daily_df = get_daily_data(code, config, cache)
                if daily_df is None:
                    return None, "FAIL_DATA"
                fund_data = get_fundamentals(code, cache, config)
                return evaluate_breakout(daily_df, code, name, config, market_env, fund_data)
            except Exception as e:
                logger.debug("%s(%s) 筛选异常: %s", name, code, e)
                return None, "ERROR"

        screen_start = time.time()
        budget_sec = config.SCREEN_TIME_BUDGET_MIN * 60
        progress = {"done": 0, "t": screen_start}
        stop_watch = threading.Event()

        def _watchdog() -> None:
            while not stop_watch.wait(180):
                idle = time.time() - progress["t"]
                if idle >= 240:
                    hanging = [futures[f]["code"] for f in futures if not f.done()][:8]
                    logger.warning("已 %d 秒无任务完成，疑似数据源卡住：%d/%d 完成，在途代码: %s",
                                   int(idle), progress["done"], total, ",".join(hanging) or "-")
                else:
                    logger.info("心跳：%d/%d 完成，已运行 %.1f 分钟，当前通过 %d 只",
                                progress["done"], total, (time.time() - screen_start) / 60, stats["pass"])

        logger.info("开始并发筛选：%d 只股票，%d 线程，时间预算 %.0f 分钟",
                    total, config.MAX_WORKERS, config.SCREEN_TIME_BUDGET_MIN)
        pool = ThreadPoolExecutor(max_workers=config.MAX_WORKERS)
        futures = {pool.submit(_screen_one, s): s for s in stock_list}
        watchdog = threading.Thread(target=_watchdog, daemon=True)
        watchdog.start()
        time_budget_hit = False
        try:
            for future in as_completed(futures):
                processed += 1
                progress["done"] = processed
                progress["t"] = time.time()
                sig, reason = future.result()
                if reason == "PASS" and sig is not None:
                    stats["pass"] += 1
                    signals.append(sig.to_dict())
                    logger.info("[通过] %s(%s) 评分 %.1f %s级 L%d 突破幅度 %.2f%%",
                                sig.name, sig.code, sig.score, sig.grade, sig.breakout_level, sig.breakout_margin)
                elif reason == "FAIL_FUND": stats["fail_fund"] += 1
                elif reason == "FAIL_DATA": stats["fail_data"] += 1
                elif reason == "FAIL_LIQUIDITY": stats["fail_liq"] += 1
                elif reason == "FAIL_NO_BREAKOUT": stats["fail_breakout"] += 1
                elif reason in ("FAIL_VOL_INSUFFICIENT", "FAIL_CLIMAX_VOL", "FAIL_AMOUNT_INSUFFICIENT"): stats["fail_vol"] += 1
                elif reason in ("FAIL_NO_PLATFORM", "FAIL_PLATFORM_LOOSE"): stats["fail_pattern"] += 1
                elif reason in ("FAIL_TREND_DOWN", "FAIL_BELOW_MA20"): stats["fail_trend"] += 1
                elif reason == "FAIL_FAKE_BREAKOUT": stats["fail_fake"] += 1
                elif reason in ("FAIL_CHASE", "FAIL_GAP", "FAIL_BREAKOUT_WEAK"): stats["fail_chase"] += 1
                elif reason == "FAIL_RECENT_FAILED_BREAKOUT": stats["fail_chase"] += 1
                elif reason == "FAIL_RSI_HIGH": stats["fail_rsi"] += 1
                elif reason == "FAIL_VOLATILE": stats["fail_volatile"] += 1
                elif reason == "FAIL_TECH": stats["fail_tech"] += 1
                elif reason == "ERROR": stats["error"] += 1

                if processed % config.PROGRESS_LOG_EVERY == 0 or processed == total:
                    elapsed = time.time() - screen_start
                    rate = processed / elapsed if elapsed > 0 else 0
                    eta_min = (total - processed) / rate / 60 if rate > 0 else 0
                    logger.info("进度: %d/%d (%.0f%%)，已用 %.1f 分钟，预计剩余 %.1f 分钟",
                                processed, total, processed / total * 100, elapsed / 60, eta_min)
                if time.time() - screen_start > budget_sec:
                    time_budget_hit = True
                    remaining = sum(1 for f in futures if not f.done())
                    logger.warning("已达筛选时间预算 %.0f 分钟，取消剩余 %d 只未完成任务，基于已完成 %d/%d 只出结果",
                                   config.SCREEN_TIME_BUDGET_MIN, remaining, processed, total)
                    break
        finally:
            stop_watch.set()
            pool.shutdown(wait=True, cancel_futures=True)

        screen_minutes = (time.time() - screen_start) / 60

        # 漏斗日志
        logger.info("=" * 50)
        logger.info("[FUNNEL] 放量突破漏斗数据分析   筛选耗时 %.1f 分钟", screen_minutes)
        logger.info("=" * 50)
        if time_budget_hit:
            logger.warning("[WARN] 因达到时间预算，以下漏斗仅统计已完成的 %d/%d 只", processed, total)
        logger.info("1. 初始有效股票池: %d 只 (完成处理 %d 只)", stats["total"], processed)
        logger.info("2. 数据 & 流动性达标: %d 只 (数据缺失 %d, 僵尸股 %d, 异常 %d)",
                    processed - stats["fail_data"] - stats["fail_liq"] - stats["error"],
                    stats["fail_data"], stats["fail_liq"], stats["error"])
        logger.info("3. 基本面防雷通过: %d 只 (淘汰 %d)",
                    processed - stats["fail_data"] - stats["fail_liq"] - stats["error"] - stats["fail_fund"],
                    stats["fail_fund"])
        logger.info("4. 突破 & 量能确认: %d 只 (未突破 %d, 量能不足/天量/额不足 %d)",
                    stats["pass"] + stats["fail_pattern"] + stats["fail_trend"] + stats["fail_fake"] +
                    stats["fail_chase"] + stats["fail_rsi"] + stats["fail_volatile"] + stats["fail_tech"],
                    stats["fail_breakout"], stats["fail_vol"])
        logger.info("5. 平台整理 & 趋势背景: %d 只 (无平台 %d, 趋势下行 %d)",
                    stats["pass"] + stats["fail_fake"] + stats["fail_chase"] + stats["fail_rsi"] +
                    stats["fail_volatile"] + stats["fail_tech"],
                    stats["fail_pattern"], stats["fail_trend"])
        logger.info("6. K线形态 & 假突破过滤: %d 只 (假突破 %d, 追高/跳空 %d)",
                    stats["pass"] + stats["fail_rsi"] + stats["fail_volatile"] + stats["fail_tech"],
                    stats["fail_fake"], stats["fail_chase"])
        logger.info("7. RSI & 波动率 & 评分: %d 只 (RSI过高 %d, 波动率超限 %d, 评分不达 %d)",
                    stats["pass"], stats["fail_rsi"], stats["fail_volatile"], stats["fail_tech"])
        logger.info("日线取数来源: Baostock %d 只，AkShare 兜底 %d 只，双源均失败 %d 只",
                    fetch_stats["bs_ok"], fetch_stats["ak_ok"], fetch_stats["fail"])
        logger.info("=" * 50)

        if not signals:
            logger.info("未发现符合条件的放量突破信号")
            return None

        # 确定性排序
        df = pd.DataFrame(signals).sort_values(SORT_BY, ascending=SORT_ASC).reset_index(drop=True)

        # 决赛圈周线确认（继承抄底策略的 check_weekly_trend + check_weekly_macd）
        if config.REQUIRE_WEEKLY_TREND and not df.empty:
            logger.info("进入周线确认：%d 只候选，逐只确认最多取 %d 只", len(df), max_picks)
            confirmed = []
            weekly_checked = 0
            for _, row in df.iterrows():
                if len(confirmed) >= max_picks:
                    break
                weekly_checked += 1
                wk = _fetch_weekly_dual(row["code"], config)
                trend_ok = check_weekly_trend(wk, config)
                macd_ok = check_weekly_macd(wk, config) if trend_ok and config.REQUIRE_WEEKLY_MACD_STABLE else True
                if trend_ok and macd_ok:
                    confirmed.append(row)
                    logger.info("周线确认 %s(%s) 评分 %.1f L%d: 通过（第 %d/%d 只）",
                                row["name"], row["code"], row["score"], row["breakout_level"],
                                len(confirmed), max_picks)
                else:
                    logger.info("周线确认 %s(%s) 评分 %.1f L%d: 淘汰（%s）",
                                row["name"], row["code"], row["score"], row["breakout_level"],
                                "周线趋势未过" if not trend_ok else "周线MACD未企稳")
                time.sleep(config.FETCH_DELAY)
            weekly_dropped = weekly_checked - len(confirmed)
            if weekly_dropped > 0:
                logger.info("周线确认汇总：检查 %d 只，淘汰 %d 只", weekly_checked, weekly_dropped)
            df = pd.DataFrame(confirmed).reset_index(drop=True) if confirmed else df.iloc[0:0]

        # 推荐数量上限
        if len(df) > max_picks:
            logger.info("通过 %d 只，按评分截取前 %d 只（淘汰 %d 只低分信号）",
                        len(df), max_picks, len(df) - max_picks)
            df = df.head(max_picks).reset_index(drop=True)
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
