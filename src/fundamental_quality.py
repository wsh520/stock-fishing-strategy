"""Point-in-time annual quality checks, independent of strategy and cache code.

Source contract verified against AkShare stock_finance_sina.py and its docs:
https://github.com/akfamily/akshare/blob/main/akshare/stock_fundamental/stock_finance_sina.py
https://akshare.akfamily.xyz/data/stock/stock.html#id214
``stock_financial_abstract`` returns [选项, 指标, YYYYMMDD, ...]. The
常用指标 rows 净资产收益率(ROE), 扣非净利润, 净利润, 经营现金流量净额
map to ROEWEIGHTED (%), NPCUT, NETPROFIT and MANANETR (CNY yuan).
净利润 includes minority interests; 归母净利润 is deliberately never used.
The upstream publish_date is discarded by AkShare's abstract function, so
May 1 of the following year is the conservative availability assumption.
This is disclosure gating, not a historical-vintage/restatement database.
"""
from datetime import date, datetime
import math
import re

_FIELDS = ("roe", "deducted_profit", "net_profit", "operating_cashflow")
_SINA_FIELDS = {
    "roe": "净资产收益率(ROE)",
    "deducted_profit": "扣非净利润",
    "net_profit": "净利润",
    "operating_cashflow": "经营现金流量净额",
}


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        if re.fullmatch(r"\d{8}", text):
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text[:10])
    except (ValueError, TypeError):
        return None


def _number(value):
    """Canonical input is numeric yuan / percentage points, never ratios."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _validate(as_of, years):
    day = _date(as_of)
    if day is None:
        raise ValueError("as_of must be an ISO date or date object")
    if isinstance(years, bool) or not isinstance(years, int) or years < 1:
        raise ValueError("years must be a positive integer")
    if day.year - years - 1 < 1:
        raise ValueError("as_of is too early for the requested window")
    return day


def _dimension_score(value, threshold, saturation):
    if value is None:
        return 0.0
    span = saturation - threshold
    if span <= 0 or not math.isfinite(span):
        # High custom thresholds still earn 50 at the threshold, with a
        # proportional improvement range; scaled subtraction avoids overflow.
        span = max(abs(threshold), saturation)
    progress = value / span - threshold / span
    return max(0.0, min(100.0, 50.0 + 50.0 * progress))


def evaluate_annual_quality(annual_rows, as_of, years=3, median_roe_min=10,
                            min_roe=5, cash_conversion_min=.8, financial=False,
                            require_annual_net_profit_positive=False,
                            require_annual_cashflow_positive=False):
    """Evaluate latest disclosed complete fiscal years (December year-end).

    Rows: year, roe (percentage points), deducted_profit, net_profit,
    operating_cashflow (all yuan), available_date and report_date (ISO).
    Missing available_date falls back to next May 1. Actual dates take priority,
    including late announcements. Before May 1 the baseline latest year is
    as_of.year-2, advanced to year-1 when an actual announcement is available.
    Missing latest years cannot be replaced by older rows. Duplicate years are
    ambiguous and are excluded. Inputs are never mutated.

    Status: verified=all four checks pass; failed=known violation;
    partial=some relevant data but incomplete; missing=no relevant data;
    financial_review=financial company, pending specialist review.
    Continuous score weights median ROE/min ROE/cash conversion 50%/25%/25%.
    ``require_annual_net_profit_positive`` and
    ``require_annual_cashflow_positive`` optionally make each known year's
    consolidated net profit / operating cash flow positive hard checks.  They
    default to False for compatibility with callers that only used the
    aggregate cash-conversion gate.  Missing values remain evidence gaps
    (partial), while a known non-positive value is a failed quality check.
    Each known dimension scores 50 at its threshold and 100 at saturation
    (25%, 15%, 1.5 respectively), with linear interpolation clamped to 0..100.
    If a custom threshold meets/exceeds saturation, the improvement span is
    max(abs(threshold), saturation), preserving 50 at the threshold without
    division by zero. Missing dimensions score zero. Positive deducted profit
    remains a hard gate, independent of score; a high score cannot override a
    failed/partial status. This score ranks evidence, not investment returns.
    """
    day = _validate(as_of, years)
    for value in (median_roe_min, min_roe, cash_conversion_min):
        if _number(value) is None:
            raise ValueError("quality thresholds must be finite numbers")
    median_roe_min, min_roe, cash_conversion_min = map(
        float, (median_roe_min, min_roe, cash_conversion_min))
    result = dict(status="missing", missing_tags=[], quality_score=0,
                  reason="缺少可核验年度财报", annual_rows=[], metrics={})
    if financial:
        result.update(status="financial_review", missing_tags=["financial_review"],
                      reason="金融股 pending：待行业专项核验，不适用非金融财报规则")
        return result

    normalized = []
    invalid = []
    for raw in annual_rows or []:
        if not isinstance(raw, dict):
            invalid.append("invalid_row")
            continue
        y = _number(raw.get("year"))
        if y is None or y != int(y) or not 1 <= y <= 9998:
            invalid.append("invalid_year")
            continue
        y = int(y)
        report = _date(raw.get("report_date"))
        if report != date(y, 12, 31):
            invalid.append(f"invalid_report_date:{y}")
            continue
        raw_available = raw.get("available_date")
        assumed = raw_available is None or str(raw_available).strip() == ""
        available = date(y + 1, 5, 1) if assumed else _date(raw_available)
        if available is None or available <= report:
            invalid.append(f"invalid_available_date:{y}")
            continue
        row = {key: _number(raw.get(key)) for key in _FIELDS}
        row.update(year=y, report_date=report.isoformat(),
                   available_date=available.isoformat(),
                   availability_assumed=assumed or bool(raw.get("availability_assumed", False)))
        normalized.append(row)

    latest = day.year - (1 if (day.month, day.day) >= (5, 1) else 2)
    if any(r["year"] == day.year - 1 and _date(r["available_date"]) <= day
           for r in normalized):
        latest = day.year - 1
    expected = list(range(latest - years + 1, latest + 1))
    tags = []
    selected = []
    for y in expected:
        candidates = [r for r in normalized if r["year"] == y
                      and _date(r["available_date"]) <= day]
        if not candidates:
            tags.append(f"missing_year:{y}")
            if any(r["year"] == y for r in normalized):
                tags.append(f"not_available:{y}")
            continue
        if len(candidates) > 1:
            tags.append(f"duplicate_year:{y}")
            continue
        row = candidates[0]
        selected.append(row)
        tags.extend(f"{key}:{y}" for key in _FIELDS if row[key] is None)
    tags.extend(tag for tag in invalid if ":" not in tag or
                int(tag.rsplit(":", 1)[1]) in expected)
    metrics = dict(expected_years=expected, observed_years=[r["year"] for r in selected],
                   median_roe=None, min_roe=None, deducted_profit_positive=None,
                   net_profit_sum=None, operating_cashflow_sum=None,
                   cash_conversion=None, annual_net_profit_positive=None,
                   annual_operating_cashflow_positive=None,
                   net_profit_positive_years=0,
                   operating_cashflow_positive_years=0, failed_checks=[])
    complete = len(selected) == years
    checks = {}
    roes = [r["roe"] for r in selected if r["roe"] is not None]
    if roes and min(roes) < min_roe:
        checks["min_roe"] = False
    if complete and len(roes) == years:
        ordered = sorted(roes)
        middle = len(ordered) // 2
        # Half first to avoid overflowing an even-sized finite median.
        median_value = (ordered[middle] if len(ordered) % 2 else
                        ordered[middle - 1] / 2 + ordered[middle] / 2)
        metrics.update(median_roe=median_value, min_roe=min(roes))
        checks.update(median_roe=metrics["median_roe"] >= median_roe_min,
                      min_roe=metrics["min_roe"] >= min_roe)
    profits = [r["deducted_profit"] for r in selected if r["deducted_profit"] is not None]
    if any(p <= 0 for p in profits):
        checks["deducted_profit_positive"] = False
        metrics["deducted_profit_positive"] = False
    elif complete and len(profits) == years:
        checks["deducted_profit_positive"] = True
        metrics["deducted_profit_positive"] = True
    net_values = [r["net_profit"] for r in selected]
    cash_values = [r["operating_cashflow"] for r in selected]
    metrics["net_profit_positive_years"] = sum(v is not None and v > 0 for v in net_values)
    metrics["operating_cashflow_positive_years"] = sum(v is not None and v > 0 for v in cash_values)
    if require_annual_net_profit_positive:
        if any(v is not None and v <= 0 for v in net_values):
            checks["annual_net_profit_positive"] = False
            metrics["annual_net_profit_positive"] = False
        elif complete and len(net_values) == years and all(v is not None for v in net_values):
            checks["annual_net_profit_positive"] = True
            metrics["annual_net_profit_positive"] = True
    if require_annual_cashflow_positive:
        if any(v is not None and v <= 0 for v in cash_values):
            checks["annual_operating_cashflow_positive"] = False
            metrics["annual_operating_cashflow_positive"] = False
        elif complete and len(cash_values) == years and all(v is not None for v in cash_values):
            checks["annual_operating_cashflow_positive"] = True
            metrics["annual_operating_cashflow_positive"] = True
    if complete and all(v is not None for r in selected
                        for v in (r["net_profit"], r["operating_cashflow"])):
        try:
            net = math.fsum(r["net_profit"] for r in selected)
            cash = math.fsum(r["operating_cashflow"] for r in selected)
        except OverflowError:
            net = cash = float("inf")
        if not math.isfinite(net) or not math.isfinite(cash):
            tags.append("nonfinite_aggregate")
        else:
            metrics.update(net_profit_sum=net, operating_cashflow_sum=cash)
            ratio = cash / net if net > 0 else None
            if ratio is not None and not math.isfinite(ratio):
                tags.append("nonfinite_cash_conversion")
            else:
                metrics["cash_conversion"] = ratio
                checks["cash_conversion"] = ratio is not None and ratio >= cash_conversion_min
    metrics["failed_checks"] = [key for key, passed in checks.items() if not passed]
    required_checks = ["median_roe", "min_roe", "deducted_profit_positive",
                       "cash_conversion"]
    if require_annual_net_profit_positive:
        required_checks.append("annual_net_profit_positive")
    if require_annual_cashflow_positive:
        required_checks.append("annual_operating_cashflow_positive")
    if metrics["failed_checks"]:
        status, reason = "failed", "年度质量未达标：" + ", ".join(metrics["failed_checks"])
    elif tags:
        status = "partial" if selected else "missing"
        reason = "年度质量待核验：" + ", ".join(dict.fromkeys(tags))
    elif all(checks.get(key) is True for key in required_checks):
        status, reason = "verified", f"最近连续{years}个完整年度的ROE、扣非净利润及现金转换率均达标"
    else:
        status, reason = "partial", "年度质量证据不足"
    components = {
        "median_roe": _dimension_score(metrics["median_roe"], median_roe_min, 25.0),
        "min_roe": _dimension_score(metrics["min_roe"], min_roe, 15.0),
        "cash_conversion": _dimension_score(metrics["cash_conversion"], cash_conversion_min, 1.5),
    }
    metrics["quality_components"] = components
    score = (.5 * components["median_roe"] + .25 * components["min_roe"]
             + .25 * components["cash_conversion"])
    result.update(status=status, reason=reason, missing_tags=list(dict.fromkeys(tags)),
                  quality_score=score, annual_rows=selected, metrics=metrics)
    return result


def _parse_sina_abstract(table):
    """Parse exact documented wide-table fields; unknown fields stay missing."""
    records = table.to_dict("records") if hasattr(table, "to_dict") else list(table)
    if not records:
        return []
    columns = list(dict.fromkeys(k for r in records for k in r))
    common = [r for r in records if r.get("选项") == "常用指标"]
    rows = []
    for column in columns:
        report = _date(column)
        if report is None or (report.month, report.day) != (12, 31):
            continue
        row = dict(year=report.year, report_date=report.isoformat(), available_date=None)
        for key, label in _SINA_FIELDS.items():
            values = [_number(r.get(column)) for r in common if r.get("指标") == label]
            # A duplicated or renamed metric must not silently select another basis.
            row[key] = values[0] if len(values) == 1 else None
        # AkShare currently omits this metadata. Support an explicit metadata row
        # if a caller's adapter preserves it; never substitute report_date.
        announcement = [r.get(column) for r in records if r.get("指标") == "公告日期"]
        if len(announcement) == 1:
            row["available_date"] = announcement[0]
        rows.append(row)
    return rows


def fetch_annual_quality(code, as_of, years=3, ak_client=None):
    """Fetch one company's annual evidence and return the evaluator result.

    No cache, market-wide calls or industry inference. The caller must route
    financial companies to evaluate_annual_quality(..., financial=True).
    Network/schema errors are returned as missing evidence, never a pass.
    """
    _validate(as_of, years)
    symbol = str(code).strip()
    if not re.fullmatch(r"\d{1,6}", symbol):
        raise ValueError("code must contain 1 to 6 digits")
    symbol = symbol.zfill(6)
    try:
        if ak_client is None:
            import akshare as ak_client
        table = ak_client.stock_financial_abstract(symbol=symbol)
        rows = _parse_sina_abstract(table)
    except Exception as exc:
        result = evaluate_annual_quality([], as_of, years)
        result["missing_tags"].append("fetch_error")
        result["reason"] = f"年度财报取数失败：{type(exc).__name__}: {exc}"
    else:
        result = evaluate_annual_quality(rows, as_of, years)
    result.update(code=symbol, source="akshare.stock_financial_abstract", amount_unit="CNY yuan")
    return result
