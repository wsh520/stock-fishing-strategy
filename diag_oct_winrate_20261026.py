"""十月优质低估荐股胜率 + 三只实际持仓体检（2026-10-26）

只读：DB 走 diag_db_readonly（会话只读 + 白名单），行情走新浪日K（不复权）。
用途：给用户回答「十月推荐胜率」与「云天化/华东医药/中国汽研持仓状态」。
"""
from __future__ import annotations

import sys
import time
import json
import statistics as st

import pandas as pd
import requests

import diag_db_readonly as db

AS_OF = "2026-10-26"
S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0"})

HOLDINGS = {
    "600096": ("云天化", 28.243),
    "000963": ("华东医药", 26.883),
    "601965": ("中国汽研", 13.612),
}


def kl(sym: str, n: int = 120):
    u = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}")
    for _ in range(3):
        try:
            r = S.get(u, timeout=20)
            if r.status_code != 200:
                time.sleep(1.0)
                continue
            d = pd.DataFrame(r.json())
            if d.empty:
                return None
            d["day"] = pd.to_datetime(d["day"])
            for c in ("open", "high", "low", "close"):
                d[c] = pd.to_numeric(d[c])
            if "volume" in d.columns:
                d["volume"] = pd.to_numeric(d["volume"], errors="coerce")
            return d.sort_values("day").reset_index(drop=True)
        except Exception:
            time.sleep(1.2)
    return None


def sym_of(code: str) -> str:
    return ("sh" if code[0] in "56" else "sz") + code


def main() -> int:
    db.load_env()
    conn = db.connect()

    _, recs = db.run(conn, """
        SELECT id, rec_date, code, name, strategy, rec_tier, rec_close,
               score, daily_score, stop_loss, take_profit, market_env
        FROM stock_recommendation ORDER BY rec_date, code
    """)
    rc = ["id", "rec_date", "code", "name", "strategy", "rec_tier", "rec_close",
          "score", "daily_score", "stop_loss", "take_profit", "market_env"]
    dfr = pd.DataFrame(recs, columns=rc)
    dfr["rec_date"] = pd.to_datetime(dfr["rec_date"])

    _, trk = db.run(conn, """
        SELECT rec_id, rec_date, code, week_no, track_date, close_date,
               close_price, return_pct, holding_trade_days, legacy_duplicate
        FROM stock_tracking ORDER BY rec_id, week_no
    """)
    tc = ["rec_id", "rec_date", "code", "week_no", "track_date", "close_date",
          "close_price", "return_pct", "holding_trade_days", "legacy_duplicate"]
    dft = pd.DataFrame(trk, columns=tc)

    print("=" * 78)
    print("A. 十月（>=2026-10-01）推荐清单")
    print("=" * 78)
    oct_r = dfr[dfr["rec_date"] >= pd.Timestamp("2026-10-01")].copy()
    print(oct_r[["rec_date", "code", "name", "strategy", "rec_tier", "rec_close",
                 "score", "daily_score", "stop_loss", "take_profit"]]
          .to_string(index=False))

    # 每只票拿最近追踪 + 最新行情
    print()
    print("=" * 78)
    print("B. 十月推荐：周追踪收益 + 最新市价（新浪不复权）")
    print("=" * 78)
    rows = []
    for _, r in oct_r.iterrows():
        t = dft[dft["rec_id"] == r["id"]].sort_values("week_no")
        k = kl(sym_of(r["code"]))
        last_close = float(k["close"].iloc[-1]) if k is not None and len(k) else None
        last_day = k["day"].iloc[-1].strftime("%Y-%m-%d") if k is not None and len(k) else "-"
        mtm = (last_close / float(r["rec_close"]) - 1) * 100 if last_close else None
        w = {int(x["week_no"]): float(x["return_pct"]) for _, x in t.iterrows()}
        rows.append({
            "rec_date": r["rec_date"].strftime("%Y-%m-%d"),
            "code": r["code"], "name": r["name"], "strategy": r["strategy"],
            "rec_close": float(r["rec_close"]),
            "w1_5d": w.get(1), "w2_10d": w.get(2), "w3_15d": w.get(3),
            "last_day": last_day, "last": last_close, "mtm_pct": mtm,
        })
        time.sleep(0.15)
    dfo = pd.DataFrame(rows)
    print(dfo.to_string(index=False, float_format=lambda v: f"{v:.2f}"))

    # 市场基准
    bench = kl("sh000300")
    print()
    if bench is not None:
        for anchor in ["2026-10-08", "2026-09-17"]:
            b = bench[bench["day"] <= pd.Timestamp(anchor)]
            if len(b):
                base = float(b["close"].iloc[-1])
                now = float(bench["close"].iloc[-1])
                print(f"基准 沪深300 自 {anchor} 收盘 {base:.2f} → {now:.2f}"
                      f"（{bench['day'].iloc[-1]:%Y-%m-%d}）: {(now/base-1)*100:+.2f}%")


    print()
    print("=" * 78)
    print("F. 超额收益：个股 vs 沪深300（同源新浪收盘，剔除市场beta）")
    print("=" * 78)
    bf = dfr[(dfr["strategy"] == "bottom_fishing") & (dfr["rec_tier"] == "formal")]
    b3 = bench.copy() if bench is not None else None
    if b3 is None:
        print("无基准数据")
    else:
        b3 = b3.set_index("day")["close"]

        def bench_ret(d0, d1):
            a = b3[b3.index <= pd.Timestamp(d0)]
            c = b3[b3.index <= pd.Timestamp(d1)]
            if not len(a) or not len(c):
                return None
            return (float(c.iloc[-1]) / float(a.iloc[-1]) - 1) * 100

        recs_bf = bf.sort_values("rec_date")
        for label, sub in [("全样本(bottom_fishing formal)", recs_bf),
                           ("十月(>=2026-10-01)", recs_bf[recs_bf["rec_date"] >= pd.Timestamp("2026-10-01")])]:
            recs_list = []
            for _, r in sub.iterrows():
                k = kl(sym_of(r["code"]))
                if k is None or k.empty:
                    continue
                base = k[k["day"] <= pd.Timestamp(r["rec_date"])]
                if not len(base):
                    continue
                b0 = float(base["close"].iloc[-1])
                b1 = float(k["close"].iloc[-1])
                d1 = k["day"].iloc[-1]
                stock = (b1 / b0 - 1) * 100
                bru = bench_ret(r["rec_date"], d1)
                if bru is None:
                    continue
                recs_list.append({"name": r["name"], "rec_date": r["rec_date"].strftime("%m-%d"),
                                  "days": len(k[k["day"] > pd.Timestamp(r["rec_date"])]),
                                  "stock": stock, "bench": bru, "excess": stock - bru})
                time.sleep(0.12)
            if not recs_list:
                continue
            d = pd.DataFrame(recs_list)
            print(f"\n{label}：{len(d)} 个推荐事件（持有至 2026-10-23）")
            print(f"  个股 均值 {d['stock'].mean():+.2f}% 中位 {d['stock'].median():+.2f}% "
                  f"胜率 {(d['stock']>0).mean()*100:.1f}%")
            print(f"  基准 均值 {d['bench'].mean():+.2f}%")
            print(f"  超额 均值 {d['excess'].mean():+.2f}% 中位 {d['excess'].median():+.2f}% "
                  f"跑赢比例 {(d['excess']>0).mean()*100:.1f}%")
            print(d.sort_values("excess", ascending=False)
                  .to_string(index=False, float_format=lambda v: f"{v:+.2f}"))

    print()
    print("=" * 78)
    print("C. 优质低估(bottom_fishing) 全样本胜率，按期限分列")
    print("=" * 78)
    bf = dfr[(dfr["strategy"] == "bottom_fishing") & (dfr["rec_tier"] == "formal")]
    bf2 = bf[["id", "name"]].rename(columns={"name": "rname"})
    tb = dft.merge(bf2, left_on="rec_id", right_on="id")

    def stat(sub, label):
        n_obs = len(sub)
        if not n_obs:
            print(f"{label}: 无观测")
            return
        ret = sub["return_pct"].astype(float)
        win = (ret > 0).mean() * 100
        flat = (ret == 0).mean() * 100
        print(f"{label}: 观测 {n_obs} 条 | 胜率 {win:.1f}% (持平 {flat:.1f}%) | "
              f"均值 {ret.mean():+.2f}% | 中位 {ret.median():+.2f}% | "
              f"最好 {ret.max():+.2f}% | 最差 {ret.min():+.2f}%")

    for wk, d in [(1, 5), (2, 10), (3, 15)]:
        stat(tb[tb["week_no"] == wk], f"持有{d}个交易日(week{wk})")

    print()
    print("按推荐月份分组（week3=15日）:")
    tb3 = tb[tb["week_no"] == 3].copy()
    tb3["rec_date"] = pd.to_datetime(tb3["rec_date"])
    tb3["month"] = tb3["rec_date"].dt.strftime("%Y-%m")
    for m, g in tb3.groupby("month"):
        stat(g, f"  {m}")

    print()
    print("=" * 78)
    print("D. 十月推荐：出场规则模拟（收盘价触及 stop_loss / take_profit）")
    print("=" * 78)
    for _, r in oct_r.iterrows():
        k = kl(sym_of(r["code"]))
        if k is None or k.empty:
            print(f"{r['code']} {r['name']}: 无行情")
            continue
        k = k[k["day"] > pd.Timestamp(r["rec_date"])]
        if k.empty:
            print(f"{r['code']} {r['name']}: 推荐后无行情")
            continue
        sl, tp = float(r["stop_loss"]), float(r["take_profit"])
        hit = None
        for _, b in k.iterrows():
            if b["low"] <= sl:
                hit = ("止损", b["day"].strftime("%Y-%m-%d"), (sl / float(r["rec_close"]) - 1) * 100)
                break
            if b["high"] >= tp:
                hit = ("止盈", b["day"].strftime("%Y-%m-%d"), (tp / float(r["rec_close"]) - 1) * 100)
                break
        held = len(k)
        cur = (k["close"].iloc[-1] / float(r["rec_close"]) - 1) * 100
        print(f"{r['code']} {r['name']:<6} 已持有{held:>2}日 收盘{cur:+6.2f}%  "
              f"SL={sl:.2f} TP={tp:.2f}  → "
              + (f"触发{hit[0]} {hit[1]} ({hit[2]:+.2f}%)" if hit else "均未触发，持仓中"))
        time.sleep(0.15)

    print()
    print("=" * 78)
    print("E. 实际持仓体检")
    print("=" * 78)
    for code, (name, cost) in HOLDINGS.items():
        k = kl(sym_of(code))
        if k is None or k.empty:
            print(f"{code} {name}: 无行情")
            continue
        last = float(k["close"].iloc[-1])
        day = k["day"].iloc[-1]
        pnl = (last / cost - 1) * 100
        ma20 = k["close"].rolling(20).mean().iloc[-1]
        ma60 = k["close"].rolling(60).mean().iloc[-1]
        hi = k["close"].tail(20).max()
        dd = (last / hi - 1) * 100
        print(f"{code} {name}: 成本 {cost:.3f} | {day:%Y-%m-%d} 收盘 {last:.2f} | "
              f"浮动 {pnl:+.2f}%")
        print(f"    MA20={ma20:.2f}({'上方' if last>=ma20 else '下方'}) "
              f"MA60={ma60:.2f}({'上方' if last>=ma60 else '下方'}) "
              f"距20日高点 {dd:+.2f}%")
        rr = dfr[dfr["code"] == code].sort_values("rec_date")
        if len(rr):
            r0 = rr.iloc[-1]
            print(f"    最近信号 {r0['rec_date']:%Y-%m-%d} 收 {float(r0['rec_close']):.2f} "
                  f"SL={float(r0['stop_loss']):.2f} TP={float(r0['take_profit']):.2f}")
            print(f"    距SL {(last/float(r0['stop_loss'])-1)*100:+.2f}% | "
                  f"距TP {(float(r0['take_profit'])/last-1)*100:+.2f}%")
        print(f"    近5日: " + " ".join(
            f"{b['day']:%m-%d}:{b['close']:.2f}" for _, b in k.tail(5).iterrows()))
        time.sleep(0.15)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
