"""2026-10-19 P0：行业估值快照持久化与复用。

背景：run #108/#109 的冷启动快照耗时 150.1/225.6 分钟，而 60 分钟预算会让
定时运行在 60 分钟处放弃快照、回退绝对 PE。修复分两层：
1) 预算默认值提高到 300 分钟，覆盖已验证的最坏情况；
2) 成功构建的快照写入 cache/，在 SNAPSHOT_CACHE_MAX_AGE_DAYS 内直接复用，
   把冷启动成本摊薄到一次。
本测试只验证 2) 的读写、过期、行业分类变化和关闭缓存；不发网络请求。
"""
import json
import os
import shutil
import unittest
import uuid
from datetime import date, timedelta
from unittest.mock import Mock, patch

import pandas as pd

import src.screening_engine as m
from src.screening_engine import StrategyConfig

# 【必须是相对日期，不能硬编码】本文件断言的是「SNAPSHOT_CACHE_MAX_AGE_DAYS=7 内
# 命中缓存」。原值为固定字符串 "2026-10-16"，于是 2026-10-24 起快照必然过期、
# 缓存永不命中，test_successful_build_is_persisted_and_reused 每天假失败——
# 「代码回归」和「日期流逝」被混成同一个信号。改成「昨天」后恒落在窗口内。
DAY = (date.today() - timedelta(days=1)).isoformat()
_WORKSPACE_TMP = os.path.join(os.getcwd(), ".test-artifacts")


def _stocks(count=4):
    return [{"code": f"60000{i}", "name": f"股票{i}"} for i in range(1, count + 1)]


def _industry_map(count=4):
    return {f"60000{i}": "行业A" for i in range(1, count + 1)}


def _index_frame():
    return pd.DataFrame([{"date": f"{DAY} 15:00:00"}])


def _daily_frame(pe=12.0, pb=1.5):
    return pd.DataFrame([{"date": f"{DAY} 15:00:00", "peTTM": pe, "pbMRQ": pb}])


def _config(tmpdir, *, max_age=7.0, use_cache=True):
    cfg = StrategyConfig()
    cfg.CACHE_DIR = tmpdir
    cfg.USE_CACHE = use_cache
    cfg.SNAPSHOT_TIME_BUDGET_MIN = 60.0
    cfg.SNAPSHOT_CACHE_MAX_AGE_DAYS = max_age
    cfg.MAX_WORKERS = 2
    return cfg


class SnapshotPersistenceTests(unittest.TestCase):
    def setUp(self):
        os.makedirs(_WORKSPACE_TMP, exist_ok=True)
        # Windows 下 tempfile.mkdtemp 创建的目录继承的 ACL 不允许当前进程写入；
        # 用 os.makedirs 建在 .test-artifacts/ 下（与生产 cache/ 同属可写工作区），
        # 清理失败也不影响测试结论。
        self.tmp = os.path.join(_WORKSPACE_TMP, "snapshot-" + uuid.uuid4().hex[:10])
        os.makedirs(self.tmp, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, cfg, getter):
        with patch.object(m, "get_stock_industry", return_value=_industry_map()), \
             patch.object(m, "get_index_daily", return_value=_index_frame()), \
             patch.object(m, "get_daily_data", Mock(side_effect=getter)):
            return m.build_industry_valuation_snapshot(cfg, object(), _stocks())

    def test_successful_build_is_persisted_and_reused(self):
        cfg = _config(self.tmp)
        calls: list[str] = []

        def getter(code, config, cache):
            calls.append(code)
            return _daily_frame()

        first = self._run(cfg, getter)
        self.assertEqual(len(calls), 4)
        self.assertIn("行业A", first)
        path = os.path.join(self.tmp, m._SNAPSHOT_CACHE_FILE)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["snapshot_day"], DAY)
        self.assertEqual(payload["industry_count"], 1)

        calls.clear()
        second = self._run(cfg, getter)
        self.assertEqual(calls, [], "命中缓存后不得再逐股拉取")
        self.assertEqual(list(second["行业A"]["pe"]), [12.0, 12.0, 12.0, 12.0])
        self.assertEqual(len(second["行业A"]["comparable_peers"]), 4)

    def test_expired_cache_is_rebuilt(self):
        cfg = _config(self.tmp, max_age=1.0)
        calls: list[str] = []

        def getter(code, config, cache):
            calls.append(code)
            return _daily_frame()

        self._run(cfg, getter)
        path = os.path.join(self.tmp, m._SNAPSHOT_CACHE_FILE)
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["snapshot_day"] = "2026-01-01"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        calls.clear()
        self._run(cfg, getter)
        self.assertEqual(len(calls), 4, "过期缓存必须重建")

    def test_industry_map_change_invalidates_cache(self):
        cfg = _config(self.tmp)
        calls: list[str] = []

        def getter(code, config, cache):
            calls.append(code)
            return _daily_frame()

        self._run(cfg, getter)
        changed = _industry_map()
        changed["600001"] = "行业B"
        calls.clear()
        with patch.object(m, "get_stock_industry", return_value=changed), \
             patch.object(m, "get_index_daily", return_value=_index_frame()), \
             patch.object(m, "get_daily_data", Mock(side_effect=getter)):
            snapshot = m.build_industry_valuation_snapshot(cfg, object(), _stocks())
        self.assertEqual(len(calls), 4, "行业分类变化必须重建")
        self.assertIn("行业B", snapshot)

    def test_zero_max_age_disables_reuse(self):
        cfg = _config(self.tmp, max_age=0.0)
        calls: list[str] = []

        def getter(code, config, cache):
            calls.append(code)
            return _daily_frame()

        self._run(cfg, getter)
        calls.clear()
        self._run(cfg, getter)
        self.assertEqual(len(calls), 4)

    def test_no_cache_manager_does_not_touch_disk(self):
        cfg = _config(self.tmp)
        calls: list[str] = []

        def getter(code, config, cache):
            calls.append(code)
            return _daily_frame()

        with patch.object(m, "get_stock_industry", return_value=_industry_map()), \
             patch.object(m, "get_index_daily", return_value=_index_frame()), \
             patch.object(m, "get_daily_data", Mock(side_effect=getter)):
            m.build_industry_valuation_snapshot(cfg, None, _stocks())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, m._SNAPSHOT_CACHE_FILE)))

    def test_corrupt_cache_is_ignored_and_rebuilt(self):
        cfg = _config(self.tmp)
        path = os.path.join(self.tmp, m._SNAPSHOT_CACHE_FILE)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not-json")
        calls: list[str] = []

        def getter(code, config, cache):
            calls.append(code)
            return _daily_frame()

        snapshot = self._run(cfg, getter)
        self.assertEqual(len(calls), 4)
        self.assertIn("行业A", snapshot)

    def test_default_budget_covers_observed_worst_case(self):
        self.assertGreaterEqual(StrategyConfig().SNAPSHOT_TIME_BUDGET_MIN, 240.0)

    def test_invalid_cache_age_values_are_rejected(self):
        for bad in (-1.0, float("nan")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                StrategyConfig(SNAPSHOT_CACHE_MAX_AGE_DAYS=bad)


if __name__ == "__main__":
    unittest.main()
