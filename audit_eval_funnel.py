"""独立审计脚本（只读、不改产线口径）：用项目真实函数 + 本地缓存面板，
实测 quality_value 荐股闸门在真实数据上的通过率与相互关系。
目的：为「策略评价」提供可复现的量化证据，而不是引用文档宣称。
"""
import glob
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.bottom_fishing_strategy import (  # noqa: E402
    StrategyConfig, compute_daily_signals, compute_relative_strength,
    has_halt_gap, _valuation_pe_score, _recompute_pct_chg,
)

DAY = "2026-09-21"  # 面板共同的最后交易日
MIN_BARS = 250


def load_panels(max_n=None):
    out = {}
    for f in sorted(glob.glob("backtest_cache/daily/*.csv")):
        code = os.path.splitext(os.path.basename(f))[0]
        try:
            d = pd.read_csv(f)
        except Exception:
            continue
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        d = d.sort_values("date").reset_index(drop=True)
        out[code] = d
        if max_n and len(out) >= max_n:
            break
    return out


def upto(df, day):
    return df[df["date"] <= day].reset_index(drop=True)


def main():
    cfg = StrategyConfig()
    panels = load_panels()
    print(f"面板总数 {len(panels)}；决策日 {DAY}")

    idx = None
    idx_file = sorted(glob.glob("backtest_cache/index_*.csv"))
    if idx_file:
        idx = pd.read_csv(idx_file[-1])
        idx["date"] = pd.to_datetime(idx["date"]).dt.strftime("%Y-%m-%d")
        idx = idx[idx["date"] <= DAY].reset_index(drop=True)

    c = Counter()
    tech_scores = []
    rows = []

    for code, d0 in panels.items():
        d = upto(d0, DAY)
        if len(d) < max(MIN_BARS, cfg.MIN_DAYS):
            c["FAIL_DATA/不足250根"] += 1
            continue
        if d["date"].iloc[-1] != DAY:
            c["FAIL_STALE/非共同交易日"] += 1
            continue
        for col in ("open", "high", "low", "close", "volume", "amount"):
            d[col] = pd.to_numeric(d[col], errors="coerce")
        d = d[(d["volume"] > 0) & (d["amount"] > 0)].reset_index(drop=True)
        if len(d) < MIN_BARS:
            c["FAIL_DATA/不足250根"] += 1
            continue
        if has_halt_gap(d, cfg):
            c["FAIL_HALT_GAP"] += 1
            continue
        c["通过数据/时效/停牌"] += 1

        if float(d["amount"].tail(20).mean()) < cfg.MIN_AMOUNT:
            c["FAIL_LIQUIDITY"] += 1
            continue

        w = d.tail(MIN_BARS)
        close = float(d["close"].iloc[-1])
        cw = w["close"].to_numpy(dtype=float)
        pos = float(((cw < close).sum() + 0.5 * (cw == close).sum()) / len(cw))
        if pos > cfg.LOW_POSITION_MAX:
            c["FAIL_POSITION(250日分位>0.4)"] += 1
            continue
        c["通过250日低位"] += 1

        pe = d["peTTM"].iloc[-1]
        pb = d["pbMRQ"].iloc[-1]
        try:
            pe = float(pe) if np.isfinite(float(pe)) else None
        except Exception:
            pe = None
        try:
            pb = float(pb) if np.isfinite(float(pb)) else None
        except Exception:
            pb = None
        if pe is None:
            c["估值缺PE(pending)"] += 1
            continue
        if pe <= 0:
            c["FAIL_VALUATION(PE<=0)"] += 1
            continue
        if pe > cfg.MAX_PE_TTM:
            c["FAIL_VALUATION(PE>25 绝对回退口径)"] += 1
            continue
        c["通过PE(绝对口径≤25)"] += 1

        # 技术面
        dd = _recompute_pct_chg(d)
        tech = compute_daily_signals(dd, cfg)
        if tech is None:
            c["FAIL_DATA/技术指标"] += 1
            continue
        t = tech.iloc[-1]
        ts = float(t["daily_score"])
        tech_scores.append(ts)
        atr = float(t["atr"]) if np.isfinite(t.get("atr", np.nan)) else None
        if atr is None:
            c["FAIL_VOLATILE(ATR缺失)"] += 1
            continue
        atr_pct = atr / close * 100
        if atr_pct > cfg.MAX_ATR_PCT:
            c["FAIL_VOLATILE(ATR%>10)"] += 1
            continue
        c["通过ATR风控"] += 1

        if ts < cfg.MIN_TECHNICAL_SCORE_FORMAL:
            c["pending: 技术分<45"] += 1
        else:
            c["通过技术分≥45"] += 1

        # 止跌确认（strict）
        ma20 = tech["ma20"].iloc[-1]
        ma20_prev = tech["ma20"].iloc[-(cfg.STABILIZATION_MA20_SLOPE_DAYS + 1)]
        ma20_ok = bool(np.isfinite(ma20) and np.isfinite(ma20_prev) and ma20_prev > 0
                       and (ma20 - ma20_prev) / ma20_prev >= 0
                       and tech["close"].iloc[-1] >= ma20)
        macd_ok = bool(np.isfinite(tech["macd_histogram"].iloc[-4:]).all()
                       and (tech["macd_histogram"].iloc[-1] - tech["macd_histogram"].iloc[-2]) > 0
                       and (tech["macd_histogram"].iloc[-2] - tech["macd_histogram"].iloc[-3]) > 0
                       and (tech["macd_histogram"].iloc[-3] - tech["macd_histogram"].iloc[-4]) > 0)
        if ma20_ok or macd_ok:
            c["通过止跌确认"] += 1
            stab = "ma20" if ma20_ok else "macd"
        else:
            c["FAIL_STABILIZATION(止跌未确认)"] += 1
            continue

        rs = compute_relative_strength(d, idx, cfg) if idx is not None else None
        vsc = _valuation_pe_score(pe, None, cfg)
        rows.append(dict(code=code, close=close, pos=pos, pe=pe, pb=pb, tech=ts,
                         atr_pct=atr_pct, rs=rs, stab=stab, vsc=vsc))
        c["★到达止跌层(尚未核验财务)"] += 1

    n = len(panels)
    print("\n=== 真实数据漏斗（决策日 %s，样本 %d 只）===" % (DAY, n))
    for k, v in c.most_common():
        print(f"  {k:38s} {v:5d}  {v/n*100:5.1f}%")

    if rows:
        r = pd.DataFrame(rows)
        print("\n=== 到达止跌层的 %d 只候选，横截面特征 ===" % len(r))
        print(r[["close", "pos", "pe", "tech", "atr_pct", "vsc", "rs"]].describe(
            percentiles=[.25, .5, .75, .9]).T.to_string())
        print("\n技术分分布：", np.percentile(r["tech"], [10, 25, 50, 75, 90]).round(1))
        print("止跌证据构成：", r["stab"].value_counts().to_dict())
        print(f"PE<=10 占比 {(r['pe']<=10).mean()*100:.1f}%；PB<=1 占比 {(r['pb']<=1).mean()*100:.1f}%")


if __name__ == "__main__":
    main()