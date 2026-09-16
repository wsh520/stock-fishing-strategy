"""历史回测：逐日重放选股策略并统计月度收益。

与「实时选股」共用同一套策略纯函数（src/bottom_fishing_strategy.py 的 evaluate /
compute_daily_signals / 周线确认 / 风控 / 排序 / 行业分散），保证回测口径与实盘一致。

核心设计——避免未来函数（point-in-time）：
  1. 预取每只股票 [DATA_START, DATA_END] 的日线面板并落盘缓存（一次性网络成本）；
  2. 对区间内每个交易日 d：把个股日线**截断到 d**、且只取最近 DAILY_BARS(=120) 根
     （与实盘 _window_dates 的取数窗口一致），交给策略 evaluate() 打分/否决；
     周线由「截至 d 的日线」重采样得到（对应实盘单独拉取的 60 周窗口）；
     市场环境用「截至 d 的沪深300」计算；latest_trade_date=d 触发停牌/滞后剔除；
  3. 决赛圈完全复刻 main()：排序 → 周线确认 → 行业分散 → 基本面终审 → 取满 MAX_PICKS；
  4. 收益模拟：信号日 d 收盘后生成，**次日开盘价买入**；持有期内用盘中高低价模拟触发
     +10% 止盈 / -5% 止损（同根 K 线两者都触及时保守按止损成交），否则持有到数据期末
     （或 --max-hold-days 上限）按收盘退出；
  5. 月度统计：每笔等权平均收益 + 每日等权组合的逐日盯市净值曲线，两套口径对比，
     并按 等级 / 市场环境 / 底背离 / 退出原因 归因。

基本面为控制网络成本，只对「决赛圈候选」按 d 所属披露窗口回溯拉取（as-of 时点），
商誉/扣非主源缺失时默认不用 AkShare 补齐（--fill-fund 可开启），因此个别标的的
fund 终审可能比实盘略宽松，已在报告中注明。

用法：
    python backtest.py prefetch [--workers 6] [--limit N]      # 预取行情面板到磁盘缓存（可断点续跑）
    python backtest.py run [--limit N] [--max-hold-days X]      # 逐日重放 + 收益模拟 + 月度统计 + 报告
    python backtest.py all [...]                                # prefetch 后直接 run
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 路径与策略模块导入（复用实盘策略的纯函数，确保回测口径与实盘同源）
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import baostock as bs  # noqa: E402

# 与策略模块一致的 pandas 3.x 兼容补丁（query_all_stock / query_stock_industry 翻页需要）
if not hasattr(pd.DataFrame, "append"):
    pd.DataFrame.append = lambda self, other, ignore_index=False, **kw: pd.concat(  # type: ignore[attr-defined]
        [self, other], ignore_index=ignore_index, **kw
    )

from src.bottom_fishing_strategy import (  # noqa: E402
    StrategyConfig,
    _format_bs_code,
    _normalize_bs_hist,
    _normalize_ak_hist,
    _ak_symbol,
    _apply_pool_filters,
    _quarter_candidates,
    _rank_signals,
    compute_market_environment,
    check_fundamentals,
    check_weekly_trend,
    check_weekly_macd,
    _weekly_trend_data_ok,
    _weekly_macd_data_ok,
    _weekly_fresh_enough,
    _fund_verify_state,
    evaluate,
    resolve_max_picks,
    market_crash_halt,
    bs_lock,
)

logger = logging.getLogger("backtest")

# evaluate() 否决原因 -> 中文标签（用于选股漏斗报告）
_REASON_ZH = {
    "PASS": "通过", "FAIL_DATA": "数据不足/列缺失", "FAIL_STALE": "停牌或数据滞后",
    "FAIL_HALT_GAP": "K线跨停牌缺口(指标失真)",
    "FAIL_LIQUIDITY": "流动性不足(日均额<3000万)",
    "FAIL_FUND": "基本面防雷(ROE/负债率)", "FAIL_TECH": "技术评分未达门槛",
    "FAIL_CHASE": "当日追高(涨幅>5%)", "FAIL_GAP": "跳空高开(>2%)",
    "FAIL_RSI_HIGH": "RSI14过高(>60)", "FAIL_CLIMAX_VOL": "天量(量比>4)",
    "FAIL_NOT_BOTTOM": "非底部(距60日高点回撤<10%)", "FAIL_DEEP_CRASH": "深度崩盘(回撤>70%)",
    "FAIL_RECENT_RALLY": "近5日已反弹(>12%)", "FAIL_POSITION": "区间位置偏高(>50%)",
    "FAIL_MACD_MOM": "MACD柱未连续改善", "FAIL_KDJ": "KDJ未金叉/高位",
    "FAIL_VOLATILE": "波动率超限(ATR>3.33%现价)",
    "FAIL_RR": "盈亏比不足(<1.5)", "ERROR": "异常",
    # 非 evaluate() 归因，由 _screen_day 的组合层风控产生
    "HALT_MARKET_CRASH": "市场级熔断(指数近5日跌幅超阈值)",
    "HALT_MAX_PICKS_ZERO": "推荐上限为0(熊市不推荐)",
}

# ---------------------------------------------------------------------------
# 回测配置
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    # 回测（选股）区间：逐日重放的交易日范围，含端点
    BT_START: str = "2026-08-01"
    BT_END: str = "2026-09-01"
    # 行情面板取数区间：DATA_START 需早于 BT_START 足够多，以覆盖
    #   日线 120 根窗口 + 周线 60 周（约需 8~9 个月历史）；DATA_END 决定持有退出的「期末」
    DATA_START: str = "2025-11-01"
    DATA_END: str = ""            # 空 = 自动取今天（baostock 返回到最近可用交易日）
    # 每日 evaluate 传入的日线根数上限，与实盘 StrategyConfig.DAILY_BARS 对齐（避免未来函数 + 控制算力）
    DAILY_EVAL_BARS: int = 120
    # 收益模拟
    ENTRY_MODE: str = "next_open"      # next_open=次日开盘买入 / rec_close=信号日收盘买入
    USE_STOP_TP: bool = True           # 是否用盘中高低价模拟止损/止盈
    MAX_HOLD_DAYS: int = 0             # 0=不设上限，持有到数据期末；>0=最多持有 N 个交易日
    STOP_FIRST_ON_BOTH: bool = True    # 同一根 K 线同时触及止损与止盈时，保守按止损成交
    DAILY_BUDGET: float = 10000.0      # 组合净值模拟中每日投入的等额资金（仅用于盯市曲线口径）
    # 缓存 / 输出目录
    CACHE_DIR: str = os.path.join(_PROJECT_ROOT, "backtest_cache")
    OUT_DIR: str = os.path.join(_PROJECT_ROOT, "backtest_output")
    # 运行控制
    WORKERS: int = 6                   # prefetch 进程数 / replay 线程数
    FILL_OPTIONAL_FUNDAMENTALS: bool = False   # 决赛圈是否用 AkShare 补齐商誉/扣非（慢，默认关）
    LIMIT: int = 0                     # >0 时只取股票池前 N 只（冒烟测试用）
    # 行情数据源：baostock（默认）/ akshare（东财+新浪，独立通道，baostock 被限流时用）
    DATA_SOURCE: str = "baostock"
    REFRESH: bool = False              # True 时预取忽略已有缓存，全部重新从接口拉取

    def data_end(self) -> str:
        return self.DATA_END or datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 磁盘缓存路径
# ---------------------------------------------------------------------------

def _daily_cache_path(bc: BacktestConfig, code: str) -> str:
    return os.path.join(bc.CACHE_DIR, "daily", f"{code}.csv")


def _fund_cache_path(bc: BacktestConfig, code: str) -> str:
    return os.path.join(bc.CACHE_DIR, "fund", f"{code}.json")


def _meta_path(bc: BacktestConfig, name: str) -> str:
    return os.path.join(bc.CACHE_DIR, name)


def _ensure_dirs(bc: BacktestConfig) -> None:
    os.makedirs(os.path.join(bc.CACHE_DIR, "daily"), exist_ok=True)
    os.makedirs(os.path.join(bc.CACHE_DIR, "fund"), exist_ok=True)
    os.makedirs(bc.OUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 数据抓取（baostock，显式日期区间；与实盘不同：不使用 now() 窗口）
# ---------------------------------------------------------------------------

_HIST_FIELDS = "date,open,high,low,close,volume,amount,pctChg,turn"


def _bs_login_safe(max_retry: int = 5) -> bool:
    for attempt in range(max_retry):
        try:
            with bs_lock:
                lg = bs.login()
            if getattr(lg, "error_code", None) == "0":
                return True
            logger.warning("baostock 登录失败(%d/%d): %s", attempt + 1, max_retry, getattr(lg, "error_msg", ""))
        except Exception as e:  # noqa: BLE001
            logger.warning("baostock 登录异常(%d/%d): %s", attempt + 1, max_retry, e)
        if attempt < max_retry - 1:
            time.sleep(1.0 * (attempt + 1))
    return False


def _fetch_hist(code: str, start: str, end: str, freq: str = "d", fields: str = _HIST_FIELDS) -> Optional[pd.DataFrame]:
    """按显式日期区间取行情，标准化列名（date/open/high/low/close/volume/amount/pct_chg/turnover）。"""
    bs_code = _format_bs_code(code)
    for attempt in range(3):
        try:
            with bs_lock:
                rs = bs.query_history_k_data_plus(bs_code, fields, start_date=start, end_date=end,
                                                  frequency=freq, adjustflag="2")
            if rs.error_code != "0":
                raise RuntimeError(rs.error_msg)
            return _normalize_bs_hist(rs.get_data() if len(rs.data) > 0 else None)
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                logger.debug("取数失败 %s(%s~%s): %s", code, start, end, e)
                return None
            time.sleep(0.5 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# 数据抓取（AkShare 直连：东财 + 新浪双通道，独立于 baostock，不受其限流影响）
# ---------------------------------------------------------------------------

def _ak_import():
    try:
        import akshare as ak  # noqa: PLC0415
        return ak
    except ImportError:
        logger.error("未安装 akshare，无法使用 --source akshare")
        return None


def _fetch_hist_ak(code: str, start: str, end: str, adjust: str = "qfq",
                   max_retry: int = 4) -> Optional[pd.DataFrame]:
    """AkShare 按显式日期区间取个股日线，标准化为与 baostock 缓存同构的列。
    通道1=东方财富 stock_zh_a_hist（含 pct_chg/turnover）；通道2=新浪 stock_zh_a_daily 兜底。"""
    ak = _ak_import()
    if ak is None:
        return None
    symbol = _ak_symbol(code)
    s, e = start.replace("-", ""), end.replace("-", "")
    for attempt in range(max_retry):
        try:
            raw = ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                     start_date=s, end_date=e, adjust=adjust)
            df = _normalize_ak_hist(raw)
            if df is not None and not df.empty:
                return df
        except Exception as ex:  # noqa: BLE001
            if attempt == max_retry - 1:
                logger.debug("ak(东财)取数失败 %s(%s~%s): %s", code, start, end, ex)
        time.sleep(0.4 * (attempt + 1))
    # 新浪兜底（返回全量历史，需截窗；无 pct_chg，用收盘价自算）
    try:
        sina_sym = f"sh{symbol}" if symbol.startswith("6") else f"sz{symbol}"
        raw = ak.stock_zh_a_daily(symbol=sina_sym, adjust=adjust)
        if raw is not None and not raw.empty and "date" in raw.columns:
            keep = [c for c in ("date", "open", "high", "low", "close", "volume", "amount", "turnover") if c in raw.columns]
            df = raw[keep].copy()
            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            for col in ("open", "high", "low", "close", "volume", "amount", "turnover"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["date", "close"]).sort_values("date")
            df["pct_chg"] = df["close"].pct_change() * 100
            df = df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)
            return df if not df.empty else None
    except Exception as ex:  # noqa: BLE001
        logger.debug("ak(新浪)取数失败 %s: %s", code, ex)
    return None


def _fetch_index_ak(start: str, end: str, symbol: str = "sh000300",
                    max_retry: int = 4) -> Optional[pd.DataFrame]:
    """AkShare 取沪深300指数日线（stock_zh_index_daily 返回全量历史，截取到 [start,end]）。
    指数无 pct_chg/turnover，pct_chg 用收盘价自算；市场环境仅需 close。"""
    ak = _ak_import()
    if ak is None:
        return None
    for attempt in range(max_retry):
        try:
            raw = ak.stock_zh_index_daily(symbol=symbol)
            if raw is not None and "date" in raw.columns and "close" in raw.columns:
                df = raw.copy()
                df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
                for col in ("open", "high", "low", "close", "volume"):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["date", "close"]).sort_values("date")
                df["pct_chg"] = df["close"].pct_change() * 100
                df = df[(df["date"] >= start) & (df["date"] <= end)].reset_index(drop=True)
                if not df.empty:
                    return df
        except Exception as ex:  # noqa: BLE001
            if attempt == max_retry - 1:
                logger.debug("ak 指数取数失败 %s: %s", symbol, ex)
        time.sleep(0.5 * (attempt + 1))
    return None


def _fetch_pool_ak(config: StrategyConfig, max_retry: int = 4) -> Optional[pd.DataFrame]:
    """AkShare 取全 A 股代码/名称列表，复用实盘 _apply_pool_filters 过滤到主板非 ST。
    注意：为当前快照（非严格 as-of BT_START），退市股可能缺失，属可接受的轻微幸存者偏差。"""
    ak = _ak_import()
    if ak is None:
        return None
    for attempt in range(max_retry):
        try:
            raw = ak.stock_info_a_code_name()
            if raw is not None and "code" in raw.columns and "name" in raw.columns:
                df = raw[["code", "name"]].copy()
                df["code"] = df["code"].astype(str).str.zfill(6)
                df["name"] = df["name"].astype(str)
                return _apply_pool_filters(df, config)
        except Exception as ex:  # noqa: BLE001
            if attempt == max_retry - 1:
                logger.debug("ak 股票池取数失败: %s", ex)
        time.sleep(1.0 * (attempt + 1))
    return None


def _fetch_industry_ak(max_retry: int = 3) -> dict:
    """AkShare 构建 6 位代码 -> 行业名 映射（东财行业板块 + 成分股）。
    仅用于决赛圈行业分散；失败时返回空 dict，调用方降级为不做行业去重（不阻断选股）。"""
    ak = _ak_import()
    if ak is None:
        return {}
    out: dict[str, str] = {}
    names: list = []
    for attempt in range(max_retry):
        try:
            boards = ak.stock_board_industry_name_em()
            if boards is not None and "板块名称" in boards.columns:
                names = boards["板块名称"].dropna().astype(str).tolist()
                break
        except Exception as ex:  # noqa: BLE001
            logger.debug("ak 行业板块列表失败(%d/%d): %s", attempt + 1, max_retry, ex)
        time.sleep(1.0 * (attempt + 1))
    if not names:
        return {}
    for i, nm in enumerate(names, 1):
        for attempt in range(2):
            try:
                cons = ak.stock_board_industry_cons_em(symbol=nm)
                if cons is not None and "代码" in cons.columns:
                    for c in cons["代码"].astype(str).str.zfill(6):
                        out.setdefault(c, nm)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5 * (attempt + 1))
        if i % 20 == 0:
            logger.info("  行业映射进度 %d/%d 板块，累计 %d 条", i, len(names), len(out))
    return out


# ---- prefetch 子进程 worker（每个进程独立登录 baostock）----

_W_BC: Optional[BacktestConfig] = None


def _worker_init(bc_dict: dict) -> None:
    global _W_BC
    _W_BC = BacktestConfig(**bc_dict)
    _bs_login_safe()


def _worker_fetch_panel(code: str) -> tuple[str, bool]:
    """抓取单只股票日线面板并落盘缓存（断点续跑：已存在则跳过）。返回 (code, ok)。"""
    assert _W_BC is not None
    bc = _W_BC
    path = _daily_cache_path(bc, code)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return code, True
    df = _fetch_hist(code, bc.DATA_START, bc.data_end(), freq="d")
    if df is None or df.empty:
        # 写一个空标记文件，避免每次重跑都重复拉取无数据标的
        try:
            pd.DataFrame(columns=["date"]).to_csv(path, index=False)
        except Exception:  # noqa: BLE001
            pass
        return code, False
    try:
        df.to_csv(path, index=False)
    except Exception:  # noqa: BLE001
        return code, False
    return code, True


def fetch_index_panel(bc: BacktestConfig) -> pd.DataFrame:
    """沪深300日线面板（市场环境 + 交易日历 + 数据时效基准）。"""
    path = _meta_path(bc, f"index_{bc.DATA_START}_{bc.data_end()}.csv")
    if os.path.exists(path) and not bc.REFRESH:
        df = pd.read_csv(path, dtype={"date": str})
        if not df.empty:
            return df
    if bc.DATA_SOURCE == "akshare":
        df = _fetch_index_ak(bc.DATA_START, bc.data_end())
    else:
        df = _fetch_hist("000300", bc.DATA_START, bc.data_end(), freq="d",
                         fields="date,open,high,low,close,volume,amount,pctChg")
    if df is None or df.empty:
        raise RuntimeError("无法获取沪深300指数行情，回测终止")
    df.to_csv(path, index=False)
    return df


def fetch_pool(bc: BacktestConfig, config: StrategyConfig) -> pd.DataFrame:
    """股票池（as-of BT_START 最近的交易日，减少幸存者偏差），复用实盘 _apply_pool_filters。"""
    path = _meta_path(bc, f"pool_{bc.BT_START}.csv")
    if os.path.exists(path) and not bc.REFRESH:
        df = pd.read_csv(path, dtype={"code": str, "name": str})
        if not df.empty:
            return df
    if bc.DATA_SOURCE == "akshare":
        df = _fetch_pool_ak(config)
        if df is None or df.empty:
            raise RuntimeError("无法获取股票列表（AkShare），回测终止")
        logger.info("股票池（AkShare 当前快照，主板非 ST）：%d 只", len(df))
        df.to_csv(path, index=False)
        return df
    # 从 BT_START 向前找最近的交易日拉取当日全部证券
    raw = None
    start = datetime.strptime(bc.BT_START, "%Y-%m-%d")
    for i in range(10):
        day = (start - timedelta(days=i)).strftime("%Y-%m-%d")
        with bs_lock:
            rs = bs.query_all_stock(day=day)
        if rs.error_code != "0":
            continue
        data = rs.get_data()
        if data is not None and not data.empty:
            raw = data
            logger.info("股票池 as-of 交易日：%s（%d 条原始记录）", day, len(raw))
            break
    if raw is None:
        raise RuntimeError("无法获取股票列表，回测终止")
    raw = raw[raw["code"].str.match(r"^(sh\.(60|68)|sz\.(00|30)|bj\.(4|8))", na=False)]
    raw = raw[raw["tradeStatus"] == "1"].copy()
    raw = raw.rename(columns={"code_name": "name"})
    raw["code"] = raw["code"].apply(lambda x: x.split(".")[1] if "." in x else x)
    df = _apply_pool_filters(raw[["code", "name"]], config)
    df.to_csv(path, index=False)
    return df


def fetch_industry(bc: BacktestConfig) -> dict:
    """行业分类（6位代码 -> 行业名），用于行业分散。一次性拉取。"""
    path = _meta_path(bc, "industry.json")
    if os.path.exists(path) and not bc.REFRESH:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    if bc.DATA_SOURCE == "akshare":
        out = _fetch_industry_ak()
        if out:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
        else:
            logger.warning("AkShare 行业映射获取失败，降级为不做行业分散（不影响其余筛选）")
        return out
    out: dict[str, str] = {}
    with bs_lock:
        rs = bs.query_stock_industry()
    if getattr(rs, "error_code", None) == "0":
        for _, row in rs.get_data().iterrows():
            code = str(row.get("code", "")).split(".")[-1].strip()
            ind = str(row.get("industry", "") or "").strip()
            if code and ind:
                out[code] = ind
    if out:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    return out


_BS_OK = True   # replay 中根据登录结果更新；False 时基本面直接走 AkShare 兜底


def _fetch_fund_ak_asof(code: str, as_of: str) -> Optional[dict]:
    """AkShare 兜底基本面（as-of）：取披露日 <= as_of 的最近一期财报的 ROE / 资产负债率。
    akshare 走东财/新浪通道，与 baostock 独立，不受其限流影响。"""
    try:
        import akshare as ak
    except ImportError:
        return None
    symbol = code.zfill(6)
    result = {"roe": None, "debt_ratio": None, "goodwill_ratio": None, "deducted_profit_ratio": None}
    try:
        df = ak.stock_financial_analysis_indicator(symbol=symbol, start_year="2024")
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty or "日期" not in df.columns:
        return None
    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df[df["日期"].notna() & (df["日期"] <= as_of)].sort_values("日期", ascending=False)
    if df.empty:
        return None
    row = df.iloc[0]
    for col in df.columns:
        cs, cl = str(col), str(col).lower()
        if result["roe"] is None and ("净资产收益率" in cs or "roe" in cl):
            v = pd.to_numeric(row[col], errors="coerce")
            if not pd.isna(v):
                result["roe"] = float(v)
        if result["debt_ratio"] is None and ("资产负债率" in cs or "debt" in cl):
            v = pd.to_numeric(row[col], errors="coerce")
            if not pd.isna(v):
                result["debt_ratio"] = float(v)
    if not any(v is not None for v in result.values()):
        return None
    return result


def fetch_fundamentals_asof(bc: BacktestConfig, code: str, as_of: str) -> Optional[dict]:
    """按 as_of 时点拉取 ROE / 资产负债率：baostock（按披露窗口回溯）优先，缺失则 AkShare 兜底。带磁盘缓存。"""
    path = _fund_cache_path(bc, code)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    result = {"roe": None, "debt_ratio": None, "goodwill_ratio": None, "deducted_profit_ratio": None}

    # 主源 baostock（按 as_of 所属披露窗口逐季回溯）
    if _BS_OK:
        quarters = _quarter_candidates(datetime.strptime(as_of, "%Y-%m-%d"), n=3)
        bs_code = _format_bs_code(code)
        for (year, quarter) in quarters:
            if result["roe"] is not None and result["debt_ratio"] is not None:
                break
            try:
                with bs_lock:
                    p_rs = bs.query_profit_data(code=bs_code, year=year, quarter=quarter)
                    b_rs = bs.query_balance_data(code=bs_code, year=year, quarter=quarter)
            except Exception:  # noqa: BLE001
                continue
            if getattr(p_rs, "error_code", None) == "0" and len(p_rs.data) > 0 and result["roe"] is None:
                dfp = p_rs.get_data()
                if "roeAvg" in dfp.columns:
                    v = pd.to_numeric(dfp["roeAvg"].iloc[0], errors="coerce")
                    if not pd.isna(v):
                        result["roe"] = float(v) * 100
            if getattr(b_rs, "error_code", None) == "0" and len(b_rs.data) > 0 and result["debt_ratio"] is None:
                dfb = b_rs.get_data()
                col = next((c for c in ("liabilityToAsset", "liabToAsset", "liabRate") if c in dfb.columns), None)
                if col:
                    v = pd.to_numeric(dfb[col].iloc[0], errors="coerce")
                    if not pd.isna(v):
                        result["debt_ratio"] = float(v) * 100

    # 兜底 AkShare（核心项仍缺失时）
    if result["roe"] is None or result["debt_ratio"] is None:
        ak_res = _fetch_fund_ak_asof(code, as_of)
        if ak_res:
            for k in ("roe", "debt_ratio"):
                if result[k] is None and ak_res.get(k) is not None:
                    result[k] = ak_res[k]

    if not any(v is not None for v in result.values()):
        return None
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    return result


# ---------------------------------------------------------------------------
# prefetch 编排
# ---------------------------------------------------------------------------

def _ak_fetch_panel(bc: BacktestConfig, code: str) -> tuple[str, bool]:
    """AkShare 抓取单只股票日线面板并落盘缓存。返回 (code, ok)。"""
    path = _daily_cache_path(bc, code)
    df = _fetch_hist_ak(code, bc.DATA_START, bc.data_end(), adjust=StrategyConfig().ADJUST)
    if df is None or df.empty:
        try:
            pd.DataFrame(columns=["date"]).to_csv(path, index=False)
        except Exception:  # noqa: BLE001
            pass
        return code, False
    try:
        df.to_csv(path, index=False)
    except Exception:  # noqa: BLE001
        return code, False
    return code, True


def _cmd_prefetch_ak(bc: BacktestConfig, config: StrategyConfig) -> None:
    """AkShare 直连预取：指数 / 股票池 / 行业 + 全池日线面板（多线程，无需 baostock 登录）。"""
    logger.info("[AkShare] 预取指数面板 / 股票池 / 行业分类 ...")
    idx = fetch_index_panel(bc)
    pool = fetch_pool(bc, config)
    ind = fetch_industry(bc)
    logger.info("[AkShare] 指数 %d 根；股票池 %d 只；行业映射 %d 条", len(idx), len(pool), len(ind))

    codes = pool["code"].astype(str).tolist()
    if bc.LIMIT > 0:
        codes = codes[: bc.LIMIT]
    if bc.REFRESH:
        todo = list(codes)
    else:
        todo = [c for c in codes if not (os.path.exists(_daily_cache_path(bc, c)) and os.path.getsize(_daily_cache_path(bc, c)) > 0)]
    logger.info("[AkShare] 需抓取日线面板 %d 只（跳过已缓存 %d 只），线程数 %d",
                len(todo), len(codes) - len(todo), bc.WORKERS)
    if not todo:
        logger.info("[AkShare] 全部面板已缓存，prefetch 完成")
        return

    t0 = time.time()
    done = ok = 0
    with ThreadPoolExecutor(max_workers=bc.WORKERS) as ex:
        futures = [ex.submit(_ak_fetch_panel, bc, c) for c in todo]
        for fut in as_completed(futures):
            try:
                _code, good = fut.result()
                ok += 1 if good else 0
            except Exception:  # noqa: BLE001
                pass
            done += 1
            if done % 200 == 0 or done == len(todo):
                el = time.time() - t0
                eta = el / done * (len(todo) - done)
                logger.info("  [AkShare] 面板抓取进度 %d/%d（有数据 %d）已用 %.1f 分钟，预计剩余 %.1f 分钟",
                            done, len(todo), ok, el / 60, eta / 60)
    logger.info("[AkShare] prefetch 完成：成功 %d / %d，用时 %.1f 分钟", ok, len(todo), (time.time() - t0) / 60)


def cmd_prefetch(bc: BacktestConfig) -> None:
    _ensure_dirs(bc)
    config = StrategyConfig()
    if bc.DATA_SOURCE == "akshare":
        _cmd_prefetch_ak(bc, config)
        return
    if not _bs_login_safe():
        raise RuntimeError("baostock 登录失败，无法预取数据")
    try:
        logger.info("预取指数面板 / 股票池 / 行业分类 ...")
        idx = fetch_index_panel(bc)
        pool = fetch_pool(bc, config)
        ind = fetch_industry(bc)
        logger.info("指数 %d 根；股票池 %d 只；行业映射 %d 条", len(idx), len(pool), len(ind))

        codes = pool["code"].astype(str).tolist()
        if bc.LIMIT > 0:
            codes = codes[: bc.LIMIT]
        todo = [c for c in codes if not (os.path.exists(_daily_cache_path(bc, c)) and os.path.getsize(_daily_cache_path(bc, c)) > 0)]
        logger.info("需抓取日线面板 %d 只（已缓存 %d 只），进程数 %d", len(todo), len(codes) - len(todo), bc.WORKERS)
        if not todo:
            logger.info("全部面板已缓存，prefetch 完成")
            return

        t0 = time.time()
        done = ok = 0
        # 每个子进程独立登录 baostock，绕开单进程 bs_lock 串行瓶颈
        with ProcessPoolExecutor(max_workers=bc.WORKERS, initializer=_worker_init,
                                 initargs=(asdict(bc),)) as ex:
            futures = [ex.submit(_worker_fetch_panel, c) for c in todo]
            for fut in as_completed(futures):
                try:
                    _code, good = fut.result()
                    ok += 1 if good else 0
                except Exception:  # noqa: BLE001
                    pass
                done += 1
                if done % 200 == 0 or done == len(todo):
                    el = time.time() - t0
                    eta = el / done * (len(todo) - done)
                    logger.info("  面板抓取进度 %d/%d（有数据 %d）已用 %.1f 分钟，预计剩余 %.1f 分钟",
                                done, len(todo), ok, el / 60, eta / 60)
        logger.info("prefetch 完成：成功 %d / %d，用时 %.1f 分钟", ok, len(todo), (time.time() - t0) / 60)
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 内存面板加载（replay 用）
# ---------------------------------------------------------------------------

@dataclass
class StockPanel:
    code: str
    name: str
    df: pd.DataFrame                 # 含 date/open/high/low/close/volume/amount/pct_chg/turnover
    dates: list                      # 升序日期字符串列表
    idx_of_date: dict                # date -> 行号


def load_panels(bc: BacktestConfig, pool: pd.DataFrame) -> dict:
    panels: dict[str, StockPanel] = {}
    name_map = dict(zip(pool["code"].astype(str), pool["name"].astype(str)))
    codes = pool["code"].astype(str).tolist()
    if bc.LIMIT > 0:
        codes = codes[: bc.LIMIT]
    for code in codes:
        path = _daily_cache_path(bc, code)
        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            continue
        try:
            df = pd.read_csv(path, dtype={"date": str})
        except Exception:  # noqa: BLE001
            continue
        if df.empty or "close" not in df.columns or len(df) < 5:
            continue
        df = df.sort_values("date").reset_index(drop=True)
        dates = df["date"].tolist()
        panels[code] = StockPanel(code=code, name=name_map.get(code, ""), df=df,
                                  dates=dates, idx_of_date={d: i for i, d in enumerate(dates)})
    return panels


def _resample_weekly(daily_upto: pd.DataFrame) -> Optional[pd.DataFrame]:
    """把「截至 d 的日线」重采样为周线（W-FRI），对应实盘单独拉取的周线窗口。
    当周为未走完的部分周，其 close=最新交易日 close，与实盘 baostock 周线行为一致。"""
    if daily_upto is None or daily_upto.empty:
        return None
    d = daily_upto.copy()
    d["_dt"] = pd.to_datetime(d["date"])
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    agg = {k: v for k, v in agg.items() if k in d.columns}
    wk = d.set_index("_dt").resample("W-FRI").agg(agg).dropna(subset=["close"]).reset_index()
    if wk.empty:
        return None
    wk["date"] = wk["_dt"].dt.strftime("%Y-%m-%d")
    return wk.drop(columns=["_dt"])


# ---------------------------------------------------------------------------
# 逐日重放（复刻 main() 的选股 + 决赛圈）
# ---------------------------------------------------------------------------

def _screen_day(bc: BacktestConfig, config: StrategyConfig, day: str,
                panels: dict, index_upto: pd.DataFrame, industry_map: dict) -> tuple[list, list, Counter]:
    """对单个交易日 d 重放选股，返回 (formal_picks, pending_picks, 否决原因计数)。"""
    market_env = compute_market_environment(index_upto, config)
    regime = market_env.get("regime", "unknown")
    reasons: Counter = Counter()

    # 组合层风控（与实盘 main() 一致，否则回测会高估实盘表现）：
    #   - 市场级熔断：指数近 MARKET_CRASH_LOOKBACK 日急跌 → 当日不选股
    #   - 推荐上限按 regime 收缩：牛 MAX_PICKS / 中性 NEUTRAL_ / 熊 BEAR_（unknown 折叠为熊）
    # 注：回测不套用 regime 滞回（_apply_regime_hysteresis 依赖磁盘状态，跨回测日会串味），
    #     因此这里的 regime 是未滞回的原始值，与实盘可能存在 1~2 个交易日的切换延迟差异。
    if market_crash_halt(index_upto, config) is not None:
        reasons["HALT_MARKET_CRASH"] += 1
        return [], [], reasons
    max_picks = resolve_max_picks(regime, config)
    if max_picks <= 0:
        reasons["HALT_MAX_PICKS_ZERO"] += 1
        return [], [], reasons

    # 1) 全池技术面筛选（fund_data=None：基本面延后到决赛圈终审，减少网络成本，最终集合等价）
    def _eval_one(sp: StockPanel):
        i = sp.idx_of_date.get(day)
        if i is None:
            return None, "FAIL_STALE"          # 当日无 K 线（停牌/滞后）
        lo = max(0, i + 1 - bc.DAILY_EVAL_BARS)
        daily_slice = sp.df.iloc[lo: i + 1]
        try:
            return evaluate(daily_slice, sp.code, sp.name, config, market_env, None,
                            latest_trade_date=day)
        except Exception:  # noqa: BLE001
            return None, "ERROR"

    signals: list[dict] = []
    pool_list = list(panels.values())
    with ThreadPoolExecutor(max_workers=bc.WORKERS) as ex:
        for sig, reason in ex.map(lambda sp: _eval_one(sp), pool_list):
            reasons[reason] += 1
            if reason == "PASS" and sig is not None:
                signals.append(sig.to_dict())

    if not signals:
        return [], [], reasons

    df = _rank_signals(pd.DataFrame(signals))

    # 2) 决赛圈：周线确认 + 行业分散 + 基本面终审，取满 max_picks（复刻 main() weekly 分支）
    weekly_enabled = config.REQUIRE_WEEKLY_TREND or config.REQUIRE_WEEKLY_MACD_STABLE
    formal: list[dict] = []
    pending: list[dict] = []
    industry_used: dict[str, int] = {}

    for _, row in df.iterrows():
        if len(formal) >= max_picks:
            break
        code = str(row["code"])
        sp = panels.get(code)
        row = row.to_dict()

        # 周线确认（截至 d 重采样）
        weekly_status = "confirmed"
        if weekly_enabled and sp is not None:
            i = sp.idx_of_date.get(day)
            wk = _resample_weekly(sp.df.iloc[: i + 1]) if i is not None else None
            weekly_ok = True
            if config.REQUIRE_WEEKLY_TREND:
                if not _weekly_trend_data_ok(wk, config):
                    weekly_status = "unverified"
                weekly_ok = check_weekly_trend(wk, config)
            if weekly_ok and config.REQUIRE_WEEKLY_MACD_STABLE:
                if not _weekly_macd_data_ok(wk, config):
                    weekly_status = "unverified"
                weekly_ok = check_weekly_macd(wk, config)
            if weekly_status == "confirmed" and not _weekly_fresh_enough(wk, day):
                weekly_status = "unverified"
            if not weekly_ok:
                continue
            row["weekly_status"] = weekly_status
            if weekly_status == "unverified":
                row["tier"] = "pending"
                pending.append(row)
                continue
        else:
            row["weekly_status"] = "disabled"

        # 行业分散
        industry = industry_map.get(code, "") or ""
        if industry and industry_used.get(industry, 0) >= config.MAX_PICKS_PER_INDUSTRY:
            continue

        # 基本面终审（as-of d 的披露窗口）
        fund = fetch_fundamentals_asof(bc, code, day)
        if bc.FILL_OPTIONAL_FUNDAMENTALS and fund is not None:
            try:
                from src.bottom_fishing_strategy import _fill_optional_fundamentals
                fund = _fill_optional_fundamentals(code, dict(fund), config)
            except Exception:  # noqa: BLE001
                pass
        if fund is None:
            row["tier"] = "pending"
            pending.append(row)
            continue
        if not check_fundamentals(fund, config, code=code, name=str(row["name"])):
            continue
        fund_status, fund_tags = _fund_verify_state(fund)
        row["fund_status"] = fund_status
        kept = [t for t in str(row.get("missing_tags", "") or "").split(",") if t and not t.startswith("fund")]
        row["missing_tags"] = ",".join(kept + fund_tags)
        if fund_status != "verified":
            row["tier"] = "pending"
            pending.append(row)
            continue

        if industry:
            industry_used[industry] = industry_used.get(industry, 0) + 1
        row["tier"] = "formal"
        row["market_env"] = regime
        formal.append(row)

    return formal, pending, reasons


def replay(bc: BacktestConfig, config: StrategyConfig) -> tuple[list, list, pd.DataFrame, Counter, list]:
    _ensure_dirs(bc)
    # 行情面板走磁盘缓存，筛选阶段无需联网；baostock 仅决赛圈拉基本面时需要。
    # 登录失败（如被限流/黑名单）不致命：继续重放，决赛圈基本面缺失的候选降级为待核验。
    global _BS_OK
    if bc.DATA_SOURCE == "akshare":
        _BS_OK = False
        logger.info("数据源=AkShare：跳过 baostock 登录，决赛圈基本面走 AkShare as-of 兜底")
    else:
        bs_ok = _bs_login_safe(max_retry=2)
        _BS_OK = bs_ok
        if not bs_ok:
            logger.warning("baostock 登录失败（可能被限流/黑名单）；行情走缓存继续重放，"
                           "决赛圈基本面将尝试 AkShare 兜底，仍缺失则相关候选降级为待核验")
    try:
        index = fetch_index_panel(bc)
        pool = fetch_pool(bc, config)
        industry_map = fetch_industry(bc)
        panels = load_panels(bc, pool)
        logger.info("已加载 %d 只股票面板；行业映射 %d 条", len(panels), len(industry_map))
        if not panels:
            raise RuntimeError("无可用面板缓存，请先运行 prefetch")

        # 交易日历：指数在 [BT_START, BT_END] 内的交易日
        cal = [d for d in index["date"].tolist() if bc.BT_START <= d <= bc.BT_END]
        logger.info("回测区间交易日 %d 天：%s ~ %s", len(cal), cal[0] if cal else "-", cal[-1] if cal else "-")

        index_dates = index["date"].tolist()
        all_picks: list[dict] = []
        all_pending: list[dict] = []
        overall_reasons: Counter = Counter()
        day_funnels: list[dict] = []
        for n, day in enumerate(cal, 1):
            t = time.time()
            j = bisect.bisect_right(index_dates, day) - 1
            index_upto = index.iloc[: j + 1]
            formal, pending, reasons = _screen_day(bc, config, day, panels, index_upto, industry_map)
            overall_reasons.update(reasons)
            day_funnels.append({"date": day, "n_pool": len(panels),
                                "n_formal": len(formal), "n_pending": len(pending), **dict(reasons)})
            for r in formal:
                r["rec_date"] = day
                all_picks.append(r)
            for r in pending:
                r["rec_date"] = day
                all_pending.append(r)
            top = ", ".join(f"{_REASON_ZH.get(k, k)}={v}" for k, v in reasons.most_common(4) if k != "PASS")
            logger.info("[%d/%d] %s 正式推荐 %d 只，待核验 %d 只（%.1fs）| 主要否决：%s%s",
                        n, len(cal), day, len(formal), len(pending), time.time() - t, top,
                        " | 入选 " + ", ".join(f"{r['name']}({r['code']}){r['score']}" for r in formal) if formal else "")
        return all_picks, all_pending, index, overall_reasons, day_funnels
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 收益模拟
# ---------------------------------------------------------------------------

def simulate_trade(bc: BacktestConfig, pick: dict, panels: dict, trading_days: list) -> dict:
    """对一条推荐做收益模拟：次日开盘买入 → 盘中止损/止盈 → 否则持有到期末按收盘退出。

    返回含 entry/exit/收益/峰值/逐日盯市序列 的 dict。
    """
    code = str(pick["code"])
    d = str(pick["rec_date"])
    sp = panels.get(code)
    out = {
        "rec_date": d, "code": code, "name": pick.get("name", ""),
        "score": pick.get("score"), "grade": pick.get("grade"),
        "market_env": pick.get("market_env"), "has_divergence": bool(pick.get("has_divergence")),
        "signals_hit": pick.get("signals_hit", ""),
        "rec_close": pick.get("close"), "stop_loss": pick.get("stop_loss"),
        "take_profit": pick.get("take_profit"),
    }
    if sp is None:
        out.update(entry_date=None, entry_price=None, exit_date=None, exit_price=None,
                   exit_reason="NO_PANEL", return_pct=np.nan, peak_pct=np.nan, hold_days=0, mtm=[])
        return out

    df = sp.df
    dates = sp.dates
    i = sp.idx_of_date.get(d)
    if i is None:
        out.update(entry_date=None, entry_price=None, exit_date=None, exit_price=None,
                   exit_reason="NO_REC_BAR", return_pct=np.nan, peak_pct=np.nan, hold_days=0, mtm=[])
        return out

    stop = float(pick.get("stop_loss")) if pick.get("stop_loss") is not None else None
    tp = float(pick.get("take_profit")) if pick.get("take_profit") is not None else None

    # 入场
    if bc.ENTRY_MODE == "next_open":
        ei = i + 1
        if ei >= len(df):
            out.update(entry_date=None, entry_price=None, exit_date=None, exit_price=None,
                       exit_reason="NO_NEXT_BAR", return_pct=np.nan, peak_pct=np.nan, hold_days=0, mtm=[])
            return out
        entry_price = float(df.iloc[ei]["open"])
        entry_date = dates[ei]
        start_i = ei            # 从入场当日开始监控止损止盈
    else:  # rec_close
        entry_price = float(df.iloc[i]["close"])
        entry_date = d
        start_i = i + 1

    if entry_price <= 0:
        out.update(entry_date=entry_date, entry_price=entry_price, exit_date=None, exit_price=None,
                   exit_reason="BAD_ENTRY", return_pct=np.nan, peak_pct=np.nan, hold_days=0, mtm=[])
        return out

    # 持有上限（交易日数）
    max_i = len(df) - 1
    if bc.MAX_HOLD_DAYS > 0:
        max_i = min(max_i, start_i + bc.MAX_HOLD_DAYS - 1)

    exit_i = None
    exit_price = None
    exit_reason = "PERIOD_END"
    peak_close = entry_price
    mtm = []  # 逐日盯市：(date, close)

    for k in range(start_i, max_i + 1):
        bar = df.iloc[k]
        low = float(bar["low"]) if not pd.isna(bar["low"]) else float(bar["close"])
        high = float(bar["high"]) if not pd.isna(bar["high"]) else float(bar["close"])
        close = float(bar["close"])
        mtm.append((dates[k], close))
        if not pd.isna(close):
            peak_close = max(peak_close, close)
        if bc.USE_STOP_TP and stop is not None and tp is not None:
            hit_stop = low <= stop
            hit_tp = high >= tp
            if hit_stop and hit_tp:
                if bc.STOP_FIRST_ON_BOTH:
                    exit_i, exit_price, exit_reason = k, stop, "STOP_LOSS"
                else:
                    exit_i, exit_price, exit_reason = k, tp, "TAKE_PROFIT"
                break
            if hit_stop:
                exit_i, exit_price, exit_reason = k, stop, "STOP_LOSS"
                break
            if hit_tp:
                exit_i, exit_price, exit_reason = k, tp, "TAKE_PROFIT"
                break
    if exit_i is None:
        # 未触发止损止盈：持有到期末（数据末根或 max_hold 上限）按收盘退出
        exit_i = max_i
        exit_price = float(df.iloc[exit_i]["close"])
        exit_reason = "PERIOD_END" if bc.MAX_HOLD_DAYS <= 0 else "TIMEOUT"
        if not mtm:
            mtm.append((dates[exit_i], exit_price))

    ret = (exit_price - entry_price) / entry_price * 100
    peak_ret = (peak_close - entry_price) / entry_price * 100
    out.update(
        entry_date=entry_date, entry_price=round(entry_price, 3),
        exit_date=dates[exit_i], exit_price=round(exit_price, 3),
        exit_reason=exit_reason, return_pct=round(ret, 3), peak_pct=round(peak_ret, 3),
        hold_days=int(exit_i - start_i + 1), mtm=mtm,
    )
    return out


def simulate_all(bc: BacktestConfig, picks: list, panels: dict, trading_days: list) -> list:
    trades = []
    for p in picks:
        trades.append(simulate_trade(bc, p, panels, trading_days))
    return trades


# ---------------------------------------------------------------------------
# 月度统计
# ---------------------------------------------------------------------------

def _group(trades: pd.DataFrame, key: str, labels: Optional[dict] = None) -> list:
    rows = []
    if key not in trades.columns or trades.empty:
        return rows
    for val, sub in trades.groupby(key, dropna=False):
        n = len(sub)
        win = int((sub["return_pct"] > 0).sum())
        rows.append({
            "label": (labels or {}).get(val, str(val)),
            "n": int(n),
            "win_rate": round(win / n * 100, 1) if n else 0.0,
            "avg_return": round(float(sub["return_pct"].mean()), 2) if n else 0.0,
            "avg_peak": round(float(sub["peak_pct"].mean()), 2) if n else 0.0,
        })
    return rows


def build_monthly_stats(bc: BacktestConfig, trades: list, index: pd.DataFrame,
                        overall_reasons: Optional[Counter] = None,
                        day_funnels: Optional[list] = None, n_pool: int = 0) -> dict:
    """两套收益口径：每笔等权平均 + 每日等权组合逐日盯市净值曲线。"""
    # 只保留可成交（有 entry/exit）的交易
    valid = [t for t in trades if t.get("entry_price") and t.get("exit_price") is not None
             and not pd.isna(t.get("return_pct", np.nan))]
    tdf = pd.DataFrame(valid) if valid else pd.DataFrame(
        columns=["return_pct", "peak_pct", "grade", "market_env", "has_divergence", "exit_reason", "rec_date"])

    n = len(tdf)
    per_trade = {
        "n_trades": n,
        "n_picks_total": len(trades),
        "win_rate": round(float((tdf["return_pct"] > 0).mean() * 100), 2) if n else 0.0,
        "avg_return": round(float(tdf["return_pct"].mean()), 3) if n else 0.0,
        "median_return": round(float(tdf["return_pct"].median()), 3) if n else 0.0,
        "avg_peak": round(float(tdf["peak_pct"].mean()), 3) if n else 0.0,
        "best": round(float(tdf["return_pct"].max()), 3) if n else 0.0,
        "worst": round(float(tdf["return_pct"].min()), 3) if n else 0.0,
        "avg_hold_days": round(float(tdf["hold_days"].mean()), 1) if n else 0.0,
        "by_grade": _group(tdf, "grade"),
        "by_market": _group(tdf, "market_env", {"bull": "牛市", "bear": "熊市", "neutral": "中性", "unknown": "未知"}),
        "by_divergence": _group(tdf, "has_divergence", {True: "有底背离", False: "无底背离"}),
        "by_exit": _group(tdf, "exit_reason", {"STOP_LOSS": "止损", "TAKE_PROFIT": "止盈",
                                               "PERIOD_END": "持有到期末", "TIMEOUT": "到期"}),
    }

    # 组合口径：每日投入等额资金 DAILY_BUDGET，等权分给当日推荐；逐日盯市所有未平仓+已平仓头寸
    portfolio = _build_portfolio_curve(bc, valid)

    # 基准：沪深300 在回测区间的买入持有收益
    idx = index[(index["date"] >= bc.BT_START) & (index["date"] <= bc.BT_END)]
    bench = None
    if len(idx) >= 2:
        bench = round((float(idx["close"].iloc[-1]) / float(idx["close"].iloc[0]) - 1) * 100, 3)

    funnel = {"n_pool": n_pool, "n_days": len(day_funnels or []), "by_reason": [], "daily": day_funnels or []}
    if overall_reasons:
        total_evals = sum(overall_reasons.values())
        for reason, cnt in overall_reasons.most_common():
            funnel["by_reason"].append({
                "reason": reason, "label": _REASON_ZH.get(reason, reason), "count": int(cnt),
                "pct": round(cnt / total_evals * 100, 2) if total_evals else 0.0,
            })
        funnel["total_evals"] = int(total_evals)

    return {"period": f"{bc.BT_START} ~ {bc.BT_END}", "per_trade": per_trade,
            "portfolio": portfolio, "benchmark_csi300_pct": bench, "funnel": funnel}


def _build_portfolio_curve(bc: BacktestConfig, valid: list) -> dict:
    """每日等额投入、等权分配的逐日盯市组合。

    假设：每个交易日独立投入 DAILY_BUDGET 资金（不复用、不加杠杆），等权分给当日推荐；
    每笔按其 entry/exit 与逐日收盘盯市。用于衡量信号质量的组合化表现，非真实账户资金曲线。
    """
    if not valid:
        return {"total_invested": 0.0, "final_value": 0.0, "portfolio_return_pct": 0.0,
                "max_drawdown_pct": 0.0, "n_screen_days": 0, "curve": []}

    # 收集所有相关交易日（覆盖到最后一笔退出日）
    all_days = sorted({t["entry_date"] for t in valid} | {t["exit_date"] for t in valid}
                      | {d for t in valid for d, _ in t.get("mtm", [])})
    # 每笔交易的逐日价值序列（现金/持仓）
    # 持仓期：shares * close；退出后：exit 现金。入场前：0（尚未投入）。
    per_trade_series = []
    total_invested = 0.0
    for t in valid:
        budget = bc.DAILY_BUDGET / max(1, _cohort_size(valid, t["rec_date"]))
        total_invested += budget
        shares = budget / t["entry_price"]
        exit_val = shares * t["exit_price"]
        mtm_map = dict(t.get("mtm", []))
        series = {}
        for day in all_days:
            if day < t["entry_date"]:
                continue               # 尚未入场，组合层按已划拨现金计
            if day >= t["exit_date"]:
                series[day] = exit_val  # 已退出，按退出现金计
            elif day in mtm_map:
                series[day] = shares * mtm_map[day]   # 持仓中，按当日收盘盯市
        per_trade_series.append((t, budget, series, exit_val))

    # 组合逐日净值：把每笔在 day 的价值（持仓盯市 / 退出后现金）相加；未入场的资金视为已投入现金
    curve = []
    peak = -1e18
    max_dd = 0.0
    for day in all_days:
        val = 0.0
        for (t, budget, series, exit_val) in per_trade_series:
            if day < t["entry_date"]:
                val += budget          # 已划拨但尚未入场，按现金计
            elif day in series and series[day] is not None:
                val += series[day]
            elif day >= t["exit_date"]:
                val += exit_val
            else:
                val += budget
        curve.append({"date": day, "equity": round(val, 2),
                      "return_pct": round((val / total_invested - 1) * 100, 3) if total_invested else 0.0})
        peak = max(peak, val)
        if peak > 0:
            max_dd = min(max_dd, (val / peak - 1) * 100)

    final_value = curve[-1]["equity"] if curve else total_invested
    return {
        "total_invested": round(total_invested, 2),
        "final_value": round(final_value, 2),
        "portfolio_return_pct": round((final_value / total_invested - 1) * 100, 3) if total_invested else 0.0,
        "max_drawdown_pct": round(max_dd, 3),
        "n_screen_days": len({t["rec_date"] for t in valid}),
        "curve": curve,
    }


def _cohort_size(valid: list, rec_date: str) -> int:
    return sum(1 for t in valid if t["rec_date"] == rec_date)


def _prev_day(all_days: list, day: str) -> Optional[str]:
    i = bisect.bisect_left(all_days, day)
    return all_days[i - 1] if i > 0 else None


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------

def write_outputs(bc: BacktestConfig, picks: list, pending: list, trades: list, stats: dict) -> dict:
    _ensure_dirs(bc)
    paths = {}
    picks_df = pd.DataFrame(picks)
    trades_df = pd.DataFrame([{k: v for k, v in t.items() if k != "mtm"} for t in trades])
    curve_df = pd.DataFrame(stats["portfolio"].get("curve", []))

    if not picks_df.empty:
        p = os.path.join(bc.OUT_DIR, "picks.csv")
        picks_df.to_csv(p, index=False, encoding="utf-8-sig")
        paths["picks"] = p
    if not trades_df.empty:
        p = os.path.join(bc.OUT_DIR, "trades.csv")
        trades_df.to_csv(p, index=False, encoding="utf-8-sig")
        paths["trades"] = p
    if not curve_df.empty:
        p = os.path.join(bc.OUT_DIR, "equity_curve.csv")
        curve_df.to_csv(p, index=False, encoding="utf-8-sig")
        paths["equity"] = p
    if pending:
        p = os.path.join(bc.OUT_DIR, "pending_candidates.csv")
        pd.DataFrame(pending).to_csv(p, index=False, encoding="utf-8-sig")
        paths["pending"] = p
    daily_funnel = stats.get("funnel", {}).get("daily", [])
    if daily_funnel:
        p = os.path.join(bc.OUT_DIR, "funnel.csv")
        pd.DataFrame(daily_funnel).to_csv(p, index=False, encoding="utf-8-sig")
        paths["funnel"] = p
    p = os.path.join(bc.OUT_DIR, "monthly_stats.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    paths["stats"] = p

    report = _render_report(bc, stats, picks_df, trades_df)
    rp = os.path.join(bc.OUT_DIR, "backtest_report.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write(report)
    paths["report"] = rp
    return paths


def _fmt_group(rows: list) -> str:
    if not rows:
        return "_（无样本）_"
    lines = ["| 分组 | 样本 | 胜率 | 平均收益 | 平均峰值 |", "|---|---|---|---|---|"]
    for g in rows:
        lines.append(f"| {g['label']} | {g['n']} | {g['win_rate']}% | {g['avg_return']}% | {g['avg_peak']}% |")
    return "\n".join(lines)


def _render_report(bc: BacktestConfig, stats: dict, picks_df: pd.DataFrame, trades_df: pd.DataFrame) -> str:
    pt = stats["per_trade"]
    pf = stats["portfolio"]
    bench = stats.get("benchmark_csi300_pct")
    L = []
    L.append(f"# 选股策略回测报告（{stats['period']}）\n")
    L.append("> 本报告由 `backtest.py` 生成，逐日重放实盘同款选股逻辑（`src/bottom_fishing_strategy.py`），"
             "严格截断到每个交易日、无未来函数。\n")
    L.append("## 一、回测设置\n")
    L.append(f"- 选股区间：{bc.BT_START} ~ {bc.BT_END}（区间内每个交易日各跑一次选股）")
    L.append(f"- 行情面板：{bc.DATA_START} ~ {bc.data_end()}（每日只取截至当日、最近 {bc.DAILY_EVAL_BARS} 根日线，与实盘一致）")
    L.append(f"- 买入价：**{ '次日开盘价' if bc.ENTRY_MODE=='next_open' else '信号日收盘价' }**")
    L.append(f"- 卖出规则：**盘中触发 +{StrategyConfig().FIXED_TAKE_PROFIT_PCT:.0f}% 止盈 / -{StrategyConfig().FIXED_STOP_LOSS_PCT:.0f}% 止损**"
             f"（同根 K 线两者都触及按{'止损' if bc.STOP_FIRST_ON_BOTH else '止盈'}保守成交），否则持有到{'数据期末' if bc.MAX_HOLD_DAYS<=0 else str(bc.MAX_HOLD_DAYS)+'个交易日'}按收盘退出")
    L.append(f"- 股票池：沪深主板（60/00）非 ST，as-of {bc.BT_START}" + (f"，本次限制前 {bc.LIMIT} 只" if bc.LIMIT else ""))
    _cfg0 = StrategyConfig()
    L.append(f"- 每日推荐上限：牛 {_cfg0.MAX_PICKS} / 中性 {_cfg0.NEUTRAL_MAX_PICKS} / 熊（含 unknown）{_cfg0.BEAR_MAX_PICKS} 只"
             f"，同行业最多 {_cfg0.MAX_PICKS_PER_INDUSTRY} 只")
    L.append(f"- 市场级熔断：沪深300 近 {_cfg0.MARKET_CRASH_LOOKBACK} 个交易日累计跌幅 < {_cfg0.MARKET_CRASH_HALT_PCT:.1f}% 的交易日整日不选股")
    L.append(f"- 停牌缺口过滤：相邻 K 线间隔 > {_cfg0.MAX_BAR_GAP_DAYS} 天判为曾停牌，否决（K线跨缺口时滚动指标失真）")
    L.append(f"- 排序主键：rank_score = 技术分 + 连续质量分（权重 {_cfg0.RANK_QUALITY_WEIGHT:.0f}：低波/回撤深度/区间位置/动能速率）")
    L.append(f"- 基本面：仅对决赛圈候选按披露窗口回溯拉取（as-of）；商誉/扣非补齐={'开启' if bc.FILL_OPTIONAL_FUNDAMENTALS else '关闭（主源缺失即放行，终审略宽松）'}\n")

    L.append("## 二、总体收益\n")
    L.append("| 口径 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 正式推荐笔数（可成交） | {pt['n_trades']}（生成 {pt['n_picks_total']} 条） |")
    L.append(f"| **每笔等权平均收益** | **{pt['avg_return']}%** |")
    L.append(f"| 每笔收益中位数 | {pt['median_return']}% |")
    L.append(f"| **每笔胜率** | **{pt['win_rate']}%** |")
    L.append(f"| 平均峰值收益（持有期最大浮盈） | {pt['avg_peak']}% |")
    L.append(f"| 最佳 / 最差单笔 | {pt['best']}% / {pt['worst']}% |")
    L.append(f"| 平均持有交易日 | {pt['avg_hold_days']} |")
    L.append(f"| **组合口径收益（每日等额投入、逐日盯市）** | **{pf['portfolio_return_pct']}%** |")
    L.append(f"| 组合最大回撤 | {pf['max_drawdown_pct']}% |")
    L.append(f"| 组合投入 / 期末市值 | {pf['total_invested']} / {pf['final_value']} |")
    if bench is not None:
        excess = round(pt["avg_return"] - bench, 3)
        L.append(f"| 基准沪深300同期买入持有 | {bench}%（每笔平均超额 {excess}%） |")
    L.append("")
    L.append("> 组合口径假设每个交易日独立投入等额资金（不复用、不加杠杆）等权买入当日推荐，"
             "用于衡量信号的组合化表现，非真实账户资金曲线；每笔口径把所有推荐当独立交易等权平均，"
             "与项目 `run_monthly_attribution.py` 的归因口径一致。\n")

    fn = stats.get("funnel", {})
    L.append("## 三、选股漏斗（全池逐日否决原因分布）\n")
    if fn.get("by_reason"):
        pass_n = next((r["count"] for r in fn["by_reason"] if r["reason"] == "PASS"), 0)
        L.append(f"区间内对 {fn.get('n_pool', 0)} 只主板股票 × {fn.get('n_days', 0)} 个交易日 = "
                 f"{fn.get('total_evals', 0)} 次评估，技术面通过（PASS）{pass_n} 次。各「首个否决原因」分布如下：\n")
        L.append("| 否决原因 | 次数 | 占比 |")
        L.append("|---|---|---|")
        for r in fn["by_reason"]:
            L.append(f"| {r['label']} | {r['count']} | {r['pct']}% |")
        L.append("")
        L.append("> 一只股票在某日被多个条件同时否决时，只记录 `evaluate()` 按判定顺序返回的**首个**原因，"
                 "因此上表反映「最先卡在哪一关」，用于定位策略在当期市场的主要瓶颈。\n")
    else:
        L.append("_（无漏斗数据）_\n")

    L.append("## 四、分维度归因\n")
    L.append("**按等级**\n\n" + _fmt_group(pt["by_grade"]) + "\n")
    L.append("**按市场环境**\n\n" + _fmt_group(pt["by_market"]) + "\n")
    L.append("**按底背离**\n\n" + _fmt_group(pt["by_divergence"]) + "\n")
    L.append("**按退出原因**\n\n" + _fmt_group(pt["by_exit"]) + "\n")

    L.append("## 五、每日推荐与逐日收益\n")
    if not picks_df.empty and not trades_df.empty:
        daily = trades_df.groupby("rec_date").agg(
            n=("code", "count"),
            avg_ret=("return_pct", "mean"),
            win=("return_pct", lambda s: (s > 0).mean() * 100),
        ).round(2)
        L.append("| 交易日 | 推荐数 | 当日等权平均收益 | 胜率 |")
        L.append("|---|---|---|---|")
        for day, r in daily.iterrows():
            L.append(f"| {day} | {int(r['n'])} | {r['avg_ret']}% | {r['win']:.0f}% |")
        L.append("")
    else:
        L.append("_（区间内无正式推荐）_\n")

    L.append("## 六、明细文件\n")
    L.append("- `picks.csv`：每日正式推荐明细（代码/名称/评分/等级/止损止盈/入选依据）")
    L.append("- `trades.csv`：每笔交易的入场/退出/收益/峰值/持有天数/退出原因")
    L.append("- `equity_curve.csv`：组合逐日盯市净值曲线")
    L.append("- `funnel.csv`：逐日选股漏斗（每日各否决原因计数）")
    L.append("- `monthly_stats.json`：全部统计结构化数据")
    L.append("- `pending_candidates.csv`：待核验候选（财务/周线数据缺失，未进正式推荐）\n")
    L.append("---\n_免责声明：本回测仅为策略逻辑的历史复现研究，含简化假设（成交价、资金模型、"
             "基本面时点近似），不构成投资建议，历史表现不代表未来收益。_")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout, force=True)


def _apply_args(bc: BacktestConfig, args: argparse.Namespace) -> BacktestConfig:
    if getattr(args, "start", None):
        bc.BT_START = args.start
    if getattr(args, "end", None):
        bc.BT_END = args.end
    if getattr(args, "data_end", None):
        bc.DATA_END = args.data_end
    if getattr(args, "limit", 0):
        bc.LIMIT = args.limit
    if getattr(args, "workers", 0):
        bc.WORKERS = args.workers
    if getattr(args, "max_hold_days", None) is not None:
        bc.MAX_HOLD_DAYS = args.max_hold_days
    if getattr(args, "entry", None):
        bc.ENTRY_MODE = args.entry
    if getattr(args, "fill_fund", False):
        bc.FILL_OPTIONAL_FUNDAMENTALS = True
    if getattr(args, "source", None):
        bc.DATA_SOURCE = args.source
    if getattr(args, "refresh", False):
        bc.REFRESH = True
    if bc.DATA_SOURCE == "akshare":
        # 与 baostock 旧缓存物理隔离，确保本次数据全部来自 akshare 接口
        bc.CACHE_DIR = os.path.join(_PROJECT_ROOT, "backtest_cache_ak")
    return bc


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--start", help="回测起始日 YYYY-MM-DD（默认 2026-08-01）")
    p.add_argument("--end", help="回测结束日 YYYY-MM-DD（默认 2026-09-01）")
    p.add_argument("--data-end", dest="data_end", help="行情面板末日（默认今天，决定持有退出的期末）")
    p.add_argument("--limit", type=int, default=0, help="只取股票池前 N 只（冒烟测试）")
    p.add_argument("--workers", type=int, default=0, help="并发数（prefetch 进程 / replay 线程）")
    p.add_argument("--max-hold-days", dest="max_hold_days", type=int, help="最多持有 N 个交易日（默认 0=持有到数据期末）")
    p.add_argument("--entry", choices=["next_open", "rec_close"], help="买入价口径")
    p.add_argument("--fill-fund", action="store_true", help="决赛圈用 AkShare 补齐商誉/扣非（慢）")
    p.add_argument("--source", choices=["baostock", "akshare"], help="行情数据源（baostock 被限流时用 akshare 直连）")
    p.add_argument("--refresh", action="store_true", help="忽略已有缓存，全部重新从接口拉取")


def main(argv: Optional[list] = None) -> None:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="backtest.py", description="选股策略历史回测（逐日重放 + 月度收益统计）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_pre = sub.add_parser("prefetch", help="预取行情面板到磁盘缓存（可断点续跑）")
    _add_common(p_pre)
    p_run = sub.add_parser("run", help="逐日重放 + 收益模拟 + 月度统计 + 报告")
    _add_common(p_run)
    p_all = sub.add_parser("all", help="prefetch 后直接 run")
    _add_common(p_all)
    args = parser.parse_args(argv)

    bc = _apply_args(BacktestConfig(), args)
    config = StrategyConfig()
    _ensure_dirs(bc)

    t0 = time.time()
    if args.cmd in ("prefetch", "all"):
        logger.info("=" * 60)
        logger.info("Step 1/2 预取行情数据 ...")
        cmd_prefetch(bc)
    if args.cmd in ("run", "all"):
        logger.info("=" * 60)
        logger.info("Step 2/2 逐日重放选股 ...")
        picks, pending, index, overall_reasons, day_funnels = replay(bc, config)
        logger.info("重放完成：正式推荐 %d 条，待核验 %d 条，用时 %.1f 分钟",
                    len(picks), len(pending), (time.time() - t0) / 60)

        panels = load_panels(bc, fetch_pool(bc, config))
        cal = [d for d in index["date"].tolist() if bc.BT_START <= d <= bc.BT_END]
        logger.info("模拟收益（%d 笔）...", len(picks))
        trades = simulate_all(bc, picks, panels, cal)
        stats = build_monthly_stats(bc, trades, index, overall_reasons, day_funnels, n_pool=len(panels))
        paths = write_outputs(bc, picks, pending, trades, stats)

        pt = stats["per_trade"]
        pf = stats["portfolio"]
        logger.info("=" * 60)
        logger.info("回测结果（%s）", stats["period"])
        logger.info("  每笔：可成交 %d 笔 | 平均收益 %.3f%% | 胜率 %.1f%% | 平均峰值 %.3f%%",
                    pt["n_trades"], pt["avg_return"], pt["win_rate"], pt["avg_peak"])
        logger.info("  组合：收益 %.3f%% | 最大回撤 %.3f%% | 投入 %s → 期末 %s",
                    pf["portfolio_return_pct"], pf["max_drawdown_pct"], pf["total_invested"], pf["final_value"])
        if stats.get("benchmark_csi300_pct") is not None:
            logger.info("  基准：沪深300同期 %.3f%%", stats["benchmark_csi300_pct"])
        logger.info("  报告：%s", paths.get("report"))
        logger.info("总耗时 %.1f 分钟", (time.time() - t0) / 60)


if __name__ == "__main__":
    main()
