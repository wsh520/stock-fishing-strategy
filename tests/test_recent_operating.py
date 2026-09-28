"""Offline contract and decision tests for current operating evidence."""
import copy
import json
import unittest
from unittest.mock import Mock

from src.recent_operating import evaluate_operating_trend, fetch_operating_trend, parse_summary


def healthy_rows():
    base = dict(revenue=1000., net_profit=100., deducted_profit=90.,
                operating_cashflow=110., gross_margin=30., net_margin=10.)
    return [dict(base, report_date="2025-03-31", available_date="2025-04-20"),
            dict(base, report_date="2026-03-31", available_date="2026-04-20")]


def evaluate(rows=None, **kwargs):
    return evaluate_operating_trend(healthy_rows() if rows is None else rows, "2026-06-12", **kwargs)


class RecentOperatingTests(unittest.TestCase):
    def test_healthy_same_period_and_purity(self):
        rows = healthy_rows()
        original = copy.deepcopy(rows)
        result = evaluate(rows)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["period"], "2026-03-31")
        self.assertEqual(result["metrics"]["revenue_yoy"], 0)
        self.assertEqual(rows, original)
        json.dumps(result, allow_nan=False)

    def test_mild_profit_contraction_is_observation(self):
        rows = healthy_rows()
        rows[-1]["net_profit"] = 95
        result = evaluate(rows)
        self.assertEqual(result["status"], "weak")
        self.assertIn("net_profit_declining", result["reasons"])
        self.assertEqual(result["missing_tags"], [])

    def test_severe_profit_contraction_and_boundary(self):
        rows = healthy_rows()
        rows[-1]["net_profit"] = 90
        self.assertEqual(evaluate(rows)["status"], "weak")
        rows[-1]["net_profit"] = 89.99
        self.assertEqual(evaluate(rows)["status"], "failed")
        rows[-1]["net_profit"] = 100
        rows[-1]["deducted_profit"] = 60
        self.assertIn("deducted_profit_severe_contraction", evaluate(rows)["reasons"])

    def test_cashflow_seasonality_uses_same_quarter(self):
        rows = healthy_rows()
        rows.insert(1, dict(rows[0], report_date="2025-12-31", available_date="2026-03-20",
                            operating_cashflow=1000.))
        self.assertEqual(evaluate(rows)["status"], "verified")
        rows[-1]["operating_cashflow"] = 100
        result = evaluate(rows)
        self.assertEqual(result["status"], "weak")
        self.assertAlmostEqual(result["metrics"]["operating_cashflow_yoy"], -100 / 11)

    def test_margin_points_not_growth_rate(self):
        rows = healthy_rows()
        rows[-1]["gross_margin"] = 27
        self.assertEqual(evaluate(rows)["status"], "verified")
        rows[-1]["gross_margin"] = 26.9
        result = evaluate(rows)
        self.assertEqual(result["status"], "weak")
        self.assertAlmostEqual(result["metrics"]["gross_margin_change_pp"], -3.1)

    def test_known_failure_dominates_missing_dimension(self):
        rows = healthy_rows()
        rows[-1]["net_profit"] = -1
        rows[-1]["gross_margin"] = None
        result = evaluate(rows)
        self.assertEqual(result["status"], "failed")
        self.assertIn("operating_gross_margin", result["missing_tags"])

    def test_joint_deterioration_is_hard_failure(self):
        rows = healthy_rows()
        rows[-1].update(revenue=990., net_profit=99., operating_cashflow=109.)
        self.assertIn("sales_profit_cash_deteriorating", evaluate(rows)["reasons"])
        self.assertEqual(evaluate(rows)["status"], "failed")

    def test_missing_nonfinite_and_bool_never_verify(self):
        for field in ("revenue", "net_profit", "deducted_profit", "operating_cashflow", "gross_margin", "net_margin"):
            for value in (None, float("nan"), float("inf"), True):
                rows = healthy_rows()
                rows[-1][field] = value
                with self.subTest(field=field, value=value):
                    result = evaluate(rows)
                    self.assertEqual(result["status"], "missing")
                    json.dumps(result, allow_nan=False)

    def test_current_incomplete_report_not_replaced_with_old(self):
        rows = healthy_rows()
        rows[-1]["revenue"] = None
        self.assertEqual(evaluate(rows)["status"], "missing")
        self.assertEqual(evaluate(rows)["period"], "2026-03-31")

    def test_disclosure_date_and_recency(self):
        rows = healthy_rows()
        rows[-1]["available_date"] = "2026-06-13"
        self.assertEqual(evaluate(rows)["status"], "missing")
        rows[-1]["available_date"] = None
        self.assertEqual(evaluate(rows)["status"], "missing")
        rows[-1]["available_date"] = "2026-03-20"  # before report end
        self.assertEqual(evaluate(rows)["status"], "missing")
        self.assertEqual(evaluate_operating_trend(healthy_rows(), "2026-09-01")["status"], "missing")
        self.assertEqual(evaluate_operating_trend(healthy_rows(), "2026-04-20")["status"], "verified")

    def test_missing_same_period_and_duplicates(self):
        self.assertEqual(evaluate(healthy_rows()[1:])["status"], "missing")
        rows = healthy_rows()
        rows.append(dict(rows[-1]))
        self.assertIn("operating_duplicate", evaluate(rows)["missing_tags"])

    def test_nonpositive_bases_do_not_invent_growth(self):
        rows = healthy_rows()
        rows[0]["net_profit"] = -100
        rows[0]["operating_cashflow"] = -110
        result = evaluate(rows)
        self.assertEqual(result["status"], "weak")
        self.assertIsNone(result["metrics"]["net_profit_yoy"])
        self.assertIsNone(result["metrics"]["operating_cashflow_yoy"])
        self.assertEqual(result["metrics"]["operating_cashflow_change"], 220)

    def test_source_exact_fields_and_duplicate_safety(self):
        report = {"rType": "合并期末", "rCurrency": "CNY", "publish_date": "20260420",
                  "data": [{"item_field": "NETPROFIT", "item_value": "123", "item_group_no": 1},
                           {"item_field": "NETPROFIT", "item_value": "999", "item_group_no": 4},
                           {"item_field": "PARENETP", "item_value": "888", "item_group_no": 1}]}
        payload = {"result": {"data": {"report_list": {"20260331": report}}}}
        row = parse_summary(payload)[0]
        self.assertEqual(row["net_profit"], 123)
        self.assertEqual(row["available_date"], "20260420")
        report["data"].append(dict(report["data"][0]))
        self.assertIsNone(parse_summary(payload)[0]["net_profit"])
        report["rType"] = "母公司期末"
        self.assertEqual(parse_summary(payload), [])

    def test_fetch_is_bounded_and_failure_is_missing(self):
        getter = Mock(side_effect=TimeoutError)
        result = fetch_operating_trend("000001", "2026-06-12", request_get=getter)
        self.assertEqual(result["status"], "missing")
        self.assertEqual(getter.call_count, 1)
        self.assertEqual(getter.call_args.kwargs["timeout"], 15)
        self.assertEqual(getter.call_args.kwargs["params"]["paperCode"], "sz000001")
        self.assertIn("operating_fetch_error", result["missing_tags"])
        with self.assertRaises(ValueError):
            fetch_operating_trend("../file", "2026-06-12", request_get=getter)

    def test_final_verification_rejects_wrong_identity_and_incomplete_evidence(self):
        from src.recent_operating import validated_operating_status
        evidence = dict(evaluate(), code="600004")
        self.assertEqual(validated_operating_status(evidence, "600004", "2026-06-12"), "verified")
        for key, value in [("code", "600005"), ("as_of", "2026-06-11"),
                           ("available_date", "2026-06-13"), ("metrics", {}),
                           ("period", "2025-12-31"), ("missing_tags", ["missing"]),
                           ("reasons", ["weak"])]:
            with self.subTest(key=key):
                wrong = dict(evidence, **{key: value})
                self.assertEqual(validated_operating_status(wrong, "600004", "2026-06-12"), "missing")
        self.assertEqual(validated_operating_status({"status": "verified"}, "600004", "2026-06-12"), "missing")

    def test_full_source_fixture_can_verify(self):
        fields = {"revenue": "BIZTOTINCO", "net_profit": "NETPROFIT", "deducted_profit": "NPCUT",
                  "operating_cashflow": "MANANETR", "gross_margin": "SGPMARGIN", "net_margin": "SNPMARGINCONMS"}
        reports = {}
        for row in healthy_rows():
            reports[row["report_date"].replace("-", "")] = {
                "rType": "合并期末", "rCurrency": "CNY", "publish_date": row["available_date"],
                "data": [{"item_field": code, "item_value": row[field], "item_group_no": 1}
                         for field, code in fields.items()]}
        response = Mock()
        response.json.return_value = {"result": {"data": {"report_list": reports}}}
        getter = Mock(return_value=response)
        result = fetch_operating_trend("600004", "2026-06-12", request_get=getter)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["code"], "600004")
        self.assertEqual(result["as_of"], "2026-06-12")
        response.raise_for_status.assert_called_once()


if __name__ == "__main__":
    unittest.main()
