"""诊断3（只读）：检验技术分/止跌闸门是否真的预测了后续表现。

方法：对低位+估值通过的样本，按当前技术分分组，统计未来 5/10/20/60 日收益。
若高分组与低分组无单调差异 ⇒ 技术分不具备排序价值（纯样本内观察，不做回测撮合）。

【重要修正 · 2026-10-03】本脚本原版的前瞻收益计算有索引 bug：
    rec[f"fwd{h}"] = (float(df.iloc[-1 + h]["close"]) / close - 1) * 100
`df.iloc[-1 + 5]` 实为 `df.iloc[4]` —— 取到的是**2024 年的历史价格**，不是未来。
配套的 `if len(df) > h` 守卫也永远成立（460 > 5），因此不会抛错，
但会让所有前瞻收益看起来「有值」，据此得出的「技术分负向」「保留组更差」
等结论全部无效。现改为：评估日从末根前移 EVAL_OFFSET 根，
使前瞻窗口落在真实存在的数据上。改任何前瞻统计脚本时都应先用 EVAL_OFFSET=0
自检一次 —— 正确实现下此时所有前瞻收益必须全为 NaN。
"""
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.bottom_fishing_strategy import (  # noqa: E402
    StrategyConfig, compute_daily_signals, has_halt_gap, assess_entry_timing,
)

DAILY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_cache", "daily")
# 评估日相对末根前移的交易日数：留出 60 日前瞻窗口 + 少量缓冲。
# 设为 0 则末根评估，此时无未来数据，所有前瞻收益必然为 NaN（可用于自检）。
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


def main():
    cfg = StrategyConfig()
    files = sorted(f[:-4] for f in os.listdir(DAILY) if f.endswith(".csv"))
    recs = []
    for code in files:
        full = load(code)
        if full is None or full.empty:
            continue
        # 评估日 = 末根前移 EVAL_OFFSET 根。其后必须留有 >=60 根，否则无前瞻窗口。
        eval_i = len(full) - 1 - EVAL_OFFSET
        if eval_i < max(cfg.LOW_POSITION_LOOKBACK, cfg.MIN_DAYS):
            continue
        df = full.iloc[:eval_i + 1].reset_index(drop=True)   # 指标只用评估日及之前
        future = full["close"].to_numpy(float)               # 前瞻收益用评估日之后
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
        pe = float(df.iloc[-1]["peTTM"]) if pd.notna(df.iloc[-1].get("peTTM")) else None
        if pe is None or pe <= 0 or pe > cfg.MAX_PE_TTM:
            continue
        tech = compute_daily_signals(df, cfg)
        if tech is None:
            continue
        d = tech.iloc[-1]
        ts = float(d["daily_score"])
        timing = assess_entry_timing(tech, cfg)

        sd = cfg.STABILIZATION_MA20_SLOPE_DAYS
        ma20_ok = False
        if timing.get("below_ma20") is False and "ma20" in tech.columns and len(tech) >= sd + 1:
            now, prev = tech["ma20"].iloc[-1], tech["ma20"].iloc[-(sd + 1)]
            if pd.notna(now) and pd.notna(prev) and float(prev) > 0:
                ma20_ok = (float(now) - float(prev)) / float(prev) >= 0
        above_ma60 = False
        if len(tech) >= 60:
            ma60 = tech["close"].rolling(60).mean().iloc[-1]
            above_ma60 = pd.notna(ma60) and close >= float(ma60)
        n = cfg.MACD_MOMENTUM_DAYS
        macd_imp = False
        if "macd_histogram" in tech.columns and len(tech) > n:
            h = pd.to_numeric(tech["macd_histogram"].iloc[-(n + 1):], errors="coerce")
            if np.isfinite(h.to_numpy()).all():
                hh = [float(x) for x in h.tolist()]
                macd_imp = all(hh[i] > hh[i - 1] for i in range(1, len(hh)))
        no_new_low = False
        ln, pn = cfg.STABILIZATION_NO_NEW_LOW_LOOKBACK, cfg.STABILIZATION_PRIOR_LOW_LOOKBACK
        if len(tech) >= ln + pn:
            lows = pd.to_numeric(tech["low"], errors="coerce")
            no_new_low = float(lows.iloc[-ln:].min()) >= float(lows.iloc[-(ln + pn):-ln].min())
        if ma20_ok and above_ma60:
            lvl = "strong"
        elif ma20_ok or (macd_imp and no_new_low):
            lvl = "medium"
        elif macd_imp:
            lvl = "weak"
        else:
            lvl = "none"

        last = df.iloc[-1]
        rec = dict(code=code, tech=ts, stab=lvl, pos=pos, pe=pe,
                   eval_date=str(last["date"]))
        for h in (5, 10, 20, 60):
            # 评估日之后第 h 根：索引 eval_i + h。越界即为「无未来数据」→ NaN。
            j = eval_i + h
            rec[f"fwd{h}"] = (float(future[j]) / close - 1) * 100 if j < len(future) else np.nan
        recs.append(rec)

    d = pd.DataFrame(recs)
    if d.empty:
        print("无样本通过低位+估值前置筛选，无法做前瞻统计。")
        return
    print(f"样本：{len(d)} 只（低位+估值通过；评估日 {d['eval_date'].min()} ~ "
          f"{d['eval_date'].max()}，末根 {EVAL_OFFSET} 根前）\n")

    print("=== 按【止跌分层】分组的未来收益中位数(%) ===")
    g = d.groupby("stab")[["fwd5", "fwd10", "fwd20", "fwd60"]].median()
    cnt = d["stab"].value_counts()
    print(f"{'层级':<8}{'样本':>6}{'F5':>9}{'F10':>9}{'F20':>9}{'F60':>9}")
    for k in ("strong", "medium", "weak", "none"):
        if k in g.index:
            row = g.loc[k]
            print(f"{k:<8}{cnt.get(k,0):>6}" + "".join(f"{row[c]:>9.2f}" for c in
                  ("fwd5", "fwd10", "fwd20", "fwd60")))
    print()
    print("  解读：若 strong/medium 的未来收益不显著优于 none，说明止跌闸门在做「剔除」而非「选优」。")
    print()

    print("=== 按【技术分】分组的未来收益中位数(%) ===")
    labels = ["技术分=0", "0~30", "30~45", "≥45"]

    def tbin(v):
        if v <= 0.1:
            return "技术分=0"
        if v <= 30.1:
            return "0~30"
        if v <= 45.1:
            return "30~45"
        return "≥45"

    d["tbin"] = d["tech"].map(tbin)
    g2 = d.groupby("tbin", observed=True)[["fwd5", "fwd10", "fwd20", "fwd60"]].median()
    c2 = d["tbin"].value_counts()
    print(f"{'技术分':<10}{'样本':>6}{'F5':>9}{'F10':>9}{'F20':>9}{'F60':>9}")
    for k in labels:
        if k in g2.index:
            row = g2.loc[k]
            print(f"{k:<10}{c2.get(k,0):>6}" + "".join(f"{row[c]:>9.2f}" for c in
                  ("fwd5", "fwd10", "fwd20", "fwd60")))
    print()

    print("=== 技术分/闸门变量与未来收益的相关性（Spearman秩相关，|ρ|越接近0越无排序价值）===")
    def spearman(a, b):
        a = pd.Series(a).rank()
        b = pd.Series(b).rank()
        if a.std() == 0 or b.std() == 0:
            return float("nan")
        return float(np.corrcoef(a, b)[0, 1])

    for h in (5, 10, 20, 60):
        sub = d[["tech", f"fwd{h}"]].dropna()
        if len(sub) > 10:
            rho = spearman(sub["tech"], sub[f"fwd{h}"])
            print(f"  技术分vs 未来{h:>2}日收益  : ρ = {rho:+.4f}   (n={len(sub)})")
    print()
    print("=== 对照组：250日分位 / PE 与未来收益的相关性 ===")
    for h in (5, 10, 20, 60):
        sub = d[["pos", f"fwd{h}"]].dropna()
        if len(sub) > 10:
            rho = spearman(sub["pos"], sub[f"fwd{h}"])
            print(f"  250日分位 vs 未来{h:>2}日收益: ρ = {rho:+.4f}   (n={len(sub)})")
    for h in (5, 10, 20, 60):
        sub = d[["pe", f"fwd{h}"]].dropna()
        if len(sub) > 10:
            rho = spearman(sub["pe"], sub[f"fwd{h}"])
            print(f"  PE          vs 未来{h:>2}日收益: ρ = {rho:+.4f}   (n={len(sub)})")
    print()
    print("=== 达标数对比：加/不加技术类闸门 ===")
    print(f"  低位+估值（无任何技术闸门）      : {len(d)} 只")
    print(f"  + 止跌闸门（非none）              : {int((d['stab'] != 'none').sum())} 只")
    print(f"  + 技术分≥45（MIN_TECHNICAL）     : {int((d['tech'] >= 45).sum())} 只")
    print()
    print("=== 被技术类闸门剔除的样本，未来表现是否更差？（关键检验）===")
    dropped = d[(d["stab"] == "none") | (d["tech"] < 45)]
    kept = d[(d["stab"] != "none") & (d["tech"] >= 45)]
    print(f"  被剔除 {len(dropped)} 只 / 保留 {len(kept)} 只")
    for h in (5, 10, 20, 60):
        dm = dropped[f"fwd{h}"].median()
        km = kept[f"fwd{h}"].median()
        print(f"  未来{h:>2}日中位数: 被剔除 {dm:+6.2f}%  vs  保留 {km:+6.2f}%   差 {km-dm:+6.2f}pp")


if __name__ == "__main__":
    main()
