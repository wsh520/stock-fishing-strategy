"""Offline regression tests for liquidity, consistent valuation and growth evidence."""
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from src import bottom_fishing_strategy as m
from test_recommendation_upgrades import make_df, make_fund, ev, DAY


class MinimalQualityRepairs(unittest.TestCase):
    def test_liquidity_boundary(self):
        for amount, expected in ((1000, 'FAIL_LIQUIDITY'), (30_000_000, 'PASS')):
            df = make_df()
            df['amount'] = amount
            with self.subTest(amount=amount):
                self.assertEqual(ev(df, make_fund())[1], expected)

    def test_industry_cheap_is_scored_using_industry(self):
        # 行业口径：PE 分位 0.2 → 估值分 80；PB=4（高于绝对上限 3.0）自本轮起不再否决
        context = dict(mode='industry', pe_pct=.2, pb_pct=.2, peers=20)
        sig, reason = ev(make_df(pe=30, pb=4), make_fund(), val_context=context)
        self.assertEqual(reason, 'PASS')
        self.assertEqual(sig.tier, 'formal')
        self.assertEqual(sig.valuation_score, 80)

    def test_one_metric_can_fall_back(self):
        # 规则4：估值准入与评分只看 PE。PB 行业内样本不足 → pb_pct 不可得（仅影响展示与
        # 风险说明），既不否决、也不拉低估值分：估值分仍为 PE 行业分位 0.2 → 80。
        context = dict(mode='industry', pe_pct=.2, pb_pct=None, peers=20)
        sig, reason = ev(make_df(pe=30, pb=.9), make_fund(), val_context=context)
        self.assertEqual(reason, 'PASS')
        self.assertEqual(sig.valuation_score, 80)

    def test_growth_missing_old_future_or_invalid_period_is_pending(self):
        for period in (None, '2024Q3', '2025Q3', '2025Q9', 'garbage'):
            with self.subTest(period=period):
                sig, reason = ev(make_df(), make_fund(forward_stat_date=period))
                self.assertEqual(reason, 'PASS')
                self.assertEqual(sig.tier, 'pending')
                self.assertIn('forward', sig.missing_tags)

    def test_growth_escape_hatch_is_explicit(self):
        cfg = m.StrategyConfig(USE_CACHE=False, FORWARD_MISSING_AS_PENDING=False)
        sig, _ = ev(make_df(), make_fund(forward_ni_yoy=None), cfg)
        self.assertEqual(sig.tier, 'formal')
        self.assertIn('近期业绩待核验', sig.signals_hit)

    def test_financial_nonfinite_is_not_verified(self):
        for key in ('roe', 'debt_ratio'):
            for val in (np.nan, np.inf, 'bad'):
                fund = {'roe': 10, 'debt_ratio': 40, key: val}
                self.assertEqual(m._fund_verify_state(fund)[0], 'partial')

    def test_stale_quotes_excluded_from_industry_snapshot(self):
        cfg = m.StrategyConfig(USE_CACHE=False)
        stocks = [{'code': '600001'}, {'code': '600002'}]
        old = make_df(end='2025-06-27')
        with patch.object(m, 'get_stock_industry', return_value={'600001': 'I', '600002': 'I'}), \
             patch.object(m, 'get_index_daily', return_value=pd.DataFrame({'date': [DAY]})), \
             patch.object(m, 'get_daily_data', side_effect=lambda code, *args: make_df() if code == '600001' else old):
            snapshot = m.build_industry_valuation_snapshot(cfg, m.CacheManager(), stocks)
        self.assertEqual(len(snapshot['I']['pe']), 1)


if __name__ == '__main__':
    unittest.main()
