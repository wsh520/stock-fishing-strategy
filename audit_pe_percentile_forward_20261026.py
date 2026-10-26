"""PE 分位分档的前瞻收益检验（2026-10-26）

目的：验证「行业/自身 PE 分位越低 → 估值分越高」这条线性单调假设是否站得住。
库内只有 38 条推荐事件，不足以判断；改用本地全市场 600 日缓存做横截面检验。

口径对齐：
  · 决策日 = 每 20 个交易日取一个（避免重叠窗口把统计量灌水）
  · pe_pct  = 该股 trailing 250 个 bar 内 peTTM 的分位（rolling rank，与
              historical_valuation 的「自身历史分位」同义，差异仅在是否含当日）
  · pos250  = (close - min250) / (max250 - min250)，用于近似策略「250日低位」池
  · 前瞻收益 = 决策日后 20 / 40 个交易日的收盘涨跌幅

输出：各 PE 分位档的均值、中位、胜率、以及「低位池内」的同一组数字。
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np
import pandas as pd

LOOKBACK = 250
HORIZONS = (20, 40)
STEP = 20
MIN_BARS = 300

BUCKETS = [(0.0, 0.05, "≤5%"), (0.05, 0.15, "6~15%"), (0.15, 0.30, "16~30%"),
           (0.30, 0.60, "31~60%"), (0.60, 1.01, ">60%")]


def load_one(path: str):
    try:
        d = pd.read_csv(path, usecols=["date", "close", "high", "low", "peTTM"],
                        dtype={"close": "float64", "high": "float64",
                               "low": "float64", "peTTM": "float64"})
    except Exception:
        return None
    if len(d) < MIN_BARS:
        return None
    d = d.sort_values("date").reset_index(drop=True)
    pe = d["peTTM"].where(d["peTTM"] > 0)
    d["pe_pct"] = pe.rolling(LOOKBACK, min_periods=LOOKBACK).rank(pct=True)
    min250 = d["low"].rolling(LOOKBACK, min_periods=LOOKBACK).min()
    max250 = d["high"].rolling(LOOKBACK, min_periods=LOOKBACK).max()
    span = (max250 - min250).replace(0, np.nan)
    d["pos250"] = (d["close"] - min250) / span
    for h in HORIZONS:
        d[f"fwd{h}"] = d["close"].shift(-h) / d["close"] - 1.0
    return d


def main() -> int:
    files = sorted(glob.glob("cache/daily_*_last600_qfq.csv"))
    if not files:
        print("找不到 cache/daily_*_last600_qfq.csv，无法检验")
        return 1
    print(f"载入 {len(files)} 只股票缓存（{LOOKBACK} bar 回看 / 未来 {HORIZONS} bar）...")

    frames = []
    for i, p in enumerate(files):
        d = load_one(p)
        if d is None:
            continue
        # 只保留有完整回看与前视的行，并按 STEP 抽样决策日（从尾部往前，保证同一日历）
        d = d.iloc[LOOKBACK - 1: len(d) - max(HORIZONS)]
        if d.empty:
            continue
        d = d.iloc[::STEP]
        frames.append(d[["date", "pe_pct", "pos250", "fwd20", "fwd40"]])
        if (i + 1) % 500 == 0:
            print(f"  已处理 {i + 1}/{len(files)}")
    if not frames:
        print("无可用样本")
        return 1

    allrows = pd.concat(frames, ignore_index=True)
    del frames
    allrows = allrows.dropna(subset=["pe_pct"])
    print(f"样本：{len(allrows):,} 个 (股票, 决策日) 观测，"
          f"决策日 {allrows['date'].nunique()} 个，"
          f"区间 {allrows['date'].min()} ~ {allrows['date'].max()}\n")

    def report(df: pd.DataFrame, label: str, horizon: int):
        col = f"fwd{horizon}"
        print(f"【{label}】前瞻 {horizon} 个交易日")
        print(f"  {'PE分位档':<8}{'观测数':>8}{'均值':>9}{'中位':>9}{'胜率':>8}{'跑赢基准*':>10}")
        for lo, hi, lab in BUCKETS:
            sub = df[(df["pe_pct"] >= lo) & (df["pe_pct"] < hi)]
            s = sub[col].dropna()
            if s.empty:
                continue
            print(f"  {lab:<8}{len(s):>8,}{s.mean()*100:>+9.2f}{s.median()*100:>+9.2f}"
                  f"{(s > 0).mean()*100:>7.1f}%")
        s = df[col].dropna()
        print(f"  {'全样本':<8}{len(s):>8,}{s.mean()*100:>+9.2f}{s.median()*100:>+9.2f}"
              f"{(s > 0).mean()*100:>7.1f}%")
        print()

    for h in HORIZONS:
        report(allrows, "全市场", h)
    # 近似策略股票池：250 日位置 ≤ 40%（与 QV_MAX_POSITION_250 同义）
    low = allrows[allrows["pos250"] <= 0.40]
    print(f"低位池（pos250 ≤ 40%）：{len(low):,} 个观测")
    for h in HORIZONS:
        report(low, "低位池", h)

    # 分位档单调性摘要：用各档均值做 Spearman 相关，负号=越便宜越好
    print("单调性摘要（各档均值的 Spearman 相关，负值 = 越便宜前瞻收益越高）：")
    for name, df in (("全市场", allrows), ("低位池", low)):
        for h in HORIZONS:
            col = f"fwd{h}"
            xs, ys = [], []
            for lo, hi, _lab in BUCKETS:
                s = df[(df["pe_pct"] >= lo) & (df["pe_pct"] < hi)][col].dropna()
                if len(s) < 50:
                    continue
                xs.append((lo + min(hi, 1.0)) / 2)
                ys.append(s.mean())
            if len(xs) >= 3:
                # Spearman = 秩的 Pearson（不依赖 scipy：本机未安装 scipy）
                xr = pd.Series(xs).rank()
                yr = pd.Series(ys).rank()
                r = float(np.corrcoef(xr, yr)[0, 1])
                print(f"  {name} / {h}日: ρ={r:+.3f}  "
                      f"档均值 {['%+.2f%%' % (y*100) for y in ys]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
