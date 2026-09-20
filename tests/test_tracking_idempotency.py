"""Offline tracking regressions. No real database, providers or notifications."""
import unittest
import sys
import types
from unittest.mock import MagicMock, patch

import pandas as pd
from store import mysql_store as store
from run_weekly_tracking import select_observations
from run_monthly_attribution import build_stats


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.dates = pd.bdate_range('2024-01-02', periods=26)
        self.market = pd.DataFrame({'date': self.dates})
        self.daily = pd.DataFrame({'date': self.dates, 'close': range(10, 36), 'volume': 100})
        self.rec = dict(id=1, rec_date=self.dates[0], tracked_weeks=4)

    def test_fixed_horizons_and_catchup_ignore_old_week_count(self):
        rows = select_observations(self.rec, self.daily, self.market)
        self.assertEqual([r['holding_trade_days'] for r in rows], [5, 10, 15, 20])
        self.assertEqual([r['close_price'] for r in rows], [15, 20, 25, 30])
        self.assertEqual(rows[-1]['close_date'], str(self.dates[20].date()))

    def test_repeat_and_old_suspended_quote(self):
        self.rec.update(last_close_date=self.dates[5], tracked_close_dates=str(self.dates[5].date()))
        self.assertEqual(select_observations(self.rec, self.daily.iloc[:6], self.market), [])
        self.daily.loc[5, 'volume'] = 0
        rows = select_observations(dict(id=1, rec_date=self.dates[0]), self.daily, self.market)
        self.assertEqual([r['holding_trade_days'] for r in rows], [10, 15, 20])

    def test_calendar_coverage_and_missing_target(self):
        self.assertEqual(select_observations(self.rec, self.daily, self.market.iloc[1:]), [])
        rows = select_observations(self.rec, self.daily.drop(index=5), self.market)
        self.assertEqual([r['holding_trade_days'] for r in rows], [10, 15, 20])
        self.rec['tracked_horizons'] = '10,15,20'
        self.assertEqual(select_observations(self.rec, self.daily.drop(index=5), self.market), [])

    def test_duplicate_unsorted_dates_do_not_shift_horizon(self):
        daily = pd.concat([self.daily, self.daily]).iloc[::-1]
        market = pd.concat([self.market, self.market]).iloc[::-1]
        self.assertEqual(select_observations(self.rec, daily, market),
                         select_observations(self.rec, self.daily, self.market))


class RunnerTests(unittest.TestCase):
    def test_run_catchup_then_rerun_without_real_io(self):
        import run_weekly_tracking as weekly
        dates = pd.bdate_range('2024-01-02', periods=26)
        frame = pd.DataFrame(dict(date=dates, close=range(10, 36), volume=100))
        rec = dict(id=1, code='600000', name='test', rec_date=dates[0], rec_close=10,
                   strategy='bottom_fishing', tracked_weeks=4)
        provider = types.ModuleType('src.bottom_fishing_strategy')
        provider.StrategyConfig = lambda: types.SimpleNamespace(CACHE_EXPIRE_HOURS=1, MAX_WORKERS=1)
        provider.CacheManager = MagicMock()
        provider.get_daily_data = MagicMock(return_value=frame)
        provider.get_index_daily = MagicMock(return_value=frame)
        provider._bs_login = MagicMock(return_value=True)
        provider._bs_logout = MagicMock()
        provider._bs_state = {'circuit_open': False}
        provider._AK_AVAILABLE = False
        notifier = types.ModuleType('notify.feishu')
        notifier.notify_tracking_result = MagicMock()
        with patch.dict(sys.modules, {'src.bottom_fishing_strategy': provider, 'notify.feishu': notifier}), \
             patch.object(store, 'is_configured', return_value=True), \
             patch.object(store, 'get_active_recommendations', return_value=[rec]), \
             patch.object(store, 'save_tracking', return_value=True) as save, \
             patch.object(weekly, '_setup_logging'):
            weekly.run([])
            self.assertEqual(save.call_count, 4)
            self.assertEqual(notifier.notify_tracking_result.call_count, 1)
            rec['tracked_horizons'] = '5,10,15,20'
            weekly.run([])
            self.assertEqual(save.call_count, 4)
            self.assertEqual(notifier.notify_tracking_result.call_count, 1)
            self.assertEqual(provider._bs_logout.call_count, 2)


class StoreTests(unittest.TestCase):
    def test_save_date_guard_and_unique_db_contract(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.execute.return_value = 1
        with patch.object(store, 'is_configured', return_value=True), patch.object(store, '_connect', return_value=conn), patch.object(store, '_ensure_tables'):
            self.assertTrue(store.save_tracking(1, '2024-01-02', '1', 1, '2024-01-09', 11, 10, 5))
            sql, args = cur.execute.call_args.args
            self.assertIn('WHERE rec_id = %s AND close_date = %s', sql)
            self.assertEqual(args[-3:], (5, 1, '2024-01-09'))
            cur.execute.return_value = 0
            self.assertFalse(store.save_tracking(1, '2024-01-02', '1', 2, '2024-01-09', 11, 10))
            self.assertFalse(store.save_tracking(1, '2024-01-02', '1', 1, '2024-01-02', 11, 10))
        ddl = store._DDL[1]
        self.assertIn('UNIQUE KEY uk_rec_close_date (rec_id, unique_close_date)', ddl)
        self.assertIn('UNIQUE KEY uk_rec_horizon', ddl)
        self.assertNotIn('UNIQUE KEY uk_rec_week', ddl)

    def test_migration_preserves_history_and_is_repeatable(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)
        cur.fetchall.side_effect = [[], [('uk_rec_week',)]]
        store._ensure_tracking_schema(conn)
        statements = [c.args[0] for c in cur.execute.call_args_list]
        self.assertTrue(any('earlier.id < t.id SET t.legacy_duplicate = 1' in s for s in statements))
        self.assertFalse(any('DELETE ' in s or 'TRUNCATE ' in s for s in statements))
        cur.reset_mock()
        cur.fetchall.side_effect = [[(c,) for c in ('holding_trade_days', 'legacy_duplicate', 'unique_close_date')],
                                   [(c,) for c in ('idx_rec_week', 'uk_rec_close_date', 'uk_rec_horizon')]]
        store._ensure_tracking_schema(conn)
        self.assertFalse(any('ALTER ' in c.args[0] or 'UPDATE ' in c.args[0] for c in cur.execute.call_args_list))

    def test_active_query_uses_distinct_dates_and_horizons(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.description = []
        cur.fetchall.return_value = []
        with patch.object(store, 'is_configured', return_value=True), patch.object(store, '_connect', return_value=conn), patch.object(store, '_ensure_tables'):
            store.get_active_recommendations()
        sql = cur.execute.call_args.args[0]
        self.assertIn('MAX(t.close_date) AS last_close_date', sql)
        self.assertIn('COUNT(DISTINCT t.close_date)', sql)
        self.assertIn('HAVING completed_horizons', sql)
        self.assertNotIn('MAX(t.week_no)', sql)


class AttributionTests(unittest.TestCase):
    def row(self, ident, horizon, ret, strategy='bottom_fishing'):
        return dict(id=ident, rec_date='2024-01-02', close_date=str(pd.Timestamp('2024-01-02') + pd.Timedelta(days=horizon or 7)),
                    holding_trade_days=horizon, return_pct=ret, week_no=1, has_divergence=0,
                    grade='A', market_env='bull', daily_score=70, strategy=strategy)

    def test_fixed_maturity_no_latest_mixing_and_strategy_split(self):
        rows = [self.row(1, 5, 100), self.row(1, 20, 2), self.row(2, 5, 90),
                self.row(3, 20, -4, 'volume_breakout'), self.row(4, None, 99)]
        rows.append(dict(rows[0]))
        stats = build_stats(rows, 90)
        self.assertEqual(stats['tracked_recs'], 2)
        self.assertEqual(stats['avg_return'], -1)
        self.assertEqual(stats['excluded_unknown_horizon'], 1)
        groups = stats['by_strategy_horizon']
        self.assertEqual(len(groups), 3)
        self.assertEqual(next(g for g in groups if g['holding_trade_days'] == 5)['n'], 2)
        self.assertEqual(len(stats['by_strategy']), 2)

    def test_empty_and_legacy_only(self):
        for rows in ([], [self.row(1, None, 2)]):
            stats = build_stats(rows, 90)
            self.assertEqual(stats['tracked_recs'], 0)
            self.assertEqual(stats['avg_return'], 0)
            self.assertEqual(stats['by_week'], [])


if __name__ == '__main__':
    unittest.main()
