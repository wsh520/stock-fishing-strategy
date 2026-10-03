"""诊断4（只读）：P4 论证 —— PE 的区分度在闸门内还剩多少？值不值得进排序？

背景：修正后的前瞻实测（评估日 2026-06-04，n=168）显示
  ρ(PE, F5) = −0.46 / ρ(PE, F20) = −0.04 / ρ(PE, F60) = −0.23
是全部因子里唯一有稳定区分度的（负号 = 越便宜越好）。

但有个必须先回答的问题：PE 已经通过两条路径影响荐股 ——
  1) 闸门：绝对 PE ≤ MAX_PE_TTM(25) 或行业分位 ≤ 60%
  2) 综合分：VALUATION_SCORE_WEIGHT(0.30) × _valuation_pe_score
再叠一层排序权重会不会是重复计算？本脚本回答：
  A. 通过估值闸门后，PE 的分布还剩多少跨度？（跨度为 0 ⇒ 闸门内已无信息）
  B. PE 在闸门内的连续性与未来收益是否仍单调？（分箱对照）
  C. 与已有 valuation_score 的相关性有多高？（高 ⇒ 重复计算，收益低）
  D. 与技术分/分位相比，PE 在闸门内的 ρ 是多少？

方法与 diag_predictive.py 同口径：EVAL_OFFSET 前移，指标只用 eval_i 及之前，
前瞻收益取真实后续行情。设 EVAL_OFFSET=0 可自检（前瞻应全 NaN）。
"""
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.bottom_fishing_strategy import (  # noqa: E402
    StrategyConfig, compute_daily_signals, has_halt_gap, _valuation_pe_score,
)

DAILY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_cache", "daily")
HORIZONS = (5, 10, 20, 60)
EVAL_OFFSET = 75


def load(code):
    p = os.path.join(DAILY, f"{code}.csv")
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p)
    if "date" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ("open", "high", "low", "close", "volume", "amount", "peTTM", "pbMRQ"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def spearman(a, b):
    x, y = pd.Series(a).rank(), pd.Series(b).rank()
    if x.std() == 0 or y.std() == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def pe_bin(v):
    """按绝对 PE 分箱（闸门上限 25 为参照系）。"""
    if v <= 6:
        return "PE≤6 极低"
    if v <= 10:
        return "6~10 低"
    if v <= 15:
        return "10~15 中低"
    if v <= 20:
        return "15~20 中"
    return "20~25 偏高"


def main():
    cfg = StrategyConfig()
    files = sorted(f[:-4] for f in os.listdir(DAILY) if f.endswith(".csv"))
    rows = []
    for code in files:
        full = load(code)
        if full is None or full.empty:
            continue
        eval_i = len(full) - 1 - EVAL_OFFSET
        if eval_i < max(cfg.LOW_POSITION_LOOKBACK, cfg.MIN_DAYS):
            continue
        df = full.iloc[:eval_i + 1].reset_index(drop=True)
        future = full["close"].to_numpy(float)
        w = df.tail(cfg.LOW_POSITION_LOOKBACK)
        if not np.isfinite(w[["open", "high", "low", "close", "volume", "amount"]].to_numpy(float)).all():
            continue
        if float(df["amount"].tail(20).mean()) < cfg.MIN_AMOUNT:
            continue
        if has_halt_gap(df, cfg):
            continue
        close = float(df.iloc[-1]["close"])
        pos = float((w["close"].values < close).sum()) / len(w)
        if pos > cfg.LOW_POSITION_MAX:
            continue
        last = df.iloc[-1]
        pe = last.get("peTTM")
        pe = float(pe) if pd.notna(pe) else None
        if pe is None or pe <= 0 or pe > cfg.MAX_PE_TTM:
            continue
        tech = compute_daily_signals(df, cfg)
        if tech is None:
            continue
        d = tech.iloc[-1]
        # atr 是 compute_daily_signals 产出的列，不在原始行情行里。
        atr = d.get("atr")
        atr = float(atr) if pd.notna(atr) else None
        if atr is None or close <= 0 or atr / close * 100.0 > cfg.MAX_ATR_PCT + 1e-12:
            continue
        rec = {"code": code, "pe": pe, "pos": pos, "tech": float(d["daily_score"]),
               "vscore": _valuation_pe_score(pe, None, cfg),
               "eval_date": str(last["date"])}
        for h in HORIZONS:
            j = eval_i + h
            rec[f"f{h}"] = ((future[j] / close - 1) * 100.0) if j < len(future) else None
        rows.append(rec)

    d = pd.DataFrame(rows)
    if d.empty:
        print("无样本通过前置筛选。")
        return
    cap = float(cfg.MAX_PE_TTM)
    print(f"行情层通过 {len(d)} 只｜评估日 {d['eval_date'].min()} ~ {d['eval_date'].max()}")
    print(f"估值闸门：绝对 PE ≤ {cap:g}\n")

    print("=" * 74)
    print("A. 通过闸门后 PE 还剩多少跨度？（跨度为 0 ⇒ 闸门内已无信息可用）")
    print("=" * 74)
    print(f"   PE 最小 {d['pe'].min():.2f} / 最大 {d['pe'].max():.2f} / "
          f"中位 {d['pe'].median():.2f} / 均值 {d['pe'].mean():.2f}")
    print(f"   跨度（max−min）= {d['pe'].max() - d['pe'].min():.2f}"
          f"，占闸门上限的 {(d['pe'].max() - d['pe'].min()) / cap * 100:.0f}%")
    print(f"   四分位：Q1={d['pe'].quantile(.25):.2f}  Q2={d['pe'].median():.2f}  "
          f"Q3={d['pe'].quantile(.75):.2f}")
    dist = d["pe"].map(pe_bin).value_counts().to_dict()
    print("   分布：" + "  ".join(f"{k}={v}" for k, v in sorted(dist.items())))

    print("\n" + "=" * 74)
    print("B. 闸门内 PE 分箱 vs 未来收益（若随 PE 升高单调下降 ⇒ 闸门内仍有信息）")
    print("=" * 74)
    d["peb"] = d["pe"].map(pe_bin)
    hdr = f"{'PE 区间':<14}{'只数':>5}" + "".join(f"{'F' + str(h):>9s}" for h in HORIZONS)
    print(hdr)
    for b in ("PE≤6 极低", "6~10 低", "10~15 中低", "15~20 中", "20~25 偏高"):
        s = d[d["peb"] == b]
        if s.empty:
            continue
        line = f"{b:<14}{len(s):>5}"
        for h in HORIZONS:
            col = s[f"f{h}"].dropna()
            line += f"{(f'{col.median():+.2f}' if len(col) else '-'):>9s}"
        print(line)
    print("\n   期望：若 PE 越小收益越高，各行 F 值应自上而下递增")

    print("\n" + "=" * 74)
    print("C. 与已有 valuation_score 的相关（高 ⇒ 叠排序权重是重复计算）")
    print("=" * 74)
    print(f"   ρ(PE, valuation_score) = {spearman(d['pe'], d['vscore']):+.4f}"
          "   ← PE 绝对值与估值分近似线性，负相关")
    print("   ⇒ valuation_score 已把 PE 的连续信息编码过一次（权重 0.30）。")

    print("\n" + "=" * 74)
    print("D. 闸门内各因子与未来收益的 ρ（对比谁更值得再加权重）")
    print("=" * 74)
    for col, label in (("pe", "PE(绝对值)"), ("vscore", "valuation_score"),
                       ("tech", "tech_score"), ("pos", "position250")):
        line = f"   {label:<18}"
        for h in HORIZONS:
            s = d[[col, f"f{h}"]].dropna()
            line += f"  ρ(F{h})={spearman(s[col], s[f'f{h}']):+.3f}" if len(s) > 5 else f"  ρ(F{h})= n/a"
        print(line)

    print("\n" + "=" * 74)
    print("E. P4 收益判定")
    print("=" * 74)
    # 分箱单调性检验：极低 PE 组 vs 偏高 PE 组的 F 值差
    lo = d[d["peb"].isin(("PE≤6 极低", "6~10 低"))].dropna(subset=["f20"])
    hi = d[d["peb"].isin(("15~20 中", "20~25 偏高"))].dropna(subset=["f20"])
    rho_pe_f5 = spearman(d.dropna(subset=["f5"])["pe"], d.dropna(subset=["f5"])["f5"])
    print(f"   低 PE 组(≤10) n={len(lo)}  F20中位数 {(f'{lo['f20'].median():+.2f}' if len(lo) else '-')}%")
    print(f"   高 PE 组(>15) n={len(hi)}  F20中位数 {(f'{hi['f20'].median():+.2f}' if len(hi) else '-')}%")
    if len(lo) and len(hi):
        diff = lo["f20"].median() - hi["f20"].median()
        print(f"   F20 差异 = {diff:+.2f} pp")
    print(f"   ρ(PE, F5) = {rho_pe_f5:+.3f}")
    print()
    print("   判据：")
    print("     · PE 跨度足够 + 分箱单调 + ρ(F5) 显著 ⇒ 值得进排序")
    print("     · 与 vscore 高度相关 ⇒ 需先确认 valuation_score 是否已饱和")
    print("     · 若 F20/F60 无区分度 ⇒ 只在短窗口有效，说明是波动而非 alpha")


if __name__ == "__main__":
    main()
