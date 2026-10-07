"""按 rank_score 排序，量化「取前 N 只」的质量分层——为「每天 2~3 只」找依据。

要回答的问题：
  1. 每日通过闸门的 9~10 只，按 rank_score 取前 2/3/5 只，被舍弃那些有什么共同特征？
  2. 被舍弃的是否确实更差（距离低点涨幅更大 / PE 更贵 / 质量分更低 / 止跌层更弱）？
  3. 若无显著差异 → 说明「取前 N」只是任意截断，需要靠**门槛**而非**截断**来控量。
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.bottom_fishing_strategy import StrategyConfig, has_halt_gap, evaluate_quality_value
from src.fundamental_quality import evaluate_annual_quality

MIN_BARS = 250
DAYS = ["2026-05-08", "2026-05-29", "2026-07-10", "2026-07-31",
        "2026-08-21", "2026-09-04", "2026-09-21"]


def load():
    panels = {}
    for f in sorted(glob.glob("backtest_cache/daily/*.csv")):
        code = os.path.splitext(os.path.basename(f))[0]
        try:
            d = pd.read_csv(f)
        except Exception:
            continue
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        for c in ("open", "high", "low", "close", "volume", "amount", "peTTM", "pbMRQ"):
            if c in d.columns:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        if not {"volume", "amount"} <= set(d.columns):
            continue
        d = d.sort_values("date").reset_index(drop=True)
        d = d[(d["volume"] > 0) & (d["amount"] > 0)].reset_index(drop=True)
        panels[code] = d
    return panels


def annual_for(code, day):
    best = None
    for h in sorted(glob.glob(f"cache/annual_quality_v1_{code}_*.json")):
        try:
            j = json.load(open(h, encoding="utf-8"))
        except Exception:
            continue
        a = j.get("annual_rows") or []
        if isinstance(a, dict):
            a = [a]
        av = [str(x.get("available_date"))[:10] for x in a if isinstance(x, dict) and x.get("available_date")]
        ys = [x.get("year") for x in a if isinstance(x, dict)]
        if not av or not ys or max(av) > day:
            continue
        if best is None or max(ys) > best[0]:
            best = (max(ys), a)
    return best[1] if best else None


def main():
    cfg = StrategyConfig()
    panels = load()
    ind = json.load(open("backtest_cache/industry.json", encoding="utf-8"))
    allrows = []
    for day in DAYS:
        snap = {}
        for code, d in panels.items():
            s = d[d["date"] <= day]
            if len(s) < MIN_BARS or not ind.get(code):
                continue
            try:
                pe = float(s["peTTM"].iloc[-1])
            except Exception:
                continue
            if np.isfinite(pe):
                snap.setdefault(ind[code], []).append(pe)
        snap = {k: np.sort(np.array(v)) for k, v in snap.items()
                if len(v) >= cfg.VALUATION_INDUSTRY_MIN_PEERS}
        for code, d in panels.items():
            s = d[d["date"] <= day].reset_index(drop=True)
            if len(s) < MIN_BARS or s["date"].iloc[-1] != day or has_halt_gap(s, cfg):
                continue
            if float(s["amount"].tail(20).mean()) < cfg.MIN_AMOUNT:
                continue
            w = s.tail(cfg.LOW_POSITION_LOOKBACK)
            close = float(s["close"].iloc[-1])
            cw = w["close"].to_numpy(dtype=float)
            pos = float(((cw < close).sum() + .5 * (cw == close).sum()) / len(cw))
            if pos > cfg.LOW_POSITION_MAX:
                continue
            try:
                pe = float(s["peTTM"].iloc[-1])
            except Exception:
                continue
            if pe is None or not np.isfinite(pe) or pe <= 0:
                continue
            ctx = None
            if ind.get(code) in snap:
                p = float((snap[ind[code]] <= pe).sum() / len(snap[ind[code]]))
                if p > cfg.VALUATION_INDUSTRY_PERCENTILE_MAX:
                    continue
                ctx = {"mode": "industry", "pe_pct": p, "pb_pct": None}
            elif pe > cfg.MAX_PE_TTM:
                continue
            ar = annual_for(code, day)
            if ar is None:
                continue
            q = evaluate_annual_quality(
                ar, day, years=cfg.QUALITY_YEARS,
                median_roe_min=cfg.QUALITY_MEDIAN_ROE_MIN, min_roe=cfg.QUALITY_MIN_ROE,
                cash_conversion_min=cfg.QUALITY_CASH_CONVERSION_MIN, financial=False,
                require_annual_net_profit_positive=cfg.QUALITY_REQUIRE_ANNUAL_NET_PROFIT_POSITIVE,
                require_annual_cashflow_positive=cfg.QUALITY_REQUIRE_ANNUAL_CASHFLOW_POSITIVE)
            if q["status"] != "verified":
                continue
            fund = {"debt_ratio": 40., "roe": 2., "annual_rows": ar}
            try:
                sig, reason = evaluate_quality_value(s, code, "X", cfg, {"regime": "neutral"},
                                                     fund, latest_trade_date=day, val_context=ctx)
            except Exception:
                continue
            if sig is None or reason != "PASS":
                continue
            if sig.tier != "formal":
                continue
            lo = float(w["low"].min())
            allrows.append(dict(day=day, code=code, rank=sig.rank_score, score=sig.score,
                                q=sig.quality_score, v=sig.valuation_score, t=sig.daily_score,
                                pe=pe, pos=pos, gain=(close / lo - 1) * 100,
                                stab=sig.stabilization_level))
    R = pd.DataFrame(allrows)
    if R.empty:
        print("无通过样本")
        return
    R["rank_pos"] = R.groupby("day")["rank"].rank(ascending=False, method="first")
    print(f"总样本 {len(R)} 条（{R['day'].nunique()} 个决策日 × 每日 {R.groupby('day').size().mean():.1f} 只）\n")
    print("=== 按 rank_score 排序后，各分层的特征均值 ===")
    tiers = []
    for name, sel in (("前1(第1名)", R.rank_pos == 1),
                      ("前2(1-2名)", R.rank_pos <= 2),
                      ("前3(1-3名)", R.rank_pos <= 3),
                      ("前5(1-5名)", R.rank_pos <= 5),
                      ("落选(6名之后)", R.rank_pos > 5)):
        s = R[sel]
        if s.empty:
            continue
        tiers.append(dict(组=name, n=len(s),
                          距低点涨幅=s.gain.median(),
                          PE中位=s.pe.median(),
                          分位中位=s.pos.median(),
                          质量分=s.q.mean(), 估值分=s.v.mean(), 技术分=s.t.mean(),
                          止跌强中=s.stab.isin(["strong"]).mean() * 100,
                          止跌弱占比=s.stab.eq("weak").mean() * 100))
    T = pd.DataFrame(tiers)
    for c in ("距低点涨幅", "分位中位"):
        T[c] = T[c].round(3)
    for c in ("PE中位", "质量分", "估值分", "技术分"):
        T[c] = T[c].round(1)
    for c in ("止跌强中", "止跌弱占比"):
        T[c] = T[c].round(0)
    print(T.to_string(index=False))

    print("\n=== 关键判断：落选组是否显著更差？===")
    keep2 = R[R.rank_pos <= 2]
    drop = R[R.rank_pos > 3]
    print(f"  距低点涨幅：前2名中位 {keep2.gain.median():+.1f}%  vs 落选组中位 {drop.gain.median():+.1f}%"
          f"  （落选更{'贵' if drop.gain.median() > keep2.gain.median() else '便宜'}）")
    print(f"  质量分    ：前2名均值 {keep2.q.mean():.1f}  vs 落选组均值 {drop.q.mean():.1f}")
    print(f"  估值分    ：前2名均值 {keep2.v.mean():.1f}  vs 落选组均值 {drop.v.mean():.1f}")
    print(f"  止跌弱占比：前2名 {keep2.stab.eq('weak').mean()*100:.0f}%  vs 落选组 {drop.stab.eq('weak').mean()*100:.0f}%")
    # 相关性：rank 与各特征的秩相关
    print("\n=== rank_score 与各特征的秩相关（相关性弱=截断近乎任意）===")
    for c in ("gain", "pos", "pe", "q", "v", "t"):
        rho = R[["rank", c]].corr(method="spearman").iloc[0, 1]
        print(f"  rank vs {c:<6} ρ = {rho:+.3f}")


if __name__ == "__main__":
    main()
