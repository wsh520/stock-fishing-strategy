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
from dataclasses import dataclass
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
    files = [p] if p.is_file() else sorted(p.glob("*.csv"))
    if not files:
        raise ValueError(f"No CSV files found under {p}")
    out: Dict[str, pd.DataFrame] = {}
    for f in files:
        df = _load_csv(f)
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
    args = ap.parse_args(argv)
    data = load_price_files(args.input)
    summary, trades = run_backtest(data, BacktestConfig(args.initial_capital, args.position_size,
        args.max_positions, args.fee_rate, args.stamp_duty, args.slippage_bps,
        args.max_holding_days, args.regime))
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
