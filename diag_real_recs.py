"""基于真实落库推荐记录（用户导出 40 条）追踪推荐后实际收益（只读分析）。

口径说明：
- 行情源：cache/daily_{market}.{code}_last600_qfq.csv（前复权，以最新交易日为锚，覆盖至 2026-09-30）
- 基准价：推荐日（rec_date）当日前复权收盘价（与追踪模块 base_close 口径一致；
  与 rec_close 的差异若显著则说明期间除权，单独标注）
- 收益 F_n = 推荐日后第 n 个交易日收盘 / 推荐日收盘 - 1
- 止损触发：次日~窗口内最低价 <= stop_loss（日内触及口径）
- 止盈触发：次日~窗口内最高价 >= take_profit
"""
import glob
import os
import sys

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))

# (id, rec_date, code, name, rec_close, score, grade, tech, rsi, stop, tp, rr, env, div, signals, fund, weekly, tier, strategy)
RECS = [
    (1, "2026-09-08", "002568", "百润股份", 18.360, 75.0, "B", 75.0, 51.5, 17.170, 20.200, 1.55, "neutral", 0, "", "", "", "", "bottom_fishing"),
    (2, "2026-09-16", "603558", "健盛集团", 13.050, 81.3, "A", 81.3, 69.9, 12.400, 14.840, 2.76, "neutral", 0, "", "missing", "unverified", "pending", "volume_breakout"),
    (3, "2026-09-17", "600933", "爱柯迪", 13.250, 69.6, "B", 90.0, 48.1, 12.480, 14.580, 1.72, "neutral", 0, "优质低估低位,动能改善,趋势转折,RSI超卖反弹,放量企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (4, "2026-09-17", "002668", "TCL智家", 9.100, 67.7, "B", 40.0, 43.3, 8.360, 10.010, 1.23, "neutral", 0, "优质低估低位,动能改善,趋势转折", "verified", "not_required", "formal", "bottom_fishing"),
    (5, "2026-09-17", "600873", "梅花生物", 7.510, 67.5, "B", 0.0, 39.2, 7.140, 8.260, 2.02, "neutral", 0, "优质低估低位,趋势待确认", "verified", "not_required", "formal", "bottom_fishing"),
    (6, "2026-09-17", "600096", "云天化", 28.290, 66.4, "B", 0.0, 37.3, 26.440, 31.120, 1.53, "neutral", 0, "优质低估低位,趋势待确认", "verified", "not_required", "formal", "bottom_fishing"),
    (7, "2026-09-17", "002705", "新宝股份", 11.400, 65.1, "B", 65.0, 53.2, 10.940, 12.540, 2.48, "neutral", 0, "优质低估低位,动能改善,趋势转折,放量企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (13, "2026-09-18", "603202", "天有为", 51.750, 70.3, "B", 40.0, 47.5, 49.660, 56.930, 2.48, "neutral", 0, "优质低估低位,动能改善,趋势转折", "verified", "not_required", "formal", "bottom_fishing"),
    (14, "2026-09-18", "603529", "爱玛科技", 21.430, 70.0, "B", 40.0, 49.2, 20.390, 23.570, 2.06, "neutral", 0, "优质低估低位,动能改善,趋势转折", "verified", "not_required", "formal", "bottom_fishing"),
    (19, "2026-09-18", "000786", "北新建材", 15.930, 74.8, "B", 40.0, 35.6, 14.980, 17.520, 1.67, "neutral", 0, "优质低估低位,动能改善,趋势转折,行业估值分位PE5%/PB20%,2026Q2 净利同比-28%", "verified", "not_required", "formal", "bottom_fishing"),
    (27, "2026-09-21", "002475", "立讯精密", 54.440, 75.6, "B", 40.0, 47.5, 50.010, 59.880, 1.23, "neutral", 0, "优质低估低位,动能改善,趋势转折,行业估值分位PE13%/PB57%,2026Q2 净利同比+17%,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (29, "2026-09-21", "002555", "三七互娱", 17.730, 75.4, "B", 40.0, 42.0, 16.870, 19.500, 2.06, "neutral", 0, "优质低估低位,动能改善,趋势转折,行业估值分位PE16%/PB38%,2026Q2 净利同比+26%,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (30, "2026-09-21", "603387", "基蛋生物", 9.940, 69.0, "B", 69.0, 67.6, 9.470, 11.400, 3.10, "neutral", 0, "", "verified", "confirmed", "formal", "volume_breakout"),
    (31, "2026-09-21", "002780", "三夫户外", 14.920, 68.8, "B", 68.8, 62.7, 14.020, 17.160, 2.49, "neutral", 0, "", "verified", "confirmed", "formal", "volume_breakout"),
    (32, "2026-09-21", "002145", "钛能化学", 4.850, 67.5, "B", 67.5, 72.6, 4.570, 5.470, 2.20, "neutral", 0, "", "verified", "confirmed", "formal", "volume_breakout"),
    (41, "2026-09-22", "600690", "海尔智家", 20.200, 81.7, "A", 50.0, 36.9, 19.300, 22.220, 2.25, "neutral", 0, "优质低估低位,动能改善,RSI超卖反弹,放量企稳,行业估值分位PE3%/PB17%,2026Q2 净利同比-13%,业绩下滑预警,⚠左侧·仍在下跌（MACD深度走弱，止跌未确认）", "verified", "not_required", "formal", "bottom_fishing"),
    (42, "2026-09-22", "601600", "中国铝业", 9.270, 78.3, "B", 58.0, 45.9, 8.670, 10.200, 1.55, "neutral", 0, "优质低估低位,动能改善,趋势转折,放量上涨,行业估值分位PE13%/PB29%,2026Q2 净利同比+94%,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (43, "2026-09-22", "000885", "城发环境", 11.740, 77.5, "B", 40.0, 35.3, 11.400, 12.910, 3.41, "neutral", 0, "优质低估低位,动能改善,趋势转折,行业估值分位PE5%/PB4%,2026Q2 净利同比-4%,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (45, "2026-09-22", "000792", "盐湖股份", 24.870, 84.6, "A", 40.0, 34.6, 23.400, 27.360, 1.69, "neutral", 0, "优质低估低位,动能改善,趋势转折,行业估值分位PE4%/PB65%,PB偏高(2.8)仅风险提示,2026Q2 净利同比+153%,近60日相对强度-8.9pp,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (46, "2026-09-22", "002847", "盐津铺子", 43.570, 82.9, "A", 0.0, 42.4, 41.030, 47.930, 1.71, "neutral", 0, "优质低估低位,趋势待确认,行业估值分位PE4%/PB93%,PB偏高(5.6)仅风险提示,2026Q2 净利同比+15%,近60日相对强度+6.4pp,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (47, "2026-09-22", "600211", "西藏药业", 36.420, 80.0, "B", 0.0, 46.0, 34.880, 40.060, 2.36, "neutral", 0, "优质低估低位,趋势待确认,行业估值分位PE4%/PB72%,PB偏高(3.0)仅风险提示,2026Q2 净利同比+0%,近60日相对强度+9.2pp,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (49, "2026-09-23", "000895", "双汇发展", 23.330, 84.4, "A", 40.0, 35.8, 22.640, 25.660, 3.40, "neutral", 0, "优质低估低位,动能改善,趋势转折,行业估值分位PE10%/PB92%,PB偏高(3.9)仅风险提示,2026Q2 净利同比-1%,业绩下滑预警,近60日相对强度+9.4pp,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (50, "2026-09-23", "600096", "云天化", 27.950, 84.6, "A", 0.0, 37.7, 26.400, 30.750, 1.80, "neutral", 0, "优质低估低位,趋势待确认,行业估值分位PE1%/PB45%,2026Q2 净利同比+3%,近60日相对强度-0.6pp,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (51, "2026-09-23", "002690", "美亚光电", 16.030, 81.9, "A", 65.0, 58.6, 15.350, 17.630, 2.37, "neutral", 0, "优质低估低位,动能改善,趋势转折,放量企稳,行业估值分位PE29%/PB86%,PB偏高(5.5)仅风险提示,2026Q2 净利同比+4%,近60日相对强度+20.5pp,右侧·站上MA20/MA60（趋势转强）", "verified", "not_required", "formal", "bottom_fishing"),
    (52, "2026-09-23", "002946", "新乳业", 14.450, 82.4, "A", 40.0, 36.7, 13.650, 15.900, 1.80, "neutral", 0, "优质低估低位,动能改善,趋势转折,行业估值分位PE10%/PB85%,PB偏高(3.5)仅风险提示,2026Q2 净利同比+16%,近60日相对强度+11.8pp,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (53, "2026-09-24", "600096", "云天化", 27.560, 84.6, "A", 0.0, 35.3, 26.050, 30.320, 1.82, "neutral", 0, "优质低估低位,趋势待确认,行业估值分位PE1%/PB47%,2026Q2 净利同比+3%,近60日相对强度-4.1pp,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (54, "2026-09-24", "002847", "盐津铺子", 43.150, 82.9, "A", 0.0, 40.6, 40.730, 47.470, 1.78, "neutral", 0, "优质低估低位,趋势待确认,行业估值分位PE4%/PB93%,PB偏高(5.6)仅风险提示,2026Q2 净利同比+15%,近60日相对强度-0.8pp,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (55, "2026-09-24", "600211", "西藏药业", 36.480, 79.7, "B", 0.0, 46.7, 34.960, 40.130, 2.40, "neutral", 0, "优质低估低位,趋势待确认,行业估值分位PE5%/PB73%,PB偏高(3.0)仅风险提示,2026Q2 净利同比+0%,近60日相对强度+5.4pp,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (56, "2026-09-24", "603605", "珀莱雅", 56.450, 79.2, "B", 0.0, 41.4, 53.360, 62.100, 1.83, "neutral", 0, "优质低估低位,趋势待确认,行业估值分位PE6%/PB75%,PB偏高(3.4)仅风险提示,2026Q2 净利同比+45%,近60日相对强度+5.6pp,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (57, "2026-09-24", "603817", "海峡环保", 6.160, 76.3, "B", 76.3, 63.2, 5.850, 7.080, 2.95, "neutral", 0, "", "verified", "confirmed", "formal", "volume_breakout"),
    (62, "2026-09-24", "002611", "东方精工", 14.440, 72.9, "B", 58.0, 48.3, 13.230, 18.770, 3.57, "neutral", 0, "优质低估低位,动能改善,趋势转折,放量上涨,行业估值分位PE1%/PB24%,2026Q2 净利同比+829%,近60日相对强度-8.2pp,右侧雏形·站上MA20（仍在MA60下方）", "verified", "not_required", "formal", "bottom_fishing"),
    (63, "2026-09-24", "001328", "登康口腔", 30.320, 61.0, "B", 51.6, 55.2, 27.170, 39.420, 2.89, "neutral", 0, "优质低估低位,动能改善,放量上涨,行业估值分位PE40%/PB77%,PB偏高(3.5)仅风险提示,2026-06-30 净利同比+19%,近60日相对强度+20.1pp,止跌强确认,右侧·站上MA20/MA60（趋势转强）", "verified", "not_required", "formal", "bottom_fishing"),
    (64, "2026-09-28", "002946", "新乳业", 14.540, 84.9, "A", 70.0, 40.3, 13.380, 18.900, 3.75, "neutral", 1, "优质低估低位,动能改善,趋势转折,底背离,行业估值分位PE10%/PB85%,PB偏高(3.5)仅风险提示,2026-06-30 净利同比+16%,近60日相对强度+9.7pp,止跌中确认,左侧·MA20下方待企稳", "verified", "not_required", "formal", "bottom_fishing"),
    (65, "2026-09-28", "000538", "云南白药", 50.330, 69.1, "B", 70.0, 57.7, 48.370, 65.430, 7.69, "neutral", 0, "优质低估低位,动能改善,趋势转折,行业估值分位PE20%/PB54%,2026-06-30 净利同比+1%,近60日相对强度+15.5pp,止跌强确认,横盘结构确认,右侧·站上MA20/MA60（趋势转强）", "verified", "not_required", "formal", "bottom_fishing"),
    (66, "2026-09-30", "605499", "东鹏饮料", 112.250, 83.7, "A", 70.0, 46.5, 104.180, 145.930, 4.17, "bear", 0, "优质低估低位,动能改善,趋势转折,行业估值分位PE29%/PB76%,PB偏高(4.2)仅风险提示,2026-06-30 净利同比+21%,近60日相对强度+1.7pp,止跌中确认,右侧雏形·站上MA20（仍在MA60下方）", "verified", "not_required", "formal", "bottom_fishing"),
    (67, "2026-09-30", "000963", "华东医药", 27.690, 74.6, "B", 60.0, 57.7, 25.830, 36.000, 4.45, "bear", 0, "优质低估低位,动能改善,放量企稳,行业估值分位PE15%/PB59%,2026-06-30 净利同比+2%,近60日相对强度+5.1pp,止跌中确认,右侧雏形·站上MA20（仍在MA60下方）", "verified", "not_required", "formal", "bottom_fishing"),
    (68, "2026-10-08", "603345", "安井食品", 78.390, 72.1, "B", 61.4, 54.7, 66.630, 101.910, 2.00, "bear", 0, "优质低估低位,自身历史PE分位16%（250个样本）,趋势待确认,行业估值分位PE16%/PB27%,2026-06-30 净利同比+23%,近60日相对强度+7.1pp,近期有效底部2026-09-09，距底+7.8%,止跌中确认,右侧雏形·站上MA20（仍在MA60下方）", "verified", "not_required", "formal", "bottom_fishing"),
    (69, "2026-10-08", "605338", "巴比食品", 18.770, 67.0, "B", 54.0, 57.1, 15.950, 24.400, 2.00, "bear", 0, "优质低估低位,自身历史PE分位20%（250个样本）,动能改善,趋势转折,放量企稳,行业估值分位PE15%/PB47%,2026-06-30 净利同比+3%,近60日相对强度+12.3pp,近期有效底部2026-09-17，距底+8.1%,止跌中确认,右侧雏形·站上MA20（仍在MA60下方）", "verified", "not_required", "formal", "bottom_fishing"),
    (70, "2026-10-09", "000963", "华东医药", 27.290, 79.0, "B", 83.4, 53.2, 23.200, 35.480, 2.00, "bear", 0, "优质低估低位,自身历史PE分位9%（250个样本）,动能改善,放量企稳,行业估值分位PE25%/PB57%,2026-06-30 净利同比+2%,近60日相对强度+7.5pp,近期有效底部2026-09-17，距底+5.8%,止跌中确认,右侧雏形·站上MA20（仍在MA60下方）", "verified", "not_required", "formal", "bottom_fishing"),
    (71, "2026-10-09", "600993", "马应龙", 22.950, 66.9, "B", 49.0, 54.6, 19.510, 29.840, 2.00, "bear", 0, "优质低估低位,自身历史PE分位9%（250个样本）,动能改善,趋势转折,放量企稳,行业估值分位PE28%/PB55%,2026-06-30 净利同比+6%,近60日相对强度+10.7pp,近期有效底部2026-09-11，距底+3.7%,止跌中确认,右侧雏形·站上MA20（仍在MA60下方）", "verified", "not_required", "formal", "bottom_fishing"),
]

COLS = ["id", "date", "code", "name", "close", "score", "grade", "tech", "rsi",
        "stop", "tp", "rr", "env", "div", "signals", "fund", "weekly", "tier", "strategy"]


def load_panel(code):
    for market in ("sh", "sz"):
        p = os.path.join(BASE, "cache", f"daily_{market}.{code}_last600_qfq.csv")
        if os.path.exists(p):
            d = pd.read_csv(p)
            d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            for c in ("open", "high", "low", "close"):
                d[c] = pd.to_numeric(d[c], errors="coerce")
            return d.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return None


def tag_of(sig):
    if "仍在下跌" in sig:
        return "左侧·仍在下跌"
    if "左侧·MA20下方" in sig:
        return "左侧·MA20下方"
    if "右侧雏形" in sig:
        return "右侧雏形·站上MA20"
    if "右侧·站上MA20/MA60" in sig:
        return "右侧·站上MA20/MA60"
    if "趋势待确认" in sig:
        return "趋势待确认(tech=0)"
    return "无位置标签"


def main():
    df = pd.DataFrame(RECS, columns=COLS)
    rows = []
    for _, r in df.iterrows():
        panel = load_panel(r["code"])
        if panel is None:
            print(f"[缺行情] {r['code']} {r['name']}")
            continue
        dates = panel["date"].tolist()
        if r["date"] not in dates:
            print(f"[推荐日不在序列] {r['code']} {r['name']} {r['date']}")
            continue
        i = dates.index(r["date"])
        base_qfq = float(panel["close"].iloc[i])
        rec = dict(r)
        rec["base_qfq"] = base_qfq
        rec["close_diff_pct"] = (base_qfq / r["close"] - 1) * 100  # 除权基准差
        for n in (1, 3, 5, 10):
            j = i + n
            rec[f"F{n}"] = (float(panel["close"].iloc[j]) / base_qfq - 1) * 100 if j < len(panel) else None
        rec["n_avail"] = len(panel) - 1 - i
        # 止损/止盈：次日开盘起，窗口取 min(10, 可用天数)
        w = min(10, len(panel) - 1 - i)
        if w > 0:
            lo = float(panel["low"].iloc[i + 1:i + 1 + w].min())
            hi = float(panel["high"].iloc[i + 1:i + 1 + w].max())
            rec["min_low"] = (lo / base_qfq - 1) * 100
            rec["max_high"] = (hi / base_qfq - 1) * 100
            rec["stop_hit"] = bool(lo <= r["stop"])
            rec["tp_hit"] = bool(hi >= r["tp"])
            # 首次触及顺序（粗判：若两者都触达，比较触达日）
            if rec["stop_hit"] and rec["tp_hit"]:
                stop_day = next(k for k in range(1, w + 1) if float(panel["low"].iloc[i + k]) <= r["stop"])
                tp_day = next(k for k in range(1, w + 1) if float(panel["high"].iloc[i + k]) >= r["tp"])
                rec["first_hit"] = "stop" if stop_day <= tp_day else "tp"
            elif rec["stop_hit"]:
                rec["first_hit"] = "stop"
            elif rec["tp_hit"]:
                rec["first_hit"] = "tp"
            else:
                rec["first_hit"] = "none"
        else:
            rec["min_low"] = rec["max_high"] = None
            rec["stop_hit"] = rec["tp_hit"] = None
            rec["first_hit"] = "no_data"
        rec["tag"] = tag_of(r["signals"])
        rows.append(rec)

    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(BASE, "audit_real_recs_out.csv"), index=False, encoding="utf-8-sig")

    bf = out[(out["strategy"] == "bottom_fishing") & (out["tier"] != "pending")].copy()
    print(f"\n=== bottom_fishing 正式推荐 {len(bf)} 条（volume_breakout {len(out[out.strategy=='volume_breakout'])} 条另列）===\n")

    print("【1】逐条明细（前复权基准，收盘口径）")
    print(f"{'日期':<11}{'代码':<8}{'名称':<7}{'tech':>6}{'F1':>8}{'F3':>8}{'F5':>8}{'F10':>8}{'窗口最低':>9}{'止损':>5}{'止盈':>5}  标签")
    for _, r in bf.iterrows():
        f = lambda k: f"{r[k]:+7.2f}%" if pd.notna(r[k]) else "    n/a"
        print(f"{r['date']:<11}{r['code']:<8}{r['name']:<7}{r['tech']:>6.1f}{f('F1')}{f('F3')}{f('F5')}{f('F10')}"
              f"{r['min_low']:>8.1f}%{'是' if r['stop_hit'] else '否':>5}{'是' if r['tp_hit'] else '否':>5}  {r['tag']}")

    print("\n【2】总体收益（F1/F3/F5/F10）")
    for n in (1, 3, 5, 10):
        s = bf[f"F{n}"].dropna()
        if len(s):
            print(f"  F{n:<3} n={len(s):<3} 中位数 {s.median():+6.2f}%  平均 {s.mean():+6.2f}%  "
                  f"上涨占比 {(s > 0).mean() * 100:4.1f}%  下跌占比 {(s < 0).mean() * 100:4.1f}%")

    print("\n【3】按位置标签分组（F5/F10 中位数与胜率）")
    for tag, g in bf.groupby("tag"):
        line = f"  {tag:<18} n={len(g):<3}"
        for n in (5, 10):
            s = g[f"F{n}"].dropna()
            line += f"  F{n}: {s.median():+6.2f}% 胜率{(s > 0).mean() * 100:4.0f}%" if len(s) else f"  F{n}: n/a"
        print(line)

    print("\n【4】按技术分分组")
    for label, g in (("tech=0（趋势待确认）", bf[bf["tech"] <= 0.1]),
                     ("tech>=40", bf[bf["tech"] >= 39.9])):
        line = f"  {label:<22} n={len(g):<3}"
        for n in (1, 5):
            s = g[f"F{n}"].dropna()
            line += f"  F{n}: {s.median():+6.2f}% 胜率{(s > 0).mean() * 100:4.0f}%" if len(s) else f"  F{n}: n/a"
        print(line)

    print("\n【5】止损/止盈触发（10日窗口，日内极值口径）")
    v = bf.dropna(subset=["stop_hit"])
    print(f"  n={len(v)}  止损触发 {v['stop_hit'].mean() * 100:.0f}%  止盈触发 {v['tp_hit'].mean() * 100:.0f}%  "
          f"双触同时止盈先到 {((v['first_hit'] == 'tp')).mean() * 100:.0f}%")
    print(f"  窗口最低价中位数 {v['min_low'].median():+.2f}%（相对推荐日收盘）")

    print("\n【6】重复推荐追踪（同一股票多次推荐）")
    for code, g in bf.groupby("code"):
        if len(g) > 1:
            seq = " → ".join(f"{r['date'][5:]} 收{r['close']:.2f}(F5 {r['F5']:+.1f}%)" if pd.notna(r["F5"])
                             else f"{r['date'][5:]} 收{r['close']:.2f}" for _, r in g.iterrows())
            print(f"  {code} {g['name'].iloc[0]}（{len(g)}次）: {seq}")

    print("\n【7】market_env 分组")
    for env, g in bf.groupby("env"):
        s5 = g["F5"].dropna()
        print(f"  {env:<8} n={len(g):<3} F5 中位数 {s5.median():+6.2f}%  胜率 {(s5 > 0).mean() * 100:4.0f}%" if len(s5) else f"  {env} n/a")

    print("\n【8】volume_breakout 对照")
    vb = out[out["strategy"] == "volume_breakout"]
    for _, r in vb.iterrows():
        f = lambda k: f"{r[k]:+7.2f}%" if pd.notna(r[k]) else "    n/a"
        print(f"  {r['date']} {r['code']} {r['name']:<6} tier={r['tier']:<8} F1{f('F1')} F5{f('F5')} F10{f('F10')} 止损触发={r['stop_hit']}")

    print("\n【9】复权基准校验（推荐日收盘 vs 前复权收盘，偏差>1%提示除权）")
    bad = out[out["close_diff_pct"].abs() > 1]
    if len(bad):
        for _, r in bad.iterrows():
            print(f"  {r['code']} {r['name']} {r['date']}: rec_close={r['close']} vs qfq={r['base_qfq']:.2f} ({r['close_diff_pct']:+.1f}%)")
    else:
        print("  全部 40 条偏差均 <1%，无除权干扰，收益计算可信")


if __name__ == "__main__":
    main()
