"""
MySQL 存储模块

通过环境变量配置连接，未配置时 get_mysql_store() 返回 None，不影响主流程。
使用 PyMySQL + DBUtils 连接池。

环境变量:
    MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE

K线数据表:
    kline_daily   — 个股日线 OHLCV（增量同步，UPSERT）
    kline_weekly  — 个股周线 OHLCV（由日线聚合或独立同步）
    index_daily   — 指数日线（沪深300等）
    stock_info    — 股票列表快照
    sync_log      — 同步任务日志（记录每次同步的进度）
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import pandas as pd

# MySQL 连接依赖（可选）
try:
    import pymysql
    from dbutils.pooled_db import PooledDB

    _MYSQL_AVAILABLE = True
except ImportError:
    _MYSQL_AVAILABLE = False


def get_mysql_store() -> Optional[MySQLStore]:
    """工厂函数：根据环境变量创建 MySQLStore 实例。

    未配置或连接失败时返回 None，主流程降级为纯 CSV 存储。
    """
    if not _MYSQL_AVAILABLE:
        return None

    host = os.environ.get("MYSQL_HOST")
    if not host:
        return None

    try:
        store = MySQLStore(
            host=host,
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            user=os.environ.get("MYSQL_USER", "root"),
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ.get("MYSQL_DATABASE", "stock_strategy"),
        )
        store._ensure_tables()
        return store
    except Exception as e:
        print(f"[WARN] MySQL 连接失败: {e}")
        return None


class MySQLStore:
    """MySQL 持久化存储，封装推荐记录、追踪状态、绩效快照的读写。"""

    def __init__(
        self,
        host: str,
        port: int = 3306,
        user: str = "root",
        password: str = "",
        database: str = "stock_strategy",
    ):
        self._pool = PooledDB(
            creator=pymysql,
            maxconnections=5,
            mincached=1,
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=True,
        )

    def _get_conn(self):
        return self._pool.connection()

    def _ensure_tables(self) -> None:
        """首次运行时自动创建所需表。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS recommendations (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        code VARCHAR(10) NOT NULL,
                        name VARCHAR(50),
                        date DATE,
                        close DECIMAL(10,2),
                        score DECIMAL(5,1),
                        grade CHAR(1),
                        weekly_score DECIMAL(5,1),
                        daily_score DECIMAL(5,1),
                        pct_chg_w DECIMAL(6,2),
                        atr_decline DECIMAL(6,2),
                        cci DECIMAL(8,1),
                        rsi DECIMAL(5,1),
                        vol_ratio DECIMAL(6,2),
                        turnover_ratio DECIMAL(6,2),
                        holds_prior_low TINYINT(1),
                        macd_divergence TINYINT(1),
                        stop_loss DECIMAL(10,2),
                        take_profit DECIMAL(10,2),
                        rr_ratio DECIMAL(5,2),
                        market_env VARCHAR(20),
                        ma20 DECIMAL(10,2),
                        saved_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_code_date (code, date),
                        INDEX idx_date (date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS tracking (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        code VARCHAR(10) NOT NULL,
                        name VARCHAR(50),
                        rec_date DATE,
                        rec_price DECIMAL(10,2),
                        current_price DECIMAL(10,2),
                        return_pct DECIMAL(6,2),
                        status VARCHAR(20),
                        stop_loss DECIMAL(10,2),
                        take_profit DECIMAL(10,2),
                        grade CHAR(1),
                        score DECIMAL(5,1),
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_code (code),
                        INDEX idx_status (status)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS performance_snapshots (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        snapshot_date DATE NOT NULL,
                        period_days INT,
                        total_signals INT,
                        win_rate DECIMAL(5,1),
                        avg_return DECIMAL(6,2),
                        max_return DECIMAL(6,2),
                        min_return DECIMAL(6,2),
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_date (snapshot_date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS backtest_trades (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        backtest_id VARCHAR(32) NOT NULL,
                        code VARCHAR(10),
                        name VARCHAR(50),
                        entry_date DATE,
                        entry_price DECIMAL(10,2),
                        exit_date DATE,
                        exit_price DECIMAL(10,2),
                        return_pct DECIMAL(8,2),
                        hold_days INT,
                        exit_reason VARCHAR(20),
                        score DECIMAL(5,1),
                        grade CHAR(1),
                        weekly_score DECIMAL(5,1),
                        daily_score DECIMAL(5,1),
                        stop_loss DECIMAL(10,2),
                        take_profit DECIMAL(10,2),
                        rr_ratio DECIMAL(5,2),
                        market_env VARCHAR(20),
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_bt_id (backtest_id),
                        INDEX idx_entry_date (entry_date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS backtest_summary (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        backtest_id VARCHAR(32) NOT NULL,
                        start_date DATE,
                        end_date DATE,
                        total_trades INT,
                        win_count INT,
                        win_rate DECIMAL(5,1),
                        avg_return DECIMAL(8,2),
                        max_return DECIMAL(8,2),
                        min_return DECIMAL(8,2),
                        profit_factor DECIMAL(8,2),
                        avg_hold_days DECIMAL(5,1),
                        stop_loss_count INT,
                        take_profit_count INT,
                        expired_count INT,
                        params_json TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_bt_id (backtest_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                # ---- K线数据表（增量同步） ----
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS kline_daily (
                        code VARCHAR(10) NOT NULL,
                        date DATE NOT NULL,
                        open DECIMAL(10,3),
                        close DECIMAL(10,3),
                        high DECIMAL(10,3),
                        low DECIMAL(10,3),
                        volume BIGINT,
                        amount DECIMAL(18,2),
                        amplitude DECIMAL(8,4),
                        pct_chg DECIMAL(8,4),
                        change_amt DECIMAL(10,3),
                        turnover DECIMAL(8,4),
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (code, date),
                        INDEX idx_date (date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS kline_weekly (
                        code VARCHAR(10) NOT NULL,
                        date DATE NOT NULL,
                        open DECIMAL(10,3),
                        close DECIMAL(10,3),
                        high DECIMAL(10,3),
                        low DECIMAL(10,3),
                        volume BIGINT,
                        amount DECIMAL(18,2),
                        amplitude DECIMAL(8,4),
                        pct_chg DECIMAL(8,4),
                        change_amt DECIMAL(10,3),
                        turnover DECIMAL(8,4),
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (code, date),
                        INDEX idx_date (date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS index_daily (
                        symbol VARCHAR(20) NOT NULL,
                        date DATE NOT NULL,
                        open DECIMAL(12,3),
                        close DECIMAL(12,3),
                        high DECIMAL(12,3),
                        low DECIMAL(12,3),
                        volume BIGINT,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (symbol, date),
                        INDEX idx_date (date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS stock_info (
                        code VARCHAR(10) NOT NULL PRIMARY KEY,
                        name VARCHAR(50),
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sync_log (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        sync_type VARCHAR(20) NOT NULL COMMENT 'daily/weekly/index/stock_list',
                        sync_date DATE NOT NULL,
                        rows_affected INT DEFAULT 0,
                        status VARCHAR(20) DEFAULT 'running',
                        started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        finished_at DATETIME NULL,
                        error_msg TEXT NULL,
                        INDEX idx_type_date (sync_type, sync_date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
        finally:
            conn.close()

    def save_recommendations(self, df: pd.DataFrame) -> None:
        """批量插入推荐记录。"""
        if df is None or df.empty:
            return
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                sql = """
                    INSERT INTO recommendations
                    (code, name, date, close, score, grade, weekly_score, daily_score,
                     pct_chg_w, atr_decline, cci, rsi, vol_ratio, turnover_ratio,
                     holds_prior_low, macd_divergence, stop_loss, take_profit,
                     rr_ratio, market_env, ma20)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """
                rows = []
                for _, r in df.iterrows():
                    rows.append((
                        r.get("code"), r.get("name"), r.get("date"),
                        r.get("close"), r.get("score"), r.get("grade"),
                        r.get("weekly_score"), r.get("daily_score"),
                        r.get("pct_chg_w"), r.get("atr_decline"),
                        r.get("cci"), r.get("rsi"),
                        r.get("vol_ratio"), r.get("turnover_ratio"),
                        int(bool(r.get("holds_prior_low"))),
                        int(bool(r.get("macd_divergence"))),
                        r.get("stop_loss"), r.get("take_profit"),
                        r.get("rr_ratio"), r.get("market_env"),
                        r.get("ma20"),
                    ))
                cur.executemany(sql, rows)
            print(f"[INFO] MySQL: 已保存 {len(rows)} 条推荐记录")
        except Exception as e:
            print(f"[WARN] MySQL 保存推荐失败: {e}")
        finally:
            conn.close()

    def update_tracking(self, report: pd.DataFrame) -> None:
        """更新追踪状态（先删旧记录再插入，保证幂等）。"""
        if report is None or report.empty:
            return
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                # 清除已有追踪记录后重新写入
                codes = report["code"].unique().tolist()
                if codes:
                    placeholders = ",".join(["%s"] * len(codes))
                    cur.execute(f"DELETE FROM tracking WHERE code IN ({placeholders})", codes)

                sql = """
                    INSERT INTO tracking
                    (code, name, rec_date, rec_price, current_price, return_pct,
                     status, stop_loss, take_profit, grade, score)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """
                rows = []
                for _, r in report.iterrows():
                    rows.append((
                        r.get("code"), r.get("name"), r.get("rec_date"),
                        r.get("rec_price"), r.get("current_price"),
                        r.get("return_pct"), r.get("status"),
                        r.get("stop_loss"), r.get("take_profit"),
                        r.get("grade"), r.get("score"),
                    ))
                cur.executemany(sql, rows)
            print(f"[INFO] MySQL: 已更新 {len(rows)} 条追踪记录")
        except Exception as e:
            print(f"[WARN] MySQL 更新追踪失败: {e}")
        finally:
            conn.close()

    def get_monthly_performance(self, days: int = 30) -> Optional[dict]:
        """查询近N天的推荐绩效统计。"""
        conn = self._get_conn()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                # 获取有追踪结果的推荐记录
                cur.execute("""
                    SELECT r.code, r.grade, t.return_pct, t.status
                    FROM recommendations r
                    INNER JOIN tracking t ON r.code = t.code AND r.date = t.rec_date
                    WHERE r.saved_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                """, (days,))
                rows = cur.fetchall()

            if not rows:
                return None

            returns = [float(r["return_pct"]) for r in rows if r.get("return_pct") is not None]
            if not returns:
                return None

            total = len(returns)
            wins = sum(1 for r in returns if r > 0)

            # 等级分布
            grade_dist: dict[str, int] = {}
            for r in rows:
                g = r.get("grade", "?")
                grade_dist[g] = grade_dist.get(g, 0) + 1

            return {
                "total_signals": total,
                "win_rate": wins / total * 100 if total > 0 else 0,
                "avg_return": sum(returns) / total,
                "max_return": max(returns),
                "min_return": min(returns),
                "grade_distribution": grade_dist,
                "period": f"近{days}天",
            }
        except Exception as e:
            print(f"[WARN] MySQL 查询绩效失败: {e}")
            return None
        finally:
            conn.close()

    def save_performance_snapshot(self, stats: dict) -> None:
        """保存绩效快照。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO performance_snapshots
                    (snapshot_date, period_days, total_signals, win_rate,
                     avg_return, max_return, min_return)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    datetime.now().strftime("%Y-%m-%d"),
                    30,
                    stats.get("total_signals", 0),
                    stats.get("win_rate", 0),
                    stats.get("avg_return", 0),
                    stats.get("max_return", 0),
                    stats.get("min_return", 0),
                ))
            print("[INFO] MySQL: 绩效快照已保存")
        except Exception as e:
            print(f"[WARN] MySQL 保存快照失败: {e}")
        finally:
            conn.close()

    def save_backtest_trades(self, backtest_id: str, df: pd.DataFrame) -> None:
        """批量插入回测交易记录。"""
        if df is None or df.empty:
            return
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                sql = """
                    INSERT INTO backtest_trades
                    (backtest_id, code, name, entry_date, entry_price,
                     exit_date, exit_price, return_pct, hold_days, exit_reason,
                     score, grade, weekly_score, daily_score,
                     stop_loss, take_profit, rr_ratio, market_env)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """
                rows = []
                for _, r in df.iterrows():
                    rows.append((
                        backtest_id,
                        r.get("code"), r.get("name"),
                        r.get("entry_date"), r.get("entry_price"),
                        r.get("exit_date"), r.get("exit_price"),
                        r.get("return_pct"), r.get("hold_days"),
                        r.get("exit_reason"),
                        r.get("score"), r.get("grade"),
                        r.get("weekly_score"), r.get("daily_score"),
                        r.get("stop_loss"), r.get("take_profit"),
                        r.get("rr_ratio"), r.get("market_env"),
                    ))
                cur.executemany(sql, rows)
            print(f"[INFO] MySQL: 已保存 {len(rows)} 条回测交易记录")
        except Exception as e:
            print(f"[WARN] MySQL 保存回测交易失败: {e}")
        finally:
            conn.close()

    def save_backtest_summary(self, backtest_id: str, summary: dict) -> None:
        """保存回测汇总统计。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO backtest_summary
                    (backtest_id, start_date, end_date, total_trades, win_count,
                     win_rate, avg_return, max_return, min_return, profit_factor,
                     avg_hold_days, stop_loss_count, take_profit_count,
                     expired_count, params_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    backtest_id,
                    summary.get("start_date"),
                    summary.get("end_date"),
                    summary.get("total_trades", 0),
                    summary.get("win_count", 0),
                    summary.get("win_rate", 0),
                    summary.get("avg_return", 0),
                    summary.get("max_return", 0),
                    summary.get("min_return", 0),
                    summary.get("profit_factor", 0),
                    summary.get("avg_hold_days", 0),
                    summary.get("stop_loss_count", 0),
                    summary.get("take_profit_count", 0),
                    summary.get("expired_count", 0),
                    summary.get("params_json", "{}"),
                ))
            print(f"[INFO] MySQL: 回测汇总已保存 (ID: {backtest_id})")
        except Exception as e:
            print(f"[WARN] MySQL 保存回测汇总失败: {e}")
        finally:
            conn.close()

    def get_last_backtest_end_date(self) -> Optional[str]:
        """查询最近一次回测的结束日期，用于增量回测续跑。

        Returns:
            "YYYYMMDD" 格式的日期字符串，无记录时返回 None。
        """
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT end_date FROM backtest_summary
                    ORDER BY created_at DESC LIMIT 1
                """)
                row = cur.fetchone()
                if row and row[0]:
                    return pd.to_datetime(row[0]).strftime("%Y%m%d")
            return None
        except Exception as e:
            print(f"[WARN] MySQL 查询回测记录失败: {e}")
            return None
        finally:
            conn.close()

    # ===================================================================
    # K线数据持久化（增量同步 + 查询）
    # ===================================================================

    def save_kline_daily(self, code: str, df: pd.DataFrame) -> int:
        """批量 UPSERT 个股日线数据到 kline_daily 表。

        使用 INSERT ... ON DUPLICATE KEY UPDATE 实现幂等写入。
        返回受影响行数。
        """
        if df is None or df.empty:
            return 0
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                sql = """
                    INSERT INTO kline_daily
                    (code, date, open, close, high, low, volume, amount,
                     amplitude, pct_chg, change_amt, turnover)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        open=VALUES(open), close=VALUES(close),
                        high=VALUES(high), low=VALUES(low),
                        volume=VALUES(volume), amount=VALUES(amount),
                        amplitude=VALUES(amplitude), pct_chg=VALUES(pct_chg),
                        change_amt=VALUES(change_amt), turnover=VALUES(turnover)
                """
                rows = []
                for _, r in df.iterrows():
                    rows.append((
                        code,
                        r.get("date"),
                        r.get("open"), r.get("close"),
                        r.get("high"), r.get("low"),
                        r.get("volume"), r.get("amount"),
                        r.get("amplitude"), r.get("pct_chg"),
                        r.get("change"), r.get("turnover"),
                    ))
                cur.executemany(sql, rows)
                return cur.rowcount
        except Exception as e:
            print(f"[WARN] MySQL 保存日线失败 ({code}): {e}")
            return 0
        finally:
            conn.close()

    def save_kline_weekly(self, code: str, df: pd.DataFrame) -> int:
        """批量 UPSERT 个股周线数据到 kline_weekly 表。"""
        if df is None or df.empty:
            return 0
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                sql = """
                    INSERT INTO kline_weekly
                    (code, date, open, close, high, low, volume, amount,
                     amplitude, pct_chg, change_amt, turnover)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        open=VALUES(open), close=VALUES(close),
                        high=VALUES(high), low=VALUES(low),
                        volume=VALUES(volume), amount=VALUES(amount),
                        amplitude=VALUES(amplitude), pct_chg=VALUES(pct_chg),
                        change_amt=VALUES(change_amt), turnover=VALUES(turnover)
                """
                rows = []
                for _, r in df.iterrows():
                    rows.append((
                        code,
                        r.get("date"),
                        r.get("open"), r.get("close"),
                        r.get("high"), r.get("low"),
                        r.get("volume"), r.get("amount"),
                        r.get("amplitude"), r.get("pct_chg"),
                        r.get("change"), r.get("turnover"),
                    ))
                cur.executemany(sql, rows)
                return cur.rowcount
        except Exception as e:
            print(f"[WARN] MySQL 保存周线失败 ({code}): {e}")
            return 0
        finally:
            conn.close()

    def save_index_daily(self, symbol: str, df: pd.DataFrame) -> int:
        """批量 UPSERT 指数日线数据。"""
        if df is None or df.empty:
            return 0
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                sql = """
                    INSERT INTO index_daily
                    (symbol, date, open, close, high, low, volume)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        open=VALUES(open), close=VALUES(close),
                        high=VALUES(high), low=VALUES(low),
                        volume=VALUES(volume)
                """
                rows = []
                for _, r in df.iterrows():
                    rows.append((
                        symbol,
                        r.get("date"),
                        r.get("open"), r.get("close"),
                        r.get("high"), r.get("low"),
                        r.get("volume"),
                    ))
                cur.executemany(sql, rows)
                return cur.rowcount
        except Exception as e:
            print(f"[WARN] MySQL 保存指数日线失败 ({symbol}): {e}")
            return 0
        finally:
            conn.close()

    def save_stock_info(self, df: pd.DataFrame) -> int:
        """全量替换股票列表（先清空再插入）。"""
        if df is None or df.empty:
            return 0
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE stock_info")
                sql = """
                    INSERT INTO stock_info (code, name)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE name=VALUES(name)
                """
                rows = [(r["code"], r["name"]) for _, r in df.iterrows()]
                cur.executemany(sql, rows)
                return len(rows)
        except Exception as e:
            print(f"[WARN] MySQL 保存股票列表失败: {e}")
            return 0
        finally:
            conn.close()

    def get_kline_daily(
        self, code: str, start: str = "", end: str = "",
    ) -> Optional[pd.DataFrame]:
        """从 DB 读取个股日线数据。start/end 格式 YYYY-MM-DD 或 YYYYMMDD。

        返回与 _normalize_hist() 输出格式一致的 DataFrame。
        """
        conn = self._get_conn()
        try:
            conditions = ["code = %s"]
            params: list = [code]
            if start:
                s = start.replace("-", "")
                s = f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else start
                conditions.append("date >= %s")
                params.append(s)
            if end:
                e = end.replace("-", "")
                e = f"{e[:4]}-{e[4:6]}-{e[6:]}" if len(e) == 8 else end
                conditions.append("date <= %s")
                params.append(e)

            where = " AND ".join(conditions)
            sql = f"""
                SELECT date, open, close, high, low, volume, amount,
                       amplitude, pct_chg, change_amt AS change, turnover
                FROM kline_daily
                WHERE {where}
                ORDER BY date
            """
            df = pd.read_sql(sql, conn, params=params)
            if df.empty:
                return None
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            return df
        except Exception as e:
            print(f"[WARN] MySQL 读取日线失败 ({code}): {e}")
            return None
        finally:
            conn.close()

    def aggregate_daily_to_weekly(self, code: str) -> int:
        """从 kline_daily 聚合生成该股票的周线数据并写入 kline_weekly。

        使用 SQL GROUP BY YEARWEEK 直接在 DB 内完成聚合，避免读出再写回。
        返回受影响行数。
        """
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                sql = """
                    INSERT INTO kline_weekly
                    (code, date, open, close, high, low, volume, amount, turnover)
                    SELECT
                        code,
                        MAX(date) AS date,
                        SUBSTRING_INDEX(GROUP_CONCAT(open ORDER BY date ASC), ',', 1) AS open,
                        SUBSTRING_INDEX(GROUP_CONCAT(close ORDER BY date DESC), ',', 1) AS close,
                        MAX(high) AS high,
                        MIN(low) AS low,
                        SUM(volume) AS volume,
                        SUM(amount) AS amount,
                        AVG(turnover) AS turnover
                    FROM kline_daily
                    WHERE code = %s
                    GROUP BY YEARWEEK(date, 1)
                    ON DUPLICATE KEY UPDATE
                        open=VALUES(open), close=VALUES(close),
                        high=VALUES(high), low=VALUES(low),
                        volume=VALUES(volume), amount=VALUES(amount),
                        turnover=VALUES(turnover)
                """
                cur.execute(sql, (code,))
                return cur.rowcount
        except Exception as e:
            print(f"[WARN] MySQL 日线聚合周线失败 ({code}): {e}")
            return 0
        finally:
            conn.close()

    def get_kline_weekly(
        self, code: str, start: str = "", end: str = "",
    ) -> Optional[pd.DataFrame]:
        """从 DB 读取个股周线数据。"""
        conn = self._get_conn()
        try:
            conditions = ["code = %s"]
            params: list = [code]
            if start:
                s = start.replace("-", "")
                s = f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else start
                conditions.append("date >= %s")
                params.append(s)
            if end:
                e = end.replace("-", "")
                e = f"{e[:4]}-{e[4:6]}-{e[6:]}" if len(e) == 8 else end
                conditions.append("date <= %s")
                params.append(e)

            where = " AND ".join(conditions)
            sql = f"""
                SELECT date, open, close, high, low, volume, amount,
                       amplitude, pct_chg, change_amt AS change, turnover
                FROM kline_weekly
                WHERE {where}
                ORDER BY date
            """
            df = pd.read_sql(sql, conn, params=params)
            if df.empty:
                return None
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            return df
        except Exception as e:
            print(f"[WARN] MySQL 读取周线失败 ({code}): {e}")
            return None
        finally:
            conn.close()

    def get_index_daily(
        self, symbol: str, start: str = "", end: str = "",
    ) -> Optional[pd.DataFrame]:
        """从 DB 读取指数日线数据。"""
        conn = self._get_conn()
        try:
            conditions = ["symbol = %s"]
            params: list = [symbol]
            if start:
                s = start.replace("-", "")
                s = f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else start
                conditions.append("date >= %s")
                params.append(s)
            if end:
                e = end.replace("-", "")
                e = f"{e[:4]}-{e[4:6]}-{e[6:]}" if len(e) == 8 else end
                conditions.append("date <= %s")
                params.append(e)

            where = " AND ".join(conditions)
            sql = f"""
                SELECT date, open, close, high, low, volume
                FROM index_daily
                WHERE {where}
                ORDER BY date
            """
            df = pd.read_sql(sql, conn, params=params)
            if df.empty:
                return None
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            return df
        except Exception as e:
            print(f"[WARN] MySQL 读取指数日线失败 ({symbol}): {e}")
            return None
        finally:
            conn.close()

    def get_stock_list_from_db(self) -> Optional[pd.DataFrame]:
        """从 DB 读取股票列表。"""
        conn = self._get_conn()
        try:
            df = pd.read_sql("SELECT code, name FROM stock_info ORDER BY code", conn)
            if df.empty:
                return None
            df["code"] = df["code"].astype(str).str.zfill(6)
            return df
        except Exception as e:
            print(f"[WARN] MySQL 读取股票列表失败: {e}")
            return None
        finally:
            conn.close()

    def get_latest_kline_date(self, table: str, code: str = "", symbol: str = "") -> Optional[str]:
        """查询某只股票/指数在指定表中的最新日期。

        返回 'YYYYMMDD' 格式，无数据时返回 None。
        """
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if table == "index_daily":
                    cur.execute(
                        "SELECT MAX(date) FROM index_daily WHERE symbol = %s",
                        (symbol,),
                    )
                elif table == "kline_weekly":
                    cur.execute(
                        "SELECT MAX(date) FROM kline_weekly WHERE code = %s",
                        (code,),
                    )
                else:
                    cur.execute(
                        "SELECT MAX(date) FROM kline_daily WHERE code = %s",
                        (code,),
                    )
                row = cur.fetchone()
                if row and row[0]:
                    return pd.to_datetime(row[0]).strftime("%Y%m%d")
            return None
        except Exception as e:
            print(f"[WARN] MySQL 查询最新日期失败: {e}")
            return None
        finally:
            conn.close()

    def log_sync_start(self, sync_type: str, sync_date: str) -> int:
        """记录同步任务开始，返回 log id。"""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sync_log (sync_type, sync_date, status) VALUES (%s, %s, 'running')",
                    (sync_type, sync_date),
                )
                return cur.lastrowid
        except Exception as e:
            print(f"[WARN] MySQL 记录同步开始失败: {e}")
            return 0
        finally:
            conn.close()

    def log_sync_finish(
        self, log_id: int, rows_affected: int = 0,
        status: str = "success", error_msg: str = "",
    ) -> None:
        """更新同步任务完成状态。"""
        if not log_id:
            return
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE sync_log SET rows_affected=%s, status=%s,
                       finished_at=NOW(), error_msg=%s WHERE id=%s""",
                    (rows_affected, status, error_msg or None, log_id),
                )
        except Exception as e:
            print(f"[WARN] MySQL 更新同步日志失败: {e}")
        finally:
            conn.close()
