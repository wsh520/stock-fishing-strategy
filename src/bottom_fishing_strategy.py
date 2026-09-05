"""
精简版日线选股策略（Baostock 主数据源 + AkShare 备用数据源 + 底背离 + 漏斗日志版）

仅使用日线数据进行选股，简化策略逻辑：
1. 市场环境过滤（沪深300日线MA20斜率）
2. 基本面防雷（ROE/负债率否决；Baostock 无商誉/扣非数据，切 AkShare 时自动补齐）
3. 日线技术指标筛选（底背离 + MA5拐头 + EMA金叉 + RSI超卖反弹 + 量价配合）
4. 风险收益比过滤（固定止损止盈）

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
    ("score", "评分"), ("grade", "等级"), ("daily_score", "日线分"),
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

    FIXED_STOP_LOSS_PCT: float = 5.0
    FIXED_TAKE_PROFIT_PCT: float = 10.0
    MIN_RR_RATIO: float = 1.5
    ATR_PERIOD: int = 14
    ATR_STOP_MULT: float = 2.0
    USE_ATR_STOP: bool = True

    MIN_AMOUNT: float = 5_000_000.0  # 近 20 日日均成交额下限（元），低于则判定流动性不足（僵尸股）
    MIN_DAYS: int = 60

    # 防追高否决：当日涨幅超过该值（%）判定为追高——涨停股买不进、大阳线次日易回调，直接否决
    MAX_ENTRY_PCT_CHG: float = 7.0
    # 防跳空否决：开盘价相对前收跳空高开超过该比例（%），追买风险大，直接否决
    MAX_GAP_UP_PCT: float = 3.0
    # 下降通道过滤：MA20 近 N 日变化率低于该阈值判定为陡峭下降（接飞刀），当日趋势转折信号不认可
    DAILY_MA20: int = 20
    MA20_TREND_LOOKBACK: int = 5
    MA20_TREND_MIN_SLOPE: float = -0.04
    # RSI 入场上限：RSI14 高于该值说明已反弹一段、不再是底部入场点，直接否决
    DAILY_RSI_ENTRY_MAX: float = 65.0
    # 天量否决：量比超过该值疑似主力出货/消息驱动，次日接力风险大，直接否决
    MAX_VOL_RATIO: float = 5.0
    # 底部区域过滤：现价距近 N 日最高价回撤不足该比例，不符合抄底定位（可能只是上涨中继回调），直接否决
    DRAWDOWN_LOOKBACK: int = 60
    MIN_DRAWDOWN_FROM_HIGH: float = 0.10
    # 最终推荐数量上限：评分降序截取前 N 只（目标每日推荐 3~5 只）
    MAX_PICKS: int = 5

    # ===== 严格确认指标（提高胜率，进一步压缩低质量信号）=====
    # 区间位置过滤：现价在近 N 日价格区间（最低~最高）中的位置超过该比例，判定不够低位，否决
    RANGE_LOOKBACK: int = 20
    POSITION_IN_RANGE_MAX: float = 0.40
    # MACD 动能确认：要求 MACD 柱当日较昨日改善（绿柱缩短或红柱放大）
    REQUIRE_MACD_MOMENTUM: bool = True
    # KDJ 确认：要求 KDJ 处于金叉状态（K>D）且 K 值不高于该上限（避免高位接力）
    REQUIRE_KDJ_GOLDEN: bool = True
    KDJ_K_MAX: float = 60.0
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
    W_DAILY_VOL_PRICE: float = 25.0
    DAILY_MULTI_RESONANCE_BONUS: float = 10.0
    DAILY_RSI_OVERBOUGHT_PENALTY: float = 3.0
    # 底背离不再直接加分，改为评级提升档数（与基础分脱钩，避免底背离股必然 A 级）
    DIVERGENCE_GRADE_LIFT: int = 1

    GRADE_A: float = 80.0
    GRADE_B: float = 60.0
    GRADE_C: float = 40.0
    BEAR_GRADE_BOOST: float = 10.0
    # 准入等级门槛：基础评级（按分数，不含底背离提升）须不低于该等级，默认 B（≥60 分）。
    # C 级仅为单一趋势转折信号（40 分），噪音过大不再推荐；设为 "C" 可恢复旧行为。
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

def _fetch_fundamentals_bs(code: str, config: Optional[StrategyConfig] = None) -> Optional[dict]:
    config = config or StrategyConfig()
    bs_code = _format_bs_code(code)
    path = ""
    if config.USE_CACHE:
        path = _cache_path(config, f"fund_{bs_code}.json")
        if _cache_fresh(path, config.FUND_CACHE_TTL_DAYS):
            if (cached := _read_cache_json(path)) is not None: return cached

    # 动态计算最近的已披露财报季度
    now = datetime.now()
    year, quarter = now.year, (now.month - 1) // 3
    if quarter == 0: year -= 1; quarter = 4

    # 商誉与扣非净利润在 Baostock 中缺失，置为 None
    result: dict[str, Optional[float]] = {"roe": None, "debt_ratio": None, "goodwill_ratio": None, "deducted_profit_ratio": None}

    def fetch_fund():
        _bs_guard(f"bs_fund({bs_code})")
        with bs_lock:
            p_rs = bs.query_profit_data(code=bs_code, year=year, quarter=quarter)
            b_rs = bs.query_balance_data(code=bs_code, year=year, quarter=quarter)
        if getattr(p_rs, "error_code", None) == "0" or getattr(b_rs, "error_code", None) == "0":
            _bs_mark_success()
            return p_rs, b_rs
        _bs_mark_failure()
        raise RuntimeError(f"bs_fund({bs_code}) 查询失败: {getattr(p_rs, 'error_msg', '')} / {getattr(b_rs, 'error_msg', '')}")

    rs = _fetch_with_retry(fetch_fund, config.MAX_RETRY, f"bs_fund({bs_code})")
    if rs:
        p_rs, b_rs = rs
        if p_rs.error_code == '0' and len(p_rs.data) > 0:
            df = p_rs.get_data()
            if "roeAvg" in df.columns:
                roe = pd.to_numeric(df["roeAvg"].iloc[0], errors="coerce")
                if not pd.isna(roe): result["roe"] = roe * 100 # Baostock 返回小数(如0.05)

        if b_rs.error_code == '0' and len(b_rs.data) > 0:
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


# ===========================================================================
# Strategy Logic (Unchanged Layer 1-4)
# ===========================================================================

def compute_market_environment(df_index: pd.DataFrame, config: StrategyConfig) -> dict:
    if df_index is None or df_index.empty or len(df_index) < config.MARKET_MA_PERIOD + config.MARKET_SLOPE_LOOKBACK:
        return {"regime": "unknown", "description": "数据不足", "ma20": 0, "slope": 0, "close": 0}
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
    # Baostock 缺失时为 None，自动放行，逻辑保持不变
    if (goodwill := fund_data.get("goodwill_ratio")) is not None and goodwill > config.MAX_GOODWILL_RATIO: return False
    if (deducted := fund_data.get("deducted_profit_ratio")) is not None and deducted < config.MIN_DEDUCTED_PROFIT_RATIO: return False
    return True

_DAILY_NEED_COLS = {"close", "volume", "amount", "date", "high", "low", "pct_chg"}

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

    past_min_close = out["close"].shift(1).rolling(config.DIVERGENCE_LOOKBACK).min()
    past_min_rsi = out["rsi14"].shift(1).rolling(config.DIVERGENCE_LOOKBACK).min()
    past_min_macd = out["macd_diff"].shift(1).rolling(config.DIVERGENCE_LOOKBACK).min()
    price_new_low = out["close"] < past_min_close
    rsi_div_state = price_new_low & (out["rsi14"] > past_min_rsi + config.DAILY_RSI_DIVERGENCE_THRESHOLD)
    macd_div_state = price_new_low & (out["macd_diff"] > past_min_macd)
    recent_div = (rsi_div_state | macd_div_state).rolling(3).max() > 0
    right_confirm = out["macd_golden_cross"] | out["rsi_rebound"] | out["ma5_turn"]
    out["bottom_divergence"] = recent_div & right_confirm

    price_up = out["close"] > out["close"].shift(1)
    vol_expand = out["daily_vol_ratio"] >= config.DAILY_VOL_EXPAND
    out["vol_price_coord"] = price_up & vol_expand
    out["multi_resonance"] = out["rsi_multi_res"] & out["macd_golden_cross"] & out["vol_price_coord"]

    out["daily_score"] = (
        out["trend_turn"].astype(float) * config.W_DAILY_TREND_TURN +
        out["rsi_rebound"].astype(float) * config.W_DAILY_RSI_REBOUND +
        out["vol_price_coord"].astype(float) * config.W_DAILY_VOL_PRICE +
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

@dataclass
class Signal:
    code: str; name: str; date: str; close: float; score: float; grade: str
    daily_score: float; rsi: float; rsi7: float; rsi21: float; vol_ratio: float
    turnover_ratio: float; stop_loss: float; take_profit: float; rr_ratio: float
    market_env: str; has_divergence: bool

    def to_dict(self) -> dict: return asdict(self)

def _grade_from_score(score: float, config: StrategyConfig) -> str:
    if score >= config.GRADE_A: return "A"
    elif score >= config.GRADE_B: return "B"
    elif score >= config.GRADE_C: return "C"
    return "D"

_GRADE_ORDER = ("D", "C", "B", "A")

def _lift_grade(grade: str, levels: int = 1) -> str:
    """评级提升（底背离奖励），A 级封顶"""
    idx = _GRADE_ORDER.index(grade) if grade in _GRADE_ORDER else 0
    return _GRADE_ORDER[min(idx + levels, len(_GRADE_ORDER) - 1)]

# ===== 严格确认指标判定（纯函数，数据缺失一律放行不误杀）=====

def _range_position_ok(daily_out: pd.DataFrame, config: StrategyConfig) -> bool:
    """现价须处于近 RANGE_LOOKBACK 日价格区间的下半部（位置 ≤ POSITION_IN_RANGE_MAX），确保买在低位"""
    win = daily_out.tail(config.RANGE_LOOKBACK)
    lo, hi = float(win["low"].min()), float(win["high"].max())
    if hi <= lo: return True
    return (float(daily_out.iloc[-1]["close"]) - lo) / (hi - lo) <= config.POSITION_IN_RANGE_MAX

def _macd_momentum_ok(daily_out: pd.DataFrame, config: StrategyConfig) -> bool:
    """MACD 柱当日较昨日改善（绿柱缩短或红柱放大）"""
    if len(daily_out) < 2: return True
    h, hp = daily_out.iloc[-1]["macd_histogram"], daily_out.iloc[-2]["macd_histogram"]
    if pd.isna(h) or pd.isna(hp): return True
    return float(h) > float(hp)

def _kdj_ok(daily_out: pd.DataFrame, config: StrategyConfig) -> bool:
    """KDJ 处于金叉状态（K>D）且 K 值不在高位（≤ KDJ_K_MAX）"""
    k, d = daily_out.iloc[-1]["kdj_k"], daily_out.iloc[-1]["kdj_d"]
    if pd.isna(k) or pd.isna(d): return True
    return float(k) > float(d) and float(k) <= config.KDJ_K_MAX

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

def evaluate(daily_df: Optional[pd.DataFrame], code: str = "", name: str = "", config: Optional[StrategyConfig] = None, market_env: Optional[dict] = None, fund_data: Optional[dict] = None) -> tuple[Optional[Signal], str]:
    if config is None: config = StrategyConfig()
    regime = (market_env or {}).get("regime", "unknown")
    grade_boost = config.BEAR_GRADE_BOOST if regime == "bear" else 0.0

    if not check_fundamentals(fund_data, config, code=code, name=name): return None, "FAIL_FUND"

    daily_out = compute_daily_signals(daily_df, config)
    if daily_out is None or daily_out.empty: return None, "FAIL_DATA"

    d_last = daily_out.iloc[-1]

    # 防追高否决：当日涨幅过大（涨停买不进、大阳线次日易回调），数据缺失时放行不误杀
    pct_chg_today = d_last.get("pct_chg")
    if pct_chg_today is not None and not pd.isna(pct_chg_today) and float(pct_chg_today) > config.MAX_ENTRY_PCT_CHG:
        return None, "FAIL_CHASE"
    # 防跳空否决：开盘相对前收跳空高开过多，追买风险大
    open_today = d_last.get("open")
    prev_close = float(daily_out.iloc[-2]["close"]) if len(daily_out) >= 2 else None
    if (open_today is not None and not pd.isna(open_today) and prev_close and prev_close > 0
            and (float(open_today) / prev_close - 1) * 100 > config.MAX_GAP_UP_PCT):
        return None, "FAIL_GAP"

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
    if high_n > 0 and (high_n - last_close) / high_n < config.MIN_DRAWDOWN_FROM_HIGH:
        return None, "FAIL_NOT_BOTTOM"

    # 严格确认指标：低位/动能/KDJ 三重确认，任一不过即否决
    if not _range_position_ok(daily_out, config): return None, "FAIL_POSITION"
    if config.REQUIRE_MACD_MOMENTUM and not _macd_momentum_ok(daily_out, config): return None, "FAIL_MACD_MOM"
    if config.REQUIRE_KDJ_GOLDEN and not _kdj_ok(daily_out, config): return None, "FAIL_KDJ"

    daily_score = float(d_last.get("daily_score", 0))
    has_div = bool(d_last.get("bottom_divergence", False))
    # 准入门槛：按基础评级（分数扣除熊市加码后定级）判断，默认须达 B 级（≥60 分），
    # 即趋势转折之外还需至少一个确认信号；设为 "C" 可恢复旧行为
    base_grade = _grade_from_score(daily_score - grade_boost, config)
    min_grade = config.MIN_PASS_GRADE if config.MIN_PASS_GRADE in _GRADE_ORDER else "B"
    if _GRADE_ORDER.index(base_grade) < _GRADE_ORDER.index(min_grade):
        return None, "FAIL_TECH"
    # 底背离：通过准入后评级提升一档（与基础分脱钩），仅用于展示/排序，不能绕过准入门槛
    grade = _lift_grade(base_grade, config.DIVERGENCE_GRADE_LIFT) if has_div and config.DIVERGENCE_GRADE_LIFT > 0 else base_grade

    atr_val = d_last.get("atr")
    atr_val = float(atr_val) if atr_val is not None and not pd.isna(atr_val) else None

    rr = compute_risk_reward(entry_price=last_close, config=config, atr=atr_val)
    if not rr["passes"]: return None, "FAIL_RR"

    sig = Signal(
        code=code, name=name, date=pd.to_datetime(d_last["date"]).strftime("%Y-%m-%d"),
        close=round(last_close, 2), score=round(daily_score, 1), grade=grade,
        daily_score=round(daily_score, 1), rsi=round(float(d_last.get("rsi14", 50)), 1),
        rsi7=round(float(d_last.get("rsi7", 50)), 1), rsi21=round(float(d_last.get("rsi21", 50)), 1),
        vol_ratio=round(float(d_last.get("daily_vol_ratio", 0)), 2),
        turnover_ratio=round(float(d_last.get("daily_turnover_ratio", 0)), 2),
        stop_loss=rr["stop_loss"], take_profit=rr["take_profit"], rr_ratio=rr["rr_ratio"],
        market_env=regime, has_divergence=has_div
    )
    return sig, "PASS"

def get_market_environment(config: StrategyConfig, cache: CacheManager) -> dict:
    if (cached := cache.get("market_env")) is not None: return cached
    df_index = get_index_daily(config, cache)
    result = compute_market_environment(df_index, config) if df_index is not None and not df_index.empty else {"regime": "unknown", "description": "数据获取失败", "ma20": 0, "slope": 0, "close": 0}
    cache.set("market_env", result)
    return result

# ===========================================================================
# 编排函数
# ===========================================================================

def main(config: Optional[StrategyConfig] = None, cache: Optional[CacheManager] = None) -> Optional[pd.DataFrame]:
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

        stock_list = get_stock_list(config, cache)
        if not stock_list:
            print("[WARN] 无法获取股票列表")
            return None
        print(f"[INFO] 待筛选股票数: {len(stock_list)}")

        signals: list[dict] = []
        processed, total = 0, len(stock_list)
        stats = {"total": total, "error": 0, "fail_data": 0, "fail_fund": 0, "fail_tech": 0, "fail_rr": 0, "pass": 0}

        def _screen_one(stock: dict) -> tuple[Optional[Signal], str]:
            code, name = stock["code"], stock["name"]
            try:
                daily_df = get_daily_data(code, config, cache)
                if daily_df is None: return None, "FAIL_DATA"
                fund_data = get_fundamentals(code, cache, config)
                return evaluate(daily_df, code, name, config, market_env, fund_data)
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
                # FAIL_CHASE（追高）/ FAIL_GAP（跳空）/ FAIL_RSI_HIGH（RSI过高）/ FAIL_CLIMAX_VOL（天量）/ FAIL_NOT_BOTTOM（非底部区域）
                # / FAIL_POSITION（区间位置偏高）/ FAIL_MACD_MOM（动能未改善）/ FAIL_KDJ（KDJ未金叉）均属技术面入场质量层
                elif reason in ("FAIL_TECH", "FAIL_CHASE", "FAIL_GAP", "FAIL_RSI_HIGH", "FAIL_CLIMAX_VOL",
                                "FAIL_NOT_BOTTOM", "FAIL_POSITION", "FAIL_MACD_MOM", "FAIL_KDJ"): stats["fail_tech"] += 1
                elif reason == "FAIL_RR": stats["fail_rr"] += 1
                elif reason == "ERROR": stats["error"] += 1

        pass_data = stats["total"] - stats["fail_data"] - stats["error"]
        pass_fund = pass_data - stats["fail_fund"]
        pass_tech = pass_fund - stats["fail_tech"]
        pass_rr = pass_tech - stats["fail_rr"]

        print("\n" + "="*50)
        print("📊 选股漏斗数据分析 (Funnel Log)")
        print("="*50)
        print(f"1. 初始有效股票池: {stats['total']} 只")
        print(f"2. 获取数据并达标: {pass_data} 只 (淘汰/缺失 {stats['fail_data'] + stats['error']} 只)")
        if pass_data > 0: print(f"3. 基本面防雷通过: {pass_fund} 只 (淘汰 {stats['fail_fund']} 只，通过率 {pass_fund/pass_data*100:.1f}%)")
        if pass_fund > 0: print(f"4. 日线技术面达标: {pass_tech} 只 (淘汰 {stats['fail_tech']} 只，通过率 {pass_tech/pass_fund*100:.1f}%)")
        if pass_tech > 0: print(f"5. 盈亏风控比达标: {pass_rr} 只 (淘汰 {stats['fail_rr']} 只，通过率 {pass_rr/pass_tech*100:.1f}%)")
        print("="*50 + "\n")

        if not signals:
            print("[INFO] 未发现符合条件的信号")
            return None

        df = pd.DataFrame(signals).sort_values(SORT_BY, ascending=SORT_ASC).reset_index(drop=True)

        # 决赛圈周线确认：按评分降序逐个拉周线确认，取满 MAX_PICKS 即止（只对决赛圈拉取，成本可控）
        if config.REQUIRE_WEEKLY_TREND and not df.empty:
            confirmed = []
            weekly_checked = 0
            for _, row in df.iterrows():
                if len(confirmed) >= config.MAX_PICKS: break
                weekly_checked += 1
                wk = _fetch_weekly_dual(row["code"], config)
                weekly_ok = check_weekly_trend(wk, config)
                if weekly_ok and config.REQUIRE_WEEKLY_MACD_STABLE:
                    weekly_ok = check_weekly_macd(wk, config)
                if weekly_ok:
                    confirmed.append(row)
                time.sleep(config.FETCH_DELAY)
            weekly_dropped = weekly_checked - len(confirmed)
            if weekly_dropped > 0:
                conds = [f"周线MA{config.WEEKLY_MA_PERIOD}" + ("站上且上行" if config.WEEKLY_MA_BOTH_REQUIRED else "站上或上行")]
                if config.REQUIRE_WEEKLY_MACD_STABLE: conds.append("周线MACD企稳")
                print(f"[INFO] 周线确认：检查 {weekly_checked} 只，淘汰 {weekly_dropped} 只（未满足 {' + '.join(conds)}）")
            df = pd.DataFrame(confirmed).reset_index(drop=True) if confirmed else df.iloc[0:0]

        # 推荐数量上限：评分降序截取前 MAX_PICKS 只（目标每日 3~5 只精推，其余评分靠后的不推荐）
        if len(df) > config.MAX_PICKS:
            print(f"[INFO] 通过 {len(df)} 只，按评分截取前 {config.MAX_PICKS} 只（淘汰 {len(df) - config.MAX_PICKS} 只低分信号）")
            df = df.head(config.MAX_PICKS).reset_index(drop=True)
        print(f"[INFO] 筛选完成，最终推荐 {len(df)} 只股票")
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