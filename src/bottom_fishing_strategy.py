"""
策略2：周线左侧寻底 + 日线右侧确认 — 5层量化选股系统

Layer 1: 市场环境过滤（沪深300周线MA20斜率）
Layer 2: 周线左侧寻底（ATR归一化跌幅 + 量能模式 + CCI超卖 + MACD底背离）
Layer 3: 基本面防雷（ROE/负债率/商誉/扣非利润否决）
Layer 4: 日线右侧确认（MA5拐头 + EMA金叉 + RSI超卖反弹 + 量价配合）
Layer 5: 风险收益比过滤（动态止损 + 止盈目标 + 最低1.5倍RR）

借鉴策略1(strategy.py)的架构：纯函数、向量化计算、参数外置、结构化输出。
借鉴策略1的逻辑：不破前低、换手率倍率、个股MA斜率、恐慌杀跌、成交额下限、次新剔除。

compute_weekly_signals() / compute_daily_signals() 为纯函数，便于单测与回测。
evaluate() 为单股实盘选股入口。
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# BaoStock 可选导入
# ---------------------------------------------------------------------------
try:
    import baostock as bs

    _BS_AVAILABLE = True
except ImportError:
    bs = None  # type: ignore[assignment]
    _BS_AVAILABLE = False

try:
    import akshare as ak

    _AK_AVAILABLE = True
except ImportError:
    ak = None  # type: ignore[assignment]
    _AK_AVAILABLE = False

# ===========================================================================
# 模块级元数据（沿用策略1模式，供 notify 层读取）
# ===========================================================================

DISPLAY_COLS = [
    ("code", "代码"),
    ("name", "名称"),
    ("date", "日期"),
    ("close", "收盘"),
    ("score", "评分"),
    ("grade", "等级"),
    ("weekly_score", "周线分"),
    ("daily_score", "日线分"),
    ("pct_chg_w", "周涨跌%"),
    ("atr_decline", "ATR跌幅"),
    ("cci", "CCI"),
    ("rsi", "RSI"),
    ("vol_ratio", "量比"),
    ("turnover_ratio", "换手比"),
    ("stop_loss", "止损"),
    ("take_profit", "止盈"),
    ("rr_ratio", "收益比"),
    ("market_env", "市场"),
]

TITLE = "左侧寻底+右侧确认选股结果"
PREFIX = "bf"
SORT_BY = ["score"]
SORT_ASC = [False]


# ===========================================================================
# StrategyConfig — 所有参数集中外置
# ===========================================================================


@dataclass
class StrategyConfig:
    """全部策略参数，带默认值。实例化后可覆盖任意字段用于回测调参。"""

    # --- Layer 1: 市场环境 ---
    CSI300_CODE: str = "sh.000300"
    CSI300_AK_SYMBOL: str = "sh000300"
    MARKET_MA_PERIOD: int = 20
    MARKET_SLOPE_LOOKBACK: int = 4
    MARKET_BULL_SLOPE: float = 0.01
    MARKET_BEAR_SLOPE: float = -0.01

    # --- Layer 2: 周线寻底 ---
    WEEKLY_ATR_PERIOD: int = 14
    WEEKLY_ATR_DECLINE_THRESHOLD: float = 1.5
    WEEKLY_CCI_PERIOD: int = 14
    WEEKLY_CCI_OVERSOLD: float = -100.0
    WEEKLY_MACD_FAST: int = 12
    WEEKLY_MACD_SLOW: int = 26
    WEEKLY_MACD_SIGNAL: int = 9
    WEEKLY_VOL_LOOKBACK: int = 10
    WEEKLY_VOL_SHRINK_RATIO: float = 0.7
    WEEKLY_VOL_EXPAND_RATIO: float = 1.5
    WEEKLY_MA_PERIOD: int = 20
    WEEKLY_SLOPE_LOOKBACK: int = 4

    # 借鉴策略1
    PANIC_PCT: float = -5.0
    PRIOR_LOW_LOOKBACK: int = 12
    TURNOVER_LOOKBACK: int = 10
    TURNOVER_RATIO_THRESHOLD: float = 1.5

    # --- Layer 3: 基本面 ---
    MIN_ROE: float = 5.0
    MAX_DEBT_RATIO: float = 70.0
    MAX_GOODWILL_RATIO: float = 20.0
    MIN_DEDUCTED_PROFIT_RATIO: float = 0.5

    # --- Layer 4: 日线确认 ---
    DAILY_MA5: int = 5
    DAILY_MA10: int = 10
    DAILY_EMA5: int = 5
    DAILY_EMA10: int = 10
    DAILY_EMA20: int = 20
    DAILY_RSI_PERIOD: int = 14
    DAILY_RSI_OVERSOLD: float = 30.0
    DAILY_RSI_REBOUND_MIN: float = 35.0
    DAILY_RSI_OVERBOUGHT: float = 70.0
    DAILY_VOL_EXPAND: float = 1.2
    DAILY_TURNOVER_LOOKBACK: int = 20

    # --- Layer 5: 风控 ---
    ATR_STOP_MULTIPLIER: float = 2.0
    TAKE_PROFIT_TARGET: str = "ma20"
    FIXED_TP_PCT: float = 15.0
    MIN_RR_RATIO: float = 1.5

    # --- 过滤（借鉴策略1） ---
    MIN_AMOUNT: float = 5_000_000.0
    MIN_WEEKS: int = 30
    MIN_DAYS: int = 60

    # --- 评分权重（周线最高70，日线最高30） ---
    W_ATR_DECLINE: float = 20.0
    W_CCI_OVERSOLD: float = 15.0
    W_VOLUME_PATTERN: float = 10.0
    W_MACD_DIVERGENCE: float = 10.0
    W_PRIOR_LOW_HOLD: float = 10.0
    W_PANIC_BONUS: float = 5.0
    W_DAILY_MA_TURN: float = 8.0
    W_DAILY_EMA_CROSS: float = 8.0
    W_DAILY_RSI_REBOUND: float = 7.0
    W_DAILY_VOL_PRICE: float = 7.0

    # --- 等级阈值 ---
    GRADE_A: float = 80.0
    GRADE_B: float = 60.0
    GRADE_C: float = 40.0
    BEAR_GRADE_BOOST: float = 10.0  # 熊市时各等级阈值上浮（收紧过滤）

    # --- 基础设施 ---
    CACHE_EXPIRE_HOURS: float = 4.0
    DATA_DIR: str = "data"
    SIGNAL_HISTORY_FILE: str = "signal_history.csv"
    TRACKING_FILE: str = "tracking_report.csv"
    STOCK_POOL: str = "all_a"
    MAX_WORKERS: int = 4
    WEEKLY_BARS: int = 60
    DAILY_BARS: int = 120
    FETCH_DELAY: float = 0.05


# ===========================================================================
# CacheManager — 内存TTL缓存
# ===========================================================================


class CacheManager:
    """带过期时间的内存缓存，避免同一运行周期内重复请求。"""

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
# 数据提供层 — BaoStock
# ===========================================================================


class BaoStockProvider:
    """BaoStock 单例包装，管理 login/logout 生命周期。"""

    _logged_in: bool = False

    @classmethod
    def login(cls) -> None:
        if not cls._logged_in and _BS_AVAILABLE:
            bs.login()
            cls._logged_in = True

    @classmethod
    def logout(cls) -> None:
        if cls._logged_in and _BS_AVAILABLE:
            try:
                bs.logout()
            except Exception:
                pass
            cls._logged_in = False

    @classmethod
    def _query(
        cls,
        code: str,
        start: str,
        end: str,
        frequency: str = "w",
        fields: str = "date,open,high,low,close,volume,amount,turn,pctChg",
    ) -> Optional[pd.DataFrame]:
        if not _BS_AVAILABLE:
            return None
        cls.login()
        rs = bs.query_history_k_data_plus(
            code, fields, start_date=start, end_date=end, frequency=frequency, adjustflag="2",
        )
        rows = []
        while (rs.error_code == "0") and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
        rename = {
            "turn": "turnover",
            "pctChg": "pct_chg",
        }
        df = df.rename(columns=rename)
        for col in ["open", "high", "low", "close", "volume", "amount", "turnover", "pct_chg"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        return df if not df.empty else None

    @classmethod
    def query_weekly(cls, code: str, start: str, end: str) -> Optional[pd.DataFrame]:
        return cls._query(code, start, end, frequency="w")

    @classmethod
    def query_daily(cls, code: str, start: str, end: str) -> Optional[pd.DataFrame]:
        return cls._query(code, start, end, frequency="d")

    @classmethod
    def query_index_weekly(cls, code: str, start: str, end: str) -> Optional[pd.DataFrame]:
        if not _BS_AVAILABLE:
            return None
        cls.login()
        rs = bs.query_history_k_data_plus(
            code,
            "date,open,high,low,close,volume,amount",
            start_date=start,
            end_date=end,
            frequency="w",
        )
        rows = []
        while (rs.error_code == "0") and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        return df if not df.empty else None


# ===========================================================================
# 数据提供层 — AkShare
# ===========================================================================


def _ak_code_to_symbol(code: str) -> str:
    """将6位数字代码转为 AkShare stock_zh_a_hist 所需的纯数字 symbol。"""
    c = code.replace("sh.", "").replace("sz.", "").replace("bj.", "")
    return c.zfill(6)


def _ak_code_to_bs(code: str) -> str:
    """将6位数字代码转为 BaoStock 格式 sh.XXXXXX / sz.XXXXXX。"""
    c = code.replace("sh.", "").replace("sz.", "").replace("bj.", "").zfill(6)
    if c.startswith(("6", "9")):
        return f"sh.{c}"
    return f"sz.{c}"


def _fetch_weekly_akshare(code: str, weeks: int = 60) -> Optional[pd.DataFrame]:
    """通过 AkShare 获取个股周线数据。"""
    if not _AK_AVAILABLE:
        return None
    try:
        symbol = _ak_code_to_symbol(code)
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(weeks=weeks)).strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(symbol=symbol, period="weekly", start_date=start, end_date=end, adjust="qfq")
        if df is None or df.empty:
            return None
        rename = {
            "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
            "最低": "low", "成交量": "volume", "成交额": "amount",
            "换手率": "turnover", "涨跌幅": "pct_chg",
        }
        df = df.rename(columns=rename)
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        for col in ["open", "high", "low", "close", "volume", "amount", "turnover", "pct_chg"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        return df.reset_index(drop=True) if not df.empty else None
    except Exception:
        return None


def _fetch_daily_akshare(code: str, days: int = 120) -> Optional[pd.DataFrame]:
    """通过 AkShare 获取个股日线数据。"""
    if not _AK_AVAILABLE:
        return None
    try:
        symbol = _ak_code_to_symbol(code)
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start, end_date=end, adjust="qfq")
        if df is None or df.empty:
            return None
        rename = {
            "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
            "最低": "low", "成交量": "volume", "成交额": "amount",
            "换手率": "turnover", "涨跌幅": "pct_chg",
        }
        df = df.rename(columns=rename)
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        for col in ["open", "high", "low", "close", "volume", "amount", "turnover", "pct_chg"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        return df.reset_index(drop=True) if not df.empty else None
    except Exception:
        return None


def _fetch_index_weekly_akshare(symbol: str = "sh000300", weeks: int = 60) -> Optional[pd.DataFrame]:
    """通过 AkShare 获取指数周线数据。"""
    if not _AK_AVAILABLE:
        return None
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(weeks=weeks)).strftime("%Y%m%d")
        df = ak.stock_zh_index_daily(symbol=symbol)
        if df is None or df.empty:
            return None
        rename = {
            "date": "date", "open": "open", "close": "close", "high": "high",
            "low": "low", "volume": "volume",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        df["date"] = pd.to_datetime(df["date"])
        start_dt = pd.to_datetime(start)
        df = df[df["date"] >= start_dt].copy()
        df["date"] = df["date"].dt.strftime("%Y-%m-%d")
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        # 转换为周线：按周聚合
        df["date_dt"] = pd.to_datetime(df["date"])
        df = df.set_index("date_dt")
        weekly = df.resample("W").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna(subset=["close"])
        weekly = weekly.reset_index()
        weekly["date"] = weekly["date_dt"].dt.strftime("%Y-%m-%d")
        weekly = weekly.drop(columns=["date_dt"])
        return weekly if not weekly.empty else None
    except Exception:
        return None


def _fetch_fundamentals_akshare(code: str) -> Optional[dict]:
    """通过 AkShare 获取基本面数据：ROE、负债率、商誉占比、扣非比。"""
    if not _AK_AVAILABLE:
        return None
    try:
        symbol = _ak_code_to_symbol(code)
        result: dict[str, Optional[float]] = {
            "roe": None,
            "debt_ratio": None,
            "goodwill_ratio": None,
            "deducted_profit_ratio": None,
        }

        # ROE + 负债率：财务指标
        try:
            df_fin = ak.stock_financial_analysis_indicator(symbol=symbol, start_year="2023")
            if df_fin is not None and not df_fin.empty:
                # 取最近一期
                for col in df_fin.columns:
                    col_lower = str(col).lower()
                    if "净资产收益率" in str(col) or "roe" in col_lower:
                        val = pd.to_numeric(df_fin.iloc[0][col], errors="coerce")
                        if not pd.isna(val):
                            result["roe"] = float(val)
                    if "资产负债率" in str(col) or "debt" in col_lower:
                        val = pd.to_numeric(df_fin.iloc[0][col], errors="coerce")
                        if not pd.isna(val):
                            result["debt_ratio"] = float(val)
        except Exception:
            pass

        # 商誉占比：资产负债表
        try:
            df_bs = ak.stock_balance_sheet_by_report_em(symbol=symbol)
            if df_bs is not None and not df_bs.empty:
                row = df_bs.iloc[0]
                goodwill = 0.0
                net_assets = 0.0
                for col in df_bs.columns:
                    if "商誉" in str(col):
                        val = pd.to_numeric(row.get(col), errors="coerce")
                        if not pd.isna(val):
                            goodwill = float(val)
                    if "股东权益合计" in str(col) or "净资产" in str(col):
                        val = pd.to_numeric(row.get(col), errors="coerce")
                        if not pd.isna(val) and val > 0:
                            net_assets = float(val)
                if net_assets > 0:
                    result["goodwill_ratio"] = goodwill / net_assets * 100
        except Exception:
            pass

        # 扣非利润占比：利润表
        try:
            df_income = ak.stock_profit_sheet_by_report_em(symbol=symbol)
            if df_income is not None and not df_income.empty:
                row = df_income.iloc[0]
                net_profit = 0.0
                deducted_profit = 0.0
                for col in df_income.columns:
                    col_str = str(col)
                    if "净利润" in col_str and "扣" not in col_str and "归" not in col_str:
                        val = pd.to_numeric(row.get(col), errors="coerce")
                        if not pd.isna(val):
                            net_profit = float(val)
                    if "扣非" in col_str or "扣除非经常" in col_str:
                        val = pd.to_numeric(row.get(col), errors="coerce")
                        if not pd.isna(val):
                            deducted_profit = float(val)
                if net_profit > 0:
                    result["deducted_profit_ratio"] = deducted_profit / net_profit
        except Exception:
            pass

        # 至少有一项有效数据才返回
        if any(v is not None for v in result.values()):
            return result
        return None
    except Exception:
        return None


def _fetch_stock_pool_akshare(pool: str = "all_a") -> list[dict]:
    """获取全A股票列表，返回 [{"code": "000001", "name": "平安银行"}, ...]。"""
    if not _AK_AVAILABLE:
        return []
    try:
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return []
        result = []
        for _, row in df.iterrows():
            code = str(row.get("代码", "")).zfill(6)
            name = str(row.get("名称", ""))
            # 排除ST和退市
            if "ST" in name or "退" in name:
                continue
            # 排除北交所（8开头）
            if code.startswith("8"):
                continue
            result.append({"code": code, "name": name})
        return result
    except Exception:
        return []


# ===========================================================================
# 统一数据接口（AkShare优先 → BaoStock降级）
# ===========================================================================


def get_weekly_data(
    code: str, config: StrategyConfig, cache: Optional[CacheManager] = None,
) -> Optional[pd.DataFrame]:
    """获取个股周线数据，优先 AkShare，降级 BaoStock。"""
    cache_key = f"weekly_{code}"
    if cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    df = _fetch_weekly_akshare(code, weeks=config.WEEKLY_BARS)
    if df is None and _BS_AVAILABLE:
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(weeks=config.WEEKLY_BARS)).strftime("%Y-%m-%d")
        bs_code = _ak_code_to_bs(code)
        df = BaoStockProvider.query_weekly(bs_code, start, end)

    if cache and df is not None:
        cache.set(cache_key, df)
    return df


def get_daily_data(
    code: str, config: StrategyConfig, cache: Optional[CacheManager] = None,
) -> Optional[pd.DataFrame]:
    """获取个股日线数据。"""
    cache_key = f"daily_{code}"
    if cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    df = _fetch_daily_akshare(code, days=config.DAILY_BARS)
    if df is None and _BS_AVAILABLE:
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=config.DAILY_BARS)).strftime("%Y-%m-%d")
        bs_code = _ak_code_to_bs(code)
        df = BaoStockProvider.query_daily(bs_code, start, end)

    if cache and df is not None:
        cache.set(cache_key, df)
    return df


def get_index_weekly(
    config: StrategyConfig, cache: Optional[CacheManager] = None,
) -> Optional[pd.DataFrame]:
    """获取沪深300指数周线数据。"""
    cache_key = "index_weekly_csi300"
    if cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    df = _fetch_index_weekly_akshare(symbol=config.CSI300_AK_SYMBOL, weeks=config.WEEKLY_BARS)
    if df is None and _BS_AVAILABLE:
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(weeks=config.WEEKLY_BARS)).strftime("%Y-%m-%d")
        df = BaoStockProvider.query_index_weekly(config.CSI300_CODE, start, end)

    if cache and df is not None:
        cache.set(cache_key, df)
    return df


def get_fundamentals(
    code: str, cache: Optional[CacheManager] = None,
) -> Optional[dict]:
    """获取基本面数据。"""
    cache_key = f"fund_{code}"
    if cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    data = _fetch_fundamentals_akshare(code)
    if cache and data is not None:
        cache.set(cache_key, data)
    return data


def get_stock_list(
    config: StrategyConfig, cache: Optional[CacheManager] = None,
) -> list[dict]:
    """获取待筛选股票列表。"""
    cache_key = "stock_list"
    if cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    stocks = _fetch_stock_pool_akshare(config.STOCK_POOL)
    if cache and stocks:
        cache.set(cache_key, stocks)
    return stocks


# ===========================================================================
# Layer 1: 市场环境计算（纯函数）
# ===========================================================================


def compute_market_environment(
    df_index: pd.DataFrame, config: StrategyConfig,
) -> dict:
    """根据沪深300周线MA20斜率判断牛/熊/中性环境。

    纯函数：输入指数周线DataFrame，输出环境描述dict。
    """
    if df_index is None or df_index.empty or len(df_index) < config.MARKET_MA_PERIOD + config.MARKET_SLOPE_LOOKBACK:
        return {"regime": "unknown", "description": "数据不足", "ma20": 0, "slope": 0, "close": 0}

    df = df_index.copy().reset_index(drop=True)
    df["ma"] = df["close"].rolling(config.MARKET_MA_PERIOD).mean()

    cur = df.iloc[-1]
    prev = df.iloc[-(1 + config.MARKET_SLOPE_LOOKBACK)]

    ma_now = float(cur["ma"])
    ma_prev = float(prev["ma"])
    close_now = float(cur["close"])

    slope = (ma_now - ma_prev) / ma_prev if ma_prev > 0 else 0.0

    if slope > config.MARKET_BULL_SLOPE:
        regime = "bull"
        desc = f"偏多（MA20斜率 {slope:.4f}，沪深300收于 {close_now:.0f}）"
    elif slope < config.MARKET_BEAR_SLOPE:
        regime = "bear"
        desc = f"偏空（MA20斜率 {slope:.4f}，沪深300收于 {close_now:.0f}）"
    else:
        regime = "neutral"
        desc = f"中性（MA20斜率 {slope:.4f}，沪深300收于 {close_now:.0f}）"

    return {
        "regime": regime,
        "description": desc,
        "ma20": round(ma_now, 2),
        "slope": round(slope, 6),
        "close": round(close_now, 2),
    }


# ===========================================================================
# Layer 2: 周线左侧寻底信号（纯函数，向量化）
# ===========================================================================

_WEEKLY_NEED_COLS = {"close", "high", "low", "volume", "pct_chg", "amount", "date"}


def compute_weekly_signals(
    df: pd.DataFrame, config: StrategyConfig,
) -> Optional[pd.DataFrame]:
    """向量化计算周线寻底信号与评分。

    返回原df + 信号/评分列的副本，其中 weekly_signal 为布尔列，weekly_score 为连续评分。
    回测时直接使用返回的 DataFrame；实盘时通过 evaluate() 只取最后一行。

    硬性门槛（全部满足才产生信号）:
      1. 个股下跌趋势: close < MA20 且 MA20 向下（借鉴策略1）
      2. ATR归一化跌幅 >= 阈值
      3. CCI14 <= 超卖线
      4. 成交额 >= 下限（借鉴策略1）
      5. 非次新股（借鉴策略1）

    评分项（0-70分）:
      - ATR跌幅深度:   0-20分
      - CCI超卖深度:   0-15分
      - 量能模式:       0-10分
      - MACD底背离:     0-10分
      - 守住前低:       0-10分（借鉴策略1）
      - 恐慌杀跌:       0-5分 （借鉴策略1）
    """
    if df is None or df.empty:
        return None
    if not _WEEKLY_NEED_COLS.issubset(df.columns):
        return None
    if len(df) < config.MIN_WEEKS:
        return None

    out = df.copy().reset_index(drop=True)

    # ------ 技术指标计算（全部向量化） ------

    # 1) 个股MA20 + 斜率（下跌趋势确认，借鉴策略1的 ma < ma_prev）
    out["ma20"] = out["close"].rolling(config.WEEKLY_MA_PERIOD).mean()
    out["ma20_prev"] = out["ma20"].shift(config.WEEKLY_SLOPE_LOOKBACK)
    in_downtrend = (out["close"] < out["ma20"]) & (out["ma20"] < out["ma20_prev"])

    # 2) ATR14
    prev_close = out["close"].shift(1)
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - prev_close).abs(),
        (out["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr14"] = tr.rolling(config.WEEKLY_ATR_PERIOD).mean()

    # ATR归一化跌幅：本周下跌幅度 / ATR（正数表示下跌）
    weekly_decline = prev_close - out["close"]
    out["atr_decline"] = weekly_decline / out["atr14"]
    atr_decline_ok = out["atr_decline"] >= config.WEEKLY_ATR_DECLINE_THRESHOLD

    # 3) CCI14
    tp = (out["high"] + out["low"] + out["close"]) / 3
    tp_sma = tp.rolling(config.WEEKLY_CCI_PERIOD).mean()
    tp_mad = tp.rolling(config.WEEKLY_CCI_PERIOD).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True,
    )
    out["cci14"] = (tp - tp_sma) / (0.015 * tp_mad)
    cci_oversold = out["cci14"] <= config.WEEKLY_CCI_OVERSOLD

    # 4) MACD
    ema_fast = out["close"].ewm(span=config.WEEKLY_MACD_FAST, adjust=False).mean()
    ema_slow = out["close"].ewm(span=config.WEEKLY_MACD_SLOW, adjust=False).mean()
    out["macd"] = ema_fast - ema_slow
    out["macd_signal"] = out["macd"].ewm(span=config.WEEKLY_MACD_SIGNAL, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]

    # MACD底背离：价格创新低但MACD柱不创新低
    price_low_min = out["low"].shift(1).rolling(config.PRIOR_LOW_LOOKBACK).min()
    macd_hist_min = out["macd_hist"].shift(1).rolling(config.PRIOR_LOW_LOOKBACK).min()
    out["macd_divergence"] = (out["low"] <= price_low_min) & (out["macd_hist"] > macd_hist_min)

    # 5) 量能模式：前期缩量 + 本周放量
    out["vol_base"] = out["volume"].shift(1).rolling(config.WEEKLY_VOL_LOOKBACK).mean()
    out["vol_ratio"] = out["volume"] / out["vol_base"].replace(0, np.nan)

    vol_prev_2 = out["volume"].shift(2) / out["vol_base"].shift(1).replace(0, np.nan)
    vol_prev_3 = out["volume"].shift(3) / out["vol_base"].shift(2).replace(0, np.nan)
    vol_shrink = (vol_prev_2 <= config.WEEKLY_VOL_SHRINK_RATIO) | (
        vol_prev_3 <= config.WEEKLY_VOL_SHRINK_RATIO
    )
    vol_expand = out["vol_ratio"] >= config.WEEKLY_VOL_EXPAND_RATIO

    # 6) 换手率倍率（借鉴策略1）
    if "turnover" in out.columns:
        out["turnover_base"] = out["turnover"].shift(1).rolling(config.TURNOVER_LOOKBACK).mean()
        out["turnover_ratio"] = out["turnover"] / out["turnover_base"].replace(0, np.nan)
    else:
        out["turnover_base"] = np.nan
        out["turnover_ratio"] = np.nan

    # 7) 不破前低（借鉴策略1）
    out["prior_low"] = out["low"].shift(1).rolling(config.PRIOR_LOW_LOOKBACK).min()
    out["holds_prior_low"] = out["low"] >= out["prior_low"]

    # ------ 硬性门槛 ------
    amount_ok = out["amount"] >= config.MIN_AMOUNT
    not_newly = pd.Series(out.index >= config.MIN_WEEKS, index=out.index)

    out["weekly_signal"] = (
        in_downtrend & atr_decline_ok & cci_oversold & amount_ok & not_newly
    ).fillna(False)

    # ------ 评分（连续值） ------

    # ATR跌幅深度: 从阈值到2倍阈值线性映射 0→满分
    atr_score = (
        (out["atr_decline"] - config.WEEKLY_ATR_DECLINE_THRESHOLD)
        / config.WEEKLY_ATR_DECLINE_THRESHOLD
    ).clip(0, 1) * config.W_ATR_DECLINE

    # CCI超卖深度: 越深分越高
    if config.WEEKLY_CCI_OVERSOLD != 0:
        cci_score = (
            (config.WEEKLY_CCI_OVERSOLD - out["cci14"]) / abs(config.WEEKLY_CCI_OVERSOLD)
        ).clip(0, 1) * config.W_CCI_OVERSOLD
    else:
        cci_score = pd.Series(0.0, index=out.index)

    # 量能模式: 缩量→放量
    vol_pattern_score = (
        vol_shrink.astype(float) * vol_expand.astype(float) * config.W_VOLUME_PATTERN
    )

    # MACD底背离
    macd_div_score = out["macd_divergence"].astype(float) * config.W_MACD_DIVERGENCE

    # 守住前低（借鉴策略1）
    prior_low_score = out["holds_prior_low"].astype(float) * config.W_PRIOR_LOW_HOLD

    # 恐慌杀跌（借鉴策略1）
    panic_score = (out["pct_chg"] <= config.PANIC_PCT).astype(float) * config.W_PANIC_BONUS

    out["weekly_score"] = (
        atr_score + cci_score + vol_pattern_score
        + macd_div_score + prior_low_score + panic_score
    ).fillna(0).round(1)

    return out


# ===========================================================================
# Layer 3: 基本面防雷（纯函数）
# ===========================================================================


def check_fundamentals(
    fund_data: Optional[dict], config: StrategyConfig,
) -> bool:
    """否决制基本面筛选。任一指标不达标即拒绝。

    fund_data 为 None 时放行（不因数据缺失误杀）。
    """
    if fund_data is None:
        return True

    roe = fund_data.get("roe")
    if roe is not None and roe < config.MIN_ROE:
        return False

    debt = fund_data.get("debt_ratio")
    if debt is not None and debt > config.MAX_DEBT_RATIO:
        return False

    goodwill = fund_data.get("goodwill_ratio")
    if goodwill is not None and goodwill > config.MAX_GOODWILL_RATIO:
        return False

    deducted = fund_data.get("deducted_profit_ratio")
    if deducted is not None and deducted < config.MIN_DEDUCTED_PROFIT_RATIO:
        return False

    return True


# ===========================================================================
# Layer 4: 日线右侧确认信号（纯函数，向量化）
# ===========================================================================

_DAILY_NEED_COLS = {"close", "volume", "date"}


def compute_daily_signals(
    df: pd.DataFrame, config: StrategyConfig,
) -> Optional[pd.DataFrame]:
    """向量化计算日线右侧确认信号与评分。

    评分项（0-30分）:
      - MA5拐头:          0-8分
      - EMA5/10金叉:      0-8分
      - RSI超卖反弹:      0-7分
      - 量价配合+换手率:  0-7分（借鉴策略1的换手率维度）
    """
    if df is None or df.empty:
        return None
    if not _DAILY_NEED_COLS.issubset(df.columns):
        return None
    if len(df) < config.MIN_DAYS:
        return None

    out = df.copy().reset_index(drop=True)

    # MA5, MA10
    out["ma5"] = out["close"].rolling(config.DAILY_MA5).mean()
    out["ma10"] = out["close"].rolling(config.DAILY_MA10).mean()

    # EMA5, EMA10, EMA20
    out["ema5"] = out["close"].ewm(span=config.DAILY_EMA5, adjust=False).mean()
    out["ema10"] = out["close"].ewm(span=config.DAILY_EMA10, adjust=False).mean()
    out["ema20"] = out["close"].ewm(span=config.DAILY_EMA20, adjust=False).mean()

    # RSI14
    delta = out["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=config.DAILY_RSI_PERIOD - 1, min_periods=config.DAILY_RSI_PERIOD).mean()
    avg_loss = loss.ewm(com=config.DAILY_RSI_PERIOD - 1, min_periods=config.DAILY_RSI_PERIOD).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi14"] = 100 - (100 / (1 + rs))

    # 日线量比
    out["daily_vol_base"] = out["volume"].shift(1).rolling(20).mean()
    out["daily_vol_ratio"] = out["volume"] / out["daily_vol_base"].replace(0, np.nan)

    # 日线换手率倍率（借鉴策略1）
    if "turnover" in out.columns:
        out["daily_to_base"] = out["turnover"].shift(1).rolling(config.DAILY_TURNOVER_LOOKBACK).mean()
        out["daily_turnover_ratio"] = out["turnover"] / out["daily_to_base"].replace(0, np.nan)
    else:
        out["daily_turnover_ratio"] = np.nan

    # ------ 信号条件 ------

    # MA5拐头：斜率从负转正
    ma5_slope = out["ma5"] - out["ma5"].shift(1)
    ma5_slope_prev = ma5_slope.shift(1)
    out["ma5_turn"] = (ma5_slope > 0) & (ma5_slope_prev <= 0)

    # EMA金叉：EMA5上穿EMA10
    out["ema_golden_cross"] = (
        (out["ema5"] > out["ema10"]) & (out["ema5"].shift(1) <= out["ema10"].shift(1))
    )

    # RSI超卖反弹：近2日曾低于超卖线，当前回升到反弹线上方
    rsi_was_oversold = (
        (out["rsi14"].shift(1) < config.DAILY_RSI_OVERSOLD)
        | (out["rsi14"].shift(2) < config.DAILY_RSI_OVERSOLD)
    )
    out["rsi_rebound"] = rsi_was_oversold & (out["rsi14"] >= config.DAILY_RSI_REBOUND_MIN)

    # 量价配合：价格上涨 + 量放大 + 换手率放大（借鉴策略1）
    price_up = out["close"] > out["close"].shift(1)
    vol_expand = out["daily_vol_ratio"] >= config.DAILY_VOL_EXPAND
    turnover_expand = True
    if "daily_turnover_ratio" in out.columns:
        turnover_expand = out["daily_turnover_ratio"] >= config.DAILY_VOL_EXPAND
    out["vol_price_coord"] = price_up & vol_expand & turnover_expand

    # ------ 评分 ------
    ma5_turn_score = out["ma5_turn"].astype(float) * config.W_DAILY_MA_TURN
    ema_cross_score = out["ema_golden_cross"].astype(float) * config.W_DAILY_EMA_CROSS
    rsi_rebound_score = out["rsi_rebound"].astype(float) * config.W_DAILY_RSI_REBOUND
    # RSI超买惩罚
    rsi_penalty = (out["rsi14"] >= config.DAILY_RSI_OVERBOUGHT).astype(float) * (-3.0)
    vol_price_score = out["vol_price_coord"].astype(float) * config.W_DAILY_VOL_PRICE

    out["daily_score"] = (
        ma5_turn_score + ema_cross_score + rsi_rebound_score + rsi_penalty + vol_price_score
    ).fillna(0).clip(0, 30).round(1)

    out["daily_signal"] = (
        out["ma5_turn"] | out["ema_golden_cross"] | out["rsi_rebound"]
    ).fillna(False)

    return out


# ===========================================================================
# Layer 5: 风险收益比（纯函数）
# ===========================================================================


def compute_risk_reward(
    entry_price: float,
    atr: float,
    ma20: float,
    config: StrategyConfig,
) -> dict:
    """计算止损、止盈、风险收益比。

    止损 = 入场价 - ATR × 倍数
    止盈 = MA20（均值回归目标）或固定百分比
    RR = 收益空间 / 风险空间
    """
    stop_loss = entry_price - atr * config.ATR_STOP_MULTIPLIER

    if config.TAKE_PROFIT_TARGET == "ma20" and ma20 > entry_price:
        take_profit = ma20
    else:
        take_profit = entry_price * (1 + config.FIXED_TP_PCT / 100)

    risk = entry_price - stop_loss
    reward = take_profit - entry_price
    rr_ratio = reward / risk if risk > 0 else 0.0

    return {
        "stop_loss": round(stop_loss, 2),
        "take_profit": round(take_profit, 2),
        "rr_ratio": round(rr_ratio, 2),
        "passes": rr_ratio >= config.MIN_RR_RATIO,
    }


# ===========================================================================
# Signal dataclass + describe()（沿用策略1模式）
# ===========================================================================


@dataclass
class Signal:
    """单只股票的完整选股信号，涵盖5层信息。"""

    code: str
    name: str
    date: str
    close: float
    score: float
    grade: str
    weekly_score: float
    daily_score: float
    pct_chg_w: float
    atr_decline: float
    cci: float
    rsi: float
    vol_ratio: float
    turnover_ratio: float
    holds_prior_low: bool
    macd_divergence: bool
    stop_loss: float
    take_profit: float
    rr_ratio: float
    market_env: str
    ma20: float

    def to_dict(self) -> dict:
        return asdict(self)


def _grade_from_score(score: float, config: StrategyConfig) -> str:
    """评分 → 信号等级映射。"""
    if score >= config.GRADE_A:
        return "A"
    elif score >= config.GRADE_B:
        return "B"
    elif score >= config.GRADE_C:
        return "C"
    return "D"


def describe(r: dict) -> str:
    """将 Signal.to_dict() 格式化为飞书 lark_md 逐条分析报告。"""
    return "\n".join([
        f"**{r['name']} {r['code']}**  {r['date']}  收盘 {r['close']}",
        f"综合评分 **{r['score']}** ({r['grade']}级) ｜ 周线 {r['weekly_score']}分 + 日线 {r['daily_score']}分",
        "",
        "**周线寻底:**",
        f"- ATR跌幅: {r['atr_decline']}倍 ｜ CCI: {r['cci']}",
        f"- 量比: {r['vol_ratio']}x ｜ 换手比: {r['turnover_ratio']}x",
        f"- 守住前低: {'是' if r['holds_prior_low'] else '否'}"
        f" ｜ MACD底背离: {'是' if r['macd_divergence'] else '否'}",
        "",
        "**日线确认:**",
        f"- RSI: {r['rsi']} ｜ 市场环境: {r['market_env']}",
        "",
        "**交易计划:**",
        f"- 止损: {r['stop_loss']} ｜ 止盈: {r['take_profit']} ｜ 风险收益比: {r['rr_ratio']}",
    ])


# ===========================================================================
# evaluate() — 单股5层评估入口（沿用策略1模式）
# ===========================================================================


def evaluate(
    weekly_df: Optional[pd.DataFrame],
    daily_df: Optional[pd.DataFrame],
    code: str = "",
    name: str = "",
    config: Optional[StrategyConfig] = None,
    market_env: Optional[dict] = None,
    fund_data: Optional[dict] = None,
) -> Optional[Signal]:
    """对单只股票执行完整5层评估，返回 Signal 或 None。

    纯逻辑函数，不做任何数据获取。沿用策略1的 evaluate(df, code, name) 模式。

    Layer 1: 市场环境 → 熊市收紧等级阈值（BEAR_GRADE_BOOST）
    Layer 2: 周线寻底 → 硬性门槛 + 评分
    Layer 3: 基本面防雷 → 否决制
    Layer 4: 日线确认 → 至少一个确认信号触发（门槛）+ 评分
    Layer 5: 风险收益比 → RR >= 1.5
    """
    if config is None:
        config = StrategyConfig()

    # Layer 1: 市场环境 → 调整等级阈值
    regime = (market_env or {}).get("regime", "unknown")
    if regime == "bear":
        grade_boost = config.BEAR_GRADE_BOOST
    else:
        grade_boost = 0.0

    # Layer 2: 周线寻底
    weekly_out = compute_weekly_signals(weekly_df, config)
    if weekly_out is None or weekly_out.empty:
        return None
    w_last = weekly_out.iloc[-1]
    if not bool(w_last.get("weekly_signal", False)):
        return None

    # Layer 3: 基本面防雷
    if not check_fundamentals(fund_data, config):
        return None

    # Layer 4: 日线确认（门槛 + 评分）
    daily_out = compute_daily_signals(daily_df, config)
    daily_score = 0.0
    rsi_val = 50.0
    daily_confirmed = False
    if daily_out is not None and not daily_out.empty:
        d_last = daily_out.iloc[-1]
        daily_score = float(d_last.get("daily_score", 0))
        rsi_val = float(d_last.get("rsi14", 50))
        daily_confirmed = bool(d_last.get("daily_signal", False))

    # 日线确认门槛：至少一个日线信号触发
    if not daily_confirmed:
        return None

    # 合并评分（熊市时等级阈值上浮，实质是要求更高分数才能通过）
    total_score = float(w_last["weekly_score"]) + daily_score
    grade = _grade_from_score(total_score - grade_boost, config)
    if grade == "D":
        return None

    # Layer 5: 风险收益比
    atr_val = float(w_last.get("atr14", 0))
    ma20_val = float(w_last.get("ma20", 0))
    if atr_val <= 0:
        return None

    rr = compute_risk_reward(
        entry_price=float(w_last["close"]),
        atr=atr_val,
        ma20=ma20_val,
        config=config,
    )
    if not rr["passes"]:
        return None

    return Signal(
        code=code,
        name=name,
        date=pd.to_datetime(w_last["date"]).strftime("%Y-%m-%d"),
        close=round(float(w_last["close"]), 2),
        score=round(total_score, 1),
        grade=grade,
        weekly_score=round(float(w_last["weekly_score"]), 1),
        daily_score=round(daily_score, 1),
        pct_chg_w=round(float(w_last.get("pct_chg", 0)), 2),
        atr_decline=round(float(w_last.get("atr_decline", 0)), 2),
        cci=round(float(w_last.get("cci14", 0)), 1),
        rsi=round(rsi_val, 1),
        vol_ratio=round(float(w_last.get("vol_ratio", 0)), 2),
        turnover_ratio=round(float(w_last.get("turnover_ratio", 0)), 2),
        holds_prior_low=bool(w_last.get("holds_prior_low", False)),
        macd_divergence=bool(w_last.get("macd_divergence", False)),
        stop_loss=rr["stop_loss"],
        take_profit=rr["take_profit"],
        rr_ratio=rr["rr_ratio"],
        market_env=regime,
        ma20=round(ma20_val, 2),
    )


# ===========================================================================
# 编排函数（匹配 run.py 的导入接口）
# ===========================================================================


def get_market_environment(
    config: StrategyConfig, cache: CacheManager,
) -> dict:
    """获取沪深300周线 → 计算市场环境 → 缓存。供 run.py 调用。"""
    cached = cache.get("market_env")
    if cached is not None:
        return cached

    df_index = get_index_weekly(config, cache)
    if df_index is None or df_index.empty:
        result = {
            "regime": "unknown",
            "description": "数据获取失败",
            "ma20": 0, "slope": 0, "close": 0,
        }
    else:
        result = compute_market_environment(df_index, config)

    cache.set("market_env", result)
    return result


def main(
    config: Optional[StrategyConfig] = None,
    cache: Optional[CacheManager] = None,
) -> Optional[pd.DataFrame]:
    """完整选股流水线。run.py 的主入口。

    接受外部传入的 config/cache 以复用 run.py 已创建的实例，避免重复数据请求。
    不传参时自动创建默认实例（兼容直接命令行调用）。

    流程: 市场环境 → 股票列表 → 并行筛选(周线+基本面+日线+风控) → 排序输出
    """
    if config is None:
        config = StrategyConfig()
    if cache is None:
        cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)

    # Layer 1: 市场环境
    market_env = get_market_environment(config, cache)
    print(f"[INFO] 市场环境: {market_env.get('description', 'unknown')}")

    # 股票列表
    stock_list = get_stock_list(config, cache)
    if not stock_list:
        print("[WARN] 无法获取股票列表")
        return None
    print(f"[INFO] 待筛选股票数: {len(stock_list)}")

    signals: list[dict] = []
    processed = 0
    total = len(stock_list)

    def _screen_one(stock: dict) -> Optional[Signal]:
        code, name = stock["code"], stock["name"]
        try:
            weekly_df = get_weekly_data(code, config, cache)
            if weekly_df is None:
                return None
            daily_df = get_daily_data(code, config, cache)
            fund_data = get_fundamentals(code, cache)
            if config.FETCH_DELAY > 0:
                time.sleep(config.FETCH_DELAY)
            return evaluate(weekly_df, daily_df, code, name, config, market_env, fund_data)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        futures = {pool.submit(_screen_one, s): s for s in stock_list}
        for future in as_completed(futures):
            processed += 1
            if processed % 500 == 0:
                print(f"[INFO] 进度: {processed}/{total}")
            sig = future.result()
            if sig is not None:
                signals.append(sig.to_dict())

    if not signals:
        print("[INFO] 未发现符合条件的信号")
        return None

    df = pd.DataFrame(signals)
    df = df.sort_values(SORT_BY, ascending=SORT_ASC).reset_index(drop=True)
    print(f"[INFO] 筛选完成，共 {len(df)} 只股票通过5层过滤")
    return df


def save_signal_history(df: pd.DataFrame) -> None:
    """将选股结果追加保存到 CSV。"""
    config = StrategyConfig()
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = os.path.join(config.DATA_DIR, config.SIGNAL_HISTORY_FILE)

    df_out = df.copy()
    df_out["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if os.path.exists(path):
        df_out.to_csv(path, mode="a", header=False, index=False)
    else:
        df_out.to_csv(path, index=False)
    print(f"[INFO] 信号已保存至 {path}")


def weekly_performance_review(config: StrategyConfig) -> Optional[pd.DataFrame]:
    """追踪历史推荐股票的后续表现。

    读取信号历史CSV，获取当前价格，计算收益率，判定止损/止盈/持有状态。
    """
    path = os.path.join(config.DATA_DIR, config.SIGNAL_HISTORY_FILE)
    if not os.path.exists(path):
        return None

    try:
        history = pd.read_csv(path)
    except Exception:
        return None

    if history.empty:
        return None

    # 筛选近4周的推荐记录
    history["date"] = pd.to_datetime(history["date"], errors="coerce")
    cutoff = datetime.now() - timedelta(weeks=4)
    recent = history[history["date"] >= cutoff].copy()
    if recent.empty:
        return None

    cache = CacheManager(expire_hours=config.CACHE_EXPIRE_HOURS)
    records = []

    for _, row in recent.iterrows():
        code = str(row.get("code", ""))
        if not code:
            continue
        try:
            daily_df = get_daily_data(code, config, cache)
            if daily_df is None or daily_df.empty:
                continue
            current_price = float(daily_df.iloc[-1]["close"])
            rec_price = float(row.get("close", 0))
            stop_loss = float(row.get("stop_loss", 0))
            take_profit = float(row.get("take_profit", 0))

            if rec_price <= 0:
                continue

            return_pct = (current_price - rec_price) / rec_price * 100

            if stop_loss > 0 and current_price <= stop_loss:
                status = "止损"
            elif take_profit > 0 and current_price >= take_profit:
                status = "止盈"
            else:
                status = "持有中"

            records.append({
                "code": code,
                "name": row.get("name", ""),
                "rec_date": row.get("date"),
                "rec_price": round(rec_price, 2),
                "current_price": round(current_price, 2),
                "return_pct": round(return_pct, 2),
                "status": status,
                "stop_loss": round(stop_loss, 2),
                "take_profit": round(take_profit, 2),
                "grade": row.get("grade", ""),
                "score": row.get("score", 0),
            })
        except Exception:
            continue

    if not records:
        return None

    report = pd.DataFrame(records)
    print(f"[INFO] 周度追踪: {len(report)} 只股票")

    # 统计
    total = len(report)
    win = len(report[report["return_pct"] > 0])
    avg_return = report["return_pct"].mean()
    print(f"[INFO] 胜率: {win}/{total} = {win / total * 100:.1f}%, 平均收益: {avg_return:.2f}%")

    return report


def update_signal_status(report: pd.DataFrame) -> None:
    """保存追踪报告到 CSV。"""
    config = StrategyConfig()
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = os.path.join(config.DATA_DIR, config.TRACKING_FILE)
    report.to_csv(path, index=False)
    print(f"[INFO] 追踪报告已保存至 {path}")


# ===========================================================================
# __main__ — 命令行入口
# ===========================================================================

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"

    if mode == "screen":
        result = main()
        if result is not None:
            print(result.to_string())
        else:
            print("未发现信号")

    elif mode == "track":
        cfg = StrategyConfig()
        report = weekly_performance_review(cfg)
        if report is not None:
            print(report.to_string())
        else:
            print("无追踪数据")

    elif mode == "full":
        result = main()
        if result is not None:
            save_signal_history(result)
            print(f"共 {len(result)} 只信号")
        cfg = StrategyConfig()
        report = weekly_performance_review(cfg)
        if report is not None:
            update_signal_status(report)

    else:
        print(f"未知模式: {mode}，可选: screen / track / full")
