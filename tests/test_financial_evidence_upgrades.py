"""No-network tests for announcement dates and financial risk evidence."""
import copy
import json
import unittest
from unittest.mock import Mock
from src.fundamental_quality import fetch_annual_quality
from src.financial_risk import (parse_balance, combine_financial_rows, evaluate_financial_risk,
                                fetch_financial_risk, validated_financial_risk_status)
from src.recent_operating import parse_summary


def payload(period="20260331", published="20260420", source="company", balance=False, values=None):
    if balance:
        fields = {"资产总计": 1000, "负债合计": 400, "商誉": 10}
        fields.update(values or {})
        items = [dict(item_title=k, item_value=v) for k, v in fields.items()]
    else:
        fields = dict(NETPROFIT=100, NPCUT=90, MANANETR=110, ROEWEIGHTED=12)
        fields.update(values or {})
        items = [dict(item_field=k, item_value=v, item_group_no=1) for k, v in fields.items()]
    return {"result": {"data": {"report_list": {period: dict(rType="合并期末", rCurrency="CNY",
            publish_date=published, data_source=source, data=items)}}}}


def response(body):
    result = Mock()
    result.json.return_value = body
    return result


def canonical(**kwargs):
    row = dict(total_assets=1000, total_liabilities=400, goodwill=10, net_profit=100,
               deducted_profit=90, report_date="2026-03-31", available_date="2026-04-20",
               source="test", currency="CNY", statement_basis="consolidated")
    row.update(kwargs)
    return row


def abstract():
    labels = {"净资产收益率(ROE)": 12, "扣非净利润": 90, "净利润": 100, "经营现金流量净额": 110}
    return [dict({"选项": "常用指标", "指标": label}, **{f"{y}1231": value for y in (2022, 2023, 2024, 2025)})
            for label, value in labels.items()]


class FinancialEvidenceUpgradesTests(unittest.TestCase):
    def test_actual_early_annual_disclosure_priority(self):
        body = {"result": {"data": {"report_list": {}}}}
        for year in (2023, 2024, 2025):
            body["result"]["data"]["report_list"].update(payload(f"{year}1231", f"{year+1}0320")["result"]["data"]["report_list"])
        ak = Mock()
        result = fetch_annual_quality("600004", "2026-04-01", ak_client=ak,
                                      request_get=Mock(return_value=response(body)))
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["metrics"]["expected_years"], [2023, 2024, 2025])
        self.assertFalse(result["availability_assumed"])
        ak.stock_financial_abstract.assert_not_called()

    def test_raw_failure_falls_back_and_preserves_assumption(self):
        ak = Mock()
        ak.stock_financial_abstract.return_value = abstract()
        result = fetch_annual_quality("600004", "2026-06-12", ak_client=ak,
                                      request_get=Mock(side_effect=TimeoutError))
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["availability_assumed"])
        self.assertTrue(all(r["availability_assumed"] for r in result["annual_rows"]))
        self.assertEqual(result["raw_fetch_error"], "TimeoutError")

    def test_known_raw_violation_not_hidden_by_fallback(self):
        ak = Mock()
        result = fetch_annual_quality("600004", "2026-06-12", ak_client=ak,
            request_get=Mock(return_value=response(payload("20251231", "20260320", values=dict(NPCUT=-1)))))
        self.assertEqual(result["status"], "failed")
        ak.stock_financial_abstract.assert_not_called()

    def test_actual_future_date_overrides_fallback_assumption(self):
        ak = Mock()
        ak.stock_financial_abstract.return_value = abstract()
        result = fetch_annual_quality("600004", "2026-06-12", ak_client=ak,
            request_get=Mock(return_value=response(payload("20251231", "20260620"))))
        self.assertEqual(result["status"], "partial")
        self.assertIn("not_available:2025", result["missing_tags"])

    def test_risk_ratios_purity_serialization_and_validation(self):
        rows = [canonical()]
        original = copy.deepcopy(rows)
        result = evaluate_financial_risk(rows, "2026-06-12")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["metrics"]["debt_ratio"], 40)
        self.assertAlmostEqual(result["metrics"]["goodwill_ratio"], 10 / 600 * 100)
        self.assertEqual(result["metrics"]["goodwill_to_assets"], 1)
        self.assertEqual(result["metrics"]["net_assets"], 600)
        self.assertEqual(result["metrics"]["deducted_profit_ratio"], .9)
        self.assertEqual(rows, original)
        json.dumps(result, allow_nan=False)
        result["code"] = "600004"
        self.assertEqual(validated_financial_risk_status(result, "600004", "2026-06-12"), "verified")
        self.assertEqual(validated_financial_risk_status(result, "600005", "2026-06-12"), "missing")
        result["metrics"]["debt_ratio"] = 0
        self.assertEqual(validated_financial_risk_status(result, "600004", "2026-06-12"), "missing")

    def test_missing_and_known_violations(self):
        for field in ("total_assets", "total_liabilities", "goodwill", "net_profit", "deducted_profit"):
            for value in (None, True, float("nan")):
                result = evaluate_financial_risk([canonical(**{field: value})], "2026-06-12")
                self.assertEqual(result["status"], "missing")
        for kwargs in (dict(total_liabilities=701), dict(goodwill=201), dict(deducted_profit=49), dict(net_profit=-1)):
            self.assertEqual(evaluate_financial_risk([canonical(**kwargs)], "2026-06-12")["status"], "failed")
        self.assertEqual(evaluate_financial_risk([canonical(total_liabilities=900, goodwill=None)], "2026-06-12")["status"], "failed")
        self.assertEqual(evaluate_financial_risk([canonical(goodwill=0)], "2026-06-12")["status"], "verified")
        self.assertEqual(evaluate_financial_risk([canonical(goodwill=120)], "2026-06-12")["status"], "verified")
        self.assertEqual(evaluate_financial_risk([canonical(goodwill=120.01)], "2026-06-12")["status"], "failed")
        self.assertIn("net_assets_nonpositive", evaluate_financial_risk(
            [canonical(total_liabilities=1000)], "2026-06-12")["reasons"])

    def test_freshness_future_and_duplicate(self):
        for kwargs in (dict(available_date="2026-06-13"), dict(available_date=None),
                       dict(report_date="2025-09-30", available_date="2025-10-20")):
            self.assertEqual(evaluate_financial_risk([canonical(**kwargs)], "2026-06-12")["status"], "missing")
        self.assertEqual(evaluate_financial_risk([canonical(), canonical()], "2026-06-12")["status"], "missing")

    def test_adapter_exact_fields_same_period_source_and_date(self):
        balance = parse_balance(payload(balance=True))
        summary = parse_summary(payload())
        self.assertEqual(evaluate_financial_risk(combine_financial_rows(balance, summary), "2026-06-12")["status"], "verified")
        for key, value in (("report_date", "20251231"), ("available_date", "20260421"),
                           ("data_source", "other"), ("source", "other")):
            different = [dict(summary[0], **{key: value})]
            self.assertEqual(evaluate_financial_risk(combine_financial_rows(balance, different), "2026-06-12")["status"], "missing")
        body = payload(balance=True)
        items = body["result"]["data"]["report_list"]["20260331"]["data"]
        items.append(dict(item_title="商誉", item_value=0))
        self.assertIsNone(parse_balance(body)[0]["goodwill"])

    def test_optional_attributable_profit_exact_label_and_duplicate_safety(self):
        body = payload("20251231", "20260320")
        items = body["result"]["data"]["report_list"]["20251231"]["data"]
        items.append(dict(item_title="归母净利润", item_field="UNVERIFIED_CODE", item_value=80, item_group_no=1))
        parsed = parse_summary(body)[0]
        self.assertEqual(parsed["attributable_profit"], 80)
        self.assertEqual(parsed["net_profit"], 100)
        items.append(dict(item_title="归属于母公司股东的净利润", item_value=81, item_group_no=1))
        self.assertIsNone(parse_summary(body)[0]["attributable_profit"])
        ak = Mock()
        ak.stock_financial_abstract.return_value = abstract() + [dict({"选项": "常用指标", "指标": "归母净利润"},
            **{f"{y}1231": 80 for y in (2023, 2024, 2025)})]
        result = fetch_annual_quality("600004", "2026-06-12", ak_client=ak)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["metrics"]["net_profit_sum"], 300)
        self.assertTrue(all(r["attributable_profit"] == 80 for r in result["annual_rows"]))

    def test_failed_risk_survives_summary_fetch_failure_and_validates(self):
        getter = Mock(side_effect=[response(payload(balance=True, values={"负债合计": 900})), TimeoutError()])
        result = fetch_financial_risk("600004", "2026-06-12", request_get=getter)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(validated_financial_risk_status(result, "600004", "2026-06-12"), "failed")
        wrong = dict(result, reasons=["invented_failure"])
        self.assertEqual(validated_financial_risk_status(wrong, "600004", "2026-06-12"), "missing")
        wrong = dict(result, currency="USD")
        self.assertEqual(validated_financial_risk_status(wrong, "600004", "2026-06-12"), "missing")

    def test_fetch_bounded_no_network(self):
        getter = Mock(side_effect=[response(payload(balance=True)), response(payload())])
        result = fetch_financial_risk("600004", "2026-06-12", request_get=getter)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(getter.call_count, 2)
        self.assertTrue(all(call.kwargs["timeout"] == 15 for call in getter.call_args_list))
        result = fetch_financial_risk("600004", "2026-06-12", request_get=Mock(side_effect=TimeoutError))
        self.assertEqual(result["status"], "missing")
        self.assertIn("financial_risk_fetch_error", result["missing_tags"])


if __name__ == "__main__":
    unittest.main()
