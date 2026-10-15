"""2026-10-15 P0 回归：商誉 JSON null 必须判为零，而非证据缺失。

背景（2026-10-15 生产日志）：6 只候选（茅台/公牛/飞科/浔兴/菜百/瑞鹄）被标
financial_risk=missing，missing_tags 含 financial_risk_goodwill / financial_risk_goodwill_ratio。
根因是新浪 fzb 对无商誉公司返回 JSON null，_number(None) -> None，被当作 unknown。

实证依据：
  * 抽样 150 只：null 50.0%、数字字符串 50.0%、原始数值 0 只（二元分布，非抓取失败）
  * 52/52 null 代码同期「资产总计/负债合计」均解析成功（整表拉取正常）
  * 公牛集团 20230930=22566721.02，其后连续 9 期 null —— 只有真实归零能产生该模式

修复口径：仅「商誉字段缺席 / 值不可解析 / 重复歧义」保持 unknown；
      「字段存在且值为 JSON null」判为零，goodwill_ratio=0，按 MAX_GOODWILL_RATIO 正常通过。
这是把误杀恢复成正确判定，不是放宽风控：阈值与判定顺序均未改动。
离线执行，不触网。
"""
import json
import os
import tempfile
import unittest
from unittest.mock import Mock

from src.bottom_fishing_strategy import StrategyConfig, enrich_financial_risk
from src.financial_risk import (_goodwill_diagnostics, combine_financial_rows,
                                evaluate_financial_risk, parse_balance,
                                validated_financial_risk_status)
from src.recent_operating import parse_summary


def balance_body(goodwill="present", value=None):
    items = [{"item_title": "资产总计", "item_value": 1000},
             {"item_title": "负债合计", "item_value": 400}]
    if goodwill == "present":
        items.append({"item_title": "商誉", "item_value": value})
    return {"result": {"data": {"report_list": {"20260331": {
        "rType": "合并期末", "rCurrency": "CNY", "publish_date": "20260420",
        "data_source": "company", "data": items}}}}}


def summary_body():
    return {"result": {"data": {"report_list": {"20260331": {
        "rType": "合并期末", "rCurrency": "CNY", "publish_date": "20260420",
        "data_source": "company",
        "data": [{"item_field": "NETPROFIT", "item_value": 100, "item_group_no": 1},
                 {"item_field": "NPCUT", "item_value": 90, "item_group_no": 1}]}}}}}


def canonical(**kwargs):
    row = dict(total_assets=1000, total_liabilities=400, goodwill=10, net_profit=100,
               deducted_profit=90, report_date="2026-03-31", available_date="2026-04-20",
               source="test", currency="CNY", statement_basis="consolidated")
    row.update(kwargs)
    return row


class GoodwillNullSemanticsTests(unittest.TestCase):
    def test_state_machine_explicit_null_is_zero(self):
        cases = [([], "absent"), ([None], "explicit_null"), ([""], "empty_or_non_numeric"),
                 (["None"], "empty_or_non_numeric"), (["abc"], "empty_or_non_numeric"),
                 ([0], "explicit_zero"), ([0.0], "explicit_zero"), (["0"], "explicit_zero"),
                 (["677416423.000000"], "valid"), ([-5], "invalid_negative"),
                 ([None, None], "duplicate"), ([10, "10"], "duplicate")]
        for values, expected in cases:
            with self.subTest(values=values):
                self.assertEqual(_goodwill_diagnostics(values, "t")["state"], expected)

    def test_only_explicit_null_imputes_zero(self):
        self.assertEqual(_goodwill_diagnostics([None], "t")["parsed_value"], 0.0)
        for values in ([], [""], ["None"], ["abc"], [None, None]):
            with self.subTest(values=values):
                self.assertIsNone(_goodwill_diagnostics(values, "t")["parsed_value"])
        self.assertEqual(_goodwill_diagnostics([None], "t")["raw_samples"], ["None"])

    def test_parse_balance_null_carries_zero_not_none(self):
        row = parse_balance(balance_body(value=None))[0]
        self.assertEqual(row["goodwill"], 0.0)
        self.assertEqual(row["goodwill_diagnostics"]["state"], "explicit_null")
        # 缺席字段仍必须保持未知，不能借修复之名把 unknown 变成 0
        absent = parse_balance(balance_body(goodwill="absent"))[0]
        self.assertIsNone(absent.get("goodwill"))
        self.assertEqual(absent["goodwill_diagnostics"]["state"], "absent")

    def test_end_to_end_null_verified_absent_missing(self):
        null_rows = combine_financial_rows(parse_balance(balance_body(value=None)),
                                           parse_summary(summary_body()))
        result = evaluate_financial_risk(null_rows, "2026-06-12")
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["missing_tags"], [])
        self.assertEqual(result["metrics"]["goodwill_ratio"], 0.0)
        self.assertEqual(result["metrics"]["goodwill_to_assets"], 0.0)
        absent_rows = combine_financial_rows(parse_balance(balance_body(goodwill="absent")),
                                             parse_summary(summary_body()))
        missing = evaluate_financial_risk(absent_rows, "2026-06-12")
        self.assertEqual(missing["status"], "missing")
        self.assertIn("financial_risk_goodwill", missing["missing_tags"])

    def test_threshold_unchanged_real_goodwill_still_fails(self):
        """修复只影响 null；真实高商誉仍必须否决，风控强度不得下降。"""
        for goodwill, expected in [(0, "verified"), (0.0, "verified"), (120.0, "verified"),
                                   (120.01, "failed"), (None, "missing")]:
            with self.subTest(goodwill=goodwill):
                self.assertEqual(
                    evaluate_financial_risk([canonical(goodwill=goodwill)], "2026-06-12")["status"],
                    expected)

    def test_evidence_cache_key_is_versioned_above_v3(self):
        """证据语义变了就必须升版 key，否则旧语义缓存会被继续命中，修复形同虚设。

        v3 把 reported-null 商誉记成 missing；升到 v4 后旧行不会被读取。
        """
        directory = tempfile.mkdtemp()
        config = StrategyConfig()
        config.CACHE_DIR = directory
        config.USE_CACHE = True
        # 决策日必须晚于披露日(2026-04-20)，否则会先被 stale 判定为 missing
        rows = combine_financial_rows(parse_balance(balance_body(value=None)),
                                      parse_summary(summary_body()))
        evidence = evaluate_financial_risk(rows, "2026-06-12")
        self.assertEqual(evidence["status"], "verified")
        self.assertEqual(validated_financial_risk_status(
            dict(evidence, code="600519"), "600519", "2026-06-12"), "verified")

        # 走生产写入路径：证据必须落到 v4 key 上
        with unittest.mock.patch("src.financial_risk.fetch_financial_risk",
                                 return_value=evidence):
            enrich_financial_risk("600519", None, config, None, "2026-06-12")
        written = os.listdir(directory)
        self.assertTrue(any(name.startswith("financial_risk_v4_") for name in written), written)
        self.assertFalse(any(name.startswith("financial_risk_v3_") for name in written), written)

    def test_production_gate_consumes_verified_null_evidence(self):
        """生产判定口径：verified 的 null 证据必须通过校验并给出零商誉比。"""
        result = evaluate_financial_risk(
            combine_financial_rows(parse_balance(balance_body(value=None)),
                                   parse_summary(summary_body())), "2026-06-12")
        self.assertEqual(validated_financial_risk_status(
            dict(result, code="600519"), "600519", "2026-06-12"), "verified")
        config = StrategyConfig()
        ratio = result["metrics"]["goodwill_ratio"]
        self.assertFalse(ratio > config.MAX_GOODWILL_RATIO)


if __name__ == "__main__":
    unittest.main(verbosity=2)