"""Offline descriptive diagnostics; original decisions remain authoritative."""
import copy
import json
import unittest
from unittest.mock import Mock

from src.recent_operating import evaluate_operating_trend, validated_operating_status
from src.financial_risk import (parse_balance, combine_financial_rows,
                                evaluate_financial_risk, fetch_financial_risk,
                                validated_financial_risk_status)

DAY = "2026-06-12"


def operating_rows(current=None, prior=None):
    base = dict(revenue=1000., net_profit=100., deducted_profit=90.,
                operating_cashflow=110., gross_margin=30., net_margin=10.)
    return [dict(base, report_date="2025-03-31", available_date="2025-04-20", **(prior or {})),
            dict(base, report_date="2026-03-31", available_date="2026-04-20", **(current or {}))]


def balance_body(values):
    items = [dict(item_title="资产总计", item_value=1000),
             dict(item_title="负债合计", item_value=400)]
    items.extend(dict(item_title="商誉", item_value=value) for value in values)
    return {"result": {"data": {"report_list": {"20260331": {
        "rType": "合并期末", "rCurrency": "CNY", "publish_date": "20260420",
        "data_source": "定期报告", "data": items}}}}}


def risk_rows(values):
    return [dict(row, net_profit=100, deducted_profit=90)
            for row in parse_balance(balance_body(values))]


class DescriptiveOperatingTests(unittest.TestCase):
    def test_levels_preserve_exact_old_status_and_reasons(self):
        cases = [
            ({}, {}, "verified", [], "stable"),
            ({"net_profit": 95}, {}, "weak", ["net_profit_declining", "net_profit_latest_quarter_declining"], "single_fluctuation"),
            ({"net_profit": 95, "gross_margin": 26}, {}, "weak",
             ["net_profit_declining", "gross_margin_contracting", "net_profit_latest_quarter_declining"], "multiple_deterioration"),
            ({}, {"net_profit": -1}, "weak", ["net_profit_nonpositive_base"], "recovering"),
            ({"net_profit": 89}, {}, "failed", ["net_profit_severe_contraction", "net_profit_latest_quarter_declining"], "severe_deterioration"),
            ({"gross_margin": None}, {}, "missing", [], "unknown"),
            ({"revenue": 990, "net_profit": 99, "operating_cashflow": 109}, {},
             "failed", ["sales_profit_cash_deteriorating"], "severe_deterioration"),
        ]
        for current, prior, status, reasons, level in cases:
            with self.subTest(level=level):
                rows = operating_rows(current, prior)
                original = copy.deepcopy(rows)
                result = evaluate_operating_trend(rows, DAY)
                self.assertEqual((result["status"], result["reasons"]), (status, reasons))
                self.assertEqual(result["signal_profile"]["level"], level)
                self.assertEqual(result["signal_profile"]["reason_codes"], reasons)
                self.assertEqual(rows, original)
                json.dumps(result, allow_nan=False)
                result["code"] = "600001"
                before = validated_operating_status(result, "600001", DAY)
                result["signal_profile"] = {"level": "stable", "label": "任意描述"}
                self.assertEqual(validated_operating_status(result, "600001", DAY), before)

    def test_same_field_counted_once_and_recovery_cannot_mask_decline(self):
        result = evaluate_operating_trend(operating_rows({"net_profit": 95}), DAY)
        self.assertEqual(result["signal_profile"]["dimensions"], ["net_profit"])
        result = evaluate_operating_trend(operating_rows({"gross_margin": 26}, {"net_profit": -1}), DAY)
        self.assertEqual(result["signal_profile"]["level"], "single_fluctuation")

    @staticmethod
    def recovering_quarter_rows():
        prior, current = operating_rows()
        rows = [prior, dict(prior, report_date="2025-06-30", available_date="2025-07-20"),
                current, dict(current, report_date="2026-06-30", available_date="2026-07-20")]
        for field in ("revenue", "net_profit", "deducted_profit"):
            rows[1][field] = prior[field] * 2
            rows[2][field] = prior[field] * .8
            rows[3][field] = prior[field] * 1.9
        rows[1]["operating_cashflow"] = rows[3]["operating_cashflow"] = 220
        return rows

    def test_cumulative_pressure_with_quarter_recovery_keeps_original_decision(self):
        result = evaluate_operating_trend(self.recovering_quarter_rows(), "2026-09-01")
        self.assertEqual(result["status"], "weak")
        self.assertEqual(result["reasons"], ["revenue_declining", "net_profit_declining", "deducted_profit_declining"])
        self.assertEqual(result["signal_profile"]["level"], "multiple_deterioration")
        recovery = result["signal_profile"]["recovery_evidence"]
        self.assertEqual(recovery["level"], "recovering_latest_quarter")
        self.assertEqual(recovery["dimensions"], ["deducted_profit", "net_profit", "revenue"])
        for value in recovery["single_quarter_yoy"].values():
            self.assertAlmostEqual(value, 10)
        result["code"] = "600001"
        self.assertEqual(validated_operating_status(result, "600001", "2026-09-01"), "weak")

    def test_quarter_recovery_requires_all_affected_fields_complete_and_positive(self):
        for prior_quarter in (None, float("nan"), float("inf"), 90, 100):
            with self.subTest(prior_quarter=prior_quarter):
                rows = self.recovering_quarter_rows()
                # Current net-profit Q2 becomes missing, flat or negative YoY.
                rows[2]["net_profit"] = prior_quarter
                result = evaluate_operating_trend(rows, "2026-09-01")
                self.assertEqual(result["status"], "weak")
                self.assertIsNone(result["signal_profile"]["recovery_evidence"])
                json.dumps(result, allow_nan=False)

    def test_quarter_recovery_does_not_hide_other_weak_signals(self):
        for changes, reason in [({"operating_cashflow": 200}, "operating_cashflow_declining"),
                                ({"gross_margin": 26}, "gross_margin_contracting"),
                                ({"net_margin": 7}, "net_margin_contracting")]:
            with self.subTest(reason=reason):
                rows = self.recovering_quarter_rows()
                # Keep revenue flat so weaker cash does not trigger the joint veto.
                rows[-1].update(revenue=2000, **changes)
                result = evaluate_operating_trend(rows, "2026-09-01")
                self.assertEqual(result["status"], "weak")
                self.assertIn(reason, result["reasons"])
                self.assertIsNone(result["signal_profile"]["recovery_evidence"])

    def test_early_missing_paths_and_failure_with_missing(self):
        rows = operating_rows()
        for candidate, day, tag in [([], DAY, "operating_disclosure"),
                                    (rows, "2026-09-01", "operating_stale"),
                                    (rows + [rows[-1]], DAY, "operating_duplicate")]:
            result = evaluate_operating_trend(candidate, day)
            self.assertEqual(result["status"], "missing")
            self.assertEqual(result["missing_tags"], [tag])
            self.assertEqual(result["signal_profile"]["level"], "unknown")
        result = evaluate_operating_trend(operating_rows({"net_profit": -1, "gross_margin": None}), DAY)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["signal_profile"]["level"], "severe_deterioration")
        self.assertIn("operating_gross_margin", result["missing_tags"])


class GoodwillDiagnosticsTests(unittest.TestCase):
    def test_raw_missing_zero_valid_and_duplicate_paths(self):
        # [None] is a reported-null 商誉, i.e. a zero balance (2026-10-15 fix);
        # only absent/garbage values stay unknown.
        cases = [([], "absent", "missing"), ([None], "explicit_null", "verified"),
                 ([""], "empty_or_non_numeric", "missing"), (["--"], "empty_or_non_numeric", "missing"),
                 ([True], "empty_or_non_numeric", "missing"), ([float("nan")], "empty_or_non_numeric", "missing"),
                 ([float("inf")], "empty_or_non_numeric", "missing"),
                 ([0, 0], "duplicate", "missing"), ([0, 10, 20], "duplicate", "missing"),
                 ([0], "explicit_zero", "verified"), (["0.00"], "explicit_zero", "verified"),
                 ([10], "valid", "verified"), ([-1], "invalid_negative", "missing")]
        for values, state, status in cases:
            with self.subTest(values=values):
                rows = risk_rows(values)
                result = evaluate_financial_risk(rows, DAY)
                diag = result["goodwill_diagnostics"]
                self.assertEqual((diag["state"], diag["field_count"]), (state, len(values)))
                self.assertEqual((result["status"], result["reasons"]), (status, []))
                if status == "missing":
                    self.assertIsNone(result["metrics"]["goodwill"])
                    self.assertIsNone(result["metrics"]["goodwill_ratio"])
                elif state in ("explicit_zero", "explicit_null"):
                    self.assertEqual(result["metrics"]["goodwill_ratio"], 0)
                    self.assertEqual(result["missing_tags"], [])
                json.dumps(result, allow_nan=False)

    def test_canonical_fallback_and_descriptions_do_not_affect_validation(self):
        # A canonical row carries the resolved balance, so a reported-null 商誉
        # arrives here as 0 and is described as an explicit zero.
        for values, expected in [([], "absent"), ([None], "explicit_zero"), ([0], "explicit_zero"), ([10], "valid")]:
            row = risk_rows(values)[0]
            row.pop("goodwill_diagnostics")
            result = evaluate_financial_risk([row], DAY)
            self.assertEqual(result["goodwill_diagnostics"]["state"], expected)
            self.assertEqual(result["goodwill_diagnostics"]["origin"], "canonical_row")
            result["code"] = "600001"
            before = validated_financial_risk_status(result, "600001", DAY)
            result["goodwill_diagnostics"] = {"state": "explicit_zero"}
            self.assertEqual(validated_financial_risk_status(result, "600001", DAY), before)

    def test_no_disclosure_stale_duplicate_and_basis_paths(self):
        row = risk_rows([0])[0]
        for rows, day, tag, state in [([], DAY, "disclosure", "no_report"),
                ([dict(row, available_date="2026-06-13")], DAY, "disclosure", "no_report"),
                ([row], "2026-09-01", "stale", "explicit_zero"),
                ([row, row], DAY, "duplicate", "explicit_zero"),
                ([dict(row, currency="USD")], DAY, "basis", "explicit_zero")]:
            result = evaluate_financial_risk(rows, day)
            self.assertEqual(result["status"], "missing")
            self.assertEqual(result["missing_tags"], ["financial_risk_" + tag])
            self.assertEqual(result["goodwill_diagnostics"]["state"], state)
            if tag == "duplicate":
                self.assertTrue(result["goodwill_diagnostics"]["report_duplicate"])

    def test_merge_preserves_raw_diagnostic_and_known_failure(self):
        balance = parse_balance(balance_body([]))
        summary = [dict(balance[0], report_source="gjzb", data_source="其他", net_profit=100, deducted_profit=90)]
        original = copy.deepcopy(balance)
        rows = combine_financial_rows(balance, summary)
        result = evaluate_financial_risk(rows, DAY)
        self.assertEqual(result["goodwill_diagnostics"]["state"], "absent")
        self.assertEqual(balance, original)
        rows[0]["total_liabilities"] = 800
        result = evaluate_financial_risk(rows, DAY)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reasons"], ["debt_ratio_outside_limit"])
        self.assertIn("financial_risk_goodwill", result["missing_tags"])

    def test_bounded_samples_and_offline_fetch_failure(self):
        result = evaluate_financial_risk(risk_rows(["x" * 1000] * 20), DAY)
        diag = result["goodwill_diagnostics"]
        self.assertEqual(diag["field_count"], 20)
        self.assertEqual(len(diag["raw_samples"]), 3)
        self.assertTrue(all(len(s) <= 80 for s in diag["raw_samples"]))
        self.assertNotIn("data", diag)
        result = fetch_financial_risk("600001", DAY, request_get=Mock(side_effect=TimeoutError))
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["goodwill_diagnostics"]["state"], "no_report")
        self.assertIn("financial_risk_fetch_error", result["missing_tags"])
        response = Mock()
        response.json.return_value = balance_body([0])
        result = fetch_financial_risk("600001", DAY,
                                     request_get=Mock(side_effect=[response, TimeoutError()]))
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["goodwill_diagnostics"]["state"], "explicit_zero")
        self.assertIn("financial_risk_net_profit", result["missing_tags"])


if __name__ == "__main__":
    unittest.main()
