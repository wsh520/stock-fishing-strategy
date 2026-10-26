"""Pure, point-in-time evidence for recommendation ranking (no data requests)."""
import re
import numpy as np
import pandas as pd


def classify_cyclical_industry(industry, codes, keywords, code=None, exempt_codes=()):
    """按行业名判定是否周期行业。

    2026-10-26 增加 code / exempt_codes：数据源（Baostock query_stock_industry）
    只给证监会大类，**C26「化学原料和化学制品制造业」同时覆盖云天化（磷肥，真周期）
    与珀莱雅（化妆品）、登康口腔（牙膏）——行业名逐字相同，无法靠关键词区分**。
    实测后果：这两只消费股被当成周期股，一是被强行施加 CYCLICAL_NORMALIZED_PE_MAX
    检查，二是把「周期组」业绩统计抬高了约 2.2 个百分点
    （-4.62% → -2.44%），归因结论直接失真。
    故只能按代码显式豁免；豁免判断放在 missing 之后，行业缺失仍判 missing，
    不会因为进了豁免名单就绕过「行业分类缺失」的待核验标记。
    """
    name = str(industry or "").strip()
    if not name or name.lower() in {"unknown", "none", "nan", "未知", "未分类"}:
        return "missing"
    if code is not None:
        exempt = {str(c).strip() for c in (exempt_codes or ())}
        if str(code).strip() in exempt:
            return "non_cyclical"
    code_match = re.match(r"^([A-Z]\d{2})(?:\D|$)", name.upper())
    if (code_match and code_match.group(1) in codes) or any(word in name for word in keywords):
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
    """Recent closing-price bottom; legacy status/reason describe age only.

    failure_reasons independently reports age and overextension failures, without
    changing anchors, gains, status or the production caller's decision order.
    Missing-data detail remains in missing_detail (not a guessed gate failure).
    """
    result = dict(status="missing", reason="data_missing", missing_detail=None,
                  recent_window=recent_window, min_age=min_age, max_age=max_age,
                  anchor=None, anchor_date=None, age=None, gain_pct=None,
                  allowed_gain_pct=None, annual_extreme_gain_pct=None,
                  failure_reasons=[])
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
    # Independent diagnostics do not alter legacy age status or caller decisions.
    if age < min_age:
        result["failure_reasons"].append("too_new")
    elif age > max_age:
        result["failure_reasons"].append("too_old")
    if not np.isfinite(atr) or atr <= 0:
        result["missing_detail"] = "invalid_atr"
        return result
    # Bounded volatility adaptation: 10% minimum, 20% maximum.
    allowed = float(np.clip(atr_multiple * atr / close * 100, 10., 20.))
    result.update(allowed_gain_pct=allowed,
                  status="verified" if min_age <= age <= max_age else "unconfirmed",
                  reason="too_new" if age < min_age else "too_old" if age > max_age else "confirmed")
    if result["gain_pct"] > allowed:
        result["failure_reasons"].append("overextended")
    return result


def assess_shadow_bottoms(out, as_of, window=120, left_bars=3, right_bars=3,
                          min_separation=5, retest_tolerance=.03, min_rebound=.04):
    """Pure shadow study; never selects/replaces a production anchor.

    Strict local closing minima confirm after right_bars observed higher closes;
    a later lower close invalidates them permanently. Double bottoms use the
    first comparable retest, an intervening rebound, then a neckline breakout.
    All candidates are chronological; gains use the FIRST bottom, not a higher
    retest. Breaks below either bottom's support permanently invalidate a pair.
    """
    if (window < left_bars + right_bars + 1 or min(left_bars, right_bars, min_separation) < 1
            or not 0 <= retest_tolerance < 1 or not 0 < min_rebound < 1):
        raise ValueError("invalid shadow parameters")
    result = dict(status="missing", reason="required_columns", as_of=str(as_of),
                  shadow_only=True, local_bottoms=[], double_bottoms=[])
    if out is None or not {"date", "close"}.issubset(out.columns):
        return result
    frame = out.reset_index(drop=True).copy()
    frame["_date"] = pd.to_datetime(frame["date"], errors="coerce")
    day = pd.Timestamp(as_of)
    frame = frame.loc[frame["_date"].notna() & (frame["_date"] <= day)].sort_values("_date")
    if frame.empty or frame["_date"].duplicated().any() or frame.iloc[-1]["_date"] != day:
        result["reason"] = ("no_eligible_bars" if frame.empty else
                            "duplicate_dates" if frame["_date"].duplicated().any() else "as_of_bar_missing")
        return result
    frame = frame.tail(window).reset_index(drop=True)
    close = pd.to_numeric(frame["close"], errors="coerce").to_numpy(float)
    if len(frame) < left_bars + right_bars + 1 or not np.isfinite(close).all() or (close <= 0).any():
        result["reason"] = "insufficient_or_invalid_close"
        return result
    date = lambda i: frame.iloc[i]["_date"].strftime("%Y-%m-%d")
    positions = []
    for i in range(left_bars, len(close)):
        if not (close[i] < close[i-left_bars:i]).all():
            continue
        following = close[i+1:min(i+right_bars+1, len(close))]
        confirmation_failed = np.flatnonzero(following <= close[i])
        confirmed_i = (i + right_bars if i + right_bars < len(close)
                       and not len(confirmation_failed) else None)
        broken = np.flatnonzero(close[i+1:] < close[i])
        invalid_i = i + 1 + int(broken[0]) if len(broken) else None
        if len(confirmation_failed):
            invalid_i = i + 1 + int(confirmation_failed[0])
        state = "invalidated" if invalid_i is not None else "confirmed" if confirmed_i is not None else "unconfirmed"
        candidate = dict(anchor=float(close[i]), anchor_date=date(i), age=len(close)-1-i,
                         gain_pct=float((close[-1]/close[i]-1)*100), status=state,
                         confirmed_date=date(confirmed_i) if confirmed_i is not None else None,
                         invalidated_date=date(invalid_i) if invalid_i is not None else None)
        result["local_bottoms"].append(candidate)
        positions.append((i, candidate))
    for p, (i, first) in enumerate(positions):
        if first["confirmed_date"] is None:
            continue
        for j, second in positions[p+1:]:
            if second["confirmed_date"] is None and second["status"] == "invalidated":
                continue
            if j-i < max(min_separation, right_bars+1):
                continue
            if close[i+1:j].min() < close[i]*(1-retest_tolerance):
                break
            if abs(close[j]/close[i]-1) > retest_tolerance:
                continue
            neckline = float(close[i+1:j].max())
            if neckline/close[i]-1 < min_rebound:
                continue
            support = float(min(close[i], close[j]))
            broken = np.flatnonzero(close[j+1:] < support)
            invalid_i = j+1+int(broken[0]) if len(broken) else None
            confirmed_i = None
            if second["confirmed_date"] is not None:
                breakout = np.flatnonzero(close[j+right_bars:] > neckline)
                if len(breakout):
                    proposed = j+right_bars+int(breakout[0])
                    if invalid_i is None or proposed < invalid_i:
                        confirmed_i = proposed
            result["double_bottoms"].append(dict(
                status="invalidated" if invalid_i is not None else "confirmed" if confirmed_i is not None else "unconfirmed",
                first_date=date(i), second_date=date(j), first_anchor=float(close[i]),
                second_anchor=float(close[j]), support=support, neckline=neckline,
                confirmed_date=date(confirmed_i) if confirmed_i is not None else None,
                invalidated_date=date(invalid_i) if invalid_i is not None else None,
                gain_pct=float((close[-1]/close[i]-1)*100),
                support_gain_pct=float((close[-1]/support-1)*100)))
            break  # First comparable retest, not a gain-optimising search.
    result.update(status="observed", reason="shadow_only")
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
