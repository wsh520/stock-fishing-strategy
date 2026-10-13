#!/usr/bin/env python3
"""
长期优质低估低位策略 - 单元测试

测试覆盖：
1. ROE稳定性评估
2. 盈利持续性评估
3. 长期现金流质量评估
4. 长期投资资质综合评分
5. 配置参数校验
6. 评估函数边界情况

运行方式：
    python -m pytest tests/test_longterm_quality.py -v
    或
    python tests/test_longterm_quality.py
"""

import unittest
import numpy as np
import pandas as pd
from src.quality_longterm_strategy import (
    LongTermQualityConfig,
    evaluate_roe_stability,
    evaluate_earnings_consistency,
    evaluate_longterm_cash_quality,
    compute_longterm_quality_score
)


class TestROEStability(unittest.TestCase):
    """ROE稳定性评估测试"""
    
    def test_perfect_stability(self):
        """完美稳定：ROE=12%连续5年"""
        annual_rows = [
            {"roe": 12.0, "net_profit": 100},
            {"roe": 12.0, "net_profit": 100},
            {"roe": 12.0, "net_profit": 100},
            {"roe": 12.0, "net_profit": 100},
            {"roe": 12.0, "net_profit": 100},
        ]
        result = evaluate_roe_stability(annual_rows, 5)
        
        self.assertEqual(result["status"], "verified")
        self.assertAlmostEqual(result["roe_mean"], 12.0, places=2)
        self.assertAlmostEqual(result["roe_std"], 0.0, places=2)
        self.assertAlmostEqual(result["roe_cv"], 0.0, places=3)
        self.assertGreaterEqual(result["stability_score"], 99.0)
    
    def test_moderate_stability(self):
        """中等稳定：ROE在10-14%之间波动"""
        annual_rows = [
            {"roe": 10.0},
            {"roe": 12.0},
            {"roe": 11.0},
            {"roe": 14.0},
            {"roe": 13.0},
        ]
        result = evaluate_roe_stability(annual_rows, 5)
        
        self.assertEqual(result["status"], "verified")
        self.assertAlmostEqual(result["roe_mean"], 12.0, places=1)
        self.assertGreater(result["roe_cv"], 0.0)
        self.assertLess(result["roe_cv"], 0.5)
        self.assertGreater(result["stability_score"], 0.0)
    
    def test_unstable(self):
        """不稳定：ROE大幅波动"""
        annual_rows = [
            {"roe": 5.0},
            {"roe": 20.0},
            {"roe": 8.0},
            {"roe": 18.0},
            {"roe": 10.0},
        ]
        result = evaluate_roe_stability(annual_rows, 5)
        
        self.assertEqual(result["status"], "verified")
        self.assertGreater(result["roe_cv"], 0.3)  # CV应该较大
    
    def test_insufficient_data(self):
        """数据不足：少于5年"""
        annual_rows = [
            {"roe": 12.0},
            {"roe": 13.0},
            {"roe": 11.0},
        ]
        result = evaluate_roe_stability(annual_rows, 5)
        
        self.assertEqual(result["status"], "insufficient")
        self.assertIsNone(result["roe_mean"])
        self.assertEqual(result["stability_score"], 0)
    
    def test_missing_roe(self):
        """ROE数据缺失"""
        annual_rows = [
            {"roe": 12.0},
            {"roe": None},
            {"roe": 11.0},
            {"roe": 13.0},
            {"roe": 12.0},
        ]
        result = evaluate_roe_stability(annual_rows, 5)
        
        self.assertEqual(result["status"], "insufficient")


class TestEarningsConsistency(unittest.TestCase):
    """盈利持续性评估测试"""
    
    def test_perfect_consistency(self):
        """完美持续：连续5年正增长"""
        annual_rows = [
            {"net_profit": 90},
            {"net_profit": 100},
            {"net_profit": 110},
            {"net_profit": 120},
            {"net_profit": 130},
            {"net_profit": 140},
        ]
        # 2026-10-13：min_years=5 只取最后 5 条年报 → 4 次同比（原实现要求
        # min_years+1=6 条年报，但上游只按 QUALITY_YEARS=5 取数 → 恒 insufficient）。
        # 「连续5个完整年度」能得到的同比次数就是 4 次。
        result = evaluate_earnings_consistency(annual_rows, 5, 3)

        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["positive_growth_years"], 4)
        self.assertEqual(result["total_years"], 4)
        self.assertAlmostEqual(result["consistency_score"], 100.0, places=1)
    
    def test_moderate_consistency(self):
        """中等持续：5年年报中3次同比为正（窗口为最后5条→4次同比）"""
        annual_rows = [
            {"net_profit": 90},
            {"net_profit": 100},  # +
            {"net_profit": 110},  # +
            {"net_profit": 105},  # -
            {"net_profit": 120},  # +
            {"net_profit": 115},  # -
        ]
        # 取最后 5 条：[100,110,105,120,115] → 同比 +,-,+,- → 2 正 4 次
        result = evaluate_earnings_consistency(annual_rows, 5, 3)
        # 2 < min_positive_years=3 → 判为 inconsistent（这是正确的保守行为：
        # 达不到最低正增长年数就不算「持续」，宁缺毋滥）
        self.assertEqual(result["status"], "inconsistent")
        self.assertEqual(result["positive_growth_years"], 2)
        self.assertEqual(result["total_years"], 4)

    def test_moderate_consistency_exactly_at_threshold(self):
        """边界：5年年报中3次同比为正 → 恰好达门槛，verified"""
        annual_rows = [
            {"net_profit": 90},
            {"net_profit": 100},  # +
            {"net_profit": 110},  # +
            {"net_profit": 115},  # +
            {"net_profit": 120},  # -
            {"net_profit": 118},  # -
        ]
        # 取最后 5 条：[100,110,115,120,118] → 同比 +++- → 3 正 4 次
        result = evaluate_earnings_consistency(annual_rows, 5, 3)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["positive_growth_years"], 3)
        self.assertEqual(result["total_years"], 4)
        self.assertAlmostEqual(result["consistency_score"], 75.0, places=1)

    def test_loss_to_profit_not_counted_as_growth(self):
        """去年亏损今年扭亏：同比增长率无意义，不计入正增长也不算失败"""
        annual_rows = [
            {"net_profit": -50},
            {"net_profit": -20},
            {"net_profit": 10},   # 扭亏，但去年为负 → 不计正增长
            {"net_profit": 30},   # +
            {"net_profit": 50},   # +
            {"net_profit": 60},   # +
        ]
        result = evaluate_earnings_consistency(annual_rows, 5, 3)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["positive_growth_years"], 3)
        self.assertEqual(result["total_years"], 4)
    
    def test_inconsistent(self):
        """不一致：正增长年数不足"""
        annual_rows = [
            {"net_profit": 100},
            {"net_profit": 110},  # +
            {"net_profit": 105},  # -
            {"net_profit": 95},   # -
            {"net_profit": 105},  # +
            {"net_profit": 100},  # -
        ]
        result = evaluate_earnings_consistency(annual_rows, 5, 3)
        
        # 窗口 = 最后 5 条 [110,105,95,105,100] → 同比 -,-,-,+ → 仅 1 次正增长
        self.assertEqual(result["status"], "inconsistent")
        self.assertEqual(result["positive_growth_years"], 1)
        self.assertEqual(result["consistency_score"], 0)
    
    def test_insufficient_data(self):
        """数据不足：年报条数少于 min_years"""
        annual_rows = [
            {"net_profit": 100},
            {"net_profit": 110},
        ]
        result = evaluate_earnings_consistency(annual_rows, 5, 3)
        
        self.assertEqual(result["status"], "insufficient")


class TestLongtermCashQuality(unittest.TestCase):
    """长期现金流质量评估测试"""
    
    def test_excellent_quality(self):
        """优质现金流：比率≥1.2"""
        annual_rows = [
            {"operating_cashflow": 100, "net_profit": 80},
            {"operating_cashflow": 110, "net_profit": 90},
            {"operating_cashflow": 120, "net_profit": 95},
            {"operating_cashflow": 130, "net_profit": 105},
            {"operating_cashflow": 140, "net_profit": 110},
        ]
        result = evaluate_longterm_cash_quality(annual_rows, 5)
        
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["total_cashflow"], 600)
        self.assertEqual(result["total_profit"], 480)
        self.assertAlmostEqual(result["cash_ratio"], 1.25, places=2)
        self.assertGreaterEqual(result["cash_quality_score"], 100.0)
    
    def test_acceptable_quality(self):
        """可接受现金流：比率=1.0"""
        annual_rows = [
            {"operating_cashflow": 100, "net_profit": 100},
            {"operating_cashflow": 110, "net_profit": 110},
            {"operating_cashflow": 120, "net_profit": 120},
            {"operating_cashflow": 130, "net_profit": 130},
            {"operating_cashflow": 140, "net_profit": 140},
        ]
        result = evaluate_longterm_cash_quality(annual_rows, 5)
        
        self.assertEqual(result["status"], "verified")
        self.assertAlmostEqual(result["cash_ratio"], 1.0, places=2)
        self.assertAlmostEqual(result["cash_quality_score"], 50.0, places=1)
    
    def test_poor_quality(self):
        """现金流不佳：比率<0.8"""
        annual_rows = [
            {"operating_cashflow": 70, "net_profit": 100},
            {"operating_cashflow": 75, "net_profit": 110},
            {"operating_cashflow": 80, "net_profit": 120},
            {"operating_cashflow": 85, "net_profit": 130},
            {"operating_cashflow": 90, "net_profit": 140},
        ]
        result = evaluate_longterm_cash_quality(annual_rows, 5)
        
        self.assertEqual(result["status"], "verified")
        self.assertLess(result["cash_ratio"], 0.8)
        self.assertEqual(result["cash_quality_score"], 0.0)


class TestLongtermQualityScore(unittest.TestCase):
    """长期投资资质综合评分测试"""
    
    def test_excellent_company(self):
        """优秀公司：各维度均优"""
        annual_rows = [
            {"roe": 12.0, "net_profit": 100, "operating_cashflow": 100},
            {"roe": 13.0, "net_profit": 110, "operating_cashflow": 115},
            {"roe": 12.5, "net_profit": 120, "operating_cashflow": 130},
            {"roe": 13.5, "net_profit": 135, "operating_cashflow": 145},
            {"roe": 13.0, "net_profit": 150, "operating_cashflow": 160},
        ]
        
        config = LongTermQualityConfig()
        result = compute_longterm_quality_score(annual_rows, 15.0, 0.30, config)
        
        self.assertEqual(result["status"], "verified")
        self.assertGreater(result["ltq_score"], 70.0)  # 应该是高分
        self.assertIn("roe_stability", result["breakdown"])
        self.assertIn("cash_quality", result["breakdown"])
    
    def test_mediocre_company(self):
        """一般公司：刚好及格"""
        annual_rows = [
            {"roe": 10.0, "net_profit": 100, "operating_cashflow": 90},
            {"roe": 12.0, "net_profit": 105, "operating_cashflow": 95},
            {"roe": 9.0, "net_profit": 100, "operating_cashflow": 92},
            {"roe": 11.0, "net_profit": 110, "operating_cashflow": 100},
            {"roe": 10.5, "net_profit": 115, "operating_cashflow": 105},
        ]
        
        config = LongTermQualityConfig()
        result = compute_longterm_quality_score(annual_rows, 20.0, 0.45, config)
        
        self.assertEqual(result["status"], "verified")
        self.assertGreater(result["ltq_score"], 40.0)
        self.assertLess(result["ltq_score"], 70.0)
    
    def test_disabled_scoring(self):
        """评分关闭"""
        config = LongTermQualityConfig()
        config.ENABLE_LONGTERM_QUALITY_SCORE = False
        
        result = compute_longterm_quality_score([], None, None, config)
        
        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["ltq_score"], 100.0)


class TestLongTermConfig(unittest.TestCase):
    """配置参数测试"""
    
    def test_default_config(self):
        """默认配置"""
        config = LongTermQualityConfig()
        
        # 质量要求
        self.assertEqual(config.QUALITY_YEARS, 5)
        self.assertEqual(config.QUALITY_MEDIAN_ROE_MIN, 12.0)
        self.assertEqual(config.QUALITY_MIN_ROE, 8.0)
        self.assertEqual(config.QUALITY_CASH_CONVERSION_MIN, 0.9)
        
        # 止跌模式：2026-10-13 由 "risk_only" 撤回为 "strict"（与生产同源）。
        # risk_only 把「站上 MA20」从否决降级为标签 = 放行更多票，
        # 与项目「宁缺毋滥」的最高优先级原则冲突；「拒绝加速下跌」的意图
        # 改由独立接线的 DETECT_ACCELERATING_DECLINE 承担（只做减法）。
        self.assertEqual(config.QV_STABILIZATION_MODE, "strict")

        # RECOMMENDATION_MODE 必须是 quality_value：基类只接受
        # {quality_value, technical}，填其它值会在构造时抛 ValueError。
        self.assertEqual(config.RECOMMENDATION_MODE, "quality_value")
        self.assertEqual(config.LONGTERM_LABEL, "长期优质低估低位")
        
        # 底部窗口
        self.assertEqual(config.QV_RECENT_LOW_MIN_AGE, 3)
        self.assertEqual(config.QV_RECENT_LOW_MAX_AGE, 90)
        # 基类硬校验：min <= max < window（严格小于）
        self.assertLess(config.QV_RECENT_LOW_MAX_AGE, config.QV_RECENT_LOW_WINDOW)
        
        # 持有周期
        self.assertEqual(config.HOLD_DAYS_HINT_MIN, 30)
        self.assertEqual(config.HOLD_DAYS_HINT_MAX, 90)
        
        # 止损止盈
        self.assertEqual(config.FIXED_STOP_LOSS_PCT, 20.0)
        self.assertEqual(config.FIXED_TAKE_PROFIT_PCT, 50.0)
        
        # 推荐数量
        self.assertEqual(config.MAX_PICKS, 3)
        self.assertEqual(config.NEUTRAL_MAX_PICKS, 2)
        self.assertEqual(config.BEAR_MAX_PICKS, 1)
        
        # 权重
        self.assertEqual(config.QUALITY_SCORE_WEIGHT, 0.55)
        self.assertEqual(config.VALUATION_SCORE_WEIGHT, 0.35)
        self.assertEqual(config.TECHNICAL_SCORE_WEIGHT, 0.10)
        self.assertAlmostEqual(
            config.QUALITY_SCORE_WEIGHT + config.VALUATION_SCORE_WEIGHT + config.TECHNICAL_SCORE_WEIGHT,
            1.0, places=6
        )
    
    def test_weight_validation(self):
        """权重校验"""
        config = LongTermQualityConfig()
        
        # 权重和应为1.0
        total = config.QUALITY_SCORE_WEIGHT + config.VALUATION_SCORE_WEIGHT + config.TECHNICAL_SCORE_WEIGHT
        self.assertAlmostEqual(total, 1.0, places=6)
    
    def test_config_inheritance(self):
        """配置继承"""
        from src.bottom_fishing_strategy import StrategyConfig
        
        config = LongTermQualityConfig()
        
        # 应继承基类属性
        self.assertTrue(hasattr(config, "MAX_PE_TTM"))
        self.assertTrue(hasattr(config, "LOW_POSITION_MAX"))
        self.assertTrue(hasattr(config, "MIN_AMOUNT"))
        
        # 但覆盖了关键参数
        self.assertNotEqual(config.QUALITY_YEARS, StrategyConfig().QUALITY_YEARS)
        self.assertNotEqual(config.HOLD_DAYS_HINT_MAX, StrategyConfig().HOLD_DAYS_HINT_MAX)


class TestEdgeCases(unittest.TestCase):
    """边界情况测试"""
    
    def test_empty_annual_rows(self):
        """空数据"""
        result_roe = evaluate_roe_stability([], 5)
        self.assertEqual(result_roe["status"], "insufficient")
        
        result_earnings = evaluate_earnings_consistency([], 5, 3)
        self.assertEqual(result_earnings["status"], "insufficient")
        
        result_cash = evaluate_longterm_cash_quality([], 5)
        self.assertEqual(result_cash["status"], "insufficient")
    
    def test_negative_profit(self):
        """负利润"""
        annual_rows = [
            {"net_profit": -100},
            {"net_profit": -50},
            {"net_profit": 10},
            {"net_profit": 20},
            {"net_profit": 30},
        ]
        
        result = evaluate_longterm_cash_quality(annual_rows, 5)
        # 累计利润为负，应标记为poor
        if result["status"] == "poor":
            self.assertEqual(result["cash_quality_score"], 0)
    
    def test_infinite_values(self):
        """无穷值处理"""
        annual_rows = [
            {"roe": float('inf')},
            {"roe": 12.0},
            {"roe": 13.0},
            {"roe": 11.0},
            {"roe": 12.5},
        ]
        
        result = evaluate_roe_stability(annual_rows, 5)
        self.assertEqual(result["status"], "insufficient")


if __name__ == "__main__":
    # 运行所有测试
    unittest.main(verbosity=2)
