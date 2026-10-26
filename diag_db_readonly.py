"""只读查询生产库的真实落库推荐（分析用，绝不写库）。

【为什么不复用 store.mysql_store】
store/mysql_store.py 的每个查询函数（含 fetch_rec_codes_for_date）第一件事都
是 `_ensure_tables(conn)`，会执行 CREATE TABLE IF NOT EXISTS 与 ALTER TABLE。
只读账号会因 DDL 权限失败，异常被 except 吞掉后返回空集 —— 结果是「库里
明明有推荐，分析却看到 0 条」，且与真实的 0 推荐无法区分。本脚本用独立的
裸连接，绕开这条路径。

【三重只读保障】
1. 会话级：SET SESSION TRANSACTION READ ONLY，并读回 @@session.transaction_read_only 校验
2. 语句级：只放行 SELECT / SHOW / DESC / EXPLAIN，且拒绝 INTO OUTFILE / FOR UPDATE
3. 流程级：本脚本不 import store，不存在任何落库调用路径

【凭据来源】
优先环境变量；缺失时自动加载 .secrets/stock-db.env（已被 .gitignore 排除）。
密码永不打印、永不进异常信息。

用法：
    python diag_db_readonly.py ping
    python diag_db_readonly.py recs --days 30
    python diag_db_readonly.py recs --strategy bottom_fishing --tier formal
    python diag_db_readonly.py date 2026-10-23
    python diag_db_readonly.py tracking --days 60
    python diag_db_readonly.py sql "SELECT strategy, COUNT(*) FROM stock_recommendation GROUP BY strategy"
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE, ".secrets", "stock-db.env")
ENV_KEYS = ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE")

try:
    import pymysql
except ImportError:
    sys.exit("缺少 PyMySQL：pip install 'PyMySQL>=1.1.1'")

_ALLOWED_STMT = re.compile(r"^\s*(SELECT|SHOW|DESC|DESCRIBE|EXPLAIN)\b", re.I)
_FORBIDDEN = re.compile(r"\b(INTO\s+(OUTFILE|DUMPFILE)|FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE)\b", re.I)


# ---------------------------------------------------------------- 凭据加载


def load_env(path: str = ENV_FILE) -> None:
    """把 KEY='value' 形式的环境文件读进 os.environ。

    已存在的环境变量优先（CI / 手工 export 时不覆盖）。绝不打印值。
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line[0] == "#" or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key and val and not os.environ.get(key):
                os.environ[key] = val


def _target_desc() -> str:
    """可打印的连接目标（不含密码）。"""
    host = os.environ.get("MYSQL_HOST", "")
    port = os.environ.get("MYSQL_PORT") or "3306"
    user = os.environ.get("MYSQL_USER", "")
    db = os.environ.get("MYSQL_DATABASE", "")
    return f"{user}@{host}:{port}/{db}"


# ---------------------------------------------------------------- 连接


def connect():
    """建立只读连接；任一只读保障未生效即抛异常，不做静默降级。"""
    load_env()
    missing = [k for k in ENV_KEYS if not os.environ.get(k)]
    if missing:
        sys.exit(
            "缺少数据库配置：" + ", ".join(missing) + "\n"
            f"请填写 {ENV_FILE}，或 export 这些变量后重试。"
        )

    port_raw = (os.environ.get("MYSQL_PORT") or "").strip()
    try:
        port = int(port_raw) if port_raw else 3306
    except ValueError:
        port = 3306

    conn = pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=port,
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"],
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=60,
    )

    # 保障 1：会话级只读，并校验真的生效
    with conn.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.execute("SELECT @@session.transaction_read_only")
        row = cur.fetchone()
    if not row or int(row[0]) != 1:
        conn.close()
        sys.exit("只读会话未能启用（transaction_read_only != 1），已中止以免误写。")

    return conn


def run(conn, sql: str, params=None) -> tuple[list[str], list[tuple]]:
    """执行受白名单约束的语句，返回 (列名, 行)。"""
    if not _ALLOWED_STMT.match(sql):
        sys.exit(f"只允许 SELECT/SHOW/DESC/EXPLAIN，已拒绝：{sql[:60]}")
    if _FORBIDDEN.search(sql):
        sys.exit(f"语句含写文件/加锁子句，已拒绝：{sql[:60]}")
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall() if cur.description else []
    return cols, rows


# ---------------------------------------------------------------- 输出


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, dt.date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float):
        return f"{v:.2f}"
    s = str(v)
    return s if len(s) <= 40 else s[:37] + "..."


def table(cols: list[str], rows: list[tuple], max_rows: int = 200) -> None:
    if not cols:
        print("(无结果集)")
        return
    body = [[_fmt(v) for v in r] for r in rows[:max_rows]]
    widths = [len(c) for c in cols]
    for r in body:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in body:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r)))
    if len(rows) > max_rows:
        print(f"... 共 {len(rows)} 行，仅显示前 {max_rows} 行")


# ---------------------------------------------------------------- 子命令


def cmd_ping(conn) -> None:
    _, rows = run(conn, "SELECT VERSION() AS ver, DATABASE() AS db, CURRENT_USER() AS who")
    print(f"连接成功：{_target_desc()}")
    print(f"  版本={rows[0][0]}  库={rows[0][1]}  账号={rows[0][2]}")

    _, rows = run(conn, "SELECT @@session.transaction_read_only")
    print(f"  会话只读={rows[0][0]}（1=生效）")

    cols, rows = run(
        conn,
        "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME",
    )
    print("\n表概览：")
    table(cols, rows)

    print("\n推荐数据概况：")
    cols, rows = run(
        conn,
        "SELECT strategy, rec_tier, COUNT(*) AS n, MIN(rec_date) AS first_d, MAX(rec_date) AS last_d "
        "FROM stock_recommendation GROUP BY strategy, rec_tier ORDER BY strategy, rec_tier",
    )
    table(cols, rows)


def cmd_recs(conn, days: int | None, strategy: str | None, tier: str | None, limit: int) -> None:
    where, params = [], []
    if days:
        since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
        where.append("rec_date >= %s")
        params.append(since)
    if strategy:
        where.append("strategy = %s")
        params.append(strategy)
    if tier:
        where.append("rec_tier = %s")
        params.append(tier)
    sql = (
        "SELECT rec_date, code, name, rec_close, score, grade, rec_tier, strategy, "
        "fund_status, weekly_status, stop_loss, take_profit, signals_hit "
        "FROM stock_recommendation"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY rec_date DESC, score DESC LIMIT %s"
    params.append(limit)
    cols, rows = run(conn, sql, tuple(params))
    print(f"推荐记录（{_target_desc()}）")
    table(cols, rows)
    if not rows:
        print("\n注意：0 行。先确认这是真实无推荐，而不是连错了库 —— 用 ping 看表行数。")


def cmd_date(conn, day: str) -> None:
    cols, rows = run(
        conn,
        "SELECT rec_date, code, name, rec_close, score, grade, rec_tier, strategy, "
        "fund_status, weekly_status, stop_loss, take_profit, signals_hit, missing_tags "
        "FROM stock_recommendation WHERE rec_date = %s ORDER BY strategy, score DESC",
        (day,),
    )
    print(f"{day} 推荐（{len(rows)} 条）")
    table(cols, rows)
    if not rows:
        cols2, rows2 = run(
            conn,
            "SELECT MAX(rec_date) AS last_rec_date, COUNT(*) AS total FROM stock_recommendation",
        )
        print(f"\n该日无记录。库中最近推荐日={rows2[0][0]}，总计 {rows2[0][1]} 条。")


def cmd_tracking(conn, days: int, code: str | None, limit: int) -> None:
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    sql = (
        "SELECT t.rec_date, t.code, r.name, t.week_no, t.holding_trade_days, "
        "t.close_date, t.close_price, t.return_pct, t.weekly_return_pct, t.monthly_return_pct "
        "FROM stock_tracking t LEFT JOIN stock_recommendation r ON r.id = t.rec_id "
        "WHERE t.rec_date >= %s"
    )
    params: list = [since]
    if code:
        sql += " AND t.code = %s"
        params.append(code)
    sql += " ORDER BY t.rec_date DESC, t.code, t.week_no LIMIT %s"
    params.append(limit)
    cols, rows = run(conn, sql, tuple(params))
    print(f"追踪记录（近 {days} 天，{len(rows)} 行）")
    table(cols, rows)


def cmd_sql(conn, sql: str, limit: int) -> None:
    cols, rows = run(conn, sql)
    print(f"{len(rows)} 行")
    table(cols, rows, max_rows=limit)


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="只读查询生产库推荐数据")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ping", help="连通性 + 表概览")

    p = sub.add_parser("recs", help="最近推荐")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--strategy", default=None,
                   help="bottom_fishing / volume_breakout / longterm_quality")
    p.add_argument("--tier", default=None, help="formal / pending")
    p.add_argument("--limit", type=int, default=100)

    p = sub.add_parser("date", help="指定日期的推荐")
    p.add_argument("day", help="YYYY-MM-DD")

    p = sub.add_parser("tracking", help="追踪记录")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--code", default=None)
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("sql", help="任意只读 SQL（受白名单约束）")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=200)

    args = ap.parse_args()
    conn = connect()
    try:
        if args.cmd == "ping":
            cmd_ping(conn)
        elif args.cmd == "recs":
            cmd_recs(conn, args.days, args.strategy, args.tier, args.limit)
        elif args.cmd == "date":
            cmd_date(conn, args.day)
        elif args.cmd == "tracking":
            cmd_tracking(conn, args.days, args.code, args.limit)
        elif args.cmd == "sql":
            cmd_sql(conn, args.query, args.limit)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
