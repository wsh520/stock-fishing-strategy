"""2026-10-27：save_recommendations 落 prev_rec_date / prev_rec_close 的端到端冒烟。

【为什么要这个】静态断言只能证明「代码里调用了 _opt_text(prev_rec_date)」，
证明不了**值落在正确的列**上。2026-10-07 的 1136（列数与值数不匹配）与
2026-10-13 的 1406 都是「看着对、参数错」那类事故。这里用假连接捕获
executemany 的真实入参，按 _INSERT_COLUMNS 反查下标逐个核对。

【覆盖】
  1) 有 prev_* 列：值落到对应位置，repeat_seq 相关列不受影响；
  2) 无 prev_* 列（首次推荐）：写 NULL 而不是 '' / 'nan' / NaT；
  3) prev_rec_close 为 NaN（上次价缺失）：写 None；
  4) 补列失败（_existing_columns 少两列）：INSERT 自动裁列，其余值不错位。

全程离线，不连接数据库。
"""
import contextlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pandas as pd

from store import mysql_store as s

_RESULTS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, bool(ok)))


class _FakeCursor:
    def __init__(self, sink: dict):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.setdefault("executed", []).append((sql, params))

    def executemany(self, sql, rows):
        self.sink["sql"] = sql
        self.sink["rows"] = list(rows)
        return len(self.sink["rows"])

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, sink: dict):
        self.sink = sink

    def cursor(self):
        return _FakeCursor(self.sink)

    def close(self):
        pass


@contextlib.contextmanager
def _patched(sink: dict, existing_cols):
    orig = (s.is_configured, s._connect, s._ensure_tables,
            s._ensure_schema_cached, s._existing_columns)
    s.is_configured = lambda: True
    s._connect = lambda: _FakeConn(sink)
    s._ensure_tables = lambda conn: None
    s._ensure_schema_cached = lambda: None
    s._existing_columns = existing_cols
    try:
        yield
    finally:
        (s.is_configured, s._connect, s._ensure_tables,
         s._ensure_schema_cached, s._existing_columns) = orig


def _row(**over):
    base = {
        "date": "2026-10-27", "code": "603013", "name": "亚普股份", "close": 15.55,
        "score": 62.5, "grade": "B", "daily_score": 55.0,
        "rsi": 40.1, "rsi7": 38.2, "rsi21": 44.0,
        "vol_ratio": 1.2, "turnover_ratio": 0.9,
        "stop_loss": 13.2, "take_profit": 17.9, "rr_ratio": 2.1,
        "market_env": "neutral", "has_divergence": 1,
        "signals_hit": "优质低估低位", "fund_status": "verified",
        "weekly_status": "confirmed", "tier": "formal",
        "missing_tags": "", "score_weights": "0.45/0.30/0.25",
        "stop_rule": "atr3x_floor15",
    }
    base.update(over)
    return pd.DataFrame([base])


def _positions(columns) -> dict:
    return {c: i for i, c in enumerate(columns)}


# ===========================================================================
# 1) 有 prev_* 列：值必须落到对应列
# ===========================================================================
_sink: dict = {}
with _patched(_sink, set(s._INSERT_COLUMNS)):
    _n = s.save_recommendations(
        _row(prev_rec_date="2026-10-23", prev_rec_close=15.11),
        strategy="bottom_fishing")

check("新增 1 条", _n == 1)
_rows = _sink.get("rows") or []
check("executemany 收到 1 行", len(_rows) == 1)
_row_vals = _rows[0] if _rows else ()
check(f"行长度 = 列数 {len(s._INSERT_COLUMNS)}",
      len(_row_vals) == len(s._INSERT_COLUMNS))

_pos = _positions(s._INSERT_COLUMNS)
check("prev_rec_date 落在正确列位",
      _row_vals[_pos["prev_rec_date"]] == "2026-10-23")
check("prev_rec_close 落在正确列位",
      abs(float(_row_vals[_pos["prev_rec_close"]]) - 15.11) < 1e-9)
check("code 未被错位（列与值对齐）", _row_vals[_pos["code"]] == "603013")
check("stop_rule 未被错位", _row_vals[_pos["stop_rule"]] == "atr3x_floor15")
check("score_weights 未被错位",
      _row_vals[_pos["score_weights"]] == "0.45/0.30/0.25")
check("signals_hit 未被错位", _row_vals[_pos["signals_hit"]] == "优质低估低位")
check("SQL 列清单与 _INSERT_COLUMNS 一致",
      all(c in _sink.get("sql", "") for c in s._INSERT_COLUMNS))

# ===========================================================================
# 2) 无 prev_* 列（首次推荐）→ NULL
# ===========================================================================
_sink = {}
with _patched(_sink, set(s._INSERT_COLUMNS)):
    s.save_recommendations(_row(), strategy="bottom_fishing")
_v = _sink["rows"][0]
check("无 prev 列: prev_rec_date 为 None（不是 ''）", _v[_pos["prev_rec_date"]] is None)
check("无 prev 列: prev_rec_close 为 None", _v[_pos["prev_rec_close"]] is None)

# ===========================================================================
# 3) prev_rec_close 为 NaN / prev_rec_date 为空串 → NULL
# ===========================================================================
_sink = {}
with _patched(_sink, set(s._INSERT_COLUMNS)):
    s.save_recommendations(_row(prev_rec_date="", prev_rec_close=float("nan")),
                           strategy="bottom_fishing")
_v = _sink["rows"][0]
check("空串/NaN → prev_rec_date None", _v[_pos["prev_rec_date"]] is None)
check("空串/NaN → prev_rec_close None", _v[_pos["prev_rec_close"]] is None)

# ===========================================================================
# 4) 补列失败（少两列）→ 自动裁列，其余仍对齐
# ===========================================================================
_partial = set(s._INSERT_COLUMNS) - {"prev_rec_date", "prev_rec_close"}
_sink = {}
with _patched(_sink, _partial):
    _n = s.save_recommendations(
        _row(prev_rec_date="2026-10-23", prev_rec_close=15.11),
        strategy="bottom_fishing")
check("补列失败: 推荐照常落库（不整批丢失）", _n == 1)
_v = _sink["rows"][0]
check("补列失败: 行长度 25", len(_v) == len(s._INSERT_COLUMNS) - 2)
_p2 = _positions([c for c in s._INSERT_COLUMNS if c in _partial])
check("补列失败: code 仍对齐", _v[_p2["code"]] == "603013")
check("补列失败: stop_rule 仍对齐", _v[_p2["stop_rule"]] == "atr3x_floor15")
check("补列失败: SQL 不含 prev_rec_date",
      "prev_rec_date" not in _sink.get("sql", ""))

# ===========================================================================
# 5) 非 formal 不得落库（既有闸门未被破坏）
# ===========================================================================
_sink = {}
with _patched(_sink, set(s._INSERT_COLUMNS)):
    _n = s.save_recommendations(_row(tier="pending"), strategy="bottom_fishing")
check("pending 不落库", _n == 0 and "rows" not in _sink)

_ok = sum(1 for _, ok in _RESULTS if ok)
print(f"\n{_ok}/{len(_RESULTS)} 通过")
sys.exit(0 if _ok == len(_RESULTS) else 1)
