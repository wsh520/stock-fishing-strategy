"""2026-10-13 修复 MySQL 1406（signals_hit 超长导致整批落库失败）的回归测试。

生产报错：(1406, "Data too long for column 'signals_hit' at row 1")
根因：signals_hit / missing_tags 原为 VARCHAR(255)，而荐股证据链已增长到
112~147 字符且仍在变长；一旦超过**实际列容量**，MySQL 整批 INSERT 失败，
当日全部推荐一条都落不了库。

修复为三重防御：
  1) 新建表 DDL 直接用 TEXT；
  2) 存量表幂等 MODIFY 到 TEXT（不破坏数据与索引）；
  3) ALTER 失败时降级为按**实际列容量**截断 + warning —— 保证单条超长
     退化为该条截断，而不是整批推荐丢失。

本文件全部离线，不连接数据库（只测 DDL 文本与纯函数行为）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from store import mysql_store as s

_RESULTS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, bool(ok)))


_SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                         "store", "mysql_store.py"), encoding="utf-8").read()

# ===========================================================================
# 1) 建表 DDL：两列必须是 TEXT（不能再是 VARCHAR(255)）
# ===========================================================================
check("DDL: signals_hit 为 TEXT", "signals_hit      TEXT" in _SRC)
check("DDL: missing_tags 为 TEXT", "missing_tags     TEXT" in _SRC)
check("DDL: 已不存在 VARCHAR(255) 的文本列定义",
      not re.search(r"(signals_hit|missing_tags)\s+VARCHAR\(255\)", _SRC))

# ===========================================================================
# 2) 存量表幂等升级
# ===========================================================================
check("升级清单含 signals_hit 且用 MODIFY",
      "signals_hit" in s._TEXT_COLUMN_UPGRADES
      and "MODIFY COLUMN signals_hit TEXT" in s._TEXT_COLUMN_UPGRADES["signals_hit"])
check("升级清单含 missing_tags 且用 MODIFY",
      "missing_tags" in s._TEXT_COLUMN_UPGRADES
      and "MODIFY COLUMN missing_tags TEXT" in s._TEXT_COLUMN_UPGRADES["missing_tags"])
check("ALTER 幂等：TEXT 类型被识别为无需升级",
      "text" in s._TEXT_TYPES and "mediumtext" in s._TEXT_TYPES)

# ===========================================================================
# 3) 截断兜底：纯函数行为
# ===========================================================================
_orig = dict(s._text_column_capacity)
_LONG = ("优质低估低位,自身历史PE分位9%（250个样本）,动能改善,放量企稳,"
         "行业估值分位PE25%/PB57%,2026-06-30 净利同比+2%,近60日相对强度+7.5pp,"
         "近期有效底部2026-09-17，距底+5.8%,止跌中确认,经营证据通过；2026-06-30；"
         "收入同比+1.8%；净利同比+2.0%；扣非同比+5.4%；经营现金流同比+1.7%；"
         "毛利率-1.1个百分点；净利率+0.0个百分点,右侧雏形·站上MA20（仍在MA60下方）")

# 列较短（存量库可能短于代码 DDL）→ 按实际容量截断，且结果一定不超容量
s._text_column_capacity = {"signals_hit": 120, "missing_tags": 255}
_out = s._clip_to_capacity(_LONG, "signals_hit", "000963")
check(f"截断: 225 字符按容量 120 截断（实际 {len(_LONG)} → {len(_out)}）",
      len(_out) <= 120 and len(_out) < len(_LONG))
check("截断: 保留尾部省略号，便于识别被裁剪", _out.endswith("..."))
check("截断: 保留头部有效内容", _out.startswith("优质低估低位"))

# 短值原样返回（不做无谓裁剪）
_SHORT = "优质低估低位,趋势待确认"
check("短值: 原样返回", s._clip_to_capacity(_SHORT, "signals_hit", "000963") == _SHORT)
check("恰好等于容量: 原样返回",
      s._clip_to_capacity("x" * 120, "signals_hit", "000963") == "x" * 120)

# 空值语义保持不变（NULL 而非空串）
check("空串 → None", s._clip_to_capacity("", "signals_hit", "x") is None)
check("None → None", s._clip_to_capacity(None, "signals_hit", "x") is None)
check("非字符串（数字）→ 字符串化",
      s._clip_to_capacity(123, "signals_hit", "x") == "123")

# TEXT 列 / 未探测到容量 → 不截断
s._text_column_capacity = {"signals_hit": None, "missing_tags": None}
check("TEXT 列: 不截断", s._clip_to_capacity(_LONG, "signals_hit", "x") == _LONG)
s._text_column_capacity = {}
check("容量未探测: 不截断（不制造静默数据丢失）",
      s._clip_to_capacity(_LONG, "signals_hit", "x") == _LONG)
s._text_column_capacity = dict(_orig)

# ===========================================================================
# 4) 落库路径确实用了截断兜底，且在容量探测之后
# ===========================================================================
check("落库: signals_hit 走 _clip_to_capacity",
      '_clip_to_capacity(r.get("signals_hit"), "signals_hit", code_str)' in _SRC)
check("落库: missing_tags 走 _clip_to_capacity",
      '_clip_to_capacity(r.get("missing_tags"), "missing_tags", code_str)' in _SRC)
check("落库: 容量探测在构建 rows 之前调用",
      _SRC.index("_ensure_schema_cached()\n    for _, r in df.iterrows():")
      < _SRC.index("rows.append(("))

# ===========================================================================
# 5) 关键不变量：截断后的长度永不超过列容量（这正是 1406 的判据）
# ===========================================================================
for limit in (30, 64, 100, 255, 512):
    s._text_column_capacity = {"signals_hit": limit}
    _o = s._clip_to_capacity(_LONG, "signals_hit", "000963")
    check(f"不变量: 容量 {limit} 时输出长度 {len(_o)} ≤ {limit}", len(_o) <= limit)
s._text_column_capacity = dict(_orig)

_ok = sum(1 for _, ok in _RESULTS if ok)
print(f"\n{_ok}/{len(_RESULTS)} 通过")
sys.exit(0 if _ok == len(_RESULTS) else 1)