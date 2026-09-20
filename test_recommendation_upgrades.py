"""荐股逻辑四项升级的离线回归测试（无网络、无数据库、无 baostock/akshare）。

覆盖：
  #1 突破策略独立成第二信号源（technical 模式不再委派 quality_value 资格）
  #2 quality_value 接入市场级刹车（regime 数量收缩 + 急跌熔断）
  #3 综合分下限 MIN_QV_SCORE（仅对 formal 生效，pending 不受其累）
  #4a 行业相对估值闸门（行业内分位 + 绝对阈值回退）
  #4b 前瞻确认闸门（当年净利同比恶化否决 / 缺失行为可配）
"""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from src import bottom_fishing_strategy as m
from src import volume_breakout_strategy as vb

DAY = "2025-06-30"


def make_df(pe=8.0, pb=0.9, end=DAY):
    """250 日区间下沿（position≈0.1）的合格行情样本；末根 PE/PB 可指定。"""
    close = np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 50)]
    dates = pd.bdate_range(end=end, periods=len(close)).strftime("%Y-%m-%d")
    df = pd.DataFrame(dict(date=dates, open=close, high=close * 1.01, low=close * .99,
                           close=close, volume=1e7, amount=close * 1e7,
                           peTTM=pe, pbMRQ=pb))
    return df


def make_fund(roe=(18, 20, 22), **extra):
    fund = {"debt_ratio": 40., "roe": 2., "annual_rows": [
        {"year": y, "report_date": f"{y}-12-31", "available_date": f"{y+1}-04-20",
         "roe": r, "deducted_profit": 90., "net_profit": 100., "operating_cashflow": 110.}
        for y, r in zip((2022, 2023, 2024), roe)]}
    fund.update(extra)
    return fund


def ev(df, fund, cfg=None, val_context=None, **kw):
    cfg = cfg or m.StrategyConfig(USE_CACHE=False)
    return m.evaluate(df, "600001", "工业企业", cfg, {"regime": "bear"}, fund,
                      latest_trade_date=kw.get("latest_trade_date", DAY), val_context=val_context)


# ===========================================================================
# #3 综合分下限 MIN_QV_SCORE
# ===========================================================================
class TestScoreFloor(unittest.TestCase):
    def test_low_score_formal_rejected_at_default_floor(self):
        # ROE 11/12/13 + PE12/PB1.2 → 综合分≈52.7，无缺项（formal 候选）→ 默认下限 60 否决
        df, fund = make_df(pe=12., pb=1.2), make_fund(roe=(11, 12, 13))
        sig, reason = ev(df, fund, m.StrategyConfig(USE_CACHE=False))  # MIN_QV_SCORE=60 默认
        self.assertIsNone(sig)
        self.assertEqual(reason, "FAIL_QV_SCORE")

    def test_floor_disabled_passes(self):
        df, fund = make_df(pe=12., pb=1.2), make_fund(roe=(11, 12, 13))
        sig, reason = ev(df, fund, m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0))
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "formal")

    def test_high_score_passes_default_floor(self):
        sig, reason = ev(make_df(), make_fund())  # 综合分≈66
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "formal")
        self.assertGreaterEqual(sig.score, 60.0)

    def test_floor_not_applied_to_pending(self):
        # 缺 PE/PB → 估值分记 0、综合分被人为压低；但属 pending，下限不应把它直接否决，
        # 否则「数据缺失」与「质量不够」两种语义被混为一谈。
        df = make_df()
        df.loc[df.index[-1], "peTTM"] = np.nan
        sig, reason = ev(df, make_fund(), m.StrategyConfig(USE_CACHE=False))
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "pending")
        self.assertLess(sig.score, 60.0)


# ===========================================================================
# #4b 前瞻确认（当年成长未恶化）
# ===========================================================================
class TestForwardConfirmation(unittest.TestCase):
    def test_deteriorating_growth_vetoed(self):
        fund = make_fund(forward_ni_yoy=-55.0)  # < FORWARD_NI_YOY_MIN(-30)
        sig, reason = ev(make_df(), fund, m.StrategyConfig(USE_CACHE=False))
        self.assertIsNone(sig)
        self.assertEqual(reason, "FAIL_FORWARD")

    def test_healthy_growth_passes_and_is_tagged(self):
        fund = make_fund(forward_ni_yoy=12.0)
        sig, reason = ev(make_df(), fund, m.StrategyConfig(USE_CACHE=False))
        self.assertEqual(reason, "PASS")
        self.assertIn("当年净利+12%", sig.signals_hit)

    def test_missing_growth_does_not_demote_by_default(self):
        # FORWARD_MISSING_AS_PENDING=False（默认）：缺成长数据不否决、不降级
        sig, reason = ev(make_df(), make_fund(), m.StrategyConfig(USE_CACHE=False))
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "formal")

    def test_missing_growth_pending_when_configured(self):
        cfg = m.StrategyConfig(USE_CACHE=False, FORWARD_MISSING_AS_PENDING=True)
        sig, reason = ev(make_df(), make_fund(), cfg)
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "pending")
        self.assertIn("forward", sig.missing_tags)

    def test_gate_disabled_ignores_bad_growth(self):
        cfg = m.StrategyConfig(USE_CACHE=False, REQUIRE_FORWARD_CONFIRMATION=False)
        sig, reason = ev(make_df(), make_fund(forward_ni_yoy=-90.0), cfg)
        self.assertEqual(reason, "PASS")

    def test_boundary_equal_to_threshold_passes(self):
        # 恰好等于阈值（-30）不算「低于」，应放行
        sig, reason = ev(make_df(), make_fund(forward_ni_yoy=-30.0),
                         m.StrategyConfig(USE_CACHE=False))
        self.assertEqual(reason, "PASS")


# ===========================================================================
# #4a 行业相对估值
# ===========================================================================
class TestIndustryRelativeValuation(unittest.TestCase):
    def test_percentile_of(self):
        arr = np.array([5., 10., 15., 20., 25.])
        self.assertAlmostEqual(m._percentile_of(arr, 15.), 0.6)   # ≤15 的有 3/5
        self.assertAlmostEqual(m._percentile_of(arr, 4.), 0.0)
        self.assertAlmostEqual(m._percentile_of(arr, 30.), 1.0)
        self.assertIsNone(m._percentile_of(np.array([]), 10.))
        self.assertIsNone(m._percentile_of(None, 10.))

    def test_context_fallbacks(self):
        cfg = m.StrategyConfig(USE_CACHE=False)
        snap = {"银行": {"pe": np.sort(np.array([5., 6., 7., 8., 9., 10.])),
                         "pb": np.sort(np.array([.5, .6, .7, .8, .9, 1.0]))}}
        # 关闭 → None
        off = m.StrategyConfig(USE_CACHE=False, USE_INDUSTRY_RELATIVE_VALUATION=False)
        self.assertIsNone(m._industry_valuation_context(snap, "银行", 8., .8, off))
        # 无快照 / 无行业 / 行业不在快照 → None
        self.assertIsNone(m._industry_valuation_context({}, "银行", 8., .8, cfg))
        self.assertIsNone(m._industry_valuation_context(snap, "", 8., .8, cfg))
        self.assertIsNone(m._industry_valuation_context(snap, "地产", 8., .8, cfg))
        # 样本不足（< MIN_PEERS=5）→ None
        thin = {"银行": {"pe": np.array([5., 6., 7.]), "pb": np.array([.5, .6, .7])}}
        self.assertIsNone(m._industry_valuation_context(thin, "银行", 6., .6, cfg))
        # 正常 → 返回分位
        ctx = m._industry_valuation_context(snap, "银行", 8., .8, cfg)
        self.assertEqual(ctx["mode"], "industry")
        self.assertAlmostEqual(ctx["pe_pct"], 4 / 6)
        self.assertAlmostEqual(ctx["pb_pct"], 4 / 6)

    def test_industry_cheap_passes_despite_high_absolute_pe(self):
        # PE=40（> 绝对上限 25）但行业内分位 0.30（便宜）→ 行业模式放行
        ctx = {"mode": "industry", "pe_pct": 0.30, "pb_pct": 0.40, "peers": 12}
        df = make_df(pe=40., pb=2.0)
        sig, reason = ev(df, make_fund(), m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0),
                         val_context=ctx)
        self.assertEqual(reason, "PASS")

    def test_industry_expensive_rejected_despite_ok_absolute_pe(self):
        # PE=20（< 绝对上限 25）但行业内分位 0.90（贵）→ 行业模式否决
        ctx = {"mode": "industry", "pe_pct": 0.90, "pb_pct": 0.40, "peers": 12}
        df = make_df(pe=20., pb=1.5)
        sig, reason = ev(df, make_fund(), m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0),
                         val_context=ctx)
        self.assertIsNone(sig)
        self.assertEqual(reason, "FAIL_VALUATION")

    def test_absolute_fallback_without_context(self):
        # 无 val_context → 绝对阈值：PE=40 > 25 否决
        sig, reason = ev(make_df(pe=40., pb=2.0), make_fund(),
                         m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0))
        self.assertEqual(reason, "FAIL_VALUATION")

    def test_negative_pe_still_vetoed_in_industry_mode(self):
        ctx = {"mode": "industry", "pe_pct": 0.10, "pb_pct": 0.10, "peers": 12}
        sig, reason = ev(make_df(pe=-5., pb=1.0), make_fund(),
                         m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0), val_context=ctx)
        self.assertEqual(reason, "FAIL_VALUATION")

    def test_snapshot_build_and_empty_industry(self):
        cfg = m.StrategyConfig(USE_CACHE=False, MAX_WORKERS=2)
        stocks = [{"code": f"60000{i}", "name": f"银行{i}"} for i in range(1, 7)]
        industry = {f"60000{i}": "银行" for i in range(1, 7)}

        def daily(code, config, cache=None):
            i = int(str(code)[-1])
            return make_df(pe=float(5 + i), pb=float(0.4 + 0.1 * i))

        with patch.object(m, "get_stock_industry", return_value=industry), \
             patch.object(m, "get_daily_data", side_effect=daily):
            snap = m.build_industry_valuation_snapshot(cfg, m.CacheManager(), stocks)
        self.assertIn("银行", snap)
        self.assertEqual(len(snap["银行"]["pe"]), 6)
        self.assertTrue(np.all(np.diff(snap["银行"]["pe"]) >= 0))  # 升序

        # 行业数据不可用 → 空快照（上层回退绝对阈值）
        with patch.object(m, "get_stock_industry", return_value={}):
            self.assertEqual(m.build_industry_valuation_snapshot(cfg, m.CacheManager(), stocks), {})


# ===========================================================================
# #1 突破策略独立（technical 模式不委派 quality_value）
# ===========================================================================
class TestBreakoutIndependence(unittest.TestCase):
    def test_technical_mode_does_not_delegate_to_quality_value(self):
        cfg = vb.VolumeBreakoutConfig(USE_CACHE=False, RECOMMENDATION_MODE="technical")
        with patch.object(vb, "evaluate_quality_value",
                          side_effect=AssertionError("technical 模式不应委派 quality_value")):
            sig, reason = vb.evaluate_breakout(make_df(), "600001", "工业企业", cfg,
                                               {"regime": "bull"}, None, latest_trade_date=DAY)
        # 不抛异常即证明独立运行；合成行情未构造突破形态，reason 应为某个 FAIL_*（而非 PASS 委派）
        self.assertTrue(reason == "PASS" or reason.startswith("FAIL_"))

    def test_run_breakout_forces_technical_mode(self):
        # run_breakout.py 必须把突破入口设为 technical，否则与抄底产出完全相同（#1 的根因）
        import run_breakout
        with open(run_breakout.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('config.RECOMMENDATION_MODE = "technical"', src)


# ===========================================================================
# #2 quality_value 市场级刹车（regime 收缩 + 急跌熔断）
# ===========================================================================
class TestQualityValueMarketBrake(unittest.TestCase):
    def _index(self, closes):
        dates = pd.bdate_range(end=DAY, periods=len(closes)).strftime("%Y-%m-%d")
        return pd.DataFrame({"date": dates, "close": closes})

    def test_crash_halt_blocks_quality_value(self):
        cfg = m.StrategyConfig(USE_CACHE=False)
        crashing = self._index([100., 100., 100., 100., 100., 100., 90.])  # 近5日 -10%
        with patch.object(m, "_bs_login", return_value=True), patch.object(m, "_bs_logout"), \
             patch.object(m, "get_market_environment", return_value={"regime": "neutral"}), \
             patch.object(m, "get_index_daily", return_value=crashing), \
             patch.object(m, "_screen_quality_pool", return_value=pd.DataFrame()) as screen:
            out = m.main(cfg, m.CacheManager())
        self.assertIsNone(out)
        self.assertEqual(screen.call_count, 0)  # 熔断在筛选之前

    def test_bear_regime_contracts_max_picks(self):
        cfg = m.StrategyConfig(USE_CACHE=False)
        flat = self._index([100.] * 7)  # 不触发熔断
        with patch.object(m, "_bs_login", return_value=True), patch.object(m, "_bs_logout"), \
             patch.object(m, "get_market_environment", return_value={"regime": "bear"}), \
             patch.object(m, "get_index_daily", return_value=flat), \
             patch.object(m, "_screen_quality_pool", return_value=pd.DataFrame()) as screen:
            m.main(cfg, m.CacheManager())
        self.assertEqual(screen.call_count, 1)
        self.assertEqual(screen.call_args.kwargs.get("max_picks"), cfg.BEAR_MAX_PICKS)

    def test_bull_regime_uses_full_max_picks(self):
        cfg = m.StrategyConfig(USE_CACHE=False)
        flat = self._index([100.] * 7)
        with patch.object(m, "_bs_login", return_value=True), patch.object(m, "_bs_logout"), \
             patch.object(m, "get_market_environment", return_value={"regime": "bull"}), \
             patch.object(m, "get_index_daily", return_value=flat), \
             patch.object(m, "_screen_quality_pool", return_value=pd.DataFrame()) as screen:
            m.main(cfg, m.CacheManager())
        self.assertEqual(screen.call_args.kwargs.get("max_picks"), cfg.MAX_PICKS)

    def test_screen_pool_honors_max_picks_cap(self):
        # _screen_quality_pool 应按传入 max_picks 截取，而非恒用 config.MAX_PICKS
        cfg = m.StrategyConfig(USE_CACHE=False, MAX_WORKERS=1, FETCH_DELAY=0, MAX_PICKS=5)
        stocks = [{"code": f"60000{i}", "name": f"工业{i}"} for i in range(1, 6)]
        with patch.object(m, "get_stock_list", return_value=stocks), \
             patch.object(m, "get_daily_data", return_value=make_df()), \
             patch.object(m, "get_fundamentals", return_value=make_fund()), \
             patch.object(m, "enrich_forward_growth", side_effect=lambda c, f, cfg, cache=None: f), \
             patch.object(m, "get_stock_industry", return_value={}):
            out = m._screen_quality_pool(cfg, m.CacheManager(), {"regime": "bear"}, DAY,
                                         max_picks=2)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
