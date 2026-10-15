"""Offline integration tests for diagnostic-only QV changes and financial exclusion."""
import logging
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import pandas as pd

from src import screening_engine as m
from src.qv_diagnostics import GateAudit, gate_mode, log_gate_audit, mark_gate
from test_quality_recommendations import DAY, fundamentals, prices


class QVDiagnosticIntegrationTests(unittest.TestCase):
    def config(self, **kwargs):
        values = dict(USE_CACHE=False, REQUIRE_RECENT_OPERATING=False,
                      REQUIRE_FINANCIAL_RISK=False, REQUIRE_HISTORICAL_VALUATION=False,
                      REQUIRE_CYCLICAL_NORMALIZED_VALUATION=False,
                      USE_INDUSTRY_RELATIVE_VALUATION=False, USE_INDUSTRY_DEDUP=False,
                      MIN_TECHNICAL_SCORE_FORMAL=0)
        values.update(kwargs)
        return m.StrategyConfig(**values)

    def signal(self):
        signal, reason = m.evaluate_quality_value(prices(), "600001", "工业企业", self.config(),
                                                  {"regime": "bull"}, fundamentals(), latest_trade_date=DAY)
        self.assertEqual(reason, "PASS")
        return signal

    def test_cooldown_preserves_qualification_but_changes_delivery_tier(self):
        frame = pd.DataFrame([dict(code="600001", name="工业企业", tier="formal", missing_tags="valuation_pb"),
                              dict(code="600002", name="另一企业", tier="formal", missing_tags="")])
        original = frame.copy(deep=True)
        pending = []
        result = m._apply_repeat_cooldown(frame.copy(), frame, {"600001"}, 10, pending)
        self.assertEqual(list(result.code), ["600002"])
        self.assertEqual(pending[0]["tier"], "pending")
        self.assertEqual(pending[0]["qualification_status"], "passed")
        self.assertEqual(pending[0]["delivery_status"], "cooldown")
        self.assertEqual(pending[0]["missing_tags"], "valuation_pb,repeat_cooldown")
        pd.testing.assert_frame_equal(frame, original)
        self.assertIn("重复推荐冷却", m._missing_tags_zh(pending[0]["missing_tags"]))

    def test_funnel_distinguishes_qualification_cooldown_and_pending(self):
        signal = self.signal()
        passed = replace(signal, code="600002", tier="formal", missing_tags="")
        blocked = replace(signal, missing_tags="operating_weak", tier="pending")
        pending = []
        with patch.object(m, "get_stock_list", return_value=[{"code": s.code, "name": s.name} for s in (passed, blocked)]), \
             patch.object(m, "run_concurrent_screen", return_value=([(passed, "PASS"), (blocked, "PASS")], 2, False)), \
             patch.object(m, "get_daily_data", side_effect=AssertionError("offline")), \
             self.assertLogs(m.logger, level="INFO") as logs:
            result = m._screen_quality_pool(self.config(), m.CacheManager(), {}, DAY, pending,
                                            recently_recommended={"600002"})
        text = "\n".join(logs.output)
        self.assertTrue(result.empty)
        self.assertEqual(len(pending), 2)
        self.assertTrue(all(row["tier"] == "pending" for row in pending))
        for fragment in ("资格通过 1", "冷却转出 1", "普通待核验 1", "观察合计 2",
                         "最终可推送=0", "[QV_COOLDOWN]", "[QV_PENDING_TECH]", "不含冷却转出"):
            self.assertIn(fragment, text)
        self.assertNotIn("technical_diagnostics", pending[0])

    def test_financial_names_and_whitelist_are_excluded_before_screening(self):
        stocks = [{"code": "600015", "name": "华夏银行"}, {"code": "601318", "name": "中国平安"},
                  {"code": "600001", "name": "工业企业"}]
        observed = []
        def screen(pool, callback, config, log):
            observed.extend(pool)
            return [], len(pool), False
        with patch.object(m, "get_stock_list", return_value=stocks), \
             patch.object(m, "run_concurrent_screen", side_effect=screen), \
             patch.object(m, "get_daily_data", side_effect=AssertionError("no data calls")), \
             self.assertLogs(m.logger, level="INFO") as logs:
            m._screen_quality_pool(self.config(), m.CacheManager(), {}, DAY)
        self.assertEqual(observed, stocks[2:])
        self.assertIn("金融排除=2，实际筛选=1", "\n".join(logs.output))

    def test_custom_strategy_universe_is_unchanged(self):
        stocks = [{"code": "600015", "name": "华夏银行"}]
        with patch.object(m, "get_stock_list", return_value=stocks), \
             patch.object(m, "run_concurrent_screen", return_value=([], 1, False)) as screen:
            m._screen_quality_pool(self.config(), m.CacheManager(), {}, DAY,
                                   evaluator=lambda *a, **kw: (None, "FAIL_DATA"))
        self.assertEqual(screen.call_args.args[0], stocks)

    def test_gate_telemetry_does_not_change_signal(self):
        config = self.config(REQUIRE_FORWARD_CONFIRMATION=False)
        args = (prices(), "600001", "工业企业", config, {}, fundamentals())
        baseline, reason = m.evaluate_quality_value(*args, latest_trade_date=DAY)
        audit = GateAudit()
        with audit.stock():
            actual, actual_reason = m.evaluate_quality_value(*args, latest_trade_date=DAY)
            # A second pass is counted once per stock.
            m.evaluate_quality_value(*args, latest_trade_date=DAY)
        self.assertEqual((actual.to_dict(), actual_reason), (baseline.to_dict(), reason))
        completed, entered = audit.snapshot()
        self.assertEqual(completed, 1)
        self.assertEqual(entered["FAIL_VALUATION"], 1)
        self.assertEqual(entered["FAIL_STABILIZATION"], 1)
        self.assertNotIn("FAIL_FORWARD", entered)
        self.assertIsNotNone(actual.technical_diagnostics["ma20_confirmed"])
        self.assertNotIn("technical_diagnostics", actual.to_dict())

    def test_gate_early_return_and_disabled_states(self):
        config = self.config()
        audit = GateAudit()
        with audit.stock():
            self.assertEqual(m.evaluate_quality_value(None, "600001", "工业企业", config)[1], "FAIL_DATA")
        self.assertEqual(audit.snapshot(), (1, {"FAIL_DATA": 1}))
        self.assertEqual(gate_mode("FAIL_QV_SCORE", config), "未启用")
        self.assertEqual(gate_mode("FAIL_ROE_UNSTABLE", config), "本策略不适用")
        self.assertIn("非选股闸门", gate_mode("ERROR", config))
        with self.assertLogs(m.logger, level="INFO") as logs:
            log_gate_audit(m.logger, config, ("FAIL_DATA", "FAIL_POSITION", "FAIL_QV_SCORE", "ERROR"),
                           {"FAIL_DATA": 1}, audit)
        text = "\n".join(logs.output)
        self.assertIn("未进入（前置或适用条件阻断）", text)
        self.assertIn("首因否决数=1", text)
        self.assertNotIn("死闸门", text)

    def test_operating_cached_evidence_gains_profile_without_refetch(self):
        from unittest.mock import Mock
        old = dict(code="600001", as_of=DAY, status="weak", reasons=["revenue_declining"],
                   missing_tags=[], metrics={})
        cache = Mock()
        cache.get.return_value = old
        with patch("src.recent_operating.fetch_operating_trend", side_effect=AssertionError("no fetch")):
            result = m.enrich_recent_operating("600001", {}, self.config(REQUIRE_RECENT_OPERATING=True), cache, DAY)
        self.assertEqual(result["operating_trend"]["signal_profile"]["level"], "single_fluctuation")
        self.assertEqual(result["operating_trend"]["status"], old["status"])
        self.assertNotIn("signal_profile", old)

    def test_old_risk_cache_is_not_claimed_to_have_raw_diagnostics(self):
        cfg = self.config(REQUIRE_FINANCIAL_RISK=True)
        cache = m.CacheManager()
        suffix = f"600001_{DAY}_{cfg.MAX_DEBT_RATIO}_{cfg.MAX_GOODWILL_RATIO}_{cfg.MIN_DEDUCTED_PROFIT_RATIO}"
        cache.set("financial_risk_v2_" + suffix, {"status": "verified", "old": True})
        fresh = {"status": "verified", "goodwill_diagnostics": {"state": "explicit_zero"}}
        with patch("src.financial_risk.fetch_financial_risk", return_value=fresh) as fetch:
            result = m.enrich_financial_risk("600001", {}, cfg, cache, DAY)
            self.assertEqual(result["financial_risk"], fresh)
            m.enrich_financial_risk("600001", {}, cfg, cache, DAY)
        fetch.assert_called_once()

    def test_audit_is_thread_local_and_resets_on_exception(self):
        audit = GateAudit()
        def work(code):
            with audit.stock():
                mark_gate(code)
                mark_gate(code)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(work, ["FAIL_DATA", "FAIL_POSITION"]))
        with self.assertRaises(ValueError):
            with audit.stock():
                mark_gate("FAIL_DATA")
                raise ValueError("test")
        mark_gate("outside")
        self.assertEqual(audit.snapshot(), (3, {"FAIL_DATA": 2, "FAIL_POSITION": 1}))


if __name__ == "__main__":
    unittest.main()
