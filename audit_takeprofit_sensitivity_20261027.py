"""止盈档位敏感性：5% / 10% / 15% 在同一批随机起点上的期望对比（离线，读本地缓存）。

问题：生产当前 止盈 +15% / 止损 max(3×ATR,15%) / 建议持有 40~60 日。
本脚本回答「把止盈改成 5% 或 10% 会更好吗」。

方法（与 audit_live_trading.py 的 C 段同源，但改成**路径模拟**而非「是否触及」）：
  · 面板：backtest_cache/daily/*.csv（800 只，2024-11 ~ 2026-09-21，主板日内极值）
  · 起点：每只票随机抽最多 40 个起点，统一要求其后还有 60 根 bar，
    使 5/10/15/20/40/60 各档**共用同一批起点**（配对比较，不受抽样差异干扰）
  · 逐日推进：先看当日最低是否触及止损、最高是否触及止盈；
    同一根 bar 双触时按**止损优先**处理（保守，无法用日线判断日内先后）
  · 到期未触发 → 按第 n 日收盘价离场
输出：每档 n 下各止盈规则的 平均收益 / 胜率 / 止损触发率 / 止盈触发率 / 时间退出率，
      以及期内最大有利波动的中位数与分位（判断档位是否可达）。
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HORIZONS = (5, 10, 15, 20, 40, 60)
TP_LIST = (3.0, 5.0, 8.0, 10.0, 15.0, None)   # None = 不设止盈（纯时间退出）
SL = 15.0
MAX_START = 40
SEED = 7


def load_panels():
    out = {}
    for f in sorted(glob.glob("backtest_cache/daily/*.csv")):
        code = os.path.splitext(os.path.basename(f))[0]
        try:
            d = pd.read_csv(f)
        except Exception:
            continue
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        for c in ("open", "high", "low", "close", "volume", "amount"):
            if c in d.columns:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        if not {"high", "low", "close"} <= set(d.columns):
            continue
        d = d.sort_values("date").reset_index(drop=True)
        d = d[(d["close"] > 0) & (d["low"] > 0) & (d["high"] > 0)].reset_index(drop=True)
        out[code] = d
    return out


def main():
    panels = load_panels()
    maxn = max(HORIZONS)
    rng = np.random.default_rng(SEED)

    # 每条样本存：各 horizon 下每种止盈规则的收益；以及 MFE / MAE 路径
    recs = []          # (n, tp, ret, exit_kind)
    mfe_by_n = {n: [] for n in HORIZONS}

    for code, d in panels.items():
        c = d["close"].to_numpy(float)
        lo = d["low"].to_numpy(float)
        hi = d["high"].to_numpy(float)
        end = len(c) - 1
        if end - maxn - 61 < 60:
            continue
        cand = np.arange(60, end - maxn - 1)
        if len(cand) == 0:
            continue
        starts = rng.choice(cand, size=min(MAX_START, len(cand)), replace=False)

        for s in starts:
            c0 = c[s]
            fwd_lo = lo[s + 1:s + 1 + maxn] / c0 - 1.0
            fwd_hi = hi[s + 1:s + 1 + maxn] / c0 - 1.0
            fwd_c = c[s + 1:s + 1 + maxn] / c0 - 1.0

            sl_hit = fwd_lo <= -SL / 100.0
            idx_sl = int(np.argmax(sl_hit)) if sl_hit.any() else 10 ** 9

            for n in HORIZONS:
                mfe_by_n[n].append(float(fwd_hi[:n].max()) * 100)
                for tp in TP_LIST:
                    if tp is None:
                        idx_tp = 10 ** 9
                    else:
                        tp_hit = fwd_hi[:n] >= tp / 100.0
                        idx_tp = int(np.argmax(tp_hit)) if tp_hit.any() else 10 ** 9
                    if idx_sl < n and idx_sl <= idx_tp:
                        recs.append((n, tp, -SL, "SL"))
                    elif idx_tp < n:
                        recs.append((n, tp, tp, "TP"))
                    else:
                        recs.append((n, tp, float(fwd_c[n - 1]) * 100, "TIME"))

    df = pd.DataFrame(recs, columns=["n", "tp", "ret", "kind"])
    print(f"面板 {len(panels)} 只｜样本 {len(df) // (len(HORIZONS) * len(TP_LIST))} 个起点"
          f"（每点覆盖 {len(HORIZONS)} 个窗口 × {len(TP_LIST)} 种止盈）\n")

    print("=" * 88)
    print("【1】期内最大有利波动（MFE）分位 —— 判断各档止盈是否可达")
    print("=" * 88)
    print(f"{'窗口':>5} {'中位':>8} {'P60':>8} {'P75':>8} {'P90':>8} {'≥5%占比':>9} {'≥10%占比':>10} {'≥15%占比':>10}")
    for n in HORIZONS:
        a = np.array(mfe_by_n[n])
        print(f"{n:>5} {np.median(a):>+7.1f}% {np.percentile(a,60):>+7.1f}% "
              f"{np.percentile(a,75):>+7.1f}% {np.percentile(a,90):>+7.1f}% "
              f"{(a>=5).mean()*100:>8.1f}% {(a>=10).mean()*100:>9.1f}% {(a>=15).mean()*100:>9.1f}%")

    print("\n" + "=" * 88)
    print(f"【2】各止盈档位的实际期望（止损固定 -{SL:.0f}%，未触发则按期末收盘离场）")
    print("=" * 88)
    for n in HORIZONS:
        print(f"\n--- 持有 {n} 个交易日 ---")
        print(f"{'止盈':>6} {'平均收益':>9} {'中位':>8} {'胜率':>7} "
              f"{'止盈退出':>9} {'止损退出':>9} {'时间退出':>9}")
        for tp in TP_LIST:
            g = df[(df.n == n) & (df.tp.fillna(-1) == (tp if tp is not None else -1))]
            if g.empty:
                continue
            lab = "不设" if tp is None else f"+{tp:.0f}%"
            print(f"{lab:>6} {g.ret.mean():>+8.2f}% {g.ret.median():>+7.2f}% "
                  f"{(g.ret>0).mean()*100:>6.1f}% "
                  f"{(g.kind=='TP').mean()*100:>8.1f}% {(g.kind=='SL').mean()*100:>8.1f}% "
                  f"{(g.kind=='TIME').mean()*100:>8.1f}%")

    print("\n" + "=" * 88)
    print("【3】结论摘要：各窗口下最优止盈档（按平均收益）")
    print("=" * 88)
    for n in HORIZONS:
        best, bestv = None, -1e9
        for tp in TP_LIST:
            g = df[(df.n == n) & (df.tp.fillna(-1) == (tp if tp is not None else -1))]
            if not g.empty and g.ret.mean() > bestv:
                bestv, best = g.ret.mean(), tp
        lab = "不设止盈" if best is None else f"+{best:.0f}%"
        print(f"  持有 {n:>2} 日 → 最优 {lab:<8} 平均 {bestv:+.2f}%")


if __name__ == "__main__":
    main()
