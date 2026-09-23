"""优质低估低位（quality_value）本轮口径加固的离线回归测试（无网络、无数据库）。

覆盖六项调整，并为每项锁定边界行为（不是盈利能力验证）：

  规则1 止跌确认闸门扩展到**全市场环境**（牛/中/熊/unknown 一致），且数据不足不当作已确认；
        保留显式关闭开关，旧配置名 QV_BEAR_TIMING_GATE 双向兼容（构造传入 + 构造后赋值）。
  规则2 最近报告期净利同比硬否决线 -30% → **-10%**，边界「恰好 -10%」不触发；
        缺失/过旧报告期仍为待核验；黄标区间同步改为 [-10%, 0%)；不误用于 technical 放量突破。
  规则3 PE 保持主要估值准入（行业分位优先、绝对上限回退）；PB 不再因偏高单独否决，
        非正 PB 仍异常、缺失只标注缺项而不自动降级。
  规则4 估值评分以实际准入的 PE 口径为主，行业 60% 分位与绝对 PE=25 锚定同一分值（40）；
        PB 不参与估值评分；综合分下限 60 的资格判定不受本轮改动影响。
  规则5 近 60 个交易日相对沪深300强度：起止日期对齐、缺失中性、只做小幅排序微调。
  规则6 quality_value 路径接入 has_halt_gap（沿用 REQUIRE_NO_HALT_GAP / MAX_BAR_GAP_DAYS）。
  对照：放量突破 technical 模式的资格/评分/否决口未经本轮改动影响。

⚠️ 本文件只证明「逻辑按要求实现」，不证明荐股胜率提升。
"""
import unittest
from dataclasses import replace
from unittest.mock import patch

import numpy as np
import pandas as pd

from src import bottom_fishing_strategy as m
from src import volume_breakout_strategy as vb

DAY = "2025-06-30"
DAY_DT = pd.Timestamp(DAY)

# ===== 行情样本 =====
# 长期下跌后稳步回升：末根收盘站上 MA20 → 止跌闸门由「站上 MA20」满足
HEALTHY = np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 50)]
# 冲高后深幅回落：末根收盘在 MA20 下方且 MACD 柱仍在走弱 → 止跌未确认
WEAK = np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 35), np.linspace(11, 9.8, 15)[1:]]
# 下跌末端斜率趋缓（绿柱缩短）：末根收盘仍在 MA20 下方，但 MACD 柱连续改善 → 止跌由 MACD 满足
BELOW_MA20_MACD_UP = np.r_[np.linspace(20, 10, 250), 10 * np.exp(-0.02 * np.arange(1, 21))]


def mk_df(close, pe=8.0, pb=0.9, end=DAY):
    """合成日线：open 取前收，high/low 包住 open/close，并带 pct_chg。

    pct_chg 与自洽的 OHLC 是 technical 路径必需字段，这里一并给出，
    使同一份样本可在 quality_value 与 technical 两条路径下复用而不因缺列失真。
    """
    close = np.asarray(close, float)
    dates = pd.bdate_range(end=end, periods=len(close)).strftime("%Y-%m-%d")
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame(dict(
        date=dates, open=open_,
        high=np.maximum(open_, close) * 1.01, low=np.minimum(open_, close) * .99,
        close=close, volume=1e7, amount=close * 1e7,
        pct_chg=(pd.Series(close).pct_change().fillna(0) * 100).values,
        peTTM=pe, pbMRQ=pb))


def mk_fund(roe=(18, 20, 22), **extra):
    fund = {"debt_ratio": 40., "roe": 2., "forward_ni_yoy": 12., "forward_stat_date": "2025Q1",
            "annual_rows": [
                {"year": y, "report_date": f"{y}-12-31", "available_date": f"{y+1}-04-20",
                 "roe": r, "deducted_profit": 90., "net_profit": 100., "operating_cashflow": 110.}
                for y, r in zip((2022, 2023, 2024), roe)]}
    fund.update(extra)
    return fund


def ev(df=None, fund=None, cfg=None, regime="bull", val_context=None, index_df=None, **kw):
    cfg = cfg or m.StrategyConfig(USE_CACHE=False)
    return m.evaluate(mk_df(HEALTHY) if df is None else df, "600001", "工业企业", cfg,
                      {"regime": regime}, mk_fund() if fund is None else fund,
                      latest_trade_date=kw.get("latest_trade_date", DAY),
                      val_context=val_context, index_df=index_df)


def dates_with_gap(n, gap_at=40, gap_days=40, end=DAY):
    """末根固定为 end、在第 gap_at 根处插入 gap_days 自然日缺口的日期序列。"""
    cur = pd.Timestamp(end)
    back = []
    for i in range(n):
        back.append(cur)
        step = 1
        if i == gap_at:
            step += gap_days
        cur = cur - pd.Timedelta(days=step)
    return [d.strftime("%Y-%m-%d") for d in reversed(back)]


def idx_df(closes, end=DAY):
    closes = np.asarray(closes, float)
    dates = pd.bdate_range(end=end, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "close": closes})


# ===========================================================================
# 规则1：止跌确认闸门（全市场环境）
# ===========================================================================
class TestStabilizationGate(unittest.TestCase):
    def test_default_on_and_all_regimes_equivalent(self):
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
        self.assertTrue(m.StrategyConfig().QV_STABILIZATION_GATE)
        # 未止跌形态在牛/中/熊/未知四种环境下口径一致
        for regime in ("bull", "neutral", "bear", "unknown"):
            with self.subTest(regime=regime):
                sig, reason = ev(mk_df(WEAK), cfg=cfg, regime=regime)
                self.assertIsNone(sig)
                self.assertEqual(reason, "FAIL_STABILIZATION")

    def test_ma20_above_satisfies_gate(self):
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
        sig, reason = ev(cfg=cfg)
        self.assertEqual(reason, "PASS")
        timing = m.assess_entry_timing(
            m.compute_daily_signals(m._recompute_pct_chg(mk_df(HEALTHY).copy()), cfg), cfg)
        self.assertIs(timing["below_ma20"], False)   # 确由「站上 MA20」满足

    def test_macd_improvement_satisfies_gate_without_ma20_or_ma60(self):
        # 不额外要求 MA60 站稳 / RSI 反弹 / KDJ 金叉：仅 MACD 柱连续改善即可确认止跌
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
        df = mk_df(BELOW_MA20_MACD_UP)
        tech = m.compute_daily_signals(m._recompute_pct_chg(df.copy()), cfg)
        timing = m.assess_entry_timing(tech, cfg)
        self.assertIs(timing["below_ma20"], True)    # 收盘仍在 MA20 下方
        self.assertIs(timing["below_ma60"], True)    # 且仍在 MA60 下方
        self.assertTrue(m._macd_momentum_ok(tech, cfg))
        self.assertFalse(cfg.QV_ENFORCE_KDJ_MACD_VETO)  # KDJ/MACD 旧否决仍默认关闭
        sig, reason = ev(df, cfg=cfg)
        self.assertEqual(reason, "PASS")

    def test_insufficient_indicator_data_is_not_confirmation(self):
        # MA20 不可得（below_ma20=None）+ MACD 柱含 NaN → 「无法确认止跌」不得当作已确认
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
        df = mk_df(BELOW_MA20_MACD_UP)
        tech = m.compute_daily_signals(m._recompute_pct_chg(df.copy()), cfg).copy()
        blank_timing = {"side": "unknown", "label": "时机未知", "below_ma20": None,
                        "below_ma60": None, "macd_weak": False, "days_since_low": None,
                        "pct_from_low": None, "ma20": None, "ma60": None, "recent_low": None}
        # (a) MACD 数据充分且改善 → 放行（证明放行来自「有数据确认」）
        with patch.object(m, "compute_daily_signals", return_value=tech), \
             patch.object(m, "assess_entry_timing", return_value=blank_timing):
            self.assertEqual(ev(df, cfg=cfg)[1], "PASS")
        # (b) 同样形态但 MACD 柱尾部为 NaN → 数据不足，不视为已确认
        nan_tech = tech.copy()
        nan_tech.loc[nan_tech.index[-(cfg.MACD_MOMENTUM_DAYS + 1):], "macd_histogram"] = np.nan
        with patch.object(m, "compute_daily_signals", return_value=nan_tech), \
             patch.object(m, "assess_entry_timing", return_value=blank_timing):
            sig, reason = ev(df, cfg=cfg)
        self.assertIsNone(sig)
        self.assertEqual(reason, "FAIL_STABILIZATION")

    def test_gate_can_be_switched_off_explicitly(self):
        off = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0, QV_STABILIZATION_GATE=False)
        self.assertEqual(ev(mk_df(WEAK), cfg=off)[1], "PASS")

    def test_legacy_config_name_migrates_both_ways(self):
        # 构造时传入旧名
        self.assertFalse(m.StrategyConfig(QV_BEAR_TIMING_GATE=False).QV_STABILIZATION_GATE)
        self.assertTrue(m.StrategyConfig(QV_BEAR_TIMING_GATE=True).QV_STABILIZATION_GATE)
        # 构造后赋值旧名（写穿），否则旧脚本会静默失去作用、闸门意外保持开启
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
        cfg.QV_BEAR_TIMING_GATE = False
        self.assertFalse(cfg.QV_STABILIZATION_GATE)
        self.assertEqual(ev(mk_df(WEAK), cfg=cfg)[1], "PASS")
        # 默认（未指定旧名）不被旧字段空值抹掉
        self.assertTrue(m.StrategyConfig(QV_BEAR_TIMING_GATE=None).QV_STABILIZATION_GATE)
        # dataclasses.replace / asdict 往返同样保持语义
        self.assertFalse(replace(m.StrategyConfig(), QV_BEAR_TIMING_GATE=False).QV_STABILIZATION_GATE)
        from dataclasses import asdict
        self.assertIsNone(asdict(m.StrategyConfig())["QV_BEAR_TIMING_GATE"])

    def test_breakout_config_inherits_and_defaults_on(self):
        vbcfg = vb.VolumeBreakoutConfig(RECOMMENDATION_MODE="technical")
        self.assertTrue(vbcfg.QV_STABILIZATION_GATE)

    def test_veto_reason_is_registered_in_funnel_attribution(self):
        self.assertIn("FAIL_STABILIZATION", m._QV_REASON_CODES)
        self.assertNotIn("FAIL_BEAR_TIMING", m._QV_REASON_CODES)
        self.assertIn("FAIL_HALT_GAP", m._QV_REASON_CODES)


# ===========================================================================
# 规则2：最近报告期净利同比硬否决线 -10%
# ===========================================================================
class TestForwardThreshold(unittest.TestCase):
    def setUp(self):
        self.cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)

    def test_boundary_exactly_minus_ten_passes(self):
        sig, reason = ev(fund=mk_fund(forward_ni_yoy=-10.0), cfg=self.cfg)
        self.assertEqual(reason, "PASS")
        # 同时落在黄标区间 [-10%, 0%) → 显式预警但不否决
        self.assertIn("业绩下滑预警", sig.signals_hit)

    def test_just_below_minus_ten_is_vetoed(self):
        for yoy in (-10.01, -11.0, -30.0, -55.0):
            with self.subTest(yoy=yoy):
                sig, reason = ev(fund=mk_fund(forward_ni_yoy=yoy), cfg=self.cfg)
                self.assertIsNone(sig)
                self.assertEqual(reason, "FAIL_FORWARD")

    def test_warning_band_covers_negative_growth_only(self):
        # [FORWARD_NI_YOY_MIN, FORWARD_NI_YOY_WARN) = [-10%, 0%) 打黄标，不否决
        for yoy, tagged in ((-9.9, True), (-1.0, True), (0.0, False), (5.0, False)):
            with self.subTest(yoy=yoy):
                sig, reason = ev(fund=mk_fund(forward_ni_yoy=yoy), cfg=self.cfg)
                self.assertEqual(reason, "PASS")
                self.assertEqual("业绩下滑预警" in sig.signals_hit, tagged)
        # 黄标区间与硬否决线不再自相矛盾
        self.assertLess(self.cfg.FORWARD_NI_YOY_MIN, self.cfg.FORWARD_NI_YOY_WARN)
        self.assertEqual(self.cfg.FORWARD_NI_YOY_MIN, -10.0)

    def test_missing_growth_still_pending_and_not_faked(self):
        sig, reason = ev(fund=mk_fund(forward_ni_yoy=None), cfg=self.cfg)
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "pending")
        self.assertIn("forward", sig.missing_tags)

    def test_stale_report_period_still_pending(self):
        for period in ("2024Q3", "2024Q4"):
            with self.subTest(period=period):
                fund = mk_fund(forward_stat_date=period, forward_ni_yoy=12.0)
                sig, reason = ev(fund=fund, cfg=self.cfg)
                self.assertEqual(reason, "PASS")
                self.assertEqual(sig.tier, "pending")
                self.assertIn("forward", sig.missing_tags)

    def test_growth_gate_not_applied_to_technical_breakout(self):
        # -90% 在 quality_value 是 FAIL_FORWARD；technical 放量突破路径不应因此改口径
        vbcfg = vb.VolumeBreakoutConfig(USE_CACHE=False, RECOMMENDATION_MODE="technical")
        sig, reason = vb.evaluate_breakout(mk_df(HEALTHY), "600001", "工业企业", vbcfg,
                                           {"regime": "bull"}, mk_fund(forward_ni_yoy=-90.0),
                                           latest_trade_date=DAY)
        self.assertNotEqual(reason, "FAIL_FORWARD")


# ===========================================================================
# 规则3：PE 准入、PB 降为异常识别/风险说明
# ===========================================================================
class TestPePbResponsibilities(unittest.TestCase):
    def setUp(self):
        self.cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)

    def test_pe_over_absolute_cap_still_vetoed(self):
        self.assertEqual(ev(mk_df(HEALTHY, pe=25.01), cfg=self.cfg)[1], "FAIL_VALUATION")
        self.assertEqual(ev(mk_df(HEALTHY, pe=25.0), cfg=self.cfg)[1], "PASS")   # 恰好等于上限放行

    def test_non_positive_pe_still_vetoed(self):
        for pe in (0.0, -1.0):
            with self.subTest(pe=pe):
                self.assertEqual(ev(mk_df(HEALTHY, pe=pe), cfg=self.cfg)[1], "FAIL_VALUATION")

    def test_pe_missing_is_pending(self):
        for val in (None, np.nan, np.inf):
            with self.subTest(val=val):
                df = mk_df(HEALTHY)
                df.loc[df.index[-1], "peTTM"] = val
                sig, reason = ev(df, cfg=self.cfg)
                self.assertEqual(reason, "PASS")
                self.assertEqual(sig.tier, "pending")
                self.assertIn("valuation_pe", sig.missing_tags)

    def test_high_pb_no_longer_vetoes_absolute_mode(self):
        for pb in (3.1, 5.0, 12.0):
            with self.subTest(pb=pb):
                sig, reason = ev(mk_df(HEALTHY, pb=pb), cfg=self.cfg)
                self.assertEqual(reason, "PASS")
                self.assertIn("PB偏高", sig.signals_hit)

    def test_high_pb_no_longer_vetoes_industry_mode(self):
        ctx = {"mode": "industry", "pe_pct": 0.20, "pb_pct": 0.95, "peers": 12}
        sig, reason = ev(mk_df(HEALTHY, pe=12.0, pb=9.0), cfg=self.cfg, val_context=ctx)
        self.assertEqual(reason, "PASS")
        self.assertIn("PB偏高", sig.signals_hit)

    def test_non_positive_pb_still_abnormal(self):
        for pb in (0.0, -1.0):
            with self.subTest(pb=pb):
                self.assertEqual(ev(mk_df(HEALTHY, pb=pb), cfg=self.cfg)[1], "FAIL_VALUATION")

    def test_missing_pb_is_flagged_but_not_demoted(self):
        for val in (None, np.nan, np.inf):
            with self.subTest(val=val):
                df = mk_df(HEALTHY)
                df.loc[df.index[-1], "pbMRQ"] = val
                sig, reason = ev(df, cfg=self.cfg)
                self.assertEqual(reason, "PASS")
                self.assertEqual(sig.tier, "formal")          # 核心证据仍齐 → 不降级
                self.assertIn("valuation_pb", sig.missing_tags)  # 但不伪装成已核验
                self.assertIsNone(sig.pb_mrq)

    def test_pe_still_primary_when_pb_high(self):
        # PB 高不再赦免 PE：PE 越限仍否决（不能把本轮改动读成「所有高 PB 无条件通过」）
        ctx = {"mode": "industry", "pe_pct": 0.95, "pb_pct": 0.10, "peers": 12}
        self.assertEqual(ev(mk_df(HEALTHY, pe=12.0, pb=0.8), cfg=self.cfg,
                            val_context=ctx)[1], "FAIL_VALUATION")

    def test_technical_mode_pb_veto_unchanged(self):
        # technical 路径的 PB 绝对上限否决未被本轮改动影响
        tcfg = m.StrategyConfig(USE_CACHE=False, RECOMMENDATION_MODE="technical")
        tech_fund = {"roe": 12.0, "debt_ratio": 45.0,
                     "goodwill_ratio": 3.0, "deducted_profit_ratio": 0.9}
        sig, reason = m.evaluate(mk_df(HEALTHY, pb=10.0), "600001", "工业企业", tcfg,
                                 {"regime": "bull"}, tech_fund, latest_trade_date=DAY)
        self.assertEqual(reason, "FAIL_VALUATION")

    def test_industry_fallback_is_labeled_by_pe_availability(self):
        # PE 是唯一估值准入口径 → 「行业口径生效」以 pe_pct 可得为准：
        # 行业 PE 样本不足（仅 PB 分位可得）时实际回退绝对 PE 上限，必须标为 absolute。
        cfg = m.StrategyConfig()
        thin_pe = {"工业": {"pe": np.array([8.0, 10.0, 12.0]),                    # < MIN_PEERS(5)
                            "pb": np.array([0.8, 1.0, 1.2, 1.4, 1.6, 1.8])}}
        ctx = m._industry_valuation_context(thin_pe, "工业", 12.0, 1.2, cfg)
        self.assertIsNotNone(ctx)
        self.assertIsNone(ctx["pe_pct"])
        frame = pd.DataFrame([{"code": "600000", "pe_ttm": 12.0, "pb_mrq": 1.2}])
        tagged = m._tag_valuation_mode(frame, thin_pe, {"600000": "工业"}, cfg)
        self.assertEqual(tagged["valuation_mode"].tolist(), ["absolute"])
        # 对照：PE 样本充足 → 行业口径
        full = {"工业": {"pe": np.array([8.0, 10.0, 12.0, 14.0, 16.0, 18.0]),
                         "pb": np.array([0.8, 1.0, 1.2, 1.4, 1.6, 1.8])}}
        tagged2 = m._tag_valuation_mode(frame, full, {"600000": "工业"}, cfg)
        self.assertEqual(tagged2["valuation_mode"].tolist(), ["industry"])


# ===========================================================================
# 规则4：估值评分以 PE 为准，两口径压线锚点一致
# ===========================================================================
class TestValuationScoring(unittest.TestCase):
    def setUp(self):
        self.cfg = m.StrategyConfig(USE_CACHE=False)

    def test_anchor_alignment_at_admission_caps(self):
        cap_pct = self.cfg.VALUATION_INDUSTRY_PERCENTILE_MAX      # 0.60
        cap_pe = self.cfg.MAX_PE_TTM                              # 25
        self.assertAlmostEqual(m._valuation_pe_score(None, cap_pct, self.cfg), 40.0, places=6)
        self.assertAlmostEqual(m._valuation_pe_score(cap_pe, None, self.cfg), 40.0, places=6)
        self.assertAlmostEqual(m._valuation_pe_score(40.0, cap_pct, self.cfg),
                               m._valuation_pe_score(cap_pe, None, self.cfg), places=6)

    def test_cheaper_scores_higher_and_bounded(self):
        pcts = [0.60, 0.50, 0.30, 0.10, 0.0]
        scores = [m._valuation_pe_score(None, p, self.cfg) for p in pcts]
        self.assertEqual(scores, sorted(scores))                  # 分位越低（越便宜）分越高
        self.assertAlmostEqual(scores[-1], 100.0)
        pes = [25.0, 20.0, 12.0, 5.0, 1.0, 0.01]
        pscores = [m._valuation_pe_score(p, None, self.cfg) for p in pes]
        self.assertEqual(pscores, sorted(pscores))                # PE 越低分越高
        self.assertAlmostEqual(pscores[0], 40.0)
        for s in scores + pscores:
            self.assertGreaterEqual(s, 0.0)
            self.assertLessEqual(s, 100.0)

    def test_missing_pe_scores_zero(self):
        self.assertEqual(m._valuation_pe_score(None, None, self.cfg), 0.0)

    def test_pb_absent_from_valuation_score(self):
        # 同一 PE 下 PB 从 0.9 抬到 30，估值分不得变化（否则等于变相重建高 PB 硬否决）
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
        a, _ = ev(mk_df(HEALTHY, pe=12.0, pb=0.9), cfg=cfg)
        b, _ = ev(mk_df(HEALTHY, pe=12.0, pb=30.0), cfg=cfg)
        self.assertAlmostEqual(a.valuation_score, b.valuation_score, places=6)
        self.assertNotIn("valuation_pb", a.missing_tags)

    def test_industry_and_absolute_capped_candidates_score_identically(self):
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
        ctx = {"mode": "industry", "pe_pct": cfg.VALUATION_INDUSTRY_PERCENTILE_MAX,
               "pb_pct": 0.30, "peers": 12}
        ind, r1 = ev(mk_df(HEALTHY, pe=12.0, pb=0.9), cfg=cfg, val_context=ctx)
        abs_, r2 = ev(mk_df(HEALTHY, pe=cfg.MAX_PE_TTM, pb=0.9), cfg=cfg)
        self.assertEqual((r1, r2), ("PASS", "PASS"))
        self.assertAlmostEqual(ind.valuation_score, abs_.valuation_score, places=6)
        self.assertAlmostEqual(ind.valuation_score, 40.0, places=6)

    def test_floor_equivalence_reports_aligned_anchor(self):
        e = m.qv_floor_equivalence(self.cfg)
        self.assertAlmostEqual(e["valuation_industry_at_cap"], e["valuation_absolute_at_cap"], places=6)
        self.assertAlmostEqual(e["valuation_absolute_at_cap"], 40.0, places=6)
        self.assertLess(e["ceiling_industry"], self.cfg.MIN_QV_SCORE)
        text = m.describe_qv_floor(self.cfg)
        self.assertIn("锚定", text)
        self.assertNotIn("绝对口径 0", text)

    def test_score_weights_and_floor_unchanged(self):
        self.assertEqual((self.cfg.QUALITY_SCORE_WEIGHT, self.cfg.VALUATION_SCORE_WEIGHT,
                          self.cfg.TECHNICAL_SCORE_WEIGHT), (0.50, 0.35, 0.15))
        self.assertEqual(self.cfg.MIN_QV_SCORE, 60.0)
        # 综合分低于下限的 formal 候选仍被否决（本轮不放松资格判定）
        cfg = m.StrategyConfig(USE_CACHE=False)     # 默认下限 60
        df, fund = mk_df(HEALTHY, pe=12.0, pb=1.2), mk_fund(roe=(11, 12, 13))
        sig, reason = ev(df, fund=fund, cfg=cfg)
        self.assertIsNone(sig)
        self.assertEqual(reason, "FAIL_QV_SCORE")


# ===========================================================================
# 规则5：近 60 日相对沪深300强度
# ===========================================================================
class TestRelativeStrength(unittest.TestCase):
    def setUp(self):
        self.cfg = m.StrategyConfig(USE_CACHE=False)

    def test_aligned_returns_excess_return_in_points(self):
        # 起止日期完全对齐的简单构造：个股 +20%、指数 +5% → +15.0 个百分点
        n = self.cfg.RS_LOOKBACK + 1
        stock = mk_df(np.linspace(10.0, 12.0, n))
        index = idx_df(np.linspace(100.0, 105.0, n))
        rs = m.compute_relative_strength(stock, index, self.cfg)
        self.assertAlmostEqual(rs, 15.0, places=6)

    def test_uses_only_common_dates(self):
        # 指数含个股没有的日期（错位交易日）：必须按共有日期对齐后再比较，而非各取各的端点
        n = self.cfg.RS_LOOKBACK + 1
        stock = mk_df(np.linspace(10.0, 12.0, n))
        index = idx_df(np.linspace(100.0, 105.0, n))
        # 指数在最前面多出 10 个更早的交易日（不在个股序列里）
        extra_dates = pd.bdate_range(end=index["date"].iloc[0], periods=11).strftime("%Y-%m-%d")[:-1]
        index = pd.concat([pd.DataFrame({"date": extra_dates,
                                         "close": np.linspace(80.0, 99.0, len(extra_dates))}),
                           index], ignore_index=True)
        self.assertAlmostEqual(m.compute_relative_strength(stock, index, self.cfg), 15.0, places=6)

    def test_missing_or_insufficient_is_none(self):
        stock = mk_df(HEALTHY)
        self.assertIsNone(m.compute_relative_strength(stock, None, self.cfg))
        self.assertIsNone(m.compute_relative_strength(stock, pd.DataFrame(), self.cfg))
        self.assertIsNone(m.compute_relative_strength(stock, pd.DataFrame({"close": [1.0] * 80}), self.cfg))
        self.assertIsNone(m.compute_relative_strength(stock, pd.DataFrame({"date": ["2025-01-01"] * 80}),
                                                      self.cfg))
        # 共有日期不足以覆盖回看窗口 → 不可计算
        short = idx_df(np.linspace(100.0, 105.0, 30))
        self.assertIsNone(m.compute_relative_strength(stock, short, self.cfg))

    def test_missing_index_leaves_rank_score_neutral(self):
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
        with_none, _ = ev(cfg=cfg)                       # 无 index_df
        self.assertIsNone(with_none.relative_strength)
        self.assertAlmostEqual(with_none.rank_score, with_none.score, places=6)
        self.assertIn("相对强度未核验", with_none.signals_hit)

    def test_only_adjusts_sorting_within_bounded_weight(self):
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
        n = cfg.RS_LOOKBACK + 1
        # 指数大跌 → 个股相对强势（正贡献）；指数大涨 → 个股相对弱势（负贡献）
        strong, _ = ev(cfg=cfg, index_df=idx_df(np.linspace(100.0, 70.0, n)))
        weak, _ = ev(cfg=cfg, index_df=idx_df(np.linspace(100.0, 200.0, n)))
        for sig in (strong, weak):
            self.assertEqual(sig.tier, "formal")         # 不影响资格/tier
        self.assertAlmostEqual(strong.score, weak.score, places=6)   # 综合分不变
        self.assertGreater(strong.rank_score, weak.rank_score)
        self.assertLessEqual(abs(strong.rank_score - strong.score), cfg.RS_WEIGHT + 1e-9)
        self.assertLessEqual(abs(weak.rank_score - weak.score), cfg.RS_WEIGHT + 1e-9)
        self.assertIn("近60日", weak.signals_hit)        # 跑输时给出风险提示标签
        self.assertIn("风险提示", weak.signals_hit)

    def test_relative_strength_cannot_bypass_score_floor(self):
        # 门槛判定用 score（不含相对强度）：明显强势也不能把 52 分的候选救过 60 分下限
        cfg = m.StrategyConfig(USE_CACHE=False)          # 默认下限 60
        n = cfg.RS_LOOKBACK + 1
        sig, reason = ev(mk_df(HEALTHY, pe=12.0, pb=1.2), fund=mk_fund(roe=(11, 12, 13)),
                         cfg=cfg, index_df=idx_df(np.linspace(100.0, 10.0, n)))
        self.assertIsNone(sig)
        self.assertEqual(reason, "FAIL_QV_SCORE")

    def test_rank_signals_orders_by_adjusted_rank_score(self):
        rows = pd.DataFrame([
            {"code": "600001", "tier": "formal", "rank_score": 62.0, "quality_score": 70.0,
             "valuation_score": 40.0, "weekly_status": "not_required"},
            {"code": "600002", "tier": "formal", "rank_score": 63.0, "quality_score": 68.0,
             "valuation_score": 42.0, "weekly_status": "not_required"},
        ])
        self.assertEqual(m._rank_signals(rows).iloc[0]["code"], "600002")
        rows.loc[0, "rank_score"] = 64.0
        self.assertEqual(m._rank_signals(rows).iloc[0]["code"], "600001")

    def test_index_data_is_wired_through_production_entry(self):
        cfg = m.StrategyConfig(USE_CACHE=False)
        index = idx_df(np.linspace(100.0, 101.0, 70))
        with patch.object(m, "_bs_login", return_value=True), patch.object(m, "_bs_logout"), \
             patch.object(m, "get_market_environment", return_value={"regime": "neutral"}), \
             patch.object(m, "get_index_daily", return_value=index), \
             patch.object(m, "_screen_quality_pool", return_value=pd.DataFrame()) as screen:
            m.main(cfg, m.CacheManager())
        self.assertIs(screen.call_args.kwargs.get("index_df"), index)

    def test_screen_pool_forwards_index_to_evaluator(self):
        # 两阶段流程（先价格预筛、后补财务重估）都必须拿到同一份指数数据，
        # 否则相对强度在正式评估阶段会永远不可计算。
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0, MAX_WORKERS=1, FETCH_DELAY=0)
        index = idx_df(np.linspace(100.0, 105.0, 70))
        seen = []

        def fake_eval(daily, code, name, conf, env, fund, **kw):
            seen.append((fund is None, kw.get("index_df")))
            return ev(mk_df(HEALTHY), cfg=conf)[0], "PASS"

        with patch.object(m, "get_stock_list", return_value=[{"code": "600001", "name": "工业"}]), \
             patch.object(m, "get_daily_data", return_value=mk_df(HEALTHY)), \
             patch.object(m, "get_fundamentals", return_value=mk_fund()), \
             patch.object(m, "get_stock_industry", return_value={}), \
             patch.object(m, "enrich_annual_fundamentals", side_effect=lambda *a, **kw: a[1]), \
             patch.object(m, "enrich_forward_growth", side_effect=lambda *a, **kw: a[1]):
            m._screen_quality_pool(cfg, m.CacheManager(), {"regime": "bull"}, DAY,
                                   evaluator=fake_eval, index_df=index)
        self.assertGreaterEqual(len(seen), 2)                      # 预筛 + 财务重估两次评估
        self.assertTrue(all(x[1] is index for x in seen))
        self.assertIn(True, [x[0] for x in seen])                  # 预筛阶段 fund_data 为 None


# ===========================================================================
# 规则6：停牌缺口（has_halt_gap）接入 quality_value
# ===========================================================================
class TestHaltGapOnQualityValue(unittest.TestCase):
    def test_gap_vetoed_on_quality_value_path(self):
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
        base = mk_df(HEALTHY)
        self.assertEqual(ev(base, cfg=cfg)[1], "PASS")            # 前置条件：原序列可 PASS
        gapped = base.copy()
        gapped["date"] = dates_with_gap(len(gapped), gap_at=40, gap_days=40)
        sig, reason = ev(gapped, cfg=cfg)
        self.assertIsNone(sig)
        self.assertEqual(reason, "FAIL_HALT_GAP")

    def test_long_holiday_not_misjudged(self):
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
        base = mk_df(HEALTHY)
        for gap in (10, 7):                                       # 春节约 11 / 国庆约 8 天间隔
            with self.subTest(gap=gap):
                df = base.copy()
                df["date"] = dates_with_gap(len(df), gap_at=40, gap_days=gap)
                self.assertEqual(ev(df, cfg=cfg)[1], "PASS")

    def test_switch_off_restores_pass(self):
        base = mk_df(HEALTHY)
        df = base.copy()
        df["date"] = dates_with_gap(len(df), gap_at=40, gap_days=40)
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0, REQUIRE_NO_HALT_GAP=False)
        self.assertEqual(ev(df, cfg=cfg)[1], "PASS")

    def test_threshold_is_configurable(self):
        base = mk_df(HEALTHY)
        df = base.copy()
        df["date"] = dates_with_gap(len(df), gap_at=40, gap_days=40)
        cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0, MAX_BAR_GAP_DAYS=60)
        self.assertEqual(ev(df, cfg=cfg)[1], "PASS")

    def test_technical_path_behaviour_unchanged(self):
        cfg = m.StrategyConfig(RECOMMENDATION_MODE="technical")
        base = mk_df(HEALTHY)
        gapped = base.copy()
        gapped["date"] = dates_with_gap(len(gapped), gap_at=40, gap_days=40)
        self.assertFalse(m.has_halt_gap(base, cfg))
        self.assertTrue(m.has_halt_gap(gapped, cfg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
