"""突破升级离线回归：真实行情指标 + 事件资格 + 持续确认 + 排序边界。"""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src import volume_breakout_strategy as vb
import test_breakout_verification_offline as offline
from test_breakout_verification_offline import GOOD_FUND, DAY


def prices(kind="held", event_volume=2.5, event_pct=5.0):
    n = 100
    close = 10 + np.sin(np.arange(n) * 1.7) * .025
    frame = pd.DataFrame({"date": pd.bdate_range(end=DAY, periods=n).strftime("%Y-%m-%d"),
        "open": close - .01, "high": 10.1, "low": 9.9, "close": close,
        "volume": 1e7, "amount": 1e8, "pct_chg": np.r_[0., np.diff(close) / close[:-1] * 100]})
    frame.loc[97, ["open", "high", "low", "close", "volume", "amount", "pct_chg"]] = [10., 10.52, 9.98, 10.5, event_volume * 1e7, event_volume * 1e8, event_pct]
    frame.loc[98, ["open", "high", "low", "close", "pct_chg"]] = [10.5, 10.56, 10.4, 10.52, .19]
    frame.loc[99, ["open", "high", "low", "close", "pct_chg"]] = [10.52, 10.57, 10.4, 10.53, .095]
    if kind == "retest":
        frame.loc[99, "low"] = 10.15
    elif kind == "failed":
        frame.loc[98, "close"] = 10.0
    return frame


def cfg(**kwargs):
    options = dict(REQUIRE_BR_KDJ_NOT_HIGH=False, REQUIRE_BR_MACD_NOT_WEAK=False,
                   MIN_PASS_GRADE="D", REQUIRE_RECENT_OPERATING=False, REQUIRE_FINANCIAL_RISK=False)
    options.update(kwargs)
    return vb.VolumeBreakoutConfig(**options)


class BreakoutUpgradeTests(unittest.TestCase):
    def evaluate(self, frame=None, config=None, index=None):
        frame = prices() if frame is None else frame
        return vb.evaluate_breakout(frame, "600001", "工业", config or cfg(), {"regime": "bull"},
                                    GOOD_FUND, latest_trade_date=str(frame.date.iloc[-1]), index_df=index)

    def test_defaults_and_explicit_quality_mode(self):
        self.assertEqual(vb.VolumeBreakoutConfig().RECOMMENDATION_MODE, "technical")
        self.assertTrue(vb.VolumeBreakoutConfig().WEEKLY_MA_BOTH_REQUIRED)
        self.assertFalse(cfg(WEEKLY_MA_BOTH_REQUIRED=False).WEEKLY_MA_BOTH_REQUIRED)
        base, _ = self.evaluate(prices().iloc[:98])
        with patch.object(vb, "evaluate_quality_value", return_value=(base, "PASS")) as evaluate:
            signal, reason = self.evaluate(config=cfg(RECOMMENDATION_MODE="quality_value"))
            self.assertEqual(reason, "PASS")
            self.assertEqual(signal.code, base.code)
            evaluate.assert_called_once()

    def test_runner_modes_and_pending_logging(self):
        import run_breakout
        self.assertEqual(run_breakout._parse_args([]).recommendation_mode, "technical")
        self.assertEqual(run_breakout._parse_args(["--recommendation-mode", "quality_value"]).recommendation_mode, "quality_value")
        from unittest.mock import Mock
        log = Mock()
        rows = [{"code": "A", "missing_tags": "weekly,financial_risk"},
                {"code": "B", "missing_tags": "weekly"}]
        vb.log_breakout_pending(rows, log)
        self.assertEqual(log.info.call_args_list[0].args[1:], (2, {"weekly": 2, "financial_risk": 1}))
        self.assertEqual(log.info.call_count, 3)

    def test_weekly_default_rejects_only_rising_but_below_ma(self):
        closes = np.linspace(10., 20., 50)
        closes[-1] = 15.
        weekly = pd.DataFrame({"date": pd.date_range(end=DAY, periods=50, freq="W-FRI").strftime("%Y-%m-%d"), "close": closes})
        self.assertFalse(vb.check_weekly_trend(weekly, cfg()))
        self.assertTrue(vb.check_weekly_trend(weekly, cfg(WEEKLY_MA_BOTH_REQUIRED=False)))

    def test_first_event_fixed_anchor_and_score_composition(self):
        frame = prices().iloc[:98]
        signal, reason = self.evaluate(frame)
        self.assertEqual(reason, "PASS")
        self.assertEqual(signal.breakout_category, "first_breakout")
        self.assertAlmostEqual(signal.breakout_anchor, 10.1)
        self.assertEqual(signal.breakout_event_date, frame.date.iloc[-1])
        self.assertAlmostEqual(signal.volume_br_score,
                               signal.volume_ratio_score + signal.consolidation_score, places=2)
        self.assertLessEqual(signal.volume_ratio_score, 17.5)
        self.assertLessEqual(signal.consolidation_score, 7.5)
        parts = sum(getattr(signal, k) for k in ("breakout_score", "volume_br_score", "pattern_score", "trend_br_score", "momentum_br_score"))
        self.assertAlmostEqual(parts, signal.daily_score, delta=.051)

    def test_held_and_retest_do_not_require_new_event_candle_or_volume(self):
        for kind, expected in [("held", "held_confirmed"), ("retest", "retest_confirmed")]:
            frame = prices(kind)
            signal, reason = self.evaluate(frame)
            self.assertEqual(reason, "PASS")
            self.assertEqual(signal.breakout_category, expected)
            self.assertEqual(signal.breakout_event_date, frame.date.iloc[97])
            self.assertEqual(signal.breakout_confirmation_date, DAY)
            self.assertLess(signal.vol_ratio, cfg().VOLUME_BREAKOUT_MIN)
            self.assertAlmostEqual(signal.breakout_anchor, 10.1)
            self.assertIn("事件五维子分", vb.describe_breakout(signal.to_dict()))
            self.assertIn("量比", vb.describe_breakout(signal.to_dict()))

    def test_confirmation_cannot_rescue_ineligible_event(self):
        for frame in (prices(event_volume=1.0), prices(event_pct=.5), prices("failed")):
            signal, reason = self.evaluate(frame)
            self.assertIsNone(signal)
            self.assertEqual(reason, "FAIL_NO_BREAKOUT")
        self.assertIsNone(self.evaluate(config=cfg(RECENT_BREAKOUT_DAYS=0))[0])

    def test_confirmation_requires_current_persistent_state(self):
        frame = prices()
        out = vb.compute_breakout_signals(frame, cfg())
        after = out.iloc[98:].copy()
        for key, value in [("ma20_rising", False), ("close_above_ma20", False),
                           ("recent_failed_breakout", True), ("atr", 1.0), ("close", 12.0)]:
            altered = after.copy()
            altered.loc[altered.index[-1], key] = value
            self.assertIsNone(vb.check_breakout_confirmation(altered, 10.1, cfg()))

    def test_prefix_equivalence_no_future_data(self):
        frame = prices()
        full = vb.compute_breakout_signals(frame, cfg())
        prefix = vb.compute_breakout_signals(frame.iloc[:98], cfg())
        for key in ("breakout_any", "breakout_anchor", "daily_score", "early_start", "recent_failed_breakout"):
            pd.testing.assert_series_equal(full[key].iloc[:98], prefix[key])
        altered = frame.copy()
        altered.loc[98:, ["close", "high", "volume"]] = [100., 101., 1e10]
        newer = vb.compute_breakout_signals(altered, cfg())
        pd.testing.assert_series_equal(full.daily_score.iloc[:98], newer.daily_score.iloc[:98])

    def test_shared_current_resistance_and_passive_drop_default_excluded(self):
        # L3 均线下降，使昨收已经站上当前锚点；不应声称今日价格主动跨越。
        frame = prices()
        frame.loc[96:99, "close"] = [13., 10.2, 10.5, 10.5]
        frame["high"] = frame.close + .1
        out = vb.compute_breakout_signals(frame, cfg(BREAKOUT_MA_LONG=3, REQUIRE_L1_OR_L2=False))
        self.assertTrue(out.resistance_drop_trigger.iloc[-1])
        self.assertFalse(out.breakout_any.iloc[-1])
        allowed = vb.compute_breakout_signals(frame, cfg(BREAKOUT_MA_LONG=3, REQUIRE_L1_OR_L2=False, ALLOW_RESISTANCE_DROP_BREAKOUT=True))
        self.assertTrue(allowed.breakout_l3.iloc[-1])
        # 每个主动事件的昨收都必须低于/等于当前同级锚点阈值。
        out = vb.compute_breakout_signals(prices(), cfg())
        events = out.index[out.breakout_any]
        for i in events:
            self.assertLessEqual(out.close.iloc[i - 1], out.breakout_anchor.iloc[i] * 1.005)

    def test_optimal_margin_is_bounded_and_configurable(self):
        config = cfg()
        self.assertEqual(vb.breakout_margin_quality(2., config), 1.)
        self.assertLess(vb.breakout_margin_quality(5., config), 1.)
        self.assertEqual(vb.breakout_margin_quality(7., config), 0.)
        self.assertEqual(vb.breakout_margin_quality(4., cfg(BREAKOUT_MARGIN_OPTIMAL_HIGH=4.)), 1.)
        with self.assertRaises(ValueError):
            vb.breakout_margin_quality(2., cfg(BREAKOUT_MARGIN_OPTIMAL_HIGH=8.))

    def test_early_start_label_uses_only_prior_two_days(self):
        frame = prices().iloc[:98].copy()
        frame.loc[95, ["pct_chg", "volume"]] = [3., 1.5e7]
        out = vb.compute_breakout_signals(frame, cfg())
        self.assertTrue(out.early_start.iloc[-1])
        frame.loc[95, "volume"] = 1e7
        self.assertFalse(vb.compute_breakout_signals(frame, cfg()).early_start.iloc[-1])

    def test_relative_strength_alignment_missing_and_bounded(self):
        stock = prices()
        benchmark = stock[["date", "close"]].copy()
        benchmark["close"] = 100.
        expected = vb.breakout_period_return(stock, DAY, 20)[2]
        self.assertAlmostEqual(vb.compute_breakout_relative_strength(stock, benchmark, DAY), expected)
        future = pd.concat([benchmark, pd.DataFrame({"date": ["2099-01-01"], "close": [1.]})])
        self.assertEqual(vb.compute_breakout_relative_strength(stock, future, DAY), expected)
        self.assertIsNone(vb.compute_breakout_relative_strength(stock, benchmark.iloc[:-1], DAY))
        self.assertEqual(vb.breakout_strength_adjustment(None, np.nan, cfg()), 0.)
        self.assertEqual(vb.breakout_strength_adjustment(1000., 1000., cfg()), 3.)
        self.assertEqual(vb.breakout_strength_adjustment(-1000., -1000., cfg()), -3.)
        base, _ = self.evaluate()
        strong, _ = self.evaluate(index=benchmark)
        self.assertEqual((base.score, base.grade), (strong.score, strong.grade))
        self.assertLessEqual(abs(strong.ranking_score - strong.score), 3.)

    def test_industry_snapshot_same_period_and_reuse(self):
        period = ("2026-05-01", DAY, 3.)
        snapshot = vb.build_breakout_industry_snapshot([("A", period), ("B", (*period[:2], 1.)), ("C", None)], {"A": "工业", "B": "工业", "C": "工业"})
        self.assertEqual(snapshot[("工业", *period[:2])], {"A": 3., "B": 1.})

    def test_financial_risk_finalization_missing_failed_and_verified(self):
        from src.financial_risk import evaluate_financial_risk
        config = cfg(MAX_PICKS=2, FETCH_DELAY=0, USE_INDUSTRY_DEDUP=False, REQUIRE_FINANCIAL_RISK=True)
        pipeline = offline.BreakoutVerificationTests()
        for debt, expected in [(None, "pending"), (900., "rejected"), (300., "formal")]:
            rows = [] if debt is None else [dict(report_date="2026-03-31", available_date="2026-04-20",
                total_assets=1000., total_liabilities=debt, goodwill=0., net_profit=100., deducted_profit=90.,
                source="offline", statement_basis="consolidated", currency="CNY")]
            evidence = dict(evaluate_financial_risk(rows, DAY, debt_max=config.MAX_DEBT_RATIO,
                goodwill_max=config.MAX_GOODWILL_RATIO, deducted_ratio_min=config.MIN_DEDUCTED_PROFIT_RATIO), code="000001")
            with patch.object(vb, "enrich_financial_risk", side_effect=lambda code, fund, *args, **kw: dict(fund, financial_risk=evidence)):
                result, pending = pipeline.run_pipeline(config=config)
            self.assertEqual(not result.empty, expected == "formal")
            self.assertEqual(bool(pending), expected == "pending")
            if pending:
                self.assertIn("financial_risk", pending[0]["missing_tags"])
            if expected == "formal":
                self.assertEqual(result.financial_risk_status.tolist(), ["verified"])

    def test_industry_strength_main_flow_excludes_self_and_keeps_grade(self):
        template, _ = self.evaluate(prices().iloc[:98])
        config = cfg(MAX_PICKS=4, FETCH_DELAY=0, USE_INDUSTRY_DEDUP=False)
        codes = ["000001", "000002", "000003", "000004"]
        periods = [("2026-05-01", DAY, ret) for ret in (4., 1., 1., 1.)]
        def signal(_, code, name, *args, **kwargs):
            data = template.to_dict()
            data.update(code=code, name=name, date=DAY)
            return vb.BreakoutSignal(**data), "PASS"
        with patch.object(vb, "evaluate_breakout", side_effect=signal), \
             patch.object(vb, "breakout_period_return", side_effect=periods) as returns:
            result, _ = offline.BreakoutVerificationTests().run_pipeline(
                config=config, funds={code: GOOD_FUND for code in codes}, industries={code: "工业" for code in codes})
        self.assertEqual(returns.call_count, 4)
        self.assertEqual(result.iloc[0].code, "000001")
        self.assertEqual(result.iloc[0].industry_relative_strength, 3.)
        self.assertAlmostEqual(result.iloc[0].relative_strength_adjustment, .45)
        self.assertEqual(result.score.nunique(), 1)
        self.assertEqual(result.grade.nunique(), 1)

    def test_confirmed_event_can_complete_formal_screening(self):
        # 复用原终审离线装置，但使用真实信号计算，确保确认也进入正式筛选。
        pipeline = offline.BreakoutVerificationTests()
        config = cfg(MAX_PICKS=2, FETCH_DELAY=0, USE_INDUSTRY_DEDUP=False)
        real = vb.compute_breakout_signals
        with patch.object(vb, "compute_breakout_signals", side_effect=lambda *_: real(prices(), config)):
            # run_pipeline 自己会 mock 指标，故在 evaluate 层替换为真实行情与函数。
            evaluate = vb.evaluate_breakout
            def actual(frame, *args, **kwargs):
                with patch.object(vb, "compute_breakout_signals", wraps=real):
                    return evaluate(prices() if "breakout_any" in frame.columns else frame, *args, **kwargs)
            with patch.object(vb, "evaluate_breakout", side_effect=actual):
                result, pending = pipeline.run_pipeline(config=config)
        self.assertEqual(result.tier.tolist(), ["formal"])
        self.assertEqual(result.breakout_category.tolist(), ["held_confirmed"])
        self.assertEqual(pending, [])


if __name__ == "__main__":
    unittest.main()
