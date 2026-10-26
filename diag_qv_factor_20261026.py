"""优质低估口径的因子分组体检（2026-10-26）

问题：38 条正式推荐里，哪些因子真正区分了盈亏？哪些是噪音？
只读 DB + 新浪日K；行业用 cache/industry_bs.json（与生产同源，classify_cyclical_industry 同实现）。
"""
from __future__ import annotations

import json
import re
import sys
import time

import pandas as pd
import requests

import diag_db_readonly as db
from src.recommendation_factors import classify_cyclical_industry
from src.screening_engine import StrategyConfig

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0"})
CFG = StrategyConfig()


def kl(sym, n=90):
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
            return d.sort_values("day").reset_index(drop=True)
        except Exception:
            time.sleep(1.2)
    return None


def sym_of(code):
    return ("sh" if code[0] in "56" else "sz") + code


def pe_bucket(txt):
    m = re.search(r"PE\s*(\d+)%|PE分位\s*(\d+)%", txt or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def main():
    db.load_env()
    conn = db.connect()
    _, rows = db.run(conn, """
        SELECT rec_date, code, name, score, daily_score, signals_hit
        FROM stock_recommendation
        WHERE strategy='bottom_fishing' AND rec_tier='formal'
        ORDER BY rec_date
    """)
    df = pd.DataFrame(rows, columns=["rec_date", "code", "name", "score", "daily_score", "signals_hit"])
    df["rec_date"] = pd.to_datetime(df["rec_date"])
    conn.close()

    ind = json.load(open("cache/industry_bs.json", encoding="utf-8"))
    bench = kl("sh000300").set_index("day")["close"]

    def bret(d0, d1):
        a = bench[bench.index <= pd.Timestamp(d0)]
        b = bench[bench.index <= pd.Timestamp(d1)]
        if not len(a) or not len(b):
            return None
        return (float(b.iloc[-1]) / float(a.iloc[-1]) - 1) * 100

    recs = []
    for _, r in df.iterrows():
        k = kl(sym_of(r["code"]))
        if k is None or k.empty:
            continue
        base = k[k["day"] <= r["rec_date"]]
        if not len(base):
            continue
        b0 = float(base["close"].iloc[-1])
        b1 = float(k["close"].iloc[-1])
        d1 = k["day"].iloc[-1]
        bru = bret(r["rec_date"], d1)
        industry = ind.get(r["code"], "")
        recs.append({
            "rec_date": r["rec_date"], "code": r["code"], "name": r["name"],
            "score": float(r["score"]), "tech": float(r["daily_score"]),
            "ret": (b1 / b0 - 1) * 100, "bench": bru if bru is not None else float("nan"),
            "days": len(k[k["day"] > r["rec_date"]]),
            "industry": industry,
            "cyclical": classify_cyclical_industry(industry, CFG.CYCLICAL_INDUSTRY_CODES,
                                                   CFG.CYCLICAL_INDUSTRY_KEYWORDS),
            "pe_pct": pe_bucket(r["signals_hit"]),
            "stabil_weak": "趋势待确认" in (r["signals_hit"] or ""),
            "trend_turn": "趋势转折" in (r["signals_hit"] or ""),
            "pb_high": "PB偏高" in (r["signals_hit"] or ""),
            "self_hist": "自身历史PE分位" in (r["signals_hit"] or ""),
        })
        time.sleep(0.12)

    d = pd.DataFrame(recs)
    d["excess"] = d["ret"] - d["bench"]

    def show(label, sub):
        if not len(sub):
            print(f"{label:<26} 无样本")
            return
        print(f"{label:<26} n={len(sub):>2}  收益 均值{sub['ret'].mean():+6.2f}% 中位{sub['ret'].median():+6.2f}% "
              f"胜率{(sub['ret']>0).mean()*100:5.1f}% | 超额 均值{sub['excess'].mean():+6.2f}% "
              f"跑赢{(sub['excess']>0).mean()*100:5.1f}%")

    print("=" * 110)
    print("全样本 38 事件（持有至 2026-10-23）")
    show("总计", d)

    print("\n【1】口径时期（09-28 修复技术分降级前 / 后）")
    show("  ≤09-24 旧口径", d[d["rec_date"] <= pd.Timestamp("2026-09-24")])
    show("  ≥09-30 现口径", d[d["rec_date"] >= pd.Timestamp("2026-09-30")])

    print("\n【2】技术分（MIN_TECHNICAL_SCORE_FORMAL=40）")
    show("  技术分 < 40", d[d["tech"] < 40])
    show("  技术分 ≥ 40", d[d["tech"] >= 40])

    print("\n【3】止跌等级（趋势待确认 = 仅 medium 确认，未达 strong）")
    show("  趋势待确认", d[d["stabil_weak"]])
    show("  含趋势转折", d[d["trend_turn"]])
    show("  趋势待确认 & 技术分≥40", d[d["stabil_weak"] & (d["tech"] >= 40)])
    show("  趋势待确认 & 技术分<40", d[d["stabil_weak"] & (d["tech"] < 40)])

    print("\n【4】行业 PE 分位（越低越「便宜」，估值分越高）")
    has_pe = d["pe_pct"].notna()
    show("  PE分位 ≤5%", d[has_pe & (d["pe_pct"] <= 5)])
    show("  PE分位 6~15%", d[has_pe & (d["pe_pct"] > 5) & (d["pe_pct"] <= 15)])
    show("  PE分位 >15%", d[has_pe & (d["pe_pct"] > 15)])
    show("  PE 分位缺失", d[~has_pe])

    print("\n【5】周期行业（生产开关 REQUIRE_CYCLICAL_NORMALIZED_VALUATION=%s）"
          % CFG.REQUIRE_CYCLICAL_NORMALIZED_VALUATION)
    show("  周期 cyclical", d[d["cyclical"] == "cyclical"])
    show("  非周期 non_cyclical", d[d["cyclical"] == "non_cyclical"])
    print("  周期组明细：")
    print(d[d["cyclical"] == "cyclical"][["rec_date", "name", "industry", "pe_pct", "tech", "ret", "excess"]]
          .assign(rec_date=lambda x: x["rec_date"].dt.strftime("%m-%d"))
          .to_string(index=False, float_format=lambda v: f"{v:+.2f}"))

    print("\n【6】PB 偏高标签（只打标签，不否决）")
    show("  PB偏高", d[d["pb_high"]])
    show("  无 PB偏高", d[~d["pb_high"]])

    print("\n【7】估值数据来源（行业分位 vs 自身历史分位）")
    show("  自身历史PE分位(10月起)", d[d["self_hist"]])
    show("  行业PE分位(9月)", d[~d["self_hist"]])

    print("\n【8】综合分档（0.45质量+0.30估值+0.25技术）")
    for lo, hi in [(0, 70), (70, 78), (78, 200)]:
        show(f"  score {lo}~{hi}", d[(d["score"] >= lo) & (d["score"] < hi)])

    print("\n【9】重复推荐（同一代码多次进正式池）")
    dup = d.groupby("code").size()
    for code, n in dup[dup > 1].items():
        sub = d[d["code"] == code]
        show(f"  {code} {sub['name'].iloc[0]} ×{n}", sub)

    return 0


if __name__ == "__main__":
    sys.exit(main())
