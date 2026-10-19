"""行业估值快照时间预算（2026-10-17，run #109 实证；2026-10-19 调整）。

背景：run #109 行业估值快照 225.6 分钟，占 job 360 分钟硬上限的 63%，而并发筛选
本身只要 1.8 分钟；快照初期预估「剩余 244.9 分钟」——Baostock 再慢约 60%，整个
job 会被 6 小时上限杀掉，两套策略全部丢失。

口径：超过 SNAPSHOT_TIME_BUDGET_MIN 即停止拉取剩余股票并**放弃本次快照**
（返回空 dict → 上层全市场回退 PE 绝对阈值，已有显式 warning 日志），而不是
将就使用部分覆盖的横截面——行业分位建立在全行业样本上，截断样本会让分位静默
偏移（宁缺毋滥）。已拉取的日线仍在 cache/ 内，筛选阶段直接复用。

2026-10-19 调整：60 分钟在 #108/#109 的 150/225 分钟冷启动面前必然中途放弃，
等于静默切换估值口径；默认预算提高到 300 分钟，并通过
cache/industry_valuation_snapshot_v1.json 在 7 天内复用快照。本文件只测预算
语义（0 关闭、超时丢弃、非法值拒绝），持久化见 test_snapshot_persistence.py。
"""
import time
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from src.screening_engine import StrategyConfig
import src.screening_engine as m

DAY = "2026-10-16"


def _config(budget_min, workers=2):
    cfg = StrategyConfig()
    cfg.SNAPSHOT_TIME_BUDGET_MIN = budget_min
    cfg.MAX_WORKERS = workers
    return cfg


def _index_frame():
    return pd.DataFrame([{"date": f"{DAY} 15:00:00"}])


def _daily_frame():
    return pd.DataFrame([{"date": f"{DAY} 15:00:00", "peTTM": 12.0, "pbMRQ": 1.5}])


def _slow_getter(delay, counter):
    def _get(code, config, cache):
        counter.append(code)
        time.sleep(delay)
        return _daily_frame()
    return _get


class SnapshotTimeBudgetTests(unittest.TestCase):
    def _stocks(self, count):
        return [{"code": f"60000{i}", "name": f"股票{i}"} for i in range(1, count + 1)]

    def _industry_map(self, count):
        return {f"60000{i}": "行业A" for i in range(1, count + 1)}

    def _run(self, budget_min, count, delay, workers=2):
        calls: list[str] = []
        cfg = _config(budget_min, workers)
        with patch.object(m, "get_stock_industry", return_value=self._industry_map(count)), \
             patch.object(m, "get_index_daily", return_value=_index_frame()), \
             patch.object(m, "get_daily_data", Mock(side_effect=_slow_getter(delay, calls))):
            snapshot = m.build_industry_valuation_snapshot(cfg, None, self._stocks(count))
        return snapshot, calls

    def test_budget_exhaustion_discards_partial_snapshot(self):
        """超预算 → 返回空（回退绝对阈值），且剩余股票不再拉取。"""
        snapshot, calls = self._run(budget_min=0.01, count=20, delay=0.2)  # 预算 0.6s
        self.assertEqual(snapshot, {})
        self.assertGreater(len(calls), 0)
        self.assertLess(len(calls), 20)

    def test_generous_budget_builds_full_snapshot(self):
        snapshot, calls = self._run(budget_min=60.0, count=6, delay=0.01)
        self.assertEqual(len(calls), 6)
        self.assertIn("行业A", snapshot)
        self.assertEqual(len(snapshot["行业A"]["pe"]), 6)

    def test_zero_budget_disables_the_limit(self):
        """<=0 表示不设预算（不推荐但合法），快照照常完整构建。"""
        snapshot, calls = self._run(budget_min=0.0, count=6, delay=0.01)
        self.assertEqual(len(calls), 6)
        self.assertIn("行业A", snapshot)

    def test_invalid_budget_values_are_rejected(self):
        for bad in (-1.0, float("nan")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                StrategyConfig(SNAPSHOT_TIME_BUDGET_MIN=bad)


if __name__ == "__main__":
    unittest.main()
