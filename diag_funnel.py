"""诊断脚本（只读）：用本地缓存实测 quality_value 各闸门的淘汰分布。

目的：回答「最应该优化哪一点」。不改动任何生产代码，只输出统计。
"""
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.bottom_fishing_strategy import (  # noqa: E402
    StrategyConfig, compute_daily_signals, has_halt_gap, _rank_quality_score,
    bottom_structure_confirmed, assess_entry_timing,
)
from src.volume_breakout_strategy import VolumeBreakoutConfig  # noqa: E402

DAILY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_cache", "daily")


def load(code):
    p = os.path.join(DAILY, f"{code}.csv")
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p)
    if "date" not in df.columns or df["date"].isna().all():
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ("open", "high", "low", "close", "volume", "amount", "peTTM", "pbMRQ"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def main():
    cfg = StrategyConfig()
    files = sorted(f[:-4] for f in os.listdir(DAILY) if f.endswith(".csv"))
    counters = Counter()
    stab_level = Counter()
    tech_scores = []
    rank_scores = []
    rows_detail = []
    last_dates = Counter()

    for code in files:
        df = load(code)
        if df is None or df.empty:
            counters["SKIP_EMPTY"] += 1
            continue
        counters["TOTAL"] += 1
        last_dates[str(df.iloc[-1]["date"])] += 1

        n = cfg.LOW_POSITION_LOOKBACK
        if len(df) < max(n, cfg.MIN_DAYS):
            counters["FAIL_DATA_LEN"] += 1
            continue
        w = df.tail(n)
        if not np.isfinite(w[["open", "high", "low", "close", "volume", "amount"]].to_numpy(float)).all():
            counters["FAIL_DATA_NONFINITE"] += 1
            continue
        if float(df["amount"].tail(20).mean()) < cfg.MIN_AMOUNT:
            counters["FAIL_LIQUIDITY"] += 1
            continue
        if has_halt_gap(df, cfg):
            counters["FAIL_HALT_GAP"] += 1
            continue

        close = float(df.iloc[-1]["close"])
        position = float((w["close"].values < close).sum()) / len(w)
        if position > cfg.LOW_POSITION_MAX:
            counters["FAIL_POSITION"] += 1
            continue
        counters["pass_low_position"] += 1

        pe = float(df.iloc[-1]["peTTM"]) if pd.notna(df.iloc[-1].get("peTTM")) else None
        pb = float(df.iloc[-1]["pbMRQ"]) if pd.notna(df.iloc[-1].get("pbMRQ")) else None
        if pe is None:
            counters["PE_MISSING"] += 1
        elif pe <= 0:
            counters["FAIL_VALUATION_LOSS"] += 1
            continue
        elif pe > cfg.MAX_PE_TTM:
            counters["FAIL_VALUATION_ABS"] += 1
            continue
        counters["pass_valuation"] += 1

        tech = compute_daily_signals(df, cfg)
        if tech is None:
            counters["FAIL_DATA_TECH"] += 1
            continue
        d = tech.iloc[-1]
        ts = float(d["daily_score"])
        tech_scores.append(ts)

        timing = assess_entry_timing(tech, cfg)
        # 复刻 evaluate_quality_value 的止跌分层
        _ma20_ok = False
        if timing.get("below_ma20") is False and "ma20" in tech.columns:
            sd = cfg.STABILIZATION_MA20_SLOPE_DAYS
            if len(tech) >= sd + 1:
                now = tech["ma20"].iloc[-1]
                prev = tech["ma20"].iloc[-(sd + 1)]
                if pd.notna(now) and pd.notna(prev) and prev > 0:
                    _ma20_ok = ((float(now) - float(prev)) / float(prev)) >= 0
        _above_ma60 = False
        if len(tech) >= 60:
            ma60 = tech["close"].rolling(60).mean().iloc[-1]
            _above_ma60 = pd.notna(ma60) and close >= float(ma60)
        _n = cfg.MACD_MOMENTUM_DAYS
        _macd_improving = False
        if "macd_histogram" in tech.columns and len(tech) > _n:
            h = pd.to_numeric(tech["macd_histogram"].iloc[-(_n + 1):], errors="coerce")
            if np.isfinite(h.to_numpy()).all():
                hh = [float(x) for x in h.tolist()]
                _macd_improving = all(hh[i] > hh[i - 1] for i in range(1, len(hh)))
        _no_new_low = False
        _low_n = cfg.STABILIZATION_NO_NEW_LOW_LOOKBACK
        _prior_n = cfg.STABILIZATION_PRIOR_LOW_LOOKBACK
        if _macd_improving and len(tech) >= _low_n + _prior_n:
            lows = pd.to_numeric(tech["low"], errors="coerce")
            recent = lows.iloc[-_low_n:]
            prior = lows.iloc[-(_low_n + _prior_n):-_low_n]
            _no_new_low = float(recent.min()) >= float(prior.min())
        if _ma20_ok and _above_ma60:
            lvl = "strong"
        elif _ma20_ok or (_macd_improving and _no_new_low):
            lvl = "medium"
        elif _macd_improving:
            lvl = "weak"
        else:
            lvl = "none"
        stab_level[lvl] += 1
        if lvl == "none":
            counters["FAIL_STABILIZATION"] += 1
            continue
        counters["pass_stabilization"] += 1
        if lvl == "weak":
            counters["  └weak(pending)"] += 1

        atr = float(d["atr"]) if pd.notna(d.get("atr")) else None
        if atr is None:
            counters["FAIL_VOLATILE(atr缺失)"] += 1
            continue
        atr_pct = atr / close * 100
        if atr_pct - cfg.MAX_ATR_PCT > 1e-12:
            counters["FAIL_VOLATILE"] += 1
            continue
        counters["pass_atr"] += 1

        structure = bottom_structure_confirmed(tech, cfg)
        rq = _rank_quality_score(tech, cfg)
        rank_scores.append(ts + rq)
        rows_detail.append(dict(code=code, position=position, pe=pe, tech=ts,
                               stab=lvl, atr_pct=atr_pct, rq=rq, structure=structure))

    print("=" * 78)
    print("quality_value 行情层闸门实测漏斗（本地缓存 %d 只，末根日期分布见下）" % counters["TOTAL"])
    print("=" * 78)
    for k, v in counters.most_common():
        print(f"  {k:<32} {v:>5}")
    base = counters["TOTAL"]
    print()
    print("=== 关键转化率 ===")
    for a, b in (("pass_low_position", "TOTAL"), ("pass_valuation", "pass_low_position"),
                 ("pass_stabilization", "pass_valuation"), ("pass_atr", "pass_stabilization")):
        if counters[b]:
            print(f"  {a:<22} {counters[a]:>4} / {b:<20} = {counters[a]/counters[b]*100:5.1f}%")
    print()
    print("=== 止跌分层分布（通过低位+估值后） ===")
    tot = sum(stab_level.values()) or 1
    for k in ("strong", "medium", "weak", "none"):
        print(f"  {k:<8} {stab_level.get(k,0):>4}  {stab_level.get(k,0)/tot*100:5.1f}%")
    print()
    if tech_scores:
        arr = np.array(tech_scores)
        print(f"=== 技术分分布（n={len(arr)}）===")
        for q in (10, 25, 50, 75, 90):
            print(f"  P{q:<3} = {np.percentile(arr, q):6.1f}")
        print(f"  <45 (MIN_TECHNICAL_SCORE_FORMAL) = {(arr < 45).sum():>4}  {(arr<45).mean()*100:5.1f}%")
        print()
    if rank_scores:
        a2 = np.array(rank_scores)
        print(f"=== rank_score 分布（技术分+排序质量分, n={len(a2)}）===")
        for q in (10, 25, 50, 75, 90):
            print(f"  P{q:<3} = {np.percentile(a2, q):6.2f}")
        print(f"  并列(取整后)最多的值: {Counter(np.round(a2,1)).most_common(5)}")
    print()
    print("=== 末根交易日分布 ===")
    for d, c in last_dates.most_common(6):
        print(f"  {d}  {c}")
    if rows_detail:
        d = pd.DataFrame(rows_detail)
        print()
        print("=== 通过全部行情闸门的样本 ===")
        print(f"  共 {len(d)} 只；其中横盘结构确认 {int(d['structure'].sum())} 只"
              f"（{d['structure'].mean()*100:.0f}%）")
        print(d[["code", "position", "pe", "tech", "stab", "atr_pct", "rq"]]
              .sort_values("tech", ascending=False).head(20).to_string(index=False))


if __name__ == "__main__":
    main()
