"""2026-10-24 P1（只读）：重复推荐冷却期敏感性 + 压制事件前瞻证据。

背景：10 日冷却近期三次把「资格通过」压成 0 正式（#113/#116），#118 也压掉 4/5。
问题：冷却期该不该缩短？本实验用三个视角回答：

  1) 压制时年龄：每次 cooldown_blocked 事件，股票距上次正式推荐几个交易日。
     年龄小（1~2 天）= 连续资格（churn 风险，10-13 新增冷却的初衷）；
     年龄大（7+ 天）= 一周前推过、现在又够格（可能是被过度拦截的真机会）。
  2) 压制后走势：被压制的股票在随后 1/2/3/5 个交易日真实涨跌——
     跌 ⇒ 冷却保护了我们（不该缩短）；涨 ⇒ 冷却让我们错失交付（该缩短）。
  3) 反事实：若窗口 = 5 个交易日，各 run 正式推荐数会变成多少。

数据：GitHub Actions 日志时间线（/tmp/gh_logs/cooldown_manifest.json，
只含 signal_date 已知且实际落库的 run）+ 本地实时日线（前瞻铁律：
指标只用压制日及之前，前瞻取其后真实行情）。

只读诊断，不碰仓库、不改生产配置；结论先作阈值论证素材。
"""
import json
import os
import sys
from collections import defaultdict

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.screening_engine import (  # noqa: E402
    StrategyConfig, get_daily_data, get_index_daily,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
MANIFEST = "/tmp/gh_logs/cooldown_manifest.json"
OUT_DIR = os.path.join(ROOT, "data")
OUT_CSV = os.path.join(OUT_DIR, "diag_cooldown_forward_20261024.csv")
HORIZONS = (1, 2, 3, 5)


def approx_signal_date(created_at: str) -> str:
    """无「行情日期」字段的旧 run：created_at(UTC)+8h 再减 1 天 ≈ 信号日。
    已对全部有 signal_date 的 run 校验（见 main 内断言）。"""
    t = pd.Timestamp(created_at) + pd.Timedelta(hours=8) - pd.Timedelta(days=1)
    return t.strftime("%Y-%m-%d")


def main():
    manifest = json.load(open(MANIFEST, encoding="utf-8"))

    # ---- 1. 形式推荐时间线（只算实际落库/用户可见的 run）----
    # rec_date 语义 = 数据截止日（行情日期，旧 run 回落快照日）；DB 去重后同代码
    # 在有效期内只留 1 行，但用户侧每次都收到了推送（notify 不依赖 DB），
    # 所以时间线按「用户可见的推荐」计（含 #101 马应龙：DB 落库失败但仍推送了）。
    for r in manifest:
        if r.get("signal_date") and r["event"] == "schedule":
            assert approx_signal_date(r["created_at"]) == r["signal_date"], (
                f"近似失效: #{r['run']} {r['created_at']} vs {r['signal_date']}")
    timeline = []  # (rec_date, code, name, run)
    skipped = []
    for r in manifest:
        if r.get("no_save"):
            continue  # 测试模式没落库，不进冷却口径
        day = r.get("rec_date")
        if day is None:
            skipped.append(r["run"])
            continue
        for f in r["formal"]:
            timeline.append((day, f["code"], f["name"], r["run"]))
    if skipped:
        print(f"跳过无数据日的 run: {skipped}")

    # ---- 2. 拉日线（8 只相关票 + 交易日历）----
    codes = sorted({c for _, c, _, _ in timeline} |
                   {c["code"] for r in manifest for c in r["cooldown_blocked"]})
    print(f"涉及 {len(codes)} 只代码，拉取日线…")
    cfg = StrategyConfig()
    frames = {}
    for code in codes:
        df = get_daily_data(code, cfg)
        if df is None or df.empty:
            print(f"  {code}: 无数据")
            continue
        d = df.copy()
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        d = d.sort_values("date").reset_index(drop=True)
        frames[code] = d
    assert frames, "一只都没拉到"
    cal = sorted(set().union(*[set(d["date"]) for d in frames.values()]))
    cal = [d for d in cal if d >= "2026-09-01"]
    print(f"交易日历 {cal[0]}..{cal[-1]}，共 {len(cal)} 天")

    # 大盘基准（沪深300）：区分「冷却保护了我们」与「大盘 beta」
    idx = get_index_daily(cfg)
    idx_close = {}
    if idx is not None and not idx.empty:
        d = idx.copy()
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        idx_close = dict(zip(d["date"], d["close"]))
        print(f"基准沪深300 {min(idx_close)}..{max(idx_close)}")

    def fwd(code_or_idx, signal, h):
        """从 signal 日收盘起 h 个交易日的涨跌%；code_or_idx=None 用指数。"""
        if signal not in cal:
            return None
        j = cal.index(signal) + h
        if j >= len(cal):
            return None
        base = close_on(code_or_idx, signal) if code_or_idx else idx_close.get(signal)
        px = close_on(code_or_idx, cal[j]) if code_or_idx else idx_close.get(cal[j])
        return round((px / base - 1) * 100, 2) if (px and base) else None

    def close_on(code, day):
        d = frames.get(code)
        if d is None:
            return None
        row = d[d["date"] == day]
        return float(row["close"].iloc[0]) if len(row) else None

    def age_days(last_rec, signal):
        """(last_rec, signal] 区间内的交易日数；last_rec 不在日历内按最近的前交易日计。"""
        if last_rec is None:
            return None
        days = [d for d in cal if last_rec < d <= signal]
        return len(days)

    # ---- 3. 逐冷却压制事件：年龄 + 前瞻 ----
    rows = []
    for r in manifest:
        signal = r.get("signal_date")
        if not signal:
            continue
        for c in r["cooldown_blocked"]:
            code = c["code"]
            prior = [(d, run) for d, cc, _, run in timeline if cc == code and d < signal]
            last_rec, last_run = (max(prior) if prior else (None, None))
            age = age_days(last_rec, signal)
            # 前瞻：以压制日（信号日）收盘为基准（与 06-04 实验同口径）
            base = close_on(code, signal)
            rec = {"run": r["run"], "signal": signal, "code": code, "name": c["name"],
                   "last_rec": last_rec, "last_run": last_run, "age_td": age,
                   "base_close": base}
            for h in HORIZONS:
                fut = cal[cal.index(signal) + h] if cal.index(signal) + h < len(cal) else None
                if fut is None:
                    rec[f"F{h}"] = None
                    continue
                px = close_on(code, fut)
                rec[f"F{h}"] = round((px / base - 1) * 100, 2) if (px and base) else None
            rows.append(rec)

    # ---- 4. 汇总 ----
    print("\n===== 冷却压制事件明细（年龄 + 前瞻%） =====")
    print(f"{'run':>5} {'信号日':>10} {'代码':>7} {'名称':<6} {'上次推荐':>10} {'年龄':>3} "
          f"{'F1':>7} {'F2':>7} {'F3':>7} {'F5':>7}")
    for x in rows:
        fmt = lambda v: f"{v:7.2f}" if v is not None else "    n/a"
        print(f"{x['run']:>5} {x['signal']:>10} {x['code']:>7} {x['name']:<6} "
              f"{str(x['last_rec']):>10} {str(x['age_td']):>3} "
              f"{fmt(x['F1'])} {fmt(x['F2'])} {fmt(x['F3'])} {fmt(x['F5'])}")

    print("\n===== 年龄分布 =====")
    ages = defaultdict(int)
    for x in rows:
        ages[x["age_td"]] += 1
    for a in sorted(ages, key=lambda v: (v is None, v)):
        print(f"  年龄 {a} 交易日: {ages[a]} 次")

    # ---- 5. 反事实：5 交易日窗口 ----
    print("\n===== 反事实：窗口 10→5 交易日（年龄<=5 仍拦） =====")
    by_run = defaultdict(list)
    for x in rows:
        by_run[x["run"]].append(x)
    total_actual = total_cf = 0
    for r in manifest:
        signal = r.get("signal_date")
        if not signal or not r["cooldown_blocked"]:
            continue
        blocked = by_run[r["run"]]
        cf_release = [x for x in blocked if x["age_td"] is None or x["age_td"] > 5]
        still = [x for x in blocked if x["age_td"] is not None and x["age_td"] <= 5]
        actual = len(r["formal"])
        cf = actual + len(cf_release)
        total_actual += actual
        total_cf += cf
        print(f"  #{r['run']} {signal}: 实际正式 {actual}；5 日窗口放行 "
              f"{[x['name'] for x in cf_release]}（年龄 {[x['age_td'] for x in cf_release]}）"
              f" → 反事实 {cf}；仍拦 {[x['name'] for x in still]}")
    print(f"  三次 run 合计：实际 {total_actual} → 反事实 {total_cf}")

    # ---- 6. 前瞻汇总：按「5 日窗口是否放行」分组 ----
    print("\n===== 压制后走势分组（可用前瞻窗口；基准=同期沪深300） =====")
    for h in (1, 2, 3):
        iv = [fwd(None, x["signal"], h) for x in rows]
        iv = [v for v in iv if v is not None]
        if iv:
            print(f"  基准 F{h}: n={len(iv)} 均值={sum(iv)/len(iv):+.2f}%")
    for label, sel in (("仍拦(年龄<=5)", lambda x: x["age_td"] is not None and x["age_td"] <= 5),
                       ("放行(年龄>5)", lambda x: x["age_td"] is None or x["age_td"] > 5)):
        sub = [x for x in rows if sel(x)]
        for h in (1, 2, 3):
            vals = [x[f"F{h}"] for x in sub if x[f"F{h}"] is not None]
            if vals:
                print(f"  {label} F{h}: n={len(vals)} 均值={sum(vals)/len(vals):+.2f}% "
                      f"中位={sorted(vals)[len(vals)//2]:+.2f}% 区间[{min(vals):+.2f},{max(vals):+.2f}]")

    # ---- 6b. 正式推荐同期前瞻（被交付的 vs 被压制的）----
    print("\n===== 正式推荐前瞻（对照：被交付的股票后来怎样） =====")
    for day, cc, nn, run in sorted(timeline):
        if day < "2026-10-08":
            continue
        f1, f2, f3, f5 = (fwd(cc, day, h) for h in (1, 2, 3, 5))
        fmt = lambda v: f"{v:+7.2f}" if v is not None else "    n/a"
        print(f"  #{run} {day} {cc} {nn:<6} F1{fmt(f1)} F2{fmt(f2)} F3{fmt(f3)} F5{fmt(f5)}")

    # ---- 7. 落盘明细 ----
    os.makedirs(OUT_DIR, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n明细写出 {OUT_CSV}")

    # ---- 8. 附：无冷却时代的历史重复推荐（churn 证据）----
    print("\n===== 附：同一代码相邻两次正式推荐的间隔（交易日） =====")
    per_code = defaultdict(list)
    for d, cc, nn, run in timeline:
        per_code[cc].append((d, nn, run))
    for cc, lst in sorted(per_code.items()):
        lst.sort()
        if len(lst) > 1:
            gaps = [age_days(lst[i][0], lst[i + 1][0]) for i in range(len(lst) - 1)]
            print(f"  {cc} {lst[0][1]}: {[d for d, _, _ in lst]} 间隔={gaps}")


if __name__ == "__main__":
    main()
