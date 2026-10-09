"""Pure, point-in-time evidence for recommendation ranking (no data requests)."""
import re
import numpy as np
import pandas as pd


def classify_cyclical_industry(industry, codes, keywords):
    name = str(industry or "").strip()
    if not name or name.lower() in {"unknown", "none", "nan", "未知", "未分类"}:
        return "missing"
    code = re.match(r"^([A-Z]\d{2})(?:\D|$)", name.upper())
    if (code and code.group(1) in codes) or any(word in name for word in keywords):
        return "cyclical"
    return "non_cyclical"


def historical_valuation(frame, as_of, lookback=250, min_samples=120):
    result = dict(status="missing", percentile=None, samples=0, median_pe=None)
    if frame is None or not {"date", "peTTM"}.issubset(frame.columns):
        return result
    frame = frame.reset_index(drop=True)
    dates = pd.to_datetime(frame["date"], errors="coerce")
    day = pd.Timestamp(as_of)
    eligible = frame.loc[dates.notna() & (dates <= day)].copy()
    eligible["_date"] = dates.loc[eligible.index]
    if eligible.empty or eligible["_date"].duplicated().any():
        return result
    eligible = eligible.sort_values("_date")
    if eligible.iloc[-1]["_date"] != day:
        return result
    current = pd.to_numeric(pd.Series([eligible.iloc[-1]["peTTM"]]), errors="coerce").iloc[0]
    history = pd.to_numeric(eligible.loc[eligible["_date"] < day, "peTTM"].tail(lookback), errors="coerce")
    history = history[np.isfinite(history) & (history > 0)]
    result["samples"] = len(history)
    if not np.isfinite(current) or current <= 0 or len(history) < min_samples:
        return result
    result.update(status="verified", percentile=float(((history < current).sum() + .5 * (history == current).sum()) / len(history)), median_pe=float(history.median()))
    return result


def quality_value_technical(out, event_window=5):
    """40 trend + 30 momentum + 30 volume; persistent states and decaying events."""
    close = pd.to_numeric(out["close"], errors="coerce")
    ma20 = pd.to_numeric(out["ma20"], errors="coerce")
    slope = pd.to_numeric(out["ma20_slope"], errors="coerce")
    # Above MA20 with a flat/up MA20 remains evidence after a one-day crossover.
    trend_state = ((close >= ma20) & (slope >= 0)).astype(float)
    distance = ((close / ma20 - 1) / .04).clip(0, 1).fillna(0)
    slope_strength = (slope / .02).clip(0, 1).fillna(0)
    trend = 40 * trend_state * (.75 + .125 * distance + .125 * slope_strength)
    hist = pd.to_numeric(out["macd_histogram"], errors="coerce")
    improvement = hist.diff().gt(0).astype(float).rolling(3, min_periods=3).mean().fillna(0)
    k = pd.to_numeric(out["kdj_k"], errors="coerce")
    d = pd.to_numeric(out["kdj_d"], errors="coerce")
    kdj_state = ((k > d) & (k <= 80)).astype(float)
    rsi = pd.to_numeric(out["rsi14"], errors="coerce")
    rsi_state = ((rsi >= 35) & (rsi <= 60) & (rsi.diff() >= 0)).astype(float)
    momentum = 30 * np.maximum.reduce([improvement.to_numpy() * .8, kdj_state.to_numpy() * .8, rsi_state.to_numpy() * .7])
    # An event is strongest on its date and loses weight each following bar.
    window = max(1, int(event_window))
    event = pd.Series(0., index=out.index)
    for column in ("trend_turn", "macd_golden_cross", "rsi_rebound"):
        if column in out:
            flag = out[column].fillna(False).astype(float)
            for age in range(window):
                event = np.maximum(event, flag.shift(age, fill_value=0) * (1 - age / window))
    momentum = pd.Series(np.maximum(momentum, event.to_numpy() * 30), index=out.index)
    raw_volume = out.get("vol_price_continuous", out["vol_price_quality"])
    volume = pd.to_numeric(raw_volume, errors="coerce").fillna(0).clip(0, 25) / 25 * 30
    return pd.DataFrame({"qv_trend_score": trend, "qv_momentum_score": momentum,
                         "qv_volume_score": volume, "qv_event_strength": event,
                         "qv_daily_score": (trend + momentum + volume).clip(0, 100).round(1)})


def assess_low_structure(out, as_of, recent_window=60, min_age=5, max_age=40, atr_multiple=5.):
    """Recent confirmed closing-price bottom; isolated intraday wicks are diagnostic."""
    result = dict(status="missing", reason="data_missing", missing_detail=None,
                  recent_window=recent_window, min_age=min_age, max_age=max_age,
                  anchor=None, anchor_date=None, age=None, gain_pct=None,
                  allowed_gain_pct=None, annual_extreme_gain_pct=None)
    if out is None or not {"date", "close", "low", "atr"}.issubset(out.columns):
        result["missing_detail"] = "required_columns"
        return result
    dates = pd.to_datetime(out["date"], errors="coerce")
    frame = out.loc[dates.notna() & (dates <= pd.Timestamp(as_of))].copy()
    frame["_date"] = dates.loc[frame.index]
    frame = frame.sort_values("_date").reset_index(drop=True)
    if frame.empty or frame["_date"].duplicated().any() or frame.iloc[-1]["_date"] != pd.Timestamp(as_of):
        result["missing_detail"] = ("no_eligible_bars" if frame.empty else
                                    "duplicate_dates" if frame["_date"].duplicated().any() else "as_of_bar_missing")
        return result
    closes = pd.to_numeric(frame["close"], errors="coerce")
    atr = pd.to_numeric(pd.Series([frame.iloc[-1]["atr"]]), errors="coerce").iloc[0]
    if len(frame) < recent_window or not np.isfinite(closes.tail(recent_window)).all() or (closes.tail(recent_window) <= 0).any():
        result["missing_detail"] = "insufficient_bars" if len(frame) < recent_window else "invalid_close"
        return result
    anchor_index = closes.tail(recent_window).idxmin()
    age = len(frame) - 1 - int(anchor_index)
    anchor = float(closes.loc[anchor_index])
    close = float(closes.iloc[-1])
    extreme = pd.to_numeric(frame["low"].tail(250), errors="coerce")
    if np.isfinite(extreme).all() and extreme.min() > 0:
        result["annual_extreme_gain_pct"] = float((close / extreme.min() - 1) * 100)
    result.update(anchor=anchor, anchor_date=frame.loc[anchor_index, "_date"].strftime("%Y-%m-%d"),
                  age=age, gain_pct=(close / anchor - 1) * 100)
    if not np.isfinite(atr) or atr <= 0:
        result["missing_detail"] = "invalid_atr"
        return result
    # Bounded volatility adaptation: 10% minimum, 20% maximum.
    allowed = float(np.clip(atr_multiple * atr / close * 100, 10., 20.))
    result.update(allowed_gain_pct=allowed,
                  status="verified" if min_age <= age <= max_age else "unconfirmed",
                  reason="too_new" if age < min_age else "too_old" if age > max_age else "confirmed")
    return result


def normalized_earnings_valuation(pe, annual_rows, operating, min_years=3):
    """Current market value / median annual attributable profit; never mix profit bases."""
    profits = []
    for row in annual_rows or []:
        value = row.get("attributable_profit")
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(value) and value > 0:
            profits.append(value)
    current = (operating or {}).get("metrics", {}).get("attributable_profit_ttm")
    try:
        pe, current = float(pe), float(current)
    except (TypeError, ValueError):
        return dict(status="missing", normalized_pe=None, years=len(profits))
    if len(profits) < min_years or not np.isfinite([pe, current]).all() or min(pe, current) <= 0:
        return dict(status="missing", normalized_pe=None, years=len(profits))
    normalized = pe * current / float(np.median(profits))
    if not np.isfinite(normalized):
        return dict(status="missing", normalized_pe=None, years=len(profits))
    return dict(status="verified", normalized_pe=normalized, years=len(profits))


def cyclical_earnings_risk(annual_rows, operating, multiplier=1.5):
    """Consolidated profit comparison is a peak-risk label, NOT a PE adjustment."""
    profits = [float(r["net_profit"]) for r in annual_rows if r.get("net_profit") is not None
               and np.isfinite(float(r["net_profit"])) and float(r["net_profit"]) > 0]
    current = (operating or {}).get("metrics", {}).get("net_profit_ttm")
    if len(profits) < 3 or current is None or not np.isfinite(current):
        return dict(status="missing", peak_ratio=None)
    ratio = float(current / np.median(profits))
    return dict(status="peak" if ratio > multiplier else "normal", peak_ratio=ratio)
