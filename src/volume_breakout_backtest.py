"""Standalone event driven backtest for the volume breakout strategy.

Input files are daily OHLCV CSVs.  A signal is evaluated on the close of day t
and can only be entered at the next available day's open, which avoids
look-ahead.  The module is intentionally independent of the network/data
fetching pipeline and can therefore be run on exported historical data.
Only explicitly formal signals become events. OHLCV-only CSVs lack multi-year
financial evidence, so the default quality mode produces zero trades; financial
history is never fabricated or substituted with today's data.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd

from src.volume_breakout_strategy import VolumeBreakoutConfig, evaluate_breakout


_ALIASES = {"日期": "date", "交易日期": "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount",
            "涨跌幅": "pct_chg", "代码": "code", "名称": "name"}


@dataclass
class BacktestConfig:
    initial_capital: float = 100000.0
    position_size_pct: float = 0.20
    max_positions: int = 5
    fee_rate: float = 0.0003
    stamp_duty: float = 0.0005
    slippage_bps: float = 10.0
    max_holding_days: int = 20
    regime: str = "bull"


def _load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.rename(columns={c: _ALIASES.get(str(c), str(c).strip().lower()) for c in df.columns})
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    for c in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # Fill missing cells as well as missing columns so partially populated
    # exports remain deterministic under the strategy's fail-closed checks.
    derived_amount = df["close"] * df["volume"]
    if "amount" not in df:
        df["amount"] = derived_amount
    else:
        df["amount"] = df["amount"].fillna(derived_amount)
    derived_pct = df["close"].pct_change() * 100
    if "pct_chg" not in df:
        df["pct_chg"] = derived_pct
    else:
        df["pct_chg"] = df["pct_chg"].fillna(derived_pct)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close", "volume"]).sort_values("date")
    return df.reset_index(drop=True)


def load_price_files(input_path: str) -> Dict[str, pd.DataFrame]:
    p = Path(input_path)
    single = p.is_file()
    files = [p] if single else sorted(p.glob("*.csv"))
    if not files:
        raise ValueError(f"No CSV files found under {p}")
    out: Dict[str, pd.DataFrame] = {}
    for f in files:
        try:
            df = _load_csv(f)
        except Exception:
            # 预取会为「无数据标的」落一个只含 date 列的空标记文件；
            # 目录级加载时应跳过它，而不是让整个回测失败。显式指定单文件时仍然报错。
            if single:
                raise
            continue
        code = str(df["code"].iloc[0]) if "code" in df and pd.notna(df["code"].iloc[0]) else f.stem
        out[code] = df
    return out


def _candidate_events(data: Dict[str, pd.DataFrame], cfg: VolumeBreakoutConfig, regime: str):
    events = []
    for code, df in data.items():
        if len(df) < cfg.MIN_DAYS + 1:
            continue
        name = str(df["name"].iloc[0]) if "name" in df else ""
        # Compute each day using data available through that close only.
        for i in range(cfg.MIN_DAYS - 1, len(df) - 1):
            hist = df.iloc[: i + 1]
            sig, reason = evaluate_breakout(hist, code=code, name=name,
                                             config=cfg, market_env={"regime": regime},
                                             latest_trade_date=pd.Timestamp(df.iloc[i]["date"]).strftime("%Y-%m-%d"))
            if reason == "PASS" and sig is not None and getattr(sig, "tier", None) == "formal":
                events.append({"code": code, "name": name, "signal_date": df.iloc[i]["date"],
                               "entry_date": df.iloc[i + 1]["date"], "entry_index": i + 1,
                               "signal": sig, "df": df})
    return events


def run_backtest(data: Dict[str, pd.DataFrame], cfg: Optional[BacktestConfig] = None,
                 strategy_cfg: Optional[VolumeBreakoutConfig] = None):
    cfg = cfg or BacktestConfig()
    strategy_cfg = strategy_cfg or VolumeBreakoutConfig()
    events = sorted(_candidate_events(data, strategy_cfg, cfg.regime),
                    key=lambda x: (x["entry_date"], -float(getattr(x["signal"], "rank_score", 0.0)),
                                   str(x["code"])))
    by_date: Dict[pd.Timestamp, list] = {}
    for e in events:
        by_date.setdefault(pd.Timestamp(e["entry_date"]), []).append(e)
    bars = {code: df.set_index("date") for code, df in data.items()}
    all_dates = sorted(set(d for b in bars.values() for d in b.index))
    cash = float(cfg.initial_capital)
    active: List[dict] = []
    trades: List[dict] = []
    slip = cfg.slippage_bps / 10000.0

    for date in all_dates:
        # Exit positions before processing today's new entries.
        for pos in list(active):
            bar = bars[pos["code"]].loc[date] if date in bars[pos["code"]].index else None
            if bar is None:
                continue
            pos["days"] += 1
            stop_hit = float(bar["low"]) <= pos["stop"]
            target_hit = float(bar["high"]) >= pos["target"]
            timed_out = pos["days"] >= cfg.max_holding_days
            if not (stop_hit or target_hit or timed_out):
                continue
            # Conservative assumption when both barriers print in one bar: stop first.
            if stop_hit:
                # Gaps through the stop are filled at the open; otherwise at the stop level.
                reason, raw_exit = "stop", min(pos["stop"], float(bar["open"]))
            elif target_hit:
                # A gap above target is filled at the open, otherwise at target.
                reason, raw_exit = "take_profit", max(pos["target"], float(bar["open"]))
            else:
                reason, raw_exit = "time", float(bar["close"])
            exit_price = raw_exit * (1 - slip)
            proceeds = pos["shares"] * exit_price
            sell_fee = proceeds * (cfg.fee_rate + cfg.stamp_duty)
            cash += proceeds - sell_fee
            pnl = proceeds - sell_fee - pos["cost"]
            trades.append({"code": pos["code"], "signal_date": pos["signal_date"].strftime("%Y-%m-%d"),
                           "entry_date": pos["entry_date"].strftime("%Y-%m-%d"),
                           "exit_date": date.strftime("%Y-%m-%d"), "entry": pos["entry"],
                           "exit": round(exit_price, 4), "shares": pos["shares"],
                           "pnl": round(pnl, 2), "return_pct": round(pnl / pos["cost"] * 100, 3),
                           "reason": reason, "score": pos["score"]})
            active.remove(pos)

        for e in by_date.get(pd.Timestamp(date), []):
            if any(p["code"] == e["code"] for p in active) or len(active) >= cfg.max_positions:
                continue
            bar = bars[e["code"]].loc[date]
            raw_entry = float(bar["open"])
            if raw_entry <= 0:
                continue
            entry = raw_entry * (1 + slip)
            alloc = min(cash * cfg.position_size_pct, cash)
            shares = int(alloc / entry)
            if shares <= 0:
                continue
            buy_value = shares * entry
            buy_fee = buy_value * cfg.fee_rate
            if buy_value + buy_fee > cash:
                continue
            cash -= buy_value + buy_fee
            sig = e["signal"]
            # Rebase the signal's close-based plan to the executable next-open price.
            stop = max(float(sig.stop_loss), entry * (1 - strategy_cfg.FIXED_STOP_LOSS_PCT_BREAKOUT / 100))
            # A next-open gap can move above the close-based plan; keep the
            # stop strictly below the executable entry to avoid instant fills.
            stop = min(stop, entry * (1 - 1e-6))
            target = entry * (1 + strategy_cfg.FIXED_TAKE_PROFIT_PCT_BREAKOUT / 100)
            active.append({"code": e["code"], "signal_date": pd.Timestamp(e["signal_date"]),
                          "entry_date": pd.Timestamp(date), "entry": entry, "shares": shares,
                          "cost": buy_value + buy_fee, "stop": stop, "target": target,
                          "days": 0, "score": float(sig.score)})

    # Mark still-open positions at the last available close; they are not counted as closed wins.
    for pos in active:
        last = bars[pos["code"]].iloc[-1]
        mark = pos["shares"] * float(last["close"])
        cash += mark * (1 - cfg.fee_rate - cfg.stamp_duty)
    pnl_total = cash - cfg.initial_capital
    wins = sum(t["pnl"] > 0 for t in trades)
    summary = {"initial_capital": cfg.initial_capital, "final_equity": round(cash, 2),
               "pnl": round(pnl_total, 2), "return_pct": round(pnl_total / cfg.initial_capital * 100, 3),
               "trades": len(trades), "closed_wins": wins,
               "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
               "open_positions": len(active), "signals": len(events)}
    return summary, trades


# ===========================================================================
# KDJ / MACD 闸门 A/B 与漏斗诊断
#
# 动机：`backtest.py ab` 的重放走的是抄底策略 evaluate()（quality_value / technical
# 两个分支都指向 bottom_fishing_strategy），因此**无法**验证本策略层 3.8 的
# REQUIRE_BR_KDJ_NOT_HIGH / REQUIRE_BR_MACD_NOT_WEAK 两个闸门。本模块是突破策略
# 唯一的本地回测入口，故把闸门 A/B 与漏斗归因放在此处。
#
# 重要前提：只有 `RECOMMENDATION_MODE == "technical"` 时层 3.8 才会执行。默认
# quality_value 下 evaluate_breakout 会先委派给 evaluate_quality_value（需要多年
# 财务数据），OHLCV 缓存面板必然在层 1 失败 → 恒 0 信号。因此 A/B 强制 technical。
# ===========================================================================

#: 闸门变体表：键为变体名，值为叠加到 VolumeBreakoutConfig 上的覆盖字段。
GATE_VARIANTS: Dict[str, Dict[str, object]] = {
    "gates_on": {},                                                       # 生产默认（两个闸门都开）
    "gates_off": {"REQUIRE_BR_MACD_NOT_WEAK": False,
                  "REQUIRE_BR_KDJ_NOT_HIGH": False},                      # 回到 P0-1 之前
    "kdj_off": {"REQUIRE_BR_KDJ_NOT_HIGH": False},                       # 单独关 KDJ 闸门
    "macd_off": {"REQUIRE_BR_MACD_NOT_WEAK": False},                     # 单独关 MACD 闸门
}


def _technical_cfg(overrides: Optional[Dict[str, object]] = None) -> VolumeBreakoutConfig:
    """构造 technical 模式的突破配置（A/B 所有变体共用此基底）。"""
    base = VolumeBreakoutConfig(RECOMMENDATION_MODE="technical")
    return VolumeBreakoutConfig(**{**asdict(base), **(overrides or {})})


def funnel_counts(data: Dict[str, pd.DataFrame], regime: str = "bull",
                  tail_bars: int = 250, cfg: Optional[VolumeBreakoutConfig] = None
                  ) -> "Counter":
    """统计 evaluate_breakout 的否决原因分布，用于定位「0 信号」卡在哪一层。

    只取每只面板尾部 `tail_bars` 根（保留 MIN_DAYS 预热），逐日评估。返回 Counter。
    """
    cfg = cfg or _technical_cfg()
    reasons: Counter = Counter()
    for code, df in data.items():
        d = df.tail(tail_bars).reset_index(drop=True)
        if len(d) <= cfg.MIN_DAYS:
            continue
        for i in range(cfg.MIN_DAYS - 1, len(d) - 1):
            _, reason = evaluate_breakout(
                d.iloc[: i + 1], code=code, name="", config=cfg,
                market_env={"regime": regime},
                latest_trade_date=pd.Timestamp(d.iloc[i]["date"]).strftime("%Y-%m-%d"))
            reasons[reason] += 1
    return reasons


def gate_ab(data: Dict[str, pd.DataFrame], bt_cfg: Optional[BacktestConfig] = None,
            variants: Optional[Iterable[str]] = None, tail_bars: int = 250
            ) -> Dict[str, dict]:
    """对层 3.8 的 KDJ/MACD 闸门做多组配置横向 A/B。

    所有变体共用同一份（已裁剪的）行情面板，差异只来自被改动的闸门开关。
    返回 {变体名: summary}，summary 即 run_backtest 的统计字典。
    """
    bt_cfg = bt_cfg or BacktestConfig()
    names = list(variants) if variants else list(GATE_VARIANTS)
    unknown = [n for n in names if n not in GATE_VARIANTS]
    if unknown:
        raise ValueError(f"未知变体 {unknown}（可选：{', '.join(GATE_VARIANTS)}）")
    sub = {c: df.tail(tail_bars).reset_index(drop=True) for c, df in data.items()}
    sub = {c: d for c, d in sub.items() if len(d) > 60}
    out: Dict[str, dict] = {}
    for n in names:
        summary, _trades = run_backtest(sub, bt_cfg, _technical_cfg(GATE_VARIANTS[n]))
        out[n] = summary
    return out


def render_gate_ab(rows: Dict[str, dict], data_note: str = "") -> str:
    """把 gate_ab 结果渲染成 Markdown 对照表。"""
    lines = ["# 突破策略 KDJ / MACD 闸门 A/B 对照", "",
             f"- 闸门开启变体：{', '.join(GATE_VARIANTS['gates_on'] .keys()) or '（生产默认全开）'}",
             "- 口径：technical 模式（quality_value 下闸门不参与荐股资格）",
             f"- 样本：{data_note or '本地日线缓存'}",
             "",
             "| 变体 | 正式信号 | 可成交 | 胜率% | 组合收益% | PnL | 期末权益 |",
             "|---|---|---|---|---|---|---|"]
    for name, s in rows.items():
        lines.append(f"| {name} | {s['signals']} | {s['trades']} | {s['win_rate_pct']} | "
                     f"{s['return_pct']} | {s['pnl']} | {s['final_equity']} |")
    lines += ["", "## 读表须知", "",
              "- 若各变体 `正式信号` 全为 0，说明漏斗在闸门之前就已闭合，"
              "此时对照表**不能**用来判断闸门有效性，须先看漏斗归因。",
              "- 判定标准：`正式信号` 出现差异且组合收益不降 → 闸门在过滤劣质信号；"
              "信号减少但收益下降 → 闸门误杀，应放宽阈值。",
              ""]
    return "\n".join(lines)


def _print_funnel(reasons: Counter, n_eval: int) -> None:
    print(f"[漏斗] 评估 {n_eval} 次，否决原因分布：")
    for r, n in reasons.most_common():
        print(f"        {r:30s} {n:7d}  ({n / max(n_eval, 1) * 100:5.2f}%)")


def main(argv: Optional[Iterable[str]] = None):
    ap = argparse.ArgumentParser(description="Volume breakout standalone backtest")
    ap.add_argument("--input", required=True, help="CSV file or directory of CSV files")
    ap.add_argument("--output", help="JSON output path (default: stdout)")
    ap.add_argument("--initial-capital", type=float, default=100000)
    ap.add_argument("--position-size", type=float, default=0.20, help="fraction of cash per position")
    ap.add_argument("--max-positions", type=int, default=5)
    ap.add_argument("--fee-rate", type=float, default=0.0003)
    ap.add_argument("--stamp-duty", type=float, default=0.0005)
    ap.add_argument("--slippage-bps", type=float, default=10)
    ap.add_argument("--max-holding-days", type=int, default=20)
    ap.add_argument("--regime", choices=["bull", "neutral", "bear", "unknown"], default="bull")
    ap.add_argument("--funnel", action="store_true",
                    help="只打印各层否决原因分布（定位「0 信号」卡在哪一层）")
    ap.add_argument("--ab-gates", nargs="*", metavar="VARIANT",
                    help=f"KDJ/MACD 闸门 A/B 变体（可选：{', '.join(GATE_VARIANTS)}）；"
                         f"给出该标志但不带值 = 跑全部变体")
    ap.add_argument("--max-stocks", type=int, default=0, help="只取前 N 只（控制 A/B 耗时）")
    ap.add_argument("--tail-bars", type=int, default=250, help="每只面板只取尾部 N 根 K 线")
    args = ap.parse_args(argv)
    data = load_price_files(args.input)
    if args.max_stocks > 0:
        data = {c: data[c] for c in sorted(data)[: args.max_stocks]}

    bt_cfg = BacktestConfig(args.initial_capital, args.position_size, args.max_positions,
                            args.fee_rate, args.stamp_duty, args.slippage_bps,
                            args.max_holding_days, args.regime)

    if args.funnel:
        print(f"[漏斗] 样本 {len(data)} 只 × 尾部 {args.tail_bars} 根，regime={args.regime}")
        reasons = funnel_counts(data, regime=args.regime, tail_bars=args.tail_bars)
        _print_funnel(reasons, sum(reasons.values()))
        return

    if args.ab_gates is not None:
        rows = gate_ab(data, bt_cfg, args.ab_gates or None, tail_bars=args.tail_bars)
        note = f"{len(data)} 只 × 尾部 {args.tail_bars} 根，regime={args.regime}"
        report = render_gate_ab(rows, data_note=note)
        print(report)
        if args.output:
            Path(args.output).write_text(report, encoding="utf-8")
        return

    summary, trades = run_backtest(data, bt_cfg)
    result = {"summary": summary, "trades": trades,
              "data_note": "Only formal signals are traded. OHLCV CSVs have no multi-year financial "
                           "evidence; incomplete default quality-mode data produces zero trades."}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
