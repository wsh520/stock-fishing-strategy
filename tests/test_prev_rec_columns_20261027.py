"""2026-10-27 修复：重复推荐锚点（prev_rec_date / prev_rec_close）未落库。

【缺陷】2026-10-26 的 label 冷却（7b4c20e）在 screening_engine._apply_repeat_cooldown
里为命中行写了 prev_rec_date / prev_rec_close，但 store.save_recommendations 的
INSERT 列清单里没有这两列 —— 值在落库时被**静默丢弃**，只剩 signals_hit 里的人类
可读文本。后果：
  1) 事后无法用 SQL 重建「这只票 10 日内被推了几次、上次什么价」，跨 sleeve 冷却
     的效果无法被独立验证（只能靠翻日志）；
  2) 查库时若按这两列过滤会直接报 (1054, "Unknown column 'prev_rec_date'")。

【修复】
  a) DDL + 幂等补列：stock_recommendation 增 prev_rec_date DATE / prev_rec_close
     DECIMAL(10,3)；
  b) INSERT 落值：_opt_text(prev_rec_date)、_f(prev_rec_close)（空串/NaN 一律 NULL，
     不许出现 'nan' 字面量）；
  c) 顺带加固：INSERT 列清单改为按 _existing_columns（补列后的真实集合）动态裁剪，
     任一补列 ALTER 失败只丢该列标注，不再让整批推荐因 Unknown column 全部落库失败
     —— 落实「宁可少标注也要落库」。

【不改什么】repeat_seq 保持 per-sleeve 累计语义（用户要求「按 sleeve 分开统计」），
不与卡片「跨 sleeve 第 N 次」强行统一；两者分母不同，已在 _repeat_seq_map 文档写明。

本文件全部离线，不连接数据库。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pandas as pd

from store import mysql_store as s

_RESULTS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, bool(ok)))


_SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "store", "mysql_store.py"), encoding="utf-8").read()

# ===========================================================================
# 1) DDL 与幂等补列清单
# ===========================================================================
check("DDL: prev_rec_date 为 DATE", "prev_rec_date    DATE" in _SRC)
check("DDL: prev_rec_close 为 DECIMAL(10, 3)", "prev_rec_close   DECIMAL(10, 3)" in _SRC)
check("补列清单含 prev_rec_date", "prev_rec_date" in s._RECOMMENDATION_ALTERS)
check("补列清单含 prev_rec_close", "prev_rec_close" in s._RECOMMENDATION_ALTERS)
check("补列 prev_rec_date 用 ADD COLUMN 且 AFTER superseded_by",
      "ADD COLUMN prev_rec_date DATE" in s._RECOMMENDATION_ALTERS["prev_rec_date"]
      and "AFTER superseded_by" in s._RECOMMENDATION_ALTERS["prev_rec_date"])
check("补列 prev_rec_close 接在 prev_rec_date 之后",
      "AFTER prev_rec_date" in s._RECOMMENDATION_ALTERS["prev_rec_close"])

# ===========================================================================
# 2) INSERT 列清单：两列必须在，且顺序与取值元组一致
# ===========================================================================
check("_INSERT_COLUMNS 含 prev_rec_date", "prev_rec_date" in s._INSERT_COLUMNS)
check("_INSERT_COLUMNS 含 prev_rec_close", "prev_rec_close" in s._INSERT_COLUMNS)
check("_INSERT_COLUMNS 无重复列名",
      len(set(s._INSERT_COLUMNS)) == len(s._INSERT_COLUMNS))
check(f"_INSERT_COLUMNS 共 27 列（实际 {len(s._INSERT_COLUMNS)}）",
      len(s._INSERT_COLUMNS) == 27)

# ===========================================================================
# 3) 落库确实取了这两个值（防止再次被丢弃）
# ===========================================================================
check("落库: prev_rec_date 走 _opt_text",
      '_opt_text(r.get("prev_rec_date"))' in _SRC)
check("落库: prev_rec_close 走 _f（NaN → NULL）",
      '_f(r.get("prev_rec_close"))' in _SRC)
check("落库: 列数与元组长度不一致时快速失败",
      "长度不一致" in _SRC and "len(_INSERT_COLUMNS)" in _SRC)

# ===========================================================================
# 4) 动态列裁剪：ALTER 失败时不拖垮整批落库
# ===========================================================================
_orig_cols = s._existing_columns
try:
    s._existing_columns = None
    _sql, _keep = s._build_insert_sql()
    check("未探测成功(None): 退回全列（与改造前行为一致）",
          len(_keep) == len(s._INSERT_COLUMNS) and "prev_rec_date" in _sql)

    # 模拟「prev_rec_close 补列失败」：该列必须从 INSERT 中消失，其余列照常
    s._existing_columns = {c for c in s._INSERT_COLUMNS if c != "prev_rec_close"}
    _sql, _keep = s._build_insert_sql()
    check("缺 prev_rec_close: 该列被裁掉", "prev_rec_close" not in _sql)
    check("缺 prev_rec_close: prev_rec_date 保留", "prev_rec_date" in _sql)
    check("缺 prev_rec_close: 占位符数量与保留列数一致",
          _sql.count("%s") == len(_keep))
    check("缺 prev_rec_close: 只少一列",
          len(_keep) == len(s._INSERT_COLUMNS) - 1)

    # 模拟「所有新增列都没补上」：核心列仍能落库
    _core = {"rec_date", "code", "name", "rec_close", "strategy", "rec_tier"}
    s._existing_columns = set(_core)
    _sql, _keep = s._build_insert_sql()
    check("极端降级: 仅核心列也能构建 INSERT",
          all(c in _sql for c in _core) and "prev_rec_date" not in _sql)

    # 空集合是异常状态，必须报错而不是生成空列 INSERT
    s._existing_columns = set()
    try:
        s._build_insert_sql()
        check("空列集合: 应抛错", False)
    except ValueError:
        check("空列集合: 抛 ValueError（不生成非法 INSERT）", True)
finally:
    s._existing_columns = _orig_cols

# ===========================================================================
# 5) 空值语义：NULL，绝不写 'nan' / '' / NaT 字面量
# ===========================================================================
check('空串 → None', s._opt_text("") is None)
check('None → None', s._opt_text(None) is None)
check('NaN → None', s._opt_text(float("nan")) is None)
check('NaT → None', s._opt_text(pd.NaT) is None)
check("'nan' 字面量 → None", s._opt_text("nan") is None)
check("正常日期原样返回", s._opt_text("2026-10-23") == "2026-10-23")
check("prev_rec_close NaN → None", s._f(float("nan")) is None)
check("prev_rec_close None → None", s._f(None) is None)
check("prev_rec_close 正常值", s._f(15.11) == 15.11)

# ===========================================================================
# 6) 关键顺序不变量：列集合探测必须在构建 rows 之前
#    （与 test_mysql_text_columns 同源，防止有人把 _ensure_schema_cached 挪走）
# ===========================================================================
check("列探测在构建 rows 之前",
      _SRC.index("_ensure_schema_cached()\n    for _, r in df.iterrows():")
      < _SRC.index("rows.append(("))

# ===========================================================================
# 7) 统计侧不得按 superseded_by / prev_* 过滤（沿用 2026-10-26 的不变量）
# ===========================================================================
check("_mark_repeat_sequence 不按 superseded_by 排除统计样本",
      "统计/追踪查询一律不得按 superseded_by" in _SRC)

_ok = sum(1 for _, ok in _RESULTS if ok)
print(f"\n{_ok}/{len(_RESULTS)} 通过")
sys.exit(0 if _ok == len(_RESULTS) else 1)
