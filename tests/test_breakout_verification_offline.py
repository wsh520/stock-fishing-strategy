"""突破终审与通知边界的离线回归；所有外部数据源均替换为 mock。"""
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np
import pandas as pd

from src import volume_breakout_strategy as vb
from notify import feishu


DAY = "2026-06-12"  # Friday
GOOD_FUND = {"roe": 12., "debt_ratio": 30., "goodwill_ratio": 0., "deducted_profit_ratio": 100.}


def daily():
    n = 90
    return pd.DataFrame({
        "date": pd.bdate_range(end=DAY, periods=n).strftime("%Y-%m-%d"),
        "open": 10., "high": 10.6, "low": 9.9, "close": 10.5,
        "volume": 10000000., "amount": 200000000., "pct_chg": 3.,
        "breakout_any": True, "breakout_level": 3, "breakout_margin": 3.,
        "daily_vol_ratio": 2.5, "volume_percentile": 1., "amount_ratio": 2.,
        "body_ratio": .8, "is_bullish_candle": True, "close_to_high": .99,
        "platform_range": 5., "platform_tightness": .01,
        "ma20_rising": True, "ma60_ok": True, "close_above_ma20": True,
        "recent_failed_breakout": False, "rsi14": 55., "atr": .2,
        "daily_score": 90., "level_l1": 10., "daily_turnover_ratio": 2.,
    })


def weekly(end=DAY, n=60):
    return pd.DataFrame({"date": pd.date_range(end=end, periods=n, freq="W-FRI").strftime("%Y-%m-%d"),
                         "close": np.arange(n, dtype=float) + 10})


class BreakoutVerificationTests(unittest.TestCase):
    def config(self, **kwargs):
        return vb.VolumeBreakoutConfig(RECOMMENDATION_MODE="technical", FETCH_DELAY=0,
            REQUIRE_BR_MACD_NOT_WEAK=False, REQUIRE_BR_KDJ_NOT_HIGH=False, **kwargs)

    def evaluate(self, frame=None, **kwargs):
        frame = daily() if frame is None else frame
        with patch.object(vb, "compute_breakout_signals", return_value=frame):
            return vb.evaluate_breakout(frame, "000001", "测试", self.config(),
                                       {"regime": "bull"}, GOOD_FUND, **kwargs)

    def test_evaluate_keeps_pending(self):
        sig, reason = self.evaluate(latest_trade_date=DAY)
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "pending")

    def test_stale_and_invalid_last_trade(self):
        self.assertEqual(self.evaluate(latest_trade_date="2026-06-15")[1], "FAIL_STALE")
        for field, value in [("amount", np.inf), ("volume", 0), ("close", np.nan),
                             ("tradestatus", "0")]:
            frame = daily()
            frame.loc[frame.index[-1], field] = value
            self.assertEqual(self.evaluate(frame, latest_trade_date=DAY)[1], "FAIL_DATA")

    def test_missing_adaptive_volume_metrics_fail_closed(self):
        # The adaptive volume filters require a complete lookback window.  A
        # missing value must not be treated as "no additional filter".
        for field in ("volume_percentile", "amount_ratio"):
            frame = daily()
            frame.loc[frame.index[-1], field] = np.nan
            self.assertEqual(self.evaluate(frame, latest_trade_date=DAY)[1],
                             "FAIL_VOL_INSUFFICIENT")

    def test_short_history_is_rejected_before_indicator_evaluation(self):
        # Default adaptive percentile lookback is 60; a 61-row window cannot
        # provide the full lagged history required by the evaluator.
        self.assertIsNone(vb.compute_breakout_signals(daily().iloc[:61], self.config()))

    def test_l2_failed_breakout_enters_cooldown(self):
        # Keep an older resistance inside the 60-day window but outside the
        # 20-day window, then break L2 and fall back below its anchored level.
        n = 90
        close = np.full(n, 10.0)
        high = np.full(n, 10.1)
        low = np.full(n, 9.9)
        open_ = np.full(n, 10.0)
        high[10:30] = 11.0
        close[10:30] = 10.5
        low[10:30] = 10.0
        open_[10:30] = 10.2
        event = 69
        close[event], high[event], low[event], open_[event] = 10.8, 10.9, 9.95, 10.1
        close[event + 1], high[event + 1], low[event + 1], open_[event + 1] = 9.9, 10.0, 9.8, 9.95
        pct_chg = np.r_[0.0, np.diff(close) / close[:-1] * 100]
        frame = pd.DataFrame({
            "date": pd.bdate_range(end="2026-06-12", periods=n).strftime("%Y-%m-%d"),
            "open": open_, "high": high, "low": low, "close": close,
            "volume": np.full(n, 1e7), "amount": np.full(n, 2e8),
            "pct_chg": pct_chg,
        })
        config = self.config()
        out = vb.compute_breakout_signals(frame, config)
        self.assertIsNotNone(out)
        self.assertEqual(int(out.loc[event, "breakout_level"]), 2)
        self.assertTrue(bool(out.loc[event + 1, "recent_failed_breakout"]))

    def run_pipeline(self, funds=None, weeks=None, config=None, latest=DAY, crash=None, industries=None, filled=None):
        funds = funds or {"000001": GOOD_FUND}
        config = config or self.config(MAX_PICKS=2, USE_INDUSTRY_DEDUP=False)
        stocks = [{"code": code, "name": "股票" + code} for code in funds]
        pending = []
        def screen(stocks, worker, *_):
            return [worker(stock) for stock in stocks], len(stocks), False
        with ExitStack() as stack:
            mocks = {
                "_bs_login": True, "_bs_logout": None,
                "get_market_environment": {"regime": "bull"},
                "get_index_daily": pd.DataFrame({"date": [latest], "close": [100.]}) if latest else None,
                "market_crash_halt": crash, "get_stock_list": stocks,
                "get_daily_data": daily(), "compute_breakout_signals": daily(),
                "get_stock_industry": industries or {},
            }
            handles = {name: stack.enter_context(patch.object(vb, name, return_value=value))
                       for name, value in mocks.items()}
            stack.enter_context(patch.object(vb, "run_concurrent_screen", side_effect=screen))
            stack.enter_context(patch.object(vb, "get_fundamentals", side_effect=lambda code, *_: funds[code]))
            stack.enter_context(patch.object(vb, "_fill_optional_fundamentals", side_effect=lambda code, fund, cfg: filled if filled is not None else fund))
            stack.enter_context(patch.object(vb, "_fetch_weekly_dual", side_effect=lambda code, *_: (weeks or {}).get(code, weekly())))
            result = vb.main_breakout(config, cache=object(), pending_out=pending)
            if crash is not None:
                handles["get_stock_list"].assert_not_called()
        return result, pending

    def test_formal_pipeline_and_notification(self):
        result, pending = self.run_pipeline()
        self.assertEqual(result.tier.tolist(), ["formal"])
        self.assertEqual(result.weekly_status.tolist(), ["confirmed"])
        self.assertEqual(pending, [])
        mixed = pd.concat([result, pd.DataFrame([{"code": "SECRET_PENDING", "tier": "pending"},
                                               {"code": "SECRET_MISSING"}])], ignore_index=True)
        with patch.object(feishu, "_send_feishu") as send:
            feishu.notify_screening_result(mixed, strategy="volume_breakout")
            text = json.dumps(send.call_args.args[0], ensure_ascii=False)
            self.assertIn("000001", text)
            self.assertNotIn("SECRET", text)

    def test_notification_missing_tier_is_not_recommended(self):
        with patch.object(feishu, "_send_feishu") as send:
            feishu.notify_screening_result(pd.DataFrame([{"code": "SECRET"}]))
            text = json.dumps(send.call_args.args[0], ensure_ascii=False)
            self.assertIn("今日无正式推荐", text)
            self.assertNotIn("SECRET", text)

    def test_missing_fund_and_weekly_pending(self):
        for fund in [None, {"roe": np.nan, "debt_ratio": 30.}, {"roe": np.inf, "debt_ratio": 30.}]:
            result, pending = self.run_pipeline(funds={"000001": fund})
            self.assertTrue(result.empty)
            self.assertEqual(pending[0]["tier"], "pending")
        for wk in [weekly(n=3), weekly(end="2026-05-01"), pd.DataFrame(),
                   weekly().assign(close=np.nan)]:
            result, pending = self.run_pipeline(weeks={"000001": wk})
            self.assertTrue(result.empty)
            self.assertEqual(pending[0]["weekly_status"], "unverified")

    def test_missing_market_date_and_crash(self):
        result, pending = self.run_pipeline(latest=None)
        self.assertTrue(result.empty)
        self.assertIn("market_date", pending[0]["missing_tags"])
        result, pending = self.run_pipeline(crash=-8.)
        self.assertIsNone(result)
        self.assertEqual(pending, [])

    def test_independent_weekly_switches_and_rejection(self):
        for trend, macd in [(False, True), (True, False), (False, False)]:
            config = self.config(REQUIRE_WEEKLY_TREND=trend, REQUIRE_WEEKLY_MACD_STABLE=macd,
                                 USE_INDUSTRY_DEDUP=False)
            with patch.object(vb, "check_weekly_trend", return_value=False) as t, \
                 patch.object(vb, "check_weekly_macd", return_value=False) as m:
                result, pending = self.run_pipeline(config=config)
                self.assertEqual(result.empty, trend or macd)
                self.assertEqual(pending, [])
                self.assertEqual(t.called, trend)
                self.assertEqual(m.called, macd)

    def test_refills_slots_after_pending_rejection_and_industry_cap(self):
        funds = {"000001": None, "000002": GOOD_FUND, "000003": GOOD_FUND,
                 "000004": {**GOOD_FUND, "roe": -100.}, "000005": GOOD_FUND}
        industries = {"000002": "A", "000003": "A", "000005": "B"}
        for enabled in [True, False]:
            config = self.config(MAX_PICKS=2, MAX_PICKS_PER_INDUSTRY=1, USE_INDUSTRY_DEDUP=True,
                REQUIRE_WEEKLY_TREND=enabled, REQUIRE_WEEKLY_MACD_STABLE=enabled)
            result, pending = self.run_pipeline(funds=funds, industries=industries, config=config)
            self.assertEqual(result.code.tolist(), ["000002", "000005"])
            self.assertEqual([r["code"] for r in pending], ["000001"])

    def test_late_fund_fill_can_promote_or_reject(self):
        for filled, formal in [(GOOD_FUND, True), ({**GOOD_FUND, "roe": -100.}, False)]:
            # Initial screening sees no fund data; only final verification gets supplementation.
            result, pending = self.run_pipeline(funds={"000001": None}, filled=filled)
            self.assertEqual(not result.empty, formal)
            self.assertEqual(pending, [])

    def test_unclosed_bar_cannot_supply_required_history(self):
        wk = weekly(n=34)
        wk.loc[len(wk)] = ["2026-06-17", 100.]
        # Weekly data are checked after removal even if the source returns a future bar.
        result, pending = self.run_pipeline(weeks={"000001": wk})
        self.assertTrue(result.empty)
        self.assertEqual(pending[0]["weekly_status"], "unverified")


if __name__ == "__main__":
    unittest.main()
