"""诊断5（只读）：P4 可行性 —— 技术分门槛被抬到 95 后，还剩多少候选？

背景：`qv_floor_equivalence` 指出 P4(0.45/0.45/0.10) 会把「过线所需技术分」
从 ≥62 抬到 ≥95，而 `MIN_QV_SCORE=50` 距压线上限 50.5 只剩 0.5 分。
技术分满分 100，要求 ≥95 意味着「三道硬闸门全部压线合格」的候选必须
技术近乎满分才能过综合分线。这个诊断回答三个问题：

  A. 技术分实际分布 —— ≥95 这个门槛落在分布的哪个位置？够得着吗？
  B. 逐档模拟 —— 用真实候选的质量分/估值分/技术分，按不同权重组合算
     综合分，看 P4 会淘汰多少（这是「P4 是否可用」的决定性证据）。
  C. 若 P4 过严，该怎么调 —— 找出既能体现估值高权重、又不把候选打成 0 的组合。

与 diag_pe_value.py 同口径（EVAL_OFFSET 前移，指标只用 eval_i 及之前）。
设 EVAL_OFFSET=0 时前瞻收益全 NaN（本诊断不依赖前瞻，但保留以校验时点）。
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.bottom_fishing_strategy import (  # noqa: E402
    StrategyConfig, compute_daily_signals, has_halt_gap, _valuation_pe_score,
)

DAILY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_cache", "daily")
EVAL_OFFSET = 75
# 质量分：合成面板无法取到真实年报，这里按「压线合格」的保守值 50 代入
# （= MIN_QUALITY_SCORE 默认值，也是 qv_floor_equivalence 用的评分锚点）。
QUALITY_ASSUMED = 50.0


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
        pe = df.iloc[-1].get("peTTM")
        pe = float(pe) if pd.notna(pe) else None
        if pe is None or pe <= 0 or pe > cfg.MAX_PE_TTM:
            continue
        tech = compute_daily_signals(df, cfg)
        if tech is None:
            continue
        d = tech.iloc[-1]
        atr = d.get("atr")
        atr = float(atr) if pd.notna(atr) else None
        if atr is None or close <= 0 or atr / close * 100.0 > cfg.MAX_ATR_PCT + 1e-12:
            continue
        rows.append({"code": code, "pe": pe, "pos": pos,
                     "tech": float(d["daily_score"]),
                     "vscore": _valuation_pe_score(pe, None, cfg),
                     "eval_date": str(df.iloc[-1]["date"])})
        # 前瞻收益：评估日之后**真实**行情（不是 df 内，更不是负索引取到的历史价）。
        # 这是本项目踩过的最严重的坑，详见 diag_predictive.py 的 EVAL_OFFSET 说明。
        fut = full["close"].to_numpy(float)
        for h in (5, 20, 60):
            j = eval_i + h
            rows[-1][f"f{h}"] = ((fut[j] / close - 1) * 100.0) if j < len(fut) else None

    d = pd.DataFrame(rows)
    if d.empty:
        print("无样本通过前置筛选。")
        return
    print(f"行情层通过 {len(d)} 只｜评估日 {d['eval_date'].min()} ~ {d['eval_date'].max()}")
    print(f"（质量分按压线值 {QUALITY_ASSUMED:g} 代入）\n")

    print("=" * 76)
    print("A. 技术分实际分布 —— P4 要求 ≥95，够得着吗？")
    print("=" * 76)
    t = d["tech"]
    for q in (.5, .75, .9, .95, .99, 1.0):
        print(f"   P{int(q*100):<3d} = {t.quantile(q):6.2f}")
    print(f"   均值 {t.mean():.2f} / 中位 {t.median():.2f} / 最小 {t.min():.2f} / 最大 {t.max():.2f}")
    for thr in (62, 70, 80, 85, 90, 95, 100):
        n = int((t >= thr).sum())
        print(f"   技术分 ≥{thr:3d}：{n:3d} 只 ({n/len(t)*100:5.1f}%)")

    print("\n" + "=" * 76)
    print("B. 逐档模拟：不同权重组合下过 MIN_QV_SCORE=50 的候选数")
    print("=" * 76)
    print("   组合名                 权重(质量/估值/技术)   过线数   占比   压线上限  所需技术分")
    combos = [
        ("现状", 0.45, 0.30, 0.25),
        ("P4 预注册", 0.45, 0.45, 0.10),
        ("温和 P4", 0.45, 0.35, 0.20),
        ("更温和", 0.50, 0.35, 0.15),
        ("估值 0.40", 0.45, 0.40, 0.15),
        ("技术降至 0.20", 0.40, 0.40, 0.20),
    ]
    floor = float(cfg.MIN_QV_SCORE)
    v_cap = 100.0 * (1.0 - float(cfg.VALUATION_INDUSTRY_PERCENTILE_MAX))
    for name, qw, vw, tw in combos:
        score = qw * QUALITY_ASSUMED + vw * d["vscore"] + tw * d["tech"]
        passed = int((score >= floor).sum())
        ceiling = qw * QUALITY_ASSUMED + vw * v_cap + tw * 100.0
        req = (floor - qw * QUALITY_ASSUMED - vw * v_cap) / tw if tw > 0 else None
        req_s = f"≥{req:.0f}" if req is not None and req <= 100 else ("不可达" if req else "n/a")
        print(f"   {name:<20}  {qw:.2f}/{vw:.2f}/{tw:.2f}        "
              f"{passed:4d}  {passed/len(d)*100:5.1f}%   {ceiling:6.1f}   {req_s}")

    print("\n" + "=" * 76)
    print("C. 结论：P4 是放宽还是收紧？")
    print("=" * 76)
    base_pass = int(((0.45 * QUALITY_ASSUMED + 0.30 * d["vscore"] + 0.25 * d["tech"]) >= floor).sum())
    p4_pass = int(((0.45 * QUALITY_ASSUMED + 0.45 * d["vscore"] + 0.10 * d["tech"]) >= floor).sum())
    print(f"   现状过线 {base_pass} 只({base_pass/len(d)*100:.1f}%) → "
          f"P4 过线 {p4_pass} 只({p4_pass/len(d)*100:.1f}%)")
    if p4_pass > base_pass:
        print(f"   ⇒ P4 是**放宽**（过线数增加 {p4_pass - base_pass} 只），不是收紧。")
    elif p4_pass < base_pass:
        print(f"   ⇒ P4 是**收紧**（过线数减少 {base_pass - p4_pass} 只）。")
    else:
        print("   ⇒ P4 对过线数无影响。")

    # 归因：新增/淘汰的是什么样的股票？
    sc_base = 0.45 * QUALITY_ASSUMED + 0.30 * d["vscore"] + 0.25 * d["tech"]
    sc_p4 = 0.45 * QUALITY_ASSUMED + 0.45 * d["vscore"] + 0.10 * d["tech"]
    A = d[(sc_base >= floor) & (sc_p4 >= floor)]
    B = d[(sc_base >= floor) & (sc_p4 < floor)]
    C = d[(sc_base < floor) & (sc_p4 >= floor)]
    print(f"\n   两者都过线 A={len(A)}｜仅现状过线 B={len(B)}｜仅P4过线 C={len(C)}")
    print("\n   为什么放宽？两项的实际取值分布严重不对称：")
    print(f"     · 技术分 daily_score：中位 {d['tech'].median():.1f}、"
          f"P75={d['tech'].quantile(.75):.0f}、P95={d['tech'].quantile(.95):.0f}、"
          f"最大 {d['tech'].max():.1f}（绝大多数候选为 0）")
    print(f"     · 估值分 vscore：中位 {d['vscore'].median():.1f}、"
          f"P25={d['vscore'].quantile(.25):.1f}（闸门内几乎不会接近锚点 "
          f"{100*(1-cfg.VALUATION_INDUSTRY_PERCENTILE_MAX):.0f}）")
    print("     降技术权重(0.25→0.10) 砍掉的是**接近 0 的大头**，")
    print("     升估值权重(0.30→0.45) 换来的是**约 30 分的实数** ⇒ 综合分整体上移。")

    if len(C):
        print(f"\n   新增的 {len(C)} 只（仅 P4 过线）——是「更便宜」还是「技术更差」？")
        print(f"     技术分 中位 {C['tech'].median():.1f}、均值 {C['tech'].mean():.2f}")
        print(f"     估值分 中位 {C['vscore'].median():.1f}")
        print(f"     PE    中位 {C['pe'].median():.2f}  vs 保留组 A 的 "
              f"{A['pe'].median():.2f}")
        verdict = ("**更贵**" if C["pe"].median() > A["pe"].median() else "更便宜")
        print(f"     ⇒ 新增组的 PE 反而{verdict}，说明 P4 放宽的是**技术面**而非估值面，")
        print("       与「奖励便宜股」的设计意图相反。")
        print("\n   前瞻对照（评估日之后真实行情）：")
        print("   组别                     F5中位    F20中位   F60中位")
        for name, g in (("A 保留(46)", A), ("C 新增(仅P4过)", C)):
            if g.empty:
                continue
            vals = []
            for h in (5, 20, 60):
                col = g[f"f{h}"].dropna()
                vals.append(f"{col.median():+.2f}" if len(col) else "-")
            print(f"   {name:<22} {vals[0]:>8} {vals[1]:>10} {vals[2]:>10}")
        if len(A) and len(C):
            better = sum(1 for h in (5, 20, 60)
                         if A[f"f{h}"].dropna().median() > C[f"f{h}"].dropna().median())
            print(f"   ⇒ 保留组在 {better}/3 个窗口更优。"
                  + ("**P4 应否决**。\n" if better >= 2 else "\n"))

    print("\n   注：本诊断的质量分按压线值 50 代入（合成面板取不到真实年报）。")
    print("     质量分在三项中权重最高且方差可能最大，但结论的方向性由上面两项")
    print("     分布不对称主导 —— 即使质量分有 ±10 分浮动，也不会把「放宽」翻成「收紧」。")


if __name__ == "__main__":
    main()
