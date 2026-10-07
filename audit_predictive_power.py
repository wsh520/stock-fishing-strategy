"""独立审计（只读）：对 quality_value 的各闸门做前瞻收益检验（cross-sectional IC）。

动机：项目内所有 ρ 结论均来自 n=168、单一决策日（2026-06-04）的样本。
本脚本用本地 800 只面板、多决策日、多个前瞻窗口重算，检验：
  1) 各闸门（250日分位/PE/技术分/止跌确认/相对强度/ATR%）与 F5/F10/F20/F60 的秩相关；
  2) 「通过 vs 未通过」两组的收益差异（闸门的增量判别力）；
  3) 综合分与未来收益的关系（评分是否只是排序装饰）。
所有计算只用决策日及之前的价格做信号，前瞻收益用之后真实行情。
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

MIN_BARS = 250


def spearman(a, b):
    x = pd.Series(a).rank()
    y = pd.Series(b).rank()
    m = x.notna() & y.notna()
    if m.sum() < 8:
        return np.nan, int(m.sum())
    return float(np.corrcoef(x[m], y[m])[0, 1]), int(m.sum())


def load_panels():
    out = {}
    for f in sorted(glob.glob("backtest_cache/daily/*.csv")):
        code = os.path.splitext(os.path.basename(f))[0]
        try:
            d = pd.read_csv(f)
        except Exception:
            continue
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        for col in ("open", "high", "low", "close", "volume", "amount", "peTTM", "pbMRQ"):
            if col in d.columns:
                d[col] = pd.to_numeric(d[col], errors="coerce")
        if not {"volume", "amount"} <= set(d.columns):
            continue
        d = d.sort_values("date").reset_index(drop=True)
        d = d[(d["volume"] > 0) & (d["amount"] > 0)].reset_index(drop=True)
        out[code] = d
    return out


def features_at(d, day, cfg, idx):
    """返回该股在该决策日的特征与闸门结果（只用 <= day 的数据）。"""
    sub = d[d["date"] <= day].reset_index(drop=True)
    if len(sub) < max(MIN_BARS, cfg.MIN_DAYS):
        return None
    if has_halt_gap(sub, cfg):
        return None
    if float(sub["amount"].tail(20).mean()) < cfg.MIN_AMOUNT:
        return None
    w = sub.tail(MIN_BARS)
    close = float(sub["close"].iloc[-1])
    cw = w["close"].to_numpy(dtype=float)
    pos = float(((cw < close).sum() + 0.5 * (cw == close).sum()) / len(cw))
    pe = sub["peTTM"].iloc[-1]
    try:
        pe = float(pe) if np.isfinite(float(pe)) else None
    except Exception:
        pe = None
    if pe is None:
        return None
    tech = compute_daily_signals(_recompute_pct_chg(sub), cfg)
    if tech is None:
        return None
    t = tech.iloc[-1]
    atr = float(t["atr"]) if np.isfinite(t.get("atr", np.nan)) else None
    if atr is None or close <= 0:
        return None
    ma20 = tech["ma20"].iloc[-1]
    ma20_prev = tech["ma20"].iloc[-(cfg.STABILIZATION_MA20_SLOPE_DAYS + 1)]
    ma20_ok = bool(np.isfinite(ma20) and np.isfinite(ma20_prev) and ma20_prev > 0
                   and (ma20 - ma20_prev) / ma20_prev >= 0 and close >= ma20)
    h = tech["macd_histogram"].iloc[-4:]
    macd_ok = bool(np.isfinite(h).all() and all(
        h.iloc[i] - h.iloc[i + 1] > 0 for i in range(3)))
    rs = compute_relative_strength(sub, idx, cfg) if idx is not None else np.nan
    return dict(
        close=close, pos=pos, pe=pe, tech=float(t["daily_score"]),
        atr_pct=atr / close * 100.0, rs=rs,
        ma20_ok=ma20_ok, macd_ok=macd_ok,
        vsc=_valuation_pe_score(pe, None, cfg),
        stab=bool(ma20_ok or macd_ok),
        low_ok=bool(pos <= cfg.LOW_POSITION_MAX),
        pe_ok=bool(0 < pe <= cfg.MAX_PE_TTM),
        tech_ok=bool(float(t["daily_score"]) >= cfg.MIN_TECHNICAL_SCORE_FORMAL),
        atr_ok=bool(atr / close * 100.0 <= cfg.MAX_ATR_PCT),
    )


def fwd(d, day, n):
    """未来 n 个交易日收盘相对决策日收盘的收益（%），用决策日之后的真实行情。"""
    after = d[d["date"] > day].reset_index(drop=True)
    if len(after) < n:
        return None
    c0 = d[d["date"] <= day]["close"].iloc[-1]
    return float(after["close"].iloc[n - 1] / c0 - 1) * 100


def main():
    cfg = StrategyConfig()
    panels = load_panels()
    idx = None
    f = sorted(glob.glob("backtest_cache/index_*.csv"))
    if f:
        idx = pd.read_csv(f[-1])
        idx["date"] = pd.to_datetime(idx["date"]).dt.strftime("%Y-%m-%d")
        idx = idx.sort_values("date").reset_index(drop=True)

    days = ["2026-05-08", "2026-05-29", "2026-06-19", "2026-07-10",
            "2026-07-31", "2026-08-21"]
    FWD = [5, 10, 20, 60]
    recs = []
    for day in days:
        for code, d in panels.items():
            ft = features_at(d, day, cfg, idx)
            if ft is None:
                continue
            ok = True
            rr = {}
            for n in FWD:
                v = fwd(d, day, n)
                rr[f"F{n}"] = v
                if v is None:
                    ok = False
            if not ok:
                continue
            ft["day"] = day
            ft.update(rr)
            recs.append(ft)
        print(f"{day}: 累积样本 {len(recs)}")

    R = pd.DataFrame(recs)
    print(f"\n=== 样本：{len(R)} 个「股票×决策日」观测，{R['day'].nunique()} 个决策日 ===\n")

    print("【1】各因子 vs 前瞻收益 秩相关(Spearman ρ)")
    facs = ["pos", "pe", "tech", "atr_pct", "rs", "vsc"]
    print(f"{'因子':<10}" + "".join(f"{'F'+str(n):>10}" for n in FWD))
    for f_ in facs:
        line = f"{f_:<10}"
        for n in FWD:
            rho, m = spearman(R[f_], R[f"F{n}"])
            line += f"{rho:>10.3f}" if np.isfinite(rho) else f"{'NA':>10}"
        print(line)

    print("\n【2】单闸门「通过 vs 未通过」的未来收益中位数差(pp)与胜率")
    gates = [("low_ok", "250日低位(≤0.4)"), ("pe_ok", "PE(0,25]"),
             ("tech_ok", "技术分≥45"), ("stab", "止跌确认"),
             ("atr_ok", "ATR%≤10")]
    for g, name in gates:
        line = f"{name:<16}"
        for n in FWD:
            a = R.loc[R[g], f"F{n}"].median()
            b = R.loc[~R[g], f"F{n}"].median()
            line += f"{(a-b):>+10.2f}" if np.isfinite(a) and np.isfinite(b) else f"{'NA':>10}"
        print(line + "   <- 差值(通过-未通过)")

    print("\n【3】逐层漏斗（全部条件逐层叠加后的剩余样本与收益）")
    steps = [("通过ATR风控", "atr_ok"), ("+PE(0,25]", "pe_ok"),
             ("+250日低位≤0.4", "low_ok"), ("+技术分≥45", "tech_ok"),
             ("+止跌确认", "stab")]
    mask = pd.Series(True, index=R.index)
    for name, g in steps:
        mask &= R[g]
        sub = R[mask]
        if len(sub) < 5:
            print(f"  {name:<16} n={len(sub):<5} 样本不足")
            continue
        med = "".join(f"{sub[f'F{n}'].median():>+9.2f}" for n in FWD)
        win = "".join(f"{(sub[f'F{n}']>0).mean()*100:>8.1f}%" for n in FWD)
        print(f"  {name:<16} n={len(sub):<5} 中位数{med}   胜率{win}")

    print("\n【4】综合分与未来收益（评分是否为有效排序）")
    sub = R[R.stab & R.low_ok & R.pe_ok & R.atr_ok].copy()
    if len(sub) >= 12:
        sub["score"] = (cfg.QUALITY_SCORE_WEIGHT * 60
                        + cfg.VALUATION_SCORE_WEIGHT * sub["vsc"]
                        + cfg.TECHNICAL_SCORE_WEIGHT * sub["tech"])
        for n in FWD:
            rho, m = spearman(sub["score"], sub[f"F{n}"])
            print(f"  综合分(近似) vs F{n}: ρ={rho:+.3f} (n={m})")
        q = pd.qcut(sub["score"].rank(method="first"), 3, labels=["低分","中分","高分"])
        for lab in ["低分", "中分", "高分"]:
            g = sub[q == lab]
            if len(g) < 3:
                continue
            print(f"  {lab}组 n={len(g):<4}" + "".join(
                f"F{n}:{g[f'F{n}'].median():+.2f}pp " for n in FWD))

    print("\n【5】全样本基准（无任何闸门）")
    print("  n=%d" % len(R) + "".join(
        f"F{n}:中位{R[f'F{n}'].median():+.2f}pp/胜率{(R[f'F{n}']>0).mean()*100:.1f}% "
        for n in FWD))
    print(f"\n注：F* 为相对决策日收盘的未来 N 个交易日收益；胜率=收益>0 的比例。")


if __name__ == "__main__":
    main()