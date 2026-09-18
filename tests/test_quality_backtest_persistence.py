"""Offline regression checks for recommendation tiers and point-in-time replay."""
import importlib.util
import sys
import types
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

if "baostock" not in sys.modules and importlib.util.find_spec("baostock") is None:
    sys.modules["baostock"] = types.ModuleType("baostock")

import pandas as pd
import backtest as bt
from store import mysql_store as store
from src import volume_breakout_backtest as vb


class RecommendationTests(unittest.TestCase):
    def test_store_requires_explicit_formal(self):
        with patch.object(store, "is_configured", return_value=True), patch.object(store, "_connect") as connect:
            self.assertEqual(store.save_recommendations(pd.DataFrame([{"code": "1"}])), 0)
            self.assertEqual(store.save_recommendations(pd.DataFrame([{"tier": None}, {"tier": "pending"}])), 0)
            connect.assert_not_called()
            cur = connect.return_value.cursor.return_value.__enter__.return_value
            cur.executemany.return_value = 1
            rows = pd.DataFrame([dict(date="2024-01-01", code="1", name="A", close=10, tier=t)
                                 for t in ("formal", "pending", None)])
            with patch.object(store, "_ensure_tables"):
                self.assertEqual(store.save_recommendations(rows), 1)
            self.assertEqual(len(cur.executemany.call_args.args[1]), 1)
            self.assertEqual(cur.executemany.call_args.args[1][0][20], "formal")

    def test_announcements_and_cache_are_asof(self):
        class Result:
            error_code = "0"
            data = [1]
            def __init__(self, field, value):
                self.field, self.value = field, value
            def get_data(self):
                return pd.DataFrame([{"pubDate": "2024-04-20", self.field: self.value}])
        with TemporaryDirectory() as tmp:
            bc = bt.BacktestConfig(CACHE_DIR=tmp, OUT_DIR=tmp)
            bt._ensure_dirs(bc)
            with patch.object(bt, "_BS_OK", True), patch.object(bt, "_quarter_candidates", return_value=[(2024, 1)]), \
                 patch.object(bt.bs, "query_profit_data", create=True, return_value=Result("roeAvg", .03)) as query, \
                 patch.object(bt.bs, "query_balance_data", create=True, return_value=Result("liabilityToAsset", .4)), \
                 patch.object(bt, "_fetch_fund_ak_asof", return_value=None):
                later = bt.fetch_fundamentals_asof(bc, "600000", "2024-04-22")
                self.assertEqual(later["roe"], 12)
                self.assertEqual(later["debt_ratio"], 40)
                self.assertIsNone(bt.fetch_fundamentals_asof(bc, "600000", "2024-04-19"))
                self.assertEqual(query.call_count, 2)
                self.assertEqual(bt.fetch_fundamentals_asof(bc, "600000", "2024-04-22"), later)
                self.assertEqual(query.call_count, 2)
        self.assertTrue(bt._published_asof(pd.DataFrame([{"日期": "2023-12-31"}]), "2024-05-01").empty)

    def test_quality_rechecks_then_ranks_without_weekly_gate(self):
        dates = pd.bdate_range("2022-01-01", periods=400).strftime("%Y-%m-%d").tolist()
        panels = {c: bt.StockPanel(c, c, pd.DataFrame({"date": dates}), dates,
                                  {d: i for i, d in enumerate(dates)}) for c in ("1", "2", "3")}
        cfg = types.SimpleNamespace(RECOMMENDATION_MODE="quality_value", LOW_POSITION_LOOKBACK=250,
                                    MAX_PICKS=2, MAX_PICKS_PER_INDUSTRY=1)
        seen = []
        def evaluate(df, code, name, config, env, fund, latest_trade_date):
            seen.append((code, len(df), fund, latest_trade_date))
            tier = "pending" if fund is None or code == "3" else "formal"
            score = {"1": 1, "2": 99, "3": 80}[code] if fund else 100
            return pd.Series(dict(code=code, tier=tier, rank_score=score)), "PASS"
        with patch.object(bt, "evaluate", side_effect=evaluate), \
             patch.object(bt, "compute_market_environment", return_value={"regime": "bear"}), \
             patch.object(bt, "fetch_fundamentals_asof", return_value={"debt_ratio": 20}) as fund, \
             patch.object(bt, "enrich_annual_fundamentals", side_effect=lambda c, f, cfg, as_of: {**f, "annual_rows": []}) as enrich, \
             patch.object(bt, "check_weekly_trend", side_effect=AssertionError("weekly gate")), \
             patch.object(bt, "_fund_verify_state", side_effect=AssertionError("legacy tier override")):
            formal, pending, _ = bt._screen_day(bt.BacktestConfig(DAILY_EVAL_BARS=120), cfg, dates[-1],
                                               panels, pd.DataFrame(), {"1": "bank", "2": "bank", "3": "bank"})
        self.assertEqual([r["code"] for r in formal], ["2"])
        self.assertEqual([r["code"] for r in pending], ["3"])
        self.assertEqual(len(seen), 6)
        self.assertTrue(all(n >= 350 and day == dates[-1] for _, n, _, day in seen))
        self.assertEqual(enrich.call_args.kwargs["as_of"], dates[-1])
        self.assertEqual(fund.call_count, 3)

    def test_default_ohlcv_without_financials_has_zero_trades(self):
        dates = pd.bdate_range("2022-01-03", periods=360)
        prices = [15 - 5 * i / 359 for i in range(360)]
        frame = pd.DataFrame(dict(date=dates, open=prices, close=prices,
                                  high=[p * 1.01 for p in prices], low=[p * .99 for p in prices],
                                  volume=[10_000_000] * 360, amount=[100_000_000] * 360,
                                  pct_chg=[0] * 360, pe_ttm=[8] * 360, pb_mrq=[1] * 360))
        summary, trades = vb.run_backtest({"600000": frame})
        self.assertEqual(summary["signals"], 0)
        self.assertEqual(summary["open_positions"], 0)
        self.assertEqual(trades, [])

    def test_breakout_events_fail_closed_and_rank(self):
        dates = pd.date_range("2024-01-01", periods=3)
        frame = pd.DataFrame(dict(date=dates, open=[10]*3, high=[10]*3, low=[10]*3, close=[10]*3, volume=[100]*3))
        strategy = types.SimpleNamespace(MIN_DAYS=2, FIXED_STOP_LOSS_PCT_BREAKOUT=5,
                                         FIXED_TAKE_PROFIT_PCT_BREAKOUT=10)
        for tier in (None, "pending"):
            sig = types.SimpleNamespace(tier=tier)
            with patch.object(vb, "evaluate_breakout", return_value=(sig, "PASS")):
                self.assertEqual(vb._candidate_events({"1": frame}, strategy, "bull"), [])
        def evaluate(hist, code, **kwargs):
            return types.SimpleNamespace(tier="formal", rank_score=int(code), score=1,
                                         stop_loss=9.5), "PASS"
        with patch.object(vb, "evaluate_breakout", side_effect=evaluate):
            summary, trades = vb.run_backtest({"1": frame, "2": frame},
                                              vb.BacktestConfig(max_positions=1), strategy)
        self.assertEqual(summary["signals"], 2)
        # Verify selection order directly by observing rank score in generated candidates.
        events = [dict(entry_date=dates[1], code=c, signal=types.SimpleNamespace(rank_score=s, score=1, stop_loss=9.5))
                  for c, s in (("1", 1), ("2", 99))]
        for e in events:
            e.update(signal_date=dates[0], entry_index=1, df=frame)
        with patch.object(vb, "_candidate_events", return_value=events):
            _, trades = vb.run_backtest({"1": frame, "2": frame},
                                       vb.BacktestConfig(max_positions=1, max_holding_days=1), strategy)
        self.assertEqual(trades[0]["code"], "2")


if __name__ == "__main__":
    unittest.main()
