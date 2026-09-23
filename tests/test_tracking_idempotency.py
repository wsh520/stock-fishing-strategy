"""Offline tracking regressions. No real database, providers or notifications."""
import json
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

    def test_ex_adjustment_uses_same_series_base_price(self):
        """除权除息：分母必须取「同一次拉取」序列里的推荐日收盘价。

        前复权以最新交易日为锚点，除权后历史价会被整体重算 —— 落库的「推荐日当时」价格
        与追踪时拉到的收盘价不再同基准。模拟 10 送 10（同序列推荐日与目标日都是 5.00，
        含权收益 0%）：若拿落库价 10.00 当分母就会算出 −50% 的假暴跌。
        """
        daily = pd.DataFrame({'date': self.dates, 'close': [5.0] * 26, 'volume': 100})
        rec = dict(id=1, rec_date=self.dates[0], rec_close=10.0)   # 落库值 = 除权前价格
        rows = select_observations(rec, daily, self.market)
        self.assertEqual([r['holding_trade_days'] for r in rows], [5, 10, 15, 20])
        self.assertEqual({r['base_close'] for r in rows}, {5.0})   # 同序列基准价
        self.assertEqual({r['close_price'] for r in rows}, {5.0})  # 同序列目标价
        # 用同序列基准价算收益 → 0%，用落库价算 → −50%
        self.assertEqual(rows[0]['close_price'] / rows[0]['base_close'] - 1, 0.0)


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

    def test_run_uses_same_series_price_as_return_denominator(self):
        """落库的是除权前的推荐价 10.00，同序列基准价是 5.00 —— 必须用后者做分母。"""
        import run_weekly_tracking as weekly
        dates = pd.bdate_range('2024-01-02', periods=26)
        frame = pd.DataFrame(dict(date=dates, close=[5.0] * 26, volume=100))
        rec = dict(id=1, code='600000', name='test', rec_date=dates[0], rec_close=10.0,
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
        kwargs = save.call_args_list[0].kwargs
        self.assertEqual(kwargs['rec_close'], 5.0)                       # 同序列基准价，而非落库的 10.0
        report = notifier.notify_tracking_result.call_args.args[0]
        self.assertEqual(set(report['return_pct']), {0.0})               # 含权收益 0%，不是 −50%
        self.assertEqual(set(report['rec_price']), {5.0})


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
    def row(self, ident, horizon, ret, strategy='bottom_fishing', grade='A',
            market_env='bull', daily_score=70):
        return dict(id=ident, rec_date='2024-01-02', close_date=str(pd.Timestamp('2024-01-02') + pd.Timedelta(days=horizon or 7)),
                    holding_trade_days=horizon, return_pct=ret, week_no=1, has_divergence=0,
                    grade=grade, market_env=market_env, daily_score=daily_score, strategy=strategy)

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

    def test_observation_group_labels_peak_as_extreme_not_mean(self):
        """逐条观测分组（by_week）：样本单位是「条观测」，第二列是组内极值而非均值。

        报表把这一列渲染成「平均峰值」时，用户会把 max 当 mean 读（数值被系统性放大），
        故必须由 peak_label / n_unit 显式改名，且策略名要中文化。
        """
        rows = [self.row(1, 5, 100), self.row(1, 20, 2), self.row(2, 5, 90), self.row(2, 20, -4)]
        stats = build_stats(rows, 90)
        g5 = next(g for g in stats['by_week'] if g['holding_trade_days'] == 5)
        self.assertEqual(g5['avg_return'], 95.0)      # (100 + 90) / 2
        self.assertEqual(g5['avg_peak'], 100.0)       # 组内最高收益，不是 95
        self.assertEqual(g5['peak_label'], '最高收益')
        self.assertEqual(g5['n_unit'], '条观测')
        self.assertIn('优质低估低位', g5['label'])
        self.assertNotIn('bottom_fishing', g5['label'])

    def test_null_dimensions_remain_visible_and_partition_sums(self):
        """market_env / grade 为 NULL 的推荐不能被 groupby 静默丢弃。

        丢弃会让「各档样本数之和 < 总数」（用户一眼看出加不齐），而且「未知」档恰恰是
        数据质量最该暴露的一档。补缺后要求各分组之和等于总样本数。
        """
        rows = [self.row(1, 20, 1), self.row(2, 20, 2, grade=None, market_env=None)]
        stats = build_stats(rows, 90)
        self.assertEqual(stats['tracked_recs'], 2)
        self.assertEqual(sum(g['n'] for g in stats['by_market']), 2)
        self.assertEqual(sum(g['n'] for g in stats['by_grade']), 2)
        self.assertIn('未知', [g['label'] for g in stats['by_market']])
        self.assertIn('未知', [g['label'] for g in stats['by_grade']])

    def test_score_bucket_boundary_counts_60_as_passing_bucket(self):
        """恰好 60 分必须归入「60-80分」档（默认 right=True 会把它算成「60分以下」）。"""
        rows = [self.row(1, 20, 1, daily_score=60), self.row(2, 20, 1, daily_score=59.9)]
        stats = build_stats(rows, 90)
        buckets = {g['label']: g['n'] for g in stats['by_score']}
        self.assertEqual(buckets.get('60-80分'), 1)
        self.assertEqual(buckets.get('60分以下'), 1)

    def test_immature_recommendations_are_counted_not_silently_dropped(self):
        """只统计已满 20 交易日的推荐，但未成熟的条数必须显式报出。"""
        rows = [self.row(1, 20, 1), self.row(2, 5, 3), self.row(3, 10, 3)]
        stats = build_stats(rows, 90)
        self.assertEqual(stats['tracked_recs'], 1)
        self.assertEqual(stats['excluded_immature'], 2)


class NotificationWordingTests(unittest.TestCase):
    """卡片文案即统计口径：单位与期限口径必须写在卡上，否则数字会被读错。"""

    def _cards(self, fn):
        import notify.feishu as feishu
        cards: list[dict] = []
        original = feishu._send_feishu
        feishu._send_feishu = lambda card: (cards.append(card), True)[1]
        try:
            fn(feishu)
        finally:
            feishu._send_feishu = original
        return json.dumps(cards, ensure_ascii=False)

    def test_tracking_card_uses_observation_units_and_horizon_split(self):
        report = pd.DataFrame([
            dict(name='甲', code='600001', strategy='bottom_fishing', status='推荐后5交易日',
                 holding_trade_days=5, rec_price=10.0, current_price=11.0, return_pct=10.0),
            dict(name='甲', code='600001', strategy='bottom_fishing', status='推荐后20交易日',
                 holding_trade_days=20, rec_price=10.0, current_price=9.0, return_pct=-10.0),
        ])
        blob = self._cards(lambda feishu: feishu.notify_tracking_result(report))
        self.assertIn('追踪观测', blob)
        self.assertIn('2 条', blob)
        self.assertIn('混合口径', blob)
        # 标题也必须写明是混合口径，避免通知栏预览被当成同一期限的胜率
        self.assertIn('混合胜率', blob)
        # 同一期限才可比：必须给出按 5/20 交易日的分列
        self.assertIn('推荐后5交易日', blob)
        self.assertIn('推荐后20交易日', blob)
        # 一行 = 一次观测，不是一只股票 —— 不能再出现「追踪数量 N 只」
        self.assertNotIn('追踪数量', blob)
        self.assertNotIn('5只', blob)

    def test_attribution_card_states_maturity_scope_and_peak_semantics(self):
        stats = {
            'period': '近90天 · 推荐后20交易日', 'tracked_recs': 1, 'win_rate': 100.0,
            'avg_return': 5.0, 'avg_peak': 8.0, 'excluded_immature': 2,
            'by_week': [{'label': '优质低估低位 · 推荐后5交易日', 'n': 2, 'win_rate': 50.0,
                         'avg_return': 1.0, 'avg_peak': 10.0,
                         'peak_label': '最高收益', 'n_unit': '条观测'}],
        }
        blob = self._cards(lambda feishu: feishu.notify_attribution_report(stats))
        self.assertIn('仅含已满 20 交易日', blob)
        self.assertIn('未满 20 交易日未计入', blob)
        self.assertIn('最高收益', blob)
        self.assertIn('条观测', blob)
        self.assertIn('按策略与持有期限', blob)
        # 旧的「31 天窗口」脚注与实际口径（推荐后 20 交易日 / 90 天窗口）不符，必须消失
        self.assertNotIn('31 天窗口', blob)


if __name__ == '__main__':
    unittest.main()
