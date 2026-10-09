"""Recent operating evidence from Sina's consolidated financial summary.

Contract checked against AkShare stock_finance_sina.py and a public gjzb response:
https://raw.githubusercontent.com/akfamily/akshare/master/akshare/stock_fundamental/stock_finance_sina.py
The raw endpoint preserves publish_date, which stock_financial_abstract discards.
Amounts are CNY yuan; margins are percentage points. Compare the same reporting
period last year, never adjacent quarters or annualized quarterly observations.
This is disclosure gating, not a historical-vintage financial database.
"""
from datetime import date, datetime
import math
import re

SOURCE_URL = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
_FIELDS = {
    "BIZTOTINCO": "revenue", "NETPROFIT": "net_profit",
    "NPCUT": "deducted_profit", "MANANETR": "operating_cashflow",
    "SGPMARGIN": "gross_margin", "SNPMARGINCONMS": "net_margin",
}
_SUMMARY_FIELDS = dict(_FIELDS, ROEWEIGHTED="roe")


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return (datetime.strptime(text, "%Y%m%d").date() if re.fullmatch(r"\d{8}", text)
                else date.fromisoformat(text[:10]))
    except (ValueError, TypeError):
        return None


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_summary(payload):
    """Parse exact item codes from 常用指标, preserving actual disclosure dates."""
    reports = payload.get("result", {}).get("data", {}).get("report_list", {})
    if not isinstance(reports, dict):
        return []
    rows = []
    for period, report in reports.items():
        if not isinstance(report, dict):
            continue
        # Do not mix parent-company statements or foreign-currency amounts.
        if report.get("rType") != "合并期末" or report.get("rCurrency") != "CNY":
            continue
        row = {"report_date": period, "available_date": report.get("publish_date"),
               "source": SOURCE_URL, "report_source": "gjzb", "data_source": report.get("data_source"),
               "statement_basis": "consolidated", "currency": "CNY"}
        seen = set()
        for item in report.get("data", []):
            if not isinstance(item, dict) or str(item.get("item_group_no")) != "1":
                continue
            field = _SUMMARY_FIELDS.get(item.get("item_field"))
            if item.get("item_title") in {"归母净利润", "归属于母公司股东的净利润"}:
                field = "attributable_profit"
            if field is None:
                continue
            value = _number(item.get("item_value"))
            # An ambiguous duplicated source field is missing evidence, not last-wins.
            row[field] = None if field in seen else value
            seen.add(field)
        rows.append(row)
    return rows


def _minimum_period(day):
    if day.month >= 11:
        return date(day.year, 9, 30)
    if day.month >= 9:
        return date(day.year, 6, 30)
    if day.month >= 5:
        return date(day.year, 3, 31)
    return date(day.year - 1, 9, 30)


def _supplemental_trends(usable, duplicates, latest):
    """Optional evidence: Qn=YTDn-YTD(n-1); TTM=prior FY+YTD-prior YTD.

    All inputs have passed disclosure gating; missing/duplicate operands produce
    None. Ratios and margins are never subtracted as cumulative flow amounts.
    """
    metrics = {}
    fields = ("revenue", "net_profit", "deducted_profit", "operating_cashflow", "attributable_profit")

    def amount(period, field):
        return None if period in duplicates else _number(usable.get(period, {}).get(field))

    def quarter(period, field):
        value = amount(period, field)
        if period.month == 3:
            return value
        month = period.month - 3
        previous = date(period.year, month, 30 if month in (6, 9) else 31)
        previous_value = amount(previous, field)
        return _number(value - previous_value) if value is not None and previous_value is not None else None

    def ttm(period, field):
        value = amount(period, field)
        if period.month == 12:
            return value
        annual = amount(date(period.year - 1, 12, 31), field)
        prior = amount(date(period.year - 1, period.month, period.day), field)
        return _number(annual + value - prior) if all(v is not None for v in (value, annual, prior)) else None

    prior_period = date(latest.year - 1, latest.month, latest.day)
    for field in fields:
        for label, derive in (("single_quarter", quarter), ("ttm", ttm)):
            value, previous = derive(latest, field), derive(prior_period, field)
            metrics[field + "_" + label] = value
            metrics[field + "_" + label + "_yoy"] = (
                _number((value / previous - 1) * 100)
                if value is not None and previous is not None and previous > 0 else None)
    return metrics


def evaluate_operating_trend(rows, as_of, profit_yoy_min=-10.0,
                             gross_margin_drop_max=3.0, net_margin_drop_max=2.0,
                             *, yoy_decline_tolerance=3.0,
                             cashflow_decline_tolerance=3.0, cash_conversion_min=.8):
    """Return verified/weak/failed/missing using latest disclosed same-period data.

    Missing or stale evidence cannot verify. Known losses or severe profit
    contraction fail even if another dimension is missing.
    Mild deterioration beyond yoy_decline_tolerance is weak (observation only).
    Declines inside the tolerance pass individual growth checks; simultaneous
    sales/profit/cash deterioration still vetoes. Cash declines within
    cashflow_decline_tolerance pass only when current cumulative cash / net
    profit meets cash_conversion_min. All percentages use percentage points.
    Optional single-quarter / TTM evidence never fills core missing fields.
    Negative prior-year profit is
    a turnaround, not a meaningful ordinary growth rate. Cash-flow changes use
    amounts when the prior base is nonpositive; no invented percentage growth.
    """
    day = _day(as_of)
    if day is None:
        raise ValueError("as_of must be a valid decision date")
    limits = [_number(x) for x in (profit_yoy_min, gross_margin_drop_max, net_margin_drop_max,
                                   yoy_decline_tolerance, cashflow_decline_tolerance,
                                   cash_conversion_min)]
    if any(x is None for x in limits) or any(x < 0 for x in limits[1:]):
        raise ValueError("operating thresholds must be finite, margin limits nonnegative")
    result = {"status": "missing", "as_of": day.isoformat(), "period": None, "available_date": None,
              "metrics": {}, "missing_tags": [], "reasons": [], "source": SOURCE_URL}
    usable, duplicates, supplemental_rows = {}, set(), []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        period, published = _day(row.get("report_date")), _day(row.get("available_date"))
        if period is None or (period.month, period.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)):
            continue
        if published is None or not period <= published <= day:
            continue
        supplemental_rows.append(dict(
            {field: _number(row.get(field)) for field in (*_FIELDS.values(), "attributable_profit")},
            report_date=period.isoformat(), available_date=published.isoformat()))
        if period in usable:
            duplicates.add(period)
        usable[period] = row
    result["supplemental_rows"] = supplemental_rows
    if not usable:
        result["missing_tags"] = ["operating_disclosure"]
        return result
    latest = max(usable)
    result.update(period=latest.isoformat(), available_date=_day(usable[latest]["available_date"]).isoformat())
    if latest < _minimum_period(day):
        result["missing_tags"] = ["operating_stale"]
        return result
    prior_day = date(latest.year - 1, latest.month, latest.day)
    if latest in duplicates or prior_day in duplicates:
        result["missing_tags"] = ["operating_duplicate"]
        return result
    current, prior = usable[latest], usable.get(prior_day, {})
    missing, failed, weak, metrics = [], [], [], {}
    values = {}
    for field in _FIELDS.values():
        current_value, prior_value = _number(current.get(field)), _number(prior.get(field))
        values[field] = current_value, prior_value
        metrics[field] = current_value
        if current_value is None or prior_value is None:
            missing.append("operating_" + field)
    for field in ("revenue", "net_profit", "deducted_profit"):
        cur, prev = values[field]
        yoy = None
        if cur is not None and prev is not None and prev > 0:
            yoy = (cur / prev - 1.0) * 100.0
            if not math.isfinite(yoy):
                yoy = None
                missing.append("operating_" + field + "_yoy")
        elif prev is not None and prev <= 0:
            weak.append(field + "_nonpositive_base")
        metrics[field + "_yoy"] = yoy
        if cur is not None and cur <= 0:
            failed.append(field + "_nonpositive")
        if yoy is not None and field != "revenue" and yoy < limits[0] - 1e-10:
            failed.append(field + "_severe_contraction")
        elif yoy is not None and yoy < -limits[3] - 1e-10:
            weak.append(field + "_declining")
    for field, maximum in (("gross_margin", limits[1]), ("net_margin", limits[2])):
        cur, prev = values[field]
        delta = cur - prev if cur is not None and prev is not None else None
        if delta is not None and not math.isfinite(delta):
            delta = None
            missing.append("operating_" + field + "_change")
        metrics[field + "_change_pp"] = delta
        if delta is not None and delta < -maximum - 1e-10:
            weak.append(field + "_contracting")
    cash, prior_cash = values["operating_cashflow"]
    cash_change = cash - prior_cash if cash is not None and prior_cash is not None else None
    if cash_change is not None and not math.isfinite(cash_change):
        cash_change = None
        missing.append("operating_cashflow_change")
    metrics["operating_cashflow_change"] = cash_change
    metrics["operating_cashflow_yoy"] = (
        (cash / prior_cash - 1) * 100 if cash is not None and prior_cash is not None and prior_cash > 0 else None)
    if metrics["operating_cashflow_yoy"] is not None and not math.isfinite(metrics["operating_cashflow_yoy"]):
        metrics["operating_cashflow_yoy"] = None
    if cash is not None and cash < 0:
        weak.append("operating_cashflow_negative")
    profit = values["net_profit"][0]
    conversion = cash / profit if cash is not None and profit is not None and profit > 0 else None
    metrics["cash_conversion"] = conversion if conversion is not None and math.isfinite(conversion) else None
    cash_yoy = metrics["operating_cashflow_yoy"]
    if cash_change is not None and cash_change < 0 and (
            cash_yoy is None or cash_yoy < -limits[4] - 1e-10
            or metrics["cash_conversion"] is None or metrics["cash_conversion"] < limits[5]):
        weak.append("operating_cashflow_declining")
    metrics.update(_supplemental_trends(usable, duplicates, latest))
    # Simultaneous deterioration in sales, earnings and operating cash is stronger evidence.
    if (metrics["revenue_yoy"] is not None and metrics["revenue_yoy"] < -1e-10
            and metrics["net_profit_yoy"] is not None and metrics["net_profit_yoy"] < -1e-10
            and cash_change is not None and cash_change < 0):
        failed.append("sales_profit_cash_deteriorating")
    result.update(metrics=metrics, missing_tags=list(dict.fromkeys(missing)),
                  reasons=list(dict.fromkeys(failed + weak)),
                  status="failed" if failed else "missing" if missing else "weak" if weak else "verified")
    return result


def summarize_operating(evidence, status=None):
    """Short factual explanation; unknown evidence never receives a health label."""
    evidence = evidence if isinstance(evidence, dict) else {}
    state = status or evidence.get("status", "missing")
    labels = {"verified": "经营证据通过", "weak": "经营走弱·观察", "failed": "经营恶化·否决",
              "missing": "经营证据待核验", "not_required": "未要求近期经营核验"}
    parts = [labels.get(state, labels["missing"])]
    if state in {"verified", "weak", "failed"}:
        parts.append(str(evidence.get("period") or "报告期未知"))
        metrics = evidence.get("metrics", {})
        for field, label in (("revenue_yoy", "收入"), ("net_profit_yoy", "净利"),
                             ("deducted_profit_yoy", "扣非"), ("operating_cashflow_yoy", "经营现金流")):
            value = _number(metrics.get(field))
            if value is not None:
                parts.append(f"{label}同比{value:+.1f}%")
        for field, label in (("gross_margin_change_pp", "毛利率"), ("net_margin_change_pp", "净利率")):
            value = _number(metrics.get(field))
            if value is not None:
                parts.append(f"{label}{value:+.1f}个百分点")
    return "；".join(parts)


def validated_operating_status(evidence, code, as_of):
    """Validate evidence identity, decision date, disclosure and verified metrics."""
    day = _day(as_of)
    if day is None or not isinstance(evidence, dict):
        return "missing"
    if str(evidence.get("code", "")) != str(code).zfill(6) or _day(evidence.get("as_of")) != day:
        return "missing"
    status = evidence.get("status")
    if status not in {"verified", "weak", "failed"}:
        return "missing"
    period, published = _day(evidence.get("period")), _day(evidence.get("available_date"))
    if period is None or published is None or not _minimum_period(day) <= period <= published <= day:
        return "missing"
    if (period.month, period.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)):
        return "missing"
    if status == "verified":
        metrics = evidence.get("metrics")
        required = tuple(_FIELDS.values()) + (
            "revenue_yoy", "net_profit_yoy", "deducted_profit_yoy",
            "gross_margin_change_pp", "net_margin_change_pp", "operating_cashflow_change")
        if (not isinstance(metrics, dict) or evidence.get("missing_tags") or evidence.get("reasons")
                or any(_number(metrics.get(field)) is None for field in required)):
            return "missing"
    return status


def validated_operating_ttm(evidence, code, as_of):
    """Recompute TTM operands and their disclosures before using them for valuation.

    Legacy evidence without operand rows can still support its core trend checks,
    but cannot establish normalized cyclical valuation.
    """
    if validated_operating_status(evidence, code, as_of) not in {"verified", "weak"}:
        return None
    rows = evidence.get("supplemental_rows")
    if not isinstance(rows, list) or not rows:
        return None
    checked = evaluate_operating_trend(rows, as_of)
    if (checked["period"] != evidence.get("period")
            or checked["available_date"] != evidence.get("available_date")):
        return None
    actual, claimed = checked["metrics"], evidence.get("metrics", {})
    for field in ("net_profit_ttm", "attributable_profit_ttm"):
        original = claimed.get(field)
        value, recomputed = _number(original), _number(actual.get(field))
        if value is None:
            if original is not None or recomputed is not None:
                return None
        elif recomputed is None or not math.isclose(value, recomputed, rel_tol=1e-10, abs_tol=1e-8):
            return None
    return dict(evidence, metrics=dict(claimed, **{field: actual.get(field)
                for field in ("net_profit_ttm", "attributable_profit_ttm")}))


def fetch_operating_trend(code, as_of, profit_yoy_min=-10.0,
                          gross_margin_drop_max=3.0, net_margin_drop_max=2.0,
                          request_get=None, *, yoy_decline_tolerance=3.0,
                          cashflow_decline_tolerance=3.0, cash_conversion_min=.8):
    """One bounded read-only request; fetch/schema failures remain missing evidence."""
    symbol = str(code).strip()
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("code must be six digits")
    if _day(as_of) is None:
        raise ValueError("as_of must be a valid decision date")
    market = "sh" if symbol.startswith("6") else "sz" if symbol.startswith(("0", "3")) else "bj"
    try:
        if request_get is None:
            import requests
            request_get = requests.get
        response = request_get(SOURCE_URL, params={"paperCode": market + symbol,
            "source": "gjzb", "type": "0", "page": "1", "num": "12"}, timeout=15)
        response.raise_for_status()
        rows = parse_summary(response.json())
    except Exception as exc:
        result = evaluate_operating_trend([], as_of, profit_yoy_min,
                                          gross_margin_drop_max, net_margin_drop_max,
                                          yoy_decline_tolerance=yoy_decline_tolerance,
                                          cashflow_decline_tolerance=cashflow_decline_tolerance,
                                          cash_conversion_min=cash_conversion_min)
        result["missing_tags"].append("operating_fetch_error")
        result["error_type"] = type(exc).__name__
        result["code"] = symbol
        return result
    result = evaluate_operating_trend(rows, as_of, profit_yoy_min,
                                      gross_margin_drop_max, net_margin_drop_max,
                                          yoy_decline_tolerance=yoy_decline_tolerance,
                                          cashflow_decline_tolerance=cashflow_decline_tolerance,
                                          cash_conversion_min=cash_conversion_min)
    result["code"] = symbol
    return result
