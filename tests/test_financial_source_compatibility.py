"""Regression for observed Sina fzb/gjzb provenance labels (no network)."""
import copy
import unittest
from unittest.mock import Mock, patch

from src.financial_risk import (
    combine_financial_rows, evaluate_financial_risk, fetch_financial_risk,
    parse_balance, validated_financial_risk_status,
)
from src.recent_operating import SOURCE_URL, parse_summary
from tests.test_financial_evidence_upgrades import payload, response


class FinancialSourceCompatibilityTests(unittest.TestCase):
    def reports(self, published="20260825", balance_values=None, summary_values=None):
        return (
            payload("20260630", published, source="定期报告", balance=True, values=balance_values),
            payload("20260630", published, source="其他", values=summary_values),
        )

    def test_observed_candidates_recover_through_fetch_and_validation(self):
        # Selected fields observed from Sina on 2026-10-09; full payload not needed.
        samples = (
            ("603345", "20260825", 20616295243.17, 4366860697.88, 698062114.66,
             828795503.45, 752977900.06),
            ("605338", "20260821", 2727438326.43, 517013717.54, 96925303.97,
             136185474.86, 105821957.45),
        )
        for code, published, assets, liabilities, goodwill, profit, deducted in samples:
            with self.subTest(code=code):
                balance, summary = self.reports(
                    published, {"资产总计": assets, "负债合计": liabilities, "商誉": goodwill},
                    {"NETPROFIT": profit, "NPCUT": deducted})
                getter = Mock(side_effect=[response(balance), response(summary)])
                result = fetch_financial_risk(code, "2026-10-08", request_get=getter)
                self.assertEqual(result["status"], "verified")
                self.assertEqual(validated_financial_risk_status(result, code, "2026-10-08"), "verified")
                self.assertAlmostEqual(result["metrics"]["deducted_profit_ratio"], deducted / profit)
                self.assertEqual(result["missing_tags"], [])
                audit = result["merge_diagnostics"]
                self.assertEqual(audit["origin_match"], "sina_periodic_summary")
                self.assertEqual(audit["balance"]["data_source"], "定期报告")
                self.assertEqual(audit["summary_candidates"][0]["data_source"], "其他")
                self.assertTrue(all(c.kwargs["params"]["paperCode"] == "sh" + code
                                    for c in getter.call_args_list))
                self.assertEqual(getter.call_count, 2)

    def test_alias_keeps_identity_disclosure_basis_and_origin_constraints(self):
        b, s = self.reports()
        balance, summary = parse_balance(b), parse_summary(s)
        for key, value in (("report_date", "20260331"), ("available_date", "20260826"),
                           ("available_date", None), ("code", "600999"),
                           ("currency", "USD"), ("statement_basis", "parent"),
                           ("source", "another-provider"), ("report_source", "lrb"),
                           ("data_source", "业绩快报"), ("data_source", None)):
            with self.subTest(key=key, value=value):
                rows = combine_financial_rows(balance, [dict(summary[0], **{key: value})])
                self.assertEqual(evaluate_financial_risk(rows, "2026-10-08")["status"], "missing")
                self.assertEqual(rows[0]["merge_diagnostics"]["status"], "unmatched")
        # Matching labels from an unrelated endpoint must not gain Sina's exception.
        other_b = [dict(balance[0], source="another-provider")]
        other_s = [dict(summary[0], source="another-provider")]
        self.assertEqual(evaluate_financial_risk(combine_financial_rows(other_b, other_s),
                                               "2026-10-08")["status"], "missing")
        reverse_b = [dict(balance[0], data_source="其他")]
        reverse_s = [dict(summary[0], data_source="定期报告")]
        self.assertEqual(evaluate_financial_risk(combine_financial_rows(reverse_b, reverse_s),
                                               "2026-10-08")["status"], "missing")

    def test_duplicate_match_is_not_arbitrarily_selected(self):
        b, s = self.reports()
        balance, summary = parse_balance(b), parse_summary(s)
        rows = combine_financial_rows(balance, summary + copy.deepcopy(summary))
        self.assertEqual(rows[0]["merge_diagnostics"]["status"], "ambiguous")
        self.assertEqual(evaluate_financial_risk(rows, "2026-10-08")["status"], "missing")
        # Even exact label matching does not override another compatible report.
        rows = combine_financial_rows(balance, summary + [dict(summary[0], data_source="定期报告")])
        self.assertEqual(rows[0]["merge_diagnostics"]["matching_reports"], 2)
        self.assertNotIn("net_profit", rows[0])

    def test_missing_goodwill_and_known_violations_still_block(self):
        for values, expected, tag in (({"商誉": None}, "missing", "financial_risk_goodwill"),
                                      ({"负债合计": 800}, "failed", "debt_ratio_outside_limit")):
            b, s = self.reports(balance_values=values)
            result = fetch_financial_risk("603345", "2026-10-08",
                                         request_get=Mock(side_effect=[response(b), response(s)]))
            self.assertEqual(result["status"], expected)
            self.assertIn(tag, result["missing_tags"] + result["reasons"])
            self.assertEqual(result["metrics"]["net_profit"], 100)
        b, s = self.reports(summary_values={"NPCUT": 40})
        result = fetch_financial_risk("603345", "2026-10-08",
                                     request_get=Mock(side_effect=[response(b), response(s)]))
        self.assertEqual(result["status"], "failed")
        self.assertIn("deducted_profit_ratio_outside_limit", result["reasons"])

    def test_future_disclosure_stays_unavailable_and_inputs_unchanged(self):
        b, s = self.reports(published="20261009")
        balance, summary = parse_balance(b), parse_summary(s)
        original = copy.deepcopy((balance, summary))
        result = evaluate_financial_risk(combine_financial_rows(balance, summary), "2026-10-08")
        self.assertEqual(result["status"], "missing")
        self.assertEqual((balance, summary), original)

    def test_repaired_evidence_unlocks_only_the_financial_blocker(self):
        from src import bottom_fishing_strategy as m
        from test_quality_recommendations import prices, fundamentals, DAY
        cfg = m.StrategyConfig(USE_CACHE=False, REQUIRE_RECENT_OPERATING=False,
                               MIN_TECHNICAL_SCORE_FORMAL=0, QV_LOW_ANCHOR_MODE="legacy")
        fund = fundamentals()
        context = {"mode": "absolute", "industry": "C39计算机、通信和其他电子设备制造业"}
        args = (prices(), "600001", "测试企业", cfg, {"regime": "bull"})
        before, reason = m.evaluate(*args, fund, latest_trade_date=DAY, val_context=context)
        self.assertEqual((reason, before.tier, before.missing_tags), ("PASS", "pending", "financial_risk"))
        getter = Mock(side_effect=[
            response(payload("20250331", "20250420", source="定期报告", balance=True)),
            response(payload("20250331", "20250420", source="其他")),
        ])
        fund["financial_risk"] = fetch_financial_risk("600001", DAY, request_get=getter)
        after, reason = m.evaluate(*args, fund, latest_trade_date=DAY, val_context=context)
        self.assertEqual((reason, after.tier, after.missing_tags), ("PASS", "formal", ""))
        self.assertEqual(after.daily_score, before.daily_score)
        # A different pending condition remains effective after the merge repair.
        cfg.MIN_TECHNICAL_SCORE_FORMAL = 101
        blocked, reason = m.evaluate(*args, fund, latest_trade_date=DAY, val_context=context)
        self.assertEqual((reason, blocked.tier, blocked.missing_tags), ("PASS", "pending", "technical_weak"))

    def test_risk_cache_version_bypasses_old_evidence(self):
        from src import bottom_fishing_strategy as m
        cfg = m.StrategyConfig(USE_CACHE=False)
        cache = m.CacheManager()
        suffix = f"603345_2026-10-08_{cfg.MAX_DEBT_RATIO}_{cfg.MAX_GOODWILL_RATIO}_{cfg.MIN_DEDUCTED_PROFIT_RATIO}"
        cache.set("financial_risk_v1_" + suffix, {"status": "missing", "old": True})
        fresh = {"status": "verified", "code": "603345"}
        with patch("src.financial_risk.fetch_financial_risk", return_value=fresh) as fetch:
            result = m.enrich_financial_risk("603345", {}, cfg, cache, "2026-10-08")
            self.assertEqual(result["financial_risk"], fresh)
            fetch.assert_called_once()
            m.enrich_financial_risk("603345", {}, cfg, cache, "2026-10-08")
            fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
