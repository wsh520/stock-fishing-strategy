"""重复推荐冷却「标注模式」回归（2026-10-26）

【背景】2026-10-13 引入的冷却把命中票从 formal 移入 pending：不推送、不落库。
使用者看到的是「这只票消失了」，既分不清是冷却拦的还是闸门否决的，也无法事后
统计「同一只票第 2、3 次推荐到底赚不赚」。

【用户口径】冷却的票可以重新推荐；新推荐把旧行打「废弃」标签；统计时旧行
**照常计入**、支持按多条分开统计、统计规则不变。

于是冷却拆成三种动作：
  label（默认）保留在 formal + 写 repeat_count/prev_rec_date/prev_rec_close + 加标签
  block        2026-10-13 旧行为（移入 pending，不推送不落库）
  off          不查库不判定

本文件锁定 label 的语义与「统计不排除旧行」这一不变式。

跑法：python tests/test_repeat_cooldown_label_20261026.py
"""
from __future__ import annotations

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import store.mysql_store as store
from src import screening_engine as m

FRAME = pd.DataFrame([
    {"code": "600096", "name": "云天化", "tier": "formal", "score": 84.6,
     "close": 27.56, "signals_hit": "低位,止跌确认"},
    {"code": "002847", "name": "盐津铺子", "tier": "formal", "score": 82.9,
     "close": 41.20, "signals_hit": "低位"},
    {"code": "000538", "name": "云南白药", "tier": "formal", "score": 69.1,
     "close": 55.10, "signals_hit": "低位"},
    {"code": "600519", "name": "贵州茅台", "tier": "pending", "score": 55.0,
     "close": 1500.0, "signals_hit": ""},
])

DETAIL = {
    "600096": {"count": 2, "last_date": "2026-10-23", "last_close": 27.95},
    "002847": {"count": 1, "last_date": "2026-10-22", "last_close": 45.00},
}


def formal_only():
    return FRAME[FRAME["tier"] == "formal"].copy()


class NormalizeTest(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(m._normalize_cooldown_info(None), {})
        self.assertEqual(m._normalize_cooldown_info(set()), {})
        self.assertEqual(m._normalize_cooldown_info({}), {})

    def test_set_degrades_to_count_one(self):
        """旧调用传 set：标签照样能打，只是没有「上次价格」信息。"""
        out = m._normalize_cooldown_info({"600096"})
        self.assertEqual(out, {"600096": {"count": 1, "last_date": None,
                                          "last_close": None}})

    def test_dict_passthrough(self):
        out = m._normalize_cooldown_info(DETAIL)
        self.assertEqual(out["600096"]["count"], 2)
        self.assertEqual(out["600096"]["last_close"], 27.95)

    def test_dict_int_value(self):
        """容忍 {code: 3} 这种极简写法（内部/调试时会出现）。"""
        self.assertEqual(m._normalize_cooldown_info({"600096": 3}),
                         {"600096": {"count": 3}})


class LabelModeTest(unittest.TestCase):
    def test_keeps_every_row(self):
        """label 不改变放行集合：行数必须与输入完全一致。"""
        out = m._apply_repeat_cooldown(formal_only(), FRAME, DETAIL, 10, [], mode="label")
        self.assertEqual(len(out), 3)
        self.assertEqual(list(out["code"]), ["600096", "002847", "000538"])

    def test_writes_repeat_fields(self):
        out = m._apply_repeat_cooldown(formal_only(), FRAME, DETAIL, 10, [], mode="label")
        row = out[out["code"] == "600096"].iloc[0]
        self.assertEqual(row["repeat_count"], 2)
        self.assertEqual(row["prev_rec_date"], "2026-10-23")
        self.assertAlmostEqual(float(row["prev_rec_close"]), 27.95)

    def test_tag_has_sequence_and_price_move(self):
        """标签必须能自解释：第几次 + 上次价 → 本次价 + 涨跌幅。"""
        out = m._apply_repeat_cooldown(formal_only(), FRAME, DETAIL, 10, [], mode="label")
        sig = out[out["code"] == "600096"].iloc[0]["signals_hit"]
        self.assertIn("重复推荐(10日内第3次)", sig)
        self.assertIn("2026-10-23@27.95", sig)
        self.assertIn("27.56", sig)
        self.assertIn("-1.4%", sig)

    def test_existing_signals_preserved(self):
        """加标签不得吞掉原有入选依据（it 是复盘「为什么推了这只」的唯一凭据）。"""
        out = m._apply_repeat_cooldown(formal_only(), FRAME, DETAIL, 10, [], mode="label")
        sig = out[out["code"] == "600096"].iloc[0]["signals_hit"]
        self.assertTrue(sig.startswith("低位,止跌确认"))

    def test_non_hit_rows_untouched(self):
        out = m._apply_repeat_cooldown(formal_only(), FRAME, DETAIL, 10, [], mode="label")
        row = out[out["code"] == "000538"].iloc[0]
        self.assertTrue(pd.isna(row.get("repeat_count")))
        self.assertEqual(row["signals_hit"], "低位")

    def test_set_input_still_labels(self):
        """set 入参（拿不到次数/上次价）也要打标签，count 退化为 1。"""
        out = m._apply_repeat_cooldown(formal_only(), FRAME, {"600096"}, 10,
                                      [], mode="label")
        row = out[out["code"] == "600096"].iloc[0]
        self.assertEqual(row["repeat_count"], 1)
        self.assertIn("第2次", row["signals_hit"])
        self.assertNotIn("上次", row["signals_hit"])

    def test_does_not_touch_pending_out(self):
        """label 模式下冷却不再产生待核验行（否则卡片上会出现重复记录）。"""
        pending: list = []
        m._apply_repeat_cooldown(formal_only(), FRAME, DETAIL, 10, pending, mode="label")
        self.assertEqual(pending, [])

    def test_input_frame_not_mutated(self):
        frame = formal_only()
        m._apply_repeat_cooldown(frame, FRAME, DETAIL, 10, [], mode="label")
        self.assertEqual(len(frame), 3)
        self.assertNotIn("repeat_count", frame.columns)


class BlockModeTest(unittest.TestCase):
    def test_block_still_filters(self):
        """block 保留为可一键回退的旧行为，必须仍能拦截并转出 pending。"""
        pending: list = []
        out = m._apply_repeat_cooldown(formal_only(), FRAME, DETAIL, 10, pending, mode="block")
        self.assertEqual(len(out), 1)
        self.assertEqual(list(out["code"]), ["000538"])
        self.assertEqual({p["code"] for p in pending}, {"600096", "002847"})

    def test_default_arg_is_block_for_compat(self):
        """函数默认值保持 block：既有位置参数调用（2026-10-13 回归）语义不变。"""
        sig = inspect.signature(m._apply_repeat_cooldown)
        self.assertEqual(sig.parameters["mode"].default, "block")

    def test_unknown_mode_falls_back_to_block(self):
        """未知取值必须退到保守方向（少推荐），不得静默变成 label。"""
        pending: list = []
        out = m._apply_repeat_cooldown(formal_only(), FRAME, DETAIL, 10, pending,
                                      mode="labl")
        self.assertEqual(len(out), 1)
        self.assertEqual(len(pending), 2)


class ConfigTest(unittest.TestCase):
    def test_default_mode_is_label(self):
        cfg = m.StrategyConfig()
        self.assertEqual(cfg.QV_REPEAT_COOLDOWN_MODE, "label")
        self.assertEqual(cfg.QV_REPEAT_COOLDOWN_DAYS, 10)

    def test_longterm_inherits_label(self):
        """长期 sleeve 复用同一 config 字段，口径必须一致（避免两套 sleeve 行为分叉）。"""
        from src.quality_longterm_strategy import LongTermQualityConfig
        self.assertEqual(LongTermQualityConfig().QV_REPEAT_COOLDOWN_MODE, "label")


class _FakeCursor:
    """极简游标：只为校验 store 层 SQL 语义，不连库。"""

    def __init__(self, prior=None, ids=None):
        self.prior = prior or {}
        self.ids = ids or []
        self.calls: list[tuple] = []

    def execute(self, sql, params=None):
        self.calls.append(("execute", sql, params))

    def executemany(self, sql, seq):
        seq = list(seq)
        self.calls.append(("executemany", sql, seq))
        return len(seq)

    def fetchall(self):
        sql = self.calls[-1][1]
        if "COUNT(*)" in sql:
            return list(self.prior.items())
        return list(self.ids)


class StoreMarkingTest(unittest.TestCase):
    KEYS = [("2026-10-26", "600096", "bottom_fishing")]

    def test_seq_map_is_next_sequence(self):
        cur = _FakeCursor(prior={"600096": 2})
        self.assertEqual(store._repeat_seq_map(cur, "bottom_fishing", self.KEYS),
                         {"600096": 3})

    def test_seq_map_handles_first_time(self):
        cur = _FakeCursor(prior={})
        self.assertEqual(store._repeat_seq_map(cur, "bottom_fishing", self.KEYS),
                         {"600096": 1})

    def test_mark_writes_seq_and_supersedes_older_only(self):
        cur = _FakeCursor(ids=[(77, "2026-10-26", "600096")])
        marked = store._mark_repeat_sequence(
            cur, "bottom_fishing", self.KEYS, {"600096": 3})
        sqls = [c[1] for c in cur.calls if c[0] == "executemany"]
        self.assertEqual(marked, 1)
        self.assertTrue(any("SET repeat_seq" in s for s in sqls))
        sup = [s for s in sqls if "superseded_by" in s]
        self.assertEqual(len(sup), 1)
        # 只把**更早**的行标为废弃，且不覆盖已有的链路（每行指向其直接后继）
        self.assertIn("rec_date < %s", sup[0])
        self.assertIn("superseded_by IS NULL", sup[0])
        self.assertNotIn("rec_date >=", sup[0])

    def test_mark_tolerates_missing_row(self):
        cur = _FakeCursor(ids=[])
        self.assertEqual(store._mark_repeat_sequence(
            cur, "bottom_fishing", self.KEYS, {"600096": 1}), 0)


class StatisticsNotExcludedTest(unittest.TestCase):
    """核心不变式：统计/追踪/冷却都不得把 superseded 的旧行排除。

    用户明确要求「统计的时候还是正常统计，支持统计多条，分开统计，统计规则
    还是保持原来的」。一旦某个读路径加了 `WHERE superseded_by IS NULL`，
    旧推荐就会从样本里消失，等于把「第 1 次推荐」的亏损抹掉。
    """

    READ_PATHS = ("get_active_recommendations", "get_attribution_rows",
                  "fetch_recent_recommended_codes",
                  "fetch_recent_recommendation_detail")

    def test_read_paths_do_not_filter_superseded(self):
        for name in self.READ_PATHS:
            src = inspect.getsource(getattr(store, name))
            # 只禁止**过滤性**用法；文档里提到该列名（解释为什么不排除）是允许的
            for bad in ("superseded_by IS NULL", "superseded_by IS NOT NULL",
                        "superseded_by = %s", "WHERE superseded_by"):
                self.assertNotIn(bad, src,
                                 f"{name} 不得按 superseded_by 过滤（统计必须照常计入旧行）")

    def test_marking_columns_exist_in_schema(self):
        for col in ("repeat_seq", "superseded_by"):
            self.assertIn(col, store._RECOMMENDATION_ALTERS,
                          f"存量库需要幂等补列 {col}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
