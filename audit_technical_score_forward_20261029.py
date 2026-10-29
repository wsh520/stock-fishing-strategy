"""技术分门槛的前瞻收益检验（2026-10-29）

【要回答的问题】用户要求「技术分和基础分双高，技术分不低于 70 才推」。
但技术分 70 是不是一个能提升胜率的门槛？还是只是把推荐数量砍掉？
本脚本用本地全市场 600 日缓存做横截面检验，直接看「技术分越高 → 未来收益越好」
这条单调关系是否成立，以及每个候选门槛的代价（样本量损失）。

【口径对齐生产】
  · 技术分 = `recommendation_factors.quality_value_technical` 的 `qv_daily_score`
    （趋势40 + 动能30 + 量价30，clip 0~100），指标由 `compute_daily_signals` 生成，
    与生产同一份代码，不是近似复现。
  · 低位池 = pos250 = (close - min250(low)) / (max250(high) - min250(low)) <= 0.40，
    对应生产「250日低位 ≤40%」闸门。
  · 决策日 = 每 20 个交易日取一个，避免重叠窗口把统计量灌水（同 audit_pe_percentile_forward）。
  · 前瞻收益 = 决策日收盘 → 之后第 20 / 40 个交易日收盘，前复权口径同一序列。

【边界】
  · 单期截面检验，不是策略回测：不含经营/财务/估值闸门，只回答「技术分本身有没有信息」。
  · 结果只作阈值论证素材，不直接改生产门槛。

跑法：python audit_technical_score_forward_20261029.py
输出：控制台表格 + data/diag_technical_score_forward_20261029.csv
"""
from __future__ import annotations

import glob
import os
import random
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.recommendation_factors import quality_value_technical  # noqa: E402
from src.screening_engine import StrategyConfig, compute_daily_signals  # noqa: E402

LOOKBACK = 250
HORIZONS = (20, 40)
STEP = 20
MIN_BARS = 300
SAMPLE_SIZE = 1200      # 抽样只数（固定种子，结果可复现）
SEED = 20261029

# 分档：40 是当前正式门槛，70/80 是用户考虑的候选门槛
BUCKETS = [(0, 40, "<40 (当前会降 pending)"), (40, 50, "40~50"), (50, 60, "50~60"),
           (60, 70, "60~70"), (70, 80, "70~80"), (80, 101, "≥80")]


def load_one(path: str, config) -> pd.DataFrame | None:
    """返回带技术分/位置/前瞻收益的逐 bar 表；不满足长度要求返回 None。"""
    try:
        d = pd.read_csv(path)
    except Exception:
        return None
    need = {"date", "close", "high", "low", "volume", "amount", "pct_chg"}
    if not need.issubset(d.columns) or len(d) < MIN_BARS:
        return None
    d = d.sort_values("date").reset_index(drop=True)
    for c in ("close", "high", "low", "volume", "amount", "pct_chg"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["close", "high", "low"]).reset_index(drop=True)
    if len(d) < MIN_BARS:
        return None
    out = compute_daily_signals(d, config)
    if out is None or out.empty:
        return None
    tech = quality_value_technical(out)
    out["tech_score"] = pd.to_numeric(tech["qv_daily_score"], errors="coerce")
    lo = out["low"].rolling(LOOKBACK, min_periods=LOOKBACK).min()
    hi = out["high"].rolling(LOOKBACK, min_periods=LOOKBACK).max()
    span = (hi - lo).replace(0, np.nan)
    out["pos250"] = (out["close"] - lo) / span
    d["tech_score"] = out["tech_score"].to_numpy()
    d["pos250"] = out["pos250"].to_numpy()
    for h in HORIZONS:
        d[f"fwd{h}"] = d["close"].shift(-h) / d["close"] - 1.0
    return d[["date", "close", "tech_score", "pos250"] + [f"fwd{h}" for h in HORIZONS]]


def _worker(path: str) -> pd.DataFrame | None:
    return load_one(path, StrategyConfig())


def main() -> int:
    # 逐只算技术分约 0.25s/只（1200 只 ≈5 分钟）。中间样本落盘缓存，
    # 改分析口径时直接复用，不必重复算（--refresh 强制重算）。
    rows_cache = os.path.join("data", "diag_technical_score_rows_20261029.csv.gz")
    refresh = "--refresh" in sys.argv
    if os.path.exists(rows_cache) and not refresh:
        allrows = pd.read_csv(rows_cache)
        print(f"复用已缓存的样本 {len(allrows):,} 条（{rows_cache}；--refresh 可重算）")
        base_cols = ["date", "tech_score", "pos250"] + [f"fwd{h}" for h in HORIZONS]
        allrows = allrows[[c for c in base_cols if c in allrows.columns]]
    else:
        allrows = _build_rows()
        if allrows is None or allrows.empty:
            return 1
        os.makedirs("data", exist_ok=True)
        allrows.to_csv(rows_cache, index=False, compression="gzip")
        print(f"样本已缓存到 {rows_cache}")

    print(f"样本：{len(allrows):,} 个 (股票, 决策日) 观测，决策日 {allrows['date'].nunique()} 个，"
          f"区间 {allrows['date'].min()} ~ {allrows['date'].max()}\n")
    # 基准：每个决策日全样本均值，用于算超额（同一天内比较，剔除市场 beta）
    for h in HORIZONS:
        col = f"fwd{h}"
        allrows[f"exc{h}"] = allrows[col] - allrows.groupby("date")[col].transform("mean")
    _report_sections(allrows)
    return 0


def _build_rows() -> pd.DataFrame | None:
    files = sorted(glob.glob("cache/daily_*_last600_qfq.csv"))
    if not files:
        print("找不到 cache/daily_*_last600_qfq.csv，无法检验")
        return None
    # 单只约 0.25 秒（pandas 指标链）。3046 只串行约 15 分钟；ProcessPool 在本机
    # Windows + spawn 下会卡死（子进程重导入 screening_engine 时阻塞），
    # 故改为**固定种子抽样**：1200 只 × ~6 个决策日 ≈ 7000 观测，
    # 对分档均值/秩相关的统计功效足够，且结果可复现。
    total = len(files)
    if total > SAMPLE_SIZE:
        files = random.Random(SEED).sample(files, SAMPLE_SIZE)
        print(f"抽样 {SAMPLE_SIZE}/{total} 只（seed={SEED}，可复现）")
    print(f"载入 {len(files)} 只股票缓存（回看 {LOOKBACK} bar / 前瞻 {HORIZONS} / 步长 {STEP}）...")

    frames: list[pd.DataFrame] = []
    t0 = time.time()
    skipped = 0
    for i, path in enumerate(files):
        d = _worker(path)
        if d is None:
            skipped += 1
            continue
        d = d.iloc[LOOKBACK - 1: len(d) - max(HORIZONS)]
        if d.empty:
            continue
        frames.append(d.iloc[::STEP])
        if (i + 1) % 100 == 0:
            print(f"  已处理 {i + 1}/{len(files)}（跳过 {skipped}，{time.time() - t0:.0f}s）",
                  flush=True)
    if not frames:
        print("无可用样本")
        return None
    allrows = pd.concat(frames, ignore_index=True)
    del frames
    allrows = allrows.dropna(subset=["tech_score"])
    print(f"构建完成：{len(allrows):,} 条（耗时 {time.time() - t0:.0f}s）")
    return allrows


def _report_sections(allrows: pd.DataFrame) -> None:
    def report(df: pd.DataFrame, label: str) -> None:
        print(f"【{label}】样本 {len(df):,}")
        header = f"  {'技术分档':<24}{'观测':>8}"
        for h in HORIZONS:
            header += f"{'F' + str(h) + '均值':>9}{'F' + str(h) + '胜率':>9}{'F' + str(h) + '超额':>9}"
        print(header)
        for lo, hi, lab in BUCKETS:
            sub = df[(df["tech_score"] >= lo) & (df["tech_score"] < hi)]
            if sub.empty:
                continue
            line = f"  {lab:<24}{len(sub):>8,}"
            for h in HORIZONS:
                col, exc = f"fwd{h}", f"exc{h}"
                s = sub[col].dropna()
                e = sub[exc].dropna()
                if s.empty:
                    line += f"{'-':>9}{'-':>9}{'-':>9}"
                else:
                    line += (f"{s.mean() * 100:>+8.2f}%"
                             f"{(s > 0).mean() * 100:>8.1f}%"
                             f"{e.mean() * 100:>+8.2f}%")
            print(line)
        print()

    print("=" * 108)
    print("一、全样本：技术分分档")
    print("=" * 108)
    report(allrows, "全部股票")

    low = allrows[allrows["pos250"] <= 0.40]
    print("=" * 108)
    print("二、低位池（pos250 ≤ 40%，对应生产「250日低位」闸门）")
    print("=" * 108)
    report(low, "低位池")

    # 门槛模拟：每个候选门槛下还剩多少观测、收益如何
    print("=" * 108)
    print("三、门槛模拟（低位池内，技术分 ≥ 阈值的子集）")
    print("=" * 108)
    print(f"  {'门槛':<10}{'观测':>9}{'占低位池':>10}"
          f"{'F20均值':>9}{'F20胜率':>9}{'F20超额':>9}{'F40均值':>9}{'F40超额':>9}")
    base_n = len(low)
    rows = []
    for thr in (0, 40, 50, 55, 60, 65, 70, 75, 80, 85):
        sub = low[low["tech_score"] >= thr]
        if sub.empty:
            continue
        r = {"threshold": thr, "n": len(sub), "share": len(sub) / base_n}
        line = f"  ≥{thr:<9}{len(sub):>9,}{len(sub) / base_n * 100:>9.1f}%"
        for h in HORIZONS:
            s = sub[f"fwd{h}"].dropna()
            e = sub[f"exc{h}"].dropna()
            r[f"f{h}_mean"] = s.mean() * 100
            r[f"f{h}_win"] = (s > 0).mean() * 100
            r[f"f{h}_exc"] = e.mean() * 100
            r[f"f{h}_n"] = len(s)
            if h == 20:
                line += f"{s.mean() * 100:>+8.2f}%{(s > 0).mean() * 100:>8.1f}%{e.mean() * 100:>+8.2f}%"
            else:
                line += f"{s.mean() * 100:>+8.2f}%{e.mean() * 100:>+8.2f}%"
        rows.append(r)
        print(line)

    # 单调性：技术分与超额收益的秩相关（numpy 实现，不依赖 scipy）
    def spearman(x: pd.Series, y: pd.Series) -> tuple[float, float]:
        xr = x.rank(method="average").to_numpy()
        yr = y.rank(method="average").to_numpy()
        n = len(xr)
        if n < 30:
            return float("nan"), float("nan")
        rx = xr - xr.mean()
        ry = yr - yr.mean()
        denom = np.sqrt((rx ** 2).sum() * (ry ** 2).sum())
        if denom == 0:
            return float("nan"), float("nan")
        rho = float((rx * ry).sum() / denom)
        # 大样本近似：t = rho * sqrt((n-2)/(1-rho^2))，用正态近似给 p
        import math
        if abs(rho) >= 1:
            return rho, 0.0
        t = rho * math.sqrt((n - 2) / (1 - rho ** 2))
        p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
        return rho, p

    print()
    for h in HORIZONS:
        sub = low[[f"tech_score", f"exc{h}"]].dropna()
        rho, p = spearman(sub["tech_score"], sub[f"exc{h}"])
        print(f"  低位池内 技术分 vs F{h} 超额：Spearman ρ={rho:+.3f}（p={p:.2e}, n={len(sub):,}）")

    # ===== 控制 pos250 后技术分是否还有独立效应 =====
    # 低位池内部技术分高的票，pos250 也更高（更接近 40% 上限）。若不加控制，
    # 「技术分高 → 后续差」可能只是「位置高 → 后续差」的替身。故在 pos250 分档内比较。
    print()
    print("=" * 108)
    print("四、控制位置（pos250 分档内）看技术分效应 —— 排除「技术分高只是位置高」的替身解释")
    print("=" * 108)
    pos_buckets = [(0.0, 0.15, "pos≤15%"), (0.15, 0.25, "15~25%"),
                   (0.25, 0.33, "25~33%"), (0.33, 0.401, "33~40%")]
    print(f"  {'位置档':<12}{'技术分档':<14}{'观测':>7}{'F20超额':>10}{'F40超额':>10}")
    for plo, phi, plab in pos_buckets:
        cell = low[(low["pos250"] >= plo) & (low["pos250"] < phi)]
        if cell.empty:
            continue
        for tlo, thi, tlab in [(0, 40, "<40"), (40, 50, "40~50"), (50, 101, "≥50")]:
            sub = cell[(cell["tech_score"] >= tlo) & (cell["tech_score"] < thi)]
            e20 = sub["exc20"].dropna()
            e40 = sub["exc40"].dropna()
            if len(sub) < 15:
                continue
            print(f"  {plab:<12}{tlab:<14}{len(sub):>7,}"
                  f"{e20.mean() * 100:>+9.2f}%{e40.mean() * 100:>+9.2f}%")
        print()

    # ===== 分时段稳健性：技术分效应是否只在某一段市场成立 =====
    print("=" * 108)
    print("五、分时段稳健性（低位池，技术分 ≥50 相对全池的超额差）")
    print("=" * 108)
    low_dt = low.copy()
    low_dt["ym"] = low_dt["date"].astype(str).str[:7]
    low_dt["half"] = np.where(low_dt["ym"] < "2026-01", "2025H2及以前", "2026H1+")
    print(f"  {'时段':<14}{'技术分≥50 观测':>15}{'F20超额':>10}{'技术分<40 观测':>15}{'F20超额':>10}{'差':>10}")
    for half, grp in low_dt.groupby("half"):
        hi_sub = grp[grp["tech_score"] >= 50]["exc20"].dropna()
        lo_sub = grp[grp["tech_score"] < 40]["exc20"].dropna()
        if len(hi_sub) < 15 or len(lo_sub) < 15:
            continue
        print(f"  {half:<14}{len(hi_sub):>15,}{hi_sub.mean() * 100:>+9.2f}%"
              f"{len(lo_sub):>15,}{lo_sub.mean() * 100:>+9.2f}%"
              f"{(hi_sub.mean() - lo_sub.mean()) * 100:>+9.2f}%")

    out_csv = os.path.join("data", "diag_technical_score_forward_20261029.csv")
    os.makedirs("data", exist_ok=True)
    pd.DataFrame(rows).to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"\n门槛模拟明细已写入 {out_csv}")


if __name__ == "__main__":
    raise SystemExit(main())
