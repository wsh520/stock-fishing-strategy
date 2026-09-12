"""main() 主流程分层回归测试（打桩数据源，不访问网络）。

单测 test_entry_filters.py 覆盖的是「单只股票的判定逻辑」，本文件覆盖 main() 的编排逻辑：
1. 周线路径：财务完整 → 正式推荐（formal）；财务缺失 → 待核验；周线缺失 → 待核验
2. 周线开关全关路径：weekly_status=disabled，财务缺失者仍不进正式名单
3. 待核验候选通过 pending_out 传出，不与正式推荐混排
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import numpy as np
import pandas as pd

import bottom_fishing_strategy as m

LATEST = "2025-06-27"  # 基准交易日（周五）

_FAILS: list[str] = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    if not ok:
        _FAILS.append(label)


def daily_df(end: str = LATEST) -> pd.DataFrame:
    """缓跌 + 走平 + 放量小阳反弹（与单测 A 场景同构），末根日期可控"""
    part1 = np.linspace(20.0, 17.0, 90)
    part2 = np.linspace(17.0, 14.5, 25)[1:]
    closes = np.concatenate([part1, part2, np.full(4, 14.5)])
    closes = np.append(closes, closes[-1] * 1.035)
    n = len(closes)
    dates = pd.date_range(end=pd.Timestamp(end), periods=n).strftime("%Y-%m-%d")
    open_ = np.concatenate([[closes[0]], closes[:-1]])
    volume = np.full(n, 10_000_000.0)
    volume[-1] = 30_000_000.0
    pct = pd.Series(closes).pct_change().fillna(0) * 100
    return pd.DataFrame({
        "date": dates, "open": open_, "high": np.maximum(open_, closes) * 1.01,
        "low": np.minimum(open_, closes) * 0.99, "close": closes, "volume": volume,
        "amount": closes * volume, "pct_chg": pct.values,
    })


def index_df(end: str = LATEST, rows: int = 40) -> pd.DataFrame:
    dates = pd.date_range(end=pd.Timestamp(end), periods=rows).strftime("%Y-%m-%d")
    closes = np.linspace(3500.0, 3600.0, rows)
    return pd.DataFrame({"date": dates, "close": closes, "open": closes,
                         "high": closes * 1.005, "low": closes * 0.995,
                         "volume": 1e8, "amount": 1e11, "pct_chg": 0.1})


def weekly_df(end: str = LATEST, rows: int = 40) -> pd.DataFrame:
    """40 根上升周线，末根落在最新交易日当周（周五）→ 数据充分且新鲜"""
    dates = pd.date_range(end=pd.Timestamp(end), periods=rows, freq="W-FRI").strftime("%Y-%m-%d")
    closes = np.linspace(12.0, 16.0, rows)
    return pd.DataFrame({"date": dates, "open": closes, "high": closes * 1.01,
                         "low": closes * 0.99, "close": closes, "volume": 1e6,
                         "amount": 1e7, "pct_chg": 0.2})


def _stub_sources() -> None:
    """打桩全部数据源，避免任何网络访问"""
    m.get_index_daily = lambda config, cache=None: index_df()
    m.get_stock_list = lambda config, cache=None: [
        {"code": "600000", "name": "财务完整"},
        {"code": "600001", "name": "财务缺失"},
        {"code": "600002", "name": "周线缺失"},
    ]
    m.get_daily_data = lambda code, config, cache=None: daily_df()
    m.get_fundamentals = lambda code, cache=None, config=None: (
        {"roe": 12.0, "debt_ratio": 45.0} if code == "600000" else None)
    m.get_stock_industry = lambda config, cache=None: {}
    m._fetch_weekly_dual = lambda code, config: (None if code == "600002" else weekly_df())
    m._bs_login = lambda max_retry=5: True
    m._bs_logout = lambda: None
    m._AK_AVAILABLE = False  # 关闭 AkShare 补齐：验证「商誉/扣非缺失只记标签、不降级」


def main() -> int:
    _stub_sources()

    # ===== 场景 1：周线路径（默认配置） =====
    cfg = m.StrategyConfig()
    cfg.FETCH_DELAY = 0.0
    cfg.MAX_WORKERS = 1
    pending: list[dict] = []
    df = m.main(config=cfg, cache=m.CacheManager(), pending_out=pending)
    check("周线路径: 正式推荐 1 只（财务完整者）",
          df is not None and len(df) == 1 and df.iloc[0]["code"] == "600000")
    if df is not None and len(df) == 1:
        row = df.iloc[0]
        check("周线路径: tier=formal", row["tier"] == "formal")
        check("周线路径: weekly_status=confirmed", row["weekly_status"] == "confirmed")
        check("周线路径: fund_status=verified", row["fund_status"] == "verified")
        check("周线路径: 可选项缺失记录在 missing_tags",
              "fund_goodwill" in str(row["missing_tags"]) and "fund_deducted" in str(row["missing_tags"]))
        check("周线路径: 入选依据非空", bool(str(row["signals_hit"]).strip()))
        check("周线路径: 等级与分数同源", row["grade"] == m._grade_from_score(float(row["score"]), cfg))
    pend = {r["code"]: r for r in pending}
    check("周线路径: 待核验候选 2 只", len(pending) == 2)
    check("周线路径: 财务缺失→pending/fund_status",
          pend.get("600001", {}).get("tier") == "pending"
          and pend.get("600001", {}).get("fund_status") == "missing")
    check("周线路径: 周线缺失→pending/weekly_status",
          pend.get("600002", {}).get("tier") == "pending"
          and pend.get("600002", {}).get("weekly_status") == "unverified")

    # ===== 场景 2：周线开关全关（disabled 路径 + 无周线时仍做财务终审） =====
    cfg2 = m.StrategyConfig()
    cfg2.FETCH_DELAY = 0.0
    cfg2.MAX_WORKERS = 1
    cfg2.REQUIRE_WEEKLY_TREND = False
    cfg2.REQUIRE_WEEKLY_MACD_STABLE = False
    pending2: list[dict] = []
    df2 = m.main(config=cfg2, cache=m.CacheManager(), pending_out=pending2)
    check("禁用路径: 正式推荐 1 只（财务完整者）",
          df2 is not None and len(df2) == 1 and df2.iloc[0]["code"] == "600000")
    if df2 is not None and len(df2) == 1:
        check("禁用路径: weekly_status=disabled", df2.iloc[0]["weekly_status"] == "disabled")
        check("禁用路径: 财务完整者 tier=formal", df2.iloc[0]["tier"] == "formal")
    check("禁用路径: 财务缺失者进入待核验（不占正式名额）",
          any(r["code"] == "600001" for r in pending2))

    total = 13
    print(f"\n{total - len(_FAILS)}/{total} 通过")
    return 0 if not _FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
