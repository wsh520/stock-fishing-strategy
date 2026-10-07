"""荐股逻辑四项升级的离线回归测试（无网络、无数据库、无 baostock/akshare）。

覆盖：
  #1 突破策略独立成第二信号源（technical 模式不再委派 quality_value 资格）
  #2 quality_value 接入市场级刹车（regime 数量收缩 + 急跌熔断）
  #3 综合分下限 MIN_QV_SCORE（仅对 formal 生效，pending 不受其累）
  #3b 维度短板门槛（MIN_QUALITY_SCORE 只判已核验样本 / MIN_TECHNICAL_SCORE_FORMAL 降级 pending）
  #4a 行业相对估值闸门（行业内分位 + 绝对阈值回退）
  #4b 前瞻确认闸门（当年净利同比恶化否决 / 缺失行为可配）
"""
import unittest
from dataclasses import replace
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
    fund = {"debt_ratio": 40., "roe": 2., "forward_ni_yoy": 12., "forward_stat_date": "2025Q1", "annual_rows": [
        {"year": y, "report_date": f"{y}-12-31", "available_date": f"{y+1}-04-20",
         "roe": r, "deducted_profit": 90., "net_profit": 100., "operating_cashflow": 110.}
        for y, r in zip((2022, 2023, 2024), roe)]}
    fund.update(extra)
    return fund


def ev(df, fund, cfg=None, val_context=None, **kw):
    cfg = cfg or m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False)
    return m.evaluate(df, "600001", "工业企业", cfg, {"regime": "bear"}, fund,
                      latest_trade_date=kw.get("latest_trade_date", DAY), val_context=val_context)


# ===========================================================================
# #3 综合分下限 MIN_QV_SCORE
# ===========================================================================
class TestScoreFloor(unittest.TestCase):
    def test_low_score_formal_rejected_at_default_floor(self):
        # 2026-10-07：MIN_QV_SCORE 由 50 下调至 38（见配置注释）。
        # 构造「质量分压线(50) + 估值分低(PE24) → 综合分 < 38」的 formal 候选：
        #   0.45×50.0 + 0.30×估值分(PE24) + 0.25×0(技术分门槛置0) < 38
        # 该样本质量/估值各自都「压线合格」，只有综合分不达标 —— 正是本闸门该拦的情形。
        df, fund = make_df(pe=24., pb=1.2), make_fund(roe=(5, 10, 10))
        sig, reason = ev(df, fund, m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, MIN_TECHNICAL_SCORE_FORMAL=0))
        self.assertIsNone(sig)
        self.assertEqual(reason, "FAIL_QV_SCORE")

    def test_floor_disabled_passes(self):
        df, fund = make_df(pe=12., pb=1.2), make_fund(roe=(11, 12, 13))
        # MIN_TECHNICAL_SCORE_FORMAL=0：隔离技术短板门槛（其行为由 TestDimensionFloors 覆盖）
        sig, reason = ev(df, fund, m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, MIN_QV_SCORE=0,
                                                    MIN_TECHNICAL_SCORE_FORMAL=0))
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "formal")

    def test_high_score_passes_default_floor(self):
        # 综合分≈62.3 ≥ 默认下限 38；技术短板门槛置 0 隔离（本用例只验证综合分下限语义）
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, MIN_TECHNICAL_SCORE_FORMAL=0)
        sig, reason = ev(make_df(), make_fund(), cfg)
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "formal")
        self.assertGreaterEqual(sig.score, m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", ).MIN_QV_SCORE)

    def test_default_floor_below_formal_theoretical_minimum(self):
        """2026-10-07 新增：默认下限必须低于「三项恰好压线合格」的理论最低分 45.75。

        这是本次下调的核心不变量 —— 下限低于 45.75 意味着
        「质量50 / 估值40 / 技术45」全部恰好达标的候选**必然通过**，
        该闸门不再被用来二次筛技术分（那正是旧值 50 的问题：
        0.45×50+0.30×40+0.25×T ≥ 50 ⟹ T ≥ 62，近似一道隐式技术分硬闸门）。
        """
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy")
        formal_min = (cfg.QUALITY_SCORE_WEIGHT * 50.0 + cfg.VALUATION_SCORE_WEIGHT * 40.0
                      + cfg.TECHNICAL_SCORE_WEIGHT * cfg.MIN_TECHNICAL_SCORE_FORMAL)
        # 用计算式而非硬编码：MIN_TECHNICAL_SCORE_FORMAL 于 2026-10-07 由 45 降至 40
        # （方案 A），压线理论最低分随之由 45.75 变为 44.5。硬编码会在调参时立刻失效。
        self.assertAlmostEqual(formal_min, 44.5, places=2)
        self.assertLess(cfg.MIN_QV_SCORE, formal_min,
                        "默认综合分下限必须低于压线理论最低分，否则变成隐式技术分闸门")
        self.assertLessEqual(cfg.MIN_TECHNICAL_SCORE_FORMAL, 40.0,
                             "技术分门槛不得高于 40（再上调会把方案 A 的放行额度收回）")

    def test_floor_not_applied_to_pending(self):
        # 缺 PE → 估值分记 0、综合分被人为压低；但属 pending，下限不应把它直接否决，
        # 否则「数据缺失」与「质量不够」两种语义被混为一谈。
        # 2026-10-07（下限 50→38）：改用 PE=24 让有 PE 时的分数本就低于下限，
        # 再抽掉 PE 使分数进一步走低 —— 断言「分数确实低于下限」这个前提更明确。
        df = make_df(pe=24., pb=1.2)
        sig, reason = ev(df, make_fund(roe=(5, 10, 10)), m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False))
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "pending")
        self.assertLess(sig.score, m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", ).MIN_QV_SCORE)
        df_missing = df.copy()
        df_missing.loc[df_missing.index[-1], "peTTM"] = np.nan
        sig2, reason2 = ev(df_missing, make_fund(roe=(5, 10, 10)), m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False))
        self.assertEqual(reason2, "PASS")
        self.assertEqual(sig2.tier, "pending")


class TestDimensionFloors(unittest.TestCase):
    """维度短板门槛（MIN_QUALITY_SCORE / MIN_TECHNICAL_SCORE_FORMAL，2026-09 新增）。

    质量短板只判**已核验**样本：verified 的评分锚点决定质量分恒 ≥50，默认门槛 50
    是保底防线，上调后才实际拦截「已核验但平庸」的候选；质量数据缺失/部分缺失/
    金融专项待核验仍按分层约定降级 pending（缺失不误杀），不被本门槛否决。
    技术短板不否决、只把 tech_score < 45 的候选降为 pending（基本面好但入场时机未到）。
    """

    def test_verified_mediocre_quality_rejected_when_floor_raised(self):
        # ROE 11/12/13 → verified、质量分≈66.2；门槛上调到 70 → FAIL_QUALITY_FLOOR
        df, fund = make_df(pe=12., pb=1.2), make_fund(roe=(11, 12, 13))
        sig, reason = ev(df, fund, m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, MIN_QUALITY_SCORE=70))
        self.assertIsNone(sig)
        self.assertEqual(reason, "FAIL_QUALITY_FLOOR")

    def test_missing_quality_not_rejected_by_floor(self):
        # 年度质量数据缺失（质量分=0）：只判 verified 的门槛不得否决，仍降级 pending
        sig, reason = ev(make_df(), {}, m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False))
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "pending")
        self.assertEqual(sig.quality_status, "missing")

    def test_low_tech_score_demoted_to_pending_not_rejected(self):
        # 技术分 0 < 45：不否决（综合分≈51.2 已过下限），降为 pending 保留跟踪价值
        sig, reason = ev(make_df(pe=12., pb=1.2), make_fund(roe=(11, 12, 13)),
                         m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False))
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "pending")
        self.assertEqual(sig.daily_score, 0.0)

    def test_tech_floor_can_be_disabled(self):
        sig, reason = ev(make_df(pe=12., pb=1.2), make_fund(roe=(11, 12, 13)),
                         m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, MIN_TECHNICAL_SCORE_FORMAL=0))
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "formal")


class TestScoreFloorEquivalence(unittest.TestCase):
    """把「MIN_QV_SCORE=50 相对硬闸门的等效门槛」固化成可执行断言。

    背景：评分口径决定了「恰好压线达标」样本的分数锚点——
      · 质量分：_dimension_score 在阈值处恰好给 50 分；
      · 估值分（仅 PE）：行业口径在分位 0.60（闸门上限）处给 40 分；绝对口径在
        PE=MAX_PE_TTM（闸门上限）处**同样**给 40 分（锚点对齐，见 _valuation_pe_score）。
    权重 0.45/0.30/0.25 下，压线合格者即便技术分满分，综合分上限也只有 59.5；
    而 formal 候选的理论最低分（质量 50 + 估值 40 + 技术门槛 45）= 45.75。
    默认下限 50 落在两者之间：不再是「严格强于硬闸门」（旧口径 60 > 上限 54），
    而是**高于 formal 理论最低分**——能实际拦截「三项都仅勉强达标」的候选，
    压线合格者须凭技术分（≥62）过线。改动评分权重/闸门值/下限都会使这些数字漂移，故在此锁死；
    并直接校验随产品代码发布的 qv_floor_equivalence()/describe_qv_floor()，避免文档与实现漂移。
    """

    def test_quality_score_floor_for_verified_sample(self):
        from src.fundamental_quality import evaluate_annual_quality
        # 三档同时压线的最小可达样本：3 年 ROE=(5,10,10)（中位 10=阈值、最低 5=阈值）
        # + 现金转换 0.8=阈值 → 仍判 verified，但质量分只有 50
        rows = [{"year": y, "report_date": f"{y}-12-31", "available_date": f"{y+1}-04-20",
                 "roe": roe, "deducted_profit": 90., "net_profit": 100.,
                 "operating_cashflow": 80.} for y, roe in ((2022, 5.), (2023, 10.), (2024, 10.))]
        q = evaluate_annual_quality(rows, DAY, years=3)
        self.assertEqual(q["status"], "verified")
        self.assertAlmostEqual(q["quality_score"], 50.0, places=2)
        # 对照：三年皆 10（看起来更"均匀"）也只有 56.25
        even = [dict(r, roe=10.0) for r in rows]
        self.assertAlmostEqual(evaluate_annual_quality(even, DAY, years=3)["quality_score"],
                               56.25, places=2)

    def test_shipped_helper_reports_floor_between_formal_min_and_ceiling(self):
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", )
        e = m.qv_floor_equivalence(cfg)
        # 2026-10-07：下限由 50 下调至 38（低于 formal 理论最低分 45.75），
        # 使该闸门不再被当作「隐式技术分 ≥62 筛选」使用，回归「技术面不否决」的设计声明。
        self.assertAlmostEqual(e["floor"], 38.0, places=2)
        self.assertAlmostEqual(e["min_verified_quality"], 50.0, places=2)
        self.assertAlmostEqual(e["valuation_industry_at_cap"], 40.0, places=6)
        # 规则4：绝对口径压线处与行业口径锚定相同估值分（均为 40）
        self.assertAlmostEqual(e["valuation_absolute_at_cap"], 40.0, places=6)
        # 压线合格者技术分满分时的综合分上限 = 0.45×50 + 0.30×40 + 0.25×100 = 59.5
        for key in ("ceiling_industry", "ceiling_absolute"):
            self.assertAlmostEqual(e[key], 59.5, places=2)
        # 2026-10-07：下限 38 落在 formal 理论最低分 45.75 **之下**
        # → 「三项恰好压线合格」的候选必然通过，本闸门只拦「有明显短板」的情形，
        #   不再二次筛技术分（旧值 50 会要求技术分 ≥62，等价于隐式技术硬闸门）。
        formal_min = (cfg.QUALITY_SCORE_WEIGHT * 50.0 + cfg.VALUATION_SCORE_WEIGHT * 40.0
                      + cfg.TECHNICAL_SCORE_WEIGHT * cfg.MIN_TECHNICAL_SCORE_FORMAL)
        self.assertAlmostEqual(formal_min, 44.5, places=2)
        self.assertLess(cfg.MIN_QV_SCORE, formal_min)
        self.assertLess(cfg.MIN_QV_SCORE, e["ceiling_industry"])
        # 日志说明必须随配置动态给出正确结论（当前口径：上限不低于下限）
        desc = m.describe_qv_floor(cfg)
        self.assertIn("不低于下限", desc)
        self.assertNotIn("严格强于三道硬闸门", desc)

    def test_describe_qv_floor_strict_branch_when_floor_above_ceiling(self):
        # 下限抬到压线上限之上（如旧口径 60 > 59.5）时，结论必须切回「严格强于硬闸门」
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", MIN_QV_SCORE=60)
        desc = m.describe_qv_floor(cfg)
        self.assertIn("严格强于三道硬闸门", desc)
        self.assertIn("不可达（需 >100）", desc)

    def test_implied_technical_requirement_matches_documented_values(self):
        # 反解 0.45q + 0.30v + 0.25t ≥ MIN_QV_SCORE 所需技术分，与配置注释/README 口径一致。
        # 规则4后：行业/绝对口径压线处估值分均为 40（锚点对齐）。
        # 2026-10-07：下限 50→38 后，压线质量分(50)+压线估值分(40) 的组合**不再需要技术分**
        # （0.45×50+0.30×40 = 34.5 < 38，仍需 t ≥ 14）——这是本次修复的核心结果：
        # 该闸门不再是「隐式技术分 ≥62 硬闸门」，技术面权重回归「仅排序、不否决」。
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", )

        def required_tech(quality_score, valuation_score):
            return (cfg.MIN_QV_SCORE - cfg.QUALITY_SCORE_WEIGHT * quality_score
                    - cfg.VALUATION_SCORE_WEIGHT * valuation_score) / cfg.TECHNICAL_SCORE_WEIGHT

        # 质量分=压线下限 50，估值分=40：技术分只需 ≥14（原 50 时需 ≥62）
        self.assertAlmostEqual(required_tech(50, 40), 14.0, places=1)
        # 质量分 70：无需技术分（负值 → 无约束）
        self.assertLessEqual(required_tech(70, 40), 0.0)
        self.assertLessEqual(required_tech(90, 40), 0.0)
        # 与随代码发布的 helper 数值一致
        e = m.qv_floor_equivalence(cfg)
        self.assertAlmostEqual(e["req_tech_industry"], required_tech(50, 40), places=1)
        self.assertAlmostEqual(e["req_tech_absolute"], required_tech(50, 40), places=1)


# ===========================================================================
# #4b 前瞻确认（当年成长未恶化）
# ===========================================================================
class TestForwardConfirmation(unittest.TestCase):
    def test_deteriorating_growth_vetoed(self):
        # 硬否决线为 FORWARD_NI_YOY_MIN(-10%)：明显低于阈值一律 FAIL_FORWARD
        for yoy in (-55.0, -30.0, -10.01):
            with self.subTest(yoy=yoy):
                sig, reason = ev(make_df(), make_fund(forward_ni_yoy=yoy),
                                 m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False))
                self.assertIsNone(sig)
                self.assertEqual(reason, "FAIL_FORWARD")

    def test_healthy_growth_passes_and_is_tagged(self):
        fund = make_fund(forward_ni_yoy=12.0)
        sig, reason = ev(make_df(), fund, m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False))
        self.assertEqual(reason, "PASS")
        self.assertIn("2025Q1 净利同比+12%", sig.signals_hit)

    def test_missing_growth_pending_by_default(self):
        sig, reason = ev(make_df(), make_fund(forward_ni_yoy=None), m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False))
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "pending")

    def test_missing_growth_pending_when_configured(self):
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, FORWARD_MISSING_AS_PENDING=True)
        sig, reason = ev(make_df(), make_fund(forward_ni_yoy=None), cfg)
        self.assertEqual(reason, "PASS")
        self.assertEqual(sig.tier, "pending")
        self.assertIn("forward", sig.missing_tags)

    def test_gate_disabled_ignores_bad_growth(self):
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, REQUIRE_FORWARD_CONFIRMATION=False)
        sig, reason = ev(make_df(), make_fund(forward_ni_yoy=-90.0), cfg)
        self.assertEqual(reason, "PASS")

    def test_boundary_equal_to_threshold_passes(self):
        # 恰好等于阈值（-10）不算「低于」，应放行
        sig, reason = ev(make_df(), make_fund(forward_ni_yoy=-10.0),
                         m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False))
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
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False)
        snap = {"银行": {"pe": np.arange(5., 15.),
                         "pb": np.arange(5., 15.) / 10}}
        # 关闭 → None
        off = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, USE_INDUSTRY_RELATIVE_VALUATION=False)
        self.assertIsNone(m._industry_valuation_context(snap, "银行", 8., .8, off))
        # 无快照 / 无行业 / 行业不在快照 → None
        self.assertIsNone(m._industry_valuation_context({}, "银行", 8., .8, cfg))
        self.assertIsNone(m._industry_valuation_context(snap, "", 8., .8, cfg))
        self.assertIsNone(m._industry_valuation_context(snap, "地产", 8., .8, cfg))
        # 9 只仍不足以启用行业分位；PE/PB 各自独立检查样本数。
        thin = {"银行": {"pe": snap["银行"]["pe"][:9], "pb": snap["银行"]["pb"][:9]}}
        self.assertIsNone(m._industry_valuation_context(thin, "银行", 8., .8, cfg))
        mixed = {"银行": {"pe": snap["银行"]["pe"][:9], "pb": snap["银行"]["pb"]}}
        self.assertIsNone(m._industry_valuation_context(mixed, "银行", 8., .8, cfg)["pe_pct"])
        # 10 只满足默认样本门槛。
        ctx = m._industry_valuation_context(snap, "银行", 8., .8, cfg)
        self.assertEqual(ctx["mode"], "industry")
        self.assertEqual(ctx["peers"], 10)
        self.assertAlmostEqual(ctx["pe_pct"], 4 / 10)
        self.assertAlmostEqual(ctx["pb_pct"], 4 / 10)

    def test_industry_cheap_passes_despite_high_absolute_pe(self):
        # PE=40（> 绝对上限 25）但行业内分位 0.30（便宜）→ 行业模式放行
        ctx = {"mode": "industry", "pe_pct": 0.30, "pb_pct": 0.40, "peers": 12}
        df = make_df(pe=40., pb=2.0)
        sig, reason = ev(df, make_fund(), m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, MIN_QV_SCORE=0),
                         val_context=ctx)
        self.assertEqual(reason, "PASS")

    def test_industry_expensive_rejected_despite_ok_absolute_pe(self):
        # PE=20（< 绝对上限 25）但行业内分位 0.90（贵）→ 行业模式否决
        ctx = {"mode": "industry", "pe_pct": 0.90, "pb_pct": 0.40, "peers": 12}
        df = make_df(pe=20., pb=1.5)
        sig, reason = ev(df, make_fund(), m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, MIN_QV_SCORE=0),
                         val_context=ctx)
        self.assertIsNone(sig)
        self.assertEqual(reason, "FAIL_VALUATION")

    def test_absolute_fallback_without_context(self):
        # 无 val_context → 绝对阈值：PE=40 > 25 否决
        sig, reason = ev(make_df(pe=40., pb=2.0), make_fund(),
                         m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, MIN_QV_SCORE=0))
        self.assertEqual(reason, "FAIL_VALUATION")

    def test_negative_pe_still_vetoed_in_industry_mode(self):
        ctx = {"mode": "industry", "pe_pct": 0.10, "pb_pct": 0.10, "peers": 12}
        sig, reason = ev(make_df(pe=-5., pb=1.0), make_fund(),
                         m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, MIN_QV_SCORE=0), val_context=ctx)
        self.assertEqual(reason, "FAIL_VALUATION")

    def test_snapshot_build_and_empty_industry(self):
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, MAX_WORKERS=2)
        stocks = [{"code": f"60000{i}", "name": f"银行{i}"} for i in range(1, 7)]
        industry = {f"60000{i}": "银行" for i in range(1, 7)}

        def daily(code, config, cache=None):
            i = int(str(code)[-1])
            return make_df(pe=float(5 + i), pb=float(0.4 + 0.1 * i))

        with patch.object(m, "get_stock_industry", return_value=industry), \
             patch.object(m, "get_index_daily", return_value=pd.DataFrame({"date": [DAY]})), \
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
        cfg = vb.VolumeBreakoutConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, RECOMMENDATION_MODE="technical")
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

    def _crashing(self, cfg):
        """构造一根必定越阈值的指数序列。

        跌幅按 MARKET_CRASH_HALT_PCT 相对构造（阈值再深 1pp），不写死 -10%：
        2026-10-05 锚点由 −4.0 放宽到 −8.0 后，写死的 -10% 仍能触发但已远离边界，
        阈值一旦再放宽就会静默失效而不报警。
        """
        pct = (cfg.MARKET_CRASH_HALT_PCT - 1.0) / 100.0
        n = cfg.MARKET_CRASH_LOOKBACK
        base, last = 100.0, 100.0 * (1.0 + pct)
        return self._index([base] * (n + 1) + [last])

    def test_crash_halt_blocks_quality_value(self):
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False)
        crashing = self._crashing(cfg)
        with patch.object(m, "_bs_login", return_value=True), patch.object(m, "_bs_logout"), \
             patch.object(m, "get_market_environment", return_value={"regime": "neutral"}), \
             patch.object(m, "get_index_daily", return_value=crashing), \
             patch.object(m, "_screen_quality_pool", return_value=pd.DataFrame()) as screen:
            out = m.main(cfg, m.CacheManager())
        self.assertIsNone(out)
        self.assertEqual(screen.call_count, 0)  # 熔断在筛选之前

    def test_halt_threshold_relaxes_the_shutdown_window(self):
        """锚点放宽（−4% → −8%）后，「跌 4%~8%」区间应恢复选股，而非继续整日停荐。

        固化 2026-10-05 的放宽语义：−6% 在旧锚点下熔断、在新锚点下放行。
        """
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False)
        mid = self._index([100.] * (cfg.MARKET_CRASH_LOOKBACK + 1) + [94.0])  # 近5日 -6%
        self.assertIsNotNone(m.market_crash_halt(mid, replace(cfg, MARKET_CRASH_HALT_PCT=-4.0)))
        self.assertIsNone(m.market_crash_halt(mid, cfg))
        with patch.object(m, "_bs_login", return_value=True), patch.object(m, "_bs_logout"), \
             patch.object(m, "get_market_environment", return_value={"regime": "neutral"}), \
             patch.object(m, "get_index_daily", return_value=mid), \
             patch.object(m, "_screen_quality_pool", return_value=pd.DataFrame()) as screen:
            m.main(cfg, m.CacheManager())
        self.assertEqual(screen.call_count, 1)  # 已进入筛选，未被熔断拦下

    def test_bear_regime_contracts_max_picks(self):
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False)
        flat = self._index([100.] * 7)  # 不触发熔断
        with patch.object(m, "_bs_login", return_value=True), patch.object(m, "_bs_logout"), \
             patch.object(m, "get_market_environment", return_value={"regime": "bear"}), \
             patch.object(m, "get_index_daily", return_value=flat), \
             patch.object(m, "_screen_quality_pool", return_value=pd.DataFrame()) as screen:
            m.main(cfg, m.CacheManager())
        self.assertEqual(screen.call_count, 1)
        self.assertEqual(screen.call_args.kwargs.get("max_picks"), cfg.BEAR_MAX_PICKS)

    def test_bull_regime_uses_full_max_picks(self):
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False)
        flat = self._index([100.] * 7)
        with patch.object(m, "_bs_login", return_value=True), patch.object(m, "_bs_logout"), \
             patch.object(m, "get_market_environment", return_value={"regime": "bull"}), \
             patch.object(m, "get_index_daily", return_value=flat), \
             patch.object(m, "_screen_quality_pool", return_value=pd.DataFrame()) as screen:
            m.main(cfg, m.CacheManager())
        self.assertEqual(screen.call_args.kwargs.get("max_picks"), cfg.MAX_PICKS)

    def test_screen_pool_honors_max_picks_cap(self):
        # _screen_quality_pool 应按传入 max_picks 截取，而非恒用 config.MAX_PICKS
        # MIN_TECHNICAL_SCORE_FORMAL=0：合成样本技术分为 0，隔离技术短板降级对本用例的干扰
        cfg = m.StrategyConfig(REQUIRE_RECENT_OPERATING=False, DAILY_SCORING_MODE="legacy", USE_CACHE=False, MAX_WORKERS=1, FETCH_DELAY=0, MAX_PICKS=5,
                               MIN_TECHNICAL_SCORE_FORMAL=0)
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
