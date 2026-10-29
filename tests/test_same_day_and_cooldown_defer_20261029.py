"""跨 sleeve 同日硬拦截 + 冷却票名额后置 回归（2026-10-29）

【背景（本周实测）】2026-10-26 ~ 10-28 三个交易日，`stock_recommendation` 只落了
9 行、去重后仅 3 只股票，其中 603013 亚普股份连续三天在 `bottom_fishing` 与
`longterm_quality` 各落一条。两个 sleeve 的执行参数互相矛盾：

    bottom_fishing    权重 0.45/0.30/0.25   止损 atr3x_floor15   止盈 +15%（→17.88）
    longterm_quality  权重 0.55/0.35/0.10   止损 atr4x_floor20   止盈 +50%（→23.33）

用户手工执行时同一只票会收到两个止盈价 —— 这不是信息冗余，是执行歧义。
跨 sleeve 冷却当前是 label 模式（只标注不拦截），因此管不住这件事。

同日另一个现象：10-28 通过 formal 7 只、命中冷却 5 只，熊市名额上限 2，
最终推送的 2 只**全部**来自冷却名单。原因是老票技术分随价格上行走高
（603013 评分 70.8 → 77.4），排序天然压过刚达标的新票 —— 冷却标注变成
「老票自我强化」，名额分不到新面孔。

本文件锁定两项修复的语义：
  1. `_apply_same_day_cross_sleeve_block`：命中即硬拦截、转 pending 且带标签
     （可见可查，名额让给下一名，而不是当日产出归零）
  2. `_defer_cooldown_rows`：冷却标注票整体后置，两组内部各自保持原顺序；
     全部命中/全部未命中时不重排

并锁定「已接线」这一事实（源码级断言）——函数写好但没被调用等于没有风控。

跑法：python tests/test_same_day_and_cooldown_defer_20261029.py
"""
from __future__ import annotations

import inspect
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import store.mysql_store as store
from src import screening_engine as m


def make_frame(rows):
    """构造 _screen_quality_pool 内部使用的 frame（含 tier / missing_tags）。"""
    base = []
    for i, r in enumerate(rows):
        base.append({
            "code": r["code"], "name": r.get("name", r["code"]),
            "tier": r.get("tier", "formal"),
            "score": r.get("score", 80.0 - i),
            "rank_score": r.get("rank_score", r.get("score", 80.0 - i)),
            "close": r.get("close", 10.0),
            "signals_hit": r.get("signals_hit", "低位"),
            "missing_tags": r.get("missing_tags", ""),
        })
    return pd.DataFrame(base)


class SameDayBlockTest(unittest.TestCase):
    def test_hit_is_removed_and_parked_as_pending(self):
        """命中当日已被其他 sleeve 推荐的票：剔除 + 转 pending + 打标签。"""
        frame = make_frame([
            {"code": "603013", "name": "亚普股份", "score": 77.4, "rank_score": 77.9},
            {"code": "002991", "name": "甘源食品", "score": 75.2},
            {"code": "603043", "name": "广州酒家", "score": 74.0},
        ])
        formal = frame.copy()
        pending: list[dict] = []
        excluded: list[dict] = []

        out = m._apply_same_day_cross_sleeve_block(
            formal, frame, {"603013"}, pending, excluded)

        self.assertEqual(list(out["code"]), ["002991", "603043"])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["code"], "603013")
        self.assertEqual(pending[0]["tier"], "pending")
        self.assertEqual(pending[0]["delivery_status"], "same_day_other_sleeve")
        self.assertIn("same_day_other_sleeve", pending[0]["missing_tags"])
        self.assertEqual([r["code"] for r in excluded], ["603013"])
        self.assertEqual(excluded[0]["score"], 77.4)

    def test_no_hit_returns_unchanged(self):
        frame = make_frame([{"code": "002991"}, {"code": "603043"}])
        pending: list[dict] = []
        out = m._apply_same_day_cross_sleeve_block(
            frame.copy(), frame, {"600066"}, pending, None)
        self.assertEqual(len(out), 2)
        self.assertEqual(pending, [])

    def test_empty_input_never_blocks(self):
        """空集合 / None 必须原样返回：未配置 DB 时不能把当日产出清零。"""
        frame = make_frame([{"code": "002991"}, {"code": "603043"}])
        for taken in (None, set(), [], {}, ""):
            out = m._apply_same_day_cross_sleeve_block(
                frame.copy(), frame, taken, [], None)
            self.assertEqual(len(out), 2, msg=f"taken={taken!r} 不应拦截")

    def test_empty_formal_returns_unchanged(self):
        empty = pd.DataFrame([], columns=["code", "name", "tier", "score"])
        out = m._apply_same_day_cross_sleeve_block(empty, empty, {"603013"}, [], None)
        self.assertTrue(out.empty)

    def test_pending_rows_are_not_touched(self):
        """frame 里 tier=pending 的同代码行不该被二次转 pending（只处理 formal）。"""
        frame = make_frame([
            {"code": "603013", "tier": "formal"},
            {"code": "603013", "tier": "pending", "missing_tags": "operating_weak"},
        ])
        formal = frame[frame["tier"] == "formal"].copy()
        pending: list[dict] = []
        m._apply_same_day_cross_sleeve_block(formal, frame, {"603013"}, pending, None)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["tier"], "pending")


class CooldownDeferTest(unittest.TestCase):
    def _formal(self):
        return pd.DataFrame([
            {"code": "603013", "name": "亚普股份", "score": 77.4, "repeat_count": 6},
            {"code": "002991", "name": "甘源食品", "score": 75.2, "repeat_count": None},
            {"code": "603043", "name": "广州酒家", "score": 74.0, "repeat_count": 3},
            {"code": "600066", "name": "宇通客车", "score": 71.0, "repeat_count": None},
        ])

    def test_cooldown_rows_move_to_back_preserving_inner_order(self):
        out = m._defer_cooldown_rows(self._formal())
        # 新票在前且保持原相对顺序；冷却票在后且保持原相对顺序
        self.assertEqual(list(out["code"]), ["002991", "600066", "603013", "603043"])

    def test_index_is_reset(self):
        out = m._defer_cooldown_rows(self._formal())
        self.assertEqual(list(out.index), [0, 1, 2, 3])

    def test_all_cooled_is_no_op(self):
        """全部命中冷却时不重排 —— 重排没有任何效果，只会污染日志。"""
        df = pd.DataFrame([
            {"code": "603013", "score": 77.4, "repeat_count": 6},
            {"code": "603043", "score": 74.0, "repeat_count": 3},
        ])
        out = m._defer_cooldown_rows(df)
        self.assertEqual(list(out["code"]), ["603013", "603043"])

    def test_none_cooled_is_no_op(self):
        df = pd.DataFrame([
            {"code": "002991", "score": 75.2, "repeat_count": None},
            {"code": "600066", "score": 71.0, "repeat_count": None},
        ])
        out = m._defer_cooldown_rows(df)
        self.assertEqual(list(out["code"]), ["002991", "600066"])

    def test_missing_column_is_no_op(self):
        """非 label 模式不会写 repeat_count 列，此时必须原样返回。"""
        df = pd.DataFrame([{"code": "002991", "score": 75.2}])
        out = m._defer_cooldown_rows(df)
        self.assertEqual(list(out["code"]), ["002991"])

    def test_empty_is_no_op(self):
        empty = pd.DataFrame([], columns=["code", "score", "repeat_count"])
        self.assertTrue(m._defer_cooldown_rows(empty).empty)


class SwitchSemanticsTest(unittest.TestCase):
    def test_defaults_are_on(self):
        """两项修复默认开启；回退靠显式置 False，不靠改代码。"""
        cfg = m.StrategyConfig()
        self.assertTrue(cfg.QV_SAME_DAY_CROSS_SLEEVE_BLOCK)
        self.assertTrue(cfg.QV_COOLDOWN_RANK_DEFER)

    def test_pool_accepts_new_params(self):
        sig = inspect.signature(m._screen_quality_pool)
        self.assertIn("same_day_taken", sig.parameters)
        self.assertIn("same_day_excluded_out", sig.parameters)
        self.assertIsNone(sig.parameters["same_day_taken"].default)

    def test_main_accepts_new_params(self):
        """run.py 走 main() 而非直接调 _screen_quality_pool，参数必须能透传。"""
        sig = inspect.signature(m.main)
        self.assertIn("same_day_taken", sig.parameters)
        self.assertIn("same_day_excluded_out", sig.parameters)


class WiredInTest(unittest.TestCase):
    """源码级断言：函数存在但没接线 = 死闸门。"""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "src", "screening_engine.py"), encoding="utf-8") as fh:
            self.engine_src = fh.read()

    def test_pool_calls_same_day_block(self):
        self.assertIn("_apply_same_day_cross_sleeve_block(\n            formal, frame, same_day_taken",
                      self.engine_src)
        self.assertIn("same_day_blocked = _before_same_day - len(formal)", self.engine_src)

    def test_block_runs_after_cooldown_label(self):
        """顺序不变量：先 label 标注（写 repeat_count），再同日剔除。

        反过来会让被剔除的行丢掉冷却标注，pending 里就看不到「第几次」。
        """
        i_cool = self.engine_src.index("formal = _apply_repeat_cooldown(")
        i_same = self.engine_src.index("_apply_same_day_cross_sleeve_block(\n            formal, frame, same_day_taken")
        self.assertLess(i_cool, i_same)

    def test_defer_runs_before_cap_and_after_industry_dedup(self):
        """顺序不变量：行业去重决定谁还活着，名额后置只管谁先占名额。"""
        i_dedup = self.engine_src.index("formal = _dedup_by_industry(")
        i_defer = self.engine_src.index("formal = _defer_cooldown_rows(formal)")
        i_cap = self.engine_src.index("selected = formal.head(cap)")
        self.assertLess(i_dedup, i_defer)
        self.assertLess(i_defer, i_cap)

    def test_cap_cut_is_logged(self):
        """名额截取必须打印被剔除名单——这是全链路唯一没有留痕的决策步骤。"""
        self.assertIn("[QV_CAP_CUT]", self.engine_src)

    def test_switch_off_is_logged(self):
        """死闸门原则：开关关闭 / 拿到空集合都要显式留痕。"""
        self.assertIn("跨 sleeve 同日去重：关闭", self.engine_src)
        self.assertIn("跨 sleeve 同日去重：未生效", self.engine_src)

    def test_zh_label_registered(self):
        """pending 标签必须有中文映射，否则卡片上会露英文 key。"""
        self.assertIn('"same_day_other_sleeve": "资格已通过·当日已被其他策略推荐"',
                      self.engine_src)


class EntriesWiredTest(unittest.TestCase):
    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.root = root

    def _read(self, name):
        with open(os.path.join(self.root, name), encoding="utf-8") as fh:
            return fh.read()

    def test_run_longterm_queries_and_passes(self):
        src = self._read("run_longterm.py")
        self.assertIn("fetch_rec_codes_for_date", src)
        self.assertIn("exclude_strategy=STRATEGY_LONGTERM_QUALITY", src)
        self.assertIn("same_day_taken=same_day_taken", src)

    def test_run_py_queries_and_passes(self):
        src = self._read("run.py")
        self.assertIn("fetch_rec_codes_for_date", src)
        self.assertIn('exclude_strategy="bottom_fishing"', src)
        self.assertIn("same_day_taken=same_day_taken", src)

    def test_no_save_skips_query(self):
        """--no-save 测试运行不得查库，否则会污染判读（不写库却依赖库）。"""
        longterm_src = self._read("run_longterm.py")
        self.assertIn("enabled=not args.no_save", longterm_src)
        run_src = self._read("run.py")
        self.assertIn("enabled=save_enabled", run_src)


class StoreQueryTest(unittest.TestCase):
    def test_exclude_strategy_builds_filtered_sql(self):
        captured = {}

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params):
                captured["sql"] = sql
                captured["params"] = params

            def fetchall(self):
                return [("603013",), ("002991",)]

        class _Conn:
            def cursor(self):
                return _Cur()

            def close(self):
                pass

        with mock.patch.object(store, "is_configured", return_value=True), \
             mock.patch.object(store, "_connect", return_value=_Conn()), \
             mock.patch.object(store, "_ensure_tables", lambda conn: None):
            out = store.fetch_rec_codes_for_date(
                "2026-10-28", exclude_strategy="longterm_quality")

        self.assertEqual(out, {"603013", "002991"})
        self.assertIn("strategy <> %s", captured["sql"])
        self.assertEqual(captured["params"], ("2026-10-28", "longterm_quality"))

    def test_without_exclude_keeps_old_behaviour(self):
        captured = {}

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params):
                captured["sql"] = sql
                captured["params"] = params

            def fetchall(self):
                return []

        class _Conn:
            def cursor(self):
                return _Cur()

            def close(self):
                pass

        with mock.patch.object(store, "is_configured", return_value=True), \
             mock.patch.object(store, "_connect", return_value=_Conn()), \
             mock.patch.object(store, "_ensure_tables", lambda conn: None):
            store.fetch_rec_codes_for_date("2026-10-28")

        self.assertNotIn("strategy <>", captured["sql"])
        self.assertEqual(captured["params"], ("2026-10-28",))

    def test_not_configured_returns_empty(self):
        with mock.patch.object(store, "is_configured", return_value=False):
            self.assertEqual(store.fetch_rec_codes_for_date("2026-10-28"), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
