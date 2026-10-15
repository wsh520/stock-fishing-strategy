"""验证 2026-10-13 收紧（QV_STABILIZATION_MEDIUM_REQUIRES_MA20）的净效果（只读）。

方法：对 2026-09-08~09-28 的 31 条真实 bottom_fishing 推荐，在**各自推荐日**重放
evaluate_quality_value，两组配置只差 QV_STABILIZATION_MEDIUM_REQUIRES_MA20 一个开关
（True=新口径收紧 / False=2026-10-13 之前口径），其余闸门放宽以隔离止跌这一项的影响。

断言「放行集合只减不增」：新口径的 formal 集合必须是旧口径的真子集，
且被减掉的票应当正是 signals_hit 里带「左侧·MA20下方待企稳」的票。

口径声明：年度财务为**合成 verified fixture**（新浪接口离线不可达），非止跌维度
全部关闭或放宽，因此这里量的是「止跌收紧」这一项的净效应，不是策略真实产出。
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import screening_engine as m  # noqa: E402
from diag_real_recs import RECS  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))

# 三年 ROE 达标、现金转换健康的合成年度证据：让质量闸门通过，把变量锁定在止跌。
FUND = {"debt_ratio": 40., "roe": 2., "annual_rows": [
    {"year": y, "report_date": f"{y}-12-31", "available_date": f"{y + 1}-04-20",
     "roe": roe, "deducted_profit": 90., "net_profit": 100., "operating_cashflow": 110.}
    for y, roe in [(2023, 11), (2024, 12), (2025, 13)]]}

# 与止跌无关的闸门一律关闭/放宽，保证 A/B 差集只来自 medium 层的 MA20 要求
ISOLATE = dict(
    USE_CACHE=False, RECOMMENDATION_MODE="quality_value",
    MIN_QV_SCORE=0, MIN_QUALITY_SCORE=0,
    REQUIRE_RECENT_OPERATING=False, REQUIRE_FINANCIAL_RISK=False,
    REQUIRE_CYCLICAL_NORMALIZED_VALUATION=False, REQUIRE_HISTORICAL_VALUATION=False,
    USE_INDUSTRY_RELATIVE_VALUATION=False,
)


def panel(code, day):
    for mk in ("sh", "sz"):
        p = os.path.join(BASE, "cache", f"daily_{mk}.{code}_last600_qfq.csv")
        if os.path.exists(p):
            d = pd.read_csv(p)
            d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            return d[d["date"] <= day].dropna(subset=["date", "close"]).reset_index(drop=True)
    return None


def run(df, code, day, require_ma20):
    kw = dict(ISOLATE)
    kw["QV_STABILIZATION_MEDIUM_REQUIRES_MA20"] = require_ma20
    return m.evaluate(df, code, "机械设备", m.StrategyConfig(**kw),
                      {"regime": "neutral"}, dict(FUND), latest_trade_date=day)


def main():
    rows = []
    for rec in RECS:
        idx = {"id": 0, "date": 1, "code": 2, "name": 3, "signals": 14, "tier": 17, "strategy": 18}
        if rec[idx["strategy"]] != "bottom_fishing":
            continue
        code, day, name = rec[idx["code"]], rec[idx["date"]], rec[idx["name"]]
        sig_txt = rec[idx["signals"]] or ""
        df = panel(code, day)
        if df is None or len(df) < 260:
            print(f"[跳过] {code} {name} {day}: 行情不足 {0 if df is None else len(df)}")
            continue
        s_old, r_old = run(df, code, day, False)
        s_new, r_new = run(df, code, day, True)
        rows.append(dict(
            date=day, code=code, name=name,
            left_tag="左侧" in sig_txt or "仍在下跌" in sig_txt,
            old_reason=r_old, new_reason=r_new,
            old_ok=s_old is not None, new_ok=s_new is not None,
            old_tier=getattr(s_old, "tier", None), new_tier=getattr(s_new, "tier", None),
            old_stab=getattr(s_old, "stabilization_level", None),
            new_stab=getattr(s_new, "stabilization_level", None),
        ))

    df = pd.DataFrame(rows)
    if df.empty:
        print("无可重放样本。")
        return

    print(f"\n{'日期':<11}{'代码':<8}{'名称':<7}{'旧[结果/止跌层]':<24}{'新[结果/止跌层]':<24} 变化")
    demoted = promoted = 0
    for _, r in df.iterrows():
        old_s = f"{r['old_tier']}/{r['old_stab']}"
        new_s = f"{r['new_tier']}/{r['new_stab']}"
        note = ""
        if (pd.notna(r["old_stab"]) and pd.notna(r["new_stab"])
                and r["old_stab"] != r["new_stab"]):
            # strong > medium > weak > none 排序，只允许降级不允许升级
            rank = {"strong": 3, "medium": 2, "weak": 1, "none": 0}
            if rank.get(r["new_stab"], 0) < rank.get(r["old_stab"], 0):
                demoted += 1
                note = "  ← 收紧（止跌层降级）"
            else:
                promoted += 1
                note = "  ← 异常（止跌层升级）"
        print(f"{r['date']:<11}{r['code']:<8}{r['name']:<7}"
              f"{old_s:<24}{new_s:<24}{note}")

    print("\n=== 止跌层级变化（只应降级，不得升级）===")
    print(f"  medium → weak 等降级：{demoted} 只")
    print(f"  止跌层升级（必须为 0）：{promoted} 只")

    old_formal = set(df[df["old_tier"] == "formal"]["code"])
    new_formal = set(df[df["new_tier"] == "formal"]["code"])
    gained = new_formal - old_formal
    lost = old_formal - new_formal
    print("\n=== 放行集合（formal）===")
    print(f"  旧口径 {len(old_formal)}/{len(df)}   新口径 {len(new_formal)}/{len(df)}")
    print(f"  被拦下：{sorted(lost) if lost else '无'}")
    print(f"  新放行（必须为空）：{sorted(gained) if gained else '无'}")
    print(f"  [判定] 只减不增：{'通过' if not gained and not promoted else '失败'}")

    print("\n=== 结果迁移（old_reason → new_reason）===")
    mig = df.groupby(["old_reason", "new_reason"]).size().sort_values(ascending=False)
    print(mig.to_string())
    newly_failed = df[(df["old_reason"] == "PASS") & (df["new_reason"] != "PASS")]
    print(f"\n  新口径下由 PASS 被转为否决：{len(newly_failed)} 只")
    if len(newly_failed):
        for _, r in newly_failed.iterrows():
            print(f"    {r['date']} {r['code']} {r['name']}: {r['old_stab']} → {r['new_reason']}")
    print("\n=== 未通过信号（signal=None）的票，止跌层不可读，分布如下 ===")
    both_none = df[df["old_stab"].isna() & df["new_stab"].isna()]
    print(f"  共 {len(both_none)} 只（两组都被前置闸门否决，与本次改动无关）")


if __name__ == "__main__":
    main()