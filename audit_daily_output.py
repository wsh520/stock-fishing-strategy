"""多决策日实测：当前库级默认口径下 quality_value 的日产出、瓶颈与止跌闸门 A/B。

2026-10-12 重写（相对 2026-10-07 版本的三处校准 + 两项新增）：
  校准1（关键）：旧版 fund 未提供 financial_risk / industry，而当前库级默认
    （REQUIRE_FINANCIAL_RISK=True、REQUIRE_CYCLICAL_NORMALIZED_VALUATION=True）下
    这两项缺失会恒把候选压成 pending —— 旧版测出的「产出」被系统性低估。
    本版按决策日动态合成**自洽**的 verified 证据（直接调用生产校验函数生成，
    保证 validated_* 复核通过），使链路与生产同构。
  校准2：fake_operating 的 period 旧版硬编码 2026-06-30，早于该日期的决策日一律
    missing；本版按决策日取「满足 _minimum_period <= period <= day 的最大季末」，
    并提供 supplemental_rows 使周期股的 TTM 正常化估值可核验。
  校准3：val_context 旧版缺 industry 键，行业分类恒 missing。
  新增A：决策日扩展到 14 个（2025-11 ~ 2026-09，跨多种市场环境），可用 --days 覆盖。
  新增B：--stab-mode strict/risk_only/disabled 三口径对照 + 各口径放行组的
    F5/F10/F20/F60 前瞻收益中位数（面板后续真实价格，非历史价）。

已知近似（结论解读必须带上的口径声明）：
  · 近期经营与财务风险为**合成 verified 证据**（新浪接口离线不可达）⇒ 产出是
    **上限估计**；真实运行中这两层会淘汰一部分候选。
  · 行业估值用全行业分位，未实现生产的「PB/PE 盈利能力 proxy 可比组」。
  · 前瞻收益未剔除停牌缺口（停牌股已被 evaluate 否决，但后续价格序列可能跨缺口）。
  · 年度质量用 cache/annual_quality_v1_*.json 真实缓存；historical_valuation 走
    面板真实 peTTM 序列。

只读：不修改任何产线口径，不联网。
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter
from datetime import date, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.bottom_fishing_strategy import (  # noqa: E402
    StrategyConfig, has_halt_gap, evaluate_quality_value,
)
from src.financial_risk import evaluate_financial_risk, SOURCE_URL as RISK_SOURCE_URL  # noqa: E402
from src.recent_operating import (  # noqa: E402
    evaluate_operating_trend, SOURCE_URL as OPERATING_SOURCE_URL,
)
from src.fundamental_quality import evaluate_annual_quality  # noqa: E402

MIN_BARS = 250
DEFAULT_DAYS = ["2025-11-14", "2025-12-12", "2026-01-16", "2026-02-13", "2026-03-13",
                "2026-04-10", "2026-05-08", "2026-05-29", "2026-06-05", "2026-07-10",
                "2026-07-31", "2026-08-21", "2026-09-04", "2026-09-21"]
QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


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


def _minimum_period(day):
    if day.month >= 11:
        return date(day.year, 9, 30)
    if day.month >= 9:
        return date(day.year, 6, 30)
    if day.month >= 5:
        return date(day.year, 3, 31)
    return date(day.year - 1, 9, 30)


def _latest_quarter(day):
    """满足 _minimum_period(day) <= p <= day 的最大季末；无则 None。"""
    best = None
    for year in (day.year, day.year - 1):
        for m, d in QUARTER_ENDS:
            p = date(year, m, d)
            if _minimum_period(day) <= p <= day and (best is None or p > best):
                best = p
    return best


def _synth_operating_rows(day):
    """五个报告期的合成合并报表行：p0、上年同期、前一季、上年同期的前一季、上年报。"""
    p0 = _latest_quarter(day)
    if p0 is None:
        return None, None
    p1 = date(p0.year - 1, p0.month, p0.day)
    idx = QUARTER_ENDS.index((p0.month, p0.day))
    pm, pd_ = QUARTER_ENDS[(idx - 1) % 4]
    p2 = date(p0.year if idx > 0 else p0.year - 1, pm, pd_)
    p3 = date(p2.year - 1, p2.month, p2.day)
    annual_ly = date(p0.year - 1, 12, 31)
    values = {"revenue": 1000.0, "net_profit": 100.0, "deducted_profit": 90.0,
              "operating_cashflow": 100.0, "attributable_profit": 95.0,
              "gross_margin": 30.0, "net_margin": 10.0, "roe": 12.0}
    rows = []
    for p in sorted({p0, p1, p2, p3, annual_ly}):
        rows.append(dict({"report_date": p, "available_date": min(p + timedelta(days=60), day),
                          "source": OPERATING_SOURCE_URL, "report_source": "gjzb",
                          "data_source": "其他", "statement_basis": "consolidated",
                          "currency": "CNY"}, **values))
    return rows, p0


def fake_operating(code, day):
    """合成「已核验」近期经营证据（含 TTM operands，供周期股正常化估值核验）。

    近期经营走新浪接口联网获取、无磁盘缓存，离线审计无法复现真实值；这里用
    恒定的温和增长合成 verified 证据，只为验证闸门链路与产出量级，
    **不代表真实的近期经营判断**（真实结论须以联网运行为准）。
    """
    day = date.fromisoformat(str(day)[:10])
    rows, p0 = _synth_operating_rows(day)
    if rows is None:
        return None
    checked = evaluate_operating_trend(rows, day)
    if checked["status"] not in {"verified", "weak"}:
        return None
    metrics = {"revenue": 1000.0, "net_profit": 100.0, "deducted_profit": 90.0,
               "operating_cashflow": 100.0, "gross_margin": 30.0, "net_margin": 10.0,
               "revenue_yoy": 0.0, "net_profit_yoy": 0.0, "deducted_profit_yoy": 0.0,
               "gross_margin_change_pp": 0.0, "net_margin_change_pp": 0.0,
               "operating_cashflow_change": 0.0, "operating_cashflow_yoy": 0.0,
               "net_profit_ttm": checked["metrics"].get("net_profit_ttm"),
               "attributable_profit_ttm": checked["metrics"].get("attributable_profit_ttm")}
    return {"code": str(code).zfill(6), "as_of": day, "status": "verified",
            "period": checked["period"], "available_date": checked["available_date"],
            "metrics": metrics, "missing_tags": [], "reasons": [],
            "supplemental_rows": rows, "source": OPERATING_SOURCE_URL}


def fake_financial_risk(code, day):
    """合成「已核验」财务风险证据：直接调用生产 evaluate_financial_risk 生成，
    使 validated_financial_risk_status 的复核（指标重算比对）天然通过。"""
    day = date.fromisoformat(str(day)[:10])
    p0 = _latest_quarter(day)
    if p0 is None:
        return None
    row = {"report_date": p0, "available_date": min(p0 + timedelta(days=60), day),
           "source": RISK_SOURCE_URL, "statement_basis": "consolidated", "currency": "CNY",
           "total_assets": 1000.0, "total_liabilities": 400.0, "goodwill": 50.0,
           "net_profit": 100.0, "deducted_profit": 90.0}
    result = evaluate_financial_risk([row], day)
    if result["status"] != "verified":
        return None
    return dict(result, code=str(code).zfill(6), as_of=day)


def annual_for(code, day):
    """取该股在 day 之前最新可用年度（3 年），走与生产相同的年度质量评估。"""
    best = None
    for h in sorted(glob.glob(f"cache/annual_quality_v1_{code}_*.json")):
        try:
            j = json.load(open(h, encoding="utf-8"))
        except Exception:
            continue
        a = j.get("annual_rows") or []
        if isinstance(a, dict):
            a = [a]
        ys = [x.get("year") for x in a if isinstance(x, dict)]
        if not ys:
            continue
        av = [str(x.get("available_date"))[:10] for x in a if isinstance(x, dict) and x.get("available_date")]
        if not av or max(av) > day:
            continue
        if best is None or max(ys) > best[0]:
            best = (max(ys), a)
    return None if best is None else best[1]


def run_day(panels, ind, snap, day, cfg):
    c = Counter()
    finals = []
    for code, d in panels.items():
        s = d[d["date"] <= day].reset_index(drop=True)
        if len(s) < MIN_BARS or s["date"].iloc[-1] != day:
            c["跳过(非当日/不足)"] += 1
            continue
        if has_halt_gap(s, cfg):
            c["停牌缺口"] += 1
            continue
        c["1数据/时效/停牌"] += 1
        if float(s["amount"].tail(20).mean()) < cfg.MIN_AMOUNT:
            c["2流动性"] += 1
            continue
        w = s.tail(cfg.LOW_POSITION_LOOKBACK)
        close = float(s["close"].iloc[-1])
        cw = w["close"].to_numpy(dtype=float)
        pos = float(((cw < close).sum() + .5 * (cw == close).sum()) / len(cw))
        if pos > cfg.LOW_POSITION_MAX:
            c["3低位>0.4"] += 1
            continue
        c["2流动性+低位"] += 1
        try:
            pe = float(s["peTTM"].iloc[-1])
        except Exception:
            pe = None
        if pe is None or not np.isfinite(pe):
            c["4估值缺失(pending)"] += 1
            continue
        if pe <= 0:
            c["4 PE<=0"] += 1
            continue
        ind_name = ind.get(code, "")
        ctx = None
        if ind_name in snap:
            arr = snap[ind_name]
            ctx = {"mode": "industry", "industry": ind_name,
                   "pe_pct": float((arr <= pe).sum() / len(arr)), "pb_pct": None}
            if ctx["pe_pct"] > cfg.VALUATION_INDUSTRY_PERCENTILE_MAX:
                c["4行业PE分位>0.6"] += 1
                continue
            c["3通过PE(行业口径)"] += 1
        elif pe > cfg.MAX_PE_TTM:
            c["4 PE>25(绝对)"] += 1
            continue
        else:
            ctx = {"mode": "absolute", "industry": ind_name}
            c["3通过PE(绝对口径)"] += 1

        ar = annual_for(code, day)
        if ar is None:
            c["5年度财务缺失(pending)"] += 1
            continue
        q = evaluate_annual_quality(
            ar, day, years=cfg.QUALITY_YEARS,
            median_roe_min=cfg.QUALITY_MEDIAN_ROE_MIN, min_roe=cfg.QUALITY_MIN_ROE,
            cash_conversion_min=cfg.QUALITY_CASH_CONVERSION_MIN, financial=False,
            require_annual_net_profit_positive=cfg.QUALITY_REQUIRE_ANNUAL_NET_PROFIT_POSITIVE,
            require_annual_cashflow_positive=cfg.QUALITY_REQUIRE_ANNUAL_CASHFLOW_POSITIVE)
        if q["status"] == "failed":
            c["5年度质量不达标"] += 1
            continue
        if q["status"] != "verified":
            c["5年度质量partial(pending)"] += 1
            continue
        c["4通过年度质量"] += 1

        operating = fake_operating(code, day)
        risk = fake_financial_risk(code, day)
        fund = {"debt_ratio": 40., "roe": 12., "annual_rows": ar,
                "operating_trend": operating, "financial_risk": risk}
        try:
            sig, reason = evaluate_quality_value(s, code, "X", cfg, {"regime": "neutral"},
                                                 fund, latest_trade_date=day, val_context=ctx)
        except Exception as exc:
            c["ERROR"] += 1
            c[f"ERROR:{type(exc).__name__}"] += 1
            continue
        if sig is None:
            c[f"6{reason}"] += 1
            continue
        if sig.tier != "formal":
            c["6通过但降级pending"] += 1
            for t in str(sig.missing_tags or "").split(","):
                if t:
                    c[f"pending:{t}"] += 1
            continue
        c["6通过全部闸门"] += 1
        lo = float(w["low"].min())
        finals.append((code, sig.score, (close / lo - 1) * 100, sig.stabilization_level))
    return c, finals


def forward_returns(panels, finals, day):
    """放行组在决策日之后 5/10/20/60 个 bar 的收益中位数（面板真实后续价格）。"""
    out = {}
    for h in (5, 10, 20, 60):
        vals = []
        for code, *_ in finals:
            d = panels.get(code)
            if d is None:
                continue
            s = d[d["date"] <= day]
            i = len(s) - 1
            if i + h >= len(d):
                continue
            base = float(s["close"].iloc[-1])
            if base > 0:
                vals.append((float(d["close"].iloc[i + h]) / base - 1) * 100)
        out[h] = (float(np.median(vals)) if vals else None, len(vals))
    return out


def main():
    ap = argparse.ArgumentParser(description="当前口径多决策日产出实测 + 止跌闸门 A/B")
    ap.add_argument("--days", default=",".join(DEFAULT_DAYS), help="逗号分隔决策日")
    ap.add_argument("--stab-mode", default="strict",
                    choices=["strict", "risk_only", "disabled"], help="止跌闸门口径")
    args = ap.parse_args()
    days = [d.strip() for d in args.days.split(",") if d.strip()]

    cfg = StrategyConfig()
    cfg.QV_STABILIZATION_MODE = args.stab_mode
    panels = load_panels()
    ind = json.load(open("backtest_cache/industry.json", encoding="utf-8"))

    print(f"面板 {len(panels)} 只｜决策日 {len(days)} 个（{days[0]} ~ {days[-1]}）")
    print(f"库级默认：MIN_QV_SCORE={cfg.MIN_QV_SCORE} MIN_QUALITY_SCORE={cfg.MIN_QUALITY_SCORE} "
          f"MIN_TECHNICAL_SCORE_FORMAL={cfg.MIN_TECHNICAL_SCORE_FORMAL} "
          f"止跌闸门={cfg.QV_STABILIZATION_GATE}({args.stab_mode}) "
          f"V值高位保护=recent_structure(距底≤clip(5×ATR,10%,20%))")
    print("近似声明：近期经营/财务风险为合成 verified 证据 ⇒ 产出为上限估计；行业估值为全行业分位\n")

    results = []
    for day in days:
        snap = {}
        for code, d in panels.items():
            s = d[d["date"] <= day]
            if len(s) < MIN_BARS or not ind.get(code):
                continue
            try:
                pe = float(s["peTTM"].iloc[-1])
            except Exception:
                continue
            if np.isfinite(pe):
                snap.setdefault(ind[code], []).append(pe)
        snap = {k: np.sort(np.array(v)) for k, v in snap.items()
                if len(v) >= cfg.VALUATION_INDUSTRY_MIN_PEERS}
        c, finals = run_day(panels, ind, snap, day, cfg)
        results.append((day, c, finals))
        print(f"{day}: 全通过 {len(finals)} 只 | " + " | ".join(f"{k}:{v}" for k, v in c.most_common(6)))
        if finals:
            gs = [f[2] for f in finals]
            print(f"        距低点涨幅 中位{np.median(gs):+.1f}% 最大{max(gs):+.1f}% | "
                  f"止跌层 {Counter(f[3] for f in finals).most_common()}")

    print("\n" + "=" * 78)
    counts = [len(f) for _, _, f in results]
    print(f"{len(days)} 个决策日实际产出：{counts}")
    print(f"  中位数 {np.median(counts):.0f} 只/日   均值 {np.mean(counts):.1f} 只/日   "
          f"范围 {min(counts)}~{max(counts)}")
    print(f"  空仓天数：{sum(1 for x in counts if x == 0)}/{len(days)}")
    print(f"  达到「每天 2~3 只」的天数：{sum(1 for x in counts if 2 <= x <= 3)}/{len(days)}")
    print(f"  超过 MAX_PICKS={cfg.MAX_PICKS} 的天数：{sum(1 for x in counts if x > cfg.MAX_PICKS)}/{len(days)}")

    print("\n瓶颈定位（各层平均只数；带数字前缀的是通过/淘汰计数器，含义见键名）：")
    agg = Counter()
    for _, c, _ in results:
        for k, v in c.items():
            agg[k] += v / len(results)
    for k, v in agg.most_common():
        print(f"  {k:<26} 平均 {v:6.1f} 只")

    all_finals = [(day, f) for day, _, fs in results for f in fs]
    if all_finals:
        print("\n" + "=" * 78)
        print(f"止跌闸门={args.stab_mode}：放行组合计 {len(all_finals)} 条，前瞻收益中位数 %")
        print("  （面板后续真实价格；每条=一次「决策日×个股」观测，后期决策日窗口不足则剔除）")
        by_day = {}
        for day, f in all_finals:
            by_day.setdefault(day, []).append(f)
        med = {}
        for h in (5, 10, 20, 60):
            vals = []
            for day, fs in by_day.items():
                for code, *_ in fs:
                    d = panels.get(code)
                    if d is None:
                        continue
                    s = d[d["date"] <= day]
                    i = len(s) - 1
                    if i + h >= len(d):
                        continue
                    base = float(s["close"].iloc[-1])
                    if base > 0:
                        vals.append((float(d["close"].iloc[i + h]) / base - 1) * 100)
            med[h] = (float(np.median(vals)) if vals else float("nan"), len(vals))
        print("  " + "  ".join(f"F{h}={med[h][0]:+.2f}%(n={med[h][1]})" for h in (5, 10, 20, 60)))


if __name__ == "__main__":
    main()
