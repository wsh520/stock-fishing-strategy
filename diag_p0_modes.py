"""诊断3（只读）：P0 三口径（strict / risk_only / disabled）止跌闸门对比 + 前瞻收益。

与 diag_stabilization.py 的区别：那个脚本只拆解 strict 口径内部的死因结构；
本脚本直接对三种 QV_STABILIZATION_MODE 各跑一遍同一份本地缓存，输出：
  · 各口径的放行数量与分层构成
  · 每层候选的 5/10/20/60 日前瞻收益中位数（用「候选当日收盘价」为基准）
  · P1（QV_POSITION_RANK_WEIGHT）在各层内的排序单调性检验

纯离线：只读 backtest_cache/daily，不联网、不写生产数据、不改任何策略口径。
口径与生产一致的部分：低位分位、PE 绝对阈值（未启用行业分位，因快照需联网）、
停牌缺口、流动性、MACD 深度弱势复用 macd_not_deeply_weak。
与生产不完全一致之处：本脚本只看行情层，财务/近期经营闸门未参与（那部分由 A/B 覆盖）。
"""
import os
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.bottom_fishing_strategy import (  # noqa: E402
    StrategyConfig, compute_daily_signals, has_halt_gap, macd_not_deeply_weak,
    _macd_momentum_ok,
)

DAILY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_cache", "daily")
HORIZONS = (5, 10, 20, 60)


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
    """本环境无 scipy，自实现 Spearman（先取秩再算 Pearson）。"""
    x, y = pd.Series(a).rank(), pd.Series(b).rank()
    if x.std() == 0 or y.std() == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def classify(tech, df, cfg, close):
    """返回 (strict层, risk_only层) —— 两个口径共用同一批中间量，避免重复计算。"""
    timing = {}
    ma20_ok = False
    if "ma20" in tech.columns and len(tech) >= int(cfg.STABILIZATION_MA20_SLOPE_DAYS) + 1:
        sd = max(1, int(cfg.STABILIZATION_MA20_SLOPE_DAYS))
        now = tech["ma20"].iloc[-1]
        prev = tech["ma20"].iloc[-1 - sd]
        if pd.notna(now) and pd.notna(prev) and prev > 0:
            ma20_ok = float(now) >= float(prev)
    above_ma60 = False
    if "close" in tech.columns and len(tech) >= 60:
        ma60 = tech["close"].rolling(60).mean().iloc[-1]
        above_ma60 = pd.notna(ma60) and float(tech["close"].iloc[-1]) >= float(ma60)
    n_mom = max(1, int(cfg.MACD_MOMENTUM_DAYS))
    macd_improving = False
    if "macd_histogram" in tech.columns and len(tech) > n_mom:
        h = pd.to_numeric(tech["macd_histogram"].iloc[-1 - n_mom:], errors="coerce")
        if np.isfinite(h.to_numpy()).all():
            macd_improving = _macd_momentum_ok(tech, cfg)
    low_n = max(2, int(cfg.STABILIZATION_NO_NEW_LOW_LOOKBACK))
    prior_n = max(low_n, int(cfg.STABILIZATION_PRIOR_LOW_LOOKBACK))
    no_new_low = False
    if "low" in tech.columns and len(tech) >= low_n + prior_n:
        lows = pd.to_numeric(tech["low"], errors="coerce")
        recent, prior = lows.iloc[-low_n:], lows.iloc[-1 - (low_n + prior_n):-low_n]
        no_new_low = bool(np.isfinite(recent.to_numpy()).all()
                          and np.isfinite(prior.to_numpy()).all()
                          and (recent > 0).all() and (prior > 0).all()
                          and float(recent.min()) >= float(prior.min()))
    # strict
    if ma20_ok and above_ma60:
        s_lv = "strong"
    elif ma20_ok or (macd_improving and no_new_low):
        s_lv = "medium"
    elif macd_improving:
        s_lv = "weak"
    else:
        s_lv = "none"
    # risk_only：危险形态直接否决，其余降级为 watch
    danger = not macd_not_deeply_weak(tech, cfg.MACD_WEAK_DAYS, cfg.MACD_WEAK_HIST_PCT)
    if danger:
        r_lv = "veto"
    elif ma20_ok and above_ma60:
        r_lv = "strong"
    elif ma20_ok or (above_ma60 and no_new_low) or (macd_improving and no_new_low):
        r_lv = "medium"
    elif macd_improving:
        r_lv = "weak"
    else:
        r_lv = "watch"
    return s_lv, r_lv, {
        "ma20_ok": ma20_ok, "above_ma60": above_ma60,
        "macd_improving": macd_improving, "no_new_low": no_new_low,
        "danger": danger,
    }


def fwd(closes, i, h):
    if i + h >= len(closes):
        return None
    base = closes[i]
    if base <= 0:
        return None
    return (closes[i + h] / base - 1.0) * 100.0


def main():
    cfg = StrategyConfig()
    files = sorted(f[:-4] for f in os.listdir(DAILY) if f.endswith(".csv"))
    rows = []
    n_seen = 0
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
        n_seen += 1
        close = float(df.iloc[-1]["close"])
        pos = float((w["close"].values < close).sum()) / len(w)
        if pos > cfg.LOW_POSITION_MAX:
            continue
        pe = float(df.iloc[-1]["peTTM"]) if pd.notna(df.iloc[-1].get("peTTM")) else None
        if pe is None or pe <= 0 or pe > cfg.MAX_PE_TTM:
            continue
        atr_v = None
        tech = compute_daily_signals(df, cfg)
        if tech is None:
            continue
        d = tech.iloc[-1]
        if pd.notna(d.get("atr")):
            atr_v = float(d["atr"])
        if atr_v is None or close <= 0:
            continue
        if atr_v / close * 100.0 > cfg.MAX_ATR_PCT + 1e-12:
            continue
        s_lv, r_lv, el = classify(tech, df, cfg, close)
        closes = df["close"].to_numpy(float)
        i = len(closes) - 1
        rec = {"code": code, "strict": s_lv, "risk_only": r_lv,
               "pos": pos, "tech": float(d["daily_score"])}
        rec.update({f"f{h}": fwd(closes, i, h) for h in HORIZONS})
        rec.update({f"el_{k}": v for k, v in el.items()})
        rows.append(rec)

    d = pd.DataFrame(rows)
    print(f"总样本 {n_seen} 只 → 低位+估值+ATR 后剩 {len(d)} 只\n")
    print("=" * 78)
    print("P0 三口径放行数量对比（止跌闸门是 quality_value 下唯一的技术准入）")
    print("=" * 78)
    for name in ("strict", "risk_only"):
        c = Counter(d[name].tolist())
        if name == "strict":
            keep = [k for k in ("strong", "medium", "weak") if c.get(k)]
        else:
            keep = [k for k in ("strong", "medium", "weak") if c.get(k)]
        n_keep = sum(c.get(k, 0) for k in keep)
        print(f"\n【{name}】")
        for k in ("strong", "medium", "weak", "watch", "none", "veto"):
            if c.get(k):
                print(f"   {k:7s} {c[k]:4d}")
        print(f"   → 可进正式池(strong+medium+weak) = {n_keep}  "
              f"(占 {n_keep / len(d) * 100:.1f}%)")
    print("\n" + "=" * 78)
    print("各分层的前瞻收益中位数 %（用候选当日收盘为基准）")
    print("=" * 78)
    hdr = f"{'层':10s}{'只数':>6s}" + "".join(f"{'F' + str(h):>9s}" for h in HORIZONS)
    for name in ("strict", "risk_only"):
        print(f"\n【{name}】")
        print(hdr)
        for lv in ("strong", "medium", "weak", "watch", "none", "veto"):
            sub = d[d[name] == lv]
            if sub.empty:
                continue
            line = f"{lv:10s}{len(sub):>6d}"
            for h in HORIZONS:
                s = sub[f"f{h}"].dropna()
                line += f"{(f'{s.median():+.2f}' if len(s) else '-'):>9s}"
            print(line)

    print("\n" + "=" * 78)
    print("P1 检验：250日分位 vs 未来收益（Spearman ρ，取值应显著为负=越低越好）")
    print("=" * 78)
    for name, keep_lv in (("strict", ("strong", "medium", "weak")),
                          ("risk_only", ("strong", "medium", "weak"))):
        sub = d[d[name].isin(keep_lv)]
        print(f"\n【{name} 通过者 n={len(sub)}】")
        for h in HORIZONS:
            s = sub[["pos", f"f{h}"]].dropna()
            if len(s) > 5:
                print(f"   ρ(position, F{h}) = {spearman(s['pos'], s[f'f{h}']):+.3f}"
                      f"   n={len(s)}")
        # 技术分对照
        for h in (20, 60):
            s = sub[["tech", f"f{h}"]].dropna()
            if len(s) > 5:
                print(f"   ρ(tech_score, F{h}) = {spearman(s['tech'], s[f'f{h}']):+.3f}  (对照)")

    print("\n" + "=" * 78)
    print("P1 可行性：在各层内，分位能否把前瞻收益高的样本排到前面")
    print("=" * 78)
    for name in ("strict", "risk_only"):
        sub = d[d[name].isin(("strong", "medium", "weak"))].copy()
        if sub.empty:
            continue
        cap = float(cfg.LOW_POSITION_MAX)
        sub["pos_score"] = 10.0 * (1 - (sub["pos"] / cap).clip(0, 1))
        print(f"\n【{name}】按 P1 权重排序后的前后对比（F20）")
        s20 = sub.dropna(subset=["f20"])
        if s20.empty:
            continue
        top = s20.nlargest(max(1, len(s20) // 5), "pos_score")
        bot = s20.nsmallest(max(1, len(s20) // 5), "pos_score")
        print(f"   分位权重最高的 20%：n={len(top):3d}  F20中位数 {top['f20'].median():+.2f}%  "
              f"实际分位均值 {top['pos'].mean():.3f}")
        print(f"   分位权重最低的 20%：n={len(bot):3d}  F20中位数 {bot['f20'].median():+.2f}%  "
              f"实际分位均值 {bot['pos'].mean():.3f}")

    print("\n" + "=" * 78)
    print("风险_only 的 watch 层：无止跌证据、无危险证据 —— 是否真的该全淘汰？")
    print("=" * 78)
    sub = d[d["risk_only"] == "watch"]
    if not sub.empty:
        print(hdr)
        for h in HORIZONS:
            s = sub[f"f{h}"].dropna()
            print(f"   watch 层 F{h} 中位数 = {(f'{s.median():+.2f}' if len(s) else '-')}  n={len(s)}")
        strict_pass = d[d["strict"].isin(("strong", "medium", "weak"))].dropna(subset=["f20"])
        watch_ok = sub.dropna(subset=["f20"])
        if len(strict_pass) and len(watch_ok):
            print(f"\n   strict 通过组 F20 = {strict_pass['f20'].median():+.2f}% (n={len(strict_pass)})")
            print(f"   watch 观察组 F20 = {watch_ok['f20'].median():+.2f}% (n={len(watch_ok)})")
            diff = watch_ok["f20"].median() - strict_pass["f20"].median()
            print(f"   差异 = {diff:+.2f} 个百分点 → "
                  f"{'watch 层不劣于通过组，不应全淘汰' if diff > 0 else 'watch 层确实更差，维持淘汰'}")


if __name__ == "__main__":
    main()
