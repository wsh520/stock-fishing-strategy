"""Offline unittest fixtures use the verified AkShare/Sina wide-table schema."""
import copy
import json
import unittest
from unittest.mock import Mock

from src.fundamental_quality import evaluate_annual_quality, fetch_annual_quality


def annual_rows():
    return [dict(year=y, roe=roe, deducted_profit=80.0, net_profit=100.0,
                 operating_cashflow=80.0, available_date=None, report_date=f"{y}-12-31")
            for y, roe in zip((2022, 2023, 2024), (5.0, 10.0, 15.0))]


def sina_fixture():
    # Exact source labels checked against stock_financial_abstract's source and
    # a single 600004 gjzb response. Values are synthetic to test thresholds;
    # 归母净利润 deliberately differs from consolidated 净利润.
    labels = {"净资产收益率(ROE)": [5, 10, 15], "扣非净利润": [8e7] * 3,
              "净利润": [1e8] * 3, "经营现金流量净额": [8e7] * 3,
              "归母净利润": [5e7] * 3}
    result = [dict({"选项": "常用指标", "指标": label},
                   **dict(zip(("20221231", "20231231", "20241231"), values)),
                   **{"20250331": 999}) for label, values in labels.items()]
    result.append({"选项": "盈利能力", "指标": "净资产收益率(ROE)",
                   "20221231": 999, "20231231": 999, "20241231": 999})
    return result


class AnnualQualityTests(unittest.TestCase):
    def evaluate(self, rows=None, **kwargs):
        return evaluate_annual_quality(annual_rows() if rows is None else rows,
                                       kwargs.pop("as_of", "2025-05-01"), **kwargs)

    def test_inclusive_thresholds_and_purity(self):
        rows = annual_rows()
        original = copy.deepcopy(rows)
        result = self.evaluate(rows)
        self.assertEqual(rows, original)
        self.assertEqual(result["status"], "verified")
        self.assertAlmostEqual(result["quality_score"], 50)
        self.assertEqual(result["metrics"]["cash_conversion"], .8)
        self.assertEqual(result["metrics"]["median_roe"], 10)
        json.dumps(result, allow_nan=False)

    def test_stronger_verified_quality_scores_higher(self):
        basic = self.evaluate()
        stronger = annual_rows()
        for row, roe in zip(stronger, (10, 17.5, 20)):
            row.update(roe=roe, operating_cashflow=115)
        middle = self.evaluate(stronger)
        for row, roe in zip(stronger, (15, 25, 30)):
            row.update(roe=roe, operating_cashflow=150)
        best = self.evaluate(stronger)
        self.assertEqual([r["status"] for r in (basic, middle, best)], ["verified"] * 3)
        self.assertAlmostEqual(middle["quality_score"], 75)
        self.assertAlmostEqual(best["quality_score"], 100)
        self.assertLess(basic["quality_score"], middle["quality_score"])
        stronger[0]["deducted_profit"] = -1
        failed = self.evaluate(stronger)
        self.assertEqual(failed["status"], "failed")
        self.assertAlmostEqual(failed["quality_score"], 100)

    def test_missing_dimension_has_zero_score(self):
        rows = annual_rows()
        rows[0]["roe"] = None
        result = self.evaluate(rows)
        self.assertEqual(result["metrics"]["quality_components"]["median_roe"], 0)
        self.assertEqual(result["metrics"]["quality_components"]["min_roe"], 0)
        self.assertAlmostEqual(result["quality_score"], 12.5)

    def test_high_custom_thresholds_score_finitely(self):
        for threshold in (25, 50, 1e308):
            rows = annual_rows()
            for row in rows:
                row.update(roe=threshold, net_profit=1, operating_cashflow=2)
            result = self.evaluate(rows, median_roe_min=threshold, min_roe=threshold,
                                   cash_conversion_min=2)
            self.assertEqual(result["status"], "verified")
            self.assertAlmostEqual(result["quality_score"], 50)
            json.dumps(result, allow_nan=False)
        for row in rows:
            row["roe"] = -1e308
        result = self.evaluate(rows, median_roe_min=1e308, min_roe=1e308)
        self.assertGreaterEqual(result["quality_score"], 0)
        self.assertLessEqual(result["quality_score"], 100)

    def test_fail_each_rule(self):
        for field, values in [("roe", [5, 9, 10]), ("roe", [4, 12, 20]),
                              ("deducted_profit", [80, 0, 80]),
                              ("operating_cashflow", [80, 79, 80])]:
            with self.subTest(field=field, values=values):
                rows = annual_rows()
                for row, value in zip(rows, values):
                    row[field] = value
                self.assertEqual(self.evaluate(rows)["status"], "failed")

    def test_cash_ratio_is_ratio_of_sums(self):
        rows = annual_rows()
        for row, net, cash in zip(rows, (100, 100, 800), (200, 200, 400)):
            row.update(net_profit=net, operating_cashflow=cash)
        self.assertEqual(self.evaluate(rows)["metrics"]["cash_conversion"], .8)

    def test_optional_annual_profit_and_cashflow_hard_gates(self):
        rows = annual_rows()
        rows[1]["net_profit"] = -10.0
        result = self.evaluate(rows, require_annual_net_profit_positive=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("annual_net_profit_positive", result["metrics"]["failed_checks"])
        self.assertEqual(result["metrics"]["net_profit_positive_years"], 2)

        rows = annual_rows()
        rows[0]["operating_cashflow"] = 0.0
        result = self.evaluate(rows, require_annual_cashflow_positive=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("annual_operating_cashflow_positive", result["metrics"]["failed_checks"])
        self.assertEqual(result["metrics"]["operating_cashflow_positive_years"], 2)

    def test_annual_hard_gate_missing_value_is_partial(self):
        rows = annual_rows()
        rows[1]["operating_cashflow"] = None
        result = self.evaluate(rows, require_annual_cashflow_positive=True)
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("annual_operating_cashflow_positive", result["metrics"]["failed_checks"])
        self.assertIsNone(result["metrics"]["annual_operating_cashflow_positive"])
        self.assertIn("operating_cashflow:2023", result["missing_tags"])

    def test_zero_negative_denominator_is_failure(self):
        for value in (0, -100):
            rows = annual_rows()
            for row in rows:
                row.update(net_profit=value, operating_cashflow=-100)
            self.assertEqual(self.evaluate(rows)["status"], "failed")

    def test_missing_and_nonfinite_fields_never_verify(self):
        for field in ("roe", "deducted_profit", "net_profit", "operating_cashflow"):
            for value in (None, float("nan"), float("inf"), -float("inf"), True, "--"):
                with self.subTest(field=field, value=value):
                    rows = annual_rows()
                    rows[1][field] = value
                    result = self.evaluate(rows)
                    self.assertEqual(result["status"], "partial")
                    self.assertIn(f"{field}:2023", result["missing_tags"])
                    self.assertLess(result["quality_score"], 100)
                    json.dumps(result, allow_nan=False)

    def test_overflow_aggregate_never_verifies(self):
        rows = annual_rows()
        for row in rows:
            row.update(net_profit=1e308, operating_cashflow=1e308)
        result = self.evaluate(rows)
        self.assertEqual(result["status"], "partial")
        self.assertIn("nonfinite_aggregate", result["missing_tags"])
        json.dumps(result, allow_nan=False)

    def test_missing_latest_does_not_replace_with_old_year(self):
        rows = annual_rows()
        rows[-1].update(year=2021, report_date="2021-12-31")
        result = self.evaluate(rows)
        self.assertEqual(result["status"], "partial")
        self.assertIn("missing_year:2024", result["missing_tags"])
        self.assertEqual(result["metrics"]["expected_years"], [2022, 2023, 2024])

    def test_gap_and_duplicate(self):
        self.assertEqual(self.evaluate(annual_rows()[::2])["status"], "partial")
        rows = annual_rows() + [annual_rows()[1]]
        result = self.evaluate(rows)
        self.assertEqual(result["status"], "partial")
        self.assertIn("duplicate_year:2023", result["missing_tags"])

    def test_no_data(self):
        result = self.evaluate([])
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["quality_score"], 0)

    def test_conservative_may_boundary(self):
        rows = annual_rows()
        april = self.evaluate(rows, as_of="2025-04-30")
        self.assertEqual(april["metrics"]["expected_years"], [2021, 2022, 2023])
        self.assertNotIn(2024, april["metrics"]["observed_years"])
        self.assertEqual(self.evaluate(rows)["status"], "verified")

    def test_actual_early_announcement_advances_window(self):
        rows = annual_rows()
        rows[-1]["available_date"] = "2025-03-15"
        result = self.evaluate(rows, as_of="2025-03-15")
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result["annual_rows"][-1]["availability_assumed"])

    def test_late_announcement_blocks_even_after_may(self):
        rows = annual_rows()
        rows[-1]["available_date"] = "2025-06-01"
        result = self.evaluate(rows)
        self.assertEqual(result["status"], "partial")
        self.assertIn("not_available:2024", result["missing_tags"])
        self.assertEqual(self.evaluate(rows, as_of="2025-06-01")["status"], "verified")

    def test_invalid_dates_and_years(self):
        for key, value in [("year", 2024.5), ("year", True),
                           ("report_date", "2024-09-30"),
                           ("report_date", "2023-12-31"),
                           ("report_date", None), ("available_date", "broken"),
                           ("available_date", "2024-12-30")]:
            rows = annual_rows()
            rows[-1][key] = value
            self.assertNotEqual(self.evaluate(rows)["status"], "verified")

    def test_financial_always_pending_not_nonfinancial_failure(self):
        rows = annual_rows()
        rows[0]["roe"] = -100
        result = self.evaluate(rows, financial=True)
        self.assertEqual(result["status"], "financial_review")
        self.assertIn("pending", result["reason"])
        self.assertEqual(result["quality_score"], 0)
        self.assertEqual(result["metrics"], {})

    def test_known_violation_remains_failed_when_another_field_missing(self):
        rows = annual_rows()
        rows[0]["deducted_profit"] = -1
        rows[1]["roe"] = None
        self.assertEqual(self.evaluate(rows)["status"], "failed")

    def test_custom_window_thresholds_and_validation(self):
        result = self.evaluate(annual_rows()[1:], years=2, median_roe_min=12.5)
        self.assertEqual(result["status"], "verified")
        for kwargs in ({"years": 0}, {"years": 2.5}, {"years": True},
                       {"as_of": "bad"}, {"min_roe": float("nan")}):
            with self.assertRaises(ValueError):
                self.evaluate(**kwargs)

    def test_fetch_real_schema_units_and_consolidated_profit(self):
        client = Mock()
        client.stock_financial_abstract.return_value = sina_fixture()
        result = fetch_annual_quality("63", "2025-05-01", ak_client=client)
        client.stock_financial_abstract.assert_called_once_with(symbol="000063")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["metrics"]["net_profit_sum"], 3e8)
        self.assertEqual(result["metrics"]["cash_conversion"], .8)
        self.assertEqual(result["annual_rows"][-1]["report_date"], "2024-12-31")
        self.assertEqual(result["annual_rows"][-1]["available_date"], "2025-05-01")

    def test_captured_sina_600004_annual_values(self):
        # Captured from CompanyFinanceService.getFinanceReport2022, gjzb,
        # sh600004; transformed exactly as AkShare does (publication discarded).
        values = {
            "净资产收益率(ROE)": (2.54, 5.15, 7.45),
            "扣非净利润": (382603900.06, 906113528.96, 958700637.88),
            "净利润": (481003664.14, 965752959.58, 1513673711.49),
            "经营现金流量净额": (2359665123.90, 3410126652.46, 3074788371.68),
            "归母净利润": (441905715.29, 925847484.77, 1468348737.38),
        }
        table = [dict({"选项": "常用指标", "指标": key},
                      **dict(zip(("20231231", "20241231", "20251231"), vals)))
                 for key, vals in values.items()]
        client = Mock()
        client.stock_financial_abstract.return_value = table
        result = fetch_annual_quality("600004", "2026-05-01", ak_client=client)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["metrics"]["median_roe"], 5.15)
        self.assertEqual(result["metrics"]["min_roe"], 2.54)
        self.assertAlmostEqual(result["metrics"]["cash_conversion"],
                               sum(values["经营现金流量净额"]) / sum(values["净利润"]))
        self.assertEqual(result["annual_rows"][-1]["net_profit"], 1513673711.49)

    def test_finite_large_even_median(self):
        rows = annual_rows()[1:]
        for row in rows:
            row["roe"] = 1e308
        result = self.evaluate(rows, years=2)
        self.assertEqual(result["metrics"]["median_roe"], 1e308)
        json.dumps(result, allow_nan=False)

    def test_dataframe_like_adapter(self):
        table = Mock()
        table.to_dict.return_value = sina_fixture()
        client = Mock()
        client.stock_financial_abstract.return_value = table
        self.assertEqual(fetch_annual_quality("600004", "2025-05-01", ak_client=client)["status"], "verified")
        table.to_dict.assert_called_once_with("records")

    def test_parent_profit_cannot_replace_missing_consolidated_profit(self):
        client = Mock()
        client.stock_financial_abstract.return_value = [r for r in sina_fixture() if r["指标"] != "净利润"]
        result = fetch_annual_quality("600004", "2025-05-01", ak_client=client)
        self.assertEqual(result["status"], "partial")
        self.assertIn("net_profit:2024", result["missing_tags"])

    def test_preserved_announcement_metadata(self):
        fixture = sina_fixture() + [{"指标": "公告日期", "20241231": "20250301"}]
        client = Mock()
        client.stock_financial_abstract.return_value = fixture
        result = fetch_annual_quality("600004", "2025-03-01", ak_client=client)
        self.assertEqual(result["status"], "verified")

    def test_fetch_errors_empty_and_unknown_schema_are_missing(self):
        for data in ([], [{"unknown": 1}], None):
            client = Mock()
            client.stock_financial_abstract.return_value = data
            self.assertEqual(fetch_annual_quality("1", "2025-05-01", ak_client=client)["status"], "missing")
        client = Mock()
        client.stock_financial_abstract.side_effect = TimeoutError("offline")
        result = fetch_annual_quality("1", "2025-05-01", ak_client=client)
        self.assertEqual(result["status"], "missing")
        self.assertIn("fetch_error", result["missing_tags"])
        with self.assertRaises(ValueError):
            fetch_annual_quality("bad", "2025-05-01", ak_client=client)


if __name__ == "__main__":
    unittest.main()
