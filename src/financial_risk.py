"""Offline financial-risk evaluator and bounded Sina adapter.

Balance fields use exact 新浪资产负债表 item_title: 资产总计/负债合计/商誉.
Summary uses consolidated NETPROFIT (including minority interests) and NPCUT;
debt_ratio=liabilities/assets*100; goodwill_ratio=goodwill/(assets-liabilities)*100;
goodwill_to_assets=goodwill/assets*100. These are percentage points;
deducted_profit_ratio is a fraction. Absence of 商誉 is unknown, never zero.
"""
import re
from .recent_operating import SOURCE_URL, _day, _number, _minimum_period, parse_summary


def parse_balance(payload):
    reports = payload.get("result", {}).get("data", {}).get("report_list", {})
    if not isinstance(reports, dict):
        return []
    fields = {"资产总计": "total_assets", "负债合计": "total_liabilities", "商誉": "goodwill"}
    rows = []
    for period, report in reports.items():
        if not isinstance(report, dict) or report.get("rType") != "合并期末" or report.get("rCurrency") != "CNY":
            continue
        row = dict(report_date=period, available_date=report.get("publish_date"),
                   source=SOURCE_URL, report_source="fzb", data_source=report.get("data_source"),
                   statement_basis="consolidated", currency="CNY")
        seen = set()
        for item in report.get("data", []):
            if not isinstance(item, dict):
                continue
            field = fields.get(item.get("item_title"))
            if field:
                row[field] = None if field in seen else _number(item.get("item_value"))
                seen.add(field)
        rows.append(row)
    return rows


def evaluate_financial_risk(rows, as_of, debt_max=70., goodwill_max=20.,
                            deducted_ratio_min=.5):
    """Evaluate latest disclosed consolidated CNY row; all ratios use same row.

    Required canonical fields: total_assets, total_liabilities, goodwill,
    net_profit, deducted_profit, report_date, available_date, source,
    statement_basis='consolidated', currency='CNY'. Stale/missing evidence is
    missing; known violations dominate missing dimensions.
    """
    day = _day(as_of)
    limits = [_number(v) for v in (debt_max, goodwill_max, deducted_ratio_min)]
    if day is None or any(v is None or v < 0 for v in limits):
        raise ValueError("valid decision date and finite nonnegative ratio thresholds required")
    result = dict(status="missing", as_of=day.isoformat(), period=None, available_date=None,
                  source=SOURCE_URL, metrics={}, missing_tags=[], reasons=[],
                  thresholds=dict(debt_max=limits[0], goodwill_max=limits[1],
                                  deducted_ratio_min=limits[2]))
    usable = {}
    duplicates = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        period, available = _day(row.get("report_date")), _day(row.get("available_date"))
        if (period is None or available is None or not period <= available <= day
                or (period.month, period.day) not in ((3, 31), (6, 30), (9, 30), (12, 31))):
            continue
        if period in usable:
            duplicates.add(period)
        usable[period] = row
    if not usable:
        result["missing_tags"] = ["financial_risk_disclosure"]
        return result
    period = max(usable)
    row = usable[period]
    result.update(period=period.isoformat(), available_date=_day(row["available_date"]).isoformat())
    if "merge_diagnostics" in row:
        result["merge_diagnostics"] = row["merge_diagnostics"]
    if period < _minimum_period(day) or period in duplicates:
        result["missing_tags"] = ["financial_risk_stale" if period < _minimum_period(day) else "financial_risk_duplicate"]
        return result
    if row.get("statement_basis") != "consolidated" or row.get("currency") != "CNY" or not row.get("source"):
        result["missing_tags"] = ["financial_risk_basis"]
        return result
    result["source"] = row["source"]
    result["data_source"] = row.get("data_source")
    result["statement_basis"] = row["statement_basis"]
    result["currency"] = row["currency"]
    fields = ("total_assets", "total_liabilities", "goodwill", "net_profit", "deducted_profit")
    metrics = {field: _number(row.get(field)) for field in fields}
    tags = ["financial_risk_" + field for field in fields if metrics[field] is None]
    failed = []
    for field in ("total_assets", "net_profit", "deducted_profit"):
        if metrics[field] is not None and metrics[field] <= 0:
            failed.append(field + "_nonpositive")
    for field in ("total_liabilities", "goodwill"):
        if metrics[field] is not None and metrics[field] < 0:
            tags.append("financial_risk_invalid_" + field)
            metrics[field] = None
    assets, liabilities = metrics["total_assets"], metrics["total_liabilities"]
    net_assets = _number(assets - liabilities) if assets is not None and liabilities is not None else None
    metrics["net_assets"] = net_assets
    if net_assets is not None and net_assets <= 0:
        failed.append("net_assets_nonpositive")
    metrics["goodwill_to_assets"] = (_number(metrics["goodwill"] / assets * 100)
        if metrics["goodwill"] is not None and assets is not None and assets > 0 else None)
    for ratio, numerator, denominator, threshold, maximum in (
            ("debt_ratio", "total_liabilities", "total_assets", limits[0], True),
            ("goodwill_ratio", "goodwill", "net_assets", limits[1], True),
            ("deducted_profit_ratio", "deducted_profit", "net_profit", limits[2], False)):
        num, den = metrics[numerator], metrics[denominator]
        value = _number(num / den * (100 if maximum else 1)) if num is not None and den is not None and den > 0 else None
        metrics[ratio] = value
        if value is None:
            tags.append("financial_risk_" + ratio)
        elif (value > threshold + 1e-10 if maximum else value < threshold - 1e-10):
            failed.append(ratio + "_outside_limit")
    result.update(status="failed" if failed else "missing" if tags else "verified",
                  metrics=metrics, missing_tags=list(dict.fromkeys(tags)), reasons=failed)
    return result


def _compatible_report_origin(balance, summary):
    """Only the observed Sina fzb=定期报告 / gjzb=其他 pair is an alias.

    data_source classifies the provider's report, not its endpoint. Do not
    generalize this exception to another provider, report kind or label pair.
    """
    if not balance.get("source") or balance.get("source") != summary.get("source"):
        return False
    origin = balance.get("data_source")
    if origin and origin == summary.get("data_source"):
        return True
    return (balance.get("source") == SOURCE_URL
            and balance.get("report_source") == "fzb"
            and summary.get("report_source") == "gjzb"
            and origin == "定期报告" and summary.get("data_source") == "其他")


def combine_financial_rows(balance_rows, summary_rows):
    """Join one matching report, preserving the raw provenance of both tables."""
    rows = []
    for balance in balance_rows:
        period, published = _day(balance.get("report_date")), _day(balance.get("available_date"))
        same_period = [s for s in summary_rows
                       if period is not None and _day(s.get("report_date")) == period]
        matches = [s for s in same_period
                   if published is not None and _day(s.get("available_date")) == published
                   and all(s.get(k) == balance.get(k) for k in
                           ("code", "currency", "statement_basis"))
                   and _compatible_report_origin(balance, s)]
        row = dict(balance)
        provenance_keys = ("code", "source", "report_source", "data_source", "report_date",
                           "available_date", "currency", "statement_basis")
        row["merge_diagnostics"] = {
            "status": "matched" if len(matches) == 1 else "ambiguous" if matches else "unmatched",
            "matching_reports": len(matches),
            "balance": {k: balance.get(k) for k in provenance_keys},
            "summary_candidates": [{k: s.get(k) for k in provenance_keys} for s in same_period],
        }
        if len(matches) == 1:
            row.update(net_profit=matches[0].get("net_profit"),
                       deducted_profit=matches[0].get("deducted_profit"))
            row["merge_diagnostics"]["origin_match"] = (
                "exact" if balance.get("data_source") == matches[0].get("data_source")
                else "sina_periodic_summary")
        rows.append(row)
    return rows


def fetch_financial_risk(code, as_of, debt_max=70., goodwill_max=20.,
                         deducted_ratio_min=.5, request_get=None):
    """Two bounded requests (15 seconds each); dependency injection for offline tests."""
    symbol = str(code).strip()
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("code must be six digits")
    # Validate configuration before a network request.
    evaluate_financial_risk([], as_of, debt_max, goodwill_max, deducted_ratio_min)
    market = "sh" if symbol.startswith("6") else "sz" if symbol.startswith(("0", "3")) else "bj"
    rows, error = [], None
    try:
        if request_get is None:
            import requests
            request_get = requests.get
        parsed = {}
        for source, parser in (("fzb", parse_balance), ("gjzb", parse_summary)):
            response = request_get(SOURCE_URL, params=dict(paperCode=market + symbol,
                source=source, type="0", page="1", num="12"), timeout=15)
            response.raise_for_status()
            parsed[source] = [dict(row, code=symbol) for row in parser(response.json())]
            if source == "fzb":
                rows = parsed[source]
        rows = combine_financial_rows(parsed["fzb"], parsed["gjzb"])
    except Exception as exc:
        error = type(exc).__name__
    result = evaluate_financial_risk(rows, as_of, debt_max, goodwill_max, deducted_ratio_min)
    result["code"] = symbol
    if error:
        result["missing_tags"].append("financial_risk_fetch_error")
        result["error_type"] = error
    return result


def validated_financial_risk_status(evidence, code, as_of):
    """Identity/disclosure/freshness and recomputed metrics validation for main side."""
    day = _day(as_of)
    if (day is None or not isinstance(evidence, dict)
            or evidence.get("code") != str(code).zfill(6) or _day(evidence.get("as_of")) != day):
        return "missing"
    period, available = _day(evidence.get("period")), _day(evidence.get("available_date"))
    if period is None or available is None or not _minimum_period(day) <= period <= available <= day:
        return "missing"
    status = evidence.get("status")
    if status not in {"verified", "failed"}:
        return "missing"
    if status == "verified" and (evidence.get("missing_tags") or evidence.get("reasons")):
        return "missing"
    row = dict(evidence.get("metrics") or {}, report_date=period, available_date=available,
               source=evidence.get("source"), statement_basis=evidence.get("statement_basis"),
               currency=evidence.get("currency"))
    try:
        checked = evaluate_financial_risk([row], day, **evidence.get("thresholds", {}))
    except (ValueError, TypeError):
        return "missing"
    if status == "failed":
        return "failed" if checked["status"] == "failed" and checked["reasons"] == evidence.get("reasons") else "missing"
    return "verified" if checked["status"] == "verified" and all(
        _number(evidence["metrics"].get(k)) == checked["metrics"][k]
        for k in ("debt_ratio", "goodwill_ratio", "deducted_profit_ratio")) else "missing"
