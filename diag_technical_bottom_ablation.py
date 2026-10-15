"""Offline technical ablation and bottom comparison, local CSV only.

Example: python diag_technical_bottom_ablation.py --input backtest_cache/daily \
    --as-of 2025-06-30 --as-of 2025-05-30 --limit 100
OHLC plus volume and amount are required: missing volume is not fabricated.
No fetching, database, push, config mutation, or production evaluation is used.
"""
import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.screening_engine import StrategyConfig, compute_daily_signals, assess_entry_timing
from src.recommendation_factors import assess_low_structure, assess_shadow_bottoms


def strict_stabilization(technical, cfg):
    """Mirror current strict levels; weak is pending, not formal admission."""
    timing = assess_entry_timing(technical, cfg)
    sd = max(1, int(cfg.STABILIZATION_MA20_SLOPE_DAYS))
    ma20_ok = False
    if timing.get("below_ma20") is False and len(technical) > sd:
        now, prev = technical.ma20.iloc[-1], technical.ma20.iloc[-sd-1]
        ma20_ok = bool(np.isfinite([now, prev]).all() and prev > 0 and now >= prev)
    above60 = len(technical) >= 60 and technical.close.iloc[-1] >= technical.close.tail(60).mean()
    n = max(1, int(cfg.MACD_MOMENTUM_DAYS))
    h = technical.macd_histogram.tail(n+1).to_numpy(float)
    momentum = len(h) == n+1 and np.isfinite(h).all() and (np.diff(h) > 0).all()
    ln = max(2, int(cfg.STABILIZATION_NO_NEW_LOW_LOOKBACK))
    pn = max(ln, int(cfg.STABILIZATION_PRIOR_LOW_LOOKBACK))
    lows = technical.low.tail(ln+pn).to_numpy(float)
    no_new_low = (len(lows) == ln+pn and np.isfinite(lows).all() and (lows > 0).all()
                  and lows[-ln:].min() >= lows[:-ln].min())
    if ma20_ok:
        return "strong" if above60 else "medium"
    if momentum and no_new_low and not cfg.QV_STABILIZATION_MEDIUM_REQUIRES_MA20:
        return "medium"
    return "weak" if momentum else "none"


def evaluate_frame(frame, as_of, code="local", horizons=(5, 10, 20), cfg=None):
    """Return one diagnostic row; features use <=as_of, labels use later closes.

    Returns (row, skip_reason). This is a technical-only population, not a
    reconstruction of the financial/valuation/risk eligible production pool.
    """
    cfg = cfg or StrategyConfig()
    required = {"date", "open", "high", "low", "close", "volume", "amount"}
    if not required.issubset(frame.columns):
        return None, "missing_columns:" + ",".join(sorted(required-set(frame.columns)))
    full = frame.copy().reset_index(drop=True)
    full["date"] = pd.to_datetime(full.date, errors="coerce")
    if full.date.isna().any() or full.date.duplicated().any():
        return None, "invalid_or_duplicate_dates"
    full = full.sort_values("date").reset_index(drop=True)
    day = pd.Timestamp(as_of)
    history = full.loc[full.date <= day].copy()
    if history.empty or history.iloc[-1].date != day:
        return None, "as_of_bar_missing"
    numeric = ["open", "high", "low", "close", "volume", "amount"]
    history[numeric] = history[numeric].apply(pd.to_numeric, errors="coerce")
    if (history.iloc[-1][["volume", "amount"]] == 0).any():
        return None, "as_of_no_trading"
    # Filter only already-observed suspension placeholders before indicators.
    history = history.loc[(history.volume != 0) & (history.amount != 0)].copy().reset_index(drop=True)
    if not np.isfinite(history[numeric].to_numpy(float)).all():
        return None, "invalid_ohlcva"
    if ((history[["open", "high", "low", "close"]] <= 0).any().any()
            or (history[["volume", "amount"]] < 0).any().any()
            or (history.high < history[["open", "close", "low"]].max(axis=1)).any()
            or (history.low > history[["open", "close"]].min(axis=1)).any()):
        return None, "invalid_ohlcva"
    history["pct_chg"] = history.close.pct_change(fill_method=None) * 100
    technical = compute_daily_signals(history, cfg)
    if technical is None:
        return None, "insufficient_bars"
    score = float(technical.iloc[-1]["qv_daily_score" if cfg.QV_TECHNICAL_STATE_SCORING else "daily_score"])
    if not np.isfinite(score):
        return None, "invalid_score"
    level = strict_stabilization(technical, cfg)
    stab, tech = level in ("strong", "medium"), score >= 40
    bottom = assess_low_structure(technical, day, cfg.QV_RECENT_LOW_WINDOW,
                                 cfg.QV_RECENT_LOW_MIN_AGE, cfg.QV_RECENT_LOW_MAX_AGE,
                                 cfg.QV_LOW_GAIN_ATR_MULT)
    row = dict(code=code, as_of=day.strftime("%Y-%m-%d"), technical_score=score,
               stabilization_level=level, current_stab_and_tech40=stab and tech,
               stabilization_only=stab, technical_only=tech,
               production_bottom=bottom, shadow_bottoms=assess_shadow_bottoms(technical, day))
    # Labels are separate from features; history filtering cannot offset indices.
    future = full.loc[full.date > day, ["close", "volume", "amount"]].apply(pd.to_numeric, errors="coerce")
    active = np.isfinite(future[["volume", "amount"]]).all(axis=1) & (future[["volume", "amount"]] > 0).all(axis=1)
    labels = future.loc[active, "close"].reset_index(drop=True)
    for h in horizons:
        if h <= 0:
            raise ValueError("horizons must be positive")
        value = labels.iloc[h-1] if h <= len(labels) else np.nan
        row[f"forward_{h}bar_pct"] = float((value/history.close.iloc[-1]-1)*100) if np.isfinite(value) and value > 0 else None
    return row, None


def summarize(rows, horizons=(5, 10, 20), min_samples=30, min_dates=5):
    groups = {}
    for name in ("current_stab_and_tech40", "stabilization_only", "technical_only"):
        selected = [r for r in rows if r[name]]
        group = dict(selected=len(selected), unique_codes=len({r["code"] for r in selected}),
                     unique_dates=len({r["as_of"] for r in selected}), forward={})
        for h in horizons:
            key = f"forward_{h}bar_pct"
            complete = [r for r in selected if r[key] is not None]
            values = [r[key] for r in complete]
            enough = len(values) >= min_samples and len({r["as_of"] for r in complete}) >= min_dates
            group["forward"][str(h)] = dict(complete=len(values), missing=len(selected)-len(values),
                median_pct=float(np.median(values)) if values else None,
                mean_pct=float(np.mean(values)) if values else None,
                sample_status="descriptive_only" if enough else "insufficient_no_return_conclusion")
        groups[name] = group
    return dict(scope="technical_only_not_full_production_selection", threshold=40,
                stabilization_admission="strict strong/medium; weak remains pending",
                sample_unit="stock-date; overlapping windows and repeated stocks are dependent",
                conclusion="仅描述统计；样本不足不可推收益结论；即使达到数量门槛亦不证明收益改善。",
                limitations=["No financial/valuation/risk universe reconstruction", "No transaction costs, fills or benchmark adjustment",
                             "Local cache may contain survivorship bias or revised adjusted prices"],
                sufficiency_rule=dict(min_complete_samples=min_samples, min_distinct_dates=min_dates),
                rows=len(rows), groups=groups)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="local CSV or directory of CSV files")
    parser.add_argument("--as-of", required=True, action="append", help="repeat for multiple decision dates")
    parser.add_argument("--limit", type=int, default=0, help="0 means all local files")
    parser.add_argument("--min-samples", type=int, default=30)
    parser.add_argument("--min-dates", type=int, default=5)
    parser.add_argument("--output", type=Path, help="optional local JSON report; otherwise stdout")
    args = parser.parse_args(argv)
    if args.limit < 0 or min(args.min_samples, args.min_dates) < 1:
        parser.error("invalid sample or file limit")
    if not args.input.is_file() and not args.input.is_dir():
        parser.error(f"input does not exist or is not a regular file/directory: {args.input}")
    files = [args.input] if args.input.is_file() else sorted(args.input.glob("*.csv"))
    if not files:
        parser.error(f"no CSV files found in input directory: {args.input}")
    if args.limit:
        files = files[:args.limit]
    rows, skips = [], Counter()
    cfg = StrategyConfig()
    for path in files:
        try:
            frame = pd.read_csv(path)
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            skips["read_error:"+type(exc).__name__] += 1
            continue
        for day in sorted(set(args.as_of)):
            row, reason = evaluate_frame(frame, day, path.stem, cfg=cfg)
            if row is None:
                skips[reason] += 1
            else:
                rows.append(row)
    report = summarize(rows, min_samples=args.min_samples, min_dates=args.min_dates)
    report.update(files=len(files), skipped=dict(skips), observations=rows)
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    return report


if __name__ == "__main__":
    main()
