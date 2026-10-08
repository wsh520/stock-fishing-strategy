"""Offline upgrades: explicit operating tolerances and optional flow evidence."""
import copy
import unittest
from unittest.mock import Mock
from src.recent_operating import evaluate_operating_trend, fetch_operating_trend


def row(period, **kwargs):
    year, month, _ = map(int, period.split("-"))
    values = dict(revenue=1000., net_profit=100., deducted_profit=90.,
                  operating_cashflow=110., gross_margin=30., net_margin=10.,
                  report_date=period, available_date=f"{year + (month == 12)}-{3 if month == 12 else month + 1:02d}-20")
    values.update(kwargs)
    return values


class OperatingUpgradesTests(unittest.TestCase):
    def evaluate(self, current=None, **kwargs):
        return evaluate_operating_trend([row("2025-03-31"), row("2026-03-31", **(current or {}))],
                                        "2026-06-12", **kwargs)

    def test_three_percent_default_and_configurable_boundary(self):
        self.assertEqual(self.evaluate(dict(revenue=970, net_profit=97, deducted_profit=87.3))["status"], "verified")
        self.assertEqual(self.evaluate(dict(net_profit=96.99))["status"], "weak")
        self.assertEqual(self.evaluate(dict(net_profit=98), yoy_decline_tolerance=0)["status"], "weak")
        self.assertEqual(self.evaluate(dict(net_profit=89))["status"], "failed")

    def test_micro_cash_decline_requires_healthy_conversion(self):
        self.assertEqual(self.evaluate(dict(operating_cashflow=109))["status"], "verified")
        self.assertEqual(self.evaluate(dict(operating_cashflow=100))["status"], "weak")
        self.assertEqual(self.evaluate(dict(operating_cashflow=109), cash_conversion_min=1.2)["status"], "weak")
        self.assertEqual(self.evaluate(dict(operating_cashflow=109), cashflow_decline_tolerance=0)["status"], "weak")

    def test_joint_decline_still_vetoes_inside_tolerance(self):
        result = self.evaluate(dict(revenue=990, net_profit=99, operating_cashflow=109))
        self.assertEqual(result["status"], "failed")
        self.assertIn("sales_profit_cash_deteriorating", result["reasons"])

    def test_missing_supplementary_evidence_does_not_force_pending(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "verified")
        self.assertIsNone(result["metrics"]["net_profit_ttm"])
        self.assertEqual(result["metrics"]["net_profit_single_quarter"], 100)
        self.assertEqual(self.evaluate(dict(net_profit=None))["status"], "missing")

    def test_cumulative_quarter_and_ttm_exact_arithmetic(self):
        rows = [row("2024-06-30", net_profit=50), row("2024-12-31", net_profit=120),
                row("2025-03-31", net_profit=40), row("2025-06-30", net_profit=70),
                row("2025-12-31", net_profit=150), row("2026-03-31", net_profit=50),
                row("2026-06-30", net_profit=90)]
        for r in rows:
            r["attributable_profit"] = r["net_profit"] * .8
        original = copy.deepcopy(rows)
        result = evaluate_operating_trend(rows, "2026-09-01")
        metrics = result["metrics"]
        self.assertEqual(metrics["net_profit_single_quarter"], 40)
        self.assertAlmostEqual(metrics["net_profit_single_quarter_yoy"], 100 / 3)
        self.assertEqual(metrics["net_profit_ttm"], 170)
        self.assertEqual(metrics["attributable_profit_ttm"], 136)
        self.assertAlmostEqual(metrics["net_profit_ttm_yoy"], (170 / 140 - 1) * 100)
        self.assertEqual(rows, original)
        rows[4]["available_date"] = "2026-09-02"
        self.assertIsNone(evaluate_operating_trend(rows, "2026-09-01")["metrics"]["net_profit_ttm"])

    def test_parameter_validation_and_fetch_forwarding(self):
        for field in ("yoy_decline_tolerance", "cashflow_decline_tolerance", "cash_conversion_min"):
            for value in (-1, float("nan"), True):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.evaluate(**{field: value})
        getter = Mock(side_effect=TimeoutError)
        result = fetch_operating_trend("600004", "2026-06-12", -10, 3, 2, getter,
                                      yoy_decline_tolerance=0, cashflow_decline_tolerance=0)
        self.assertEqual(result["status"], "missing")
        self.assertEqual(getter.call_args.kwargs["timeout"], 15)


if __name__ == "__main__":
    unittest.main()
