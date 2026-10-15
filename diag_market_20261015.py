"""2026-10-15 全市场抽样诊断（只读，新浪实时行情）。

目的：用真实 10-14 收盘行情验证
  Q1 底部年龄age60 在甜区(5~40)是否有样本；too_old/too_new 各占多少
  Q2 止跌 strict(站上MA20 且 MA20近5日斜率>=0) 的实际通过率
  Q3 底部 confirmed x 止跌通过 的交集规模（formal 必要条件）
  Q4 max_age 敏感性
只读，不改任何生产口径。
"""
import glob
import os
import random
import re
import sys
import time

import numpy as np
import pandas as pd
import requests

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0"})
N = int(sys.argv[1]) if len(sys.argv) > 1 else 350
AS_OF = "2026-10-14"


def kl(sym, n=300):
    u = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
    for _ in range(3):
        try:
            r = S.get(u, timeout=20)
            if r.status_code != 200:
                time.sleep(1)
                continue
            d = pd.DataFrame(r.json())
            if d.empty:
                return None
            d["day"] = pd.to_datetime(d["day"])
            d["close"] = pd.to_numeric(d["close"])
            return d.sort_values("day").reset_index(drop=True)
        except Exception:
            time.sleep(1.2)
    return None


def universe():
    out = set()
    for p in glob.glob("cache/daily_*_last600_qfq.csv"):
        m = re.match(r"daily_(sh|sz)\.(\d+)_", os.path.basename(p))
        if m:
            out.add(m.group(1) + m.group(2))
    return sorted(out)


syms = universe()
random.seed(20261015)
samp = random.sample(syms, min(N, len(syms)))
print(f"universe={len(syms)} sample={len(samp)} as_of~{AS_OF}", flush=True)

rows, fail = [], 0
for i, sy in enumerate(samp):
    d = kl(sy)
    if d is None or len(d) < 250:
        fail += 1
        continue
    if str(d["day"].iloc[-1].date()) != AS_OF:
        fail += 1
        continue
    cl = d["close"]
    last = float(cl.iloc[-1])
    ma20 = cl.rolling(20).mean()
    ma60 = cl.rolling(60).mean()
    slope = float((ma20.iloc[-1] - ma20.iloc[-6]) / ma20.iloc[-6] * 100)
    w = cl.tail(60).to_numpy()
    ai = int(np.argmin(w))
    rows.append(dict(
        sym=sy, close=last,
        above20=bool(last >= ma20.iloc[-1]),
        above60=bool(last >= ma60.iloc[-1]),
        slope=slope, age=59 - ai,
        gain=float((last / w[ai] - 1) * 100),
        pos250=float((last / cl.tail(250).min() - 1) * 100),
        chg20=float((last / cl.iloc[-21] - 1) * 100),
        chg5=float((last / cl.iloc[-6] - 1) * 100),
    ))
    time.sleep(0.1)
    if i % 100 == 0:
        print(f"  ..{i} ok={len(rows)}", flush=True)

df = pd.DataFrame(rows)
print(f"\n有效样本 {len(df)}  失败 {fail}")
if df.empty:
    raise SystemExit(1)

pd.set_option("display.width", 200)
lab = ["<5 too_new", "5-19", "20-39甜区", "40-44", ">44 too_old"]
cut = pd.cut(df["age"], [-1, 4, 19, 39, 44, 200], labels=lab)
print("\n=== Q1 age60 分布 ===")
print(cut.value_counts().reindex(lab).to_string())
print(f"甜区(5~40) 合计: {int(((df['age'] >= 5) & (df['age'] <= 40)).sum())} "
      f"({float(((df['age'] >= 5) & (df['age'] <= 40)).mean()) * 100:.1f}%)")

print("\n=== Q2 止跌 strict ===")
print(f"站上MA20      : {int(df['above20'].sum())} ({df['above20'].mean() * 100:.1f}%)")
print(f"站上MA20&斜率>=0: {int((df['above20'] & (df['slope'] >= 0)).sum())} "
      f"({float((df['above20'] & (df['slope'] >= 0)).mean()) * 100:.1f}%)")
print(f"站上MA20但斜率<0: {int((df['above20'] & (df['slope'] < 0)).sum())}  <- 趋势未修复")
print(f"站上MA60      : {int(df['above60'].sum())} ({df['above60'].mean() * 100:.1f}%)")

df["bot"] = np.where((df["age"] >= 5) & (df["age"] <= 40), "confirmed",
                     np.where(df["age"] < 5, "too_new", "too_old"))
df["stab"] = np.where(df["above20"] & (df["slope"] >= 0) & df["above60"], "strong",
                      np.where(df["above20"] & (df["slope"] >= 0), "medium", "weak"))
print("\n=== Q3 底部 x 止跌 交叉 ===")
print(pd.crosstab(df["bot"], df["stab"]).to_string())
okb = set(df[df["bot"] == "confirmed"]["sym"])
oks = set(df[df["stab"].isin(["strong", "medium"])]["sym"])
n = len(df)
print(f"\n底部confirmed {len(okb)} ({len(okb)/n*100:.1f}%)")
print(f"止跌通过      {len(oks)} ({len(oks)/n*100:.1f}%)")
print(f"交集(formal必要) {len(okb & oks)} ({len(okb & oks)/n*100:.1f}%)  → 全市场约 {int(len(okb & oks)/n*3005)} 只")
print(f"并集          {len(okb | oks)} ({len(okb | oks)/n*100:.1f}%)")

print("\n=== Q4 max_age 敏感性（样本内比例 × 3005 外推）===")
for ma in (40, 45, 50, 55, 59):
    c = int(((df["age"] >= 5) & (df["age"] <= ma)).sum())
    it = int(((df["age"] >= 5) & (df["age"] <= ma) & df["above20"] & (df["slope"] >= 0)).sum())
    print(f"  max_age={ma}: confirmed={c:4d} ({c/n*100:5.1f}% → ~{int(c/n*3005):4d}只)   "
          f"交集={it:3d} ({it/n*100:4.1f}% → ~{int(it/n*3005):3d}只)")

print(f"\n=== 位置闸门 pos250<=40%: {int((df['pos250'] <= 40).sum())} "
      f"({float((df['pos250'] <= 40).mean()) * 100:.1f}%) ===")
print("\n=== 市场状态 ===")
print(df[["chg5", "chg20"]].describe().round(2).to_string())
print(f"20日跌幅>15%: {int((df['chg20'] < -15).sum())}  <- 处于中期下跌的票")
df.to_csv("tmp_market_20261015.csv", index=False)
print("\nsaved tmp_market_20261015.csv")