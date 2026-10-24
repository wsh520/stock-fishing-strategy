"""2026-10-24 P1（只读）：放宽 operating_weak 的前瞻证据实验。

背景：近期生产产出 0~2 只/天，影子账本 67/159 near-miss 中 operating_weak 是最大
单一阻断来源（10-24 运行 30 只待核验里 20 只含 operating_weak）。问题：若放宽
「近期经营走弱」闸门，放进来的票历史表现如何？本实验在唯一有 pit 基本面的
决策日 2026-06-04 上重放完整 quality_value 筛选（与生产同口径），把结果按
阻断原因分层，计算各组 F5/F20/F60，直接回答「放宽是否有据」。

方法（与 diag_pe_value / audit_live_trading 同口径）：
  1) 800 面板逐只前移评估日（EVAL_OFFSET=75），断言落点=2026-06-04；
  2) 第一轮 evaluate(fund_data=None) 走价格层筛（流动性/位置/PE/ATR/止跌/缺口）；
  3) 幸存者补当时已披露证据：年度财务取 annual_quality_v1 缓存（公告日<=决策日），
     近期经营走 fetch_operating_trend(code, 决策日)（as-of 过滤在模块内）；
  4) 第二轮 evaluate(fund_data) 完整重评 → formal / pending + missing_tags；
  5) 前瞻收益取 eval_i 之后真实行情（F5/F20/F60）。

铁律：指标只用 eval_i 及之前；前瞻取其后；EVAL_OFFSET=0 自检时前瞻必须全 NaN。
边界：单决策日、经营数据接口可用率约 70%（失败记 missing 不记 weak，不伪造）；
结论只作阈值论证素材，不直接改生产口径。
"""
import glob
import json
import os
import sys
import time
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.screening_engine import (  # noqa: E402
    StrategyConfig, evaluate, compute_market_environment, has_halt_gap,
)
from src.recent_operating import fetch_operating_trend  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
DAILY = os.path.join(ROOT, "backtest_cache", "daily")
FUND_DIR = os.path.join(ROOT, "backtest_cache", "fund")
EVAL_OFFSET = 75
HORIZONS = (5, 20, 60)
EXPECT_DAY = "2026-06-04"
OP_SLEEP = 0.25


def load_panels():
    out = {}
    for f in sorted(glob.glob(os.path.join(DAILY, "*.csv"))):
        code = os.path.splitext(os.path.basename(f))[0]
        try:
            d = pd.read_csv(f)
        except Exception:
            continue
        if "date" not in d.columns:
            continue
        d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        for c in ("open", "high", "low", "close", "volume", "amount", "peTTM", "pbMRQ"):
            if c in d.columns:
                d[c] = pd.to_numeric(d[c], errors="coerce")
        if not {"volume", "amount"} <= set(d.columns):
            continue
        d = d.sort_values("date").reset_index(drop=True)
        d = d[(d["volume"] > 0) & (d["amount"] > 0)].reset_index(drop=True)
        out[code] = d
    return out


def load_index():
    files = sorted(glob.glob(os.path.join(ROOT, "backtest_cache", "index_*.csv")))
    if not files:
        return None
    idx = pd.read_csv(files[-1])
    idx["date"] = pd.to_datetime(idx["date"]).dt.strftime("%Y-%m-%d")
    return idx.sort_values("date").reset_index(drop=True)


def annual_rows_from_cache(code, day):
    """年度财务只取已缓存、且公告日 <= 决策日的行（无缓存返回 None，不联网伪造）。"""
    rows_all = []
    for h in sorted(glob.glob(os.path.join(ROOT, "cache", f"annual_quality_v1_{code}_*.json"))):
        try:
            j = json.load(open(h, encoding="utf-8"))
        except Exception:
            continue
        for r in j.get("annual_rows", []):
            avail = str(r.get("available_date") or "")[:10]
            if avail and avail <= day:
                rows_all.append(r)
    if not rows_all:
        return None
    best = {}
    for r in rows_all:
        key = str(r.get("report_date") or r.get("year"))
        prev = best.get(key)
        if prev is None or str(r.get("available_date") or "") > str(prev.get("available_date") or ""):
            best[key] = r
    return sorted(best.values(), key=lambda r: str(r.get("report_date") or ""))


def pit_fund(code, day):
    p = os.path.join(FUND_DIR, f"pit_v2_{code}_{day}.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return None
    return None


def fwd_return(full, eval_i, n):
    j = eval_i + n
    if j >= len(full):
        return None
    c0 = float(full["close"].iloc[eval_i])
    return float(full["close"].iloc[j] / c0 - 1) * 100.0


def main():
    cfg = StrategyConfig()
    panels = load_panels()
    index = load_index()
    print(f"面板 {len(panels)} 只｜指数 {len(index) if index is not None else 0} 根")

    survivors = []
    day_mismatch = 0
    for code, full in panels.items():
        eval_i = len(full) - 1 - EVAL_OFFSET
        if eval_i < max(cfg.LOW_POSITION_LOOKBACK, cfg.MIN_DAYS):
            continue
        day = str(full["date"].iloc[eval_i])
        if day != EXPECT_DAY:
            day_mismatch += 1
            continue
        daily = full.iloc[: eval_i + 1].reset_index(drop=True)
        index_upto = index[index["date"] <= day].reset_index(drop=True) if index is not None else None
        market_env = compute_market_environment(index_upto, cfg) if index_upto is not None and not index_upto.empty else {"regime": "unknown"}
        try:
            sig, reason = evaluate(daily, code, "", cfg, market_env, None,
                                   latest_trade_date=day, index_df=index_upto)
        except Exception as exc:  # noqa: BLE001
            print(f"  evaluate 异常 {code}: {exc}")
            continue
        if reason == "PASS" and sig is not None:
            survivors.append(dict(code=code, daily=daily, full=full, eval_i=eval_i,
                                  day=day, market_env=market_env, index_upto=index_upto))
    print(f"评估日落点 {EXPECT_DAY}（跳过日期不符 {day_mismatch} 只）｜价格层幸存 {len(survivors)} 只")
    regime = Counter(s["market_env"].get("regime", "unknown") for s in survivors)
    print(f"市场环境分布: {dict(regime)}")

    # ---- 补当时已披露证据并完整重评 ----
    rows = []
    op_fetch = 0
    op_fail = 0
    for i, s in enumerate(survivors):
        code = s["code"]
        fund = pit_fund(code, s["day"]) or {}
        annual = annual_rows_from_cache(code, s["day"])
        if annual is not None:
            fund = dict(fund, annual_rows=annual)
        try:
            op = fetch_operating_trend(code, s["day"])
            op_fetch += 1
            fund = dict(fund, operating_trend=op)
        except Exception:  # noqa: BLE001
            op_fail += 1
        time.sleep(OP_SLEEP)
        try:
            sig, reason = evaluate(s["daily"], code, "", cfg, s["market_env"], fund,
                                   latest_trade_date=s["day"], index_df=s["index_upto"])
        except Exception as exc:  # noqa: BLE001
            print(f"  重评异常 {code}: {exc}")
            continue
        row = dict(code=code, reason=reason, day=s["day"],
                   regime=s["market_env"].get("regime", "unknown"))
        if sig is not None:
            d = sig.to_dict()
            row["tier"] = d.get("tier")
            row["tags"] = str(d.get("missing_tags") or "")
            row["score"] = d.get("score")
            row["stabilization_level"] = d.get("stabilization_level")
            row["operating_status"] = d.get("operating_status")
        row["op_status"] = (fund.get("operating_trend") or {}).get("status")
        row["op_metrics_ni_yoy"] = ((fund.get("operating_trend") or {}).get("metrics") or {}).get("net_profit_yoy")
        row["annual_rows"] = len(fund.get("annual_rows") or [])
        for n in HORIZONS:
            row[f"F{n}"] = fwd_return(s["full"], s["eval_i"], n)
        rows.append(row)
        if (i + 1) % 40 == 0:
            print(f"  重评进度 {i + 1}/{len(survivors)}")

    print(f"经营数据拉取 {op_fetch} 次（异常 {op_fail}）")
    df = pd.DataFrame(rows)
    out = os.path.join(ROOT, "data", "diag_operating_relax_20260604.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"明细写出: {out}（{len(df)} 行）")

    # ---- 自检：EVAL_OFFSET=0 时前瞻必须全 NaN（铁律回归）----
    zero_ok = all(fwd_return(s["full"], len(s["full"]) - 1, n) is None for s in survivors[:5] for n in HORIZONS)
    print(f"铁律自检（EVAL_OFFSET=0 前瞻应全 None）: {'通过' if zero_ok else '失败!'}")

    # ---- 分层汇总 ----
    def block(tags):
        return tuple(t for t in str(tags).split(",") if t)

    def show(label, sub):
        if sub.empty:
            print(f"  {label:<34} n=0")
            return
        bits = []
        for n in HORIZONS:
            v = sub[f"F{n}"].dropna()
            bits.append(f"F{n}: n={len(v):>3} 中位 {v.median():+6.2f}% 均值 {v.mean():+6.2f}% 胜率 {(v > 0).mean() * 100:4.0f}%" if len(v) else f"F{n}: 无")
        print(f"  {label:<34} n={len(sub):<4} " + " | ".join(bits))

    print("\n" + "=" * 100)
    print("【1】完整重评结果分层（价格层幸存 → 加基本面证据后的终局）")
    print("=" * 100)
    formal = df[(df["reason"] == "PASS") & (df.get("tier") == "formal")]
    show("formal（生产会推送的）", formal)
    pend = df[df["reason"] != "PASS"]
    show("pending·仅 operating_weak（放宽目标）", pend[pend["tags"].apply(lambda t: block(t) == ("operating_weak",))])
    show("pending·仅 stabilization_weak", pend[pend["tags"].apply(lambda t: block(t) == ("stabilization_weak",))])
    show("pending·仅 technical_weak", pend[pend["tags"].apply(lambda t: block(t) == ("technical_weak",))])
    show("pending·仅 recent_bottom_unconfirmed", pend[pend["tags"].apply(lambda t: block(t) == ("recent_bottom_unconfirmed",))])
    show("pending·多阻塞(≥2)", pend[pend["tags"].apply(lambda t: len(block(t)) >= 2)])
    show("pending·全部", pend)

    print("\n" + "=" * 100)
    print("【2】横截面检验：经营证据状态 vs 前瞻收益（价格层幸存全体，不受终局分层影响）")
    print("=" * 100)
    print(f"  经营状态分布: {dict(Counter(df['op_status']))}")
    for status in ("verified", "weak"):
        show(f"operating={status}", df[df["op_status"] == status])
    show("operating=missing（无证据，生产中亦 pending）", df[df["op_status"] == "missing"])

    print("\n" + "=" * 100)
    print("【3】若放宽 operating_weak：把『仅 operating_weak 被挡』的票并入后的组合表现")
    print("=" * 100)
    relaxed = pd.concat([formal, pend[pend["tags"].apply(lambda t: block(t) == ("operating_weak",))]])
    show("formal + 仅 operating_weak", relaxed)
    print(f"  formal 单独: n={len(formal)}；放宽后增量: +{len(relaxed) - len(formal)} 只")

    print("\n【4】否决原因计数（完整重评）")
    for k, v in Counter(df["reason"]).most_common(15):
        print(f"  {k:<34} {v}")


if __name__ == "__main__":
    main()
