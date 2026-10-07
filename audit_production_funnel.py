"""生产口径复核：quality_value 默认走 USE_INDUSTRY_RELATIVE_VALUATION=True（行业内分位≤0.60），
上一轮 audit 用的是绝对 PE≤25 回退口径，低估了产出。本脚本按生产口径重算漏斗，
并给出「宁缺毋滥」原则下可安全修复的空间。

只读：不修改任何产线口径。
"""
import glob
import json
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.bottom_fishing_strategy import (  # noqa: E402
    StrategyConfig, compute_daily_signals, has_halt_gap, _recompute_pct_chg,
    _industry_valuation_context, _valuation_pe_score, _is_financial_stock,
)
from src.fundamental_quality import evaluate_annual_quality  # noqa: E402

MIN_BARS = 250
DAY = "2026-09-21"


def load_panels():
    out = {}
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
        out[code] = d
    return out


def main():
    cfg = StrategyConfig()
    assert cfg.USE_INDUSTRY_RELATIVE_VALUATION, "生产默认应为行业相对估值"
    panels = load_panels()
    ind = json.load(open("backtest_cache/industry.json", encoding="utf-8"))

    # 构建行业 PE/PB 横截面（对齐生产 build_industry_valuation_snapshot 的口径）
    snap = {}
    for code, d in panels.items():
        if len(d) < MIN_BARS:
            continue
        s = ind.get(code, "")
        if not s:
            continue
        try:
            pe = float(d["peTTM"].iloc[-1])
            pb = float(d["pbMRQ"].iloc[-1])
        except Exception:
            continue
        if not np.isfinite(pe):
            continue
        snap.setdefault(s, []).append((pe, pb if np.isfinite(pb) else None))
    snapshot = {}
    for s, vals in snap.items():
        pes = np.array([v[0] for v in vals])
        pbs = np.array([v[1] for v in vals if v[1] is not None])
        if len(pes) < 2:
            continue
        pe_sorted = np.sort(pes)
        pb_sorted = np.sort(pbs) if len(pbs) == len(pes) else None
        snapshot[s] = {"pe_sorted": pe_sorted.tolist(),
                       "pb_sorted": pb_sorted.tolist() if pb_sorted is not None else None}
    print(f"行业快照：{len(snapshot)} 个行业可比较（分位口径）\n")

    c = Counter()
    surv = []
    for code, d in panels.items():
        sub = d[d["date"] <= DAY].reset_index(drop=True)
        if len(sub) < max(MIN_BARS, cfg.MIN_DAYS) or has_halt_gap(sub, cfg):
            c["1停牌/数据不足"] += 1
            continue
        if sub["date"].iloc[-1] != DAY:
            continue
        c["1通过数据/时效/停牌"] += 1
        if float(sub["amount"].tail(20).mean()) < cfg.MIN_AMOUNT:
            c["2流动性不足"] += 1
            continue
        w = sub.tail(MIN_BARS)
        close = float(sub["close"].iloc[-1])
        cw = w["close"].to_numpy(dtype=float)
        pos = float(((cw < close).sum() + .5 * (cw == close).sum()) / len(cw))
        if pos > cfg.LOW_POSITION_MAX:
            c["3低位不合格"] += 1
            continue
        c["2通过流动性+低位"] += 1

        pe = pb = None
        try:
            pe = float(sub["peTTM"].iloc[-1])
            pb = float(sub["pbMRQ"].iloc[-1])
        except Exception:
            pass
        if pe is None or not np.isfinite(pe):
            c["4估值数据缺失(pending)"] += 1
            continue
        if pe <= 0:
            c["4 PE<=0 否决"] += 1
            continue
        ind_name = ind.get(code, "")
        pe_pct = pb_pct = None
        if ind_name and ind_name in snapshot:
            s = snapshot[ind_name]
            arr = np.array(s["pe_sorted"])
            if len(arr) >= cfg.VALUATION_INDUSTRY_MIN_PEERS:
                pe_pct = float((arr <= pe).sum() / len(arr))
                if s["pb_sorted"] and pb and np.isfinite(pb):
                    b = np.array(s["pb_sorted"])
                    pb_pct = float((b <= pb).sum() / len(b))
        if pe_pct is not None:
            if pe_pct > cfg.VALUATION_INDUSTRY_PERCENTILE_MAX:
                c["4行业PE分位>0.6 否决"] += 1
                continue
            c["3通过PE(行业分位口径)"] += 1
        elif pe > cfg.MAX_PE_TTM:
            c["4 PE>25 绝对回退否决"] += 1
            continue
        else:
            c["3通过PE(绝对回退口径)"] += 1

        tech = compute_daily_signals(_recompute_pct_chg(sub), cfg)
        if tech is None:
            continue
        t = tech.iloc[-1]
        atr = float(t["atr"]) if np.isfinite(t.get("atr", np.nan)) else None
        atr_pct = atr / close * 100 if atr else None
        if atr_pct is None or atr_pct > cfg.MAX_ATR_PCT:
            c["5 ATR风控否决(0只=死闸门)"] += 1
            continue
        c["4通过ATR风控"] += 1
        techscore = float(t["daily_score"])
        if techscore < cfg.MIN_TECHNICAL_SCORE_FORMAL:
            c["6技术分<45(pending)"] += 1
            continue
        c["5通过技术分>=45"] += 1

        ma20 = tech["ma20"].iloc[-1]
        mp = tech["ma20"].iloc[-(cfg.STABILIZATION_MA20_SLOPE_DAYS + 1)]
        ma20ok = bool(np.isfinite(ma20) and np.isfinite(mp) and mp > 0
                      and (ma20 - mp) / mp >= 0 and close >= ma20)
        h = tech["macd_histogram"].iloc[-4:]
        macdok = bool(np.isfinite(h).all() and all(h.iloc[i] - h.iloc[i + 1] > 0 for i in range(3)))
        if not (ma20ok or macdok):
            c["7止跌确认否决"] += 1
            continue
        c["6通过止跌确认"] += 1
        surv.append(dict(code=code, pos=pos, pe=pe, pe_pct=pe_pct, tech=techscore,
                         from_low=(close / float(cw.min()) - 1) * 100, via="ma20" if ma20ok else "macd"))

    n = len(panels)
    print("=" * 70)
    print(f"生产口径漏斗（{n} 只主板，决策日 {DAY}）")
    print("=" * 70)
    for k in sorted(c):
        print(f"  {k:<30} {c[k]:>5} 只  {c[k]/n*100:>5.1f}%")

    if surv:
        print(f"\n通过全部技术+估值闸门：{len(surv)} 只")
        S = pd.DataFrame(surv)
        print(f"  距 250 日低点涨幅：中位 {S['from_low'].median():+.1f}%"
              f"（已涨超 15% 的占 {(S['from_low']>15).mean()*100:.0f}%）")
        # 财务核验
        ok = failed = other = 0
        for r in surv:
            hits = sorted(glob.glob(f"cache/annual_quality_v1_{r['code']}_*.json"))
            ar = None
            for hh in hits:
                try:
                    j = json.load(open(hh, encoding="utf-8"))
                except Exception:
                    continue
                a = j.get("annual_rows") or []
                if isinstance(a, dict):
                    a = [a]
                ys = [x.get("year") for x in a if isinstance(x, dict)]
                if ys and max(ys) <= 2025 and (ar is None or len(a) > len(ar)):
                    ar = a
            if ar is None:
                other += 1
                continue
            res = evaluate_annual_quality(
                ar, DAY, years=cfg.QUALITY_YEARS,
                median_roe_min=cfg.QUALITY_MEDIAN_ROE_MIN, min_roe=cfg.QUALITY_MIN_ROE,
                cash_conversion_min=cfg.QUALITY_CASH_CONVERSION_MIN, financial=False,
                require_annual_net_profit_positive=cfg.QUALITY_REQUIRE_ANNUAL_NET_PROFIT_POSITIVE,
                require_annual_cashflow_positive=cfg.QUALITY_REQUIRE_ANNUAL_CASHFLOW_POSITIVE)
            if res.get("status") == "verified":
                ok += 1
            elif res.get("status") == "failed":
                failed += 1
            else:
                other += 1
        print(f"\n叠加 3 年年度质量核验：")
        print(f"   verified（正式推荐候选）= {ok}")
        print(f"   failed（质量不达标）    = {failed}")
        print(f"   partial/missing（待核验）= {other}")


if __name__ == "__main__":
    main()