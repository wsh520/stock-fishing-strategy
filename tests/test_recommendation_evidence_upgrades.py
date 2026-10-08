"""Recommendation upgrades: provider-independent evidence and integration boundaries."""
import copy
from contextlib import contextmanager
import uuid
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd
from src import bottom_fishing_strategy as m
from src.market_data_integrity import BEIJING, latest_completed_day, quote_cache_valid, quote_metadata
from src.recommendation_factors import historical_valuation, quality_value_technical, assess_low_structure, normalized_earnings_valuation
from test_quality_recommendations import prices, fundamentals, DAY


@contextmanager
def quote_test_directory():
    root = (Path(__file__).resolve().parents[1] / ".test-artifacts").resolve()
    folder = root / ("quote_" + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    try:
        yield folder
    finally:
        assert folder.resolve().parent == root
        for child in folder.iterdir():
            assert child.resolve().parent == folder.resolve()
            child.unlink()
        folder.rmdir()


class QuoteIntegrityTests(unittest.TestCase):
    def test_intraday_cache_cannot_become_closed_by_touch(self):
        morning = datetime(2025, 6, 30, 10, tzinfo=BEIJING)
        evening = morning.replace(hour=21)
        with quote_test_directory() as folder:
            path = str(Path(folder) / "daily_test.csv")
            with patch.object(m, "_beijing_now", return_value=morning):
                m._write_cache_csv(prices(), path)
                self.assertTrue(m._cache_fresh_today(path))
            Path(path).touch()
            with patch.object(m, "_beijing_now", return_value=evening):
                self.assertFalse(m._cache_fresh_today(path))
                m._write_cache_csv(prices(), path)
                self.assertTrue(m._cache_fresh_today(path))

    def test_legacy_cache_without_fetch_metadata_is_rejected(self):
        self.assertFalse(quote_cache_valid(None, datetime.now(BEIJING)))
        now = datetime(2025, 6, 30, 21, tzinfo=BEIJING)
        self.assertFalse(quote_cache_valid(quote_metadata(prices(), now + timedelta(hours=1)), now))

    def test_calendar_handles_holidays_and_intraday_without_weekday_guess(self):
        rows = [{"calendar_date": "2025-06-27", "is_trading_day": "1"},
                {"calendar_date": "2025-06-30", "is_trading_day": "0"},
                {"calendar_date": "2025-07-01", "is_trading_day": "1"}]
        self.assertEqual(latest_completed_day(rows, datetime(2025, 6, 30, 21, tzinfo=BEIJING)), "2025-06-27")
        self.assertEqual(latest_completed_day(rows, datetime(2025, 7, 1, 10, tzinfo=BEIJING)), "2025-06-27")
        self.assertEqual(latest_completed_day(rows, datetime(2025, 7, 1, 21, tzinfo=BEIJING)), "2025-07-01")
        self.assertIsNone(latest_completed_day(rows, datetime(2025, 7, 2, 21, tzinfo=BEIJING)))

    def test_same_stale_index_and_stock_do_not_prove_market_freshness(self):
        cfg = m.StrategyConfig(USE_CACHE=False)
        now = datetime(2025, 7, 1, 21, tzinfo=BEIJING)
        with patch.object(m, "_beijing_now", return_value=now), patch.object(m, "latest_completed_trade_day", return_value="2025-07-01"):
            self.assertIsNone(m._closed_market_frame(prices(), cfg))
        with patch.object(m, "_beijing_now", return_value=now.replace(hour=10)), patch.object(m, "latest_completed_trade_day", return_value=DAY):
            self.assertEqual(m._closed_market_frame(prices(end="2025-07-01"), cfg).iloc[-1]["date"], DAY)

    def test_memory_quote_key_changes_at_close(self):
        cfg = m.StrategyConfig(USE_CACHE=False)
        cache = m.CacheManager()
        with patch.object(m, "_fetch_daily_dual", return_value=prices()) as fetch:
            with patch.object(m, "_beijing_now", return_value=datetime(2025, 6, 30, 10, tzinfo=BEIJING)):
                m.get_daily_data("600001", cfg, cache)
                m.get_daily_data("600001", cfg, cache)
            with patch.object(m, "_beijing_now", return_value=datetime(2025, 6, 30, 21, tzinfo=BEIJING)):
                m.get_daily_data("600001", cfg, cache)
            self.assertEqual(fetch.call_count, 2)


class FactorTests(unittest.TestCase):
    def test_history_excludes_future_and_uses_midrank_ties(self):
        frame = prices()
        result = historical_valuation(frame, DAY)
        self.assertEqual(result["percentile"], .5)
        future = frame.tail(1).copy()
        future["date"], future["peTTM"] = "2025-07-01", 9999.
        self.assertEqual(historical_valuation(pd.concat([frame, future]), DAY), result)
        frame.loc[frame.index[-1], "peTTM"] = 20.
        self.assertEqual(historical_valuation(frame, DAY)["percentile"], 1.)
        frame.loc[frame.index[:-1], "peTTM"] = np.nan
        self.assertEqual(historical_valuation(frame, DAY)["status"], "missing")

    def test_healthy_state_keeps_score_after_event_expires(self):
        frame = m.compute_daily_signals(m._recompute_pct_chg(prices()), m.StrategyConfig())
        frame["trend_turn"] = False
        frame["macd_golden_cross"] = False
        frame["rsi_rebound"] = False
        frame["close"], frame["ma20"], frame["ma20_slope"] = 10.2, 10., .005
        frame["macd_histogram"] = np.arange(len(frame)) * .01
        frame["kdj_k"], frame["kdj_d"] = 50., 40.
        scores = quality_value_technical(frame)
        self.assertGreater(scores.iloc[-1].qv_daily_score, 40.)
        self.assertEqual(scores.iloc[-1].qv_event_strength, 0.)
        self.assertTrue(scores.qv_daily_score.between(0, 100).all())

    def test_recent_anchor_ignores_old_intraday_wick_and_adapts_to_volatility(self):
        frame = m.compute_daily_signals(m._recompute_pct_chg(prices()), m.StrategyConfig())
        frame.loc[frame.index[-200], "low"] = 1.
        # Bottom 20 bars ago, then modest recovery.
        frame.loc[frame.index[-60:], "close"] = np.r_[np.linspace(11, 10, 40), np.linspace(10.01, 10.5, 20)]
        frame["atr"] = .3
        result = assess_low_structure(frame, DAY)
        self.assertEqual(result["status"], "verified")
        self.assertAlmostEqual(result["gain_pct"], 5.)
        self.assertGreater(result["annual_extreme_gain_pct"], 900.)
        self.assertGreater(result["allowed_gain_pct"], 10.)

    def test_normalized_pe_never_substitutes_consolidated_profit(self):
        rows = [dict(net_profit=100., attributable_profit=80.) for _ in range(3)]
        op = {"metrics": {"attributable_profit_ttm": 160., "net_profit_ttm": 1000.}}
        self.assertEqual(normalized_earnings_valuation(10., rows, op)["normalized_pe"], 20.)
        self.assertEqual(normalized_earnings_valuation(10., [dict(net_profit=100.)] * 3, op)["status"], "missing")

    def test_comparable_group_matches_profitability_before_pe_rank(self):
        cfg = m.StrategyConfig(VALUATION_INDUSTRY_MIN_PEERS=3)
        bucket = {"pe": np.array([5., 10., 12., 20., 40., 50.]), "pb": np.ones(6),
                  "comparable_peers": [dict(pe=pe, pb=pe * ratio, earnings_to_equity=ratio)
                                       for pe, ratio in [(10., .1), (12., .12), (20., .15), (5., .01), (40., .8), (50., 1.)]]}
        ctx = m._industry_valuation_context({"制造": bucket}, "制造", 12., 1.2, cfg)
        self.assertEqual(ctx["comparable_mode"], "earnings_to_equity_band")
        self.assertEqual(ctx["pe_peers"], 3)


class IntegrationTests(unittest.TestCase):
    def config(self, **kwargs):
        return m.StrategyConfig(USE_CACHE=False, QV_LOW_ANCHOR_MODE="legacy", MIN_TECHNICAL_SCORE_FORMAL=0,
                                REQUIRE_RECENT_OPERATING=False, **kwargs)

    def evaluate(self, frame=None, fund=None, config=None, **kwargs):
        kwargs.setdefault("val_context", {"mode": "absolute", "industry": "C39计算机、通信和其他电子设备制造业"})
        return m.evaluate(prices() if frame is None else frame, "600001", "工业企业", config or self.config(),
                          {"regime": "bull"}, fundamentals() if fund is None else fund, latest_trade_date=DAY, **kwargs)

    def cycle_fund(self, attributable_ttm=100.):
        from src.recent_operating import evaluate_operating_trend
        data = fundamentals()
        for row in data["annual_rows"]:
            row["attributable_profit"] = 100.
        base = dict(revenue=1000., net_profit=100., deducted_profit=90., operating_cashflow=110.,
                    gross_margin=30., net_margin=10., attributable_profit=100.)
        rows = [dict(base, report_date="2024-03-31", available_date="2024-04-20"),
                dict(base, report_date="2024-12-31", available_date="2025-04-20"),
                dict(base, report_date="2025-03-31", available_date="2025-04-20")]
        rows[-1]["attributable_profit"] = attributable_ttm
        data["operating_trend"] = dict(evaluate_operating_trend(rows, DAY), code="600001")
        return data

    def test_actual_industry_codes_enforce_normalized_valuation(self):
        cfg = self.config(REQUIRE_FINANCIAL_RISK=False)
        for industry in ("C31黑色金属冶炼和压延加工业", "C26化学原料和化学制品制造业", "G55水上运输业"):
            with self.subTest(industry=industry):
                context = {"mode": "absolute", "industry": industry}
                self.assertEqual(self.evaluate(fund=self.cycle_fund(400.), config=cfg, val_context=context)[1], "FAIL_CYCLICAL_VALUATION")
                signal, reason = self.evaluate(fund=self.cycle_fund(100.), config=cfg, val_context=context)
                self.assertEqual((reason, signal.tier, signal.normalized_pe), ("PASS", "formal", 8.))

    def test_industry_absolute_cap_accepts_classification_code(self):
        cfg = self.config(REQUIRE_FINANCIAL_RISK=False, INDUSTRY_PE_ABSOLUTE_CAPS={"C31": 7.})
        self.assertEqual(self.evaluate(config=cfg, val_context={"mode": "absolute", "industry": "C31黑色金属冶炼和压延加工业"})[1], "FAIL_VALUATION")

    def test_unknown_industry_and_missing_parent_profit_stay_pending(self):
        cfg = self.config(REQUIRE_FINANCIAL_RISK=False)
        signal, _ = self.evaluate(config=cfg, val_context=None)
        self.assertEqual(signal.tier, "pending")
        self.assertIn("industry_classification", signal.missing_tags)
        signal, _ = self.evaluate(config=cfg, val_context={"mode": "absolute", "industry": "C31黑色金属冶炼和压延加工业"})
        self.assertEqual(signal.tier, "pending")
        self.assertIn("cyclical_normalized_valuation", signal.missing_tags)

    def test_wrong_stock_or_future_operating_cannot_drive_normalized_pe(self):
        cfg = self.config(REQUIRE_FINANCIAL_RISK=False)
        for key, value in (("code", "600002"), ("available_date", "2025-07-01")):
            data = self.cycle_fund(400.)
            data["operating_trend"][key] = value
            signal, reason = self.evaluate(fund=data, config=cfg, val_context={"mode": "absolute", "industry": "C31黑色金属冶炼和压延加工业"})
            self.assertEqual((reason, signal.tier, signal.normalized_pe), ("PASS", "pending", None))

    def test_ttm_requires_recomputed_disclosed_operands(self):
        cfg = self.config(REQUIRE_FINANCIAL_RISK=False)
        context = {"mode": "absolute", "industry": "C31黑色金属冶炼和压延加工业"}
        for mutation in ("metric", "absent_rows", "future_annual", "wrong_prior"):
            data = self.cycle_fund(400.)
            evidence = data["operating_trend"]
            if mutation == "metric":
                evidence["metrics"]["attributable_profit_ttm"] = 100.
            elif mutation == "absent_rows":
                evidence.pop("supplemental_rows")
            elif mutation == "future_annual":
                evidence["supplemental_rows"][1]["available_date"] = "2025-07-01"
            else:
                evidence["supplemental_rows"][0]["attributable_profit"] = 400.
            signal, reason = self.evaluate(fund=data, config=cfg, val_context=context)
            self.assertEqual((reason, signal.tier, signal.normalized_pe), ("PASS", "pending", None))
            self.assertIn("cyclical_normalized_valuation", signal.missing_tags)

    def test_absolute_mode_pipeline_still_gets_industry_for_cycle_review(self):
        cfg = self.config(REQUIRE_FINANCIAL_RISK=False, USE_INDUSTRY_RELATIVE_VALUATION=False,
                          USE_INDUSTRY_DEDUP=False)
        with patch.object(m, "get_stock_list", return_value=[{"code": "600001", "name": "工业企业"}]), \
             patch.object(m, "get_daily_data", return_value=prices()), \
             patch.object(m, "get_stock_industry", return_value={"600001": "C31黑色金属冶炼和压延加工业"}) as industry, \
             patch.object(m, "build_industry_valuation_snapshot", side_effect=AssertionError("absolute mode cannot build industry PE snapshot")), \
             patch.object(m, "get_fundamentals", return_value=self.cycle_fund(400.)):
            result = m._screen_quality_pool(cfg, m.CacheManager(), {"regime": "bull"}, DAY)
        self.assertTrue(result is None or result.empty)
        industry.assert_called_once()

    def test_current_risk_limits_cannot_be_relaxed_by_evidence_thresholds(self):
        from src.financial_risk import evaluate_financial_risk
        data = fundamentals()
        row = dict(total_assets=1000., total_liabilities=400., goodwill=10., net_profit=100.,
                   deducted_profit=10., report_date="2025-03-31", available_date="2025-04-20",
                   source="offline", currency="CNY", statement_basis="consolidated")
        data["financial_risk"] = dict(evaluate_financial_risk([row], DAY, deducted_ratio_min=0.), code="600001")
        self.assertEqual(self.evaluate(fund=data)[1], "FAIL_FINANCIAL_RISK")

    def test_default_pipeline_promotes_only_complete_evidence(self):
        from src.financial_risk import evaluate_financial_risk
        config = m.StrategyConfig(USE_CACHE=False, USE_INDUSTRY_RELATIVE_VALUATION=False,
                                  USE_INDUSTRY_DEDUP=False)
        frame = prices()
        close = np.r_[np.linspace(20., 10., 275), np.linspace(10.02, 10.8, 25)]
        frame["close"], frame["open"], frame["high"], frame["low"] = close, close, close * 1.01, close * .99
        frame["amount"] = close * 1e7
        frame.loc[frame.index[-1], ["open", "high", "low", "volume", "amount"]] = [10.6, 10.85, 10.55, 2e7, 2e8]
        data = self.cycle_fund()
        row = dict(total_assets=1000., total_liabilities=400., goodwill=10., net_profit=100.,
                   deducted_profit=90., report_date="2025-03-31", available_date="2025-04-20",
                   source="offline", currency="CNY", statement_basis="consolidated")
        risk = dict(evaluate_financial_risk([row], DAY), code="600001")
        with patch.object(m, "get_stock_list", return_value=[{"code": "600001", "name": "工业企业"}]), \
             patch.object(m, "get_daily_data", return_value=frame), \
             patch.object(m, "get_stock_industry", return_value={"600001": "C39计算机、通信和其他电子设备制造业"}), \
             patch.object(m, "get_fundamentals", return_value=data), \
             patch("src.recent_operating.fetch_operating_trend", return_value=data["operating_trend"]) as operating_fetch, \
             patch("src.financial_risk.fetch_financial_risk", return_value=risk) as risk_fetch:
            result = m._screen_quality_pool(config, m.CacheManager(), {"regime": "bull"}, DAY)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["tier"], "formal")
        self.assertEqual(result.iloc[0]["financial_risk_status"], "verified")
        self.assertGreaterEqual(result.iloc[0]["daily_score"], config.MIN_TECHNICAL_SCORE_FORMAL)
        self.assertGreater(result.iloc[0]["historical_pe_samples"], 120)
        operating_fetch.assert_called_once()
        risk_fetch.assert_called_once()

    def test_invalid_new_configuration_cannot_disable_evidence_silently(self):
        cases = [dict(QV_LOW_ANCHOR_MODE="typo"), dict(HISTORICAL_VALUATION_WEIGHT=float("nan")),
                 dict(HISTORICAL_VALUATION_MIN_SAMPLES=251), dict(QV_RECENT_LOW_MAX_AGE=60),
                 dict(QV_TECHNICAL_EVENT_WINDOW=0), dict(CYCLICAL_NORMALIZED_PE_MAX=0)]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                m.StrategyConfig(**case)

    def test_missing_risk_evidence_stays_pending_even_with_good_annual_quality(self):
        signal, reason = self.evaluate()
        self.assertEqual(reason, "PASS")
        self.assertEqual(signal.tier, "pending")
        self.assertIn("financial_risk", signal.missing_tags)

    def test_industry_relative_cheap_cannot_override_expensive_own_history(self):
        frame = prices()
        frame.loc[frame.index[-1], "peTTM"] = 20.
        self.assertEqual(self.evaluate(frame=frame, config=self.config(REQUIRE_FINANCIAL_RISK=False),
                                      val_context={"mode": "industry", "pe_pct": .1})[1], "FAIL_HISTORICAL_VALUATION")

    def test_redundant_floors_are_explicitly_disabled(self):
        config = m.StrategyConfig()
        self.assertEqual((config.MIN_QUALITY_SCORE, config.MIN_QV_SCORE), (0., 0.))

    def test_default_value_state_score_does_not_change_legacy_daily_score(self):
        config = m.StrategyConfig(REQUIRE_FINANCIAL_RISK=False, QV_LOW_ANCHOR_MODE="legacy", REQUIRE_RECENT_OPERATING=False)
        output = m.compute_daily_signals(m._recompute_pct_chg(prices()), config)
        signal, reason = self.evaluate(config=config)
        self.assertEqual(reason, "PASS")
        self.assertEqual(signal.daily_score, output.iloc[-1].qv_daily_score)
        self.assertGreater(signal.daily_score, output.iloc[-1].daily_score)


if __name__ == "__main__":
    unittest.main()
