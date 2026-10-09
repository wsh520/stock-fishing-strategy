"""Offline bottom diagnostic boundaries, source fidelity, and final pending logs."""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src import bottom_fishing_strategy as m
from src.recommendation_factors import assess_low_structure
from test_quality_recommendations import DAY, fundamentals, prices


def bottom_frame(age):
    frame = pd.DataFrame({"date": pd.bdate_range(end=DAY, periods=70),
                          "close": 10.5, "low": 10., "atr": .3})
    frame.loc[len(frame) - 1 - age, "close"] = 10.
    return frame


class BottomDiagnosticsTests(unittest.TestCase):
    def config(self):
        return m.StrategyConfig(
            USE_CACHE=False, REQUIRE_RECENT_OPERATING=False,
            REQUIRE_FINANCIAL_RISK=False, REQUIRE_HISTORICAL_VALUATION=False,
            REQUIRE_CYCLICAL_NORMALIZED_VALUATION=False, MIN_TECHNICAL_SCORE_FORMAL=0,
            QV_LOW_ANCHOR_MODE="recent_structure", USE_INDUSTRY_RELATIVE_VALUATION=False,
            USE_INDUSTRY_DEDUP=False)

    def test_age_boundaries_are_inclusive_trading_bars(self):
        for age, status, reason in [(0, "unconfirmed", "too_new"),
                                    (4, "unconfirmed", "too_new"),
                                    (5, "verified", "confirmed"),
                                    (40, "verified", "confirmed"),
                                    (41, "unconfirmed", "too_old"),
                                    (59, "unconfirmed", "too_old")]:
            with self.subTest(age=age):
                frame = bottom_frame(age)
                result = assess_low_structure(frame, DAY)
                self.assertEqual((result["status"], result["reason"], result["age"]),
                                 (status, reason, age))
                self.assertEqual(result["anchor_date"], frame.iloc[-age - 1].date.strftime("%Y-%m-%d"))
                self.assertEqual((result["min_age"], result["max_age"], result["recent_window"]), (5, 40, 60))
                self.assertAlmostEqual(result["gain_pct"], 0. if age == 0 else 5.)
                self.assertAlmostEqual(result["allowed_gain_pct"], 5 * .3 / frame.iloc[-1].close * 100)

    def test_custom_window_and_first_tied_minimum_are_reported(self):
        frame = bottom_frame(9)
        frame.loc[len(frame) - 4, "close"] = 10.
        result = assess_low_structure(frame, DAY, recent_window=12, min_age=2, max_age=8)
        self.assertEqual((result["age"], result["reason"]), (9, "too_old"))
        self.assertEqual((result["recent_window"], result["min_age"], result["max_age"]), (12, 2, 8))

    def test_missing_data_has_precise_detail_and_keeps_known_anchor(self):
        frame = bottom_frame(20)
        frame.loc[len(frame) - 1, "atr"] = np.nan
        result = assess_low_structure(frame, DAY)
        self.assertEqual((result["status"], result["reason"], result["missing_detail"]),
                         ("missing", "data_missing", "invalid_atr"))
        self.assertEqual(result["age"], 20)
        self.assertEqual(result["anchor_date"], frame.iloc[-21].date.strftime("%Y-%m-%d"))
        self.assertAlmostEqual(result["gain_pct"], 5.)
        self.assertIsNone(result["allowed_gain_pct"])
        for data, detail in [(None, "required_columns"),
                             (frame.tail(59), "insufficient_bars"),
                             (frame.iloc[:-1], "as_of_bar_missing"),
                             (pd.concat([frame, frame.tail(1)], ignore_index=True), "duplicate_dates"),
                             (frame.assign(close=np.nan), "invalid_close")]:
            with self.subTest(detail=detail):
                result = assess_low_structure(data, DAY)
                self.assertEqual((result["reason"], result["missing_detail"]), ("data_missing", detail))

    def evaluate(self, config):
        return m.evaluate_quality_value(prices(), "600001", "工业企业", config,
                                        {"regime": "bull"}, fundamentals(), latest_trade_date=DAY)

    def test_evaluator_keeps_actual_result_and_serialization_hides_it(self):
        captured = []
        def capture(*args, **kwargs):
            result = assess_low_structure(*args, **kwargs)
            captured.append(result)
            return result
        with patch("src.recommendation_factors.assess_low_structure", side_effect=capture) as assess:
            signal, reason = self.evaluate(self.config())
        self.assertEqual((reason, signal.tier), ("PASS", "pending"))
        self.assertIn("recent_bottom_unconfirmed", signal.missing_tags)
        assess.assert_called_once()
        self.assertIs(signal.bottom_diagnostics, captured[0])
        self.assertEqual(signal.bottom_diagnostics["reason"], "too_old")
        self.assertNotIn("bottom_diagnostics", signal.to_dict())
        self.assertNotIn("too_old", m.describe(signal.to_dict()))

    def test_final_pending_logs_use_saved_result_without_recomputation(self):
        config = self.config()
        signal, reason = self.evaluate(config)
        missing = bottom_frame(20)
        missing.loc[len(missing) - 1, "atr"] = np.nan
        for age, expected, frame in [(4, "too_new", bottom_frame(4)),
                                     (41, "too_old", bottom_frame(41)),
                                     (20, "data_missing", missing)]:
            with self.subTest(reason=expected):
                signal.bottom_diagnostics = assess_low_structure(frame, DAY)
                pending = []
                with patch.object(m, "get_stock_list", return_value=[{"code": signal.code, "name": signal.name}]), \
                     patch.object(m, "run_concurrent_screen", return_value=([(signal, reason)], 1, False)), \
                     patch("src.recommendation_factors.assess_low_structure", side_effect=AssertionError("must not recompute")), \
                     patch.object(m, "get_daily_data", side_effect=AssertionError("must not fetch")), \
                     self.assertLogs(m.logger, level="INFO") as logs:
                    result = m._screen_quality_pool(config, m.CacheManager(), {"regime": "bull"}, DAY, pending)
                self.assertTrue(result.empty)
                self.assertEqual(len(pending), 1)
                self.assertNotIn("bottom_diagnostics", pending[0])
                text = "\n".join(logs.output)
                bottom = next(line for line in logs.output if "[QV_PENDING_BOTTOM]" in line)
                self.assertIn(signal.bottom_diagnostics["anchor_date"], bottom)
                for fragment in (f"距底交易bar数={age}", "min_age=5 max_age=40 window=60",
                                 "gain_pct=", "allowed_gain_pct=", f"原因={expected}"):
                    self.assertIn(fragment, bottom)
                self.assertLess(text.index("[QV_PENDING]"), text.index("[QV_PENDING_BOTTOM]"))
                self.assertLess(text.index("[QV_PENDING_BOTTOM]"), text.index("[QV_PENDING_SUMMARY]"))


if __name__ == "__main__":
    unittest.main()
