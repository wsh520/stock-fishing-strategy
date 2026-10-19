"""2026-10-19 P1：影子账本（pending/near-miss）记录与汇总测试。

覆盖：幂等写入、重复运行 seen_count、formal 取代标记、valuation_pb 过滤、
NaN 清洗、损坏行跳过、按阻断组合×期限汇总。不发网络请求。
"""
import json
import os
import shutil
import unittest
import uuid

import pandas as pd

from src.screening_engine import StrategyConfig
from src.shadow_ledger import (
    build_shadow_record,
    load_shadow_records,
    record_shadow_pending,
    summarize_shadow_records,
    summarize_tracking,
)

_WORKSPACE_TMP = os.path.join(os.getcwd(), ".test-artifacts")


def _tmpdir() -> str:
    os.makedirs(_WORKSPACE_TMP, exist_ok=True)
    path = os.path.join(_WORKSPACE_TMP, "shadow-" + uuid.uuid4().hex[:10])
    os.makedirs(path, exist_ok=True)
    return path


def _config(tmp: str) -> StrategyConfig:
    config = StrategyConfig()
    config.CACHE_DIR = os.path.join(tmp, "cache")
    config.USE_CACHE = True
    return config


def _row(**overrides) -> dict:
    row = {
        "date": "2026-10-16",
        "code": "600066",
        "name": "宇通客车",
        "tier": "pending",
        "delivery_status": "pending",
        "qualification_status": "blocked",
        "score": 83.61,
        "rank_score": 83.61,
        "daily_score": 56.1,
        "close": 28.71,
        "pe_ttm": 10.0,
        "pb_mrq": 1.2,
        "position_250": 0.2,
        "stabilization_level": "medium",
        "quality_status": "verified",
        "financial_risk_status": "verified",
        "operating_status": "weak",
        "operating_summary": "经营走弱·观察；净利同比-4.6%",
        "missing_tags": "operating_weak",
        "signals_hit": "优质低估低位,止跌中确认",
    }
    row.update(overrides)
    return row


class ShadowLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_is_idempotent_and_counts_repeats(self):
        config = _config(self.tmp)
        data_dir = os.path.join(self.tmp, "data")
        changed, total = record_shadow_pending([_row()], None, config, data_dir=data_dir)
        self.assertEqual((changed, total), (1, 1))
        cache_path = os.path.join(config.CACHE_DIR, "shadow_recommendations.jsonl")
        data_path = os.path.join(data_dir, "shadow_recommendations.jsonl")
        self.assertTrue(os.path.exists(cache_path))
        self.assertTrue(os.path.exists(data_path))

        changed, total = record_shadow_pending([_row()], None, config, data_dir=data_dir)
        self.assertEqual((changed, total), (1, 1))
        records = load_shadow_records(cache_path)
        record = records[("2026-10-16", "600066")]
        self.assertEqual(record["seen_count"], 2)
        self.assertEqual(record["blockers"], ["operating_weak"])
        self.assertTrue(record["is_near_miss"])

    def test_formal_supersedes_pending_record(self):
        config = _config(self.tmp)
        data_dir = os.path.join(self.tmp, "data")
        record_shadow_pending([_row()], None, config, data_dir=data_dir)
        formal = pd.DataFrame([{"code": "600066"}])
        record_shadow_pending([_row()], formal, config, data_dir=data_dir)
        record = load_shadow_records(
            os.path.join(config.CACHE_DIR, "shadow_recommendations.jsonl")
        )[("2026-10-16", "600066")]
        self.assertTrue(record["superseded_by_formal"])

    def test_valuation_pb_is_not_a_blocker_by_default(self):
        config = _config(self.tmp)
        record = build_shadow_record(_row(missing_tags="valuation_pb"), config=config)
        self.assertEqual(record["blockers"], ["pending_reason_unknown"])
        config.QV_VALUATION_DUAL_GUARD = True
        record = build_shadow_record(_row(missing_tags="valuation_pb"), config=config)
        self.assertEqual(record["blockers"], ["valuation_pb"])

    def test_nan_and_missing_fields_are_cleaned(self):
        config = _config(self.tmp)
        record = build_shadow_record(_row(score=float("nan"), close=None, operating_status=None),
                                     config=config)
        self.assertIsNone(record["score"])
        self.assertIsNone(record["close"])
        self.assertIsNone(record["operating_status"])

    def test_float_code_is_normalized(self):
        record = build_shadow_record(_row(code=600066.0), config=_config(self.tmp))
        self.assertEqual(record["code"], "600066")

    def test_load_skips_corrupt_lines(self):
        path = os.path.join(self.tmp, "ledger.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{bad json\n")
            handle.write(json.dumps({"rec_date": "2026-10-16", "code": "600066"}) + "\n")
        records = load_shadow_records(path)
        self.assertEqual(len(records), 1)
        self.assertIn(("2026-10-16", "600066"), records)

    def test_summaries_count_blockers_and_returns(self):
        config = _config(self.tmp)
        data_dir = os.path.join(self.tmp, "data")
        record_shadow_pending([
            _row(),
            _row(code="601965", name="中国汽研", missing_tags="recent_bottom_unconfirmed"),
        ], None, config, data_dir=data_dir)
        summary = summarize_shadow_records(load_shadow_records(
            os.path.join(config.CACHE_DIR, "shadow_recommendations.jsonl")))
        self.assertEqual(summary["records"], 2)
        self.assertEqual(summary["near_miss"], 2)
        self.assertEqual(summary["by_blocker"]["operating_weak"], 1)

        tracking = summarize_tracking([
            {"holding_trade_days": 5, "return_pct": 10.0,
             "blockers": ["operating_weak"], "is_near_miss": True},
            {"holding_trade_days": 5, "return_pct": -5.0,
             "blockers": ["operating_weak"], "is_near_miss": True},
            {"holding_trade_days": 20, "return_pct": 3.0,
             "blockers": ["recent_bottom_unconfirmed"], "is_near_miss": True},
        ])
        row = next(item for item in tracking["by_blockers"]
                   if item["blockers"] == "operating_weak" and item["holding_trade_days"] == 5)
        self.assertEqual(row["n"], 2)
        self.assertEqual(row["mean"], 2.5)
        self.assertEqual(row["median"], 2.5)
        self.assertEqual(row["win_rate"], 0.5)
        self.assertEqual(len(tracking["overall"]), 2)


if __name__ == "__main__":
    unittest.main()
