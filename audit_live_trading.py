"""实盘口径专项审计（剔除回测维度）：手工交易语境下最严重问题的定位。

三个问题：
A) 信号可产出性：53 只到达止跌层的候选，叠加真实年度财务核验后还剩多少？
B) 定位自洽性：止跌闸门对「深度低位股」的杀伤；通过闸门的股票此前已涨多少（是否在追高）。
C) 执行参数可行性：15% 止损 / 30% 止盈，在建议持有 10~20 交易日内各自触发概率。
   若止损频繁触发而止盈几乎不可达 → 结构性正期望缺失。
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
    _is_financial_stock,
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


def annual_rows_for(code, day):
    """从项目真实缓存取该股年度财务（只取已缓存、决策日可用的年度行）。"""
    hits = sorted(glob.glob(f"cache/annual_quality_v1_{code}_*.json"))
    if not hits:
        return None
    best = None
    for h in hits:
        try:
            j = json.load(open(h, encoding="utf-8"))
        except Exception:
            continue
        ar = j.get("annual_rows") or []
        if isinstance(ar, dict):
            ar = [ar]
        if not ar:
            continue
        yrs = [r.get("year") for r in ar if isinstance(r, dict)]
        if not yrs:
            continue
        if max(yrs) <= 2025 and (best is None or len(ar) > len(best[1])):
            best = (h, ar)
    return best[1] if best else None


def main():
    cfg = StrategyConfig()
    panels = load_panels()
    print(f"面板 {len(panels)} 只｜决策日 {DAY}\n")

    # ---------- A + B：逐股走到止跌层，同时记录闸门杀伤与已涨幅 ----------
    reach, killed_by_stab = [], []
    stab_detail = {"deep_low": [0, 0], "mid_low": [0, 0]}   # [通过, 总数]
    gain_when_pass = []
    for code, d in panels.items():
        sub = d[d["date"] <= DAY].reset_index(drop=True)
        if len(sub) < max(MIN_BARS, cfg.MIN_DAYS) or has_halt_gap(sub, cfg):
            continue
        if sub["date"].iloc[-1] != DAY:
            continue
        if float(sub["amount"].tail(20).mean()) < cfg.MIN_AMOUNT:
            continue
        w = sub.tail(MIN_BARS)
        close = float(sub["close"].iloc[-1])
        cw = w["close"].to_numpy(dtype=float)
        pos = float(((cw < close).sum() + .5 * (cw == close).sum()) / len(cw))
        if pos > cfg.LOW_POSITION_MAX:
            continue
        try:
            pe = float(sub["peTTM"].iloc[-1])
        except Exception:
            pe = None
        if pe is None or not (0 < pe <= cfg.MAX_PE_TTM):
            continue
        tech = compute_daily_signals(_recompute_pct_chg(sub), cfg)
        if tech is None:
            continue
        t = tech.iloc[-1]
        atr = float(t["atr"]) if np.isfinite(t.get("atr", np.nan)) else None
        if atr is None or atr / close * 100 > cfg.MAX_ATR_PCT:
            continue
        if float(t["daily_score"]) < cfg.MIN_TECHNICAL_SCORE_FORMAL:
            continue
        # 到这里 = 到达止跌层
        ma20 = tech["ma20"].iloc[-1]
        ma20_prev = tech["ma20"].iloc[-(cfg.STABILIZATION_MA20_SLOPE_DAYS + 1)]
        ma20_ok = bool(np.isfinite(ma20) and np.isfinite(ma20_prev) and ma20_prev > 0
                       and (ma20 - ma20_prev) / ma20_prev >= 0 and close >= ma20)
        h = tech["macd_histogram"].iloc[-4:]
        macd_ok = bool(np.isfinite(h).all() and all(h.iloc[i] - h.iloc[i + 1] > 0 for i in range(3)))
        stab = bool(ma20_ok or macd_ok)
        bucket = "deep_low" if pos <= 0.20 else "mid_low"
        stab_detail[bucket][1] += 1
        if stab:
            stab_detail[bucket][0] += 1
            lo250 = float(cw.min())
            gain20 = (close / float(sub["close"].iloc[-21]) - 1) * 100 if len(sub) >= 21 else np.nan
            gain_low = (close / lo250 - 1) * 100
            gain_when_pass.append(dict(code=code, pos=pos, pe=pe, gain20=gain20,
                                       gain_from_low=gain_low, via="ma20" if ma20_ok else "macd"))
        else:
            killed_by_stab.append(dict(code=code, pos=pos, pe=pe))

    reach = gain_when_pass + killed_by_stab
    print("=" * 66)
    print("【A】信号可产出性：到达止跌层 %d 只，止跌闸门放行 %d 只（%.0f%%）"
          % (len(reach), len(gain_when_pass),
             len(gain_when_pass) / max(len(reach), 1) * 100))
    print("=" * 66)

    # 财务核验叠加
    fin_verified = fin_finreview = fin_missing = 0
    for r in gain_when_pass:
        code = r["code"]
        ar = annual_rows_for(code, DAY)
        if ar is None:
            fin_missing += 1
            continue
        res = evaluate_annual_quality(
            ar, DAY, years=cfg.QUALITY_YEARS,
            median_roe_min=cfg.QUALITY_MEDIAN_ROE_MIN, min_roe=cfg.QUALITY_MIN_ROE,
            cash_conversion_min=cfg.QUALITY_CASH_CONVERSION_MIN, financial=False,
            require_annual_net_profit_positive=cfg.QUALITY_REQUIRE_ANNUAL_NET_PROFIT_POSITIVE,
            require_annual_cashflow_positive=cfg.QUALITY_REQUIRE_ANNUAL_CASHFLOW_POSITIVE)
        st = res.get("status")
        if st == "verified":
            fin_verified += 1
        elif st == "failed":
            fin_finreview += 1
        else:
            fin_missing += 1
    print(f"叠加 3 年年度质量核验（ROE/扣非/现金流）：")
    print(f"   verified（可正式推荐候选） = {fin_verified} 只")
    print(f"   failed（质量不达标）      = {fin_finreview} 只")
    print(f"   partial/missing（待核验） = {fin_missing} 只")
    print(f"→ 800 只起、实盘口径下最终可推荐数量级 ≈ {fin_verified} 只")

    # ---------- B：定位自洽性 ----------
    print("\n" + "=" * 66)
    print("【B】止跌闸门对不同低位深度的杀伤 + 通过者已涨幅")
    print("=" * 66)
    for b, name in (("deep_low", "深度低位（250日分位 ≤0.20）"),
                    ("mid_low", "中度低位（0.20~0.40）")):
        ok, tot = stab_detail[b]
        print(f"  {name}: {tot} 只到达 → 止跌闸门放行 {ok} 只（{ok/max(tot,1)*100:.0f}%）")
    if gain_when_pass:
        g = pd.DataFrame(gain_when_pass)
        print(f"\n  放行 {len(g)} 只的「已涨幅」（买入成本证据）：")
        print(f"     距 250 日低点： 中位 {g['gain_from_low'].median():+.1f}%  "
              f"P75 {g['gain_from_low'].quantile(.75):+.1f}%  最大 {g['gain_from_low'].max():+.1f}%")
        print(f"     近 20 日涨幅： 中位 {g['gain20'].median():+.1f}%  "
              f"P75 {g['gain20'].quantile(.75):+.1f}%")
        print(f"     已从低点涨超 20% 的占比：{(g['gain_from_low'] > 20).mean()*100:.0f}%")
        print(f"     证据构成：{g['via'].value_counts().to_dict()}")

    # ---------- C：执行参数可行性 ----------
    print("\n" + "=" * 66)
    print("【C】执行参数：15% 止损 / 30% 止盈 在 10~20 交易日内的触发概率")
    print("=" * 66)
    n = 20
    hits_sl = hits_tp = both = tot = 0
    dist_sl, dist_tp = [], []
    rng = np.random.default_rng(7)
    for code, d in panels.items():
        sub = d[d["date"] <= DAY].reset_index(drop=True)
        if len(sub) < 260:
            continue
        c = sub["close"].to_numpy(dtype=float)
        lo = sub["low"].to_numpy(dtype=float)
        hi = sub["high"].to_numpy(dtype=float)
        end = len(c) - 1
        starts = rng.choice(np.arange(60, end - n - 1), size=min(40, max(1, (end - n - 60) // 5)), replace=False)
        for s in starts:
            c0 = c[s]
            if c0 <= 0:
                continue
            seg_lo = lo[s + 1:s + 1 + n] / c0
            seg_hi = hi[s + 1:s + 1 + n] / c0
            tot += 1
            sl = bool((seg_lo <= 1 - cfg.FIXED_STOP_LOSS_PCT / 100).any())
            tp = bool((seg_hi >= 1 + cfg.FIXED_TAKE_PROFIT_PCT / 100).any())
            hits_sl += sl
            hits_tp += tp
            both += (sl and tp)
            dist_sl.append(float(seg_lo.min() - 1) * 100)
            dist_tp.append(float(seg_hi.max() - 1) * 100)
    print(f"  样本 {tot} 次「随机起点持有 {n} 个交易日」（主板真实日内极值，含日内触及）")
    print(f"     触及 −15% 止损： {hits_sl/tot*100:5.1f}%   ← 建议持有期内")
    print(f"     触及 +30% 止盈： {hits_tp/tot*100:5.1f}%")
    print(f"     两者都触及：     {both/tot*100:5.1f}%   （同日双触按止损保守处理）")
    print(f"     期内最大有利波动中位：{np.median(dist_tp):+.1f}%")
    print(f"     期内最大不利波动中位：{np.median(dist_sl):+.1f}%")
    print(f"\n  → 期望：止损触发 {hits_sl/tot*100:.0f}% vs 止盈触发 {hits_tp/tot*100:.0f}%"
          f"（差 {(hits_sl-hits_tp)/tot*100:.0f} 个百分点）")


if __name__ == "__main__":
    main()