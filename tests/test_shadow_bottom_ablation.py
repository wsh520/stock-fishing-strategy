"""Independent offline checks: diagnostic compatibility, PIT and ablation boundaries."""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.recommendation_factors import assess_low_structure, assess_shadow_bottoms
from diag_technical_bottom_ablation import evaluate_frame, summarize, strict_stabilization, main
from src.bottom_fishing_strategy import StrategyConfig, compute_daily_signals


def bars(values):
    close = np.array(values, dtype=float)
    return pd.DataFrame(dict(date=pd.bdate_range("2024-01-01", periods=len(close)),
        close=close, open=close, high=close*1.01, low=close*.99,
        atr=.1, volume=1e6, amount=close*1e6))


class DiagnosticCompatibilityTests(unittest.TestCase):
    def test_age_and_gain_are_independent_without_changing_legacy_fields(self):
        for age in (4, 5, 40, 41, 59):
            frame = bars([13.]*70)
            frame.loc[len(frame)-1-age, "close"] = 10.
            result = assess_low_structure(frame, frame.date.iloc[-1])
            age_reason = "too_new" if age < 5 else "too_old" if age > 40 else "confirmed"
            self.assertEqual(result["reason"], age_reason)
            self.assertEqual(result["status"], "verified" if 5 <= age <= 40 else "unconfirmed")
            self.assertEqual(result["anchor"], 10.)
            self.assertEqual(result["age"], age)
            self.assertAlmostEqual(result["gain_pct"], 30.)
            expected = ([] if age_reason == "confirmed" else [age_reason]) + ["overextended"]
            self.assertEqual(result["failure_reasons"], expected)

    def test_missing_atr_keeps_age_failure_without_guessing_gain_limit(self):
        frame = bars([10.]+[13.]*59)
        frame.loc[59, "atr"] = np.nan
        result = assess_low_structure(frame, frame.date.iloc[-1])
        self.assertEqual(result["failure_reasons"], ["too_old"])
        self.assertEqual(result["status"], "missing")
        self.assertIsNone(result["allowed_gain_pct"])

    def test_future_and_index_labels_do_not_change_result(self):
        frame = bars([13.]*40+[10.]+[11.]*29)
        day = frame.date.iloc[-1]
        original = frame.copy(deep=True)
        future = bars([1.]*100).iloc[70:]
        self.assertEqual(assess_low_structure(frame, day), assess_low_structure(pd.concat([frame, future]), day))
        pd.testing.assert_frame_equal(frame, original)


class ShadowBottomTests(unittest.TestCase):
    def pattern(self):
        return bars([12, 11.5, 11, 10, 10.3, 10.7, 11, 10.8, 10.5, 10.1, 10.4, 10.8, 11.2])

    def test_confirmation_requires_observed_right_bars_and_neckline(self):
        frame = self.pattern()
        early = assess_shadow_bottoms(frame, frame.date.iloc[11])
        self.assertEqual(early["double_bottoms"][0]["status"], "unconfirmed")
        final = assess_shadow_bottoms(frame, frame.date.iloc[12])
        pair = final["double_bottoms"][0]
        self.assertEqual(pair["status"], "confirmed")
        self.assertEqual(pair["confirmed_date"], frame.date.iloc[12].strftime("%Y-%m-%d"))
        self.assertEqual(pair["first_anchor"], 10.)
        self.assertEqual(pair["second_anchor"], 10.1)
        self.assertAlmostEqual(pair["gain_pct"], 12.)
        self.assertGreater(pair["gain_pct"], (11.2/10.1-1)*100)

    def test_future_cannot_confirm_or_invalidate_and_input_is_not_mutated(self):
        frame = self.pattern()
        original = frame.copy(deep=True)
        day = frame.date.iloc[11]
        self.assertEqual(assess_shadow_bottoms(frame, day), assess_shadow_bottoms(frame.iloc[:12], day))
        pd.testing.assert_frame_equal(frame, original)
        broken = bars(frame.close.tolist()+[9.8, 11.5])
        pair = assess_shadow_bottoms(broken, broken.date.iloc[-1])["double_bottoms"][0]
        self.assertEqual(pair["status"], "invalidated")
        self.assertEqual(pair["invalidated_date"], broken.date.iloc[-2].strftime("%Y-%m-%d"))
        self.assertIsNotNone(pair["confirmed_date"])

    def test_high_retest_is_not_a_double_bottom(self):
        frame = bars([12, 11, 10.5, 10, 11, 12, 13, 12.5, 12, 11.8, 12, 12.5, 13.2])
        self.assertEqual(assess_shadow_bottoms(frame, frame.date.iloc[-1])["double_bottoms"], [])

    def test_missing_duplicate_and_unconfirmed_are_explicit(self):
        frame = self.pattern()
        self.assertEqual(assess_shadow_bottoms(pd.concat([frame, frame.tail(1)]), frame.date.iloc[-1])["reason"], "duplicate_dates")
        self.assertEqual(assess_shadow_bottoms(frame.iloc[:-1], frame.date.iloc[-1])["reason"], "as_of_bar_missing")
        result = assess_shadow_bottoms(frame, frame.date.iloc[9])
        self.assertEqual(result["local_bottoms"][-1]["status"], "unconfirmed")
        failed = bars([12, 11, 10.5, 10, 9.9, 10.2, 10.3])
        local = assess_shadow_bottoms(failed, failed.date.iloc[-1])["local_bottoms"][0]
        self.assertEqual(local["status"], "invalidated")
        self.assertIsNone(local["confirmed_date"])


class OfflineAblationTests(unittest.TestCase):
    def frame(self):
        return bars(np.r_[np.linspace(20, 10, 260), np.linspace(10.02, 11, 40)])

    def test_local_only_future_labels_and_no_feature_leakage(self):
        frame = self.frame()
        day = frame.date.iloc[279]
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            row, reason = evaluate_frame(frame, day)
            prefix, _ = evaluate_frame(frame.iloc[:280], day)
        self.assertIsNone(reason)
        for key in ("technical_score", "production_bottom", "shadow_bottoms", "stabilization_level"):
            self.assertEqual(row[key], prefix[key])
        self.assertAlmostEqual(row["forward_5bar_pct"], (frame.close.iloc[284]/frame.close.iloc[279]-1)*100)
        self.assertIsNone(prefix["forward_5bar_pct"])
        self.assertEqual(row["current_stab_and_tech40"], row["stabilization_only"] and row["technical_only"])

    def test_latest_bar_has_no_forward_labels_and_insufficient_report(self):
        frame = self.frame()
        row, _ = evaluate_frame(frame, frame.date.iloc[-1])
        for h in (5, 10, 20):
            self.assertIsNone(row[f"forward_{h}bar_pct"])
        report = summarize([row])
        for group in report["groups"].values():
            self.assertEqual(group["forward"]["5"]["sample_status"], "insufficient_no_return_conclusion")
        self.assertIn("不可推收益结论", report["conclusion"])

    def test_missing_volume_is_not_fabricated(self):
        frame = self.frame().drop(columns="volume")
        row, reason = evaluate_frame(frame, frame.date.iloc[-1])
        self.assertIsNone(row)
        self.assertEqual(reason, "missing_columns:volume")

    def test_suspension_bars_do_not_affect_features_or_forward_offsets(self):
        frame = self.frame()
        day = frame.date.iloc[279]
        frame.loc[50:54, "volume"] = 0.
        frame.loc[100:104, "amount"] = 0.
        # Halt placeholders may contain unusable prices; they must be removed first.
        frame.loc[50:54, ["open", "high", "low", "close"]] = 0.
        frame.loc[280, "volume"] = 0.
        frame.loc[282, "amount"] = 0.
        original = frame.copy(deep=True)
        row, reason = evaluate_frame(frame, day)
        cleaned = frame.loc[(frame.volume > 0) & (frame.amount > 0)]
        reference, _ = evaluate_frame(cleaned, day)
        prefix, _ = evaluate_frame(frame.loc[frame.date <= day], day)
        self.assertIsNone(reason)
        self.assertEqual(row, reference)
        for key in ("technical_score", "production_bottom", "shadow_bottoms", "stabilization_level"):
            self.assertEqual(row[key], prefix[key])
        # Future active bars are 281, 283, 284, 285, 286, not full-row index 284.
        self.assertAlmostEqual(row["forward_5bar_pct"], (frame.close.iloc[286]/frame.close.iloc[279]-1)*100)
        self.assertIsNone(row["forward_20bar_pct"])
        pd.testing.assert_frame_equal(frame, original)

    def test_as_of_zero_volume_or_amount_is_skipped(self):
        for column in ("volume", "amount"):
            with self.subTest(column=column):
                frame = self.frame()
                frame.loc[len(frame)-1, column] = 0.
                row, reason = evaluate_frame(frame, frame.date.iloc[-1])
                self.assertIsNone(row)
                self.assertEqual(reason, "as_of_no_trading")

    def test_future_suspensions_change_labels_not_features(self):
        frame = self.frame()
        day = frame.date.iloc[279]
        reference, _ = evaluate_frame(frame, day)
        frame.loc[frame.date > day, "volume"] = 0.
        row, _ = evaluate_frame(frame, day)
        for key in ("technical_score", "production_bottom", "shadow_bottoms", "stabilization_level"):
            self.assertEqual(row[key], reference[key])
        for h in (5, 10, 20):
            self.assertIsNone(row[f"forward_{h}bar_pct"])

    def test_cli_missing_or_empty_input_fails_clearly(self):
        import io
        for exists, message in ((False, "input does not exist"), (True, "no CSV files found")):
            with self.subTest(directory_exists=exists), \
                 patch("diag_technical_bottom_ablation.Path.is_file", return_value=False), \
                 patch("diag_technical_bottom_ablation.Path.is_dir", return_value=exists), \
                 patch("diag_technical_bottom_ablation.Path.glob", return_value=[]), \
                 patch("sys.stderr", new_callable=io.StringIO) as stderr, \
                 self.assertRaises(SystemExit) as raised:
                main(["--input", "missing-test-directory", "--as-of", "2025-06-30"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn(message, stderr.getvalue())

    def test_strict_medium_needs_ma20_and_weak_is_not_formal(self):
        frame = self.frame()
        frame["pct_chg"] = frame.close.pct_change()*100
        cfg = StrategyConfig()
        technical = compute_daily_signals(frame, cfg)
        technical["ma20"] = 100.
        technical["macd_histogram"] = np.arange(len(technical))*.01
        self.assertEqual(strict_stabilization(technical, cfg), "weak")


if __name__ == "__main__":
    unittest.main()
