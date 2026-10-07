"""多决策日实测：修复后 quality_value 的实际日产出与瓶颈定位。

目的：主人期望「每天推荐 2~3 只」。本脚本用真实面板 + 真实年度财务缓存，
在多个决策日上跑完整链路，回答：
  1. 实际每天产出几只？（中位数/分布/为空天数）
  2. 瓶颈在哪一层？（哪层把候选压到个位数）
  3. MAX_PICKS=5 与「每天 2~3 只」的期望是否匹配？
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
    StrategyConfig, has_halt_gap, evaluate_quality_value,
)

MIN_BARS = 250


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


def fake_operating(code, day):
    """合成「已核验」近期经营证据。

    REQUIRE_RECENT_OPERATING=True 时，缺 operating_trend 会一律降级 pending。
    该证据走新浪接口联网获取、有内存缓存但**无磁盘缓存**，故离线审计无法复现；
    这里合成字段齐备的 verified 证据，只为验证闸门链路与产出量级，
    **不代表真实的近期经营判断**（真实结论须以联网运行为准）。
    """
    from src.recent_operating import _FIELDS
    metrics = {k: 100.0 for k in _FIELDS.values()}
    metrics.update({"revenue_yoy": 5.0, "net_profit_yoy": 6.0, "deducted_profit_yoy": 7.0,
                    "gross_margin_change_pp": 0.0, "net_margin_change_pp": 0.0,
                    "operating_cashflow_change": 1.0, "operating_cashflow_yoy": 5.0})
    return {"code": str(code).zfill(6), "as_of": day, "status": "verified",
            "period": "2026-06-30", "available_date": "2026-08-28",
            "metrics": metrics, "missing_tags": [], "reasons": []}


def annual_for(code, day):
    """取该股在 day 之前最新可用年度（3 年），走与生产相同的年度质量评估。"""
    from src.fundamental_quality import evaluate_annual_quality
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
        # 决策日可用性：available_date <= day
        av = [x.get("available_date") for x in a if isinstance(x, dict)]
        av = [str(v)[:10] for v in av if v]
        if not av or max(av) > day:
            continue
        if best is None or max(ys) > best[0]:
            best = (max(ys), a)
    if best is None:
        return None
    return best[1]


def run_day(panels, ind, snap, day, cfg, use_industry=True):
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
            pb = float(s["pbMRQ"].iloc[-1])
        except Exception:
            pe = pb = None
        if pe is None or not np.isfinite(pe):
            c["4估值缺失(pending)"] += 1
            continue
        if pe <= 0:
            c["4 PE<=0"] += 1
            continue
        ctx = None
        if use_industry and ind.get(code) in snap:
            ctx = {"mode": "industry",
                   "pe_pct": float((snap[ind[code]] <= pe).sum() / len(snap[ind[code]])),
                   "pb_pct": None}
            if ctx["pe_pct"] > cfg.VALUATION_INDUSTRY_PERCENTILE_MAX:
                c["4行业PE分位>0.6"] += 1
                continue
            c["3通过PE(行业口径)"] += 1
        elif pe > cfg.MAX_PE_TTM:
            c["4 PE>25(绝对)"] += 1
            continue
        else:
            c["3通过PE(绝对口径)"] += 1

        ar = annual_for(code, day)
        if ar is None:
            c["5年度财务缺失(pending)"] += 1
            continue
        from src.fundamental_quality import evaluate_annual_quality
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

        fund = {"debt_ratio": 40., "roe": 2., "annual_rows": ar,
                "operating_trend": fake_operating(code, day)}
        try:
            sig, reason = evaluate_quality_value(s, code, "X", cfg, {"regime": "neutral"},
                                                 fund, latest_trade_date=day, val_context=ctx)
        except Exception:
            c["ERROR"] += 1
            continue
        if sig is None:
            c[f"6{reason}"] += 1
            continue
        # 与生产链路一致：只有 tier=="formal" 才进正式名单（weak 层等虽 PASS 但降级 pending）
        if sig.tier != "formal":
            c["6通过但降级pending"] += 1
            continue
        c["6通过全部闸门"] += 1
        w2 = s.tail(cfg.LOW_POSITION_LOOKBACK)
        lo = float(w2["low"].min())
        finals.append((code, sig.score, (close / lo - 1) * 100, sig.stabilization_level))
    return c, finals


def main():
    cfg = StrategyConfig()
    panels = load_panels()
    ind = json.load(open("backtest_cache/industry.json", encoding="utf-8"))

    # 行业 PE 快照（按日构建，避免用未来数据）
    days = ["2026-05-08", "2026-05-29", "2026-06-19", "2026-07-10",
            "2026-07-31", "2026-08-21", "2026-09-04", "2026-09-21"]
    print(f"配置：MAX_PICKS(牛)={cfg.MAX_PICKS} / 中性={cfg.NEUTRAL_MAX_PICKS} / 熊={cfg.BEAR_MAX_PICKS}")
    print(f"      QV_MAX_GAIN_FROM_250D_LOW={cfg.QV_MAX_GAIN_FROM_250D_LOW}  MIN_QV_SCORE={cfg.MIN_QV_SCORE}")
    print(f"      MIN_TECHNICAL_SCORE_FORMAL={cfg.MIN_TECHNICAL_SCORE_FORMAL}  MAX_ATR_PCT={cfg.MAX_ATR_PCT}\n")

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
        n_all = sum(v for k, v in c.items() if k.startswith("1"))
        print(f"{day}: 全通过 {len(finals)} 只 | "
              + " | ".join(f"{k}:{v}" for k, v in c.most_common(6)))
        if finals:
            gs = [f[2] for f in finals]
            print(f"        距低点涨幅 中位{np.median(gs):+.1f}% 最大{max(gs):+.1f}% | "
                  f"止跌层 {Counter(f[3] for f in finals).most_common()}")

    print("\n" + "=" * 74)
    counts = [len(f) for _, _, f in results]
    print(f"8 个决策日实际产出：{counts}")
    print(f"  中位数 {np.median(counts):.0f} 只/日   均值 {np.mean(counts):.1f} 只/日   "
          f"范围 {min(counts)}~{max(counts)}")
    print(f"  空仓天数：{sum(1 for c in counts if c == 0)}/8")
    print(f"  达到「每天 2~3 只」的天数：{sum(1 for c in counts if 2 <= c <= 3)}/8")
    print(f"  超过 MAX_PICKS={cfg.MAX_PICKS} 的天数：{sum(1 for c in counts if c > cfg.MAX_PICKS)}/8")

    print("\n瓶颈定位（各层平均通过数）：")
    agg = Counter()
    for _, c, _ in results:
        for k, v in c.items():
            agg[k] += v / len(results)
    prev = None
    for k, v in agg.most_common():
        print(f"  {k:<26} 平均 {v:6.1f} 只")


if __name__ == "__main__":
    main()
