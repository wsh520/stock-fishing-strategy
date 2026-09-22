"""Default quality/value recommendation integration tests; no network or database."""
import copy
import unittest
from dataclasses import replace
from unittest.mock import patch

import numpy as np
import pandas as pd
from src import bottom_fishing_strategy as m
from src import volume_breakout_strategy as vb

DAY = "2025-06-30"


def prices(end=DAY):
    close = np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 50)]
    dates = pd.bdate_range(end=end, periods=len(close)).strftime("%Y-%m-%d")
    # peTTM=8 / pbMRQ=0.9：让「合格」样本的综合分稳过生产默认下限 MIN_QV_SCORE=60
    # （0.50×质量 + 0.35×估值 + 0.15×技术）。各估值/质量闸门测试会在末根覆盖这两个值，
    # 故基准取值不影响它们；这里只需保证「应当入选」的样本分数达标。
    return pd.DataFrame(dict(date=dates, open=close, high=close * 1.01,
                             low=close * .99, close=close, volume=1e7,
                             amount=close * 1e7, peTTM=8., pbMRQ=0.9))


def fundamentals():
    # 年度 ROE 18/20/22：质量分稳过下限；季度 roe=2 仍保留，用于验证「最新季度低 ROE
    # 不再否决连续盈利的年度质量」（见 test_low_long_term_but_high_short_term_is_eligible）。
    return {"debt_ratio": 40., "roe": 2., "forward_ni_yoy": 12., "forward_stat_date": "2025Q1", "annual_rows": [
        {"year": year, "report_date": f"{year}-12-31", "available_date": f"{year+1}-04-20",
         "roe": roe, "deducted_profit": 90., "net_profit": 100., "operating_cashflow": 110.}
        for year, roe in [(2022, 18), (2023, 20), (2024, 22)]]}


class QualityRecommendations(unittest.TestCase):
    def setUp(self):
        self.cfg = m.StrategyConfig(USE_CACHE=False)
        self.df = prices()

    def evaluate(self, df=None, fund=None, **kwargs):
        return m.evaluate(self.df if df is None else df, "600001", "工业企业", self.cfg,
                          {"regime": "bear"}, fundamentals() if fund is None else fund,
                          latest_trade_date=kwargs.get("latest_trade_date", DAY))

    def test_low_long_term_but_high_short_term_is_eligible(self):
        sig, reason = self.evaluate()
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "formal")
        self.assertLess(sig.position_250, .4)
        self.assertIn("质量分", m.describe(sig.to_dict()))
        # Latest quarterly ROE 2% no longer vetoes consistently profitable annual quality.
        self.assertEqual(sig.quality_status, "verified")

    def test_missing_each_valuation_is_pending(self):
        for col in ("peTTM", "pbMRQ"):
            for val in (None, np.nan, np.inf):
                df = self.df.copy()
                df.loc[df.index[-1], col] = val
                with self.subTest(col=col, val=val):
                    sig, reason = self.evaluate(df)
                    self.assertEqual(reason, "PASS")
                    self.assertEqual(sig.tier, "pending")

    def test_bad_valuation_cannot_be_rescued_by_technical_score(self):
        for col, val in (("peTTM", 26), ("peTTM", -1), ("pbMRQ", 3.1), ("pbMRQ", 0)):
            df = self.df.copy()
            df.loc[df.index[-1], col] = val
            self.assertEqual(self.evaluate(df)[1], "FAIL_VALUATION")

    def test_annual_missing_nonfinite_and_failure(self):
        for field in ("roe", "deducted_profit", "operating_cashflow", "net_profit"):
            fund = fundamentals()
            fund["annual_rows"][-1][field] = np.nan
            sig, reason = self.evaluate(fund=fund)
            self.assertEqual(reason, "PASS")
            self.assertEqual(sig.tier, "pending")
        fund = fundamentals()
        fund["annual_rows"][-1]["deducted_profit"] = -1
        self.assertEqual(self.evaluate(fund=fund)[1], "FAIL_FUND")
        self.assertEqual(self.evaluate(fund={})[0].tier, "pending")

    def test_stale_and_short_history(self):
        self.assertEqual(self.evaluate(latest_trade_date="2025-07-01")[1], "FAIL_STALE")
        self.assertEqual(self.evaluate(self.df.tail(249))[1], "FAIL_DATA")
        self.assertEqual(self.evaluate(latest_trade_date=None)[0].tier, "pending")
        df = self.df.copy()
        df.loc[df.index[-1], "volume"] = 0
        self.assertEqual(self.evaluate(df)[1], "FAIL_STALE")

    def test_high_long_term_position_rejected(self):
        df = self.df.copy()
        for col in ("open", "close"):
            df.loc[df.index[-1], col] = 19
        df.loc[df.index[-1], "high"] = 19.2
        df.loc[df.index[-1], "low"] = 18.8
        self.assertEqual(self.evaluate(df)[1], "FAIL_POSITION")

    def test_financials_require_special_review(self):
        sig, reason = m.evaluate(self.df, "600000", "测试银行", self.cfg,
                                 {}, fundamentals(), latest_trade_date=DAY)
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "pending")
        self.assertEqual(sig.quality_status, "financial_review")

    def test_both_strategies_share_eligibility_and_score(self):
        a, _ = self.evaluate()
        b, reason = vb.evaluate_breakout(self.df, "600001", "工业企业",
                                         vb.VolumeBreakoutConfig(USE_CACHE=False),
                                         {"regime": "bear"}, fundamentals(), latest_trade_date=DAY)
        self.assertEqual(reason, "PASS")
        self.assertEqual((b.tier, b.score, b.position_250), (a.tier, a.score, a.position_250))
        self.assertEqual(b.breakout_level, 0)

    def test_technical_flags_no_longer_hard_veto(self):
        with patch.object(m, "_kdj_ok", side_effect=AssertionError("old gate invoked")), \
             patch.object(m, "_macd_momentum_ok", side_effect=AssertionError("old gate invoked")):
            self.assertEqual(self.evaluate()[0].tier, "formal")

    def test_notice_shows_quality_and_trade_plan(self):
        from notify import feishu
        sig, _ = self.evaluate()
        with patch.object(feishu, "_send_feishu") as send:
            feishu.notify_screening_result(pd.DataFrame([sig.to_dict()]), strategy="volume_breakout")
        text = str(send.call_args.args[0])
        self.assertIn("优质低估低位", text)
        self.assertIn("质量分", text)
        # 措辞与内容双重锁定：卡片上的都是 formal 正式推荐（不是「候选/仅供参考」），
        # 且每只必须带可执行操作计划（建仓区间 / 止损 / 止盈 / 建议持有周期）。
        self.assertNotIn("候选", text)
        self.assertIn("正式推荐", text)
        self.assertIn("建仓", text)
        self.assertIn("止损", text)
        self.assertIn("止盈", text)
        self.assertIn("建议持有", text)

    def test_trade_plan_fields_and_band(self):
        plan = " ".join(m.describe_trade_plan({"close": 10.0, "stop_loss": 9.5,
                                               "take_profit": 11.0, "rr_ratio": 2.0}))
        self.assertIn("建仓 9.85 ~ 10.15", plan)          # 10.00 × (1 ± ENTRY_BAND_PCT=1.5%)
        self.assertIn("止损 9.50（-5.0%）", plan)
        self.assertIn("止盈 11.00（+10.0%）", plan)
        self.assertIn("建议持有 10~20 个交易日", plan)

    def test_trade_plan_omitted_without_close(self):
        # 旧数据/字段不全时整段不渲染，避免卡片出现 nan 或空洞的「操作计划」标题。
        for row in ({}, {"close": None}, {"close": 0}, {"close": float("nan")}):
            with self.subTest(row=row):
                self.assertEqual(m.describe_trade_plan(row), [])

    def test_main_default_does_not_invoke_market_or_weekly_gate(self):
        index = pd.DataFrame({"date": [DAY], "close": [100.]})
        for module, entry in ((m, m.main), (vb, vb.main_breakout)):
            cfg = self.cfg if module is m else vb.VolumeBreakoutConfig(USE_CACHE=False)
            with patch.object(module, "_bs_login", return_value=True), \
                 patch.object(module, "_bs_logout"), \
                 patch.object(module, "get_market_environment", return_value={"regime": "bear"}), \
                 patch.object(module, "get_index_daily", return_value=index), \
                 patch.object(module, "_screen_quality_pool", return_value=pd.DataFrame()) as screen:
                entry(cfg, m.CacheManager())
                self.assertEqual(screen.call_count, 1)

    def test_quality_pool_verifies_before_capping_and_ranking(self):
        cfg = replace(self.cfg, MAX_PICKS=1, MAX_WORKERS=1, FETCH_DELAY=0)
        stocks = [{"code": "600001", "name": "待核验"}, {"code": "600002", "name": "完整"}]
        pending = []
        def fund(code, *args):
            data = fundamentals()
            if code == "600001":
                data["annual_rows"] = []
            return data
        with patch.object(m, "get_stock_list", return_value=stocks), \
             patch.object(m, "get_daily_data", return_value=self.df), \
             patch.object(m, "get_fundamentals", side_effect=fund), \
             patch.object(m, "get_stock_industry", return_value={}):
            result = m._screen_quality_pool(cfg, m.CacheManager(), {"regime": "bear"}, DAY, pending)
        self.assertEqual(result.code.tolist(), ["600002"])
        self.assertEqual([r["code"] for r in pending], ["600001"])


if __name__ == "__main__":
    unittest.main()
