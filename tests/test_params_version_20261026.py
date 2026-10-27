"""落库口径快照回归（2026-10-26）

问题：`score` 与 `stop_loss` 两列在不同日期代表不同规则，事后回看无法判别。
  · score 权重：09-28 前 0.50/0.35/0.15 → 之后 0.45/0.30/0.25
  · stop_loss：10-07 前纯 3×ATR → 之后 max(3×ATR, 15%)
实测危害：云天化 09-24 落库 SL=26.05（-5.5% 旧口径），10-22 收 26.03 → 旧口径
已止损 / 新口径（24.01）未止损，同一字段两套规则给出相反结论。

跑法：python tests/test_params_version_20261026.py
"""
from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.quality_longterm_strategy import LongTermQualityConfig
from src.screening_engine import StrategyConfig, params_fingerprint
from store import mysql_store

STORE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "store", "mysql_store.py")


class ParamsVersionTest(unittest.TestCase):
    def test_production_fingerprint(self):
        """生产口径：权重 0.45/0.30/0.25，止损 max(3×ATR, 15%)。"""
        fp = params_fingerprint(StrategyConfig())
        self.assertEqual(fp["score_weights"], "0.45/0.30/0.25")
        self.assertEqual(fp["stop_rule"], "atr3x_floor15")

    def test_longterm_fingerprint_differs_from_production(self):
        """长期 sleeve 是另一套口径：权重 0.55/0.35/0.10、止损 4×ATR / 20% 下限。

        这正是「同一列不同口径」的实例，而且同一天同一只票会同时出现两套值：
        亚普股份 10-22 —— longterm 落库 SL=12.24/TP=22.95（-20%/+50%），
        bottom_fishing 落库 SL=13.01/TP=17.59（-15%/+15%）。
        只按 score 或 stop_loss 的数值对比两个 sleeve，会得出完全错误的结论。
        """
        fp = params_fingerprint(LongTermQualityConfig())
        self.assertEqual(fp["score_weights"], "0.55/0.35/0.10")
        self.assertEqual(fp["stop_rule"], "atr4x_floor20")
        prod = params_fingerprint(StrategyConfig())
        self.assertNotEqual(fp["score_weights"], prod["score_weights"])
        self.assertNotEqual(fp["stop_rule"], prod["stop_rule"])

    def test_fingerprint_follows_config_overrides(self):
        """改权重/关 ATR 止损必须体现在快照里，否则快照等于假的。"""
        cfg = StrategyConfig()
        cfg.QUALITY_SCORE_WEIGHT, cfg.VALUATION_SCORE_WEIGHT, cfg.TECHNICAL_SCORE_WEIGHT = 0.50, 0.35, 0.15
        cfg.USE_ATR_STOP = False
        fp = params_fingerprint(cfg)
        self.assertEqual(fp["score_weights"], "0.50/0.35/0.15")
        self.assertEqual(fp["stop_rule"], "fixed15")

    def test_fingerprint_survives_broken_config(self):
        """配置取不到时置 None，不得抛错阻断落库。"""
        class Broken:
            QUALITY_SCORE_WEIGHT = object()
        fp = params_fingerprint(Broken())
        self.assertIsNone(fp["score_weights"])
        self.assertIsNone(fp["stop_rule"])

    def test_ddl_and_alters_declare_new_columns(self):
        """新列必须同时出现在建表 DDL 与存量表补列里，否则老库永远没有这两列。"""
        for col in ("score_weights", "stop_rule"):
            self.assertIn(col, mysql_store._RECOMMENDATION_ALTERS,
                          f"{col} 缺 _RECOMMENDATION_ALTERS 补列语句")
            self.assertIn(f"ADD COLUMN {col}", mysql_store._RECOMMENDATION_ALTERS[col])

    def test_opt_text_does_not_persist_nan(self):
        """缺失值必须写成 NULL，不能落成字面量 'nan'（否则事后查库会被当成口径名）。"""
        import math
        self.assertIsNone(mysql_store._opt_text(None))
        self.assertIsNone(mysql_store._opt_text(math.nan))
        self.assertIsNone(mysql_store._opt_text(""))
        self.assertIsNone(mysql_store._opt_text("  "))
        self.assertIsNone(mysql_store._opt_text("None"))
        self.assertIsNone(mysql_store._opt_text("nan"))
        self.assertEqual(mysql_store._opt_text(" 0.45/0.30/0.25 "), "0.45/0.30/0.25")

    def test_insert_columns_match_placeholders(self):
        """INSERT 的列数与 %s 占位符数必须一致（经典 1136 列数不匹配生产事故）。

        2026-10-27：INSERT 由源码字面量改为 _build_insert_sql() 按实际列动态生成
        （补列 ALTER 失败时只裁掉那一列，不拖垮整批落库）。故这里改断言**运行时**
        生成的 SQL —— 原意图（列数 == 占位符数）不变，且额外锁定「保留下标数与列数
        一致」，防止裁剪时列/值错位落库。
        """
        sql, keep = mysql_store._build_insert_sql()
        cols_part = re.search(r"INSERT INTO stock_recommendation \((.*?)\) VALUES",
                              sql, re.S)
        self.assertIsNotNone(cols_part, "INSERT 语句缺少列清单")
        columns = [c.strip() for c in cols_part.group(1).split(",") if c.strip()]
        placeholders = sql.count("%s")
        self.assertEqual(len(columns), placeholders,
                         f"列 {len(columns)} 个但占位符 {placeholders} 个")
        self.assertEqual(len(columns), len(keep),
                         "裁剪后保留下标数与列数不一致 → 值会错位落库")
        self.assertEqual(len(columns), len(mysql_store._INSERT_COLUMNS),
                         "未探测列集合时应退回全列")
        # 新列必须真的在插入列表里，否则列建了却永远为 NULL
        for col in ("score_weights", "stop_rule", "prev_rec_date", "prev_rec_close"):
            self.assertIn(col, columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
