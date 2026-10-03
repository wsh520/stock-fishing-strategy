"""诊断2（只读）：拆解止跌闸门内部四条路径的贡献 + 技术分离散度分析。"""
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
    path_counter = Counter()
    tech_vals = []
    # 通过低位+估值后的样本，逐条记录止跌四要素
    cand = []
    for code in files:
        df = load(code)
        if df is None or df.empty or len(df) < max(cfg.LOW_POSITION_LOOKBACK, cfg.MIN_DAYS):
            continue
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
        tech_vals.append(ts)
        timing = assess_entry_timing(tech, cfg)

        sd = cfg.STABILIZATION_MA20_SLOPE_DAYS
        ma20_ok = False
        ma20_slope = None
        if timing.get("below_ma20") is False and "ma20" in tech.columns and len(tech) >= sd + 1:
            now, prev = tech["ma20"].iloc[-1], tech["ma20"].iloc[-(sd + 1)]
            if pd.notna(now) and pd.notna(prev) and float(prev) > 0:
                ma20_slope = (float(now) - float(prev)) / float(prev)
                ma20_ok = ma20_slope >= 0
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
        no_new_low = None
        ln, pn = cfg.STABILIZATION_NO_NEW_LOW_LOOKBACK, cfg.STABILIZATION_PRIOR_LOW_LOOKBACK
        if len(tech) >= ln + pn:
            lows = pd.to_numeric(tech["low"], errors="coerce")
            no_new_low = float(lows.iloc[-ln:].min()) >= float(lows.iloc[-(ln + pn):-ln].min())

        cand.append(dict(code=code, ma20_ok=ma20_ok, ma20_slope=ma20_slope,
                          above_ma60=above_ma60, macd_imp=macd_imp,
                          no_new_low=no_new_low, tech=ts, pos=pos, pe=pe,
                          below_ma20=timing.get("below_ma20"), side=timing.get("side")))

    d = pd.DataFrame(cand)
    print(f"=== 通过低位+估值的样本：{len(d)} 只 ===\n")

    print("--- 止跌四要素各自的命中率 ---")
    print(f"  收盘站上MA20 且 MA20 5日斜率≥0 : {d['ma20_ok'].sum():>4} ({d['ma20_ok'].mean()*100:5.1f}%)")
    print(f"  收盘站上MA60                    : {d['above_ma60'].sum():>4} ({d['above_ma60'].mean()*100:5.1f}%)")
    print(f"  MACD柱连续3日改善               : {d['macd_imp'].sum():>4} ({d['macd_imp'].mean()*100:5.1f}%)")
    print(f"  近5日不创新低(macd改善者中)     : {int(d['macd_imp'].sum() and d.loc[d['macd_imp'],'no_new_low'].fillna(False).sum()):>4}")
    print()

    print("--- 止跌最终分层 ---")
    def level(r):
        if r.ma20_ok and r.above_ma60:
            return "strong"
        if r.ma20_ok or (r.macd_imp and r.no_new_low):
            return "medium"
        if r.macd_imp:
            return "weak"
        return "none"
    d["stab"] = d.apply(level, axis=1)
    vc = d["stab"].value_counts()
    for k in ("strong", "medium", "weak", "none"):
        print(f"  {k:<8} {vc.get(k,0):>4}  {vc.get(k,0)/len(d)*100:5.1f}%")
    print()
    print("--- none 组的死因拆解（145 只为何全灭）---")
    none = d[d["stab"] == "none"]
    print(f"  none 总数                : {len(none)}")
    print(f"  其中收盘在 MA20 下方      : {none['below_ma20'].eq(True).sum()}")
    print(f"  其中站上MA20但MA20仍下行  : {(none['below_ma20'].eq(False) & none['ma20_ok'].eq(False)).sum()}")
    print(f"  MACD未改善               : {none['macd_imp'].eq(False).sum()}")
    print(f"  side=falling(MACD深度弱)  : {none['side'].eq('falling').sum()}")
    print()
    print("--- 技术分取值集合（证明离散度）---")
    tv = Counter(np.round(tech_vals, 1))
    print(f"  不同取值个数: {len(tv)}  →  {sorted(tv.keys())}")
    print(f"  各取值出现次数: {dict(sorted(tv.items()))}")
    print()
    print("--- 技术分 < 45 的样本（被 MIN_TECHNICAL_SCORE_FORMAL 降为 pending）---")
    low = d[d["tech"] < 45]
    print(f"  {len(low)} / {len(d)} 只 = {len(low)/len(d)*100:.1f}%")
    print(f"  其中 stab 分布: {low['stab'].value_counts().to_dict()}")
    print(f"  其中横盘结构确认: {int(low['no_new_low'].fillna(False).sum())} 只(以no_new_low代理)")
    print()
    print("--- 若只看基本面/估值不看技术，最终能出几只？---")
    print(f"  低位+估值通过 : {len(d)} 只")
    print(f"  再过止跌闸门   : {len(d) - vc.get('none',0)} 只")
    print(f"  再要求技术≥45  : {(d['tech'] >= 45).sum()} 只")
    print(f"  再要求技术≥62(MIN_QV_SCORE等效): {(d['tech'] >= 62).sum()} 只")


if __name__ == "__main__":
    main()
