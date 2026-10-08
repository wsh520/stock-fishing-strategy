"""正式推荐展示回归：截获最终 HTTP payload，所有通知及数据请求保持离线。"""
import json
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from notify import feishu


def recommendation(**updates):
    row = dict(code="000001", name="展示测试", date="2026-06-12", close=10.5,
               score=83.5, grade="A", daily_score=83.5, tier="formal",
               stop_loss=9.8, take_profit=12.08, rr_ratio=2.26,
               fund_status="verified", weekly_status="confirmed", market_env="bull",
               rsi=55., vol_ratio=2.4, breakout_level=3, breakout_margin=2.5,
               platform_range=7., avg_amount=12345.6, signals_hit="离线测试",
               historical_pe_percentile=.235, historical_pe_samples=180,
               financial_risk_status="verified", normalized_pe=18.6, cyclical_risk="peak",
               breakout_event_date="2026-06-09", breakout_confirmation_date="2026-06-12",
               breakout_category="held_confirmed", breakout_anchor=10.1,
               breakout_score=24.2, volume_br_score=21.6, pattern_score=13.7,
               trend_br_score=12., momentum_br_score=12., volume_ratio_score=15.4,
               consolidation_score=6.2, market_relative_strength=3.7,
               industry_relative_strength=-1.2, relative_strength_adjustment=.4)
    row.update(updates)
    return row


class RecommendationDisplayTests(unittest.TestCase):
    def payloads(self, rows, strategy="volume_breakout", pending=None):
        response = Mock()
        response.json.return_value = {"code": 0}
        with patch.object(feishu, "WEBHOOK_URL", "https://offline.invalid/webhook"), \
             patch.object(feishu.requests, "post", return_value=response) as post, \
             patch.object(feishu.requests, "get", side_effect=AssertionError("禁止联网取数")):
            feishu.notify_screening_result(pd.DataFrame(rows), strategy=strategy,
                                           pending=pending, market_env="离线牛市")
        return [call.kwargs["json"] for call in post.call_args_list]

    def body(self, payload):
        self.assertEqual(payload["msg_type"], "interactive")
        return "\n".join(element.get("content", "") for element in payload["card"]["elements"])

    def test_quality_evidence_visible_in_final_payload(self):
        row = recommendation(weekly_status="not_required", quality_score=88.,
                             valuation_score=82., pe_ttm=12., pb_mrq=1.4, position_250=.2,
                             technical_trend_score=32., technical_momentum_score=24.,
                             technical_volume_score=18.)
        payloads = self.payloads([row], strategy="bottom_fishing")
        self.assertEqual(len(payloads), 2)
        body = self.body(payloads[1])
        for text in ("自身历史PE分位: 23.5%", "有效历史样本: 180", "正常化PE 18.6",
                     "多年归母利润中位数口径", "盈利高峰风险", "财务风险: 已核验通过",
                     "技术子分: 趋势 32.0 / 动能 24.0 / 量能 18.0", "操作计划"):
            self.assertIn(text, body)
        self.assertNotIn("配置", body)

    def test_confirmed_breakout_preserves_event_score_and_dates(self):
        for category, label in (("held_confirmed", "已站稳确认"),
                                ("retest_confirmed", "回踩确认")):
            with self.subTest(category=category):
                body = self.body(self.payloads([recommendation(breakout_category=category)])[1])
                for text in (f"信号: {label}", "原事件评分: 83.5", "事件日: 2026-06-09",
                             "确认日: 2026-06-12", "确认日收盘（2026-06-12）: 10.5",
                             "固定阻力锚点: 10.10", "原事件五维子分: 突破 24.2 / 量能 21.6",
                             "平台 13.7 / 趋势 12.0 / 动能 12.0",
                             "原事件量能组成: 量比 15.4 + 整理 6.2（实际子分）",
                             "市场 3.7 | 行业 -1.2 | 排序调整 0.4",
                             "当前收盘价格日期为确认日"):
                    self.assertIn(text, body)
                self.assertNotIn("/17.5", body)
                self.assertNotIn("/7.5", body)
                self.assertNotIn("默认权重", body)
                self.assertEqual(body.count("五维子分:"), 1)
                self.assertEqual(body.count("量能组成:"), 1)

    def test_amount_keeps_upstream_ten_thousand_yuan_unit(self):
        body = self.body(self.payloads([recommendation()])[1])
        self.assertIn("日均额: 12345.6万元", body)
        self.assertNotIn("日均额: 1.2", body)

    def test_missing_evidence_is_neither_zero_nor_pass(self):
        row = recommendation(historical_pe_percentile=np.nan, historical_pe_samples=0,
                             normalized_pe=np.inf, cyclical_risk="missing",
                             financial_risk_status=pd.NA, breakout_anchor=0,
                             breakout_score=np.nan, volume_br_score=None,
                             volume_ratio_score=pd.NA, consolidation_score=np.inf,
                             market_relative_strength=None, industry_relative_strength=np.nan,
                             relative_strength_adjustment=None, avg_amount=0.)
        body = self.body(self.payloads([row])[1])
        for text in ("自身历史PE分位: 缺数据", "有效历史样本: 缺数据", "正常化PE 缺数据",
                     "财务风险: 待核验", "固定阻力锚点: 缺数据", "突破 缺数据 / 量能 缺数据",
                     "量比 缺数据 + 整理 缺数据", "市场 缺数据 | 行业 缺数据",
                     "日均额: 缺数据万元"):
            self.assertIn(text, body)
        self.assertNotIn("财务风险: 已核验通过", body)
        self.assertNotIn("nan", body.lower())
        self.assertNotIn("inf", body.lower())

    def test_real_zero_scores_and_rs_remain_visible(self):
        body = self.body(self.payloads([recommendation(breakout_score=0.,
            volume_ratio_score=0., consolidation_score=0., market_relative_strength=0.,
            industry_relative_strength=0., historical_pe_percentile=0.)])[1])
        self.assertIn("突破 0.0", body)
        self.assertIn("量比 0.0 + 整理 0.0", body)
        self.assertIn("市场 0.0 | 行业 0.0", body)
        self.assertIn("自身历史PE分位: 0.0%", body)

    def test_unrequired_risk_and_normalized_pe_not_labelled_pass(self):
        body = self.body(self.payloads([recommendation(financial_risk_status="not_required",
            cyclical_risk="not_required", normalized_pe=None)])[1])
        self.assertIn("财务风险: 本次未要求核验", body)
        self.assertIn("正常化PE 不适用", body)
        self.assertNotIn("财务风险: 已核验通过", body)
        failed = self.body(self.payloads([recommendation(financial_risk_status="failed")])[1])
        self.assertIn("财务风险: 未通过", failed)

    def test_first_event_unknown_category_and_missing_dates_are_explicit(self):
        first = self.body(self.payloads([recommendation(breakout_category="first_breakout",
            breakout_event_date="", breakout_confirmation_date="")])[1])
        self.assertIn("信号: 首次突破 | 事件日: 2026-06-12 | 确认日: 缺数据", first)
        self.assertIn("事件日收盘（2026-06-12）", first)
        unknown = self.body(self.payloads([recommendation(breakout_category=np.nan,
            breakout_event_date=None, breakout_confirmation_date=None)])[1])
        self.assertIn("信号: 类别待核验 | 事件日: 缺数据", unknown)
        self.assertNotIn("信号: 首次突破", unknown)

    def test_pandas_missing_cycle_status_does_not_imply_no_risk(self):
        body = self.body(self.payloads([recommendation(cyclical_risk=pd.NA,
            normalized_pe=pd.NA)])[1])
        self.assertIn("正常化PE 缺数据", body)
        self.assertIn("中位数口径） | 待核验", body)
        self.assertNotIn("未见盈利高峰风险", body)

    def test_confirmed_event_missing_confirmation_date_not_replaced_with_event_date(self):
        body = self.body(self.payloads([recommendation(breakout_confirmation_date=None)])[1])
        self.assertIn("事件日: 2026-06-09 | 确认日: 缺数据", body)
        self.assertIn("确认日收盘（缺数据）", body)
        self.assertNotIn("确认日收盘（2026-06-09）", body)

    def test_pending_not_rendered_and_trade_plan_unchanged(self):
        from src.volume_breakout_strategy import describe_breakout
        row = recommendation()
        original_plan = describe_breakout(row).split("操作计划", 1)[1].split("**信心", 1)[0]
        pending = recommendation(code="999999", name="不应展示的待核验票", tier="pending")
        payloads = self.payloads([row, pending], pending=pd.DataFrame([pending]))
        self.assertEqual(len(payloads), 2)
        body = self.body(payloads[1])
        self.assertEqual(body.split("操作计划", 1)[1].split("**信心", 1)[0], original_plan)
        self.assertNotIn("999999", json.dumps(payloads, ensure_ascii=False))
        self.assertNotIn("不应展示的待核验票", json.dumps(payloads, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
