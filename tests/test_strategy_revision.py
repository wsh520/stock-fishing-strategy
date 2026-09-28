"""Production-default recommendation revision; all data sources are mocked."""
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from src import bottom_fishing_strategy as m
from src.recent_operating import evaluate_operating_trend
from test_quality_recommendations import prices, fundamentals, DAY
from test_quality_value_hardening import mk_df, BELOW_MA20_MACD_UP


def evidence(code="600001", **changes):
    base = dict(revenue=1000., net_profit=100., deducted_profit=90., operating_cashflow=110.,
                gross_margin=30., net_margin=10.)
    rows = [dict(base, report_date="2024-03-31", available_date="2024-04-20"),
            dict(base, report_date="2025-03-31", available_date="2025-04-20", **{})]
    rows[-1].update(changes)
    return dict(evaluate_operating_trend(rows, DAY), code=code)


def fund(**changes):
    return dict(fundamentals(), operating_trend=evidence(**changes))


def cfg(**changes):
    options = dict(USE_CACHE=False, MIN_TECHNICAL_SCORE_FORMAL=0)
    options.update(changes)
    return m.StrategyConfig(**options)


def evaluate(finance=None, frame=None, config=None):
    return m.evaluate(prices() if frame is None else frame, "600001", "工业企业", config or cfg(),
                      {"regime": "bull"}, fund() if finance is None else finance,
                      latest_trade_date=DAY)


class RevisedRecommendationTests(unittest.TestCase):
    def test_complete_production_evidence_formal(self):
        signal, reason = evaluate()
        self.assertEqual(reason, "PASS")
        self.assertEqual(signal.tier, "formal")
        self.assertEqual(signal.operating_status, "verified")
        self.assertIn(signal.stabilization_level, ("strong", "medium"))
        text = m.describe(signal.to_dict())
        self.assertIn("规则", text)
        self.assertIn("收入同比", text)

    def test_operating_weak_missing_and_hard_failure(self):
        for data, status in [(fund(net_profit=95), "weak"), (fund(gross_margin=None), "missing")]:
            signal, reason = evaluate(data)
            self.assertEqual((reason, signal.tier, signal.operating_status), ("PASS", "pending", status))
        self.assertEqual(evaluate(fund(net_profit=80))[1], "FAIL_RECENT_FUNDAMENTAL")
        self.assertEqual(evaluate(fund(revenue=990, net_profit=99, operating_cashflow=109))[1], "FAIL_RECENT_FUNDAMENTAL")

    def test_default_does_not_trust_old_single_metric_or_wrong_evidence(self):
        data = fund()
        data["forward_ni_yoy"] = -90  # obsolete other-source quarter must not override current evidence
        self.assertEqual(evaluate(data)[0].tier, "formal")
        for key, value in (("code", "600002"), ("as_of", "2025-06-27"),
                           ("available_date", "2025-07-01"), ("metrics", {})):
            data = fund()
            data["operating_trend"][key] = value
            self.assertEqual(evaluate(data)[0].tier, "pending")
        self.assertEqual(evaluate(fundamentals())[0].tier, "pending")

    def test_macd_only_new_lows_stay_observation_despite_score(self):
        signal, reason = evaluate(frame=mk_df(BELOW_MA20_MACD_UP), config=cfg(MIN_QV_SCORE=100))
        self.assertEqual(reason, "PASS")
        self.assertEqual(signal.tier, "pending")
        self.assertEqual(signal.stabilization_level, "weak")
        self.assertIn("stabilization_weak", signal.missing_tags)

    def test_nonfinite_stabilization_evidence_cannot_promote(self):
        original = m.compute_daily_signals
        def output(frame, config, invalid=False):
            result = original(frame, config)
            result.loc[result.index[-25:], "low"] = 1.0
            if invalid:
                result.loc[result.index[-7], "low"] = np.inf
            return result
        frame = mk_df(BELOW_MA20_MACD_UP)
        with patch.object(m, "compute_daily_signals", side_effect=lambda df, c: output(df, c)):
            self.assertEqual(evaluate(frame=frame)[0].stabilization_level, "medium")
        with patch.object(m, "compute_daily_signals", side_effect=lambda df, c: output(df, c, True)):
            signal, reason = evaluate(frame=frame)
            self.assertEqual((reason, signal.tier, signal.stabilization_level), ("PASS", "pending", "weak"))

    def test_technical_shortfall_remains_pending_before_composite_floor(self):
        signal, reason = evaluate(config=cfg(MIN_TECHNICAL_SCORE_FORMAL=100, MIN_QV_SCORE=100))
        self.assertEqual((reason, signal.tier), ("PASS", "pending"))
        self.assertIn("technical_weak", signal.missing_tags)

    def test_two_phase_screen_can_promote_only_after_evidence(self):
        config = cfg(USE_INDUSTRY_RELATIVE_VALUATION=False, USE_INDUSTRY_DEDUP=False)
        def run(stocks, worker, *_):
            return [worker(stock) for stock in stocks], len(stocks), False
        with patch.object(m, "get_stock_list", return_value=[{"code": "600001", "name": "工业企业"}]), \
             patch.object(m, "get_daily_data", return_value=prices()), \
             patch.object(m, "get_fundamentals", return_value=fundamentals()), \
             patch.object(m, "enrich_annual_fundamentals", return_value=fundamentals()), \
             patch.object(m, "enrich_recent_operating", return_value=fund()) as enrich, \
             patch.object(m, "run_concurrent_screen", side_effect=run):
            pending = []
            result = m._screen_quality_pool(config, m.CacheManager(), {"regime": "bull"}, DAY, pending)
        self.assertEqual(result.code.tolist(), ["600001"])
        self.assertEqual(pending, [])
        enrich.assert_called_once()

    def test_grouped_score_has_no_resonance_double_count(self):
        config = cfg()
        frame = m.compute_daily_signals(m._recompute_pct_chg(prices()), config)
        trend = frame.trend_turn.astype(float) * 40
        volume = frame.vol_price_quality / 25 * 30
        expected = (trend + frame.momentum_group_score + volume).clip(0, 100).round(1)
        pd.testing.assert_series_equal(frame.daily_score, expected, check_names=False)
        self.assertLessEqual(frame.momentum_group_score.max(), 30)
        self.assertTrue(frame.daily_score.between(0, 100).all())
        legacy = cfg(W_DAILY_TREND_TURN=60, W_DAILY_RSI_REBOUND=0, DAILY_MULTI_RESONANCE_BONUS=0, DAILY_VOL_EXPAND=999)
        old = m.compute_daily_signals(m._recompute_pct_chg(prices()), legacy)
        pd.testing.assert_series_equal(old.daily_score, old.trend_turn.astype(float) * 60, check_names=False)

    def test_bottom_structure_requires_price_geometry(self):
        loose = pd.DataFrame({"close": np.r_[np.linspace(10, 9, 20), np.linspace(9, 8, 20)]})
        loose["high"], loose["low"] = loose.close * 1.03, loose.close * .97
        self.assertFalse(m.bottom_structure_confirmed(loose, cfg()))
        flat = pd.DataFrame({"close": np.full(40, 10.), "high": np.r_[np.full(20, 10.5), np.full(20, 10.1)],
                             "low": np.r_[np.full(20, 9.5), np.full(20, 9.9)]})
        self.assertTrue(m.bottom_structure_confirmed(flat, cfg()))
        flat.loc[39, "low"] = np.nan
        self.assertFalse(m.bottom_structure_confirmed(flat, cfg()))

    def test_operating_cache_isolates_identity_date_and_parameters(self):
        cache = m.CacheManager()
        config = cfg()
        with patch("src.recent_operating.fetch_operating_trend", return_value=evidence()) as fetch:
            m.enrich_recent_operating("600001", {}, config, cache, DAY)
            m.enrich_recent_operating("600001", {}, config, cache, DAY)
            self.assertEqual(fetch.call_count, 1)
            changed = cfg(RECENT_OPERATING_PROFIT_YOY_MIN=-20)
            m.enrich_recent_operating("600001", {}, changed, cache, DAY)
            self.assertEqual(fetch.call_count, 2)
            m.enrich_recent_operating("600001", {}, config, cache, "2025-07-01")
            self.assertEqual(fetch.call_count, 3)


if __name__ == "__main__":
    unittest.main()
