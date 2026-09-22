"""
精简版日线选股策略（Baostock 主数据源 + AkShare 备用数据源 + 底背离 + 漏斗日志版）

仅使用日线数据进行选股，简化策略逻辑：
1. 市场环境过滤（沪深300日线MA20斜率 + regime 滞回；数据不足时明示「未知」，不隐含为正常）
2. 基本面防雷（年化ROE/负债率核心否决；商誉/扣非为可选否决，主源不提供时决赛圈用 AkShare 按字段补齐）
3. 日线技术指标筛选（底背离 + MA5拐头 + EMA金叉 + RSI超卖反弹 + 量价质量分）
4. 波动率风控（ATR% 超上限否决，止损/止盈/盈亏比仅作展示与落库）+ 决赛圈周线确认 + 行业分散

运行保障：全模块统一 logging；socket 全局默认超时 20s 防数据源假死；并发筛选带
心跳看门狗（连续 4 分钟无完成打印在途代码）与时间预算（SCREEN_TIME_BUDGET_MIN，
超时按已完成结果出报告）。

荐股质量分层（数据缺失不等于筛选通过）：
- 正式推荐（formal）：财务核心项已核验、启用的周线确认已确认、行情日期与市场最新交易日一致
- 待核验候选（pending）：技术面通过但财务或周线数据缺失，不与正式推荐混排
- 暂不推荐：行情日期滞后（停牌/数据过期）或关键否决项未通过

数据层：Baostock 为主、AkShare 为备的双数据源架构。
- Baostock 连接管理：登录真实重试（检查 error_code）、查询失败自动重连、线程安全锁
- 熔断机制：Baostock 连续失败达阈值后熔断，本次运行后续请求直接走 AkShare
- 单条取数失败（返回空或异常）自动降级 AkShare 兜底
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("strategy")

# 全局 socket 默认超时：baostock/akshare 底层 socket 默认无超时，曾在持有 bs_lock
# 的线程上永久阻塞导致全池假死（GitHub Actions 跑满 6h 被取消）。勿删。
SOCKET_TIMEOUT = 20
socket.setdefaulttimeout(SOCKET_TIMEOUT)

# 北京时间（东八区）：GitHub Actions runner 为 UTC，凡涉及「今日是否收盘 /
# 本周是否完结」的判断都必须用该时区，禁止直接使用 datetime.now() 的本地时区。
BEIJING_TZ = timezone(timedelta(hours=8))

def _beijing_now() -> datetime:
    """当前北京时间（感知时区的 datetime）。"""
    return datetime.now(BEIJING_TZ)

# 日线取数来源统计（本次运行）：Baostock 命中 / AkShare 兜底命中 / 双源均失败。
# 多线程下简单自增在 GIL 保护下安全（与 _bs_state 同一约定）。
fetch_stats = {"bs_ok": 0, "ak_ok": 0, "fail": 0}

try:
    import baostock as bs
    _BS_AVAILABLE = True
except ImportError:  # 允许只使用本地 CSV 的独立回测模块在无 Baostock 环境下运行
    bs = None
    _BS_AVAILABLE = False

# Baostock (0.9.0, 已停更) 的 ResultData.get_data() 翻页时内部使用 DataFrame.append，
# 该方法在 pandas 2.0 中被移除。数据量小（个股日线/指数/基本面，单页装下）时不触发，
# 但 query_all_stock 返回 2000+ 行必须翻页，会抛 AttributeError 导致股票列表拉取失败。
# 兼容补丁：用 pd.concat 补回 append（仅影响本进程，赋值语义与 baostock 内部用法一致）。
if not hasattr(pd.DataFrame, "append"):
    pd.DataFrame.append = lambda self, other, ignore_index=False, **kw: pd.concat(  # type: ignore[attr-defined]
        [self, other], ignore_index=ignore_index, **kw
    )

# AkShare 备用数据源（可选依赖，未安装时自动跳过 fallback）
try:
    import akshare as ak
    _AK_AVAILABLE = True
except ImportError:
    ak = None
    _AK_AVAILABLE = False

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MODULE_DIR)

# 全局 Baostock 线程锁，防止多线程下 C++ 底层 socket 崩溃
bs_lock = threading.Lock()

# ===========================================================================
# 模块级元数据
# ===========================================================================

DISPLAY_COLS = [
    ("code", "代码"), ("name", "名称"), ("date", "日期"), ("close", "收盘"),
    ("score", "评分"), ("grade", "等级"), ("tier", "层级"), ("rank_score", "排序分"),
    ("signals_hit", "入选依据"), ("fund_status", "财务核验"),
    ("quality_score", "质量分"), ("valuation_score", "估值分"),
    ("pe_ttm", "PE"), ("pb_mrq", "PB"), ("position_250", "250日位置"),
    ("weekly_status", "周线核验"), ("missing_tags", "缺项"), ("daily_score", "日线分"),
    ("rsi", "RSI"), ("rsi7", "RSI7"), ("rsi21", "RSI21"),
    ("vol_ratio", "量比"), ("turnover_ratio", "换手比"), ("stop_loss", "止损"),
    ("take_profit", "止盈"), ("rr_ratio", "收益比"), ("market_env", "市场"),
    ("has_divergence", "底背离"),
]

TITLE = "日线技术指标选股结果"
PREFIX = "bf"
# 主排序键为 rank_score（= daily_score + 连续质量分，见 _rank_quality_score）。
# _rank_signals 在该列缺失时自动退回 score，兼容外部构造的 DataFrame（回测/单元测试）。
SORT_BY = ["rank_score"]
SORT_ASC = [False]

# P6b：quality_value 路径全部否决归因码（用于「完整闸门归因」日志，含 0 计数）。
# 长期为 0 的闸门＝对区分候选无贡献，是数据驱动瘦身的审计对象。新增否决码时同步登记。
_QV_REASON_CODES = (
    "FAIL_DATA", "FAIL_STALE", "FAIL_LIQUIDITY", "FAIL_POSITION", "FAIL_VALUATION",
    "FAIL_FUND", "FAIL_FORWARD", "FAIL_QV_SCORE", "FAIL_BEAR_TIMING",
    "FAIL_MACD_WEAK", "FAIL_KDJ_HIGH", "ERROR",
)

# ===========================================================================
# StrategyConfig
# ===========================================================================

@dataclass
class StrategyConfig:
    # quality_value：质量/估值/中长期低位必选，技术仅排序；technical 保留旧策略作对照。
    RECOMMENDATION_MODE: str = "quality_value"
    QUALITY_YEARS: int = 3
    QUALITY_MEDIAN_ROE_MIN: float = 10.0
    QUALITY_MIN_ROE: float = 5.0
    QUALITY_CASH_CONVERSION_MIN: float = 0.8
    LOW_POSITION_LOOKBACK: int = 250
    LOW_POSITION_MAX: float = 0.40
    QUALITY_SCORE_WEIGHT: float = 0.50
    VALUATION_SCORE_WEIGHT: float = 0.35
    TECHNICAL_SCORE_WEIGHT: float = 0.15
    CSI300_AK_SYMBOL: str = "sh000300"  # Baostock 格式为 sh.000300
    MARKET_MA_PERIOD: int = 20
    MARKET_SLOPE_LOOKBACK: int = 4
    MARKET_BULL_SLOPE: float = 0.01
    MARKET_BEAR_SLOPE: float = -0.01
    # 指数数据缺失（regime=unknown）时按熊市保守处理：推荐上限收缩、评分门槛上浮
    UNKNOWN_AS_BEAR: bool = True
    # regime 滞回：牛/熊/中性的切换须连续 MARKET_REGIME_CONFIRM_DAYS 个交易日同向确认，
    # 避免 MA20 斜率在 ±0.01 阈值附近小幅抖动导致 regime 逐日跳变；状态持久化在
    # cache/market_regime_state.json（超 10 天未更新自动重置）。unknown 不改变已确认状态。
    MARKET_REGIME_HYSTERESIS: bool = True
    MARKET_REGIME_CONFIRM_DAYS: int = 2
    # ===== P2：regime 双指标确认（第二指标 = 长期均线 MA60 趋势）=====
    # 单一沪深300 MA20 斜率在 ±0.01 阈值附近逐日抖动，滞回只是打补丁。叠加 MA60 慢速
    # 趋势过滤：只有「斜率看多 且 现价/MA20 均在 MA60 上方」才 bull，「斜率看空 且均在
    # MA60 下方」才 bear，两者不同向降级为 neutral——从源头减少翻转、让 regime 更可信。
    # MA60 数据不足（指数样本 < MARKET_MA_LONG）时自动退回单指标，口径与改动前一致。
    # 置 False 恢复纯 MA20 斜率单指标判据。
    MARKET_REGIME_DUAL_INDICATOR: bool = True
    MARKET_MA_LONG: int = 60

    MIN_ROE: float = 5.0
    MAX_DEBT_RATIO: float = 70.0
    MAX_GOODWILL_RATIO: float = 20.0
    MIN_DEDUCTED_PROFIT_RATIO: float = 0.5
    # 金融业（银行/保险/券商等）负债率天然 80%+，通用阈值会全行业误杀，单独放宽兜底
    FINANCE_NAME_KEYWORDS: tuple = ("银行", "保险", "证券", "信托", "期货")
    FINANCE_EXEMPT_CODES: tuple = ("601318", "601336", "601601", "601628", "601319", "300059")  # 平安/新华/太保/人寿/人保/东方财富
    FINANCE_MAX_DEBT_RATIO: float = 97.0

    # ===== 估值过滤（A：低估值）=====
    # 与「低位」（C：MIN_DRAWDOWN_FROM_HIGH / POSITION_IN_RANGE_MAX）共同定义「优质低价」：
    # C 保证价格处于回撤低位，A 保证估值本身便宜——两者缺一，「低位」可能只是「贵票回调」。
    # 数据源：Baostock 日线逐 bar 自带 peTTM/pbMRQ（同一请求返回，点位正确、无前视）；
    # AkShare 东财/新浪逐 bar 不提供估值 → 该两列缺失时本层放行并记 missing_tag「valuation」
    # （估值降级为「待核验」，不硬否决，与既有「数据缺失不误杀」约定一致）。
    USE_VALUATION_FILTER: bool = True
    # 市盈率（TTM）上限：> 上限判为偏贵，否决。
    MAX_PE_TTM: float = 25.0
    # 要求 peTTM > 0：亏损股（peTTM ≤ 0）不属于「优质」，直接否决（可关以放行周期股底部）
    REQUIRE_POSITIVE_PE: bool = True
    # 市净率（MRQ）上限：> 上限判为偏贵，否决；≤ 0（净资产为负/异常）一并否决
    MAX_PB_MRQ: float = 3.0

    # ===== 行业相对估值（#4a）=====
    # 绝对阈值（PE≤25 / PB≤3）全市场一刀切，会系统性误杀「高估值但优质」的成长/科技/医药，
    # 并把名单压向银行/地产/周期等低 PE/PB 板块（低 PE 本身常是市场对衰退的定价＝价值陷阱）。
    # 开启后估值闸门改为「行业内分位」：个股 PE/PB 在其所属行业当日横截面中的分位 ≤
    # VALUATION_INDUSTRY_PERCENTILE_MAX 才算便宜（0.60＝至少比业内 40% 的同行便宜）。
    # 行业数据缺失、或行业内可比样本 < VALUATION_INDUSTRY_MIN_PEERS 时，**自动回退**到上面的
    # 绝对阈值（USE_VALUATION_FILTER / MAX_PE_TTM / MAX_PB_MRQ），保证行业接口挂掉时不误放行。
    # 横截面快照由 build_industry_valuation_snapshot 在筛选前一次性构建（复用缓存的日线，
    # 不额外增加取数成本）；回测/单测未提供快照时同样走绝对阈值回退，口径与改动前一致。
    USE_INDUSTRY_RELATIVE_VALUATION: bool = True
    VALUATION_INDUSTRY_PERCENTILE_MAX: float = 0.60
    VALUATION_INDUSTRY_MIN_PEERS: int = 5

    # ===== 前瞻确认：当年成长未恶化（#4b）=====
    # 年度质量（最近3个完整年报）与 PE/PB 都是「后视镜」：一家公司当年基本面刚崩，
    # trailing 3 年 ROE 依旧漂亮、股价跌出低位、PE 因下跌而变低——三大闸门全部通过（价值陷阱）。
    # 本层用 Baostock query_growth_data 的最新报告期「净利润同比增长率(YOYNI)」做前瞻刹车：
    # 同比 < FORWARD_NI_YOY_MIN（%）判为当年明显恶化 → FAIL_FORWARD 否决。
    # 数据缺失或报告期过旧默认降级为待核验，不把未确认的近期业绩视为通过。
    REQUIRE_FORWARD_CONFIRMATION: bool = True
    FORWARD_NI_YOY_MIN: float = -30.0
    # 成长数据缺失（AkShare 兜底日 / 离线环境 / 该接口无返回）时是否降级为待核验。
    # 默认 True；显式 False 可恢复缺失放行，标签仍显示未核验。
    # 主源不可用时可能没有正式推荐，应按数据覆盖不足处理。
    FORWARD_MISSING_AS_PENDING: bool = True
    # 业绩下滑黄标（P0）：forward 净利同比落在 [FORWARD_NI_YOY_MIN, FORWARD_NI_YOY_WARN)
    # 之间时**不否决**（硬闸门口径完全不变），只在决策简报的风险项显式预警「业绩下滑」。
    # -30% 的硬否决线过宽，-29% 的公司照样通过；黄标把「没崩但明显在走弱」这一档暴露给人工。
    # 设为 ≤ FORWARD_NI_YOY_MIN 可关闭黄标（此时只有触发硬否决才提示）。
    FORWARD_NI_YOY_WARN: float = -10.0

    # ===== 综合分下限（#3：宁缺毋滥）=====
    # quality_value 综合分 = 0.50×质量 + 0.35×估值 + 0.15×技术（0~100）。
    # 此前只用于排序、无下限：只要有票通过硬闸门就凑满 MAX_PICKS。开启后综合分 <
    # MIN_QV_SCORE 的候选直接否决（FAIL_QV_SCORE），弱市自然收敛到少推/不推。
    # 设 0 关闭该闸门（恢复改动前行为）。
    #
    # ⚠️ 该下限的实际严厉程度远高于"兜底"，它**严格强于三道硬闸门之和**，务必知悉：
    #   评分口径决定了「恰好压线达标」的公司拿不到 60 分——
    #     · 质量分：_dimension_score 在阈值处恰好给 50 分。三档同时压线的最小可达
    #       质量分就是 50（如 3 年 ROE=(5,10,10)：中位 10=阈值、最低 5=阈值、
    #       现金转换 0.8=阈值，实测 status=verified 而 quality_score=50）。
    #     · 估值分：行业口径在分位 0.60（=闸门上限）处给 40 分；
    #               绝对口径在 PE=25 / PB=3（=闸门上限）处给 0 分（100×(1−值/上限)）
    #   于是三道闸门全部恰好达标的公司：
    #     · 行业口径：0.5×50 + 0.35×40 + 0.15×技术分 = 39.0 + 0.15×技术分 ≤ 54.0
    #     · 绝对口径：0.5×50 + 0.35×0  + 0.15×技术分 = 25.0 + 0.15×技术分 ≤ 40.0
    #   两者都不足 60 —— 即「压线合格」100% 被本下限淘汰，硬闸门的阈值形同虚设。
    #   反解过线所需（0.5q+0.35v+0.15t ≥ 60）：
    #     · 行业口径（v=40）：q=50 需 t≥140（不可达）；q=70 需 ≥73；q=90 需 ≥7
    #     · 绝对口径（v=0） ：q=70 需 t≥167（不可达）；q=90 需 =100（必须满分）
    #   副作用：PE/PB 逐 bar 缺失（AkShare 兜底日）→ 估值分=0 → formal 几乎必然归零，
    #   与 FORWARD_MISSING_AS_PENDING=True 是两个独立叠加的零推荐机制。
    #   运行时可读：qv_floor_equivalence() / describe_qv_floor() 会随配置实时算出门槛并
    #   打进漏斗日志；其算术已固化为 test_recommendation_upgrades.TestScoreFloorEquivalence。
    # 调低该值时务必先跑 `python backtest.py ab --mode quality_value`。
    MIN_QV_SCORE: float = 60.0

    # ===== P0：入场时机判读（左侧/右侧 + 止跌确认；仅加标签展示，绝不改推荐口径）=====
    # quality_value（生产默认）旁路了全部技术择时闸门（weekly=not_required、技术仅 15%
    # 权重不否决、QV_ENFORCE_KDJ_MACD_VETO 默认 False），唯一位置约束是 250 日 position≤0.40。
    # 后果：一只利润下滑、股价处于低位、MACD 仍在加速下跌的深度价值股能顺利通过全部闸门
    # 被正式推荐——典型价值陷阱/接飞刀。开启后为每条推荐计算「左侧/右侧 + 是否仍在下跌」
    # 的时机读数并写入决策简报，把择时判断显式交回人工（不改「哪些股票通过」）。
    SURFACE_TIMING_READ: bool = True
    TIMING_MA_LONG: int = 60   # 时机判读用的长期均线周期（现价站上/跌破 MA60 区分右侧/左侧）
    # ===== P1：熊市抄底侧的止跌闸门（组合层暴露预算，修复"越熊越纯接飞刀"）=====
    # 背景：突破策略熊市直接空仓（BEAR_MAX_PICKS_BREAKOUT=0），而抄底侧仍出 BEAR_MAX_PICKS
    # 只、且 quality_value 无任何择时闸门——市场越差，组合越纯粹地只剩左侧接飞刀，恰好在最该
    # 收手的 regime 里加满最危险的暴露。开启后：**熊市（含 unknown 折叠）** 的 formal 推荐
    # 必须额外满足最低止跌证据「现价站上 MA20」或「MACD 柱连续 MACD_MOMENTUM_DAYS 日改善」，
    # 否则否决（FAIL_BEAR_TIMING）。牛市/中性不受影响（不改变既有口径）。
    #
    # 默认 True = 与生产入口一致。此前库级为 False、仅 run.py 显式覆盖为 True，导致
    # 「库级口径 ≠ 生产口径」：任何用 StrategyConfig() 默认值跑的实验（backtest.py ab、
    # 单元测试、临时脚本）都跑在一个生产并不存在的策略上，A/B 结论无法直接采信。
    # 现把生产口径收进库级默认，覆盖点消除；显式设 False 可关闭该闸门。
    QV_BEAR_TIMING_GATE: bool = True

    DAILY_MA5: int = 5
    DAILY_MA10: int = 10
    DAILY_EMA5: int = 5
    DAILY_EMA10: int = 10
    DAILY_EMA20: int = 20
    DAILY_RSI_PERIOD: int = 14
    DAILY_RSI_SHORT_PERIOD: int = 7
    DAILY_RSI_LONG_PERIOD: int = 21
    DAILY_RSI_OVERSOLD: float = 30.0
    DAILY_RSI_REBOUND_MIN: float = 35.0
    DAILY_RSI_OVERBOUGHT: float = 70.0
    DAILY_RSI_DIVERGENCE_THRESHOLD: float = 2.0
    DAILY_MACD_FAST: int = 12
    DAILY_MACD_SLOW: int = 26
    DAILY_MACD_SIGNAL: int = 9
    DAILY_VOL_EXPAND: float = 1.2
    DAILY_TURNOVER_LOOKBACK: int = 20
    DIVERGENCE_LOOKBACK: int = 20
    # 底背离双低点参数：前一个价格低点与当前低点至少间隔该根数（保证是两个独立低点，
    # 而非同一段下跌中的连续新低）；指标比较锚定在前一个「价格低点」当根的指标值上
    DIVERGENCE_MIN_SEPARATION: int = 5
    # DIF 背离最低幅度（价格单位；0=严格更高即可），RSI 背离沿用 DAILY_RSI_DIVERGENCE_THRESHOLD
    DAILY_MACD_DIVERGENCE_THRESHOLD: float = 0.0

    FIXED_STOP_LOSS_PCT: float = 5.0
    FIXED_TAKE_PROFIT_PCT: float = 10.0
    ATR_PERIOD: int = 14
    ATR_STOP_MULT: float = 2.0
    USE_ATR_STOP: bool = True
    # 波动率风控：ATR 占现价百分比超过该值直接否决（FAIL_VOLATILE）。
    # 该阈值与旧「RR≥1.5」数学等价（2×ATR止损+10%止盈下 RR≥1.5 ⟺ ATR≤3.33%现价），
    # 但语义直白、且 ATR 缺失时不再隐含放行（旧实现 RR 恒 2.0 永不否决）。
    # 止损/止盈/盈亏比（rr_ratio）仅作展示与落库，不参与否决。
    MAX_ATR_PCT: float = 3.33

    # ===== 交易计划（建仓区间 / 止损 / 止盈 / 建议持有周期）=====
    # 纯展示与落库，不参与任何准入、排序与否决（与 stop_loss/take_profit 同一定位）。
    # 建仓区间带宽：通知给出的建仓区间 = 信号日收盘价 × (1 ± ENTRY_BAND_PCT%)。
    # 两套策略的真实执行口径都是「信号日次日开盘买入」（见 backtest.py 的 T+1 开盘成交
    # 与 docs/minimal_repairs.md），故以收盘价为锚：次日开盘落在区间内即按计划建仓；
    # 高于上沿视为追高、放弃本轮（不为一个信号抬高成本）；低于下沿说明隔夜出现不利变动，
    # 按失效重估而不是「越低越买」。
    ENTRY_BAND_PCT: float = 1.5
    # 建议持有周期（市场交易日）：与追踪口径对齐——run_weekly_tracking.py 在推荐后
    # 第 5/10/15/20 个交易日观测表现，20 日既是观测终点，也是「到期离场」的判定日。
    HOLD_DAYS_HINT_MIN: int = 10
    HOLD_DAYS_HINT_MAX: int = 20

    MIN_AMOUNT: float = 30_000_000.0  # 近 20 日日均成交额下限（元）；原 500 万对主板几乎无筛选力，上调至 3000 万过滤低流动性标的
    MIN_DAYS: int = 60
    # 数据时效：个股最新K线日期须与市场（沪深300）最新交易日一致，
    # 否则视为停牌/数据滞后，暂不推荐（宁可少荐，不让过期数据混入名单）
    REQUIRE_FRESH_DAILY: bool = True
    # 停牌缺口检测：相邻 K 线的最大自然日间隔超过 MAX_BAR_GAP_DAYS 判定为期间曾停牌。
    # 既有两道校验都抓不到「历史缺口」：MIN_DAYS 只数 bar 总数（停牌 40 天的复牌股
    # 仍有约 60 根 bar，只是覆盖 160 个自然日），REQUIRE_FRESH_DAILY 只看末根日期。
    # 这类股票的 ma20/rsi14/atr/60日高点/量比 全部跨缺口计算——「60日高点」实际是
    # 5 个月前的高点，量比把停牌前的死量与复牌后的爆量混在同一窗口——取值无经济意义。
    # 阈值 12 天可容纳春节（相邻交易日间隔约 11 天）与国庆（约 8 天）长假。
    REQUIRE_NO_HALT_GAP: bool = True
    MAX_BAR_GAP_DAYS: int = 12

    # 防追高否决：当日涨幅超过该值（%）判定为追高——涨停股买不进、大阳线次日易回调，直接否决
    MAX_ENTRY_PCT_CHG: float = 5.0
    # 防跳空否决：开盘价相对前收跳空高开超过该比例（%），追买风险大，直接否决
    MAX_GAP_UP_PCT: float = 2.0
    # 下降通道过滤：MA20 近 N 日变化率低于该阈值判定为陡峭下降（接飞刀），当日趋势转折信号不认可
    DAILY_MA20: int = 20
    MA20_TREND_LOOKBACK: int = 5
    MA20_TREND_MIN_SLOPE: float = -0.04
    # RSI 入场上限：RSI14 高于该值说明已反弹一段、不再是底部入场点，直接否决
    DAILY_RSI_ENTRY_MAX: float = 60.0
    # 天量否决：量比超过该值疑似主力出货/消息驱动，次日接力风险大，直接否决
    MAX_VOL_RATIO: float = 4.0
    # 底部区域过滤：现价距近 N 日最高价回撤不足该比例，不符合抄底定位（可能只是上涨中继回调），直接否决
    DRAWDOWN_LOOKBACK: int = 60
    MIN_DRAWDOWN_FROM_HIGH: float = 0.10
    # 回撤上限：跌幅过深多为基本面恶化/退市风险（价值陷阱），并非健康底部，超过该比例则否决
    MAX_DRAWDOWN_FROM_HIGH: float = 0.70
    # 已反弹一段否决：近 N 日累计涨幅超过该值（%），说明底部反弹可能已走完，不再是介入点
    RECENT_GAIN_LOOKBACK: int = 5
    MAX_RECENT_GAIN_PCT: float = 12.0
    # 最终推荐数量上限：评分降序截取前 N 只（目标每日推荐 3~5 只）
    MAX_PICKS: int = 5
    # ===== 推荐数量按市场环境收缩 =====
    # A 股个股收益方差中市场因子（beta）通常解释 60~75%，「何时交易」对胜率的影响
    # 远大于「交易哪只」。原实现里 regime 的唯一作用是把准入分数线抬高
    # BEAR_GRADE_BOOST 分，推荐数量在牛/中/熊一律为 MAX_PICKS——这与上方
    # UNKNOWN_AS_BEAR 注释承诺的「推荐上限收缩」不符（该行为只在
    # volume_breakout_strategy.main_breakout 里实现了）。此处补齐，口径与突破策略对齐。
    # unknown 经 _effective_regime 折叠为 bear，自动适用熊市上限。
    NEUTRAL_MAX_PICKS: int = 4
    BEAR_MAX_PICKS: int = 2
    # 市场级熔断：沪深300 近 MARKET_CRASH_LOOKBACK 个交易日累计跌幅低于该阈值（%）
    # → 本次运行不推荐。急跌期间全市场同步满足 RSI 超卖 / MA5 拐头 / 创新低底背离，
    # 抄底信号会批量触发，而 MA20_TREND_MIN_SLOPE 是个股级过滤，抓不到系统性风险。
    # 这是组合层风控，也是本次改动中唯一新增的门槛（其余均为排序/数量层）。
    MARKET_CRASH_HALT_PCT: float = -4.0
    MARKET_CRASH_LOOKBACK: int = 5
    # 组合层：两套策略（优质低估低位 + 放量突破）同一交易日合计推荐数上限，
    # 由后运行的策略在落库前去重并截取（见 run_breakout.py）
    DAILY_TOTAL_MAX_PICKS: int = 7
    # 正式信号为空时，允许生成一只低置信度观察候选。
    # 默认关闭：与「宁可少荐」哲学一致，避免每天硬推一只低置信度票诱导实盘；
    # 需要时设为 True 恢复旧行为（候选明确标记 tier=fallback，仅作观察）。
    ENABLE_DAILY_FALLBACK: bool = False
    FALLBACK_MIN_SCORE: float = 40.0
    # 行业分散（组合层风控）：同一行业最多推荐的只数，避免 Top5 集中单一板块导致组合同涨同跌
    USE_INDUSTRY_DEDUP: bool = True
    MAX_PICKS_PER_INDUSTRY: int = 2
    INDUSTRY_CACHE_TTL_DAYS: float = 30.0

    # ===== 严格确认指标（提高胜率，进一步压缩低质量信号）=====
    # 区间位置过滤：现价在近 N 日价格区间（最低~最高）中的位置超过该比例，判定不够低位，否决
    RANGE_LOOKBACK: int = 20
    POSITION_IN_RANGE_MAX: float = 0.50
    # MACD 动能确认：要求 MACD 柱当日较昨日改善（绿柱缩短或红柱放大）
    REQUIRE_MACD_MOMENTUM: bool = True
    # MACD 柱需连续改善的天数（1=仅当日较昨日；2=连续两日改善，过滤单日反抽）
    MACD_MOMENTUM_DAYS: int = 2
    # KDJ 确认：要求 KDJ 处于金叉状态（K>D）且 K 值不高于该上限（避免高位接力）
    REQUIRE_KDJ_GOLDEN: bool = True
    KDJ_K_MAX: float = 55.0
    # KDJ 动能：要求 K 值较昨日上行（仅 K>D 不够，K 掉头时容易误判为金叉）
    REQUIRE_KDJ_RISING: bool = True

    # ===== KDJ / MACD 下限否决（荐股控制；两条策略共用同一套阈值与实现）=====
    # 背景：KDJ/MACD 在此前各策略中要么只作「评分项」、要么只作「严格确认项」，
    # 在 quality_value（当前生产默认模式）路径下完全不参与否决。实测暴露的控制
    # 漏洞：放量突破策略的动能分仅占 10/100，分档失守仍可守住 B 级 60 分准入线，
    # 对「推荐与否」没有约束力。
    # 本组是「只拦明显转弱 / 明显偏高」的下限闸门，刻意不要求金叉——理由见
    # kdj_not_overheated 的 docstring（同源指标双重闸门 + 金叉滞后误杀目标形态）。
    # MACD 侧为**双条件 AND**（深度弱势 + 仍在恶化），理由见
    # macd_not_deeply_weak 的 docstring：单用「柱值递减」会误杀匀速上行的健康形态。
    MACD_WEAK_DAYS: int = 2            # 柱值连续递减天数（末 N+1 根严格递减）
    MACD_WEAK_HIST_PCT: float = -0.5   # 深度弱势阈值：柱值/收盘价 × 100 的上限（%）
    KDJ_K_HARD_MAX: float = 85.0       # K 值硬上限（评分线另有更严的软阈值，如突破 80）
    # 高位死叉判定：K<D 且 K ≥ 该值。阈值必须贴近 KDJ_K_HARD_MAX 而不是放在中位——
    # 匀速上行时 9 日 RSV 稳定在 ~0.72，K 与 D 在 70 附近交替领先，把阈值设在 60
    # 会让「K<D」在约半数交易日成立，把整类健康形态误杀（P1 回测前置校验实测，
    # 已固化为回归测试）。设在区间顶部区域后，只有「已到顶且开始掉头」才否决。
    KDJ_DEAD_CROSS_K: float = 80.0
    # quality_value（生产默认模式）是否接入该下限否决。
    # 默认 False —— 保持「技术面不作否决」的现有荐股口径逐字节不变；
    # 置 True 后由 evaluate_quality_value 生效。切换前须先用
    # `python backtest.py ab --mode quality_value` 取得样本内证据。
    QV_ENFORCE_KDJ_MACD_VETO: bool = False

    # 周线趋势确认：仅对通过全部日线筛选的决赛圈股票拉取周线；
    # WEEKLY_MA_BOTH_REQUIRED=True 时须同时满足「收盘站上周线 MA10（容忍 2%）」和「MA10 在上行」，
    # 设为 False 退回旧行为（两条件满足其一即可）
    REQUIRE_WEEKLY_TREND: bool = True
    WEEKLY_MA_PERIOD: int = 10
    WEEKLY_SLOPE_LOOKBACK: int = 3
    WEEKLY_TOLERANCE: float = 0.02
    WEEKLY_MA_BOTH_REQUIRED: bool = False  # A+C 定位下松开：真·低位买点常出现在周线 MA10 尚未上行时，双条件会把目标 setup 全滤掉；设 True 恢复「站上且上行」严格口径
    # 周线 MACD 企稳确认：周线 MACD 柱翻红（含金叉后）或绿柱连续 2 周收窄，
    # 确认周线级别动能拐头，避免周线仍在加速下跌时抄底；设为 False 关闭
    REQUIRE_WEEKLY_MACD_STABLE: bool = True
    WEEKLY_MACD_FAST: int = 12
    WEEKLY_MACD_SLOW: int = 26
    WEEKLY_MACD_SIGNAL: int = 9
    # 周线确认只使用已收盘的周 bar：数据源周线按「周内最新交易日」标注，周中运行时
    # 末根为未收盘的半成品 bar，参与 MA10/MACD 判断会造成口径漂移。开启后，
    # 末 bar 日期落在本 ISO 周且不是周五即剔除（节假日周四收周也保守剔除，
    # 代价是确认滞后一周）。判断基准为北京时间。
    WEEKLY_REQUIRE_CLOSED_BAR: bool = True

    # 趋势转折（MA5拐头 或 EMA金叉，同源信号合并计分，避免右侧拐点同日触发导致分数通胀）
    W_DAILY_TREND_TURN: float = 40.0
    W_DAILY_RSI_REBOUND: float = 25.0
    # 量价质量分：基础条件（上涨+适度放量）之上按 收盘位置/实体方向/上影线占比 分三档，
    # 冲高回落只降分不否决（满分档须为阳线、收盘位于日内区间上部、上影线占比小）
    W_DAILY_VOL_PRICE: float = 25.0        # 满分档：放量企稳（阳线收高位、短上影）
    W_DAILY_VOL_PRICE_MID: float = 18.0    # 中档：一般放量上涨
    W_DAILY_VOL_PRICE_WEAK: float = 10.0   # 降档：放量冲高回落（收盘位于日内区间下部）
    VOLP_CLOSE_POS_FULL: float = 0.6       # 收盘位置高于该值视为收高位（(close-low)/(high-low)）
    VOLP_CLOSE_POS_MIN: float = 0.35       # 收盘位置低于该值判定冲高回落
    VOLP_MAX_UPPER_SHADOW: float = 0.35    # 满分档允许的上影线占日内区间比例上限
    DAILY_MULTI_RESONANCE_BONUS: float = 10.0
    # （已删除 DAILY_RSI_OVERBOUGHT_PENALTY：RSI14>60 已被 DAILY_RSI_ENTRY_MAX 否决，
    #   超买 -3 惩罚实际不可达，属死代码）
    # 底背离不参与评级升降（评级统一：等级=原始技术分定级），仅作形态标签与同分排序优先项

    # ===== 排序质量分（仅决定 _rank_signals 的先后顺序，不参与任何门槛判定）=====
    # 问题：daily_score 由 40/25/25/10 四个布尔分项求和，通过者只可能落在
    # {60,65,68,75,83,90,93,100} 这几个离散值上——号称 0~100 的评分实际是 3-bit 变量。
    # 且穷举可达组合可知 trend_turn 是事实上的必要条件（缺它时唯一通路是
    # rsi_rebound+放量企稳满分+multi_resonance 精确凑到 60 分），评分几乎不提供区分度。
    # 于是 _rank_signals 大量并列，实际决定 Top5 的是 rr_ratio——而
    # rr_ratio = 0.10×entry / (2×ATR) = 0.05/ATR%，是「固定止盈除以 ATR 止损」的
    # 代数残留，从未被设计为排序键；再并列就落到 code 字母序。
    # 质量分把布尔闸门内的连续信息重新引入排序（这些列 compute_daily_signals 已全部算出，
    # 零额外取数）。RANK_QUALITY_WEIGHT=0 可完全退回旧行为。
    # 注意：权重 >3 时可能跨分数档重排（65 与 68 的档差仅 3 分），这是有意为之——
    # 档位本身是布尔求和的产物，不代表质量序；需要严格「只在同档内细分」时设为 2.9。
    RANK_QUALITY_WEIGHT: float = 10.0
    RQ_W_LOW_VOL: float = 0.35        # ATR% 越低越好（低波异象 + 降低止损被扫概率）
    RQ_W_DRAWDOWN: float = 0.35       # 回撤越深越好（均值回归的空间来自跌幅）
    RQ_W_RANGE_POS: float = 0.30      # 20 日区间位置越低越好（闸门内细分）
    # 动能维度默认权重 0：REQUIRE_MACD_MOMENTUM + MACD_MOMENTUM_DAYS=2 已是硬闸门，
    # 通过者的 MACD 柱必然连续 2 日改善，该维度在整个候选集内几乎恒为满分
    # （test_optimizations_p0 实测饱和在 W×1.0），不提供区分度——与上方已删除的
    # DAILY_RSI_OVERBOUGHT_PENALTY 同属「被前置闸门架空的死权重」。
    # 保留实现与参数，便于关闭 MACD 闸门做 A/B 时重新启用。
    RQ_W_MOMENTUM: float = 0.0
    RQ_DRAWDOWN_SATURATION: float = 0.45   # 回撤深度得分饱和点（与 MAX_DRAWDOWN_FROM_HIGH 解耦）

    GRADE_A: float = 80.0
    GRADE_B: float = 60.0
    GRADE_C: float = 40.0
    # 熊市环境下准入门槛上浮的分数（直接抬高分数线，展示分数与等级始终同源）
    BEAR_GRADE_BOOST: float = 10.0
    # 准入等级门槛：技术评分须不低于该等级对应分数（默认 B=60 分）。
    # C 级仅为单一趋势转折信号（40 分），噪音过大不再推荐；设为 "C" 可恢复旧行为。
    # 熊市环境门槛上浮 BEAR_GRADE_BOOST 分；未知/非熊市按原门槛。
    MIN_PASS_GRADE: str = "B"

    CACHE_EXPIRE_HOURS: float = 4.0
    MAX_WORKERS: int = 4
    DAILY_BARS: int = 600  # 自然日取数窗口，覆盖250根有效日线及指标预热
    WEEKLY_BARS: int = 60
    FETCH_DELAY: float = 0.05
    # 并发筛选时间预算（分钟）：超时取消未完成任务，按已完成结果出报告
    SCREEN_TIME_BUDGET_MIN: float = 240.0
    # 筛选进度日志间隔（每处理 N 只打印一次进度与 ETA）
    PROGRESS_LOG_EVERY: int = 500

    ADJUST: str = "qfq"
    USE_CACHE: bool = True
    CACHE_DIR: str = os.path.join(_PROJECT_ROOT, "cache")
    CACHE_TTL_DAYS: float = 6.0
    FUND_CACHE_TTL_DAYS: float = 7.0
    # 财报季度回溯次数：从「已披露窗口」的最新一季开始向前找，最多尝试几个季度
    # （覆盖跨年披露空窗：1-4 月依赖上年年报，未出则需回退到三季报甚至半年报）
    FUND_LOOKBACK_QUARTERS: int = 4
    FUND_START_YEAR: str = "2023"
    MAX_RETRY: int = 2
    LIST_MAX_RETRY: int = 4

    FILTER_ST: bool = True
    EXCLUDE_DELISTING: bool = True
    EXCLUDE_BSE: bool = True
    EXCLUDE_CHINEXT: bool = False
    EXCLUDE_STAR: bool = False
    # 仅保留普通 A 股账户可直接交易的沪深主板（60/00 开头）：创业板开户需 10 万资产、
    # 科创板/北交所/港股通需 50 万资产，对个人资金有门槛的板块全部排除；
    # 开启后上面 EXCLUDE_CHINEXT / EXCLUDE_STAR / EXCLUDE_BSE 三个开关冗余，
    # 关闭本项则退回由各开关组合控制
    MAIN_BOARD_ONLY: bool = True

# ===========================================================================
# CacheManager
# ===========================================================================

class CacheManager:
    def __init__(self, expire_hours: float = 4.0):
        self._expire_seconds = expire_hours * 3600
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        if key in self._store:
            ts, val = self._store[key]
            if time.time() - ts < self._expire_seconds: return val
            del self._store[key]
        return None

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)

    def clear(self) -> None:
        self._store.clear()

# ===========================================================================
# 数据源一：Baostock（主）— 连接管理
# ===========================================================================

class _BsCircuitOpen(Exception):
    """Baostock 熔断中，查询直接放弃（重试无意义，应立即降级 AkShare）"""

# Baostock 全局连接状态（多线程下简单赋值/自增在 GIL 保护下安全）
_bs_state = {
    "logged_in": False,
    "consecutive_failures": 0,
    "circuit_open": False,
}
_BS_CIRCUIT_THRESHOLD = 8  # 连续失败达到该次数后熔断（单次取数最多计 MAX_RETRY+1 次）

def _bs_login(max_retry: int = 5) -> bool:
    """Baostock 登录。bs.login 失败时也返回对象，必须检查 error_code 才算真正重试。"""
    if not _BS_AVAILABLE:
        return False
    if _bs_state["logged_in"]:
        return True  # 幂等：已登录直接返回，避免 run.py 预取市场环境后 main() 重复登录
    for attempt in range(max_retry):
        try:
            with bs_lock:
                lg = bs.login()
            if getattr(lg, "error_code", None) == "0":
                _bs_state["logged_in"] = True
                _bs_state["consecutive_failures"] = 0
                _bs_state["circuit_open"] = False
                return True
            logging.warning("Baostock 登录失败(%d/%d): %s", attempt + 1, max_retry, getattr(lg, "error_msg", "未知错误"))
        except Exception as e:
            logging.warning("Baostock 登录异常(%d/%d): %s", attempt + 1, max_retry, e)
        _bs_state["logged_in"] = False
        if attempt < max_retry - 1:
            time.sleep(1.0 * (attempt + 1))
    return False

def _bs_logout() -> None:
    """安全登出（仅在曾登录成功时调用）。"""
    if not _bs_state["logged_in"]:
        return
    try:
        with bs_lock:
            bs.logout()
    except Exception:
        pass
    _bs_state["logged_in"] = False

def _bs_mark_success() -> None:
    _bs_state["consecutive_failures"] = 0

def _bs_mark_failure() -> None:
    """记录一次查询失败：标记连接断开（下次查询前自动重连），连续失败达阈值则熔断。"""
    _bs_state["consecutive_failures"] += 1
    _bs_state["logged_in"] = False  # 可能连接已断开，下次查询前触发重新登录
    if _bs_state["consecutive_failures"] >= _BS_CIRCUIT_THRESHOLD and not _bs_state["circuit_open"]:
        _bs_state["circuit_open"] = True
        logger.warning("Baostock 连续失败 %d 次，已熔断，本次运行后续请求切换至 AkShare 备用数据源", _BS_CIRCUIT_THRESHOLD)

def _bs_available() -> bool:
    return not _bs_state["circuit_open"]

def is_data_degraded() -> bool:
    """P6：本次运行是否已降级到 AkShare 备用数据源（Baostock 熔断，或主源全程零命中）。

    降级日的直接后果：AkShare 逐 bar 不提供 peTTM/pbMRQ，quality_value 的估值闸门只能记
    缺项 → 候选被降级为 pending（待核验）→ formal 正式推荐恒为 0。若不显式标记，这种
    "数据降级导致的零推荐"会被误读为"今天没有好票"。run.py / run_breakout.py 据此在
    日志与飞书无信号卡片上明示数据状态。仅读全局状态，无副作用、可在任意时刻调用。
    """
    if _bs_state.get("circuit_open"):
        return True
    # 主源全程未命中、却有 AkShare 兜底命中 → 等价于降级（即使未触发熔断阈值）
    return fetch_stats.get("bs_ok", 0) == 0 and fetch_stats.get("ak_ok", 0) > 0

def _bs_guard(label: str) -> None:
    """查询前置守卫：熔断检查 + 断线自动重连。不可用时抛异常由重试层捕获。"""
    if _bs_state["circuit_open"]:
        raise _BsCircuitOpen(f"{label}: Baostock 熔断中")
    if not _bs_state["logged_in"]:
        if not _bs_login(max_retry=2):
            _bs_mark_failure()
            raise ConnectionError(f"{label}: Baostock 连接不可用")

def _format_bs_code(code: str) -> str:
    """将标准6位代码或Akshare格式转为Baostock格式 (如 sh.600000)"""
    if code.startswith(("sh.", "sz.", "bj.")): return code
    code = code.replace("sh", "").replace("sz", "").replace("bj", "")
    if code.startswith("6") or code == "000300": return f"sh.{code}"
    elif code.startswith(("0", "3")): return f"sz.{code}"
    elif code.startswith(("4", "8")): return f"bj.{code}"
    return code

def _bs_adjust_flag(adjust: str) -> str:
    """Baostock 复权标志: 3-不复权, 1-后复权, 2-前复权"""
    if adjust == "qfq": return "2"
    if adjust == "hfq": return "1"
    return "3"

_DATE_FMT = "%Y-%m-%d"
_NUMERIC_COLS = ("open", "close", "high", "low", "volume", "amount", "pct_chg", "turnover", "peTTM", "pbMRQ")

def _fetch_with_retry(fetcher: Callable[[], Any], max_retry: int, label: str, retry_on_empty: bool = False) -> Any:
    last_err = None
    for attempt in range(max_retry + 1):
        try:
            raw = fetcher()
            if raw is not None:
                if isinstance(raw, pd.DataFrame) and raw.empty and retry_on_empty:
                    last_err = RuntimeError(f"{label} DataFrame返回空")
                else:
                    return raw
            elif not retry_on_empty:
                return None
            else:
                last_err = RuntimeError(f"{label} 返回空")
        except _BsCircuitOpen:
            return None  # 熔断中，不再重试，立即返回让上层降级 AkShare
        except Exception as e:
            last_err = e
        if attempt < max_retry:
            time.sleep(0.5 * (attempt + 1))
    logging.debug("%s 获取失败（已重试 %d 次）: %s", label, max_retry, last_err)
    return None

def _cache_path(config: StrategyConfig, name: str) -> str:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    return os.path.join(config.CACHE_DIR, name)

def _cache_fresh_today(path: str) -> bool:
    if not os.path.exists(path): return False
    return datetime.fromtimestamp(os.path.getmtime(path)).date() == datetime.now().date()

def _cache_fresh(path: str, ttl_days: float) -> bool:
    if not os.path.exists(path): return False
    return datetime.now() - datetime.fromtimestamp(os.path.getmtime(path)) < timedelta(days=ttl_days)

def _read_cache_csv(path: str, dtype: Optional[dict] = None) -> Optional[pd.DataFrame]:
    try:
        df = pd.read_csv(path, dtype=dtype or {"date": str})
        return df if not df.empty else None
    except Exception:
        return None

def _write_cache_csv(df: pd.DataFrame, path: str) -> None:
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        df.to_csv(tmp, index=False)
        os.replace(tmp, path)
    except Exception:
        pass

def _read_cache_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None

def _write_cache_json(obj: dict, path: str) -> None:
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass

def _recompute_pct_chg(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """统一 pct_chg 口径：一律由 qfq 收盘价自算，不信任数据源提供的涨跌幅字段。

    三条取数路径的 pct_chg 口径原本互不一致：
      - Baostock  pctChg        （随 adjustflag 变化的复权口径）
      - AkShare 东财「涨跌幅」  （不复权原始口径：除权日把股息除权显示为暴跌）
      - AkShare 新浪            （接口无该列，_fetch_daily_ak_sina 本就自算）
    而 close 在双源都统一按 config.ADJUST="qfq" 取数，是唯一可横向比较的字段。

    该字段直接驱动 MAX_ENTRY_PCT_CHG / MIN_BREAKOUT_PCT / MAX_BREAKOUT_PCT 三个硬否决，
    口径漂移会让同一只股票「因本次由哪个数据源服务」而被不同判决——Baostock 熔断
    切换 AkShare 时整个股票池的过滤行为随之漂移，且回测无法复现。
    首行无前收置为 NaN（MIN_DAYS=60 保证评估用的是末行，不受影响）。
    """
    if df is None or df.empty or "close" not in df.columns:
        return df
    df = df.copy()
    df["pct_chg"] = (df["close"] / df["close"].shift(1) - 1) * 100
    return df


def _normalize_bs_hist(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None or df.empty: return None
    # Baostock 列名映射（pctChg 仅保留列名占位，取值随后由 _recompute_pct_chg 统一重算）
    rename_map = {"pctChg": "pct_chg", "turn": "turnover"}
    df = df.rename(columns=rename_map)
    if "date" not in df.columns or "close" not in df.columns: return None
    for col in _NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    if df.empty: return None
    return _recompute_pct_chg(df.sort_values("date").reset_index(drop=True))

def _fetch_hist_bs(code: str, period: str, start: str, end: str, config: StrategyConfig, cache_name: Optional[str] = None) -> Optional[pd.DataFrame]:
    bs_code = _format_bs_code(code)
    path = ""
    if config.USE_CACHE:
        name = cache_name or f"{period}_{bs_code}_{start}_{end}_{config.ADJUST or 'none'}.csv"
        path = _cache_path(config, name)
        if _cache_fresh_today(path):
            cached = _read_cache_csv(path)
            if cached is not None: return cached

    freq = "d" if period == "daily" else "w"
    adj = _bs_adjust_flag(config.ADJUST)
    # 估值字段（peTTM/pbMRQ）仅日线需要，与行情同一次请求返回，零额外成本；
    # AkShare 逐 bar 不提供估值，走兜底源时该两列缺失 → 估值闸门自动放行并记缺项。
    fields = "date,open,high,low,close,volume,amount,pctChg,turn"
    if period == "daily":
        fields += ",peTTM,pbMRQ"

    def fetch_data():
        _bs_guard(f"bs_hist({bs_code},{period})")
        with bs_lock:
            rs = bs.query_history_k_data_plus(bs_code, fields, start_date=start, end_date=end, frequency=freq, adjustflag=adj)
        if rs.error_code == '0':
            _bs_mark_success()
            return rs.get_data() if len(rs.data) > 0 else None  # 空 = 该标的确实无数据
        _bs_mark_failure()
        raise RuntimeError(f"bs_hist({bs_code}) 查询失败: {rs.error_msg}")

    raw_df = _fetch_with_retry(fetch_data, config.MAX_RETRY, f"bs_hist({bs_code},{period})")
    df = _normalize_bs_hist(raw_df)
    if df is not None and path: _write_cache_csv(df, path)
    return df

def _window_dates(bars: int, unit: str) -> tuple[str, str]:
    end = datetime.now()
    delta = timedelta(weeks=bars) if unit == "weeks" else timedelta(days=bars)
    return (end - delta).strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def _window_cache_name(period: str, code: str, bars: int, config: StrategyConfig) -> str:
    return f"{period}_{code}_last{bars}_{config.ADJUST or 'none'}.csv"

def _fetch_weekly_bs(code: str, weeks: int = 60, config: Optional[StrategyConfig] = None) -> Optional[pd.DataFrame]:
    config = config or StrategyConfig()
    start, end = _window_dates(weeks, "weeks")
    return _fetch_hist_bs(code, "weekly", start, end, config, _window_cache_name("weekly", _format_bs_code(code), weeks, config))

def _fetch_daily_bs(code: str, days: int = 120, config: Optional[StrategyConfig] = None) -> Optional[pd.DataFrame]:
    config = config or StrategyConfig()
    start, end = _window_dates(days, "days")
    return _fetch_hist_bs(code, "daily", start, end, config, _window_cache_name("daily", _format_bs_code(code), days, config))

def _fetch_index_daily_bs(symbol: str, config: StrategyConfig) -> Optional[pd.DataFrame]:
    bs_code = _format_bs_code(symbol)
    path = ""
    if config.USE_CACHE:
        path = _cache_path(config, f"index_daily_{bs_code}.csv")
        if _cache_fresh_today(path):
            if (cached := _read_cache_csv(path)) is not None: return cached

    start, end = _window_dates(config.DAILY_BARS + 30, "days")
    fields = "date,open,high,low,close,volume,amount,pctChg" # 指数无 turn

    def fetch_index():
        _bs_guard(f"bs_index({bs_code})")
        with bs_lock:
            rs = bs.query_history_k_data_plus(bs_code, fields, start_date=start, end_date=end, frequency="d")
        if rs.error_code == '0':
            _bs_mark_success()
            return rs.get_data() if len(rs.data) > 0 else None
        _bs_mark_failure()
        raise RuntimeError(f"bs_index({bs_code}) 查询失败: {rs.error_msg}")

    raw_df = _fetch_with_retry(fetch_index, config.MAX_RETRY, f"bs_index({bs_code})")
    df = _normalize_bs_hist(raw_df)
    if df is not None and path: _write_cache_csv(df, path)
    return df

def _fetch_index_weekly_bs(symbol: str, weeks: int = 60, config: Optional[StrategyConfig] = None) -> Optional[pd.DataFrame]:
    config = config or StrategyConfig()
    df = _fetch_index_daily_bs(symbol, config)
    if df is None: return None
    dt = pd.to_datetime(df["date"])
    start, end = _window_dates(weeks, "weeks")
    sub = df[(dt >= pd.to_datetime(start)) & (dt <= pd.to_datetime(end))].copy()
    if sub.empty: return None
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    agg = {k: v for k, v in agg.items() if k in sub.columns}
    sub["_dt"] = pd.to_datetime(sub["date"])
    weekly = sub.set_index("_dt").resample("W").agg(agg).dropna(subset=["close"]).reset_index()
    weekly["date"] = weekly["_dt"].dt.strftime(_DATE_FMT)
    return weekly.drop(columns=["_dt"])

def _quarter_candidates(now: datetime, n: int = 4) -> list[tuple[int, int]]:
    """按交易所披露截止日推算「已确定公开」的财报季度候选（由新到旧，最多 n 个）。

    披露截止：一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30 前披露完毕。
    原实现按「当前季度」取数（(month - 1) // 3），在 2–4 月与 7 月必然取空，
    导致基本面防雷层整层静默失效；此处改为按已披露窗口回溯。
    """
    m, y = now.month, now.year
    if m >= 11:   y0, q0 = y, 3          # 三季报窗口
    elif m >= 9:  y0, q0 = y, 2          # 半年报窗口
    elif m >= 5:  y0, q0 = y, 1          # 一季报窗口
    else:         y0, q0 = y - 1, 4      # 1–4 月：依赖上年年报（未出则回溯到三季报）
    out: list[tuple[int, int]] = []
    q, yy = q0, y0
    for _ in range(max(1, n)):
        out.append((yy, q))
        q -= 1
        if q == 0: q, yy = 4, yy - 1
    return out


def _annualize_roe(roe_ytd: float, quarter: int) -> float:
    """把年初至今累计 ROE 线性年化为全年口径，与 MIN_ROE（年化阈值）可比。

    Baostock/AkShare 的 ROE 均为「报告期累计值」（如 Q2 = 半年报累计），
    直接拿单季/累计值与年化阈值比较会让门槛随财报日历漂移（Q1 数据天然只有
    全年的 1/4，极严；Q4 又等于年化值）。线性年化：Q1×4 / Q2×2 / Q3×4/3 / Q4×1。
    """
    factor = {1: 4.0, 2: 2.0, 3: 4.0 / 3.0, 4: 1.0}.get(int(quarter), 1.0)
    return roe_ytd * factor


def _fetch_fundamentals_bs(code: str, config: Optional[StrategyConfig] = None) -> Optional[dict]:
    config = config or StrategyConfig()
    bs_code = _format_bs_code(code)
    quarters = _quarter_candidates(datetime.now(), n=config.FUND_LOOKBACK_QUARTERS)
    path = ""
    if config.USE_CACHE:
        # 缓存名带起始季度标签：财报窗口滚动后自动失效，避免复用上一季的旧数据。
        # fund_v2_ 前缀：v2 起 ROE 为年化口径且含 report_period，与旧 fund_* 缓存隔离。
        path = _cache_path(config, f"fund_v2_{bs_code}_{quarters[0][0]}Q{quarters[0][1]}.json")
        if _cache_fresh(path, config.FUND_CACHE_TTL_DAYS):
            if (cached := _read_cache_json(path)) is not None: return cached

    # 商誉与扣非净利润在 Baostock 中缺失，置为 None；report_period 记录数据实际所属报告期
    result: dict[str, Optional[float] | Optional[str]] = {
        "roe": None, "debt_ratio": None, "goodwill_ratio": None, "deducted_profit_ratio": None,
        "report_period": None,
    }

    # 逐季回溯：从已披露窗口的最新一季往前找，取到数据即停
    for (year, quarter) in quarters:
        if result["roe"] is not None and result["debt_ratio"] is not None:
            break

        def fetch_fund(_y: int = year, _q: int = quarter):
            _bs_guard(f"bs_fund({bs_code},{_y}Q{_q})")
            with bs_lock:
                p_rs = bs.query_profit_data(code=bs_code, year=_y, quarter=_q)
                b_rs = bs.query_balance_data(code=bs_code, year=_y, quarter=_q)
            if getattr(p_rs, "error_code", None) == "0" or getattr(b_rs, "error_code", None) == "0":
                _bs_mark_success()
                return p_rs, b_rs
            _bs_mark_failure()
            raise RuntimeError(f"bs_fund({bs_code}) 查询失败: {getattr(p_rs, 'error_msg', '')} / {getattr(b_rs, 'error_msg', '')}")

        rs = _fetch_with_retry(fetch_fund, config.MAX_RETRY, f"bs_fund({bs_code},{year}Q{quarter})")
        if not rs:
            if _bs_state["circuit_open"]:
                break  # 已熔断，继续回溯只是无效重试，直接放弃让上层降级 AkShare
            continue
        p_rs, b_rs = rs
        got = False
        if p_rs.error_code == '0' and len(p_rs.data) > 0 and result["roe"] is None:
            df = p_rs.get_data()
            if "roeAvg" in df.columns:
                roe = pd.to_numeric(df["roeAvg"].iloc[0], errors="coerce")
                if not pd.isna(roe):
                    # Baostock 返回累计小数(如0.05)：×100 转百分比后按报告期线性年化
                    result["roe"] = _annualize_roe(float(roe) * 100, quarter)
                    got = True
        if b_rs.error_code == '0' and len(b_rs.data) > 0 and result["debt_ratio"] is None:
            df = b_rs.get_data()
            # Baostock 资产负债率字段为 liabilityToAsset（小数形式，兼容其他可能的字段名）
            debt_col = next((c for c in ("liabilityToAsset", "liabToAsset", "liabRate") if c in df.columns), None)
            if debt_col:
                debt = pd.to_numeric(df[debt_col].iloc[0], errors="coerce")
                if not pd.isna(debt):
                    result["debt_ratio"] = float(debt) * 100
                    got = True
        if got and result["report_period"] is None:
            result["report_period"] = f"{year}Q{quarter}"

    if result["roe"] is None and result["debt_ratio"] is None: return None
    if path: _write_cache_json(result, path)
    return result

def _fetch_growth_bs(code: str, config: Optional[StrategyConfig] = None) -> Optional[dict]:
    """Baostock 成长能力（query_growth_data）：取最新已披露报告期的净利润同比(YOYNI)。

    用于「前瞻确认」闸门（REQUIRE_FORWARD_CONFIRMATION）：年度质量与 PE/PB 都是后视镜，
    当年净利大幅下滑（同比 < FORWARD_NI_YOY_MIN）即判为基本面正在恶化（价值陷阱）。
    返回 {"ni_yoy": 百分比float|None, "stat_date": "YYYYQn"|None}；无数据/熔断返回 None
    （上层记 missing_tag「forward」降级为待核验，不硬否决）。仅主源 Baostock 提供，
    AkShare 兜底日拿不到成长数据 → 同样按缺失处理。
    """
    config = config or StrategyConfig()
    if not _BS_AVAILABLE:
        return None
    bs_code = _format_bs_code(code)
    quarters = _quarter_candidates(datetime.now(), n=config.FUND_LOOKBACK_QUARTERS)
    path = ""
    if config.USE_CACHE:
        path = _cache_path(config, f"growth_v1_{bs_code}_{quarters[0][0]}Q{quarters[0][1]}.json")
        if _cache_fresh(path, config.FUND_CACHE_TTL_DAYS):
            if (cached := _read_cache_json(path)) is not None:
                return cached

    result: dict[str, Optional[float] | Optional[str]] = {"ni_yoy": None, "stat_date": None}
    for (year, quarter) in quarters:
        if result["ni_yoy"] is not None:
            break

        def fetch_growth(_y: int = year, _q: int = quarter):
            _bs_guard(f"bs_growth({bs_code},{_y}Q{_q})")
            with bs_lock:
                rs = bs.query_growth_data(code=bs_code, year=_y, quarter=_q)
            if getattr(rs, "error_code", None) == "0":
                _bs_mark_success()
                return rs
            _bs_mark_failure()
            raise RuntimeError(f"bs_growth({bs_code}) 查询失败: {getattr(rs, 'error_msg', '')}")

        rs = _fetch_with_retry(fetch_growth, config.MAX_RETRY, f"bs_growth({bs_code},{year}Q{quarter})")
        if not rs:
            if _bs_state["circuit_open"]:
                break
            continue
        try:
            if getattr(rs, "error_code", None) == "0" and len(rs.data) > 0:
                df = rs.get_data()
                if "YOYNI" in df.columns:
                    ni = pd.to_numeric(df["YOYNI"].iloc[0], errors="coerce")
                    if not pd.isna(ni):
                        # Baostock 同比为小数（0.15=15%）：×100 转百分比，与 FORWARD_NI_YOY_MIN 同口径
                        result["ni_yoy"] = float(ni) * 100
                        result["stat_date"] = f"{year}Q{quarter}"
        except Exception as e:  # noqa: BLE001
            logging.debug("成长数据解析失败(%s): %s", bs_code, e)

    if result["ni_yoy"] is None:
        return None
    if path:
        _write_cache_json(result, path)
    return result

def _fetch_stock_pool_bs(config: Optional[StrategyConfig] = None) -> list[dict]:
    config = config or StrategyConfig()
    path = ""
    df: Optional[pd.DataFrame] = None
    if config.USE_CACHE:
        path = _cache_path(config, "stock_list_bs.csv")
        if _cache_fresh(path, config.CACHE_TTL_DAYS):
            df = _read_cache_csv(path, dtype={"code": str, "name": str})

    if df is None or "code" not in df.columns or "name" not in df.columns:
        def fetch_list():
            _bs_guard("bs_all_stock")
            # 非交易日（周末/节假日）query_all_stock 返回空，逐日前退找最近交易日
            for i in range(10):
                day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                with bs_lock:
                    rs = bs.query_all_stock(day=day)
                if rs.error_code != '0':
                    _bs_mark_failure()
                    raise RuntimeError(f"bs_all_stock({day}) 查询失败: {rs.error_msg}")
                _bs_mark_success()
                if len(rs.data) > 0:
                    if i > 0:
                        logger.info("今日非交易日，股票列表回退至最近交易日 %s", day)
                    return rs.get_data()
            return None  # 连续 10 天均为空（极端异常）
        
        raw = _fetch_with_retry(fetch_list, config.LIST_MAX_RETRY, "bs_all_stock")
        if raw is None or raw.empty: return []

        # 仅保留 A 股股票（沪 60/68、深 00/30、北 4/8），剔除指数/基金/债券。
        # query_all_stock 返回全部证券（7171 行中约 1700 只非股票），非股票代码
        # 后续查询会报"股票代码应为9位"，并污染熔断计数导致误切 AkShare
        raw = raw[raw["code"].str.match(r"^(sh\.(60|68)|sz\.(00|30)|bj\.(4|8))", na=False)]
        # 过滤处于交易状态的股票
        raw = raw[raw['tradeStatus'] == '1'].copy()
        raw = raw.rename(columns={"code_name": "name"})
        raw["code"] = raw["code"].apply(lambda x: x.split(".")[1] if "." in x else x)
        df = raw[["code", "name"]]
        if path: _write_cache_csv(df, path)

    return _apply_pool_filters(df, config).to_dict("records")


def _apply_pool_filters(df: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    """股票池过滤（Baostock / AkShare 共用），入参需含 code/name 列"""
    out = df.copy()
    out["code"] = out["code"].astype(str).str.strip()
    # A 股代码固定 6 位；港股（5 位，如 00700）等非 A 股代码直接排除，
    # 避免 zfill 补零后伪装成深市主板代码
    out = out[out["code"].str.fullmatch(r"\d{6}", na=False)]
    out["name"] = out["name"].astype(str)
    if config.MAIN_BOARD_ONLY:
        # 白名单仅保留沪深主板：60=沪主板(600/601/603/605)，00=深主板(000/001/002/003)；
        # 创业板(30)/科创板(68)/北交所(4/8/92)/B股(200/900)等开户有资金门槛的板块全部排除
        out = out[out["code"].str.startswith(("60", "00"))]
    if config.EXCLUDE_BSE: out = out[~out["code"].str.startswith(("8", "4", "92"))]
    if config.EXCLUDE_CHINEXT: out = out[~out["code"].str.startswith("30")]
    if config.EXCLUDE_STAR: out = out[~out["code"].str.startswith("68")]
    if config.FILTER_ST: out = out[~out["name"].str.contains("ST", case=False, na=False)]
    if config.EXCLUDE_DELISTING: out = out[~out["name"].str.contains("退", na=False)]
    return out.reset_index(drop=True)


def _fetch_industry_bs(config: Optional[StrategyConfig] = None) -> dict[str, str]:
    """一次性拉取全市场行业分类（6 位代码 -> 行业名），供推荐结果的行业分散使用。
    失败或无数据时返回空 dict，调用方降级为不做行业去重（不阻断选股流程）。"""
    config = config or StrategyConfig()
    path = ""
    if config.USE_CACHE:
        path = _cache_path(config, "industry_bs.json")
        if _cache_fresh(path, config.INDUSTRY_CACHE_TTL_DAYS):
            if (cached := _read_cache_json(path)) is not None: return cached

    out: dict[str, str] = {}

    def fetch_industry():
        _bs_guard("bs_industry")
        with bs_lock:
            rs = bs.query_stock_industry()
        if getattr(rs, "error_code", None) == "0":
            _bs_mark_success()
            return rs
        _bs_mark_failure()
        raise RuntimeError(f"bs_industry 查询失败: {getattr(rs, 'error_msg', '')}")

    rs = _fetch_with_retry(fetch_industry, config.MAX_RETRY, "bs_industry", retry_on_empty=True)
    if rs is not None:
        try:
            for _, row in rs.get_data().iterrows():
                code = str(row.get("code", "")).split(".")[-1].strip()
                industry = str(row.get("industry", "") or "").strip()
                if code and industry: out[code] = industry
        except Exception as e:
            logging.debug("行业分类解析失败: %s", e)

    if out and path: _write_cache_json(out, path)
    return out


# ===========================================================================
# 数据源二：AkShare（备用，Baostock 失败/熔断时自动切换）
# ===========================================================================

_AK_COLUMN_MAP = {
    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
    "最低": "low", "成交量": "volume", "成交额": "amount",
    "涨跌幅": "pct_chg", "换手率": "turnover",
}
_AK_INDEX_NUMERIC_COLS = ("open", "high", "low", "close", "volume")

def _ak_symbol(code: str) -> str:
    """转为 AkShare 6 位数字代码（如 sh.600000 / 600000 -> 600000）"""
    return code.replace("sh.", "").replace("sz.", "").replace("bj.", "").zfill(6)

def _normalize_ak_hist(raw: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """AkShare 个股行情标准化：中文列名映射为统一英文列名"""
    if raw is None or raw.empty: return None
    df = raw.rename(columns=_AK_COLUMN_MAP)
    keep = [c for c in _AK_COLUMN_MAP.values() if c in df.columns]
    if "date" not in keep or "close" not in keep: return None
    df = df[keep].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime(_DATE_FMT)
    for col in _NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    if df.empty: return None
    return _recompute_pct_chg(df.sort_values("date").reset_index(drop=True))

def _ak_sina_symbol(code: str) -> str:
    """转为新浪格式（如 sh600000 / sz000001）"""
    c = _ak_symbol(code)
    return f"sh{c}" if c.startswith("6") else f"sz{c}"

def _fetch_daily_ak_sina(code: str, days: int, config: StrategyConfig) -> Optional[pd.DataFrame]:
    """新浪通道兜底：东财接口不可达时使用。返回全量历史需截窗；无 pct_chg 列，用收盘价自算。"""
    symbol = _ak_sina_symbol(code)
    raw = _fetch_with_retry(
        lambda: ak.stock_zh_a_daily(symbol=symbol, adjust=config.ADJUST),
        config.MAX_RETRY, f"ak_sina({symbol})"
    )
    if raw is None or raw.empty or "date" not in raw.columns or "close" not in raw.columns: return None
    keep = [c for c in ("date", "open", "high", "low", "close", "volume", "amount", "turnover") if c in raw.columns]
    df = raw[keep].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime(_DATE_FMT)
    for col in _NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    df["pct_chg"] = df["close"].pct_change() * 100
    df = df.tail(days + 30).reset_index(drop=True)
    return df if not df.empty else None

def _fetch_daily_ak(code: str, days: int = 120, config: Optional[StrategyConfig] = None) -> Optional[pd.DataFrame]:
    if not _AK_AVAILABLE: return None
    config = config or StrategyConfig()
    symbol = _ak_symbol(code)
    path = ""
    if config.USE_CACHE:
        # 纯数字命名，与 Baostock 缓存（带 sh. 前缀）天然隔离
        path = _cache_path(config, _window_cache_name("daily", symbol, days, config))
        if _cache_fresh_today(path):
            if (cached := _read_cache_csv(path)) is not None: return cached
    start, end = _window_dates(days, "days")
    # 通道 1：东方财富
    raw = _fetch_with_retry(
        lambda: ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                   start_date=start.replace("-", ""), end_date=end.replace("-", ""),
                                   adjust=config.ADJUST),
        config.MAX_RETRY, f"ak_hist({symbol})"
    )
    df = _normalize_ak_hist(raw)
    # 通道 2：新浪（东财不可达时兜底）
    if df is None:
        df = _fetch_daily_ak_sina(code, days, config)
    if df is not None and path: _write_cache_csv(df, path)
    return df

def _fetch_index_daily_ak(symbol: str, config: StrategyConfig) -> Optional[pd.DataFrame]:
    if not _AK_AVAILABLE: return None
    ak_symbol = symbol.replace(".", "")  # sh000300 格式
    path = ""
    if config.USE_CACHE:
        path = _cache_path(config, f"index_daily_{ak_symbol}.csv")
        if _cache_fresh_today(path):
            if (cached := _read_cache_csv(path)) is not None: return cached
    raw = _fetch_with_retry(
        lambda: ak.stock_zh_index_daily(symbol=ak_symbol),
        config.MAX_RETRY, f"ak_index({ak_symbol})", retry_on_empty=True
    )
    if raw is None or "date" not in raw.columns or "close" not in raw.columns: return None
    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime(_DATE_FMT)
    for col in _AK_INDEX_NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    if df.empty: return None
    # 接口返回全量历史，截取与 Baostock 对齐的窗口
    df = df.sort_values("date").tail(config.DAILY_BARS + 30).reset_index(drop=True)
    if path: _write_cache_csv(df, path)
    return df

def _fetch_fundamentals_ak(code: str, config: Optional[StrategyConfig] = None) -> Optional[dict]:
    """AkShare 基本面：相比 Baostock 额外补齐商誉占比与扣非利润占比。

    口径与主源一致（v2）：ROE 取「显式按日期列排序后的最大报告期」累计值并按
    该报告期线性年化，结果带 report_period。旧 fund_* 缓存（单季未年化口径）隔离废弃。
    """
    if not _AK_AVAILABLE: return None
    config = config or StrategyConfig()
    symbol = _ak_symbol(code)
    path = ""
    if config.USE_CACHE:
        path = _cache_path(config, f"fund_v2_{symbol}.json")
        if _cache_fresh(path, config.FUND_CACHE_TTL_DAYS):
            if (cached := _read_cache_json(path)) is not None: return cached

    result: dict[str, Optional[float] | Optional[str]] = {
        "roe": None, "debt_ratio": None, "goodwill_ratio": None, "deducted_profit_ratio": None,
        "report_period": None,
    }

    df_fin = _fetch_with_retry(lambda: ak.stock_financial_analysis_indicator(symbol=symbol, start_year=config.FUND_START_YEAR), config.MAX_RETRY, f"ak_fund({symbol})")
    if df_fin is not None and not df_fin.empty:
        # 显式按日期列取最大报告期，不假定接口返回顺序
        date_col = next((c for c in df_fin.columns if "日期" in str(c) or "报告期" in str(c)), None)
        if date_col is not None:
            df_fin = df_fin.copy()
            df_fin["_dt"] = pd.to_datetime(df_fin[date_col], errors="coerce")
            df_fin = df_fin.dropna(subset=["_dt"]).sort_values("_dt", ascending=False)
        if not df_fin.empty:
            row = df_fin.iloc[0]
            report_dt = row["_dt"] if date_col is not None else pd.NaT
            report_quarter = int((int(report_dt.month) - 1) // 3 + 1) if pd.notna(report_dt) else None
            for col in df_fin.columns:
                col_str, col_lower = str(col), str(col).lower()
                if "净资产收益率" in col_str or "roe" in col_lower:
                    val = pd.to_numeric(row[col], errors="coerce")
                    if not pd.isna(val):
                        result["roe"] = _annualize_roe(float(val), report_quarter) if report_quarter else float(val)
                if "资产负债率" in col_str or "debt" in col_lower:
                    val = pd.to_numeric(row[col], errors="coerce")
                    if not pd.isna(val): result["debt_ratio"] = float(val)
            if pd.notna(report_dt) and (result["roe"] is not None or result["debt_ratio"] is not None):
                result["report_period"] = f"{int(report_dt.year)}Q{report_quarter}"

    df_bs = _fetch_with_retry(lambda: ak.stock_balance_sheet_by_report_em(symbol=symbol), config.MAX_RETRY, f"ak_bs({symbol})")
    if df_bs is not None and not df_bs.empty:
        row = df_bs.iloc[0]
        goodwill, net_assets = 0.0, 0.0
        for col in df_bs.columns:
            if "商誉" in str(col):
                val = pd.to_numeric(row.get(col), errors="coerce")
                if not pd.isna(val): goodwill = float(val)
            if "股东权益合计" in str(col) or "净资产" in str(col):
                val = pd.to_numeric(row.get(col), errors="coerce")
                if not pd.isna(val) and val > 0: net_assets = float(val)
        if net_assets > 0: result["goodwill_ratio"] = goodwill / net_assets * 100

    df_income = _fetch_with_retry(lambda: ak.stock_profit_sheet_by_report_em(symbol=symbol), config.MAX_RETRY, f"ak_income({symbol})")
    if df_income is not None and not df_income.empty:
        row = df_income.iloc[0]
        net_profit, deducted_profit = 0.0, 0.0
        for col in df_income.columns:
            col_str = str(col)
            if "净利润" in col_str and "扣" not in col_str and "归" not in col_str:
                val = pd.to_numeric(row.get(col), errors="coerce")
                if not pd.isna(val): net_profit = float(val)
            if "扣非" in col_str or "扣除非经常" in col_str:
                val = pd.to_numeric(row.get(col), errors="coerce")
                if not pd.isna(val): deducted_profit = float(val)
        if net_profit > 0: result["deducted_profit_ratio"] = deducted_profit / net_profit

    if result["roe"] is None and result["debt_ratio"] is None \
            and result["goodwill_ratio"] is None and result["deducted_profit_ratio"] is None:
        return None
    if path: _write_cache_json(result, path)
    return result

def _fetch_stock_pool_ak(config: Optional[StrategyConfig] = None) -> list[dict]:
    if not _AK_AVAILABLE: return []
    config = config or StrategyConfig()
    path = ""
    df: Optional[pd.DataFrame] = None
    if config.USE_CACHE:
        path = _cache_path(config, "stock_list.csv")
        if _cache_fresh(path, config.CACHE_TTL_DAYS):
            df = _read_cache_csv(path, dtype={"code": str, "name": str})
            if df is not None and ("code" not in df.columns or "name" not in df.columns): df = None
    if df is None:
        raw = _fetch_with_retry(lambda: ak.stock_info_a_code_name(), config.LIST_MAX_RETRY, "ak_stock_info", retry_on_empty=True)
        if raw is None or "code" not in raw.columns or "name" not in raw.columns: return []
        df = raw[["code", "name"]].copy()
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["name"] = df["name"].astype(str)
        if path: _write_cache_csv(df, path)
    return _apply_pool_filters(df, config).to_dict("records")


# ===========================================================================
# 主备数据源路由（Baostock 优先，失败/熔断自动降级 AkShare）
# ===========================================================================

def _fetch_daily_dual(code: str, days: int, config: StrategyConfig) -> Optional[pd.DataFrame]:
    df = None
    if _bs_available():
        df = _fetch_daily_bs(code, days=days, config=config)
        if df is not None:
            fetch_stats["bs_ok"] += 1
    if df is None:
        df = _fetch_daily_ak(code, days=days, config=config)
        if df is not None:
            fetch_stats["ak_ok"] += 1
        else:
            fetch_stats["fail"] += 1
    # 磁盘缓存命中路径（_read_cache_csv）不经过 _normalize_*，旧世代缓存里存的仍是
    # 数据源原始 pct_chg。在出口统一重算，使口径与缓存写入时间无关。
    return _recompute_pct_chg(df)

def _fetch_weekly_dual(code: str, config: StrategyConfig) -> Optional[pd.DataFrame]:
    """周线双源拉取（仅决赛圈周线趋势确认使用）：Baostock 优先，失败降级 AkShare"""
    df = None
    if _bs_available():
        df = _fetch_weekly_bs(code, weeks=config.WEEKLY_BARS, config=config)
    if df is None and _AK_AVAILABLE:
        symbol = _ak_symbol(code)
        start = (datetime.now() - timedelta(weeks=config.WEEKLY_BARS)).strftime("%Y%m%d")
        end = datetime.now().strftime("%Y%m%d")
        raw = _fetch_with_retry(
            lambda: ak.stock_zh_a_hist(symbol=symbol, period="weekly", start_date=start, end_date=end, adjust=config.ADJUST),
            config.MAX_RETRY, f"ak_weekly({symbol})"
        )
        df = _normalize_ak_hist(raw)
    return _recompute_pct_chg(df)   # 同 _fetch_daily_dual：覆盖旧世代磁盘缓存

def _fetch_index_daily_dual(symbol: str, config: StrategyConfig) -> Optional[pd.DataFrame]:
    if _bs_available():
        df = _fetch_index_daily_bs(symbol, config)
        if df is not None: return df
    return _fetch_index_daily_ak(symbol, config)

def _fetch_fundamentals_dual(code: str, config: Optional[StrategyConfig] = None) -> Optional[dict]:
    if _bs_available():
        data = _fetch_fundamentals_bs(code, config)
        if data is not None: return data
    return _fetch_fundamentals_ak(code, config)

def _fetch_stock_pool_dual(config: StrategyConfig) -> list[dict]:
    if _bs_available():
        stocks = _fetch_stock_pool_bs(config)
        if stocks: return stocks
    return _fetch_stock_pool_ak(config)


# ===========================================================================
# 统一接口路由 (对接原代码的函数签名)
# ===========================================================================

def get_daily_data(code: str, config: StrategyConfig, cache: Optional[CacheManager] = None) -> Optional[pd.DataFrame]:
    cache_key = f"daily_{code}"
    if cache and (cached := cache.get(cache_key)) is not None: return cached
    df = _fetch_daily_dual(code, days=config.DAILY_BARS, config=config)
    if cache and df is not None: cache.set(cache_key, df)
    return df

def get_index_daily(config: StrategyConfig, cache: Optional[CacheManager] = None) -> Optional[pd.DataFrame]:
    cache_key = "index_daily_csi300"
    if cache and (cached := cache.get(cache_key)) is not None: return cached
    df = _fetch_index_daily_dual(config.CSI300_AK_SYMBOL, config)
    if cache and df is not None: cache.set(cache_key, df)
    return df

def get_fundamentals(code: str, cache: Optional[CacheManager] = None, config: Optional[StrategyConfig] = None) -> Optional[dict]:
    cache_key = f"fund_{code}"
    if cache and (cached := cache.get(cache_key)) is not None: return cached
    data = _fetch_fundamentals_dual(code, config)
    if cache and data is not None: cache.set(cache_key, data)
    return data

def get_growth(code: str, cache: Optional[CacheManager] = None, config: Optional[StrategyConfig] = None) -> Optional[dict]:
    """最新报告期成长能力（净利润同比），供前瞻确认闸门使用。仅 Baostock 提供，
    主源不可用/无返回时为 None（上层记「forward」缺项降级为待核验）。"""
    cache_key = f"growth_{code}"
    if cache and (cached := cache.get(cache_key)) is not None: return cached
    data = _fetch_growth_bs(code, config) if _bs_available() else None
    if cache and data is not None: cache.set(cache_key, data)
    return data

def get_stock_list(config: StrategyConfig, cache: Optional[CacheManager] = None) -> list[dict]:
    cache_key = "stock_list"
    if cache and (cached := cache.get(cache_key)) is not None: return cached
    stocks = _fetch_stock_pool_dual(config)
    if cache and stocks: cache.set(cache_key, stocks)
    return stocks

def get_stock_industry(config: StrategyConfig, cache: Optional[CacheManager] = None) -> dict[str, str]:
    """6 位代码 -> 行业名（用于推荐结果的行业分散）。
    仅主源 Baostock 提供；主源不可用时返回空 dict，调用方自动跳过行业去重。"""
    if cache and (cached := cache.get("industry_map")) is not None: return cached
    industries = _fetch_industry_bs(config) if _bs_available() else {}
    if cache is not None: cache.set("industry_map", industries)
    return industries


# ===========================================================================
# Strategy Logic (Unchanged Layer 1-4)
# ===========================================================================

def compute_market_environment(df_index: pd.DataFrame, config: StrategyConfig) -> dict:
    if df_index is None or df_index.empty or len(df_index) < config.MARKET_MA_PERIOD + config.MARKET_SLOPE_LOOKBACK:
        return {"regime": "unknown", "description": "未知（沪深300数据不足）", "ma20": 0, "slope": 0, "close": 0,
                "ma60": 0, "trend": "unknown"}
    df = df_index.copy().reset_index(drop=True)
    df["ma"] = df["close"].rolling(config.MARKET_MA_PERIOD).mean()
    cur, prev = df.iloc[-1], df.iloc[-(1 + config.MARKET_SLOPE_LOOKBACK)]
    ma_now, ma_prev, close_now = float(cur["ma"]), float(prev["ma"]), float(cur["close"])
    slope = (ma_now - ma_prev) / ma_prev if ma_prev > 0 else 0.0
    # 第一指标：MA20 斜率（原始判据，与改动前一致）
    if slope > config.MARKET_BULL_SLOPE: slope_regime = "bull"
    elif slope < config.MARKET_BEAR_SLOPE: slope_regime = "bear"
    else: slope_regime = "neutral"
    # ===== P2：第二指标 = 长期均线 MA60 趋势（现价与 MA20 相对 MA60 的位置）=====
    # 单一 MA20 斜率在 ±0.01 阈值附近会逐日抖动，靠滞回打补丁治标不治本。叠加一个
    # 慢速趋势过滤：只有「斜率看多 且 现价/MA20 均在 MA60 上方」才判 bull，
    # 「斜率看空 且 均在 MA60 下方」才判 bear，两者不同向一律降级为 neutral。
    # 双指标同向确认从源头减少 regime 翻转；MA60 数据不足时自动退回单指标（口径不变）。
    ma_long_period = max(1, int(getattr(config, "MARKET_MA_LONG", 60)))
    ma_long_ser = df["close"].rolling(ma_long_period).mean()
    ma_long_now = float(ma_long_ser.iloc[-1]) if not pd.isna(ma_long_ser.iloc[-1]) else None
    dual = bool(getattr(config, "MARKET_REGIME_DUAL_INDICATOR", True))
    if ma_long_now is not None:
        trend_up = close_now > ma_long_now and ma_now > ma_long_now
        trend_down = close_now < ma_long_now and ma_now < ma_long_now
        trend = "up" if trend_up else ("down" if trend_down else "mixed")
    else:
        trend_up = trend_down = False
        trend = "unknown"
    if dual and ma_long_now is not None:
        if slope_regime == "bull" and trend_up: regime = "bull"
        elif slope_regime == "bear" and trend_down: regime = "bear"
        else: regime = "neutral"
    else:
        # MA60 数据不足或未启用双指标：退回单指标判据，与改动前逐字节一致
        regime = slope_regime
    _zh = {"bull": "偏多", "bear": "偏空", "neutral": "中性"}[regime]
    _trend_zh = {"up": "上行", "down": "下行", "mixed": "纠缠", "unknown": "数据不足"}[trend]
    desc = (f"{_zh}（MA20斜率 {slope:.4f}，MA{ma_long_period}趋势 {_trend_zh}，"
            f"沪深300收于 {close_now:.0f}）")
    if dual and ma_long_now is not None and regime != slope_regime:
        desc += f"［双指标：斜率判 {slope_regime}，MA{ma_long_period}未同向确认，降级中性］"
    return {"regime": regime, "description": desc, "ma20": round(ma_now, 2), "slope": round(slope, 6),
            "close": round(close_now, 2), "ma60": round(ma_long_now, 2) if ma_long_now is not None else 0,
            "trend": trend, "slope_regime": slope_regime}

def _effective_regime(regime: str, config: StrategyConfig) -> str:
    """Map unavailable market state to the configured conservative regime."""
    value = str(regime or "unknown").lower()
    if value == "unknown" and getattr(config, "UNKNOWN_AS_BEAR", True):
        return "bear"
    return value if value in {"bull", "neutral", "bear"} else "neutral"


def resolve_max_picks(regime: str, config: StrategyConfig) -> int:
    """按市场环境解析本次运行的推荐数量上限（main / backtest 共用，避免口径漂移）。

    牛 = MAX_PICKS，中性 = NEUTRAL_MAX_PICKS，熊（含 unknown 折叠）= BEAR_MAX_PICKS。
    配置类未提供 NEUTRAL_/BEAR_ 字段时退回 MAX_PICKS（保持旧行为）。
    注：放量突破策略走自己的 BEAR_MAX_PICKS_BREAKOUT 分支（熊市直接空仓 0 只），
        不使用本函数。
    """
    eff = _effective_regime(regime, config)
    if eff == "bull":
        return int(config.MAX_PICKS)
    if eff == "neutral":
        return int(getattr(config, "NEUTRAL_MAX_PICKS", config.MAX_PICKS))
    return int(getattr(config, "BEAR_MAX_PICKS", config.MAX_PICKS))


def market_crash_halt(index_df: Optional[pd.DataFrame], config: StrategyConfig) -> Optional[float]:
    """市场级熔断判定：返回触发熔断的累计跌幅（%），未触发或数据不足返回 None。

    抄底信号在指数急跌期间会全市场批量触发（所有股票同时 RSI 超卖、同时创新低
    产生底背离、同时 MA5 向下穿越后拐头），此时恰恰是接飞刀最危险的时候；
    MA20_TREND_MIN_SLOPE 是个股级过滤，无法识别「全市场同步下跌」这种系统性事件。
    数据不足时不触发（返回 None），避免指数缺数时把正常交易日误判为熔断。
    """
    n = int(getattr(config, "MARKET_CRASH_LOOKBACK", 5))
    threshold = float(getattr(config, "MARKET_CRASH_HALT_PCT", -4.0))
    if n <= 0 or index_df is None or index_df.empty or "close" not in index_df.columns:
        return None
    closes = pd.to_numeric(index_df["close"], errors="coerce").dropna().to_numpy(dtype=float)
    if len(closes) <= n:
        return None
    ret = (closes[-1] / closes[-1 - n] - 1) * 100
    return ret if ret < threshold else None


def _apply_regime_hysteresis(result: dict, config: StrategyConfig) -> dict:
    """regime 滞回：牛/熊/中性切换须连续 MARKET_REGIME_CONFIRM_DAYS 日同向确认。

    单一沪深300 MA20 斜率代理在 ±0.01 阈值附近会逐日跳变（今天 bull 明天 neutral），
    导致推荐数量上限与评分门槛跟着抖动。此处引入磁盘状态（cache/market_regime_state.json，
    超 10 天未更新自动重置）：
    - 无历史状态时直接采用当期 raw regime（启动期不加延迟）；
    - raw 与已确认状态不同 → 记为待确认，连续确认达天数才翻转；
    - raw=unknown（指数数据缺失）→ 沿用已确认状态，不污染计数；
      无已确认状态时保持 unknown（由 _effective_regime 保守视同 bear）；
    - **每个自然日最多推进一次确认计数**：状态文件 updated 已是今天就只读取、
      不再累加，确保「连续 N 个交易日」不因一天内多次运行（如一个 workflow 里
      顺序跑优质低估低位 + 放量突破两套策略）而退化为「同日确认」。
    """
    if not getattr(config, "MARKET_REGIME_HYSTERESIS", True):
        return result
    raw = str(result.get("regime", "unknown")).lower()
    path = _cache_path(config, "market_regime_state.json")
    state = _read_cache_json(path) or {}
    today = datetime.now().strftime(_DATE_FMT)
    updated = state.get("updated")
    if updated:
        try:
            if (datetime.now() - datetime.strptime(str(updated), _DATE_FMT)).days > 10:
                state = {}
        except (TypeError, ValueError):
            state = {}
    confirmed = state.get("confirmed")
    pending, count = state.get("pending"), int(state.get("count", 0) or 0)
    # 每自然日最多推进一次确认计数：同一交易日内重复运行（如一个 workflow 内顺序跑
    # 优质低估低位 + 放量突破两套策略，共用同一个 cache/ 目录）读到的是同一份收盘数据，raw 必然
    # 完全相同；若允许重复计数，「连续 N 个交易日确认」会被悄悄缩短为「同日确认」，
    # 恰好抵消滞回机制防抖动的目的。判定依据是状态文件自身的 updated 日期，
    # 不依赖调用方传参，因此对「一天跑几次」完全鲁棒。
    advanced_today = bool(updated) and str(updated) == today

    if raw == "unknown":
        eff = confirmed or "unknown"
    elif confirmed is None or raw == confirmed:
        confirmed, pending, count = raw, None, 0
        eff = raw
    elif advanced_today:
        # 今天已经推进过：沿用已确认状态，既不重复累加计数、也不改写候选，
        # 使同日多次运行得到完全一致的 regime
        eff = confirmed
    elif raw == pending:
        count += 1
        if count >= int(getattr(config, "MARKET_REGIME_CONFIRM_DAYS", 2)):
            confirmed, pending, count = raw, None, 0
        eff = confirmed
    else:
        pending, count = raw, 1
        eff = confirmed

    if raw != "unknown":
        _write_cache_json({"confirmed": confirmed, "pending": pending, "count": count,
                           "updated": today}, path)
    if eff != raw:
        result = dict(result)
        result["regime_raw"] = raw
        result["regime"] = eff
        result["description"] = f"{result.get('description', '')}（滞回：原始 {raw}，采用 {eff}）"
    return result

def _is_financial_stock(code: str, name: str, config: StrategyConfig) -> bool:
    """金融业（银行/保险/券商/信托/期货）识别：名称关键词 + 代码白名单（覆盖无关键词的知名金融股）"""
    if code and code in config.FINANCE_EXEMPT_CODES: return True
    return any(k in (name or "") for k in config.FINANCE_NAME_KEYWORDS)

def check_fundamentals(fund_data: Optional[dict], config: StrategyConfig, code: str = "", name: str = "") -> bool:
    if fund_data is None: return True
    if (roe := fund_data.get("roe")) is not None and roe < config.MIN_ROE: return False
    # 金融业负债率天然 80%+（如银行约 90%），使用放宽阈值避免全行业误杀；极端值仍否决
    debt_limit = config.FINANCE_MAX_DEBT_RATIO if _is_financial_stock(code, name, config) else config.MAX_DEBT_RATIO
    if (debt := fund_data.get("debt_ratio")) is not None and debt > debt_limit: return False
    # 商誉/扣非为主源不提供的可选否决项：Baostock 缺失时为 None 自动放行，
    # 决赛圈会用 AkShare 按字段补齐后复核（见 _fill_optional_fundamentals）
    if (goodwill := fund_data.get("goodwill_ratio")) is not None and goodwill > config.MAX_GOODWILL_RATIO: return False
    if (deducted := fund_data.get("deducted_profit_ratio")) is not None and deducted < config.MIN_DEDUCTED_PROFIT_RATIO: return False
    return True

def _fund_verify_state(fund_data: Optional[dict]) -> tuple[str, list[str]]:
    """财务核验状态分层：
    - verified：核心项（ROE+负债率）齐备，基本面防雷实际生效
    - partial ：核心项缺其一，防雷只部分生效 → 待核验候选
    - missing ：无财务数据，防雷整层未生效 → 待核验候选
    商誉/扣非为可选补充指标，缺失只记标签、不降级（决赛圈 AkShare 按字段补齐）。"""
    if fund_data is None:
        return "missing", ["fund"]
    tags: list[str] = []
    core_ok = True
    if _num_or_none(fund_data.get("roe")) is None: core_ok = False; tags.append("fund_roe")
    if _num_or_none(fund_data.get("debt_ratio")) is None: core_ok = False; tags.append("fund_debt")
    if fund_data.get("goodwill_ratio") is None: tags.append("fund_goodwill")
    if fund_data.get("deducted_profit_ratio") is None: tags.append("fund_deducted")
    return ("verified" if core_ok else "partial"), tags

def _fill_optional_fundamentals(code: str, fund_data: dict, config: StrategyConfig) -> dict:
    """按字段补齐财务数据（仅决赛圈调用，控制 AkShare 成本）：
    主源 Baostock 只提供 ROE/负债率，商誉/扣非（及主源缺失的核心项）用 AkShare 补齐，
    只填 None 字段、不覆盖主源已有值——补齐后「四项基本面防雷」才算完整执行。"""
    if not _AK_AVAILABLE:
        return fund_data
    ak_data = _fetch_fundamentals_ak(code, config)
    if ak_data:
        for key in ("roe", "debt_ratio", "goodwill_ratio", "deducted_profit_ratio"):
            if fund_data.get(key) is None and ak_data.get(key) is not None:
                fund_data[key] = ak_data[key]
    return fund_data

_DAILY_NEED_COLS = {"close", "volume", "amount", "date", "high", "low", "pct_chg"}

def _divergence_confirmed(out: pd.DataFrame, config: StrategyConfig) -> pd.Series:
    """双低点底背离（修正版：指标比较锚定在「前一个价格低点」当根的指标值上，
    而非指标自身的窗口最小值——指标最低点未必出现在价格低点，旧口径会误报背离）。

    对每根 bar t 的判定：
    1. t 收盘创近 DIVERGENCE_LOOKBACK 根新低（价格低点降低的当根体现）；
    2. 在窗口 [t-L, t-sep-1] 内找收盘价最低的 bar 作为前一个价格低点（与 t 至少
       间隔 DIVERGENCE_MIN_SEPARATION 根，保证是两个独立低点而非同段下跌的连续新低）；
    3. 价格更低 + RSI（超 DAILY_RSI_DIVERGENCE_THRESHOLD）或 DIF（超
       DAILY_MACD_DIVERGENCE_THRESHOLD）在前低点处显著更低 → 背离成立；
       任一侧指标缺失（NaN）不认定（标签宁缺毋滥）；
    4. 背离须出现在近 3 根内，且当根出现右侧企稳确认（MACD金叉/RSI超卖反弹/MA5拐头），
       才输出「已确认底背离」。
    """
    L = int(config.DIVERGENCE_LOOKBACK)
    sep = max(1, int(config.DIVERGENCE_MIN_SEPARATION))
    if L <= sep:
        return pd.Series(False, index=out.index)
    close = out["close"].to_numpy(dtype=float)
    rsi = out["rsi14"].to_numpy(dtype=float)
    macd = out["macd_diff"].to_numpy(dtype=float)
    n = len(out)
    state = np.zeros(n, dtype=bool)
    for t in range(L, n):
        c = close[t]
        if np.isnan(c):
            continue
        window = close[t - L:t]                     # t 之前的 L 根（不含 t）
        if np.isnan(window).all():
            continue
        if c >= np.nanmin(window):                  # 未创窗口新低：不是新低点
            continue
        seg = close[t - L:t - sep]                   # 排除近 sep 根，保证两个低点有间隔
        if seg.size == 0 or np.isnan(seg).all():
            continue
        prev = t - L + int(np.nanargmin(seg))       # 前一个价格低点位置
        pv = close[prev]
        if np.isnan(pv) or c >= pv:
            continue
        rsi_div = (not np.isnan(rsi[t])) and (not np.isnan(rsi[prev])) and (
            rsi[t] > rsi[prev] + config.DAILY_RSI_DIVERGENCE_THRESHOLD)
        macd_div = (not np.isnan(macd[t])) and (not np.isnan(macd[prev])) and (
            macd[t] > macd[prev] + config.DAILY_MACD_DIVERGENCE_THRESHOLD)
        state[t] = rsi_div or macd_div
    recent_div = pd.Series(state, index=out.index).astype(float).rolling(3).max() > 0
    right_confirm = out["macd_golden_cross"] | out["rsi_rebound"] | out["ma5_turn"]
    return recent_div & right_confirm

def _vol_price_quality(out: pd.DataFrame, config: StrategyConfig) -> tuple[pd.Series, pd.Series]:
    """量价质量分：在「上涨 + 适度放量」基础条件之上，按 收盘位置/实体方向/上影线占比 分档。

    - 满分 W_DAILY_VOL_PRICE（放量企稳）：阳线、收盘位置 ≥ VOLP_CLOSE_POS_FULL、上影线 ≤ VOLP_MAX_UPPER_SHADOW
    - 中档 W_DAILY_VOL_PRICE_MID（放量上涨）：基础条件满足、收盘位置一般
    - 降档 W_DAILY_VOL_PRICE_WEAK（放量冲高回落）：收盘位置 < VOLP_CLOSE_POS_MIN，承接弱，
      只降分不否决——高开冲高回落的长上影线与放量阳线收高位不应同分

    返回 (质量分, 标签)。一字板（high==low）收盘位置无法定义，按中档处理。
    """
    rng = (out["high"] - out["low"]).replace(0, np.nan)
    close_pos = ((out["close"] - out["low"]) / rng).clip(0, 1)
    upper_shadow = (out["high"] - out[["open", "close"]].max(axis=1)) / rng
    is_bull = out["close"] >= out["open"]
    strong = (close_pos >= config.VOLP_CLOSE_POS_FULL) & is_bull & (upper_shadow <= config.VOLP_MAX_UPPER_SHADOW)
    weak = close_pos < config.VOLP_CLOSE_POS_MIN
    coord = out["vol_price_coord"]
    quality = pd.Series(
        np.where(~coord, 0.0,
                 np.where(strong, config.W_DAILY_VOL_PRICE,
                          np.where(weak, config.W_DAILY_VOL_PRICE_WEAK, config.W_DAILY_VOL_PRICE_MID))),
        index=out.index,
    )
    label = pd.Series(
        np.select([~coord, strong, weak], ["", "放量企稳", "放量冲高回落"], default="放量上涨"),
        index=out.index,
    )
    return quality.fillna(0.0), label

def compute_daily_signals(df: pd.DataFrame, config: StrategyConfig) -> Optional[pd.DataFrame]:
    if df is None or df.empty or not _DAILY_NEED_COLS.issubset(df.columns) or len(df) < config.MIN_DAYS: return None
    # 注：流动性否决（近20日日均成交额 < MIN_AMOUNT → FAIL_LIQUIDITY）已上移到
    # evaluate() 前置层，与数据缺失（FAIL_DATA）分开归因，此处不再检查。
    out = df.copy().reset_index(drop=True)
    out["ma5"] = out["close"].rolling(config.DAILY_MA5).mean()
    out["ma10"] = out["close"].rolling(config.DAILY_MA10).mean()
    out["ema5"] = out["close"].ewm(span=config.DAILY_EMA5, adjust=False).mean()
    out["ema10"] = out["close"].ewm(span=config.DAILY_EMA10, adjust=False).mean()
    out["ema20"] = out["close"].ewm(span=config.DAILY_EMA20, adjust=False).mean()

    for p, col in [(config.DAILY_RSI_PERIOD, "rsi14"), (config.DAILY_RSI_SHORT_PERIOD, "rsi7"), (config.DAILY_RSI_LONG_PERIOD, "rsi21")]:
        delta = out["close"].diff()
        gain, loss = delta.clip(lower=0), (-delta).clip(lower=0)
        rs = gain.ewm(com=p-1, min_periods=p).mean() / loss.ewm(com=p-1, min_periods=p).mean().replace(0, np.nan)
        out[col] = 100 - (100 / (1 + rs))

    exp1 = out["close"].ewm(span=config.DAILY_MACD_FAST, adjust=False).mean()
    exp2 = out["close"].ewm(span=config.DAILY_MACD_SLOW, adjust=False).mean()
    out["macd_diff"] = exp1 - exp2
    out["macd_dea"] = out["macd_diff"].ewm(span=config.DAILY_MACD_SIGNAL, adjust=False).mean()
    out["macd_histogram"] = out["macd_diff"] - out["macd_dea"]

    # KDJ(9,3,3)：SMA(X,3,1) 等价于 ewm(com=2)
    _low9 = out["low"].rolling(9).min()
    _high9 = out["high"].rolling(9).max()
    _rsv = (out["close"] - _low9) / (_high9 - _low9).replace(0, np.nan) * 100
    out["kdj_k"] = _rsv.ewm(com=2, adjust=False).mean()
    out["kdj_d"] = out["kdj_k"].ewm(com=2, adjust=False).mean()

    if {"high", "low"}.issubset(out.columns):
        prev_close = out["close"].shift(1)
        tr = pd.concat([out["high"] - out["low"], (out["high"] - prev_close).abs(), (out["low"] - prev_close).abs()], axis=1).max(axis=1)
        out["atr"] = tr.ewm(com=config.ATR_PERIOD - 1, min_periods=config.ATR_PERIOD, adjust=False).mean()
    else: out["atr"] = np.nan

    out["daily_vol_base"] = out["volume"].shift(1).rolling(20).mean()
    out["daily_vol_ratio"] = out["volume"] / out["daily_vol_base"].replace(0, np.nan)
    # 换手比仅用于展示，不参与过滤（与量比同源：流通股短期不变，二者比值数学上几乎相等）
    if "turnover" in out.columns:
        out["daily_to_base"] = out["turnover"].shift(1).rolling(config.DAILY_TURNOVER_LOOKBACK).mean()
        out["daily_turnover_ratio"] = out["turnover"] / out["daily_to_base"].replace(0, np.nan)
    else:
        out["daily_turnover_ratio"] = np.nan

    ma5_slope = out["ma5"] - out["ma5"].shift(1)
    out["ma5_turn"] = (ma5_slope > 0) & (ma5_slope.shift(1) <= 0)
    out["ema_golden_cross"] = (out["ema5"] > out["ema10"]) & (out["ema5"].shift(1) <= out["ema10"].shift(1))
    # 下降通道过滤：MA20 近 N 日斜率低于阈值判定为陡峭下降，此时 MA5 拐头/EMA 金叉多为
    # 下跌中继的假拐点（接飞刀），趋势转折信号不认可；底背离的右侧确认不受此限（抄底本身发生在下降中）
    out["ma20"] = out["close"].rolling(config.DAILY_MA20).mean()
    _ma20_prev = out["ma20"].shift(config.MA20_TREND_LOOKBACK)
    out["ma20_slope"] = (out["ma20"] - _ma20_prev) / _ma20_prev.replace(0, np.nan)
    ma20_trend_ok = (out["ma20_slope"] >= config.MA20_TREND_MIN_SLOPE).fillna(False)
    # MA5拐头与EMA金叉在右侧拐点经常同日触发（同一信息），合并为一个趋势转折信号
    out["trend_turn"] = (out["ma5_turn"] | out["ema_golden_cross"]) & ma20_trend_ok
    out["macd_golden_cross"] = (out["macd_diff"] > out["macd_dea"]) & (out["macd_diff"].shift(1) <= out["macd_dea"].shift(1))
    rsi_was_oversold = (out["rsi14"].shift(1) < config.DAILY_RSI_OVERSOLD) | (out["rsi14"].shift(2) < config.DAILY_RSI_OVERSOLD)
    out["rsi_rebound"] = rsi_was_oversold & (out["rsi14"] >= config.DAILY_RSI_REBOUND_MIN)
    out["rsi_multi_res"] = (out["rsi7"] > out["rsi14"]) & (out["rsi14"] > out["rsi21"]) & (out["rsi7"] < config.DAILY_RSI_OVERBOUGHT) & (out["rsi21"] > config.DAILY_RSI_OVERSOLD)

    out["bottom_divergence"] = _divergence_confirmed(out, config)

    price_up = out["close"] > out["close"].shift(1)
    vol_expand = out["daily_vol_ratio"] >= config.DAILY_VOL_EXPAND
    out["vol_price_coord"] = price_up & vol_expand
    out["vol_price_quality"], out["vol_price_label"] = _vol_price_quality(out, config)
    out["multi_resonance"] = out["rsi_multi_res"] & out["macd_golden_cross"] & out["vol_price_coord"]

    out["daily_score"] = (
        out["trend_turn"].astype(float) * config.W_DAILY_TREND_TURN +
        out["rsi_rebound"].astype(float) * config.W_DAILY_RSI_REBOUND +
        out["vol_price_quality"].astype(float) +
        out["multi_resonance"].astype(float) * config.DAILY_MULTI_RESONANCE_BONUS
    ).fillna(0).clip(0, 100).round(1)

    return out

def compute_risk_reward(entry_price: float, config: StrategyConfig, atr: Optional[float] = None) -> dict:
    """止损/止盈/盈亏比计算（仅展示与落库，不参与否决——波动率风控见 MAX_ATR_PCT）。"""
    if config.USE_ATR_STOP and atr is not None and atr > 0: stop_loss = entry_price - config.ATR_STOP_MULT * atr
    else: stop_loss = entry_price * (1 - config.FIXED_STOP_LOSS_PCT / 100)
    take_profit = entry_price * (1 + config.FIXED_TAKE_PROFIT_PCT / 100)
    risk, reward = entry_price - stop_loss, take_profit - entry_price
    rr_ratio = reward / risk if risk > 0 else 0.0
    return {"stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2), "rr_ratio": round(rr_ratio, 2)}

def _fmt_cell(v: Any, default: str = "-") -> Any:
    """展示用取值：None/NaN 统一回退默认值，避免卡片出现 None 或 nan。"""
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except (TypeError, ValueError):
        pass
    return v


def describe_trade_plan(row: dict, config: Optional[StrategyConfig] = None) -> list[str]:
    """渲染「可执行交易计划」：建仓区间 / 止损 / 止盈 / 建议持有周期。

    定位与 stop_loss/take_profit 一致——**纯展示，不参与准入、排序与否决**。两套策略
    共用本函数，保证同一只票无论在哪个入口的卡片里，看到的操作口径完全一致。

    价位来源：
      - 建仓区间：信号日收盘价 × (1 ± ENTRY_BAND_PCT%)，次日开盘执行（见 config 注释）；
      - 止损/止盈：直接取 row，口径由各自策略决定——优质低估低位=2×ATR 或固定 -5%
        搭配固定 +10%；放量突破=突破位−1×ATR 与固定 -6% 取更紧者、固定 +15% 与
        ATR 目标取更近者；
      - 建议持有周期：HOLD_DAYS_HINT_MIN ~ MAX，与周度追踪窗口（推荐后第 5/10/15/20
        个交易日）对齐，第 20 个交易日是到期离场判定日。

    任一必需字段缺失（旧数据/异常输入）时整段不渲染，对既有调用与测试向后兼容。
    """
    close = _num_or_none(row.get("close"))
    if close is None or close <= 0:
        return []
    cfg = config or StrategyConfig()
    band = max(0.0, float(getattr(cfg, "ENTRY_BAND_PCT", 1.5) or 0.0)) / 100.0
    if band > 0:
        lo, hi = close * (1 - band), close * (1 + band)
        entry = f"建仓 {lo:.2f} ~ {hi:.2f}｜高于上沿不追，低于下沿按失效重估"
    else:
        entry = f"建仓 {close:.2f}（信号日收盘价）"
    lines = ["**操作计划**（次日开盘按区间建仓）", entry]
    price_bits = []
    stop = _num_or_none(row.get("stop_loss"))
    take = _num_or_none(row.get("take_profit"))
    if stop is not None and stop > 0:
        price_bits.append(f"止损 {stop:.2f}（{(stop / close - 1) * 100:+.1f}%）")
    if take is not None and take > 0:
        price_bits.append(f"止盈 {take:.2f}（{(take / close - 1) * 100:+.1f}%）")
    rr = _num_or_none(row.get("rr_ratio"))
    if rr is not None:
        price_bits.append(f"盈亏比 {rr:.2f}")
    if price_bits:
        lines.append(" | ".join(price_bits))
    hold_lo = int(getattr(cfg, "HOLD_DAYS_HINT_MIN", 10) or 0)
    hold_hi = int(getattr(cfg, "HOLD_DAYS_HINT_MAX", 20) or 0)
    if hold_lo > 0 and hold_hi >= hold_lo:
        lines.append(f"建议持有 {hold_lo}~{hold_hi} 个交易日"
                     f"（第 {hold_hi} 个交易日仍未触及止损/止盈，按收盘价平仓离场）")
    return lines


_FUND_STATUS_ZH = {"verified": "财务已核验", "partial": "财务部分核验", "missing": "财务未核验"}
_WEEKLY_STATUS_ZH = {"confirmed": "周线已确认", "unverified": "周线待核验", "disabled": "周线未启用", "not_required": "技术仅供排序"}
_MISSING_TAG_ZH = {
    "fund": "财务数据缺失", "fund_roe": "ROE缺失", "fund_debt": "负债率缺失",
    "fund_goodwill": "商誉缺失", "fund_deducted": "扣非缺失",
    "valuation_pe": "PE未核验", "valuation_pb": "PB未核验",
    "fund_annual": "多年财务未核验", "market_date": "行情基准日期缺失",
    "financial_review": "金融企业待专项核验",
    "valuation": "估值缺失", "pct_chg": "涨幅数据缺失", "gap": "开盘价缺失",
    "macd_mom": "MACD柱数据缺失", "kdj": "KDJ数据缺失",
    "forward": "当年成长未核验", "valuation_industry": "行业估值样本不足",
}

def _missing_tags_zh(tags: str) -> str:
    if not tags:
        return ""
    return "、".join(_MISSING_TAG_ZH.get(t, t) for t in str(tags).split(",") if t)

def _brief_lines(row: dict) -> list[str]:
    """P5 决策简报渲染：把 bull/bear/invalidation/conviction/why_today 拼成卡片行。

    仅当对应字段非空时才输出该行——technical 模式与旧数据没有这些字段时整块不渲染，
    保证 describe 输出对既有测试与旧调用完全向后兼容。
    """
    lines: list[str] = []
    if row.get("conviction"):
        lines.append(f"**信心:** {row.get('conviction')}")
    if row.get("why_today"):
        lines.append(f"**今日触发:** {row.get('why_today')}")
    if row.get("bull_case"):
        lines.append(f"**看多:** {row.get('bull_case')}")
    if row.get("bear_case"):
        lines.append(f"**风险:** {row.get('bear_case')}")
    if row.get("invalidation"):
        lines.append(f"**失效:** {row.get('invalidation')}")
    return lines

def describe(row: dict) -> str:
    """把一条推荐格式化为飞书卡片文本（notify/feishu.py 调用），按四维展示：

    1. 基础评分/等级：分数与等级同源（均按原始技术分定级，熊市只抬门槛不扣展示分）；
    2. 入选依据：实际触发的技术条件（放量企稳/放量上涨/放量冲高回落措辞区分）；
    3. 操作计划：建仓区间 / 止损 / 止盈 / 建议持有周期（见 describe_trade_plan）；
    4. 核验状态：财务/周线是否已核验、缺项明细（可选指标缺失只标注不降级）。
    quality_value 模式额外附 P5 决策简报（信心/今日触发/看多/风险/失效价）。

    标题里的「正式推荐」即 tier=formal——飞书卡片在渲染前已过滤掉全部非 formal 标的，
    故卡片上每一只都已落库并被周度追踪统计战绩。此处不再使用「候选」措辞：该词在中文里
    天然偏「备选/仅供参考」，与 formal 的真实含义相反，是明确的误导来源。
    """
    if row.get("weekly_status") == "not_required":
        lines = [
            f"**{row.get('name', '')} {row.get('code', '')}** · 正式推荐 · 优质低估低位",
            f"综合分: {_fmt_cell(row.get('score'))} | 质量分: {_fmt_cell(row.get('quality_score'))}"
            f" | 估值分: {_fmt_cell(row.get('valuation_score'))} | 技术分: {_fmt_cell(row.get('daily_score'))}",
            f"收盘: {_fmt_cell(row.get('close'))} | PE: {_fmt_cell(row.get('pe_ttm'))}"
            f" | PB: {_fmt_cell(row.get('pb_mrq'))} | 250日区间位置: {float(row.get('position_250', 0)):.1%}",
            f"入选依据: {row.get('signals_hit', '')}",
        ]
        lines.extend(describe_trade_plan(row))
        lines.append(
            f"核验: {_FUND_STATUS_ZH.get(str(row.get('fund_status')), '待核验')}"
            + (f" | 缺项: {_missing_tags_zh(str(row.get('missing_tags')))}" if row.get('missing_tags') else "")
        )
        lines.extend(_brief_lines(row))
        return "\n".join(lines)
    verify_bits = [
        _FUND_STATUS_ZH.get(str(row.get("fund_status", "")), "财务未核验"),
        _WEEKLY_STATUS_ZH.get(str(row.get("weekly_status", "")), "周线待核验"),
    ]
    missing = _missing_tags_zh(str(row.get("missing_tags", "") or ""))
    verify = " | ".join(verify_bits) + (f" | 缺项: {missing}" if missing else "")
    lines = [
        f"**{row.get('name', '')} {row.get('code', '')}** · 正式推荐 · 低位企稳",
        f"评分: {_fmt_cell(row.get('score'))} ({row.get('grade') or '-'}级)"
        f" | 收盘: {_fmt_cell(row.get('close'))}",
    ]
    if row.get("signals_hit"):
        lines.append(f"入选依据: {row.get('signals_hit')}")
    lines.extend(describe_trade_plan(row))
    lines.append(f"核验: {verify}")
    lines.append(
        f"RSI14: {_fmt_cell(row.get('rsi'))}"
        f" | 量比: {_fmt_cell(row.get('vol_ratio'))}"
        f" | 市场: {row.get('market_env') or '-'}"
        + (" | 底背离" if row.get("has_divergence") else "")
    )
    lines.extend(_brief_lines(row))
    return "\n".join(lines)

def describe_pending(row: dict) -> str:
    """把一条待核验候选格式化为飞书卡片文本：说明缺什么、为什么没进正式推荐。"""
    missing = _missing_tags_zh(str(row.get("missing_tags", "") or ""))
    reasons = [
        _FUND_STATUS_ZH.get(str(row.get("fund_status", "")), "财务未核验"),
        _WEEKLY_STATUS_ZH.get(str(row.get("weekly_status", "")), "周线待核验"),
    ]
    parts = [
        f"**{row.get('name', '')} {row.get('code', '')}**"
        f" | 评分: {_fmt_cell(row.get('score'))} ({row.get('grade') or '-'}级)",
        f"待核验: {'、'.join(reasons)}",
    ]
    if missing:
        parts.append(f"缺项: {missing}")
    return "\n".join(parts)


# ===========================================================================
# 波动率风控否决（FAIL_VOLATILE）留档与展示
# 这类标的已通过前置筛选层，唯一拦路条件是 ATR 占现价百分比超出风控上限——
# 既不是数据缺失，也不是形态不合格。单独留档的意义：
#   1) 让「今天为什么没有推荐」在日志里可逐只复核，而不是只看到一个计数；
#   2) 作为「高风险观察池」推送飞书（ATR% 收敛后可能重新达标），并显式标注风险，
#      避免被误读为推荐标的（不落库、不参与周度追踪与归因）。
# ===========================================================================

VOLATILE_LOG_LIMIT = 20   # 日志逐只打印上限，超出仅提示条数（飞书另设上限）


def _num_or_none(v: Any) -> Optional[float]:
    """转为 float，None/NaN/不可转换一律返回 None（展示层用 _fmt_cell 兜底）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def _atr_risk_level(atr_pct: float, limit: float) -> str:
    """按 ATR% 超出上限的倍数划风险档（纯展示标签，不参与任何筛选判定）。"""
    lim = _num_or_none(limit)
    val = _num_or_none(atr_pct)
    if not lim or lim <= 0 or val is None:
        return "未知"
    ratio = val / lim
    if ratio >= 2.0:
        return "极高"
    if ratio >= 1.5:
        return "高"
    if ratio >= 1.2:
        return "偏高"
    return "轻度超限"


def _build_volatile_row(code: str, name: str, date: Any, close: float, atr: float,
                        atr_pct: float, limit: float, score: float, grade: str,
                        hits: str = "", rsi: Any = None, vol_ratio: Any = None,
                        drawdown: Any = None) -> dict:
    """构造一条「波动率风控否决」记录（日志与飞书共用同一结构，两策略共用）。"""
    ratio = (float(atr_pct) / float(limit)) if limit and limit > 0 else 0.0
    # compute_risk_reward 用 1.5×ATR 作止损距离：ATR% 越大，确认止损失败所需的
    # 浮亏越深，日内噪声扫损概率越高——这是该档标的最实质的风险来源。
    stop_dist = float(atr_pct) * 1.5
    return {
        "code": str(code), "name": str(name), "date": str(date),
        "close": round(float(close), 2),
        "score": round(float(score), 1), "grade": str(grade or "-"),
        "atr": round(float(atr), 3),
        "atr_pct": round(float(atr_pct), 2),
        "atr_limit": round(float(limit), 2),
        "atr_ratio": round(ratio, 2),
        "risk_level": _atr_risk_level(atr_pct, limit),
        "risk_note": (
            f"ATR 已达现价 {float(atr_pct):.2f}%（上限 {float(limit):.2f}%，{ratio:.2f}×）："
            f"按 1.5×ATR 设止损需先承受约 {stop_dist:.1f}% 的浮亏才确认失败，日内噪声即可能扫损；"
            f"高 ATR 多源于近期暴涨暴跌，左侧介入风险显著放大"
        ),
        "rsi": _num_or_none(rsi),
        "vol_ratio": _num_or_none(vol_ratio),
        "drawdown_pct": None if _num_or_none(drawdown) is None else round(float(drawdown) * 100, 2),
        "signals_hit": str(hits or ""),
    }


def sort_volatile(rows: list[dict]) -> list[dict]:
    """波动率观察池排序：技术分降序（质量优先）→ ATR% 降序 → 代码。

    仅决定展示顺序，不影响任何筛选结果（这些标的都已经是被否决的）。
    """
    return sorted(rows, key=lambda r: (-float(_num_or_none(r.get("score")) or 0.0),
                                       -float(_num_or_none(r.get("atr_pct")) or 0.0),
                                       str(r.get("code") or "")))


def describe_volatile(row: dict) -> str:
    """把一只「波动率风控否决」的个股格式化为飞书卡片文本（高风险观察池条目）。"""
    lines = [
        f"**{row.get('name', '')} {row.get('code', '')}**（高风险观察 · **{row.get('risk_level') or '-'}**）",
        f"评分: {_fmt_cell(row.get('score'))} ({row.get('grade') or '-'}级)"
        f" | 收盘: {_fmt_cell(row.get('close'))}"
        f" | RSI14: {_fmt_cell(row.get('rsi'))}"
        f" | 量比: {_fmt_cell(row.get('vol_ratio'))}",
        f"波动率: ATR {_fmt_cell(row.get('atr_pct'))}%"
        f"（上限 {_fmt_cell(row.get('atr_limit'))}%，{_fmt_cell(row.get('atr_ratio'))}×）"
        f" | 距高点回撤: {_fmt_cell(row.get('drawdown_pct'))}%",
    ]
    if row.get("signals_hit"):
        lines.append(f"已达标项: {row.get('signals_hit')}")
    if row.get("risk_note"):
        lines.append(f"⚠️ 风险: {row.get('risk_note')}")
    return "\n".join(lines)


def log_volatile_rejects(rows: list[dict], log: logging.Logger, top: int = VOLATILE_LOG_LIMIT) -> None:
    """逐只打印波动率风控否决明细（按技术分降序）。两策略共用同一打印格式。"""
    ordered = sort_volatile(rows)
    log.info("-" * 60)
    log.info("[VOLATILE] 波动率风控否决 %d 只（已过前置筛选层，仅 ATR 超限；不推荐，仅供观察）", len(ordered))
    for r in ordered[:max(1, int(top))]:
        log.info("  · %s(%s) 评分 %s %s级 收盘 %s | ATR %s%%（上限 %s%%，%s×，风险%s）"
                 " | RSI %s 量比 %s | 已达标: %s",
                 r.get("name"), r.get("code"), r.get("score"), r.get("grade"), r.get("close"),
                 r.get("atr_pct"), r.get("atr_limit"), r.get("atr_ratio"), r.get("risk_level"),
                 _fmt_cell(r.get("rsi")), _fmt_cell(r.get("vol_ratio")), r.get("signals_hit") or "-")
    if len(ordered) > max(1, int(top)):
        log.info("  ...其余 %d 只详见飞书卡片高风险观察池", len(ordered) - max(1, int(top)))
    log.info("  说明：ATR%% 收敛至上限以内后，这些标的本可进入正式候选，可作高风险观察池跟踪")
    log.info("-" * 60)


@dataclass
class Signal:
    code: str; name: str; date: str; close: float; score: float; grade: str
    daily_score: float; rsi: float; rsi7: float; rsi21: float; vol_ratio: float
    turnover_ratio: float; stop_loss: float; take_profit: float; rr_ratio: float
    market_env: str; has_divergence: bool
    # ===== 荐股质量三维（TOP5 改进）=====
    signals_hit: str = ""             # 形态标签：实际触发的技术条件（逗号分隔）
    fund_status: str = "missing"      # 核验状态：财务 verified/partial/missing
    weekly_status: str = "unverified" # 核验状态：周线 confirmed/unverified/disabled（决赛圈更新）
    missing_tags: str = ""            # 缺项标签（逗号分隔；可选指标缺失只标注不降级）
    tier: str = "pending"             # 推荐层级：formal=正式推荐 / pending=待核验候选
    # 排序分 = score + 连续质量分（_rank_quality_score）。仅作 _rank_signals 主键与展示，
    # 不参与准入判定，因此不改变「哪些股票通过」，只改变通过者之间的先后顺序。
    # 默认 0.0 以保持 BreakoutSignal（放量突破策略自带独立 SORT_BY）构造接口兼容。
    rank_score: float = 0.0
    quality_score: float = 0.0
    valuation_score: float = 0.0
    position_250: float = 0.0
    pe_ttm: Optional[float] = None
    pb_mrq: Optional[float] = None
    quality_status: str = "missing"
    # ===== P5：决策简报（纯展示，供人工裁量；不落库、不参与排序/否决/追踪）=====
    # 交易按实际情况人工判断，故把「看多理由 / 主要风险 / 失效价 / 信心分档 / 今日为何触发」
    # 结构化成简报，让推荐从「一个代码」升级为「一份可复核的研究摘要」。
    bull_case: str = ""       # 看多理由：决定性正面因子（分号分隔）
    bear_case: str = ""       # 主要风险：为什么可能是陷阱/接飞刀（分号分隔）
    invalidation: str = ""    # 失效价：跌破即认错离场的价位与逻辑
    conviction: str = ""      # 信心分档：高/中高/中/中低/低（观察）
    why_today: str = ""       # 今日为何触发：技术变化点，或「无明显触发·左侧布局」

    def to_dict(self) -> dict: return asdict(self)

def _grade_from_score(score: float, config: StrategyConfig) -> str:
    if score >= config.GRADE_A: return "A"
    elif score >= config.GRADE_B: return "B"
    elif score >= config.GRADE_C: return "C"
    return "D"

_GRADE_ORDER = ("D", "C", "B", "A")

def _rank_quality_score(daily_out: pd.DataFrame, config: StrategyConfig) -> float:
    """连续排序质量分（0 ~ RANK_QUALITY_WEIGHT）。

    只参与 _rank_signals 的先后顺序，不参与任何否决/准入判定——「哪些股票通过筛选」
    与引入前完全一致，改变的只是通过者之间谁排在前面（原实现在大量同分时退化为
    按 rr_ratio ≈ 0.05/ATR% 乃至 code 字母序选取 Top5）。

    四个维度全部取自 compute_daily_signals 已经算出的列，零额外取数与计算成本：
      1) 低波动  ：ATR% 在 [0, MAX_ATR_PCT] 内越低越好
      2) 回撤深度：在闸门允许的 [MIN_DRAWDOWN_FROM_HIGH, RQ_DRAWDOWN_SATURATION] 内越深越好
      3) 区间位置：现价在 20 日区间内越低越好（POSITION_IN_RANGE_MAX 是闸门，这里做闸门内细分）
      4) 动能速率：MACD 柱近 3 日改善幅度（默认权重 0——REQUIRE_MACD_MOMENTUM 已是硬闸门，
         通过者在该维度恒为满分，实测无区分度；关闭 MACD 闸门做 A/B 时可重新启用）
    任何异常/缺失一律回退 0 或中性 0.5，绝不因质量分计算失败而影响主流程。
    """
    w = float(getattr(config, "RANK_QUALITY_WEIGHT", 0.0) or 0.0)
    if w <= 0:
        return 0.0
    try:
        d = daily_out.iloc[-1]
        close = float(d["close"])
        if not close > 0:
            return 0.0

        # 1) 低波动优先
        atr_cap = float(config.MAX_ATR_PCT)
        atr = d.get("atr")
        atr_pct = float(atr) / close * 100 if atr is not None and not pd.isna(atr) else atr_cap
        q_vol = 1.0 - min(max(atr_pct / atr_cap, 0.0), 1.0) if atr_cap > 0 else 0.0

        # 2) 回撤深度优先（均值回归的空间来自跌幅）
        hi = float(daily_out["high"].tail(config.DRAWDOWN_LOOKBACK).max())
        dd = (hi - close) / hi if hi > 0 else 0.0
        lo_dd = float(config.MIN_DRAWDOWN_FROM_HIGH)
        sat_dd = float(getattr(config, "RQ_DRAWDOWN_SATURATION", 0.45))
        q_dd = min(max((dd - lo_dd) / (sat_dd - lo_dd), 0.0), 1.0) if sat_dd > lo_dd else 0.0

        # 3) 区间位置越低越好
        win = daily_out.tail(config.RANGE_LOOKBACK)
        wlo, whi = float(win["low"].min()), float(win["high"].max())
        pos = (close - wlo) / (whi - wlo) if whi > wlo else 0.5
        cap = float(config.POSITION_IN_RANGE_MAX)
        q_pos = 1.0 - min(max(pos / cap, 0.0), 1.0) if cap > 0 else 0.0

        # 4) MACD 柱改善速率：(h[-1]-h[-3]) / (|h[-3]|+|h[-1]|) ∈ [-1,1] → 映射到 [0,1]
        #    无量纲，不受股价与柱值绝对量级影响；数据缺失给中性 0.5（不奖不罚）
        #    权重为 0 时整段跳过（本函数在全市场逐股调用，属热点路径）
        w_mom = float(getattr(config, "RQ_W_MOMENTUM", 0.0))
        q_mom = 0.5
        if w_mom > 0:
            hist = daily_out["macd_histogram"].iloc[-3:].to_numpy(dtype=float)
            if len(hist) == 3 and not np.isnan(hist).any():
                scale = abs(float(hist[0])) + abs(float(hist[2])) + 1e-9
                q_mom = min(max(((float(hist[2]) - float(hist[0])) / scale + 1.0) / 2.0, 0.0), 1.0)

        q = (float(getattr(config, "RQ_W_LOW_VOL", 0.35)) * q_vol
             + float(getattr(config, "RQ_W_DRAWDOWN", 0.35)) * q_dd
             + float(getattr(config, "RQ_W_RANGE_POS", 0.30)) * q_pos
             + w_mom * q_mom)
        return round(w * q, 3)
    except Exception:  # noqa: BLE001  质量分是排序增强项，任何失败都不得影响选股主流程
        return 0.0


def _rank_signals(df: pd.DataFrame) -> pd.DataFrame:
    """确定性排序：排序分降序 → 底背离优先 → 盈亏比降序 → 股票代码升序（末级键）。

    主键 rank_score = daily_score + 连续质量分（_rank_quality_score）。该列缺失时
    （回测/单元测试手工构造的 DataFrame）自动退回 score，行为与引入前完全一致。
    所有键都相同的情况下按代码排序，保证两次运行结果可复现，
    不受并发完成顺序（as_completed）影响。
    """
    if "weekly_status" in df.columns and df["weekly_status"].eq("not_required").all():
        keys = [k for k in ("rank_score", "quality_score", "valuation_score", "code") if k in df.columns]
        return df.sort_values(keys, ascending=[k == "code" for k in keys],
                              kind="mergesort").reset_index(drop=True)
    keys = SORT_BY + ["has_divergence", "rr_ratio", "code"]
    ascending = SORT_ASC + [False, False, True]
    pairs = [(k, a) for k, a in zip(keys, ascending) if k in df.columns]
    if "rank_score" not in df.columns and "score" in df.columns:
        pairs.insert(0, ("score", False))   # 向后兼容：无 rank_score 时以 score 为主键
    if not pairs:
        return df.reset_index(drop=True)
    return df.sort_values(
        by=[k for k, _ in pairs],
        ascending=[a for _, a in pairs],
        kind="mergesort",
    ).reset_index(drop=True)

# ===== 严格确认指标判定（纯函数，数据缺失一律放行不误杀）=====

def _range_position_ok(daily_out: pd.DataFrame, config: StrategyConfig) -> bool:
    """现价须处于近 RANGE_LOOKBACK 日价格区间的下半部（位置 ≤ POSITION_IN_RANGE_MAX），确保买在低位"""
    win = daily_out.tail(config.RANGE_LOOKBACK)
    lo, hi = float(win["low"].min()), float(win["high"].max())
    if hi <= lo: return True
    return (float(daily_out.iloc[-1]["close"]) - lo) / (hi - lo) <= config.POSITION_IN_RANGE_MAX

def _macd_momentum_ok(daily_out: pd.DataFrame, config: StrategyConfig) -> bool:
    """MACD 柱持续改善：要求连续 MACD_MOMENTUM_DAYS 日柱值递增（绿柱缩短或红柱放大），
    过滤单日反抽造成的假改善；数据缺失一律放行不误杀"""
    n = max(1, int(getattr(config, "MACD_MOMENTUM_DAYS", 1)))
    if len(daily_out) < n + 1: return True
    hist = daily_out["macd_histogram"].iloc[-(n + 1):].tolist()
    if any(pd.isna(v) for v in hist): return True
    hist = [float(v) for v in hist]
    return all(hist[i] > hist[i - 1] for i in range(1, len(hist)))

def _kdj_ok(daily_out: pd.DataFrame, config: StrategyConfig) -> bool:
    """KDJ 处于金叉状态（K>D）且 K 值不在高位（≤ KDJ_K_MAX）；可选要求 K 值较昨日上行"""
    k, d = daily_out.iloc[-1]["kdj_k"], daily_out.iloc[-1]["kdj_d"]
    if pd.isna(k) or pd.isna(d): return True
    if not (float(k) > float(d) and float(k) <= config.KDJ_K_MAX): return False
    if getattr(config, "REQUIRE_KDJ_RISING", False) and len(daily_out) >= 2:
        k_prev = daily_out.iloc[-2]["kdj_k"]
        if not pd.isna(k_prev) and float(k) <= float(k_prev): return False
    return True

def macd_not_deeply_weak(daily_out: pd.DataFrame, days: int = 2,
                         hist_pct_max: float = -0.5) -> bool:
    """MACD 柱未处于「深度弱势且仍在恶化」状态（双条件 AND，缺一不否决）。

    判定为弱势需**同时**满足：
      1. 深度弱势：末根柱值 / 收盘价 × 100 ≤ hist_pct_max（默认 -0.5%），
         即 DIF 已明显位于 DEA 下方，属真实的空头动能区间；
      2. 仍在恶化：末 days+1 根柱值严格递减。

    为什么不能只用「柱值递减」：MACD 柱度量的是**加速度**而非趋势。匀速上行
    （斜率恒定）必然使柱值向 0 收敛——实测标准「长期下跌 → 稳步回升」的优质
    低估形态柱值为 [+0.0088, +0.0082, +0.0076]，递减但完全健康。只用递减条件
    会把整类健康形态判为走弱（P1 回测前置校验发现，已固化为回归测试）。
    加上「深度弱势」这一合取项后，柱值 >0 的匀速上行与小幅回踩不再触发。

    与 _macd_momentum_ok 的区别在门槛方向：后者要求「连续改善」（抄底策略的入场
    确认层），本函数只否决「深度弱势且继续恶化」（动能下限），供放量突破策略与
    quality_value 模式的荐股控制使用。
    数据不足或含 NaN 一律放行——不因缺数据误杀，数据缺失由上层 FAIL_DATA 兜底。
    """
    if daily_out is None or len(daily_out) == 0:
        return True
    if "macd_histogram" not in daily_out.columns or "close" not in daily_out.columns:
        return True
    n = max(1, int(days))
    if len(daily_out) < n + 1:
        return True
    hist = daily_out["macd_histogram"].iloc[-(n + 1):]
    if hist.isna().any():
        return True
    vals = [float(v) for v in hist.tolist()]
    try:
        close = float(daily_out["close"].iloc[-1])
    except (TypeError, ValueError):
        return True
    if not np.isfinite(close) or close <= 0:
        return True
    deeply_weak = (vals[-1] / close * 100) <= float(hist_pct_max)
    deteriorating = all(vals[i] > vals[i + 1] for i in range(len(vals) - 1))
    return not (deeply_weak and deteriorating)


def kdj_not_overheated(daily_out: pd.DataFrame, k_hard_max: float, dead_cross_k: float) -> bool:
    """KDJ 未处于「高位滞涨」状态（单侧下限闸门，不要求金叉）。

    只拦两种形态：
    1. K > k_hard_max：K 值已在高位，属高位接力；
    2. K < D 且 K ≥ dead_cross_k：已到区间顶部且开始掉头，属高位死叉。
    K 处于低位/中位时的 K<D 不拦：那既可能是下跌末段常态，也可能是匀速上行中
    K/D 交替领先的噪声（见 KDJ_DEAD_CROSS_K 的阈值说明）。

    刻意不要求金叉（K>D）：那会与抄底策略的严格确认层重复（同源指标双重闸门），
    且突破日 RSV 直接打到 100、K 值单日跳升，金叉常滞后 1-2 日——硬金叉会系统性
    漏掉「窄幅整理后首根放量阳线」这一目标形态。
    数据缺失/空表一律放行。
    """
    if daily_out is None or len(daily_out) == 0:
        return True
    if "kdj_k" not in daily_out.columns or "kdj_d" not in daily_out.columns:
        return True
    last = daily_out.iloc[-1]
    k, d = last.get("kdj_k"), last.get("kdj_d")
    if k is None or d is None or pd.isna(k) or pd.isna(d):
        return True
    k, d = float(k), float(d)
    if k > float(k_hard_max):
        return False
    if k < d and k >= float(dead_cross_k):
        return False
    return True


def _drop_incomplete_weekly_bar(weekly_df: Optional[pd.DataFrame], config: StrategyConfig) -> Optional[pd.DataFrame]:
    """剔除未收盘的周 bar（WEEKLY_REQUIRE_CLOSED_BAR 开启时）。

    数据源周线按「周内最新交易日」标注：周中运行时末根为未收盘的半成品 bar，
    其 close/MA10/MACD 会随周内行情继续变化，参与确认会造成口径漂移。
    规则：末 bar 日期落在本 ISO 周（北京时间）且不是周五 → 剔除；
    节假日周四收周也保守剔除（无法区分「周四提前收周」与「周中半成品」，
    代价是确认滞后一周）。
    """
    if weekly_df is None or weekly_df.empty or "date" not in weekly_df.columns:
        return weekly_df
    if not getattr(config, "WEEKLY_REQUIRE_CLOSED_BAR", True):
        return weekly_df
    w = weekly_df.sort_values("date").reset_index(drop=True)
    last = pd.to_datetime(w["date"].iloc[-1], errors="coerce")
    if pd.isna(last):
        return w
    today = _beijing_now().date()
    last_d = last.date()
    if last_d.isocalendar()[:2] == today.isocalendar()[:2] and last_d.weekday() != 4:
        w = w.iloc[:-1].reset_index(drop=True)
    return w


def check_weekly_trend(weekly_df: Optional[pd.DataFrame], config: StrategyConfig) -> bool:
    """周线趋势确认：收盘价站上周线 MA10（容忍 WEEKLY_TOLERANCE）且 MA10 在上行；
    WEEKLY_MA_BOTH_REQUIRED=False 退回旧行为（两条件满足其一即可）；数据不足放行。
    仅使用已收盘周 bar（见 _drop_incomplete_weekly_bar）。"""
    weekly_df = _drop_incomplete_weekly_bar(weekly_df, config)
    if weekly_df is None or weekly_df.empty or "close" not in weekly_df.columns: return True
    if len(weekly_df) < config.WEEKLY_MA_PERIOD + config.WEEKLY_SLOPE_LOOKBACK: return True
    w = weekly_df.sort_values("date").reset_index(drop=True) if "date" in weekly_df.columns else weekly_df.reset_index(drop=True)
    wma = w["close"].rolling(config.WEEKLY_MA_PERIOD).mean()
    ma_now, ma_prev = float(wma.iloc[-1]), float(wma.iloc[-(1 + config.WEEKLY_SLOPE_LOOKBACK)])
    close = float(w["close"].iloc[-1])
    above = close >= ma_now * (1 - config.WEEKLY_TOLERANCE)
    rising = ma_now > ma_prev
    return (above and rising) if config.WEEKLY_MA_BOTH_REQUIRED else (above or rising)

def check_weekly_macd(weekly_df: Optional[pd.DataFrame], config: StrategyConfig) -> bool:
    """周线 MACD 企稳确认：柱值翻红（含金叉当周及之后的红柱状态，动能占优），
    或绿柱连续 2 周收窄（柱值连续两周改善，下跌动能衰减企稳）；数据不足或 NaN 放行不误杀。
    仅使用已收盘周 bar（见 _drop_incomplete_weekly_bar）。"""
    weekly_df = _drop_incomplete_weekly_bar(weekly_df, config)
    if weekly_df is None or weekly_df.empty or "close" not in weekly_df.columns: return True
    if len(weekly_df) < config.WEEKLY_MACD_SLOW + config.WEEKLY_MACD_SIGNAL: return True
    w = weekly_df.sort_values("date").reset_index(drop=True) if "date" in weekly_df.columns else weekly_df.reset_index(drop=True)
    dif = w["close"].ewm(span=config.WEEKLY_MACD_FAST, adjust=False).mean() - w["close"].ewm(span=config.WEEKLY_MACD_SLOW, adjust=False).mean()
    hist = dif - dif.ewm(span=config.WEEKLY_MACD_SIGNAL, adjust=False).mean()
    h1, h2, h3 = hist.iloc[-1], hist.iloc[-2], hist.iloc[-3]
    if pd.isna(h1) or pd.isna(h2) or pd.isna(h3): return True
    if float(h1) > 0: return True                 # 红柱（含金叉翻红），周线动能已占优
    return float(h1) > float(h2) > float(h3)      # 绿柱连续 2 周收窄，企稳确认

def _weekly_trend_data_ok(weekly_df: Optional[pd.DataFrame], config: StrategyConfig) -> bool:
    """周线均线确认的数据是否充分：None/空/长度不足 → False（无法有效确认 → 待核验候选）"""
    if weekly_df is None or weekly_df.empty or "close" not in weekly_df.columns: return False
    return len(weekly_df) >= config.WEEKLY_MA_PERIOD + config.WEEKLY_SLOPE_LOOKBACK

def _weekly_macd_data_ok(weekly_df: Optional[pd.DataFrame], config: StrategyConfig) -> bool:
    """周线 MACD 确认的数据是否充分：None/空/长度不足 → False（无法有效确认 → 待核验候选）"""
    if weekly_df is None or weekly_df.empty or "close" not in weekly_df.columns: return False
    return len(weekly_df) >= config.WEEKLY_MACD_SLOW + config.WEEKLY_MACD_SIGNAL

def _weekly_fresh_enough(weekly_df: Optional[pd.DataFrame], latest_trade_date: str) -> bool:
    """周线数据截止日期须覆盖最新交易日所在周（最后一根周线 bar 日期 ≥ 该周周一），
    停牌股的周线滞后时不足以作为「当前」趋势确认。基准日期无法解析时不做强校验。"""
    if weekly_df is None or weekly_df.empty or "date" not in weekly_df.columns: return False
    try:
        d = datetime.strptime(str(latest_trade_date), _DATE_FMT)
    except (TypeError, ValueError):
        return True
    week_start = (d - timedelta(days=d.weekday())).strftime(_DATE_FMT)
    last = str(weekly_df.sort_values("date").iloc[-1]["date"])
    return last >= week_start

def has_halt_gap(daily_out: Optional[pd.DataFrame], config: StrategyConfig) -> bool:
    """K 线序列是否存在停牌缺口（相邻 bar 的最大自然日间隔 > MAX_BAR_GAP_DAYS）。

    停牌 30~60 天的复牌股能同时骗过既有的两道校验：MIN_DAYS 只数 bar 总数
    （60 根 bar 可能覆盖 160 个自然日），REQUIRE_FRESH_DAILY 只看末根日期是否等于
    市场最新交易日。这类股票的 ma20/rsi14/kdj/atr/60日高点/量比 全部跨缺口计算：
    「近60日最高价」实际是 5 个月前的高点，量比把停牌前的死量与复牌后的爆量
    混进同一个 20 日窗口——所有下游闸门都在拿无经济意义的数字做判定。

    阈值 12 天可容纳春节（相邻交易日间隔约 11 天）与国庆（约 8 天）长假，
    超过即判定为停牌。数据缺失/无法解析时返回 False（放行不误杀，与既有约定一致）。
    """
    if daily_out is None or daily_out.empty or "date" not in daily_out.columns:
        return False
    if not getattr(config, "REQUIRE_NO_HALT_GAP", True):
        return False
    try:
        gaps = pd.to_datetime(daily_out["date"], errors="coerce").diff().dt.days
        max_gap = gaps.max()
    except Exception:  # noqa: BLE001
        return False
    if pd.isna(max_gap):
        return False
    return float(max_gap) > float(getattr(config, "MAX_BAR_GAP_DAYS", 12))


def _signal_hits(d_last) -> list[str]:
    """实际触发的技术条件标签（入选依据）。

    抽成函数供「正式推荐」与「波动率否决观察池」共用，避免两处措辞漂移。
    """
    hits: list[str] = []
    if bool(d_last.get("trend_turn", False)): hits.append("趋势转折")
    if bool(d_last.get("rsi_rebound", False)): hits.append("RSI超卖反弹")
    vp_label = str(d_last.get("vol_price_label", "") or "")
    if bool(d_last.get("vol_price_coord", False)): hits.append(vp_label or "放量上涨")
    if bool(d_last.get("multi_resonance", False)): hits.append("多周期共振")
    if bool(d_last.get("bottom_divergence", False)): hits.append("底背离")
    return hits


def evaluate(daily_df: Optional[pd.DataFrame], code: str = "", name: str = "", config: Optional[StrategyConfig] = None, market_env: Optional[dict] = None, fund_data: Optional[dict] = None, latest_trade_date: Optional[str] = None, volatile_out: Optional[list] = None, val_context: Optional[dict] = None) -> tuple[Optional[Signal], str]:
    """评估单只股票。

    latest_trade_date：市场（沪深300）最新交易日，用于行情时效校验——
    个股最新K线日期与之一致才评估（停牌/数据滞后 → 暂不推荐），None 时跳过校验。
    volatile_out：可选 list，传入后被 FAIL_VOLATILE 否决的个股会以明细 dict 追加进去
    （供日志逐只打印与飞书高风险观察池展示）；不影响返回值与准入判定。
    val_context：行业相对估值上下文（仅 quality_value 模式使用，见 _industry_valuation_context）；
    None 时估值闸门回退绝对阈值。technical 模式忽略该参数。
    """
    if config is None: config = StrategyConfig()
    if config.RECOMMENDATION_MODE == "quality_value":
        return evaluate_quality_value(daily_df, code, name, config, market_env,
                                      fund_data, latest_trade_date, val_context=val_context)
    regime = (market_env or {}).get("regime", "unknown")

    if not check_fundamentals(fund_data, config, code=code, name=name): return None, "FAIL_FUND"

    # 流动性前置检查（独立于数据缺失归因）：近 20 日日均成交额低于下限判僵尸股。
    # 放在 compute_daily_signals 之前，避免低流动性标的白算一遍指标。
    if daily_df is None or daily_df.empty: return None, "FAIL_DATA"
    if "amount" in daily_df.columns and len(daily_df) >= 20:
        _avg_amt = pd.to_numeric(daily_df["amount"], errors="coerce").tail(20).mean()
        if pd.isna(_avg_amt): return None, "FAIL_DATA"
        if float(_avg_amt) < config.MIN_AMOUNT: return None, "FAIL_LIQUIDITY"

    daily_out = compute_daily_signals(daily_df, config)
    if daily_out is None or daily_out.empty: return None, "FAIL_DATA"

    d_last = daily_out.iloc[-1]

    # 数据时效：个股最新K线须与市场最新交易日一致（停牌/数据滞后 → 暂不推荐）
    if config.REQUIRE_FRESH_DAILY and latest_trade_date is not None \
            and str(d_last["date"]) != str(latest_trade_date):
        return None, "FAIL_STALE"

    # 停牌缺口：K 线跨停牌区间时全部滚动指标失真（详见 has_halt_gap）。
    # 紧跟时效校验，使「数据不可信」的两个归因（FAIL_STALE / FAIL_HALT_GAP）在漏斗中同层。
    if has_halt_gap(daily_out, config):
        return None, "FAIL_HALT_GAP"

    # 核验状态维度一：财务核验（核心项=ROE+负债率；商誉/扣非为可选项，缺失只记标签）
    fund_status, fund_missing = _fund_verify_state(fund_data)
    missing_tags: list[str] = list(fund_missing)

    # 估值闸门（A：低估值）：peTTM/pbMRQ 取自 Baostock 日线逐 bar（点位正确、无前视）。
    # 双列均缺失（AkShare 兜底 / 旧缓存无该字段）→ 记缺项放行，不硬否决；
    # 有值时：亏损（peTTM≤0，可选）、PE 偏高、PB 异常或偏高 → FAIL_VALUATION。
    if config.USE_VALUATION_FILTER:
        pe_raw, pb_raw = d_last.get("peTTM"), d_last.get("pbMRQ")
        pe = float(pe_raw) if pe_raw is not None and not pd.isna(pe_raw) else None
        pb = float(pb_raw) if pb_raw is not None and not pd.isna(pb_raw) else None
        if pe is None and pb is None:
            missing_tags.append("valuation")
        else:
            if pe is not None:
                if (config.REQUIRE_POSITIVE_PE and pe <= 0) or pe > config.MAX_PE_TTM:
                    return None, "FAIL_VALUATION"
            if pb is not None and (pb <= 0 or pb > config.MAX_PB_MRQ):
                return None, "FAIL_VALUATION"

    # 防追高否决：当日涨幅过大（涨停买不进、大阳线次日易回调），数据缺失时放行不误杀（记缺项）
    pct_chg_today = d_last.get("pct_chg")
    if pct_chg_today is None or pd.isna(pct_chg_today):
        missing_tags.append("pct_chg")
    elif float(pct_chg_today) > config.MAX_ENTRY_PCT_CHG:
        return None, "FAIL_CHASE"
    # 防跳空否决：开盘相对前收跳空高开过多，追买风险大（开盘价缺失记缺项）
    open_today = d_last.get("open")
    prev_close = float(daily_out.iloc[-2]["close"]) if len(daily_out) >= 2 else None
    if open_today is None or pd.isna(open_today) or not prev_close or prev_close <= 0:
        missing_tags.append("gap")
    elif (float(open_today) / prev_close - 1) * 100 > config.MAX_GAP_UP_PCT:
        return None, "FAIL_GAP"

    # 已反弹一段否决：近 N 日累计涨幅过大，说明底部反弹可能已走完，此时介入性价比低。
    # 比 RSI 上限更直接——RSI14 被 14 日平滑，夹杂回调的连续上攻可能尚未触发 RSI 阈值
    n_gain = max(1, int(config.RECENT_GAIN_LOOKBACK))
    if len(daily_out) > n_gain:
        base_close = float(daily_out.iloc[-(1 + n_gain)]["close"])
        if base_close > 0 and (float(d_last["close"]) / base_close - 1) * 100 > config.MAX_RECENT_GAIN_PCT:
            return None, "FAIL_RECENT_RALLY"

    # RSI 入场上限否决：RSI14 过高说明已反弹一段，不再是底部入场点
    rsi_today = d_last.get("rsi14")
    if rsi_today is not None and not pd.isna(rsi_today) and float(rsi_today) > config.DAILY_RSI_ENTRY_MAX:
        return None, "FAIL_RSI_HIGH"
    # 天量否决：量比异常放大疑似出货/消息驱动，次日接力风险大
    vol_ratio_today = d_last.get("daily_vol_ratio")
    if vol_ratio_today is not None and not pd.isna(vol_ratio_today) and float(vol_ratio_today) > config.MAX_VOL_RATIO:
        return None, "FAIL_CLIMAX_VOL"
    # 底部区域过滤：距近 N 日高点回撤不足，不符合抄底定位（上涨中继回调不买）
    high_n = float(daily_out["high"].tail(config.DRAWDOWN_LOOKBACK).max())
    last_close = float(d_last["close"])
    drawdown = (high_n - last_close) / high_n if high_n > 0 else 0.0
    if high_n > 0 and drawdown < config.MIN_DRAWDOWN_FROM_HIGH:
        return None, "FAIL_NOT_BOTTOM"
    # 回撤上限否决：跌幅过深多为基本面恶化/退市风险（价值陷阱），并非健康底部
    if high_n > 0 and drawdown > config.MAX_DRAWDOWN_FROM_HIGH:
        return None, "FAIL_DEEP_CRASH"

    # 严格确认指标：低位/动能/KDJ 三重确认，任一不过即否决；数据缺失放行但记缺项
    if not _range_position_ok(daily_out, config): return None, "FAIL_POSITION"
    if config.REQUIRE_MACD_MOMENTUM:
        if not _macd_momentum_ok(daily_out, config): return None, "FAIL_MACD_MOM"
        n_mom = max(1, int(config.MACD_MOMENTUM_DAYS))
        if len(daily_out) <= n_mom or daily_out["macd_histogram"].iloc[-(n_mom + 1):].isna().any():
            missing_tags.append("macd_mom")
    if config.REQUIRE_KDJ_GOLDEN:
        if not _kdj_ok(daily_out, config): return None, "FAIL_KDJ"
        if pd.isna(d_last.get("kdj_k")) or pd.isna(d_last.get("kdj_d")):
            missing_tags.append("kdj")

    daily_score = float(d_last.get("daily_score", 0))
    has_div = bool(d_last.get("bottom_divergence", False))
    # 评级统一：等级与展示分数同源（均按原始技术分定级），分数与等级不再脱节；
    # 熊市不再「扣分定级」，改为直接上浮准入分数线（效果等价、口径透明）；
    # 底背离不提升评级，仅作形态标签参与展示与同分排序
    grade = _grade_from_score(daily_score, config)
    min_grade = config.MIN_PASS_GRADE if config.MIN_PASS_GRADE in _GRADE_ORDER else "B"
    grade_floor = {"A": config.GRADE_A, "B": config.GRADE_B, "C": config.GRADE_C, "D": 0.0}[min_grade]
    required_score = grade_floor + (config.BEAR_GRADE_BOOST if regime == "bear" else 0.0)
    if daily_score < required_score:
        return None, "FAIL_TECH"

    # 波动率风控：ATR 缺失时无法计算真实风险，不能以默认值静默放行。
    # ATR 超限仍归因 FAIL_VOLATILE；缺失归因 FAIL_DATA，避免把不可复核信号当作正式信号。
    atr_val = d_last.get("atr")
    atr_val = float(atr_val) if atr_val is not None and not pd.isna(atr_val) else None
    if atr_val is None or last_close <= 0:
        return None, "FAIL_DATA"
    if (atr_val / last_close * 100) > config.MAX_ATR_PCT:
        # 明细留档：此处是「技术面已达标、仅波动率超限」的最后一层否决，
        # 也是实盘中最常见的「今天为什么没有推荐」的原因，逐只记录下来。
        if volatile_out is not None:
            # 并发筛选下由 worker 线程直接 append：CPython 的 list.append 是原子操作，
            # 与 fetch_stats 的计数写入同一约定（单次运行量级 <100，无需加锁）。
            volatile_out.append(_build_volatile_row(
                code=code, name=name, date=d_last["date"], close=last_close,
                atr=atr_val, atr_pct=atr_val / last_close * 100, limit=config.MAX_ATR_PCT,
                score=daily_score, grade=grade, hits=",".join(_signal_hits(d_last)),
                rsi=d_last.get("rsi14"), vol_ratio=d_last.get("daily_vol_ratio"),
                drawdown=drawdown,
            ))
        return None, "FAIL_VOLATILE"

    rr = compute_risk_reward(entry_price=last_close, config=config, atr=atr_val)

    # 入选依据：实际触发的技术条件（量价按质量分档区分措辞；与观察池共用同一实现）
    hits: list[str] = _signal_hits(d_last)

    sig = Signal(
        code=code, name=name, date=pd.to_datetime(d_last["date"]).strftime("%Y-%m-%d"),
        close=round(last_close, 2), score=round(daily_score, 1), grade=grade,
        daily_score=round(daily_score, 1), rsi=round(float(d_last.get("rsi14", 50)), 1),
        rsi7=round(float(d_last.get("rsi7", 50)), 1), rsi21=round(float(d_last.get("rsi21", 50)), 1),
        vol_ratio=round(float(d_last.get("daily_vol_ratio", 0)), 2),
        turnover_ratio=round(float(d_last.get("daily_turnover_ratio", 0)), 2),
        stop_loss=rr["stop_loss"], take_profit=rr["take_profit"], rr_ratio=rr["rr_ratio"],
        market_env=regime, has_divergence=has_div,
        signals_hit=",".join(hits), fund_status=fund_status,
        missing_tags=",".join(dict.fromkeys(t for t in missing_tags if t)),  # 去重保序
        tier="formal" if fund_status == "verified" else "pending",
        # 排序分：技术分（决定准入，不变）+ 连续质量分（只决定同批通过者之间的先后）
        rank_score=round(daily_score + _rank_quality_score(daily_out, config), 3),
    )
    return sig, "PASS"

def enrich_annual_fundamentals(code: str, fund_data: Optional[dict], config: StrategyConfig,
                               as_of: Optional[str] = None) -> dict:
    """年度数据独立缓存，决策日期隔离；缺失不伪造、不用季度年化替代。"""
    from src.fundamental_quality import fetch_annual_quality
    result = dict(fund_data or {})
    day = str(as_of or _beijing_now().date())[:10]
    if "annual_rows" in result:
        return result
    path = _cache_path(config, f"annual_quality_v1_{code}_{day}_{config.QUALITY_YEARS}.json") if config.USE_CACHE else ""
    cached = _read_cache_json(path) if path and _cache_fresh(path, config.FUND_CACHE_TTL_DAYS) else None
    if cached is not None:
        result["annual_rows"] = cached.get("annual_rows", [])
        return result
    fetched = fetch_annual_quality(code, day, years=config.QUALITY_YEARS,
                                   ak_client=ak if _AK_AVAILABLE else None)
    rows = fetched.get("annual_rows", [])
    result["annual_rows"] = rows
    if path and rows:
        _write_cache_json({"annual_rows": rows}, path)
    return result


def enrich_forward_growth(code: str, fund_data: Optional[dict], config: StrategyConfig,
                          cache: Optional[CacheManager] = None) -> dict:
    """把「最新报告期净利润同比」并入 fund_data（forward_ni_yoy / forward_stat_date）。

    前瞻确认闸门（REQUIRE_FORWARD_CONFIRMATION）读取该字段：当年净利大幅下滑即否决，
    对治「trailing 3 年年报漂亮、但当年基本面正在崩」的价值陷阱。拿不到成长数据时
    不写该键 → evaluate_quality_value 记「forward」缺项降级为待核验（不硬否决）。
    """
    result = dict(fund_data or {})
    if not getattr(config, "REQUIRE_FORWARD_CONFIRMATION", False):
        return result
    if "forward_ni_yoy" in result:
        return result
    growth = get_growth(code, cache, config)
    if growth and growth.get("ni_yoy") is not None:
        result["forward_ni_yoy"] = growth.get("ni_yoy")
        result["forward_stat_date"] = growth.get("stat_date")
    return result


def _finite_positive_or_none(value) -> Optional[float]:
    """有限且为正的浮点，否则 None（用于行业估值横截面，剔除亏损/异常/缺失）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) and v > 0 else None


def _percentile_of(sorted_vals: Optional[np.ndarray], value: float) -> Optional[float]:
    """value 在升序数组中的分位（≤value 的占比，0~1）；数组空/None 返回 None。"""
    if sorted_vals is None or len(sorted_vals) == 0:
        return None
    return float(np.searchsorted(sorted_vals, value, side="right")) / float(len(sorted_vals))


def build_industry_valuation_snapshot(config: StrategyConfig, cache: CacheManager,
                                      stock_list: list[dict]) -> dict:
    """构建「行业 -> 当日 PE/PB 横截面（升序数组）」快照，供行业相对估值闸门使用。

    - 关闭 USE_INDUSTRY_RELATIVE_VALUATION 或行业数据不可用 → 返回空 dict
      （上层全市场回退到绝对阈值 MAX_PE_TTM / MAX_PB_MRQ，行业接口挂掉时不误放行）。
    - 复用 get_daily_data 缓存：与随后的筛选阶段共享同一份日线，不额外增加取数成本
      （快照阶段触发取数并写入内存/磁盘缓存，筛选阶段直接命中）。
    - 仅纳入 peTTM/pbMRQ 有限且为正的样本（亏损/异常值会扭曲分位）。
    """
    if not getattr(config, "USE_INDUSTRY_RELATIVE_VALUATION", False):
        return {}
    industry_map = get_stock_industry(config, cache)
    if not industry_map:
        return {}
    pe_by_ind: dict[str, list] = {}
    pb_by_ind: dict[str, list] = {}
    lock = threading.Lock()
    index = get_index_daily(config, cache)
    if index is None or index.empty:
        return {}
    snapshot_day = str(index.iloc[-1]["date"])[:10]

    # ===== 纯观测层：快照阶段进度日志 =====
    # 本函数用独立 ThreadPoolExecutor 遍历全 A 拉日线，不走 run_concurrent_screen，
    # 因此既无心跳看门狗也无 PROGRESS_LOG_EVERY 进度；冷启动 cache 全 miss 时
    # 是长达 1~2h+ 的静默黑洞（曾被误判为死锁）。这里补开始/周期进度/结束耗时三处
    # 打点，不改任何口径、返回值或异常语义。
    total_stocks = len(stock_list)
    progress_every = int(getattr(config, "SNAPSHOT_PROGRESS_LOG_EVERY", 200))
    counter = {"done": 0, "hit": 0}
    snapshot_start = time.time()
    logger.info("行业估值快照构建开始：%d 只股票，%d 线程，快照日 %s（每 %d 只打印一次进度）",
                total_stocks, config.MAX_WORKERS, snapshot_day, progress_every)

    def collect(stock: dict) -> None:
        hit = False
        try:
            code = str(stock.get("code", ""))
            ind = industry_map.get(code, "")
            if not ind:
                return
            daily = get_daily_data(code, config, cache)
            if daily is None or daily.empty:
                return
            last = daily.iloc[-1]
            if str(last.get("date", ""))[:10] != snapshot_day:
                return
            pe = _finite_positive_or_none(last.get("peTTM"))
            pb = _finite_positive_or_none(last.get("pbMRQ"))
            if pe is None and pb is None:
                return
            with lock:
                if pe is not None:
                    pe_by_ind.setdefault(ind, []).append(pe)
                if pb is not None:
                    pb_by_ind.setdefault(ind, []).append(pb)
            hit = True
        finally:
            with lock:
                counter["done"] += 1
                if hit:
                    counter["hit"] += 1
                done = counter["done"]
                hits = counter["hit"]
            if total_stocks and (done % progress_every == 0 or done == total_stocks):
                elapsed = time.time() - snapshot_start
                rate = done / elapsed if elapsed > 0 else 0
                eta_min = (total_stocks - done) / rate / 60 if rate > 0 else 0
                logger.info("行业估值快照进度: %d/%d (%.0f%%)，命中 %d，已用 %.1f 分钟，预计剩余 %.1f 分钟",
                            done, total_stocks, done / total_stocks * 100,
                            hits, elapsed / 60, eta_min)

    try:
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
            list(pool.map(collect, stock_list))
    except Exception as e:  # noqa: BLE001  快照失败不得阻断选股，降级为绝对阈值
        logger.warning("行业估值快照构建失败（已完成 %d/%d，耗时 %.1f 分钟），本次回退绝对估值阈值: %s",
                       counter["done"], total_stocks, (time.time() - snapshot_start) / 60, e)
        return {}
    snapshot: dict[str, dict] = {}
    for ind in set(pe_by_ind) | set(pb_by_ind):
        snapshot[ind] = {
            "pe": np.sort(np.asarray(pe_by_ind.get(ind, []), dtype=float)),
            "pb": np.sort(np.asarray(pb_by_ind.get(ind, []), dtype=float)),
        }
    if snapshot:
        logger.info("行业估值快照：%d 个行业纳入横截面（行业相对估值闸门生效），完成 %d/%d，命中 %d，耗时 %.1f 分钟",
                    len(snapshot), counter["done"], total_stocks, counter["hit"],
                    (time.time() - snapshot_start) / 60)
    return snapshot


def _industry_valuation_context(snapshot: Optional[dict], industry: str,
                                pe: Optional[float], pb: Optional[float],
                                config: StrategyConfig) -> Optional[dict]:
    """计算个股在其行业内的 PE/PB 分位；返回 None 表示应回退绝对阈值。

    回退条件：功能关闭 / 无快照 / 无行业归属 / 行业内可比样本 < VALUATION_INDUSTRY_MIN_PEERS。
    返回 {"mode":"industry","pe_pct":float|None,"pb_pct":float|None,"peers":int}。
    pe_pct/pb_pct 为 None 表示该指标在行业内样本不足，单指标各自回退绝对阈值。
    """
    if not getattr(config, "USE_INDUSTRY_RELATIVE_VALUATION", False):
        return None
    if not snapshot or not industry:
        return None
    bucket = snapshot.get(industry)
    if not bucket:
        return None
    pe_arr, pb_arr = bucket.get("pe"), bucket.get("pb")
    min_peers = int(getattr(config, "VALUATION_INDUSTRY_MIN_PEERS", 5))
    pe_pct = _percentile_of(pe_arr, pe) if (pe is not None and pe_arr is not None
                                            and len(pe_arr) >= min_peers) else None
    pb_pct = _percentile_of(pb_arr, pb) if (pb is not None and pb_arr is not None
                                            and len(pb_arr) >= min_peers) else None
    if pe_pct is None and pb_pct is None:
        return None
    peers = max(len(pe_arr) if pe_arr is not None else 0,
                len(pb_arr) if pb_arr is not None else 0)
    return {"mode": "industry", "pe_pct": pe_pct, "pb_pct": pb_pct, "peers": peers}


def _tag_valuation_mode(frame: pd.DataFrame, snapshot: Optional[dict],
                        industry_map: dict, config: StrategyConfig) -> pd.DataFrame:
    """给名单逐行标注实际生效的估值口径（industry / absolute），并汇总回退原因。

    行业相对估值依赖单一 Baostock 接口 `query_stock_industry`：它不可用时快照为空，
    全市场**静默**回退绝对阈值（PE≤25 / PB≤3）——而绝对阈值恰是本项目刻意避开的
    「名单压向银行/地产/周期」口径。叠加综合分下限后回退日 formal 极易归零。
    因此把口径落到数据里（`valuation_mode` 列），由日志与飞书卡片显式声明，
    不再让「今天没有好票」与「口径悄悄换了」混在一起。
    """
    modes: list[str] = []
    reasons: dict[str, int] = {}
    for _, row in frame.iterrows():
        code = str(row.get("code"))
        industry = (industry_map or {}).get(code, "") or ""
        ctx = _industry_valuation_context(snapshot, industry,
                                          _num_or_none(row.get("pe_ttm")),
                                          _num_or_none(row.get("pb_mrq")), config)
        if ctx is not None:
            modes.append("industry")
            continue
        modes.append("absolute")
        if not snapshot:
            key = "行业数据不可用（快照为空）"
        elif not industry:
            key = "无行业归属"
        elif not snapshot.get(industry):
            key = "行业不在快照内"
        else:
            key = f"行业内可比样本 <{int(getattr(config, 'VALUATION_INDUSTRY_MIN_PEERS', 5))}"
        reasons[key] = reasons.get(key, 0) + 1
    out = frame.copy()
    out["valuation_mode"] = modes
    if reasons:
        detail = "，".join(f"{k} {v} 只" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]))
        logger.warning("行业相对估值未生效的正式推荐 %d/%d 只（已回退绝对阈值 PE≤%g/PB≤%g）：%s",
                       sum(reasons.values()), len(out), config.MAX_PE_TTM, config.MAX_PB_MRQ, detail)
    return out


def qv_floor_equivalence(config: StrategyConfig) -> dict:
    """量化 MIN_QV_SCORE 相对三道硬闸门的等效门槛（纯计算，不取数、不联网）。

    评分口径决定了「恰好压线通过硬闸门」的公司拿不到下限分，因此该下限**严格强于**
    三档硬闸门之和。这个结论此前只写在配置注释里，实盘日志完全看不到，容易把
    「被下限卡掉」误读为「市场没机会」；此函数把它算成数字供漏斗日志与审计使用。

    · 最小可达质量分：构造「中位 ROE = QUALITY_MEDIAN_ROE_MIN、最低 ROE = QUALITY_MIN_ROE、
      现金转换 = QUALITY_CASH_CONVERSION_MIN」三档同时压线的样本（如 3 年 ROE=(5,10,10)），
      三项在各自阈值处各得 50 分，故质量分下限为 50。这里直接调 evaluate_annual_quality
      实测，避免与实现漂移；异常时退回理论值 50。
    · 压线估值分：行业口径在分位上限处 100×(1−0.60)=40；绝对口径在上限处 100×(1−1)=0。
    """
    qw, vw, tw = (float(config.QUALITY_SCORE_WEIGHT), float(config.VALUATION_SCORE_WEIGHT),
                  float(config.TECHNICAL_SCORE_WEIGHT))
    min_quality = 50.0
    try:
        from src.fundamental_quality import evaluate_annual_quality
        years = int(config.QUALITY_YEARS)
        med, low = float(config.QUALITY_MEDIAN_ROE_MIN), float(config.QUALITY_MIN_ROE)
        if years >= 1 and low <= med:
            roes = [low] + [med] * (years - 1)
            cash = float(config.QUALITY_CASH_CONVERSION_MIN)
            rows = [{"year": 2020 + i, "report_date": f"{2020 + i}-12-31",
                     "available_date": f"{2021 + i}-04-20", "roe": r,
                     "deducted_profit": 90.0, "net_profit": 100.0,
                     "operating_cashflow": 100.0 * cash} for i, r in enumerate(roes)]
            probe = evaluate_annual_quality(rows, f"{2020 + years}-06-30", years=years,
                                            median_roe_min=med, min_roe=low,
                                            cash_conversion_min=cash)
            if probe.get("status") == "verified" and probe.get("quality_score") is not None:
                min_quality = float(probe["quality_score"])
    except Exception:  # noqa: BLE001  纯审计信息，任何失败都不影响选股
        min_quality = 50.0
    v_industry = 100.0 * (1.0 - float(getattr(config, "VALUATION_INDUSTRY_PERCENTILE_MAX", 0.60)))
    v_absolute = 0.0
    floor = float(getattr(config, "MIN_QV_SCORE", 0.0) or 0.0)

    def _ceiling(valuation_score: float) -> float:
        return qw * min_quality + vw * valuation_score + tw * 100.0

    def _req_tech(quality_score: float, valuation_score: float) -> Optional[float]:
        if tw <= 0:
            return None
        return (floor - qw * quality_score - vw * valuation_score) / tw

    return {
        "floor": floor,
        "min_verified_quality": round(min_quality, 2),
        "valuation_industry_at_cap": round(v_industry, 2),
        "valuation_absolute_at_cap": round(v_absolute, 2),
        "ceiling_industry": round(_ceiling(v_industry), 2),
        "ceiling_absolute": round(_ceiling(v_absolute), 2),
        "req_tech_industry": _req_tech(min_quality, v_industry),
        "req_tech_absolute": _req_tech(min_quality, v_absolute),
    }


def describe_qv_floor(config: StrategyConfig) -> str:
    """把 MIN_QV_SCORE 的真实严厉度写成一行日志（数值随配置实时计算，不写死）。"""
    e = qv_floor_equivalence(config)

    def _fmt(v: Optional[float]) -> str:
        if v is None:
            return "技术分权重为 0"
        return "不可达（需 >100）" if v > 100.0 else f"≥{v:.0f}"

    return (
        f"综合分下限 {e['floor']:.0f}｜压线合格样本质量分仅 {e['min_verified_quality']:.1f}，"
        f"其综合分上限为 行业口径 {e['ceiling_industry']:.1f} / 绝对口径 {e['ceiling_absolute']:.1f}"
        f"（均低于下限 → 该下限严格强于三道硬闸门，压线合格者 100% 被淘汰）；"
        f"要过线所需技术分：行业口径 {_fmt(e['req_tech_industry'])}，"
        f"绝对口径 {_fmt(e['req_tech_absolute'])}"
    )


def assess_entry_timing(technical: pd.DataFrame, config: StrategyConfig) -> dict:
    """入场时机判读（P0）：为 quality_value 推荐补上「左侧/右侧 + 是否仍在下跌」的择时读数。

    生产模式旁路了全部技术择时闸门，深价值股可能在下跌途中被推荐（价值陷阱）。本函数
    **只产出展示标签与决策简报素材，绝不参与任何否决**，"哪些股票通过"与改动前完全一致。

    判读维度（全部取自 compute_daily_signals 已算出的列 + 本地 MA60，零额外取数）：
      - 现价 vs MA20 / MA60：站上与否区分右侧（趋势转强）/ 左侧（待企稳）；
      - MACD 是否「深度走弱且仍在恶化」（复用 macd_not_deeply_weak）：识别未止跌的接飞刀；
      - 距 250 日低点天数与幅度：区分「刚见底」与「已横盘筑底一段」。
    数据不足/异常一律回退中性读数（side="unknown"），不影响主流程。
    """
    result = {"side": "unknown", "label": "时机未知", "below_ma20": None, "below_ma60": None,
              "macd_weak": False, "days_since_low": None, "pct_from_low": None,
              "ma20": None, "ma60": None, "recent_low": None}
    try:
        if technical is None or technical.empty or "close" not in technical.columns:
            return result
        d = technical.iloc[-1]
        close = float(d["close"])
        if not close > 0:
            return result
        ma20 = float(d["ma20"]) if "ma20" in technical.columns and not pd.isna(d.get("ma20")) else None
        ma_long = max(1, int(getattr(config, "TIMING_MA_LONG", 60)))
        ma60_ser = technical["close"].rolling(ma_long).mean()
        ma60 = float(ma60_ser.iloc[-1]) if not pd.isna(ma60_ser.iloc[-1]) else None
        # 「仍在下跌」= MACD 柱深度走弱且连续恶化（与荐股控制共用同一实现与阈值）
        macd_weak = not macd_not_deeply_weak(
            technical, config.MACD_WEAK_DAYS, config.MACD_WEAK_HIST_PCT)
        # 距 250 日低点：幅度与天数
        lookback = max(1, int(getattr(config, "LOW_POSITION_LOOKBACK", 250)))
        days_since_low = pct_from_low = recent_low = None
        if "low" in technical.columns:
            win = technical["low"].tail(lookback)
            if len(win) > 0 and not win.isna().all():
                lo = float(win.min())
                if lo > 0:
                    recent_low = lo
                    pct_from_low = (close / lo - 1) * 100
                    try:
                        days_since_low = len(technical) - 1 - int(win.idxmin())
                    except (ValueError, TypeError):
                        days_since_low = None
        below_ma20 = (close < ma20) if ma20 else None
        below_ma60 = (close < ma60) if ma60 else None
        if macd_weak:
            side, label = "falling", "⚠左侧·仍在下跌（MACD深度走弱，止跌未确认）"
        elif below_ma20 is False and below_ma60 is False:
            side, label = "right", "右侧·站上MA20/MA60（趋势转强）"
        elif below_ma20 is False:
            side, label = "right_early", "右侧雏形·站上MA20（仍在MA60下方）"
        elif below_ma20 is True:
            side, label = "left", "左侧·MA20下方待企稳"
        else:
            side, label = "unknown", "时机未知"
        result.update(side=side, label=label, below_ma20=below_ma20, below_ma60=below_ma60,
                      macd_weak=macd_weak, days_since_low=days_since_low,
                      pct_from_low=pct_from_low, ma20=ma20, ma60=ma60, recent_low=recent_low)
    except Exception:  # noqa: BLE001  时机判读是展示增强项，任何失败都不得影响选股主流程
        return result
    return result


def build_decision_brief(*, timing: dict, quality: dict, pe: Optional[float], pb: Optional[float],
                         val_context: Optional[dict], industry_mode: bool, position: float,
                         close: float, stop_loss: Optional[float], forward_ni_yoy: Optional[float],
                         forward_verified: bool, forward_stat_date: Optional[str], missing: list,
                         hits: list, quality_status: str, config: StrategyConfig) -> dict:
    """组装 P5 决策简报：看多理由 / 主要风险 / 失效价 / 信心分档 / 今日为何触发。

    纯展示、纯字符串，只依赖已算出的量（timing/quality/估值/成长/缺项/技术触发），
    不取数、不否决、不改排序。任一素材缺失即省略对应条目，绝不伪造。
    返回 {"bull_case","bear_case","invalidation","conviction","why_today"}。
    """
    metrics = (quality or {}).get("metrics", {}) if isinstance(quality, dict) else {}

    def _pct(v):
        n = _num_or_none(v)
        return None if n is None else round(n * 100, 1)

    # ---- 看多理由（只列有据可查的正面因子）----
    bull: list[str] = []
    med_roe = _num_or_none(metrics.get("median_roe"))
    cash_conv = _num_or_none(metrics.get("cash_conversion"))
    if med_roe is not None:
        cc = f"、现金转换率 {cash_conv:.2f}" if cash_conv is not None else ""
        bull.append(f"近{config.QUALITY_YEARS}年中位ROE {med_roe:.1f}%{cc}")
    if pe is not None and pe > 0:
        if industry_mode and val_context and val_context.get("pe_pct") is not None:
            bull.append(f"PE {pe:.1f}（行业{val_context['pe_pct']:.0%}分位，相对便宜）")
        else:
            bull.append(f"PE {pe:.1f}" + (f"、PB {pb:.2f}" if pb is not None and pb > 0 else ""))
    elif pb is not None and pb > 0:
        bull.append(f"PB {pb:.2f}")
    pos_pct = _pct(position)
    if pos_pct is not None:
        low_txt = f"处250日区间 {pos_pct:.0f}% 低位"
        if timing.get("days_since_low") is not None:
            low_txt += f"（距阶段低点约 {int(timing['days_since_low'])} 个交易日）"
        bull.append(low_txt)
    if forward_verified and forward_ni_yoy is not None and forward_ni_yoy >= 0:
        bull.append(f"{forward_stat_date or '最新报告期'} 净利同比 {forward_ni_yoy:+.0f}%（当年成长未恶化）")
    if timing.get("side") in ("right", "right_early"):
        bull.append(timing.get("label", ""))

    # ---- 主要风险（为什么可能是陷阱/接飞刀）----
    bear: list[str] = []
    if timing.get("side") == "falling":
        bear.append("MACD深度走弱、止跌未确认，属左侧接飞刀（择时风险高，需等右侧信号）")
    elif timing.get("below_ma20"):
        bear.append("现价仍在MA20下方，趋势未转强，左侧布局需自行择时")
    warn_lo = float(getattr(config, "FORWARD_NI_YOY_MIN", -30.0))
    warn_hi = float(getattr(config, "FORWARD_NI_YOY_WARN", -10.0))
    if forward_verified and forward_ni_yoy is not None and warn_lo <= forward_ni_yoy < warn_hi:
        bear.append(f"{forward_stat_date or '最新报告期'} 净利同比 {forward_ni_yoy:+.0f}%，业绩下滑预警")
    if quality_status == "financial_review":
        bear.append("金融企业，不适用非财务财报规则，待行业专项核验")
    elif quality_status not in ("verified",):
        bear.append("多年质量未完全核验（证据不足）")
    miss_zh = _missing_tags_zh(",".join(dict.fromkeys(t for t in missing if t)))
    if miss_zh:
        bear.append(f"数据缺项：{miss_zh}")
    if not bear:
        bear.append("暂无显著风险标记，仍需人工复核行业景气度与个股基本面")

    # ---- 失效价（跌破即认错离场）----
    inv_parts: list[str] = []
    sl = _num_or_none(stop_loss)
    if sl is not None and close > 0:
        inv_parts.append(f"止损位 {sl:.2f}（约 {(sl / close - 1) * 100:.1f}%）跌破认错离场")
    rl = _num_or_none(timing.get("recent_low"))
    if rl is not None:
        inv_parts.append(f"有效跌破近{config.LOW_POSITION_LOOKBACK}日低 {rl:.2f} 视为低位逻辑失效")
    invalidation = "；".join(inv_parts) if inv_parts else "（无止损参考，需人工设定）"

    # ---- 信心分档（可解释的加分制，纯展示）----
    pts = 0
    if quality_status == "verified":
        pts += 1
    if not missing:
        pts += 1
    if timing.get("side") in ("right", "right_early"):
        pts += 1
    if forward_verified and forward_ni_yoy is not None and forward_ni_yoy >= warn_hi:
        pts += 1
    conviction = {4: "高", 3: "中高", 2: "中", 1: "中低", 0: "低（观察）"}[pts]
    if timing.get("side") == "falling":
        conviction += "·左侧未止跌"

    # ---- 今日为何触发（变化点）----
    if hits:
        why_today = "技术触发：" + "、".join(hits)
    elif timing.get("side") in ("right", "right_early"):
        why_today = "凭基本面+低估值+低位入选，且已现右侧企稳（站上均线），非纯左侧接飞刀"
    elif timing.get("side") == "falling":
        why_today = "凭基本面+低估值+低位入选，但MACD仍走弱、未见止跌，属左侧接飞刀（建议等右侧确认）"
    else:
        why_today = "无明显技术触发，凭基本面+低估值+低位入选（左侧布局，需自行择时）"

    return {
        "bull_case": "；".join(x for x in bull if x),
        "bear_case": "；".join(x for x in bear if x),
        "invalidation": invalidation,
        "conviction": conviction,
        "why_today": why_today,
    }


def evaluate_quality_value(daily_df: Optional[pd.DataFrame], code: str, name: str,
                           config: StrategyConfig, market_env: Optional[dict] = None,
                           fund_data: Optional[dict] = None,
                           latest_trade_date: Optional[str] = None,
                           val_context: Optional[dict] = None) -> tuple[Optional[Signal], str]:
    """统一荐股资格：多年质量 + 低估值 + 250日低位 + 当年成长未恶化；技术面不作否决。

    val_context：行业相对估值上下文（见 _industry_valuation_context）。None 时估值闸门
    回退到绝对阈值（MAX_PE_TTM / MAX_PB_MRQ），与改动前口径一致。
    """
    from src.fundamental_quality import evaluate_annual_quality
    if daily_df is None or daily_df.empty:
        return None, "FAIL_DATA"
    df = daily_df.copy()
    required = {"date", "open", "high", "low", "close", "volume", "amount"}
    if not required.issubset(df.columns):
        return None, "FAIL_DATA"
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime(_DATE_FMT)
    if df["date"].isna().any() or df["date"].duplicated().any():
        return None, "FAIL_DATA"
    df = df.sort_values("date").reset_index(drop=True)
    now = _beijing_now()
    if now.hour < 15 and df.iloc[-1]["date"] == str(now.date()):
        df = df.iloc[:-1].reset_index(drop=True)
    if df.empty:
        return None, "FAIL_DATA"
    day = str(df.iloc[-1]["date"])
    if latest_trade_date is not None and day != str(latest_trade_date)[:10]:
        return None, "FAIL_STALE"
    # 零量停牌占位不计为有效交易日；末日无成交不推荐。
    for col in required - {"date"}:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if not np.isfinite(df.iloc[-1][["open", "high", "low", "close", "volume", "amount"]].to_numpy(dtype=float)).all():
        return None, "FAIL_DATA"
    if df.iloc[-1]["volume"] <= 0 or df.iloc[-1]["amount"] <= 0:
        return None, "FAIL_STALE"
    df = df[(df["volume"] > 0) & (df["amount"] > 0)].reset_index(drop=True)
    n = config.LOW_POSITION_LOOKBACK
    if len(df) < max(n, config.MIN_DAYS):
        return None, "FAIL_DATA"
    window = df.tail(n)
    if not np.isfinite(window[["open", "high", "low", "close", "volume", "amount"]].to_numpy(dtype=float)).all():
        return None, "FAIL_DATA"
    if (window[["open", "high", "low", "close"]] <= 0).any().any():
        return None, "FAIL_DATA"
    if ((window["high"] < window[["open", "close", "low"]].max(axis=1)) |
            (window["low"] > window[["open", "close", "high"]].min(axis=1))).any():
        return None, "FAIL_DATA"
    if float(df["amount"].tail(20).mean()) < config.MIN_AMOUNT:
        return None, "FAIL_LIQUIDITY"
    last = df.iloc[-1]
    close = float(last["close"])
    lo, hi = float(window["low"].min()), float(window["high"].max())
    if hi <= lo:
        return None, "FAIL_DATA"
    position = (close - lo) / (hi - lo)
    if position > config.LOW_POSITION_MAX:
        return None, "FAIL_POSITION"
    missing = [] if latest_trade_date is not None else ["market_date"]
    def finite(value):
        try:
            v = float(value)
            return v if np.isfinite(v) else None
        except (ValueError, TypeError):
            return None
    pe, pb = finite(last.get("peTTM")), finite(last.get("pbMRQ"))
    # 估值闸门（A：低估值）。两种口径：
    #   - 行业相对（val_context 非空）：个股 PE/PB 在行业内分位 ≤ VALUATION_INDUSTRY_PERCENTILE_MAX
    #     即算便宜（认可「高估值行业里的相对便宜票」，避免绝对阈值误杀成长/科技/医药）；
    #   - 绝对回退（val_context 为空，或该指标行业内样本不足）：PE ≤ MAX_PE_TTM、PB ≤ MAX_PB_MRQ。
    # 两种口径下 peTTM/pbMRQ ≤ 0（亏损/净资产异常）一律否决；缺值记缺项降级为待核验。
    industry_mode = bool(val_context and val_context.get("mode") == "industry")
    pct_cap = float(getattr(config, "VALUATION_INDUSTRY_PERCENTILE_MAX", 0.60))
    for value, cap, pct_key, tag in (
            (pe, config.MAX_PE_TTM, "pe_pct", "valuation_pe"),
            (pb, config.MAX_PB_MRQ, "pb_pct", "valuation_pb")):
        if value is None:
            missing.append(tag)
            continue
        if value <= 0:
            return None, "FAIL_VALUATION"
        pct = val_context.get(pct_key) if industry_mode else None
        if pct is not None:
            if pct > pct_cap:
                return None, "FAIL_VALUATION"
        elif value > cap:
            return None, "FAIL_VALUATION"
    fund = fund_data or {}
    debt = finite(fund.get("debt_ratio"))
    financial = _is_financial_stock(code, name, config)
    debt_limit = config.FINANCE_MAX_DEBT_RATIO if financial else config.MAX_DEBT_RATIO
    if debt is None:
        missing.append("fund_debt")
    elif debt < 0 or debt > debt_limit:
        return None, "FAIL_FUND"
    goodwill = finite(fund.get("goodwill_ratio"))
    if goodwill is not None and goodwill > config.MAX_GOODWILL_RATIO:
        return None, "FAIL_FUND"
    quality = evaluate_annual_quality(
        fund.get("annual_rows", []), day, years=config.QUALITY_YEARS,
        median_roe_min=config.QUALITY_MEDIAN_ROE_MIN, min_roe=config.QUALITY_MIN_ROE,
        cash_conversion_min=config.QUALITY_CASH_CONVERSION_MIN, financial=financial)
    if quality["status"] == "failed":
        return None, "FAIL_FUND"
    missing.extend(quality.get("missing_tags", []))
    if quality["status"] != "verified" and not quality.get("missing_tags"):
        missing.append("fund_annual")
    # 最近应披露季度的同比核验；缺失、报告期过旧或晚于决策日均不算已确认。
    forward_verified = False
    if getattr(config, "REQUIRE_FORWARD_CONFIRMATION", False) and not financial:
        ni_yoy = finite(fund.get("forward_ni_yoy"))
        period = str(fund.get("forward_stat_date") or "")
        decision = datetime.strptime(day, _DATE_FMT)
        expected_year, expected_quarter = _quarter_candidates(decision, n=1)[0]
        # 1–4月年报尚未全部披露，允许上一年三季报；已披露年报也有效。
        if decision.month < 5:
            expected_quarter = 3
        try:
            year_text, quarter_text = period.split("Q")
            year, quarter = int(year_text), int(quarter_text)
            period_end = pd.Timestamp(year=year, month=quarter * 3, day=1) + pd.offsets.MonthEnd(0)
            forward_verified = (1 <= quarter <= 4 and
                (year, quarter) >= (expected_year, expected_quarter) and
                period_end.date() <= decision.date() and ni_yoy is not None)
        except (ValueError, TypeError, OverflowError):
            forward_verified = False
        if forward_verified:
            if ni_yoy < float(config.FORWARD_NI_YOY_MIN):
                return None, "FAIL_FORWARD"
        elif getattr(config, "FORWARD_MISSING_AS_PENDING", True):
            missing.append("forward")
    # 技术缺项只影响标签/分数，绝不覆盖财务和估值的待核验状态。
    df = _recompute_pct_chg(df)
    technical = compute_daily_signals(df, config)
    if technical is None:
        return None, "FAIL_DATA"
    d = technical.iloc[-1]
    tech_score = float(d["daily_score"])
    # KDJ / MACD 下限否决（默认关闭）：quality_value 模式的原有设计是「技术面不作
    # 否决」，此处只在显式开启 QV_ENFORCE_KDJ_MACD_VETO 时收紧，且只拦「动能连续
    # 走弱 / KDJ 高位滞涨」，不改变质量、估值、低位三大闸门的口径。
    if getattr(config, "QV_ENFORCE_KDJ_MACD_VETO", False):
        if not macd_not_deeply_weak(technical, config.MACD_WEAK_DAYS,
                                    config.MACD_WEAK_HIST_PCT):
            return None, "FAIL_MACD_WEAK"
        if not kdj_not_overheated(technical, config.KDJ_K_HARD_MAX, config.KDJ_DEAD_CROSS_K):
            return None, "FAIL_KDJ_HIGH"
    quality_score = float(quality.get("quality_score", 0))
    # 每个指标的评分与其准入口径一致；样本不足的单项仍用绝对估值。
    valuation_components = []
    for value, cap, key in ((pe, config.MAX_PE_TTM, "pe_pct"),
                            (pb, config.MAX_PB_MRQ, "pb_pct")):
        pct = val_context.get(key) if industry_mode else None
        component = 100 * (1 - pct) if pct is not None else (
            100 * (1 - value / cap) if value is not None else 0.0)
        valuation_components.append(min(100.0, max(0.0, component)))
    valuation_score = sum(valuation_components) / 2 if pe is not None and pb is not None else 0.0
    score = round(config.QUALITY_SCORE_WEIGHT * quality_score +
                  config.VALUATION_SCORE_WEIGHT * valuation_score +
                  config.TECHNICAL_SCORE_WEIGHT * tech_score, 2)
    # 综合分下限（#3：宁缺毋滥）。低于 MIN_QV_SCORE 的候选不推荐——弱市自然收敛到少推/不推，
    # 不再「只要有票过硬闸门就凑满 MAX_PICKS」。设 0 关闭该闸门。
    # 仅对「将要成为正式推荐」（missing 为空）的候选生效：待核验候选的估值分因数据缺失被
    # 记为 0、综合分被人为压低，对其套下限没有意义（且 pending 本就不进正式推荐）。
    min_qv = float(getattr(config, "MIN_QV_SCORE", 0.0) or 0.0)
    if min_qv > 0 and not missing and score < min_qv:
        return None, "FAIL_QV_SCORE"
    tags = ["优质低估低位" if not missing else "低位候选待核验"]
    hits = _signal_hits(d)
    tags.append("动能改善" if hits else "趋势待确认")
    tags.extend(hits)
    # 两个入口共享同一标签；避免第二入口去重后丢失突破提示。
    from src.volume_breakout_strategy import VolumeBreakoutConfig, evaluate_breakout
    breakout_config = VolumeBreakoutConfig(**{**asdict(config), "RECOMMENDATION_MODE": "technical"})
    breakout, _ = evaluate_breakout(df, code, name, breakout_config, market_env, None)
    if breakout is not None:
        tags.append("放量突破")
    # 入选依据补充：行业相对估值分位（#4a）与当年成长（#4b），便于人工裁量时一眼看清
    # 「为什么算便宜」「当年是否还在恶化」。仅在对应数据可得时展示，缺失不伪造。
    if industry_mode and val_context:
        _pp = []
        if val_context.get("pe_pct") is not None:
            _pp.append(f"PE{val_context['pe_pct']:.0%}")
        if val_context.get("pb_pct") is not None:
            _pp.append(f"PB{val_context['pb_pct']:.0%}")
        if _pp:
            tags.append("行业估值分位" + "/".join(_pp))
    _ni = finite(fund.get("forward_ni_yoy"))
    if _ni is not None:
        label = "净利同比" if forward_verified else "历史净利同比（未确认）"
        tags.append(f"{fund.get('forward_stat_date') or '报告期未知'} {label}{_ni:+.0f}%")
        # P0 业绩下滑黄标：未触发硬否决（≥FORWARD_NI_YOY_MIN）但已明显走弱（<WARN）时显式预警。
        _warn_lo = float(getattr(config, "FORWARD_NI_YOY_MIN", -30.0))
        _warn_hi = float(getattr(config, "FORWARD_NI_YOY_WARN", -10.0))
        if forward_verified and _warn_hi > _warn_lo and _warn_lo <= _ni < _warn_hi:
            tags.append("业绩下滑预警")
    elif getattr(config, "REQUIRE_FORWARD_CONFIRMATION", False):
        tags.append("近期业绩待核验")
    atr = finite(d.get("atr"))
    rr = compute_risk_reward(close, config, atr)
    # ===== P0：入场时机判读（左侧/右侧 + 是否仍在下跌）=====
    # 始终计算（P1 熊市闸门与 P5 简报都要用）；SURFACE_TIMING_READ 只控制是否加标签/出简报。
    timing = assess_entry_timing(technical, config)
    # ===== P1：熊市止跌闸门（QV_BEAR_TIMING_GATE，库级默认开启＝生产口径）=====
    # 仅在熊市（含 unknown 折叠为 bear）生效：formal 必须有最低止跌证据，否则否决，
    # 避免突破策略空仓时组合变成"纯左侧接飞刀"。牛市/中性口径完全不变。
    # 该开关此前库级为 False、仅 run.py 覆盖为 True（库级口径 ≠ 生产口径），现收进默认值。
    if getattr(config, "QV_BEAR_TIMING_GATE", False):
        _eff_regime = _effective_regime((market_env or {}).get("regime", "unknown"), config)
        if _eff_regime == "bear":
            _stabilized = (timing.get("below_ma20") is False) or _macd_momentum_ok(technical, config)
            if not _stabilized:
                return None, "FAIL_BEAR_TIMING"
    surface = bool(getattr(config, "SURFACE_TIMING_READ", True))
    if surface:
        tags.append(timing["label"])
    # ===== P5：决策简报（看多/风险/失效价/信心/今日触发）；纯展示，不落库、不参与否决 =====
    brief = (build_decision_brief(
        timing=timing, quality=quality, pe=pe, pb=pb, val_context=val_context,
        industry_mode=industry_mode, position=position, close=close, stop_loss=rr["stop_loss"],
        forward_ni_yoy=_ni, forward_verified=forward_verified,
        forward_stat_date=fund.get("forward_stat_date"), missing=missing, hits=hits,
        quality_status=quality["status"], config=config)
        if surface else {})
    return Signal(
        code=code, name=name, date=day, close=round(close, 2), score=score,
        grade=_grade_from_score(score, config), daily_score=tech_score,
        rsi=finite(d.get("rsi14")), rsi7=finite(d.get("rsi7")), rsi21=finite(d.get("rsi21")),
        vol_ratio=finite(d.get("daily_vol_ratio")), turnover_ratio=finite(d.get("daily_turnover_ratio")),
        stop_loss=rr["stop_loss"], take_profit=rr["take_profit"], rr_ratio=rr["rr_ratio"],
        market_env=(market_env or {}).get("regime", "unknown"),
        has_divergence=bool(d.get("bottom_divergence", False)), signals_hit=",".join(tags),
        fund_status="verified" if quality["status"] == "verified" and debt is not None else "partial",
        weekly_status="not_required", missing_tags=",".join(dict.fromkeys(missing)),
        tier="pending" if missing else "formal", rank_score=score,
        quality_score=quality_score, valuation_score=round(valuation_score, 2),
        position_250=round(position, 4), pe_ttm=pe, pb_mrq=pb,
        quality_status=quality["status"], **brief), "PASS"


def _screen_quality_pool(config: StrategyConfig, cache: CacheManager, market_env: dict,
                         latest_trade_date: Optional[str], pending_out: Optional[list] = None,
                         evaluator=None, max_picks: Optional[int] = None) -> Optional[pd.DataFrame]:
    """先廉价行情筛选、后年度财务核验；核验完所有候选才排序和行业限额。

    max_picks：本次推荐数量上限（由 main 按市场环境 resolve_max_picks 解析后传入）；
    None 时退回 config.MAX_PICKS（兼容回测/单测直接调用）。
    行业相对估值开启时，先用全池日线构建「行业 PE/PB 横截面快照」（复用缓存，不额外取数），
    再据此为每只候选计算行业内分位上下文 val_context 传入评估器；快照不可用时自动回退绝对阈值。
    """
    evaluator = evaluator or evaluate
    stocks = get_stock_list(config, cache)
    cap = int(config.MAX_PICKS if max_picks is None else max_picks)
    # 行业估值横截面快照（#4a）：行业数据不可用 → 空 dict → 全市场回退绝对阈值
    industry_map = get_stock_industry(config, cache) if getattr(config, "USE_INDUSTRY_RELATIVE_VALUATION", False) else {}
    snapshot = build_industry_valuation_snapshot(config, cache, stocks) if industry_map else {}
    # ===== 估值口径显式声明 =====
    # 回退到绝对阈值是合法降级，但**不能静默**：绝对口径（PE≤25/PB≤3）正是本项目刻意避开的
    # 「名单压向银行/地产/周期」口径，且叠加综合分下限后回退日 formal 极易归零。
    # 此前只有快照构建抛异常时才有 warning，行业接口空返回/无行业归属都是无声的。
    if not getattr(config, "USE_INDUSTRY_RELATIVE_VALUATION", False):
        logger.info("估值口径：**绝对阈值**（USE_INDUSTRY_RELATIVE_VALUATION=False，配置显式关闭）")
    elif not industry_map:
        logger.warning("估值口径：**绝对阈值**（行业分类不可用——query_stock_industry 无返回，"
                       "行业相对估值整体未生效；名单可能偏向低 PE/PB 的银行/地产/周期）")
    elif not snapshot:
        logger.warning("估值口径：**绝对阈值**（行业估值快照为空，行业相对估值整体未生效）")
    else:
        logger.info("估值口径：行业相对分位（快照覆盖 %d 个行业）", len(snapshot))
    # 综合分下限的真实严厉度：与三道硬闸门等效门槛一起打到日志，避免把「被下限卡掉」
    # 误读为「市场没机会」（等效门槛的算术已固化为 test_recommendation_upgrades 的断言）。
    if float(getattr(config, "MIN_QV_SCORE", 0.0) or 0.0) > 0:
        logger.info("综合分下限等效门槛：%s", describe_qv_floor(config))

    def screen(stock):
        code, name = stock["code"], stock["name"]
        daily = get_daily_data(code, config, cache)
        # 行业相对估值上下文：个股 PE/PB 在其行业内的分位（无快照/样本不足 → None → 绝对回退）
        val_ctx = None
        if snapshot:
            _pe = _finite_positive_or_none(daily.iloc[-1].get("peTTM")) if daily is not None and not daily.empty else None
            _pb = _finite_positive_or_none(daily.iloc[-1].get("pbMRQ")) if daily is not None and not daily.empty else None
            val_ctx = _industry_valuation_context(snapshot, industry_map.get(str(code), ""), _pe, _pb, config)
        pre, reason = evaluator(daily, code, name, config, market_env, None,
                                latest_trade_date=latest_trade_date, val_context=val_ctx)
        if pre is None:
            return None, reason
        fund = get_fundamentals(code, cache, config)
        if not _is_financial_stock(code, name, config):
            fund = enrich_annual_fundamentals(code, fund, config, as_of=pre.date)
            fund = enrich_forward_growth(code, fund, config, cache)  # 前瞻确认（#4b）
        return evaluator(daily, code, name, config, market_env, fund,
                         latest_trade_date=latest_trade_date, val_context=val_ctx)
    results, processed, timed_out = run_concurrent_screen(stocks, screen, config, logger)
    rows = [sig.to_dict() for sig, reason in results if sig is not None and reason == "PASS"]
    # 否决归因计数：让「今天为什么没推荐」在 quality_value 路径也可观测（此前只印候选数）
    from collections import Counter
    reasons = Counter(reason for sig, reason in results if sig is None)
    if reasons:
        top = "，".join(f"{k} {v}" for k, v in reasons.most_common(6))
        logger.info("优质低估低位否决归因（前6）：%s", top)
    # ===== P6b：完整闸门归因（含 0 计数），把"从不触发的死闸门"暴露在每次运行日志里 =====
    # 目的：闸门瘦身应由数据驱动（哪些闸门长期 0 命中＝对区分候选无贡献），而非凭直觉删。
    # 长期为 0 的闸门即审计对象——要么阈值被前置闸门架空（死权重），要么本就极少生效。
    full = "，".join(f"{c}={reasons.get(c, 0)}" for c in _QV_REASON_CODES)
    logger.info("优质低估低位闸门归因（全量）：%s", full)
    never = [c for c in _QV_REASON_CODES if reasons.get(c, 0) == 0]
    if never and processed:
        logger.info("本次从未触发的闸门（瘦身审计对象，长期为 0 需复核是否被前置条件架空）：%s",
                    "，".join(never))
    logger.info("优质低估低位筛选：已处理 %d/%d，候选 %d，时间预算耗尽=%s", processed, len(stocks), len(rows), timed_out)
    if not rows:
        return None
    frame = _rank_signals(pd.DataFrame(rows))
    pending = frame[frame["tier"] != "formal"]
    if pending_out is not None:
        pending_out.extend(pending.to_dict("records"))
    formal = frame[frame["tier"] == "formal"].copy()
    if config.USE_INDUSTRY_DEDUP and not formal.empty:
        formal = _dedup_by_industry(formal, config, cache)
    # 估值口径逐行标注：写入 valuation_mode 列，供日志汇总与飞书卡片声明本次实际口径
    if getattr(config, "USE_INDUSTRY_RELATIVE_VALUATION", False) and not formal.empty:
        formal = _tag_valuation_mode(formal, snapshot, industry_map, config)
    if len(formal) > cap:
        logger.info("通过 formal %d 只，按综合分截取前 %d 只（市场环境上限）", len(formal), cap)
    return formal.head(cap).reset_index(drop=True)


def get_market_environment(config: StrategyConfig, cache: CacheManager) -> dict:
    if (cached := cache.get("market_env")) is not None: return cached
    df_index = get_index_daily(config, cache)
    result = compute_market_environment(df_index, config) if df_index is not None and not df_index.empty else {"regime": "unknown", "description": "未知（数据获取失败）", "ma20": 0, "slope": 0, "close": 0}
    result = _apply_regime_hysteresis(result, config)
    cache.set("market_env", result)
    return result

def _dedup_by_industry(df: pd.DataFrame, config: StrategyConfig, cache: Optional[CacheManager] = None) -> pd.DataFrame:
    """行业分散：同一行业最多保留 MAX_PICKS_PER_INDUSTRY 只，保持传入顺序（评分降序）。
    行业数据缺失时原样返回，不做处理。"""
    industry_map = get_stock_industry(config, cache)
    if not industry_map or df.empty: return df
    kept, used = [], {}
    for _, row in df.iterrows():
        industry = industry_map.get(str(row["code"]), "") or ""
        if industry and used.get(industry, 0) >= config.MAX_PICKS_PER_INDUSTRY:
            continue
        if industry: used[industry] = used.get(industry, 0) + 1
        kept.append(row)
    dropped = len(df) - len(kept)
    if dropped:
        logger.info("行业分散：淘汰 %d 只（同一行业最多 %d 只）", dropped, config.MAX_PICKS_PER_INDUSTRY)
    return pd.DataFrame(kept).reset_index(drop=True) if kept else df.iloc[0:0]

# ===========================================================================
# 编排函数
# ===========================================================================

def run_concurrent_screen(
    stock_list: list[dict],
    screen_one: Callable[[dict], tuple],
    config: StrategyConfig,
    log: logging.Logger,
) -> tuple[list[tuple], int, bool]:
    """并发筛选骨架（优质低估低位/放量突破两策略共用的漏斗模板）：线程池 + 心跳看门狗 + 时间预算。

    - 看门狗：筛选期每 WATCHDOG_TICK_SEC（默认 60）秒心跳一次；连续 4 分钟无任务完成
      则升级为 warning 并打印在途股票代码定位卡点（数据源假死排查手段，bs_lock 持有
      期间卡死曾致全池阻塞）。tick-first：首轮心跳在启动后 60s 即触发，短跑（<60s 完成）
      仍无心跳——此时"开始并发筛选"与"进度: N/N (100%)"已足够 bracket 整段。
    - 时间预算：超过 SCREEN_TIME_BUDGET_MIN 取消未完成任务，按已完成结果出报告，
      漏斗统计按 processed 计（预算截断时不失真）。
    - 进度日志：每 PROGRESS_LOG_EVERY 只打印一次进度与 ETA。

    返回 (results, processed, time_budget_hit)：results 为已完成任务的
    (sig, reason) 列表（完成顺序，调用方自行做确定性排序）。
    """
    results: list[tuple] = []
    processed, total = 0, len(stock_list)
    screen_start = time.time()
    budget_sec = float(getattr(config, "SCREEN_TIME_BUDGET_MIN", 240.0)) * 60
    progress_every = int(getattr(config, "PROGRESS_LOG_EVERY", 500))
    watchdog_tick = float(getattr(config, "WATCHDOG_TICK_SEC", 60.0))
    watchdog_idle_warn = float(getattr(config, "WATCHDOG_IDLE_WARN_SEC", 240.0))
    progress = {"done": 0, "t": screen_start}
    stop_watch = threading.Event()

    pool = ThreadPoolExecutor(max_workers=config.MAX_WORKERS)
    futures = {pool.submit(screen_one, s): s for s in stock_list}

    def _watchdog() -> None:
        # tick-first：wait(N) 先睡后判，所以首轮心跳在启动后 N 秒触发。
        # N 从 180 降到 60 是为了让"短跑无心跳"的窗口从 3min 缩到 1min；
        # 长跑场景下 60s 一跳的日志密度（60 行/小时）在 CI 里仍可接受。
        while not stop_watch.wait(watchdog_tick):
            idle = time.time() - progress["t"]
            if idle >= watchdog_idle_warn:
                hanging = [futures[f]["code"] for f in futures if not f.done()][:8]
                log.warning("已 %d 秒无任务完成，疑似数据源卡住：%d/%d 完成，在途代码: %s",
                            int(idle), progress["done"], total, ",".join(hanging) or "-")
            else:
                log.info("心跳：%d/%d 完成，已运行 %.1f 分钟",
                         progress["done"], total, (time.time() - screen_start) / 60)

    log.info("开始并发筛选：%d 只股票，%d 线程，时间预算 %.0f 分钟",
             total, config.MAX_WORKERS, budget_sec / 60)
    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()
    time_budget_hit = False
    try:
        for future in as_completed(futures):
            processed += 1
            progress["done"] = processed
            progress["t"] = time.time()
            results.append(future.result())
            if processed % progress_every == 0 or processed == total:
                elapsed = time.time() - screen_start
                rate = processed / elapsed if elapsed > 0 else 0
                eta_min = (total - processed) / rate / 60 if rate > 0 else 0
                log.info("进度: %d/%d (%.0f%%)，已用 %.1f 分钟，预计剩余 %.1f 分钟",
                         processed, total, processed / total * 100, elapsed / 60, eta_min)
            if time.time() - screen_start > budget_sec:
                time_budget_hit = True
                remaining = sum(1 for f in futures if not f.done())
                log.warning("已达筛选时间预算 %.0f 分钟，取消剩余 %d 只未完成任务，基于已完成 %d/%d 只出结果",
                            budget_sec / 60, remaining, processed, total)
                break
    finally:
        stop_watch.set()
        pool.shutdown(wait=True, cancel_futures=True)
    return results, processed, time_budget_hit


def main(config: Optional[StrategyConfig] = None, cache: Optional[CacheManager] = None,
         pending_out: Optional[list] = None, volatile_out: Optional[list] = None) -> Optional[pd.DataFrame]:
    """执行选股主流程，返回正式推荐（formal）DataFrame。

    pending_out：可选 list，传出「待核验候选」（财务/周线数据缺失、不与正式推荐混排），
    由调用方决定是否展示——数据缺失不等于筛选通过，宁可少荐。
    volatile_out：可选 list，传出「波动率风控否决」明细（技术面已达标、仅 ATR 超限），
    仅用于日志与飞书高风险观察池，不落库、不参与追踪与归因。
    """
    if config is None: config = StrategyConfig()
    if cache is None: cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)

    fetch_stats["bs_ok"] = fetch_stats["ak_ok"] = fetch_stats["fail"] = 0

    # ==========================
    # 初始化数据源：Baostock 优先，登录失败降级 AkShare
    # ==========================
    if not _bs_login(max_retry=5):
        if _AK_AVAILABLE:
            logger.warning("Baostock 登录失败（已重试 5 次），本次运行降级为 AkShare 备用数据源")
            _bs_state["circuit_open"] = True
        else:
            logger.error("Baostock 登录失败，且未安装 AkShare（pip install akshare），无可用数据源")
            return None
    else:
        logger.info("数据源: Baostock（备用: %s）", "AkShare" if _AK_AVAILABLE else "未安装 akshare，无备用")

    try:
        market_env = get_market_environment(config, cache)
        logger.info("市场环境: %s", market_env.get("description", "unknown"))

        # 数据时效基准：市场（沪深300）最新交易日，个股日线/周线截止日期均须与之一致
        index_df = get_index_daily(config, cache)
        latest_trade_date = str(index_df["date"].iloc[-1]) \
            if index_df is not None and not index_df.empty and "date" in index_df.columns else None
        if latest_trade_date is None:
            logger.warning("无法获取指数行情，本次运行跳过行情时效校验（停牌股可能混入待核验流程）")

        # ===== 市场级熔断（#2）：指数急跌期间全市场抄底信号批量触发，直接不推荐 =====
        # 置于 quality_value 分支之前，使两种模式都受组合层风控保护——此前 quality_value
        # 在分支处提前 return，crash-halt 与 regime 数量收缩对其完全不生效（急跌中反而出票更多）。
        halt_ret = market_crash_halt(index_df, config)
        if halt_ret is not None:
            logger.warning("市场级熔断：沪深300 近 %d 个交易日累计 %.2f%%（阈值 %.2f%%），"
                           "本次运行不推荐（系统性下跌期间个股级过滤不足以防护）",
                           config.MARKET_CRASH_LOOKBACK, halt_ret, config.MARKET_CRASH_HALT_PCT)
            return None

        # ===== 推荐数量上限按市场环境收缩（#2，quality_value 与 technical 两种模式共用）=====
        regime_raw = market_env.get("regime", "unknown")
        regime_eff = _effective_regime(regime_raw, config)
        max_picks = resolve_max_picks(regime_raw, config)
        if regime_eff != regime_raw:
            logger.warning("市场环境 unknown（指数数据缺失），按熊市保守处理：推荐上限收缩为 %d", max_picks)
        logger.info("市场环境 %s：推荐数量上限 %d（牛 %d / 中性 %d / 熊 %d；熊市准入分数线 +%g）",
                    regime_eff, max_picks, config.MAX_PICKS, config.NEUTRAL_MAX_PICKS,
                    config.BEAR_MAX_PICKS, config.BEAR_GRADE_BOOST)
        if max_picks <= 0:
            logger.info("当前市场环境推荐上限为 0，跳过全市场筛选")
            return None

        if config.RECOMMENDATION_MODE == "quality_value":
            return _screen_quality_pool(config, cache, market_env, latest_trade_date, pending_out,
                                        max_picks=max_picks)

        stock_list = get_stock_list(config, cache)
        if not stock_list:
            logger.warning("无法获取股票列表")
            return None
        logger.info("待筛选股票数: %d", len(stock_list))

        signals: list[dict] = []
        stats = {"total": len(stock_list), "error": 0, "fail_data": 0, "fail_liq": 0,
                 "fail_stale": 0, "fail_halt": 0, "fail_fund": 0, "fail_tech": 0,
                 "fail_volatile": 0, "pass": 0}

        def _screen_one(stock: dict) -> tuple[Optional[Signal], str]:
            code, name = stock["code"], stock["name"]
            try:
                daily_df = get_daily_data(code, config, cache)
                if daily_df is None: return None, "FAIL_DATA"
                fund_data = get_fundamentals(code, cache, config)
                return evaluate(daily_df, code, name, config, market_env, fund_data,
                                latest_trade_date=latest_trade_date, volatile_out=volatile_out)
            except Exception as e:
                logger.debug("%s(%s) 筛选异常: %s", name, code, e)
                return None, "ERROR"

        # 并发筛选（共享骨架：线程池 + 心跳看门狗 + 时间预算；bs_lock 保证 Baostock 查询不互踩）
        screen_start = time.time()
        results, processed, time_budget_hit = run_concurrent_screen(stock_list, _screen_one, config, logger)
        screen_minutes = (time.time() - screen_start) / 60

        for sig, reason in results:
            if reason == "PASS" and sig is not None:
                stats["pass"] += 1
                signals.append(sig.to_dict())
            elif reason == "FAIL_FUND": stats["fail_fund"] += 1
            # FAIL_VALUATION（估值闸门）位于基本面层与技术层之间，计入 fail_fund 桶，
            # 否则漏斗的逐层相减链（pass_fund = pass_liq - fail_fund …）会因未计数而失真。
            elif reason == "FAIL_VALUATION": stats["fail_fund"] += 1
            elif reason == "FAIL_DATA": stats["fail_data"] += 1
            elif reason == "FAIL_LIQUIDITY": stats["fail_liq"] += 1
            elif reason == "FAIL_STALE": stats["fail_stale"] += 1
            elif reason == "FAIL_HALT_GAP": stats["fail_halt"] += 1
            elif reason == "FAIL_VOLATILE": stats["fail_volatile"] += 1
            # FAIL_CHASE（追高）/ FAIL_GAP（跳空）/ FAIL_RSI_HIGH（RSI过高）/ FAIL_CLIMAX_VOL（天量）/ FAIL_NOT_BOTTOM（非底部区域）
            # / FAIL_POSITION（区间位置偏高）/ FAIL_MACD_MOM（动能未改善）/ FAIL_KDJ（KDJ未金叉）均属技术面入场质量层
            elif reason in ("FAIL_TECH", "FAIL_CHASE", "FAIL_GAP", "FAIL_RSI_HIGH", "FAIL_CLIMAX_VOL",
                            "FAIL_NOT_BOTTOM", "FAIL_DEEP_CRASH", "FAIL_RECENT_RALLY",
                            "FAIL_POSITION", "FAIL_MACD_MOM", "FAIL_KDJ"): stats["fail_tech"] += 1
            elif reason == "ERROR": stats["error"] += 1

        # 漏斗日志（按已处理数计，预算截断时不失真）
        pass_data = processed - stats["fail_data"] - stats["fail_stale"] - stats["fail_halt"] - stats["error"]
        pass_liq = pass_data - stats["fail_liq"]
        pass_fund = pass_liq - stats["fail_fund"]
        pass_tech = pass_fund - stats["fail_tech"]
        pass_vol = pass_tech - stats["fail_volatile"]

        logger.info("=" * 50)
        logger.info("[FUNNEL] 选股漏斗数据分析   筛选耗时 %.1f 分钟", screen_minutes)
        logger.info("=" * 50)
        if time_budget_hit:
            logger.warning("[WARN] 因达到时间预算，以下漏斗仅统计已完成的 %d/%d 只", processed, stats["total"])
        logger.info("1. 初始有效股票池: %d 只 (完成处理 %d 只)", stats["total"], processed)
        logger.info("2. 获取数据并达标: %d 只 (淘汰/缺失 %d 只)", pass_data, stats["fail_data"] + stats["error"])
        if stats["fail_stale"] > 0:
            logger.info("   其中行情时效不符（停牌/数据滞后）: %d 只，暂不推荐", stats["fail_stale"])
        if stats["fail_halt"] > 0:
            logger.info("   其中K线跨停牌缺口（滚动指标失真）: %d 只，暂不推荐", stats["fail_halt"])
        logger.info("3. 流动性达标: %d 只 (僵尸股淘汰 %d 只)", pass_liq, stats["fail_liq"])
        if pass_liq > 0: logger.info("4. 基本面防雷通过: %d 只 (淘汰 %d 只，通过率 %.1f%%)",
                                     pass_fund, stats["fail_fund"], pass_fund / pass_liq * 100)
        if pass_fund > 0: logger.info("5. 日线技术面达标: %d 只 (淘汰 %d 只，通过率 %.1f%%)",
                                      pass_tech, stats["fail_tech"], pass_tech / pass_fund * 100)
        if pass_tech > 0: logger.info("6. 波动率风控达标: %d 只 (淘汰 %d 只，通过率 %.1f%%)",
                                      pass_vol, stats["fail_volatile"], pass_vol / pass_tech * 100)
        if stats["fail_volatile"] > 0:
            logger.info("   其中 %d 只仅因波动率超限被否决（技术面已达准入线），明细见下方 [VOLATILE] 区块",
                        stats["fail_volatile"])
        logger.info("日线取数来源: Baostock %d 只，AkShare 兜底 %d 只，双源均失败 %d 只",
                    fetch_stats["bs_ok"], fetch_stats["ak_ok"], fetch_stats["fail"])
        logger.info("=" * 50)

        # 波动率风控否决明细：技术面已过准入分数线、仅 ATR 超限被拦的标的逐只留档。
        # 这是实盘中最常见的「今天为什么没有推荐」的原因，只印计数无法复核，
        # 故在此打印明细，并经 volatile_out 传给调用方推送飞书高风险观察池。
        if volatile_out:
            # 并发筛选的完成顺序不确定，先按「技术分降序 → ATR% 降序」就地定序：
            # 使日志与飞书卡片的 Top-N 稳定可复现（同一份数据两次运行给出同样名单）。
            volatile_out[:] = sort_volatile(volatile_out)
            log_volatile_rejects(volatile_out, logger)
        elif volatile_out is not None and not stats["fail_volatile"] and not stats["pass"]:
            # 波动率层零淘汰 + 日线层无通过标的：拦截发生在上游层。直接点出主要拦截层，
            # 避免「看了 [VOLATILE] 区块却什么都没有」的困惑（实测多数空仓日属此类）。
            _upstream = {
                "数据/时效/停牌缺口": stats["fail_data"] + stats["fail_stale"] + stats["fail_halt"] + stats["error"],
                "流动性不足": stats["fail_liq"],
                "基本面防雷": stats["fail_fund"],
                "日线技术面(含评分未达准入门槛)": stats["fail_tech"],
            }
            _layer = max(_upstream, key=lambda k: _upstream[k])
            if _upstream[_layer] > 0:
                logger.info("[VOLATILE] 波动率层本轮未淘汰任何标的（拦截发生在上游）："
                            "日线筛选无通过标的，主要拦截层为「%s」%d 只", _layer, _upstream[_layer])

        # 正式技术信号为空：ENABLE_DAILY_FALLBACK 开启时进入放宽技术确认的观察候选模式
        # （核心行情安全条件仍由 evaluate() 保留；仅降低评分/确认门槛，明确标记 fallback）；
        # 未开启（默认）则直接结束——必须先 return，否则空 signals 进入 _rank_signals
        # 会因缺少 score 列抛 KeyError（2026-09-15 线上事故：fallback 默认关闭后
        # 0 只通过的运行在排序处崩溃）。
        if not signals:
            if config.ENABLE_DAILY_FALLBACK:
                relaxed = StrategyConfig(**{**asdict(config),
                    "MIN_PASS_GRADE": "C",
                    "REQUIRE_MACD_MOMENTUM": False,
                    "REQUIRE_KDJ_GOLDEN": False,
                    "POSITION_IN_RANGE_MAX": max(config.POSITION_IN_RANGE_MAX, 0.60),
                    "DAILY_RSI_ENTRY_MAX": max(config.DAILY_RSI_ENTRY_MAX, 65.0),
                })
                fallback_candidates: list[dict] = []
                for stock in stock_list:
                    try:
                        daily_df = get_daily_data(stock["code"], config, cache)
                        if daily_df is None: continue
                        sig, reason = evaluate(daily_df, stock["code"], stock["name"], relaxed,
                                                market_env, get_fundamentals(stock["code"], cache, config),
                                                latest_trade_date=latest_trade_date)
                        if sig is not None:
                            row = sig.to_dict()
                            row["tier"] = "fallback"
                            row["fallback_reason"] = "严格技术筛选无结果，使用放宽确认条件的最高分候选"
                            fallback_candidates.append(row)
                    except Exception:
                        continue
                if fallback_candidates:
                    fallback_candidates.sort(key=lambda r: (-float(r.get("score", 0) or 0), str(r.get("code", ""))))
                    chosen = fallback_candidates[0]
                    logger.info("保底观察候选：%s(%s)，评分 %s，未计入正式推荐",
                                chosen.get("name"), chosen.get("code"), chosen.get("score"))
                    if pending_out is not None:
                        pending_out.extend(fallback_candidates[1:])
                    return pd.DataFrame([chosen])
            logger.info("未发现符合条件的信号")
            return None

        # 确定性排序：排序分降序 → 底背离优先 → 盈亏比 → 股票代码（末级键，结果可复现）
        df = _rank_signals(pd.DataFrame(signals))

        # 决赛圈：周线确认 + 财务字段补齐 + 行业分散，逐个按排序分降序处理，取满 max_picks 即止。
        # 周线 MA 与周线 MACD 开关独立生效（任一启用即拉周线）；
        # 周线数据缺失/截止过旧 → 不足以确认 → 进入待核验候选，不占正式推荐名额
        weekly_enabled = config.REQUIRE_WEEKLY_TREND or config.REQUIRE_WEEKLY_MACD_STABLE
        pending_list: list[dict] = []
        if weekly_enabled and not df.empty:
            industry_map = get_stock_industry(config, cache) if config.USE_INDUSTRY_DEDUP else {}
            confirmed: list = []
            industry_used: dict[str, int] = {}
            weekly_checked = weekly_dropped = pending_weekly = industry_dropped = fund_late_dropped = pending_fund = 0
            for _, row in df.iterrows():
                if len(confirmed) >= max_picks: break
                code = str(row["code"])
                wk = _fetch_weekly_dual(code, config)
                time.sleep(config.FETCH_DELAY)
                weekly_checked += 1
                weekly_status = "confirmed"
                weekly_ok = True
                if config.REQUIRE_WEEKLY_TREND:
                    if not _weekly_trend_data_ok(wk, config): weekly_status = "unverified"
                    weekly_ok = check_weekly_trend(wk, config)
                if weekly_ok and config.REQUIRE_WEEKLY_MACD_STABLE:
                    if not _weekly_macd_data_ok(wk, config): weekly_status = "unverified"
                    weekly_ok = check_weekly_macd(wk, config)
                if weekly_status == "confirmed" and latest_trade_date is not None \
                        and not _weekly_fresh_enough(wk, latest_trade_date):
                    weekly_status = "unverified"   # 周线截止过旧（如停牌），不足以为当前趋势背书
                if not weekly_ok:
                    weekly_dropped += 1
                    continue
                row["weekly_status"] = weekly_status
                if weekly_status == "unverified":
                    row["tier"] = "pending"
                    pending_list.append(row.to_dict())
                    pending_weekly += 1
                    continue
                industry = industry_map.get(code, "") or ""
                if industry and industry_used.get(industry, 0) >= config.MAX_PICKS_PER_INDUSTRY:
                    industry_dropped += 1
                    continue
                # 决赛圈财务补齐：主源不提供的商誉/扣非（及缺失核心项）用 AkShare 按字段补后终审
                fund = get_fundamentals(code, cache, config)  # 命中内存缓存，无额外主源成本
                if fund is None:
                    row["tier"] = "pending"
                    pending_list.append(row.to_dict())
                    pending_fund += 1
                    continue
                fund = _fill_optional_fundamentals(code, fund, config)
                if not check_fundamentals(fund, config, code=code, name=str(row["name"])):
                    fund_late_dropped += 1
                    continue
                fund_status, fund_tags = _fund_verify_state(fund)
                row["fund_status"] = fund_status
                kept_tags = [t for t in str(row.get("missing_tags", "") or "").split(",") if t and not t.startswith("fund")]
                row["missing_tags"] = ",".join(kept_tags + fund_tags)
                if fund_status != "verified":
                    row["tier"] = "pending"
                    pending_list.append(row.to_dict())
                    pending_fund += 1
                    continue
                if industry: industry_used[industry] = industry_used.get(industry, 0) + 1
                confirmed.append(row)
            if weekly_dropped > 0:
                conds = [f"周线MA{config.WEEKLY_MA_PERIOD}" + ("站上且上行" if config.WEEKLY_MA_BOTH_REQUIRED else "站上或上行")]
                if config.REQUIRE_WEEKLY_MACD_STABLE: conds.append("周线MACD企稳")
                logger.info("周线确认：检查 %d 只，淘汰 %d 只（未满足 %s）", weekly_checked, weekly_dropped, " + ".join(conds))
            if fund_late_dropped > 0:
                logger.info("决赛圈财务补齐后否决 %d 只（商誉/扣非/ROE/负债率超阈值）", fund_late_dropped)
            if industry_dropped > 0:
                logger.info("行业分散：淘汰 %d 只（同一行业最多 %d 只）", industry_dropped, config.MAX_PICKS_PER_INDUSTRY)
            if pending_list:
                detail = "、".join(f"{r.get('name')}({r.get('code')})" for r in pending_list[:10])
                more = f" 等 {len(pending_list)} 只" if len(pending_list) > 10 else ""
                logger.info("待核验候选：%s%s（财务/周线数据待补全，未进正式推荐）", detail, more)
            df = pd.DataFrame(confirmed).reset_index(drop=True) if confirmed else df.iloc[0:0]
        else:
            if config.USE_INDUSTRY_DEDUP and not df.empty:
                df = _dedup_by_industry(df, config, cache)
            if not df.empty:
                df["weekly_status"] = "disabled"   # 周线确认未启用（两个开关均关闭）
            # 推荐数量上限：按排序分降序截取前 max_picks 只（周线路径在循环内已取满即止）
            if len(df) > max_picks:
                logger.info("通过 %d 只，按排序分截取前 %d 只（淘汰 %d 只低分信号）",
                            len(df), max_picks, len(df) - max_picks)
                df = df.head(max_picks).reset_index(drop=True)
            # 周线未启用时：对最终名单做财务字段补齐与终审（四项防雷口径与决赛圈一致）
            if not df.empty:
                keep_rows = []
                for _, row in df.iterrows():
                    code = str(row["code"])
                    fund = get_fundamentals(code, cache, config)
                    if fund is None:
                        pending_list.append(row.to_dict())
                        continue
                    fund = _fill_optional_fundamentals(code, fund, config)
                    if not check_fundamentals(fund, config, code=code, name=str(row["name"])):
                        continue
                    fund_status, fund_tags = _fund_verify_state(fund)
                    row["fund_status"] = fund_status
                    kept_tags = [t for t in str(row.get("missing_tags", "") or "").split(",") if t and not t.startswith("fund")]
                    row["missing_tags"] = ",".join(kept_tags + fund_tags)
                    if fund_status != "verified":
                        row["tier"] = "pending"
                        pending_list.append(row.to_dict())
                        continue
                    keep_rows.append(row)
                if len(keep_rows) < len(df):
                    logger.info("财务核验后保留 %d/%d 只（未核验/补齐后被否决的不进入正式推荐）", len(keep_rows), len(df))
                df = pd.DataFrame(keep_rows).reset_index(drop=True) if keep_rows else df.iloc[0:0]

        # 保底观察候选（默认关闭）：正式推荐为空时，从技术面已通过但周线/财务待核验的候选中选
        # 评分最高者。该候选明确标记为 fallback，不改变正式推荐的严格口径，也不应直接用于实盘买入。
        if df.empty and config.ENABLE_DAILY_FALLBACK and pending_list:
            eligible = [r for r in pending_list
                        if float(r.get("score", 0) or 0) >= config.FALLBACK_MIN_SCORE]
            if eligible:
                eligible.sort(key=lambda r: (-float(r.get("score", 0) or 0), str(r.get("code", ""))))
                fallback = dict(eligible[0])
                fallback["tier"] = "fallback"
                fallback["fallback_reason"] = "正式推荐为空，技术面最高分待核验候选"
                fallback["weekly_status"] = fallback.get("weekly_status", "unverified")
                fallback["fund_status"] = fallback.get("fund_status", "missing")
                df = pd.DataFrame([fallback])
                pending_list = [r for r in pending_list if str(r.get("code")) != str(fallback.get("code"))]
                logger.info("保底观察候选：%s(%s)，评分 %s，未计入正式推荐",
                            fallback.get("name"), fallback.get("code"), fallback.get("score"))

        if pending_out is not None:
            pending_out.extend(pending_list)
        logger.info("筛选完成，正式推荐 %d 只股票%s", len(df),
                    f"；待核验候选 {len(pending_list)} 只（数据待补全，未正式推荐）" if pending_list else "")
        return df

    finally:
        # 无论发生什么异常，确保安全退出 Baostock（未登录时自动跳过）
        _bs_logout()

def _setup_logging() -> None:
    """统一日志格式：时间戳 + 级别 + 模块名，与 run.py / run_weekly_tracking.py 一致。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


if __name__ == "__main__":
    _setup_logging()
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode == "screen":
        result = main()
        print(result.to_string() if result is not None else "未发现信号")
    elif mode == "full":
        result = main()
        if result is not None: print(f"共 {len(result)} 只信号")
    else:
        print(f"未知模式: {mode}，可选: screen / full")
