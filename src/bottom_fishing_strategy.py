"""
精简版日线选股策略（Baostock 主数据源 + AkShare 备用数据源 + 底背离 + 漏斗日志版）

仅使用日线数据进行选股，简化策略逻辑：
1. 市场环境过滤（沪深300日线MA20斜率；数据不足时明示「未知」，不隐含为正常）
2. 基本面防雷（ROE/负债率核心否决；商誉/扣非为可选否决，主源不提供时决赛圈用 AkShare 按字段补齐）
3. 日线技术指标筛选（底背离 + MA5拐头 + EMA金叉 + RSI超卖反弹 + 量价质量分）
4. 风险收益比过滤（固定止损止盈）+ 决赛圈周线确认 + 行业分散

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
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

import baostock as bs

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
    ("score", "评分"), ("grade", "等级"), ("tier", "层级"),
    ("signals_hit", "入选依据"), ("fund_status", "财务核验"),
    ("weekly_status", "周线核验"), ("missing_tags", "缺项"), ("daily_score", "日线分"),
    ("rsi", "RSI"), ("rsi7", "RSI7"), ("rsi21", "RSI21"),
    ("vol_ratio", "量比"), ("turnover_ratio", "换手比"), ("stop_loss", "止损"),
    ("take_profit", "止盈"), ("rr_ratio", "收益比"), ("market_env", "市场"),
    ("has_divergence", "底背离"),
]

TITLE = "日线技术指标选股结果"
PREFIX = "bf"
SORT_BY = ["score"]
SORT_ASC = [False]

# ===========================================================================
# StrategyConfig
# ===========================================================================

@dataclass
class StrategyConfig:
    CSI300_AK_SYMBOL: str = "sh000300"  # Baostock 格式为 sh.000300
    MARKET_MA_PERIOD: int = 20
    MARKET_SLOPE_LOOKBACK: int = 4
    MARKET_BULL_SLOPE: float = 0.01
    MARKET_BEAR_SLOPE: float = -0.01

    MIN_ROE: float = 5.0
    MAX_DEBT_RATIO: float = 70.0
    MAX_GOODWILL_RATIO: float = 20.0
    MIN_DEDUCTED_PROFIT_RATIO: float = 0.5
    # 金融业（银行/保险/券商等）负债率天然 80%+，通用阈值会全行业误杀，单独放宽兜底
    FINANCE_NAME_KEYWORDS: tuple = ("银行", "保险", "证券", "信托", "期货")
    FINANCE_EXEMPT_CODES: tuple = ("601318", "601336", "601601", "601628", "601319", "300059")  # 平安/新华/太保/人寿/人保/东方财富
    FINANCE_MAX_DEBT_RATIO: float = 97.0

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
    MIN_RR_RATIO: float = 1.5
    ATR_PERIOD: int = 14
    ATR_STOP_MULT: float = 2.0
    USE_ATR_STOP: bool = True

    MIN_AMOUNT: float = 30_000_000.0  # 近 20 日日均成交额下限（元）；原 500 万对主板几乎无筛选力，上调至 3000 万过滤低流动性标的
    MIN_DAYS: int = 60
    # 数据时效：个股最新K线日期须与市场（沪深300）最新交易日一致，
    # 否则视为停牌/数据滞后，暂不推荐（宁可少荐，不让过期数据混入名单）
    REQUIRE_FRESH_DAILY: bool = True

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
    MIN_DRAWDOWN_FROM_HIGH: float = 0.15
    # 回撤上限：跌幅过深多为基本面恶化/退市风险（价值陷阱），并非健康底部，超过该比例则否决
    MAX_DRAWDOWN_FROM_HIGH: float = 0.70
    # 已反弹一段否决：近 N 日累计涨幅超过该值（%），说明底部反弹可能已走完，不再是介入点
    RECENT_GAIN_LOOKBACK: int = 5
    MAX_RECENT_GAIN_PCT: float = 12.0
    # 最终推荐数量上限：评分降序截取前 N 只（目标每日推荐 3~5 只）
    MAX_PICKS: int = 5
    # 行业分散（组合层风控）：同一行业最多推荐的只数，避免 Top5 集中单一板块导致组合同涨同跌
    USE_INDUSTRY_DEDUP: bool = True
    MAX_PICKS_PER_INDUSTRY: int = 2
    INDUSTRY_CACHE_TTL_DAYS: float = 30.0

    # ===== 严格确认指标（提高胜率，进一步压缩低质量信号）=====
    # 区间位置过滤：现价在近 N 日价格区间（最低~最高）中的位置超过该比例，判定不够低位，否决
    RANGE_LOOKBACK: int = 20
    POSITION_IN_RANGE_MAX: float = 0.35
    # MACD 动能确认：要求 MACD 柱当日较昨日改善（绿柱缩短或红柱放大）
    REQUIRE_MACD_MOMENTUM: bool = True
    # MACD 柱需连续改善的天数（1=仅当日较昨日；2=连续两日改善，过滤单日反抽）
    MACD_MOMENTUM_DAYS: int = 2
    # KDJ 确认：要求 KDJ 处于金叉状态（K>D）且 K 值不高于该上限（避免高位接力）
    REQUIRE_KDJ_GOLDEN: bool = True
    KDJ_K_MAX: float = 55.0
    # KDJ 动能：要求 K 值较昨日上行（仅 K>D 不够，K 掉头时容易误判为金叉）
    REQUIRE_KDJ_RISING: bool = True
    # 周线趋势确认：仅对通过全部日线筛选的决赛圈股票拉取周线；
    # WEEKLY_MA_BOTH_REQUIRED=True 时须同时满足「收盘站上周线 MA10（容忍 2%）」和「MA10 在上行」，
    # 设为 False 退回旧行为（两条件满足其一即可）
    REQUIRE_WEEKLY_TREND: bool = True
    WEEKLY_MA_PERIOD: int = 10
    WEEKLY_SLOPE_LOOKBACK: int = 3
    WEEKLY_TOLERANCE: float = 0.02
    WEEKLY_MA_BOTH_REQUIRED: bool = True
    # 周线 MACD 企稳确认：周线 MACD 柱翻红（含金叉后）或绿柱连续 2 周收窄，
    # 确认周线级别动能拐头，避免周线仍在加速下跌时抄底；设为 False 关闭
    REQUIRE_WEEKLY_MACD_STABLE: bool = True
    WEEKLY_MACD_FAST: int = 12
    WEEKLY_MACD_SLOW: int = 26
    WEEKLY_MACD_SIGNAL: int = 9

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
    DAILY_RSI_OVERBOUGHT_PENALTY: float = 3.0
    # 底背离不参与评级升降（评级统一：等级=原始技术分定级），仅作形态标签与同分排序优先项

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
    DAILY_BARS: int = 120
    WEEKLY_BARS: int = 60
    FETCH_DELAY: float = 0.05

    ADJUST: str = "qfq"
    USE_CACHE: bool = True
    CACHE_DIR: str = os.path.join(_PROJECT_ROOT, "cache")
    CACHE_TTL_DAYS: float = 6.0
    FUND_CACHE_TTL_DAYS: float = 7.0
    # 财报季度回溯次数：从「已披露窗口」的最新一季开始向前找，最多尝试几个季度
    FUND_QUARTER_LOOKBACK: int = 3
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
        print(f"[WARN] Baostock 连续失败 {_BS_CIRCUIT_THRESHOLD} 次，已熔断，本次运行后续请求切换至 AkShare 备用数据源")

def _bs_available() -> bool:
    return not _bs_state["circuit_open"]

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
_NUMERIC_COLS = ("open", "close", "high", "low", "volume", "amount", "pct_chg", "turnover")

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

def _normalize_bs_hist(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if df is None or df.empty: return None
    # Baostock 列名映射
    rename_map = {"pctChg": "pct_chg", "turn": "turnover"}
    df = df.rename(columns=rename_map)
    if "date" not in df.columns or "close" not in df.columns: return None
    for col in _NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    return df.sort_values("date").reset_index(drop=True) if not df.empty else None

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
    fields = "date,open,high,low,close,volume,amount,pctChg,turn"

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

def _quarter_candidates(now: datetime, n: int = 3) -> list[tuple[int, int]]:
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


def _fetch_fundamentals_bs(code: str, config: Optional[StrategyConfig] = None) -> Optional[dict]:
    config = config or StrategyConfig()
    bs_code = _format_bs_code(code)
    quarters = _quarter_candidates(datetime.now(), n=config.FUND_QUARTER_LOOKBACK)
    path = ""
    if config.USE_CACHE:
        # 缓存名带起始季度标签：财报窗口滚动后自动失效，避免复用上一季的旧数据
        path = _cache_path(config, f"fund_{bs_code}_{quarters[0][0]}Q{quarters[0][1]}.json")
        if _cache_fresh(path, config.FUND_CACHE_TTL_DAYS):
            if (cached := _read_cache_json(path)) is not None: return cached

    # 商誉与扣非净利润在 Baostock 中缺失，置为 None
    result: dict[str, Optional[float]] = {"roe": None, "debt_ratio": None, "goodwill_ratio": None, "deducted_profit_ratio": None}

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
        if p_rs.error_code == '0' and len(p_rs.data) > 0 and result["roe"] is None:
            df = p_rs.get_data()
            if "roeAvg" in df.columns:
                roe = pd.to_numeric(df["roeAvg"].iloc[0], errors="coerce")
                if not pd.isna(roe): result["roe"] = roe * 100  # Baostock 返回小数(如0.05)
        if b_rs.error_code == '0' and len(b_rs.data) > 0 and result["debt_ratio"] is None:
            df = b_rs.get_data()
            # Baostock 资产负债率字段为 liabilityToAsset（小数形式，兼容其他可能的字段名）
            debt_col = next((c for c in ("liabilityToAsset", "liabToAsset", "liabRate") if c in df.columns), None)
            if debt_col:
                debt = pd.to_numeric(df[debt_col].iloc[0], errors="coerce")
                if not pd.isna(debt): result["debt_ratio"] = debt * 100

    if not any(v is not None for v in result.values()): return None
    if path: _write_cache_json(result, path)
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
                        print(f"[INFO] 今日非交易日，股票列表回退至最近交易日 {day}")
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
    return df.sort_values("date").reset_index(drop=True) if not df.empty else None

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
    """AkShare 基本面：相比 Baostock 额外补齐商誉占比与扣非利润占比"""
    if not _AK_AVAILABLE: return None
    config = config or StrategyConfig()
    symbol = _ak_symbol(code)
    path = ""
    if config.USE_CACHE:
        path = _cache_path(config, f"fund_{symbol}.json")
        if _cache_fresh(path, config.FUND_CACHE_TTL_DAYS):
            if (cached := _read_cache_json(path)) is not None: return cached

    result: dict[str, Optional[float]] = {"roe": None, "debt_ratio": None, "goodwill_ratio": None, "deducted_profit_ratio": None}

    df_fin = _fetch_with_retry(lambda: ak.stock_financial_analysis_indicator(symbol=symbol, start_year=config.FUND_START_YEAR), config.MAX_RETRY, f"ak_fund({symbol})")
    if df_fin is not None and not df_fin.empty:
        row = df_fin.iloc[0]
        for col in df_fin.columns:
            col_str, col_lower = str(col), str(col).lower()
            if "净资产收益率" in col_str or "roe" in col_lower:
                val = pd.to_numeric(row[col], errors="coerce")
                if not pd.isna(val): result["roe"] = float(val)
            if "资产负债率" in col_str or "debt" in col_lower:
                val = pd.to_numeric(row[col], errors="coerce")
                if not pd.isna(val): result["debt_ratio"] = float(val)

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

    if not any(v is not None for v in result.values()): return None
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
    if _bs_available():
        df = _fetch_daily_bs(code, days=days, config=config)
        if df is not None: return df
    return _fetch_daily_ak(code, days=days, config=config)

def _fetch_weekly_dual(code: str, config: StrategyConfig) -> Optional[pd.DataFrame]:
    """周线双源拉取（仅决赛圈周线趋势确认使用）：Baostock 优先，失败降级 AkShare"""
    if _bs_available():
        df = _fetch_weekly_bs(code, weeks=config.WEEKLY_BARS, config=config)
        if df is not None: return df
    if not _AK_AVAILABLE: return None
    symbol = _ak_symbol(code)
    start = (datetime.now() - timedelta(weeks=config.WEEKLY_BARS)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    raw = _fetch_with_retry(
        lambda: ak.stock_zh_a_hist(symbol=symbol, period="weekly", start_date=start, end_date=end, adjust=config.ADJUST),
        config.MAX_RETRY, f"ak_weekly({symbol})"
    )
    return _normalize_ak_hist(raw)

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
        return {"regime": "unknown", "description": "未知（沪深300数据不足）", "ma20": 0, "slope": 0, "close": 0}
    df = df_index.copy().reset_index(drop=True)
    df["ma"] = df["close"].rolling(config.MARKET_MA_PERIOD).mean()
    cur, prev = df.iloc[-1], df.iloc[-(1 + config.MARKET_SLOPE_LOOKBACK)]
    ma_now, ma_prev, close_now = float(cur["ma"]), float(prev["ma"]), float(cur["close"])
    slope = (ma_now - ma_prev) / ma_prev if ma_prev > 0 else 0.0
    if slope > config.MARKET_BULL_SLOPE: regime, desc = "bull", f"偏多（MA20斜率 {slope:.4f}，沪深300收于 {close_now:.0f}）"
    elif slope < config.MARKET_BEAR_SLOPE: regime, desc = "bear", f"偏空（MA20斜率 {slope:.4f}，沪深300收于 {close_now:.0f}）"
    else: regime, desc = "neutral", f"中性（MA20斜率 {slope:.4f}，沪深300收于 {close_now:.0f}）"
    return {"regime": regime, "description": desc, "ma20": round(ma_now, 2), "slope": round(slope, 6), "close": round(close_now, 2)}

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
    if fund_data.get("roe") is None: core_ok = False; tags.append("fund_roe")
    if fund_data.get("debt_ratio") is None: core_ok = False; tags.append("fund_debt")
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
    # 流动性过滤：近 20 日日均成交额低于下限，判定为僵尸股直接否决
    if df["amount"].tail(20).mean() < config.MIN_AMOUNT: return None
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
        out["multi_resonance"].astype(float) * config.DAILY_MULTI_RESONANCE_BONUS +
        (out["rsi14"] >= config.DAILY_RSI_OVERBOUGHT).astype(float) * (-config.DAILY_RSI_OVERBOUGHT_PENALTY)
    ).fillna(0).clip(0, 100).round(1)

    return out

def compute_risk_reward(entry_price: float, config: StrategyConfig, atr: Optional[float] = None) -> dict:
    if config.USE_ATR_STOP and atr is not None and atr > 0: stop_loss = entry_price - config.ATR_STOP_MULT * atr
    else: stop_loss = entry_price * (1 - config.FIXED_STOP_LOSS_PCT / 100)
    take_profit = entry_price * (1 + config.FIXED_TAKE_PROFIT_PCT / 100)
    risk, reward = entry_price - stop_loss, take_profit - entry_price
    rr_ratio = reward / risk if risk > 0 else 0.0
    return {"stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2), "rr_ratio": round(rr_ratio, 2), "passes": rr_ratio >= config.MIN_RR_RATIO}

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


_FUND_STATUS_ZH = {"verified": "财务已核验", "partial": "财务部分核验", "missing": "财务未核验"}
_WEEKLY_STATUS_ZH = {"confirmed": "周线已确认", "unverified": "周线待核验", "disabled": "周线未启用"}
_MISSING_TAG_ZH = {
    "fund": "财务数据缺失", "fund_roe": "ROE缺失", "fund_debt": "负债率缺失",
    "fund_goodwill": "商誉缺失", "fund_deducted": "扣非缺失",
    "pct_chg": "涨幅数据缺失", "gap": "开盘价缺失",
    "macd_mom": "MACD柱数据缺失", "kdj": "KDJ数据缺失",
}

def _missing_tags_zh(tags: str) -> str:
    if not tags:
        return ""
    return "、".join(_MISSING_TAG_ZH.get(t, t) for t in str(tags).split(",") if t)

def describe(row: dict) -> str:
    """把一条推荐格式化为飞书卡片文本（notify/feishu.py 调用），按三维展示：

    1. 基础评分/等级：分数与等级同源（均按原始技术分定级，熊市只抬门槛不扣展示分）；
    2. 入选依据：实际触发的技术条件（放量企稳/放量上涨/放量冲高回落措辞区分）；
    3. 核验状态：财务/周线是否已核验、缺项明细（可选指标缺失只标注不降级）。
    """
    verify_bits = [
        _FUND_STATUS_ZH.get(str(row.get("fund_status", "")), "财务未核验"),
        _WEEKLY_STATUS_ZH.get(str(row.get("weekly_status", "")), "周线待核验"),
    ]
    missing = _missing_tags_zh(str(row.get("missing_tags", "") or ""))
    verify = " | ".join(verify_bits) + (f" | 缺项: {missing}" if missing else "")
    lines = [
        f"**{row.get('name', '')} {row.get('code', '')}**（低位企稳候选）",
        f"评分: {_fmt_cell(row.get('score'))} ({row.get('grade') or '-'}级)"
        f" | 收盘: {_fmt_cell(row.get('close'))}"
        f" | 止损: {_fmt_cell(row.get('stop_loss'))}"
        f" | 止盈: {_fmt_cell(row.get('take_profit'))}"
        f" | RR: {_fmt_cell(row.get('rr_ratio'))}",
    ]
    if row.get("signals_hit"):
        lines.append(f"入选依据: {row.get('signals_hit')}")
    lines.append(f"核验: {verify}")
    lines.append(
        f"RSI14: {_fmt_cell(row.get('rsi'))}"
        f" | 量比: {_fmt_cell(row.get('vol_ratio'))}"
        f" | 市场: {row.get('market_env') or '-'}"
        + (" | 底背离" if row.get("has_divergence") else "")
    )
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

    def to_dict(self) -> dict: return asdict(self)

def _grade_from_score(score: float, config: StrategyConfig) -> str:
    if score >= config.GRADE_A: return "A"
    elif score >= config.GRADE_B: return "B"
    elif score >= config.GRADE_C: return "C"
    return "D"

_GRADE_ORDER = ("D", "C", "B", "A")

def _rank_signals(df: pd.DataFrame) -> pd.DataFrame:
    """确定性排序：评分降序 → 底背离优先 → 盈亏比降序 → 股票代码升序（末级键）。

    分数、背离、盈亏比完全相同时按代码排序，保证两次运行结果可复现，
    不受并发完成顺序（as_completed）影响。
    """
    return df.sort_values(
        by=SORT_BY + ["has_divergence", "rr_ratio", "code"],
        ascending=SORT_ASC + [False, False, True],
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

def check_weekly_trend(weekly_df: Optional[pd.DataFrame], config: StrategyConfig) -> bool:
    """周线趋势确认：收盘价站上周线 MA10（容忍 WEEKLY_TOLERANCE）且 MA10 在上行；
    WEEKLY_MA_BOTH_REQUIRED=False 退回旧行为（两条件满足其一即可）；数据不足放行"""
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
    或绿柱连续 2 周收窄（柱值连续两周改善，下跌动能衰减企稳）；数据不足或 NaN 放行不误杀"""
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

def evaluate(daily_df: Optional[pd.DataFrame], code: str = "", name: str = "", config: Optional[StrategyConfig] = None, market_env: Optional[dict] = None, fund_data: Optional[dict] = None, latest_trade_date: Optional[str] = None) -> tuple[Optional[Signal], str]:
    """评估单只股票。

    latest_trade_date：市场（沪深300）最新交易日，用于行情时效校验——
    个股最新K线日期与之一致才评估（停牌/数据滞后 → 暂不推荐），None 时跳过校验。
    """
    if config is None: config = StrategyConfig()
    regime = (market_env or {}).get("regime", "unknown")

    if not check_fundamentals(fund_data, config, code=code, name=name): return None, "FAIL_FUND"

    daily_out = compute_daily_signals(daily_df, config)
    if daily_out is None or daily_out.empty: return None, "FAIL_DATA"

    d_last = daily_out.iloc[-1]

    # 数据时效：个股最新K线须与市场最新交易日一致（停牌/数据滞后 → 暂不推荐）
    if config.REQUIRE_FRESH_DAILY and latest_trade_date is not None \
            and str(d_last["date"]) != str(latest_trade_date):
        return None, "FAIL_STALE"

    # 核验状态维度一：财务核验（核心项=ROE+负债率；商誉/扣非为可选项，缺失只记标签）
    fund_status, fund_missing = _fund_verify_state(fund_data)
    missing_tags: list[str] = list(fund_missing)

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

    atr_val = d_last.get("atr")
    atr_val = float(atr_val) if atr_val is not None and not pd.isna(atr_val) else None

    rr = compute_risk_reward(entry_price=last_close, config=config, atr=atr_val)
    if not rr["passes"]: return None, "FAIL_RR"

    # 入选依据：实际触发的技术条件（量价按质量分档区分措辞）
    hits: list[str] = []
    if bool(d_last.get("trend_turn", False)): hits.append("趋势转折")
    if bool(d_last.get("rsi_rebound", False)): hits.append("RSI超卖反弹")
    vp_label = str(d_last.get("vol_price_label", "") or "")
    if bool(d_last.get("vol_price_coord", False)): hits.append(vp_label or "放量上涨")
    if bool(d_last.get("multi_resonance", False)): hits.append("多周期共振")
    if has_div: hits.append("底背离")

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
    )
    return sig, "PASS"

def get_market_environment(config: StrategyConfig, cache: CacheManager) -> dict:
    if (cached := cache.get("market_env")) is not None: return cached
    df_index = get_index_daily(config, cache)
    result = compute_market_environment(df_index, config) if df_index is not None and not df_index.empty else {"regime": "unknown", "description": "未知（数据获取失败）", "ma20": 0, "slope": 0, "close": 0}
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
        print(f"[INFO] 行业分散：淘汰 {dropped} 只（同一行业最多 {config.MAX_PICKS_PER_INDUSTRY} 只）")
    return pd.DataFrame(kept).reset_index(drop=True) if kept else df.iloc[0:0]

# ===========================================================================
# 编排函数
# ===========================================================================

def main(config: Optional[StrategyConfig] = None, cache: Optional[CacheManager] = None,
         pending_out: Optional[list] = None) -> Optional[pd.DataFrame]:
    """执行选股主流程，返回正式推荐（formal）DataFrame。

    pending_out：可选 list，传出「待核验候选」（财务/周线数据缺失、不与正式推荐混排），
    由调用方决定是否展示——数据缺失不等于筛选通过，宁可少荐。
    """
    if config is None: config = StrategyConfig()
    if cache is None: cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)

    # ==========================
    # 初始化数据源：Baostock 优先，登录失败降级 AkShare
    # ==========================
    if not _bs_login(max_retry=5):
        if _AK_AVAILABLE:
            print("[WARN] Baostock 登录失败（已重试 5 次），本次运行降级为 AkShare 备用数据源")
            _bs_state["circuit_open"] = True
        else:
            print("[ERROR] Baostock 登录失败，且未安装 AkShare（pip install akshare），无可用数据源")
            return None
    else:
        print(f"[INFO] 数据源: Baostock（备用: {'AkShare' if _AK_AVAILABLE else '未安装 akshare，无备用'}）")

    try:
        market_env = get_market_environment(config, cache)
        print(f"[INFO] 市场环境: {market_env.get('description', 'unknown')}")

        # 数据时效基准：市场（沪深300）最新交易日，个股日线/周线截止日期均须与之一致
        index_df = get_index_daily(config, cache)
        latest_trade_date = str(index_df["date"].iloc[-1]) \
            if index_df is not None and not index_df.empty and "date" in index_df.columns else None
        if latest_trade_date is None:
            print("[WARN] 无法获取指数行情，本次运行跳过行情时效校验（停牌股可能混入待核验流程）")

        stock_list = get_stock_list(config, cache)
        if not stock_list:
            print("[WARN] 无法获取股票列表")
            return None
        print(f"[INFO] 待筛选股票数: {len(stock_list)}")

        signals: list[dict] = []
        processed, total = 0, len(stock_list)
        stats = {"total": total, "error": 0, "fail_data": 0, "fail_stale": 0, "fail_fund": 0, "fail_tech": 0, "fail_rr": 0, "pass": 0}

        def _screen_one(stock: dict) -> tuple[Optional[Signal], str]:
            code, name = stock["code"], stock["name"]
            try:
                daily_df = get_daily_data(code, config, cache)
                if daily_df is None: return None, "FAIL_DATA"
                fund_data = get_fundamentals(code, cache, config)
                return evaluate(daily_df, code, name, config, market_env, fund_data,
                                latest_trade_date=latest_trade_date)
            except Exception:
                return None, "ERROR"

        # 并发执行 (依靠 bs_lock 保证 Baostock 查询不会互相踩踏)
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
            futures = {pool.submit(_screen_one, s): s for s in stock_list}
            for future in as_completed(futures):
                processed += 1
                if processed % 500 == 0: print(f"[INFO] 进度: {processed}/{total}")
                sig, reason = future.result()
                if reason == "PASS" and sig is not None:
                    stats["pass"] += 1
                    signals.append(sig.to_dict())
                elif reason == "FAIL_FUND": stats["fail_fund"] += 1
                elif reason == "FAIL_DATA": stats["fail_data"] += 1
                elif reason == "FAIL_STALE": stats["fail_stale"] += 1
                # FAIL_CHASE（追高）/ FAIL_GAP（跳空）/ FAIL_RSI_HIGH（RSI过高）/ FAIL_CLIMAX_VOL（天量）/ FAIL_NOT_BOTTOM（非底部区域）
                # / FAIL_POSITION（区间位置偏高）/ FAIL_MACD_MOM（动能未改善）/ FAIL_KDJ（KDJ未金叉）均属技术面入场质量层
                elif reason in ("FAIL_TECH", "FAIL_CHASE", "FAIL_GAP", "FAIL_RSI_HIGH", "FAIL_CLIMAX_VOL",
                                "FAIL_NOT_BOTTOM", "FAIL_DEEP_CRASH", "FAIL_RECENT_RALLY",
                                "FAIL_POSITION", "FAIL_MACD_MOM", "FAIL_KDJ"): stats["fail_tech"] += 1
                elif reason == "FAIL_RR": stats["fail_rr"] += 1
                elif reason == "ERROR": stats["error"] += 1

        pass_data = stats["total"] - stats["fail_data"] - stats["fail_stale"] - stats["error"]
        pass_fund = pass_data - stats["fail_fund"]
        pass_tech = pass_fund - stats["fail_tech"]
        pass_rr = pass_tech - stats["fail_rr"]

        print("\n" + "="*50)
        print("📊 选股漏斗数据分析 (Funnel Log)")
        print("="*50)
        print(f"1. 初始有效股票池: {stats['total']} 只")
        print(f"2. 获取数据并达标: {pass_data} 只 (淘汰/缺失 {stats['fail_data'] + stats['error']} 只)")
        if stats["fail_stale"] > 0:
            print(f"   其中行情时效不符（停牌/数据滞后）: {stats['fail_stale']} 只，暂不推荐")
        if pass_data > 0: print(f"3. 基本面防雷通过: {pass_fund} 只 (淘汰 {stats['fail_fund']} 只，通过率 {pass_fund/pass_data*100:.1f}%)")
        if pass_fund > 0: print(f"4. 日线技术面达标: {pass_tech} 只 (淘汰 {stats['fail_tech']} 只，通过率 {pass_tech/pass_fund*100:.1f}%)")
        if pass_tech > 0: print(f"5. 盈亏风控比达标: {pass_rr} 只 (淘汰 {stats['fail_rr']} 只，通过率 {pass_rr/pass_tech*100:.1f}%)")
        print("="*50 + "\n")

        if not signals:
            print("[INFO] 未发现符合条件的信号")
            return None

        # 确定性排序：评分降序 → 底背离优先 → 盈亏比 → 股票代码（末级键，结果可复现）
        df = _rank_signals(pd.DataFrame(signals))

        # 决赛圈：周线确认 + 财务字段补齐 + 行业分散，逐个按评分降序处理，取满 MAX_PICKS 即止。
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
                if len(confirmed) >= config.MAX_PICKS: break
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
                print(f"[INFO] 周线确认：检查 {weekly_checked} 只，淘汰 {weekly_dropped} 只（未满足 {' + '.join(conds)}）")
            if fund_late_dropped > 0:
                print(f"[INFO] 决赛圈财务补齐后否决 {fund_late_dropped} 只（商誉/扣非/ROE/负债率超阈值）")
            if industry_dropped > 0:
                print(f"[INFO] 行业分散：淘汰 {industry_dropped} 只（同一行业最多 {config.MAX_PICKS_PER_INDUSTRY} 只）")
            if pending_list:
                detail = "、".join(f"{r.get('name')}({r.get('code')})" for r in pending_list[:10])
                more = f" 等 {len(pending_list)} 只" if len(pending_list) > 10 else ""
                print(f"[INFO] 待核验候选：{detail}{more}（财务/周线数据待补全，未进正式推荐）")
            df = pd.DataFrame(confirmed).reset_index(drop=True) if confirmed else df.iloc[0:0]
        else:
            if config.USE_INDUSTRY_DEDUP and not df.empty:
                df = _dedup_by_industry(df, config, cache)
            if not df.empty:
                df["weekly_status"] = "disabled"   # 周线确认未启用（两个开关均关闭）
            # 推荐数量上限：评分降序截取前 MAX_PICKS 只（周线路径在循环内已取满即止）
            if len(df) > config.MAX_PICKS:
                print(f"[INFO] 通过 {len(df)} 只，按评分截取前 {config.MAX_PICKS} 只（淘汰 {len(df) - config.MAX_PICKS} 只低分信号）")
                df = df.head(config.MAX_PICKS).reset_index(drop=True)
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
                    print(f"[INFO] 财务核验后保留 {len(keep_rows)}/{len(df)} 只（未核验/补齐后被否决的不进入正式推荐）")
                df = pd.DataFrame(keep_rows).reset_index(drop=True) if keep_rows else df.iloc[0:0]

        if pending_out is not None:
            pending_out.extend(pending_list)
        print(f"[INFO] 筛选完成，正式推荐 {len(df)} 只股票"
              + (f"；待核验候选 {len(pending_list)} 只（数据待补全，未正式推荐）" if pending_list else ""))
        return df

    finally:
        # 无论发生什么异常，确保安全退出 Baostock（未登录时自动跳过）
        _bs_logout()

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode == "screen":
        result = main()
        print(result.to_string() if result is not None else "未发现信号")
    elif mode == "full":
        result = main()
        if result is not None: print(f"共 {len(result)} 只信号")
    else:
        print(f"未知模式: {mode}，可选: screen / full")