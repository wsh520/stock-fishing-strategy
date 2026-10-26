"""周期性误判豁免回归（2026-10-26）

问题：Baostock query_stock_industry 只给证监会大类，C26「化学原料和化学制品制造业」
把 云天化(600096，磷肥，真周期) 与 珀莱雅(603605，化妆品)、登康口腔(001328，牙膏)
归成逐字相同的行业名，`classify_cyclical_industry` 无法用关键词区分。
实测后果：两只消费股被算进「周期组」，把该组均值从 -4.62% 抬到 -2.44%。

跑法：python tests/test_cyclical_exempt_20261026.py
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.recommendation_factors import classify_cyclical_industry
from src.quality_longterm_strategy import LongTermQualityConfig
from src.screening_engine import StrategyConfig

C26 = "C26化学原料和化学制品制造业"
CFG = StrategyConfig()


def classify(code, industry=C26):
    return classify_cyclical_industry(
        industry, CFG.CYCLICAL_INDUSTRY_CODES, CFG.CYCLICAL_INDUSTRY_KEYWORDS,
        code=code, exempt_codes=CFG.CYCLICAL_EXEMPT_CODES)


class CyclicalExemptTest(unittest.TestCase):
    def test_exempt_consumer_is_not_cyclical(self):
        """珀莱雅/登康口腔是消费必需品，行业名却与云天化完全相同 → 按代码豁免。"""
        self.assertEqual(classify("603605"), "non_cyclical")
        self.assertEqual(classify("001328"), "non_cyclical")

    def test_real_cyclical_still_cyclical(self):
        """豁免名单不得外溢：云天化、中国铝业、盐湖股份必须仍是周期股。"""
        self.assertEqual(classify("600096"), "cyclical")
        self.assertEqual(classify("601600", "C32有色金属冶炼和压延加工业"), "cyclical")
        self.assertEqual(classify("000792"), "cyclical")

    def test_missing_industry_still_missing_even_if_exempt(self):
        """行业缺失仍判 missing：豁免不得绕过「行业分类缺失」的待核验标记。

        若豁免优先于 missing，一只无行业数据的豁免票会被判 non_cyclical，
        连 industry_classification 缺项标签都不会打 —— 这是放宽，必须禁止。
        """
        for blank in ("", "  ", "unknown", "未分类", "nan", "None"):
            self.assertEqual(classify("603605", blank), "missing", f"industry={blank!r}")

    def test_backward_compatible_without_code(self):
        """不传 code 时行为与改动前逐字一致（老调用点/回测不受影响）。"""
        legacy = classify_cyclical_industry(
            C26, CFG.CYCLICAL_INDUSTRY_CODES, CFG.CYCLICAL_INDUSTRY_KEYWORDS)
        self.assertEqual(legacy, "cyclical")

    def test_exempt_list_is_wired_into_both_configs(self):
        """豁免名单必须存在于生产配置，并被长期口径继承。"""
        self.assertIn("603605", StrategyConfig.CYCLICAL_EXEMPT_CODES)
        self.assertIn("001328", StrategyConfig.CYCLICAL_EXEMPT_CODES)
        self.assertEqual(LongTermQualityConfig().CYCLICAL_EXEMPT_CODES,
                         StrategyConfig.CYCLICAL_EXEMPT_CODES)

    def test_non_cyclical_keyword_still_works(self):
        """非周期行业不受影响。"""
        self.assertEqual(classify("600519", "C15酒、饮料和精制茶制造业"), "non_cyclical")


if __name__ == "__main__":
    unittest.main(verbosity=2)
