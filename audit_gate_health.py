"""补充审计（只读）：
1) ATR 闸门是否已成死闸门（阈值 vs 主板真实 ATR% 分布）；
2) 止跌闸门的极端筛选率与样本坍塌；
3) 各闸门在本样本中的「触发数」——判断哪些闸门真正起作用；
4) 市场环境确认（本样本是否全部处于下跌期，影响结论可外推性）。
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.bottom_fishing_strategy import (  # noqa: E402
    StrategyConfig, compute_daily_signals, compute_market_environment,
    _recompute_pct_chg, _valuation_pe_score, qv_floor_equivalence, describe_qv_floor,
)

MIN_BARS = 250
DAY = "2026-09-21"


def main():
    cfg = StrategyConfig()
    fs = sorted(glob.glob("backtest_cache/daily/*.csv"))
    atr_pcts, tech_scores, close_pos, pes = [], [], [], []
    for f in fs:
        try:
            d = pd.read_csv(f)
        except Exception:
            continue
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        for c in ("open", "high", "low", "close", "volume", "amount", "peTTM"):
            if c in d.columns:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        if not {"volume", "amount"} <= set(d.columns):
            continue
        d = d.sort_values("date").reset_index(drop=True)
        d = d[d["date"] <= DAY]
        d = d[(d["volume"] > 0) & (d["amount"] > 0)].reset_index(drop=True)
        if len(d) < MIN_BARS:
            continue
        t = compute_daily_signals(_recompute_pct_chg(d), cfg)
        if t is None:
            continue
        r = t.iloc[-1]
        if not np.isfinite(r.get("atr", np.nan)):
            continue
        close = float(r["close"])
        atr_pcts.append(float(r["atr"]) / close * 100)
        tech_scores.append(float(r["daily_score"]))
        w = d.tail(MIN_BARS)["close"].to_numpy(dtype=float)
        close_pos.append(float(((w < close).sum() + .5 * (w == close).sum()) / len(w)))
        try:
            pe = float(d["peTTM"].iloc[-1])
            if np.isfinite(pe):
                pes.append(pe)
        except Exception:
            pass

    A = np.array(atr_pcts)
    T = np.array(tech_scores)
    P = np.array(close_pos)
    E = np.array(pes)
    print(f"样本 {len(A)} 只（主板，决策日 {DAY}）\n")
    print(f"【A】ATR% 真实分布 vs MAX_ATR_PCT={cfg.MAX_ATR_PCT}")
    print("  分位:", {f"P{p}": round(float(np.percentile(A, p)), 2)
                      for p in (10, 50, 75, 90, 95, 99)})
    print("  最大值 %.2f； 超过阈值(%.1f)的占比 %.3f%%" % (A.max(), cfg.MAX_ATR_PCT, (A > cfg.MAX_ATR_PCT).mean() * 100))
    print("→ 结论：", "死闸门（几乎不筛选）" if (A > cfg.MAX_ATR_PCT).mean() < 0.005 else "仍有筛选力")

    print("\n【B】技术分分布 vs MIN_TECHNICAL_SCORE_FORMAL=%.0f" % cfg.MIN_TECHNICAL_SCORE_FORMAL)
    vals, cnts = np.unique(T, return_counts=True)
    print("  取值:", {float(v): int(c) for v, c in zip(vals, cnts)})
    print("  <45 占比 %.1f%%；>=45 占比 %.1f%%" % ((T < 45).mean() * 100, (T >= 45).mean() * 100))
    print("  非零取值个数 =", len(vals), "→ 评分实际是", "离散布尔求和，非连续评分" if len(vals) <= 8 else "连续")

    print("\n【C】250日分位 vs LOW_POSITION_MAX=%.2f" % cfg.LOW_POSITION_MAX)
    print("  <=0.4 占比 %.1f%%" % ((P <= 0.4).mean() * 100))

    print("\n【D】PE 分布（>0 者）vs MAX_PE_TTM=%.0f" % cfg.MAX_PE_TTM)
    Ep = E[E > 0]
    print("  PE>0 占比 %.1f%%；(0,25] 占比 %.1f%%（全样本）" % (
        (E > 0).mean() * 100, ((E > 0) & (E <= cfg.MAX_PE_TTM)).mean() * 100))
    print("  PE>0 分位:", {f"P{p}": round(float(np.percentile(Ep, p)), 1)
                           for p in (10, 25, 50, 75, 90)})

    print("\n【E】综合分下限的等效门槛（项目自带算术）")
    print(" ", describe_qv_floor(cfg))
    q = qv_floor_equivalence(cfg)
    for k, v in q.items():
        print(f"   {k} = {v}")

    print("\n【F】本样本的市场环境（决定结论可外推性）")
    f = sorted(glob.glob("backtest_cache/index_*.csv"))
    if f:
        idx = pd.read_csv(f[-1])
        idx["date"] = pd.to_datetime(idx["date"]).dt.strftime("%Y-%m-%d")
        idx = idx.sort_values("date").reset_index(drop=True)
        for day in ("2026-05-08", "2026-05-29", "2026-06-19", "2026-08-21"):
            up = idx[idx["date"] <= day].reset_index(drop=True)
            env = compute_market_environment(up, cfg)
            r60 = (up["close"].iloc[-1] / up["close"].iloc[-60] - 1) * 100 if len(up) > 60 else np.nan
            print(f"  {day}: regime={env.get('regime')}  沪深300近60日{r60:+.1f}%")


if __name__ == "__main__":
    main()