"""
精简版日线选股策略（含底背离增强与漏斗日志版）

仅使用日线数据进行选股，简化策略逻辑：
1. 市场环境过滤（沪深300日线MA20斜率）
2. 基本面防雷（ROE/负债率/商誉/扣非利润否决）
3. 日线技术指标筛选（底背离 + MA5拐头 + EMA金叉 + RSI超卖反弹 + 量价配合）
4. 风险收益比过滤（固定止损止盈）

数据层：单一 AkShare 数据源、统一列名映射、带退避重试、
磁盘 CSV 缓存（行情按交易日失效 / 列表按 TTL 天失效）、股票池过滤由 config 开关驱动。
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

import akshare as ak

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_MODULE_DIR)

# ===========================================================================
# 模块级元数据
# ===========================================================================

DISPLAY_COLS = [
    ("code", "代码"),
    ("name", "名称"),
    ("date", "日期"),
    ("close", "收盘"),
    ("score", "评分"),
    ("grade", "等级"),
    ("daily_score", "日线分"),
    ("rsi", "RSI"),
    ("rsi7", "RSI7"),
    ("rsi21", "RSI21"),
    ("vol_ratio", "量比"),
    ("turnover_ratio", "换手比"),
    ("stop_loss", "止损"),
    ("take_profit", "止盈"),
    ("rr_ratio", "收益比"),
    ("market_env", "市场"),
    ("has_divergence", "底背离"),
]

TITLE = "日线技术指标选股结果"
PREFIX = "bf"
SORT_BY = ["score"]
SORT_ASC = [False]

# ===========================================================================
# StrategyConfig — 所有参数集中外置
# ===========================================================================

@dataclass
class StrategyConfig:
    """全部策略参数，带默认值。"""

    # --- Layer 1: 市场环境 ---
    CSI300_AK_SYMBOL: str = "sh000300"
    MARKET_MA_PERIOD: int = 20
    MARKET_SLOPE_LOOKBACK: int = 4
    MARKET_BULL_SLOPE: float = 0.01
    MARKET_BEAR_SLOPE: float = -0.01

    # --- Layer 2: 基本面 ---
    MIN_ROE: float = 5.0
    MAX_DEBT_RATIO: float = 70.0
    MAX_GOODWILL_RATIO: float = 20.0
    MIN_DEDUCTED_PROFIT_RATIO: float = 0.5

    # --- Layer 3: 日线技术指标 ---
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
    DIVERGENCE_LOOKBACK: int = 20  # 寻找前期低点的回看周期

    # --- Layer 4: 风控 ---
    FIXED_STOP_LOSS_PCT: float = 5.0
    FIXED_TAKE_PROFIT_PCT: float = 10.0
    MIN_RR_RATIO: float = 1.5
    ATR_PERIOD: int = 14
    ATR_STOP_MULT: float = 2.0
    USE_ATR_STOP: bool = True

    # --- 过滤 ---
    MIN_AMOUNT: float = 5_000_000.0
    MIN_DAYS: int = 60

    # --- 评分权重（日线最高100） ---
    W_DAILY_MA_TURN: float = 25.0
    W_DAILY_EMA_CROSS: float = 25.0
    W_DAILY_RSI_REBOUND: float = 25.0
    W_DAILY_VOL_PRICE: float = 25.0
    DAILY_MULTI_RESONANCE_BONUS: float = 10.0
    DAILY_RSI_OVERBOUGHT_PENALTY: float = 3.0
    W_DIVERGENCE_BONUS: float = 30.0  # 底背离极强信号加分

    # --- 等级阈值 ---
    GRADE_A: float = 80.0
    GRADE_B: float = 60.0
    GRADE_C: float = 40.0
    BEAR_GRADE_BOOST: float = 10.0

    # --- 基础设施 ---
    CACHE_EXPIRE_HOURS: float = 4.0
    MAX_WORKERS: int = 4
    DAILY_BARS: int = 120
    WEEKLY_BARS: int = 60
    FETCH_DELAY: float = 0.05

    # --- 数据获取 ---
    ADJUST: str = "qfq"
    USE_CACHE: bool = True
    CACHE_DIR: str = os.path.join(_PROJECT_ROOT, "cache")
    CACHE_TTL_DAYS: float = 6.0
    FUND_CACHE_TTL_DAYS: float = 7.0
    FUND_START_YEAR: str = "2023"
    MAX_RETRY: int = 2
    LIST_MAX_RETRY: int = 4

    # --- 股票池过滤开关 ---
    FILTER_ST: bool = True
    EXCLUDE_DELISTING: bool = True
    EXCLUDE_BSE: bool = True
    EXCLUDE_CHINEXT: bool = False
    EXCLUDE_STAR: bool = False

# ===========================================================================
# CacheManager — 内存TTL缓存
# ===========================================================================

class CacheManager:
    def __init__(self, expire_hours: float = 4.0):
        self._expire_seconds = expire_hours * 3600
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        if key in self._store:
            ts, val = self._store[key]
            if time.time() - ts < self._expire_seconds:
                return val
            del self._store[key]
        return None

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)

    def clear(self) -> None:
        self._store.clear()

# ===========================================================================
# 数据提供层 — AkShare
# ===========================================================================

def _ak_code_to_symbol(code: str) -> str:
    c = code.replace("sh.", "").replace("sz.", "").replace("bj.", "")
    return c.zfill(6)

_COLUMN_MAP = {
    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
    "最低": "low", "成交量": "volume", "成交额": "amount", "振幅": "amplitude",
    "涨跌幅": "pct_chg", "涨跌额": "change", "换手率": "turnover",
}
_NUMERIC_COLS = ("open", "close", "high", "low", "volume", "amount", "amplitude", "pct_chg", "change", "turnover")
_INDEX_NUMERIC_COLS = ("open", "high", "low", "close", "volume")
_DATE_FMT = "%Y-%m-%d"

def _fetch_with_retry(fetcher: Callable[[], Optional[pd.DataFrame]], max_retry: int, label: str, retry_on_empty: bool = False) -> Optional[pd.DataFrame]:
    last_err: Optional[Exception] = None
    for attempt in range(max_retry + 1):
        try:
            raw = fetcher()
            if raw is not None and not raw.empty:
                return raw
            if not retry_on_empty:
                return None
            last_err = RuntimeError(f"{label} 返回空")
        except Exception as e:
            last_err = e
        if attempt < max_retry:
            time.sleep(0.5 * (attempt + 1))
    logging.debug("%s 获取失败（已重试 %d 次）: %s", label, max_retry, last_err)
    return None

def _cache_path(config: StrategyConfig, name: str) -> str:
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    return os.path.join(config.CACHE_DIR, name)

def _cache_fresh(path: str, ttl_days: float) -> bool:
    if not os.path.exists(path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return datetime.now() - mtime < timedelta(days=ttl_days)

def _cache_fresh_today(path: str) -> bool:
    if not os.path.exists(path):
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return mtime.date() == datetime.now().date()

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
    except Exception as e:
        logging.debug("写缓存失败 %s: %s", path, e)

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

def _normalize_hist(raw: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    if raw is None or raw.empty:
        return None
    df = raw.rename(columns=_COLUMN_MAP)
    keep = [c for c in _COLUMN_MAP.values() if c in df.columns]
    if "date" not in keep or "close" not in keep:
        return None
    df = df[keep].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime(_DATE_FMT)
    for col in _NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    if df.empty:
        return None
    return df.sort_values("date").reset_index(drop=True)

def _fetch_hist(code: str, period: str, start: str, end: str, config: StrategyConfig, cache_name: Optional[str] = None) -> Optional[pd.DataFrame]:
    symbol = _ak_code_to_symbol(code)
    path = ""
    if config.USE_CACHE:
        name = cache_name or f"{period}_{symbol}_{start}_{end}_{config.ADJUST or 'none'}.csv"
        path = _cache_path(config, name)
        if _cache_fresh_today(path):
            cached = _read_cache_csv(path)
            if cached is not None:
                return cached
    raw = _fetch_with_retry(
        lambda: ak.stock_zh_a_hist(symbol=symbol, period=period, start_date=start, end_date=end, adjust=config.ADJUST),
        config.MAX_RETRY, f"stock_zh_a_hist({symbol},{period})"
    )
    df = _normalize_hist(raw)
    if df is not None and path:
        _write_cache_csv(df, path)
    return df

def _window_dates(bars: int, unit: str) -> tuple[str, str]:
    end = datetime.now()
    delta = timedelta(weeks=bars) if unit == "weeks" else timedelta(days=bars)
    return (end - delta).strftime("%Y%m%d"), end.strftime("%Y%m%d")

def _window_cache_name(period: str, symbol: str, bars: int, config: StrategyConfig) -> str:
    return f"{period}_{symbol}_last{bars}_{config.ADJUST or 'none'}.csv"

def _fetch_weekly_akshare(code: str, weeks: int = 60, config: Optional[StrategyConfig] = None) -> Optional[pd.DataFrame]:
    config = config or StrategyConfig()
    start, end = _window_dates(weeks, "weeks")
    name = _window_cache_name("weekly", _ak_code_to_symbol(code), weeks, config)
    return _fetch_hist(code, "weekly", start, end, config, cache_name=name)

def _fetch_daily_akshare(code: str, days: int = 120, config: Optional[StrategyConfig] = None) -> Optional[pd.DataFrame]:
    config = config or StrategyConfig()
    start, end = _window_dates(days, "days")
    name = _window_cache_name("daily", _ak_code_to_symbol(code), days, config)
    return _fetch_hist(code, "daily", start, end, config, cache_name=name)

def _fetch_index_daily_raw(symbol: str, config: StrategyConfig) -> Optional[pd.DataFrame]:
    path = ""
    if config.USE_CACHE:
        path = _cache_path(config, f"index_daily_{symbol}.csv")
        if _cache_fresh_today(path):
            cached = _read_cache_csv(path)
            if cached is not None:
                return cached
    raw = _fetch_with_retry(
        lambda: ak.stock_zh_index_daily(symbol=symbol),
        config.MAX_RETRY, f"stock_zh_index_daily({symbol})", retry_on_empty=True
    )
    if raw is None:
        return None
    df = raw.copy()
    if "date" not in df.columns or "close" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime(_DATE_FMT)
    for col in _INDEX_NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    if df.empty:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    if path:
        _write_cache_csv(df, path)
    return df

def fetch_index_weekly_range(symbol: str, start: str, end: str, config: Optional[StrategyConfig] = None) -> Optional[pd.DataFrame]:
    config = config or StrategyConfig()
    df = _fetch_index_daily_raw(symbol, config)
    if df is None: return None
    dt = pd.to_datetime(df["date"])
    sub = df[(dt >= pd.to_datetime(start)) & (dt <= pd.to_datetime(end))].copy()
    if sub.empty: return None
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    agg = {k: v for k, v in agg.items() if k in sub.columns}
    sub["_dt"] = pd.to_datetime(sub["date"])
    weekly = sub.set_index("_dt").resample("W").agg(agg).dropna(subset=["close"]).reset_index()
    weekly["date"] = weekly["_dt"].dt.strftime(_DATE_FMT)
    return weekly.drop(columns=["_dt"])

def _fetch_index_weekly_akshare(symbol: str = "sh000300", weeks: int = 60, config: Optional[StrategyConfig] = None) -> Optional[pd.DataFrame]:
    config = config or StrategyConfig()
    start, end = _window_dates(weeks, "weeks")
    return fetch_index_weekly_range(symbol, start, end, config)

def _fetch_fundamentals_akshare(code: str, config: Optional[StrategyConfig] = None) -> Optional[dict]:
    config = config or StrategyConfig()
    symbol = _ak_code_to_symbol(code)
    path = ""
    if config.USE_CACHE:
        path = _cache_path(config, f"fund_{symbol}.json")
        if _cache_fresh(path, config.FUND_CACHE_TTL_DAYS):
            cached = _read_cache_json(path)
            if cached is not None: return cached

    result: dict[str, Optional[float]] = {"roe": None, "debt_ratio": None, "goodwill_ratio": None, "deducted_profit_ratio": None}
    
    df_fin = _fetch_with_retry(lambda: ak.stock_financial_analysis_indicator(symbol=symbol, start_year=config.FUND_START_YEAR), config.MAX_RETRY, f"fund({symbol})")
    if df_fin is not None:
        row = df_fin.iloc[0]
        for col in df_fin.columns:
            col_str, col_lower = str(col), str(col).lower()
            if "净资产收益率" in col_str or "roe" in col_lower:
                val = pd.to_numeric(row[col], errors="coerce")
                if not pd.isna(val): result["roe"] = float(val)
            if "资产负债率" in col_str or "debt" in col_lower:
                val = pd.to_numeric(row[col], errors="coerce")
                if not pd.isna(val): result["debt_ratio"] = float(val)

    df_bs = _fetch_with_retry(lambda: ak.stock_balance_sheet_by_report_em(symbol=symbol), config.MAX_RETRY, f"bs({symbol})")
    if df_bs is not None:
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

    df_income = _fetch_with_retry(lambda: ak.stock_profit_sheet_by_report_em(symbol=symbol), config.MAX_RETRY, f"income({symbol})")
    if df_income is not None:
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

def _fetch_stock_list_raw(config: StrategyConfig) -> Optional[pd.DataFrame]:
    raw = _fetch_with_retry(lambda: ak.stock_info_a_code_name(), config.LIST_MAX_RETRY, "stock_info", retry_on_empty=True)
    if raw is None or "code" not in raw.columns or "name" not in raw.columns: return None
    df = raw[["code", "name"]].copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["name"] = df["name"].astype(str)
    return df.reset_index(drop=True)

def _apply_universe_filters(df: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    out = df.copy()
    out["code"] = out["code"].astype(str).str.zfill(6)
    out["name"] = out["name"].astype(str)
    if config.EXCLUDE_BSE: out = out[~out["code"].str.startswith(("8", "4"))]
    if config.EXCLUDE_CHINEXT: out = out[~out["code"].str.startswith(("300", "301"))]
    if config.EXCLUDE_STAR: out = out[~out["code"].str.startswith(("688", "689"))]
    if config.FILTER_ST: out = out[~out["name"].str.contains("ST", case=False, na=False)]
    if config.EXCLUDE_DELISTING: out = out[~out["name"].str.contains("退", na=False)]
    return out.reset_index(drop=True)

def _fetch_stock_pool_akshare(config: Optional[StrategyConfig] = None) -> list[dict]:
    config = config or StrategyConfig()
    path = ""
    df: Optional[pd.DataFrame] = None
    if config.USE_CACHE:
        path = _cache_path(config, "stock_list.csv")
        if _cache_fresh(path, config.CACHE_TTL_DAYS):
            df = _read_cache_csv(path, dtype={"code": str, "name": str})
            if df is not None and ("code" not in df.columns or "name" not in df.columns): df = None
    if df is None:
        df = _fetch_stock_list_raw(config)
        if df is None: return []
        if path: _write_cache_csv(df, path)
    return _apply_universe_filters(df, config).to_dict("records")

# ===========================================================================
# 统一数据接口
# ===========================================================================

def get_weekly_data(code: str, config: StrategyConfig, cache: Optional[CacheManager] = None) -> Optional[pd.DataFrame]:
    cache_key = f"weekly_{code}"
    if cache and (cached := cache.get(cache_key)) is not None: return cached
    df = _fetch_weekly_akshare(code, weeks=config.WEEKLY_BARS, config=config)
    if cache and df is not None: cache.set(cache_key, df)
    return df

def get_daily_data(code: str, config: StrategyConfig, cache: Optional[CacheManager] = None) -> Optional[pd.DataFrame]:
    cache_key = f"daily_{code}"
    if cache and (cached := cache.get(cache_key)) is not None: return cached
    df = _fetch_daily_akshare(code, days=config.DAILY_BARS, config=config)
    if cache and df is not None: cache.set(cache_key, df)
    return df

def get_index_daily(config: StrategyConfig, cache: Optional[CacheManager] = None) -> Optional[pd.DataFrame]:
    cache_key = "index_daily_csi300"
    if cache and (cached := cache.get(cache_key)) is not None: return cached
    df = _fetch_index_daily_raw(config.CSI300_AK_SYMBOL, config)
    if df is not None and len(df) > config.DAILY_BARS:
        df = df.tail(config.DAILY_BARS).reset_index(drop=True)
    if cache and df is not None: cache.set(cache_key, df)
    return df

def get_index_weekly(config: StrategyConfig, cache: Optional[CacheManager] = None) -> Optional[pd.DataFrame]:
    cache_key = "index_weekly_csi300"
    if cache and (cached := cache.get(cache_key)) is not None: return cached
    df = _fetch_index_weekly_akshare(symbol=config.CSI300_AK_SYMBOL, weeks=config.WEEKLY_BARS, config=config)
    if cache and df is not None: cache.set(cache_key, df)
    return df

def get_fundamentals(code: str, cache: Optional[CacheManager] = None, config: Optional[StrategyConfig] = None) -> Optional[dict]:
    cache_key = f"fund_{code}"
    if cache and (cached := cache.get(cache_key)) is not None: return cached
    data = _fetch_fundamentals_akshare(code, config)
    if cache and data is not None: cache.set(cache_key, data)
    return data

def get_stock_list(config: StrategyConfig, cache: Optional[CacheManager] = None) -> list[dict]:
    cache_key = "stock_list"
    if cache and (cached := cache.get(cache_key)) is not None: return cached
    stocks = _fetch_stock_pool_akshare(config)
    if cache and stocks: cache.set(cache_key, stocks)
    return stocks

# ===========================================================================
# Layer 1 & 2
# ===========================================================================

def compute_market_environment(df_index: pd.DataFrame, config: StrategyConfig) -> dict:
    if df_index is None or df_index.empty or len(df_index) < config.MARKET_MA_PERIOD + config.MARKET_SLOPE_LOOKBACK:
        return {"regime": "unknown", "description": "数据不足", "ma20": 0, "slope": 0, "close": 0}
    df = df_index.copy().reset_index(drop=True)
    df["ma"] = df["close"].rolling(config.MARKET_MA_PERIOD).mean()
    cur, prev = df.iloc[-1], df.iloc[-(1 + config.MARKET_SLOPE_LOOKBACK)]
    ma_now, ma_prev, close_now = float(cur["ma"]), float(prev["ma"]), float(cur["close"])
    slope = (ma_now - ma_prev) / ma_prev if ma_prev > 0 else 0.0
    if slope > config.MARKET_BULL_SLOPE:
        regime, desc = "bull", f"偏多（MA20斜率 {slope:.4f}，沪深300收于 {close_now:.0f}）"
    elif slope < config.MARKET_BEAR_SLOPE:
        regime, desc = "bear", f"偏空（MA20斜率 {slope:.4f}，沪深300收于 {close_now:.0f}）"
    else:
        regime, desc = "neutral", f"中性（MA20斜率 {slope:.4f}，沪深300收于 {close_now:.0f}）"
    return {"regime": regime, "description": desc, "ma20": round(ma_now, 2), "slope": round(slope, 6), "close": round(close_now, 2)}

def check_fundamentals(fund_data: Optional[dict], config: StrategyConfig) -> bool:
    if fund_data is None: return True
    if (roe := fund_data.get("roe")) is not None and roe < config.MIN_ROE: return False
    if (debt := fund_data.get("debt_ratio")) is not None and debt > config.MAX_DEBT_RATIO: return False
    if (goodwill := fund_data.get("goodwill_ratio")) is not None and goodwill > config.MAX_GOODWILL_RATIO: return False
    if (deducted := fund_data.get("deducted_profit_ratio")) is not None and deducted < config.MIN_DEDUCTED_PROFIT_RATIO: return False
    return True

# ===========================================================================
# Layer 3: 日线技术指标信号
# ===========================================================================

_DAILY_NEED_COLS = {"close", "volume", "date", "high", "low", "pct_chg", "turnover"}

def compute_daily_signals(df: pd.DataFrame, config: StrategyConfig) -> Optional[pd.DataFrame]:
    if df is None or df.empty or not _DAILY_NEED_COLS.issubset(df.columns) or len(df) < config.MIN_DAYS:
        return None

    out = df.copy().reset_index(drop=True)

    out["ma5"] = out["close"].rolling(config.DAILY_MA5).mean()
    out["ma10"] = out["close"].rolling(config.DAILY_MA10).mean()
    out["ema5"] = out["close"].ewm(span=config.DAILY_EMA5, adjust=False).mean()
    out["ema10"] = out["close"].ewm(span=config.DAILY_EMA10, adjust=False).mean()
    out["ema20"] = out["close"].ewm(span=config.DAILY_EMA20, adjust=False).mean()

    # RSI 多周期
    for p, col in [(config.DAILY_RSI_PERIOD, "rsi14"), (config.DAILY_RSI_SHORT_PERIOD, "rsi7"), (config.DAILY_RSI_LONG_PERIOD, "rsi21")]:
        delta = out["close"].diff()
        gain, loss = delta.clip(lower=0), (-delta).clip(lower=0)
        rs = gain.ewm(com=p-1, min_periods=p).mean() / loss.ewm(com=p-1, min_periods=p).mean().replace(0, np.nan)
        out[col] = 100 - (100 / (1 + rs))

    # MACD
    exp1 = out["close"].ewm(span=config.DAILY_MACD_FAST, adjust=False).mean()
    exp2 = out["close"].ewm(span=config.DAILY_MACD_SLOW, adjust=False).mean()
    out["macd_diff"] = exp1 - exp2
    out["macd_dea"] = out["macd_diff"].ewm(span=config.DAILY_MACD_SIGNAL, adjust=False).mean()
    out["macd_histogram"] = out["macd_diff"] - out["macd_dea"]

    # ATR
    if {"high", "low"}.issubset(out.columns):
        prev_close = out["close"].shift(1)
        tr = pd.concat([out["high"] - out["low"], (out["high"] - prev_close).abs(), (out["low"] - prev_close).abs()], axis=1).max(axis=1)
        out["atr"] = tr.ewm(com=config.ATR_PERIOD - 1, min_periods=config.ATR_PERIOD, adjust=False).mean()
    else:
        out["atr"] = np.nan

    out["daily_vol_base"] = out["volume"].shift(1).rolling(20).mean()
    out["daily_vol_ratio"] = out["volume"] / out["daily_vol_base"].replace(0, np.nan)

    if "turnover" in out.columns:
        out["daily_to_base"] = out["turnover"].shift(1).rolling(config.DAILY_TURNOVER_LOOKBACK).mean()
        out["daily_turnover_ratio"] = out["turnover"] / out["daily_to_base"].replace(0, np.nan)
    else:
        out["daily_turnover_ratio"] = np.nan

    # ------ 信号条件 ------
    ma5_slope = out["ma5"] - out["ma5"].shift(1)
    out["ma5_turn"] = (ma5_slope > 0) & (ma5_slope.shift(1) <= 0)
    out["ema_golden_cross"] = (out["ema5"] > out["ema10"]) & (out["ema5"].shift(1) <= out["ema10"].shift(1))
    out["macd_golden_cross"] = (out["macd_diff"] > out["macd_dea"]) & (out["macd_diff"].shift(1) <= out["macd_dea"].shift(1))
    
    rsi_was_oversold = (out["rsi14"].shift(1) < config.DAILY_RSI_OVERSOLD) | (out["rsi14"].shift(2) < config.DAILY_RSI_OVERSOLD)
    out["rsi_rebound"] = rsi_was_oversold & (out["rsi14"] >= config.DAILY_RSI_REBOUND_MIN)
    out["rsi_multi_res"] = (out["rsi7"] > out["rsi14"]) & (out["rsi14"] > out["rsi21"]) & (out["rsi7"] < config.DAILY_RSI_OVERBOUGHT) & (out["rsi21"] > config.DAILY_RSI_OVERSOLD)

    # 🔥 新增：MACD / RSI 底背离检测
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
    turnover_expand = out["daily_turnover_ratio"] >= config.DAILY_VOL_EXPAND if "daily_turnover_ratio" in out.columns else True
    out["vol_price_coord"] = price_up & vol_expand & turnover_expand
    out["multi_resonance"] = out["rsi_multi_res"] & out["macd_golden_cross"] & out["vol_price_coord"]

    # ------ 评分 ------
    out["daily_score"] = (
        out["ma5_turn"].astype(float) * config.W_DAILY_MA_TURN +
        out["ema_golden_cross"].astype(float) * config.W_DAILY_EMA_CROSS +
        out["rsi_rebound"].astype(float) * config.W_DAILY_RSI_REBOUND +
        out["vol_price_coord"].astype(float) * config.W_DAILY_VOL_PRICE +
        out["multi_resonance"].astype(float) * config.DAILY_MULTI_RESONANCE_BONUS +
        (out["rsi14"] >= config.DAILY_RSI_OVERBOUGHT).astype(float) * (-config.DAILY_RSI_OVERBOUGHT_PENALTY) +
        out["bottom_divergence"].astype(float) * config.W_DIVERGENCE_BONUS
    ).fillna(0).clip(0, 100).round(1)

    return out

# ===========================================================================
# Layer 4: 风险收益比
# ===========================================================================

def compute_risk_reward(entry_price: float, config: StrategyConfig, atr: Optional[float] = None) -> dict:
    if config.USE_ATR_STOP and atr is not None and atr > 0:
        stop_loss = entry_price - config.ATR_STOP_MULT * atr
    else:
        stop_loss = entry_price * (1 - config.FIXED_STOP_LOSS_PCT / 100)
    take_profit = entry_price * (1 + config.FIXED_TAKE_PROFIT_PCT / 100)
    risk = entry_price - stop_loss
    reward = take_profit - entry_price
    rr_ratio = reward / risk if risk > 0 else 0.0
    return {"stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2), "rr_ratio": round(rr_ratio, 2), "passes": rr_ratio >= config.MIN_RR_RATIO}

# ===========================================================================
# Signal dataclass + describe()
# ===========================================================================

@dataclass
class Signal:
    code: str
    name: str
    date: str
    close: float
    score: float
    grade: str
    daily_score: float
    rsi: float
    rsi7: float
    rsi21: float
    vol_ratio: float
    turnover_ratio: float
    stop_loss: float
    take_profit: float
    rr_ratio: float
    market_env: str
    has_divergence: bool

    def to_dict(self) -> dict:
        return asdict(self)

def _grade_from_score(score: float, config: StrategyConfig) -> str:
    if score >= config.GRADE_A: return "A"
    elif score >= config.GRADE_B: return "B"
    elif score >= config.GRADE_C: return "C"
    return "D"

def describe(r: dict) -> str:
    div_alert = "🔥 **触发 MACD/RSI 底背离！(强烈看涨)**\n" if r.get('has_divergence') else ""
    return "\n".join([
        f"**{r['name']} {r['code']}**  {r['date']}  收盘 {r['close']}",
        f"综合评分 **{r['score']}** ({r['grade']}级) ｜ 日线 {r['daily_score']}分",
        "",
        div_alert,
        "**日线技术指标:**",
        f"- RSI7: {r['rsi7']}, RSI14: {r['rsi']}, RSI21: {r['rsi21']} ｜ 量比: {r['vol_ratio']}x",
        f"- 换手比: {r['turnover_ratio']}x ｜ 市场环境: {r['market_env']}",
        "",
        "**交易计划:**",
        f"- 止损: {r['stop_loss']} ｜ 止盈: {r['take_profit']} ｜ 风险收益比: {r['rr_ratio']}",
    ])

# ===========================================================================
# evaluate() & main()
# ===========================================================================

def evaluate(daily_df: Optional[pd.DataFrame], code: str = "", name: str = "", config: Optional[StrategyConfig] = None, market_env: Optional[dict] = None, fund_data: Optional[dict] = None) -> tuple[Optional[Signal], str]:
    if config is None: config = StrategyConfig()
    regime = (market_env or {}).get("regime", "unknown")
    grade_boost = config.BEAR_GRADE_BOOST if regime == "bear" else 0.0

    if not check_fundamentals(fund_data, config): return None, "FAIL_FUND"

    daily_out = compute_daily_signals(daily_df, config)
    if daily_out is None or daily_out.empty: return None, "FAIL_DATA"

    d_last = daily_out.iloc[-1]
    daily_score = float(d_last.get("daily_score", 0))
    
    grade = _grade_from_score(daily_score - grade_boost, config)
    if grade == "D": return None, "FAIL_TECH"

    last_close = float(d_last["close"])
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
        market_env=regime, has_divergence=bool(d_last.get("bottom_divergence", False))
    )
    return sig, "PASS"

def get_market_environment(config: StrategyConfig, cache: CacheManager) -> dict:
    if (cached := cache.get("market_env")) is not None: return cached
    df_index = get_index_daily(config, cache)
    result = compute_market_environment(df_index, config) if df_index is not None and not df_index.empty else {"regime": "unknown", "description": "数据获取失败", "ma20": 0, "slope": 0, "close": 0}
    cache.set("market_env", result)
    return result

def main(config: Optional[StrategyConfig] = None, cache: Optional[CacheManager] = None) -> Optional[pd.DataFrame]:
    if config is None: config = StrategyConfig()
    if cache is None: cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)

    market_env = get_market_environment(config, cache)
    print(f"[INFO] 市场环境: {market_env.get('description', 'unknown')}")

    stock_list = get_stock_list(config, cache)
    if not stock_list:
        print("[WARN] 无法获取股票列表")
        return None
    print(f"[INFO] 待筛选股票数: {len(stock_list)}")

    signals: list[dict] = []
    processed, total = 0, len(stock_list)
    
    stats = {
        "total": total, "error": 0, "fail_data": 0,
        "fail_fund": 0, "fail_tech": 0, "fail_rr": 0, "pass": 0
    }

    def _screen_one(stock: dict) -> tuple[Optional[Signal], str]:
        code, name = stock["code"], stock["name"]
        try:
            daily_df = get_daily_data(code, config, cache)
            if daily_df is None: return None, "FAIL_DATA"
            fund_data = get_fundamentals(code, cache, config)
            if config.FETCH_DELAY > 0: time.sleep(config.FETCH_DELAY)
            return evaluate(daily_df, code, name, config, market_env, fund_data)
        except Exception:
            return None, "ERROR"

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
            elif reason == "FAIL_TECH": stats["fail_tech"] += 1
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
    print(f"[INFO] 筛选完成，最终共 {len(df)} 只股票脱颖而出")
    return df

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode == "screen":
        result = main()
        print(result.to_string() if result is not None else "未发现信号")
    elif mode == "track":
        print("跟踪功能已移除")
    elif mode == "full":
        result = main()
        if result is not None: print(f"共 {len(result)} 只信号")
    else:
        print(f"未知模式: {mode}，可选: screen / full")