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


def describe_operating_signals(evidence):
    """Descriptive only; never used by admission or status validation.

    Priority: failed -> severe; missing -> unknown; >=2 deteriorating canonical
    fields -> multiple; one -> single; only nonpositive prior bases with positive
    current values -> recovering; no signals -> stable. Cumulative and quarterly
    signals for the SAME field count once. Recovery does not override declines.
    """
    reasons = list(evidence.get("reasons") or [])
    dimensions = sorted({field for field in _FIELDS.values()
                         if any(reason.startswith(field + "_")
                                and not reason.endswith("_nonpositive_base")
                                for reason in reasons)})
    bases = [reason[:-len("_nonpositive_base")] for reason in reasons
             if reason.endswith("_nonpositive_base")]
    if evidence.get("status") == "failed":
        level, label, rule = "severe_deterioration", "严重恶化", "existing_hard_failure"
    elif evidence.get("status") not in {"verified", "weak"}:
        level, label, rule = "unknown", "证据不足", "incomplete_or_unavailable_evidence"
    elif len(dimensions) >= 2:
        level, label, rule = "multiple_deterioration", "多项恶化", "at_least_two_distinct_fields"
    elif dimensions:
        level, label, rule = "single_fluctuation", "单项波动", "one_distinct_field"
    elif bases and all((_number(evidence.get("metrics", {}).get(field)) or 0) > 0 for field in bases):
        level, label, rule = "recovering", "恢复中", "nonpositive_prior_base_positive_current_without_declines"
    elif reasons:
        level, label, rule = "unknown", "证据不足", "unclassified_signal"
    else:
        level, label, rule = "stable", "未见明显走弱", "no_existing_adverse_signals"
    # This secondary label does not replace the main deterioration level. It
    # requires *only* cumulative sales/profit declines and strictly positive,
    # finite latest-quarter YoY for every affected field; no other weak reason
    # (cash, margins, nonpositive base or quarterly decline) may be present.
    cumulative_reasons = {field + "_declining": field
                          for field in ("revenue", "net_profit", "deducted_profit")}
    recovery_evidence = None
    if (evidence.get("status") == "weak" and not evidence.get("missing_tags")
            and reasons and all(reason in cumulative_reasons for reason in reasons)):
        recovering_fields = sorted({cumulative_reasons[reason] for reason in reasons})
        quarter_yoy = {field: _number(evidence.get("metrics", {}).get(field + "_single_quarter_yoy"))
                       for field in recovering_fields}
        if all(value is not None and value > 0 for value in quarter_yoy.values()):
            recovery_evidence = dict(level="recovering_latest_quarter", label="累计承压·最新单季恢复",
                rule="only_cumulative_declines_all_affected_quarter_yoy_positive",
                dimensions=recovering_fields, single_quarter_yoy=quarter_yoy)
    return dict(level=level, label=label, rule=rule, dimensions=dimensions, reason_codes=reasons,
                recovery_evidence=recovery_evidence)


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
    result["signal_profile"] = describe_operating_signals(result)
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
    # 最新单季度同比用于揭示累计同比可能掩盖的边际恶化。
    #
    # 口径修正（2026-10-16）：单季信号**只记录进 reasons，不再参与 status 判定**。
    # 原实现把它追加进 weak，而 status = "weak" if weak else "verified"（任一命中即降级），
    # 于是单季信号事实上成了硬否决 —— 与下方注释承诺的「不作为硬否决」自相矛盾。
    # 实证代价（Actions run #108，2963 只 → 候选 11 → 正式 0）：11 只候选的
    # operating_weak 覆盖率 100%，其中 operating_cashflow 字段命中率同样 100%。
    # 典型误杀：国电南瑞累计经营现金流同比 +14.7%、青岛啤酒 +14.7%、兔宝宝 +98.3%、
    # 吉比特 +18.2%（净利 +55.6%），均为累计改善，却因单季 −32.0% / −16.7% /
    # −17.9% / −19.8% 被判weak；中国汽研更仅凭单季 −3.1%（刚过阈值）被判 weak。
    #
    # 为什么单季口径不适合做判定：单季由 YTD 差分而来（Qn = YTDn − YTD(n−1)），
    # 而现金流本身强季节性（Q1 集中收款、Q4 集中结算），单季负增长在大盘下跌期
    # 是全市场共性，不含个股信息；反之低基数/财报修订也会放大单季波动。
    # 累计口径已有cash_conversion豁免（第 285-288 行），单季信号保留在 reasons 中
    # 供 describe/summarize 与人工复核查看，但不再单独把候选打成 pending。
    latest_quarter_flags: list[str] = []
    for field in ("revenue", "net_profit", "deducted_profit"):
        q_yoy = metrics.get(field + "_single_quarter_yoy")
        if q_yoy is not None and q_yoy < -limits[3] - 1e-10:
            latest_quarter_flags.append(field + "_latest_quarter_declining")
    cash_q_yoy = metrics.get("operating_cashflow_single_quarter_yoy")
    if cash_q_yoy is not None and cash_q_yoy < -limits[4] - 1e-10:
        latest_quarter_flags.append("operating_cashflow_latest_quarter_declining")
    # Simultaneous deterioration in sales, earnings and operating cash is stronger evidence.
    if (metrics["revenue_yoy"] is not None and metrics["revenue_yoy"] < -1e-10
            and metrics["net_profit_yoy"] is not None and metrics["net_profit_yoy"] < -1e-10
            and cash_change is not None and cash_change < 0):
        failed.append("sales_profit_cash_deteriorating")
    result.update(metrics=metrics, missing_tags=list(dict.fromkeys(missing)),
                  reasons=list(dict.fromkeys(failed + weak + latest_quarter_flags)),
                  latest_quarter_signals=list(latest_quarter_flags),
                  status="failed" if failed else "missing" if missing else "weak" if weak else "verified")
    result["signal_profile"] = describe_operating_signals(result)
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
        for field, label in (("revenue_single_quarter_yoy", "最新单季收入"),
                             ("net_profit_single_quarter_yoy", "最新单季净利"),
                             ("deducted_profit_single_quarter_yoy", "最新单季扣非"),
                             ("operating_cashflow_single_quarter_yoy", "最新单季经营现金流")):
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
