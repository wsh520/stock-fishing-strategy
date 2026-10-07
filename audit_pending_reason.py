"""诊断：真实生产口径下「PASS 但降级 pending」的确切缺项分布。"""
import collections
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.bottom_fishing_strategy import StrategyConfig, has_halt_gap, evaluate_quality_value
from src.fundamental_quality import evaluate_annual_quality

DAY = "2026-09-21"


def _fake_operating(code, day):
    """构造 status=verified 的近期经营证据（字段齐备、指标非空即可通过 validated_operating_status）。"""
    from src.recent_operating import _FIELDS
    metrics = {k: 100.0 for k in _FIELDS.values()}
    metrics.update({"revenue_yoy": 5.0, "net_profit_yoy": 6.0, "deducted_profit_yoy": 7.0,
                    "gross_margin_change_pp": 0.0, "net_margin_change_pp": 0.0,
                    "operating_cashflow_change": 1.0, "operating_cashflow_yoy": 5.0})
    return {"code": str(code).zfill(6), "as_of": day, "status": "verified",
            "period": "2026-06-30", "available_date": "2026-08-28",
            "metrics": metrics, "missing_tags": [], "reasons": []}


def main():
    cfg = StrategyConfig()
    panels = {}
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
        panels[code] = d

    def annual_for(code, day):
        best = None
        for h in sorted(glob.glob(f"cache/annual_quality_v1_{code}_*.json")):
            try:
                j = json.load(open(h, encoding="utf-8"))
            except Exception:
                continue
            a = j.get("annual_rows") or []
            if isinstance(a, dict):
                a = [a]
            av = [str(x.get("available_date"))[:10] for x in a
                  if isinstance(x, dict) and x.get("available_date")]
            ys = [x.get("year") for x in a if isinstance(x, dict)]
            if not av or not ys or max(av) > day:
                continue
            if best is None or max(ys) > best[0]:
                best = (max(ys), a)
        return best[1] if best else None

    cnt = collections.Counter()
    samples = []
    for code, d in panels.items():
        s = d[d["date"] <= DAY].reset_index(drop=True)
        if len(s) < 250 or s["date"].iloc[-1] != DAY or has_halt_gap(s, cfg):
            continue
        if float(s["amount"].tail(20).mean()) < cfg.MIN_AMOUNT:
            continue
        w = s.tail(250)
        close = float(s["close"].iloc[-1])
        cw = w["close"].to_numpy(float)
        pos = float(((cw < close).sum() + .5 * (cw == close).sum()) / len(cw))
        if pos > cfg.LOW_POSITION_MAX:
            continue
        try:
            pe = float(s["peTTM"].iloc[-1])
        except Exception:
            continue
        if pe is None or not np.isfinite(pe) or pe <= 0:
            continue
        ar = annual_for(code, DAY)
        if ar is None:
            continue
        q = evaluate_annual_quality(
            ar, DAY, years=cfg.QUALITY_YEARS,
            median_roe_min=cfg.QUALITY_MEDIAN_ROE_MIN, min_roe=cfg.QUALITY_MIN_ROE,
            cash_conversion_min=cfg.QUALITY_CASH_CONVERSION_MIN, financial=False,
            require_annual_net_profit_positive=cfg.QUALITY_REQUIRE_ANNUAL_NET_PROFIT_POSITIVE,
            require_annual_cashflow_positive=cfg.QUALITY_REQUIRE_ANNUAL_CASHFLOW_POSITIVE)
        if q["status"] != "verified":
            continue
        # 合成「已核验」的近期经营证据：离线环境无法联网取新浪接口，
        # 而 REQUIRE_RECENT_OPERATING=True 时缺 operating_trend 会一律降级 pending。
        # 这里只验证**闸门链路与产出量级**，不代表真实基本面判断。
        fund = {"debt_ratio": 40., "roe": 2., "annual_rows": ar,
                "operating_trend": _fake_operating(code, DAY)}
        sig, reason = evaluate_quality_value(s, code, "X", cfg, {"regime": "neutral"},
                                            fund, latest_trade_date=DAY)
        if sig is None:
            continue
        cnt[(sig.tier, sig.stabilization_level, sig.missing_tags)] += 1
        if len(samples) < 8:
            samples.append((code, sig.tier, sig.stabilization_level,
                            sig.missing_tags, round(sig.score, 1), round(sig.daily_score, 1)))

    print("PASS 样本的 tier / 止跌层 / 缺项分布：")
    for (tier, stab, mt), v in cnt.most_common(12):
        print(f"  tier={tier:<8} 止跌={stab:<8} 缺项={mt or '(空)'!s:<46} {v} 只")
    print("\n样本明细：")
    for x in samples:
        print("  ", x)

    # 关键：technical_weak 是否是唯一卡点
    tw = sum(v for (t, s, mt), v in cnt.items() if "technical_weak" in (mt or ""))
    print(f"\n含 technical_weak（技术分 <45 降级）的样本：{tw} 只")
    print("→ 若这些样本技术分刚好在 45 附近，说明 MIN_TECHNICAL_SCORE_FORMAL 是主要卡点；"
          "\n  注意本轮技术分已连续化，取值分布更密，需重新审视该阈值")


if __name__ == "__main__":
    main()
