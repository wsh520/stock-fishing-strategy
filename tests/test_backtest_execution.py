"""Offline execution regressions; synthetic bars only, no external services."""
import argparse
import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

# Execution tests never call the optional market-data provider.
if 'baostock' not in sys.modules and importlib.util.find_spec('baostock') is None:
    sys.modules['baostock'] = types.ModuleType('baostock')

import backtest as bt
from src import volume_breakout_backtest as vb


def frame(rows):
    df = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close', 'volume'])
    df['date'] = pd.bdate_range('2024-01-01', periods=len(df)).strftime('%Y-%m-%d')
    return df


BASE = (10, 10.2, 9.8, 10, 1000)


class ExecutionTests(unittest.TestCase):
    def simulate(self, rows, **overrides):
        df = frame(rows)
        dates = df.date.tolist()
        panel = bt.StockPanel('1', 'A', df, dates, dict(zip(dates, range(len(df)))))
        cfg = bt.BacktestConfig(**dict(dict(FEE_RATE=0, STAMP_DUTY=0, SLIPPAGE_BPS=0), **overrides))
        trade = bt.simulate_trade(cfg, dict(code='1', rec_date=dates[0], stop_loss=9.5, take_profit=11), {'1': panel}, dates)
        return trade, cfg

    def test_t_plus_one_and_entry_day_mark(self):
        t, _ = self.simulate([BASE, (10, 12, 8, 10, 1000), BASE])
        self.assertEqual(t['exit_reason'], 'PERIOD_END')
        self.assertEqual(t['exit_date'], '2024-01-03')
        self.assertEqual(t['mtm'][0][0], t['entry_date'])

    def test_last_day_entry_stays_open_even_with_timeout(self):
        for mode in ('next_open', 'rec_close'):
            rows = [BASE, BASE] if mode == 'next_open' else [BASE]
            t, _ = self.simulate(rows, ENTRY_MODE=mode, MAX_HOLD_DAYS=1)
            self.assertEqual(t['exit_reason'], 'OPEN')
            self.assertIsNone(t['exit_price'])

    def test_gap_stop_uses_open(self):
        t, _ = self.simulate([BASE, BASE, (8, 9, 7.8, 8.5, 1000)])
        self.assertEqual((t['exit_reason'], t['exit_price']), ('STOP_LOSS', 8))

    def test_gap_target_precedes_later_stop(self):
        t, _ = self.simulate([BASE, BASE, (12, 12.2, 9, 10, 1000)])
        self.assertEqual((t['exit_reason'], t['exit_price']), ('TAKE_PROFIT', 12))

    def test_intraday_both_barriers_remains_conservative(self):
        t, _ = self.simulate([BASE, BASE, (10, 12, 9, 10, 1000)])
        self.assertEqual(t['exit_price'], 9.5)

    def test_blocked_entries(self):
        for bar in [(11, 11, 11, 11, 1000), (10, 10.2, 9.8, 10, 0), (float('nan'), 11, 9, 10, 1000)]:
            t, _ = self.simulate([BASE, bar, BASE])
            self.assertEqual(t['exit_reason'], 'ENTRY_BLOCKED')
            self.assertIsNone(t['entry_price'])

    def test_limit_down_and_halt_defer_timeout(self):
        t, _ = self.simulate([BASE, BASE, (9, 9, 9, 9, 1000),
                              (9, 9, 9, 9, 0), (8.5, 9, 8, 8.8, 1000)], MAX_HOLD_DAYS=2)
        self.assertEqual(t['exit_date'], '2024-01-05')
        self.assertEqual(t['exit_price'], 8.5)

    def test_limit_down_at_end_is_not_fake_sale(self):
        t, _ = self.simulate([BASE, BASE, (9, 9, 9, 9, 1000)])
        self.assertEqual(t['exit_reason'], 'OPEN')

    def test_costs_match_portfolio_and_zero_override(self):
        t, cfg = self.simulate([BASE, BASE, BASE], FEE_RATE=.0003, STAMP_DUTY=.0005, SLIPPAGE_BPS=10)
        expected = (9.99 * .9992 / (10.01 * 1.0003) - 1) * 100
        self.assertAlmostEqual(t['return_pct'], expected, places=3)
        curve = bt._build_portfolio_curve(cfg, [t])
        self.assertAlmostEqual(curve['portfolio_return_pct'], expected, places=3)
        free, _ = self.simulate([BASE, BASE, BASE])
        self.assertEqual(free['return_pct'], 0)

    def test_open_position_is_marked_in_portfolio_not_closed_stats(self):
        t, cfg = self.simulate([BASE, BASE, (9, 9, 9, 9, 1000)])
        stats = bt.build_monthly_stats(cfg, [t], frame([BASE] * 3))
        self.assertEqual(stats['per_trade']['n_trades'], 0)
        self.assertEqual(stats['per_trade']['n_open'], 1)
        self.assertEqual(stats['portfolio']['final_value'], 9000)

    def test_cli_costs_and_old_namespace(self):
        parser = argparse.ArgumentParser()
        bt._add_common(parser)
        cfg = bt._apply_args(bt.BacktestConfig(), parser.parse_args(['--fee-rate', '0', '--stamp-duty', '0', '--slippage-bps', '0']))
        self.assertEqual((cfg.FEE_RATE, cfg.STAMP_DUTY, cfg.SLIPPAGE_BPS), (0, 0, 0))
        self.assertEqual(bt._apply_args(bt.BacktestConfig(), argparse.Namespace()).FEE_RATE, .0003)

    def test_breakout_intraday_sales_cannot_fund_open_entries(self):
        a = frame([BASE, BASE, (10, 12, 9.8, 11, 1000), BASE])
        b = frame([BASE] * 4)
        for df in (a, b):
            df['date'] = pd.to_datetime(df.date)
        sig = SimpleNamespace(rank_score=1, score=1, stop_loss=9.5)
        events = [dict(code=c, signal_date=df.date.iloc[i-1], entry_date=df.date.iloc[i], signal=sig)
                  for c, df, i in [('A', a, 1), ('B', b, 2), ('B', b, 3)]]
        strategy = SimpleNamespace(FIXED_STOP_LOSS_PCT_BREAKOUT=5, FIXED_TAKE_PROFIT_PCT_BREAKOUT=10)
        with patch.object(vb, '_candidate_events', return_value=events):
            summary, trades = vb.run_backtest({'A': a, 'B': b}, vb.BacktestConfig(
                position_size_pct=1, max_positions=1, max_holding_days=1,
                fee_rate=0, stamp_duty=0, slippage_bps=0), strategy)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['code'], 'A')
        self.assertEqual(summary['open_positions'], 1)  # B may enter only on day four.
        self.assertEqual(summary['final_equity'], 110000)


if __name__ == '__main__':
    unittest.main()
