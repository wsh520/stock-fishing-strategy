"""方案 A 参数扫描：MIN_TECHNICAL_SCORE_FORMAL 下调对 formal 产出的影响。

在多个决策日 × 主板真实面板上，扫描技术分门槛 30/35/38/40/42/45，
输出每档的 formal 产出数、止跌层构成、以及**新增放行的样本特征**（是否仍在低位、
是否已涨离底部）—— 用于确认「放宽后放行的仍是好票」而不是随便放行。
"""
import glob
import json
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.bottom_fishing_strategy import StrategyConfig, has_halt_gap, evaluate_quality_value
from src.fundamental_quality import evaluate_annual_quality

DAYS = ["2026-05-08", "2026-05-29", "2026-07-10", "2026-07-31",
        "2026-08-21", "2026-09-04", "2026-09-21"]
THRESHOLDS = [45, 42, 40, 38, 35, 30]


def fake_operating(code, day):
    from src.recent_operating import _FIELDS
    m = {k: 100.0 for k in _FIELDS.values()}
    m.update({"revenue_yoy": 5.0, "net_profit_yoy": 6.0, "deducted_profit_yoy": 7.0,
              "gross_margin_change_pp": 0.0, "net_margin_change_pp": 0.0,
              "operating_cashflow_change": 1.0, "operating_cashflow_yoy": 5.0})
    return {"code": str(code).zfill(6), "as_of": day, "status": "verified",
            "period": "2026-06-30", "available_date": "2026-08-28",
            "metrics": m, "missing_tags": [], "reasons": []}


def main():
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
    ind = json.load(open("backtest_cache/industry.json", encoding="utf-8"))

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
            av = [str(x.get("available_date"))[:10] for x in a
                  if isinstance(x, dict) and x.get("available_date")]
            ys = [x.get("year") for x in a if isinstance(x, dict)]
            if not av or not ys or max(av) > day:
                continue
            if best is None or max(ys) > best[0]:
                best = (max(ys), a)
        return best[1] if best else None

    # 先算出「候选池」：除技术分门槛外全部通过（含止跌层），一次算完复用
    base = StrategyConfig()
    pool = []   # (day, code, df, fund, ctx, tech_score, stab, gain, pos, pe, q, v)
    for day in DAYS:
        snap = {}
        for code, d in panels.items():
            s = d[d["date"] <= day]
            if len(s) < 250 or not ind.get(code):
                continue
            try:
                pe = float(s["peTTM"].iloc[-1])
            except Exception:
                continue
            if np.isfinite(pe):
                snap.setdefault(ind[code], []).append(pe)
        snap = {k: np.sort(np.array(v)) for k, v in snap.items()
                if len(v) >= base.VALUATION_INDUSTRY_MIN_PEERS}
        for code, d in panels.items():
            s = d[d["date"] <= day].reset_index(drop=True)
            if len(s) < 250 or s["date"].iloc[-1] != day or has_halt_gap(s, base):
                continue
            if float(s["amount"].tail(20).mean()) < base.MIN_AMOUNT:
                continue
            w = s.tail(250)
            close = float(s["close"].iloc[-1])
            cw = w["close"].to_numpy(float)
            pos = float(((cw < close).sum() + .5 * (cw == close).sum()) / len(cw))
            if pos > base.LOW_POSITION_MAX:
                continue
            try:
                pe = float(s["peTTM"].iloc[-1])
                pb = float(s["pbMRQ"].iloc[-1])
            except Exception:
                continue
            if not np.isfinite(pe) or pe <= 0:
                continue
            ctx = None
            if ind.get(code) in snap:
                p = float((snap[ind[code]] <= pe).sum() / len(snap[ind[code]]))
                if p > base.VALUATION_INDUSTRY_PERCENTILE_MAX:
                    continue
                ctx = {"mode": "industry", "pe_pct": p, "pb_pct": None}
            elif pe > base.MAX_PE_TTM:
                continue
            ar = annual_for(code, day)
            if ar is None:
                continue
            q = evaluate_annual_quality(
                ar, day, years=base.QUALITY_YEARS,
                median_roe_min=base.QUALITY_MEDIAN_ROE_MIN, min_roe=base.QUALITY_MIN_ROE,
                cash_conversion_min=base.QUALITY_CASH_CONVERSION_MIN, financial=False,
                require_annual_net_profit_positive=base.QUALITY_REQUIRE_ANNUAL_NET_PROFIT_POSITIVE,
                require_annual_cashflow_positive=base.QUALITY_REQUIRE_ANNUAL_CASHFLOW_POSITIVE)
            if q["status"] != "verified":
                continue
            fund = {"debt_ratio": 40., "roe": 2., "annual_rows": ar,
                    "operating_trend": fake_operating(code, day)}
            # 用最低门槛跑一次，取出止跌层与技术分
            cfg_lo = StrategyConfig(MIN_TECHNICAL_SCORE_FORMAL=0)
            try:
                sig, reason = evaluate_quality_value(s, code, "X", cfg_lo, {"regime": "neutral"},
                                                     fund, latest_trade_date=day, val_context=ctx)
            except Exception:
                continue
            if sig is None:
                continue
            # 只保留「止跌层 strong/medium」的（本方案只调技术分，不动止跌层）
            if sig.stabilization_level not in ("strong", "medium"):
                continue
            lo = float(w["low"].min())
            pool.append(dict(day=day, code=code, df=s, fund=fund, ctx=ctx,
                             tech=float(sig.daily_score), stab=sig.stabilization_level,
                             gain=(close / lo - 1) * 100, pos=pos, pe=pe,
                             q=float(sig.quality_score), v=float(sig.valuation_score),
                             score=float(sig.score), rr=float(sig.rank_score)))
    print(f"候选池（止跌 strong/medium + 财务 verified + 估值通过）共 {len(pool)} 条\n")

    print("=== 各技术分门槛下的 formal 产出（按 7 个决策日）===")
    base_rows = None
    for th in THRESHOLDS:
        got = [r for r in pool if r["tech"] >= th]
        per_day = Counter(r["day"] for r in got)
        days_n = len(DAYS)
        dist = [per_day.get(d, 0) for d in DAYS]
        print(f"  门槛 {th:>3}：合计 {len(got):>3} 条 | 均值 {len(got)/days_n:>4.2f} 只/日 | "
              f"各日 {dist} | 空仓 {sum(1 for x in dist if x==0)}/{days_n}")
        if th == 45:
            base_rows = {r["code"] + r["day"] for r in got}
    got40 = [r for r in pool if r["tech"] >= 40]
    added = [r for r in got40 if r["code"] + r["day"] not in base_rows]
    print(f"\n=== 45 → 40 新增放行 {len(added)} 条，其特征 ===")
    if added:
        A = pd.DataFrame(added)
        print(f"  技术分分布：{sorted(A.tech.tolist())}")
        print(f"  距低点涨幅： 中位 {A.gain.median():+.1f}%  P75 {A.gain.quantile(.75):+.1f}%  最大 {A.gain.max():+.1f}%")
        print(f"  250日分位： 中位 {A.pos.median():.3f}")
        print(f"  PE中位 {A.pe.median():.1f}   质量分均值 {A.q.mean():.1f}   估值分均值 {A.v.mean():.1f}")
        print(f"  止跌层构成：{A.stab.value_counts().to_dict()}")
        print(f"  距低点涨幅超 15%（应被高位保护拦住，实际不会到这里）：{(A.gain>15).sum()} 条")
        print("\n  明细：")
        for _, r in A.iterrows():
            print(f"    {r['day']} {r['code']}  技术分 {r['tech']:>5.1f}  分位 {r['pos']:.3f}  "
                  f"距低点 {r['gain']:+5.1f}%  PE {r['pe']:.1f}  {r['stab']}")
    else:
        print("  （无新增）")


if __name__ == "__main__":
    main()
