"""2026-10-14 0 推荐归因诊断（只读，纯缓存，向量化）。

日志事实链：3044 只 → 候选 57 → formal 0（pending 56）。
pending 标签覆盖：stabilization_weak=47, technical_weak=46,
recent_bottom_unconfirmed=34, operating_weak=31, financial_review=20, financial_risk=10。

核心待验证假设：
H1「近期底部结构未确认」(window=60, min_age=5, max_age=40) 在当前市场
   形成"太新/太老夹逼"——7 月中的底已 age>40，10 月初的新底 age<5，
   几乎没有票落在 5~40 bar 甜区。
H2 stabilization_weak 与 technical_weak 高度重合，因为两者同一根因（未站上 MA20）：
   qv_daily_score = trend(40) + momentum(30) + volume(30)，
   trend_state 要求「站上 MA20 且 MA20 斜率≥0」，未站上则 trend=0，
   再叠加 momentum=0 时技术分上限仅剩 volume(≤30) < 40。
"""
import glob
import os
from collections import Counter

import numpy as np
import pandas as pd

WIN, MIN_AGE, MAX_AGE = 60, 5, 40

files = sorted(glob.glob(os.path.join("cache", "daily_*_last600_qfq.csv")))
recs = []
for path in files:
    code = os.path.basename(path).split("_")[1]
    try:
        df = pd.read_csv(path, parse_dates=["date"])
    except Exception:
        continue
    if df is None or df.empty or len(df) < 300:
        continue
    df = df.sort_values("date").reset_index(drop=True)
    c = pd.to_numeric(df["close"], errors="coerce").to_numpy()
    if not np.isfinite(c[-WIN:]).all() or (c[-WIN:] <= 0).any():
        continue
    ma20 = pd.Series(c).rolling(20).mean().to_numpy()
    ma60 = pd.Series(c).rolling(60).mean().to_numpy()
    last = c[-1]

    window = c[-WIN:]
    ai = int(np.argmin(window))          # 窗口内最低收盘的位置
    age = (WIN - 1) - ai
    gain_pct = (last / window[ai] - 1) * 100

    ma20_ok = bool(np.isfinite(ma20[-1]) and np.isfinite(ma20[-6]) and ma20[-1] >= ma20[-6])
    above_ma60 = bool(np.isfinite(ma60[-1]) and last >= ma60[-1])
    stab = "strong" if (ma20_ok and above_ma60) else "medium" if ma20_ok else "below_ma20"

    reason = ("confirmed" if MIN_AGE <= age <= MAX_AGE
              else "too_new" if age < MIN_AGE else "too_old")
    recs.append(dict(code=code, anchor_date=str(df["date"].iloc[-WIN + ai].date()),
                     age=age, gain_pct=gain_pct, bottom=reason, stab=stab,
                     pos250=(last / c[-250:].min() - 1) * 100,
                     ma20_gap=(last / ma20[-1] - 1) * 100))

df = pd.DataFrame(recs)
AS_OF = str(pd.read_csv(files[0], parse_dates=["date"])["date"].iloc[-1].date())
N = len(df)
print(f"样本 {N} 只（缓存锚点 {AS_OF}；CI 实际跑的是 2026-10-13，形态一致）\n")

print("=== H1｜60-bar 窗口收盘最低点 age 分布 ===")
print(df["age"].describe().round(2).to_string())
print("\nage 分桶：")
bins, labels = [-1, 4, 9, 19, 29, 39, 45, 200], ["<5 too_new", "5-9", "10-19", "20-29",
                                                    "30-40 甜区上沿", "41-45", ">45 too_old"]
print(pd.cut(df["age"], bins=bins, labels=labels).value_counts().reindex(labels).to_string())
print("\n底部结构判定：")
bc = df["bottom"].value_counts()
print(bc.to_string())
print(f"→ confirmed 仅 {bc.get('confirmed', 0)} 只（{bc.get('confirmed', 0)/N*100:.2f}%）")

print("\n底部锚点日期 Top8：")
print(df["anchor_date"].value_counts().head(8).to_string())

print("\n=== H1 检验：too_old 组的涨离幅度（它们真的已涨离底部吗？）===")
old = df[df["bottom"] == "too_old"]
print(f"too_old {len(old)} 只 → gain_pct 中位 {old['gain_pct'].median():.2f}%，"
      f"P25 {old['gain_pct'].quantile(.25):.2f}%，P75 {old['gain_pct'].quantile(.75):.2f}%")
for t in (5, 10, 15, 20):
    print(f"  gain_pct<{t}% 的：{int((old['gain_pct'] < t).sum())} 只"
          f"（占 too_old 的 {(old['gain_pct'] < t).mean()*100:.1f}%）")

print("\n=== H2｜止跌 strict 分布 ===")
sc = df["stab"].value_counts()
print(sc.to_string())
print(f"→ 站上 MA20（strong+medium）仅 {sc.get('strong',0)+sc.get('medium',0)} 只"
      f"（{(sc.get('strong',0)+sc.get('medium',0))/N*100:.2f}%），"
      f"below_ma20 {sc.get('below_ma20',0)} 只（{sc.get('below_ma20',0)/N*100:.2f}%）")

print("\n=== H2 检验｜底部结构 x 止跌 交叉 ===")
ct = pd.crosstab(df["bottom"], df["stab"])
print(ct.to_string())
print("\n两个闸门的独立放行集合：")
b_ok = set(df[df["bottom"] == "confirmed"]["code"])
s_ok = set(df[df["stab"].isin(["strong", "medium"])]["code"])
print(f"  底部 confirmed: {len(b_ok)} 只")
print(f"  止跌 strong/medium: {len(s_ok)} 只")
print(f"  交集（formal 必要条件）: {len(b_ok & s_ok)} 只")
print(f"  并集（任一放行即可）: {len(b_ok | s_ok)} 只")

print("\n=== H2 检验｜技术分上界推导（qv_daily_score 结构）===")
print("  qv_daily_score = trend(0~40) + momentum(0~30) + volume(0~30)")
print("  trend_state = (close>=MA20) & (ma20_slope>=0) → 未站上 MA20 时 trend=0")
print("  未站上 MA20 的票技术分上界 = momentum(30) + volume(30) = 60")
print("  但 momentum 需要 MACD柱改善/KDJ多头/RSI回升；这三者同时为 0 时")
print("    技术分 = volume 分，上界 30 < MIN_TECHNICAL_SCORE_FORMAL(40)")
print("  ⇒ stabilization_weak 与 technical_weak 在 below_ma20 子集内高度共线")
below = df[df["stab"] == "below_ma20"]
print(f"  实测：below_ma20 {len(below)} 只（{len(below)/N*100:.2f}%），"
      f"其 ma20 缺口中位 {below['ma20_gap'].median():.2f}%")

print("\n=== 交叉验证｜两闸门同时放行的候选画像 ===")
both = df[df["code"].isin(b_ok & s_ok)].sort_values("gain_pct")
print(f"数量 {len(both)}")
if len(both):
    print(both.head(15).to_string(index=False))

print("\n=== 敏感性｜放宽 max_age（只放宽上界，不动 min_age/window）===")
for ma in (40, 45, 50, 55, 59):
    cnt = int(((df["age"] >= MIN_AGE) & (df["age"] <= ma)).sum())
    inter = int(((df["age"] >= MIN_AGE) & (df["age"] <= ma) &
                df["stab"].isin(["strong", "medium"])).sum())
    print(f"  max_age={ma:2d}: confirmed={cnt:5d} 只（{cnt/N*100:5.2f}%）  "
          f"与止跌交集={inter} 只")

print("\n=== 敏感性｜若今日把 recent_bottom 的 too_old 降级为展示标签（不减法）===")
relaxed = df[(df["stab"].isin(["strong", "medium"])) &
             ((df["bottom"] == "confirmed") |
              ((df["bottom"] == "too_old") & (df["gain_pct"] <= 15)))]
print(f"放行集合：{len(relaxed)} 只")
if len(relaxed):
    print(relaxed.sort_values("gain_pct").head(15).to_string(index=False))