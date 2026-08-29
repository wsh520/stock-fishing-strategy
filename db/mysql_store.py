"""
MySQL 存储模块 - 选股推荐数据持久化

通过环境变量配置连接:
    MYSQL_HOST      - MySQL 服务器地址
    MYSQL_PORT      - 端口（默认 3306）
    MYSQL_USER      - 用户名
    MYSQL_PASSWORD  - 密码
    MYSQL_DATABASE  - 数据库名

如果未配置或连接失败，get_mysql_store() 返回 None，不影响主流程。
"""

import json
import logging
import os
import statistics
from datetime import datetime, date
from typing import Optional, Dict, List, Any

import pandas as pd
import numpy as np

try:
    import pymysql
    from dbutils.pooled_db import PooledDB
    _MYSQL_AVAILABLE = True
except ImportError:
    _MYSQL_AVAILABLE = False

logger = logging.getLogger(__name__)

# 单例实例
_store_instance: Optional["MySQLStore"] = None


# DataFrame 中文列名 -> MySQL 列名映射
COLUMN_MAP = {
    "代码": "code",
    "名称": "name",
    "所属行业": "industry",
    "综合评分": "total_score",
    "信号等级": "signal_level",
    "市场环境": "market_env",
    "现价": "price",
    "周线MA20": "weekly_ma20",
    "右侧买入价(日MA5)": "buy_price",
    "动态止损价": "dynamic_stop",
    "第一止盈价": "first_tp",
    "第二止盈防守线": "second_tp_defense",
    "本周涨跌幅": "week_return",
    "ATR归一化跌幅": "normalized_drop",
    "周线成交量比": "volume_ratio",
    "周线换手比": "turnover_ratio",
    "本周换手率": "weekly_turnover",
    "量能模式": "volume_mode",
    "前20周低点": "prev_20_low",
    "本周最低价": "week_low",
    "距前低距离(%)": "distance_to_low",
    "20周价格位置": "position_20",
    "本周收盘位置": "close_position",
    "下影线比例": "lower_shadow_ratio",
    "止损距离(%)": "stop_distance_pct",
    "风险收益比": "risk_reward_ratio",
    "RSI14": "rsi14",
    "日MA5": "daily_ma5",
    "日MA10": "daily_ma10",
    "日MA20": "daily_ma20",
    "前一日最高价": "prev_day_high",
    "ROE": "roe",
    "资产负债率": "debt_ratio",
    "经营现金流": "cashflow",
    "营收同比": "revenue_growth",
    "净利润同比": "profit_growth",
    "日线确认得分": "daily_confirm_score",
}


def get_mysql_store() -> Optional["MySQLStore"]:
    """
    获取 MySQLStore 单例实例。
    未配置或连接失败时返回 None（优雅降级）。
    """
    global _store_instance

    if not _MYSQL_AVAILABLE:
        logger.info("PyMySQL/DBUtils 未安装，MySQL 存储不可用")
        return None

    if _store_instance is not None:
        return _store_instance

    host = os.environ.get("MYSQL_HOST")
    if not host:
        logger.info("MYSQL_HOST 未配置，MySQL 存储已禁用")
        return None

    try:
        _store_instance = MySQLStore(
            host=host,
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            user=os.environ.get("MYSQL_USER", "root"),
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ.get("MYSQL_DATABASE", "stock_screener"),
        )
        _store_instance.ensure_tables()
        logger.info(f"MySQL 连接成功: {host}:{os.environ.get('MYSQL_PORT', '3306')}")
        return _store_instance
    except Exception as e:
        logger.warning(f"MySQL 连接失败，降级到 CSV: {e}")
        _store_instance = None
        return None


class MySQLStore:
    """MySQL 持久化层"""

    def __init__(self, host: str, port: int, user: str, password: str, database: str):
        self.pool = PooledDB(
            creator=pymysql,
            maxconnections=5,
            mincached=1,
            maxcached=3,
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )

    def _get_conn(self):
        """获取连接"""
        return self.pool.connection()

    def ensure_tables(self):
        """创建表（幂等操作）"""
        ddl_recommendations = """
        CREATE TABLE IF NOT EXISTS stock_recommendations (
            id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            recommend_date      DATE            NOT NULL,
            code                CHAR(6)         NOT NULL,
            name                VARCHAR(32)     NOT NULL,
            industry            VARCHAR(64)     DEFAULT '',
            signal_level        VARCHAR(32)     NOT NULL,
            market_env          VARCHAR(32)     DEFAULT '',
            total_score         DECIMAL(8,2)    DEFAULT 0,
            daily_confirm_score DECIMAL(8,2)    DEFAULT 0,
            price               DECIMAL(10,3)   NOT NULL,
            weekly_ma20         DECIMAL(10,3)   DEFAULT NULL,
            buy_price           DECIMAL(10,3)   DEFAULT NULL,
            dynamic_stop        DECIMAL(10,3)   DEFAULT NULL,
            first_tp            DECIMAL(10,3)   DEFAULT NULL,
            second_tp_defense   DECIMAL(10,3)   DEFAULT NULL,
            week_return         DECIMAL(8,4)    DEFAULT NULL,
            normalized_drop     DECIMAL(8,4)    DEFAULT NULL,
            volume_ratio        DECIMAL(8,4)    DEFAULT NULL,
            turnover_ratio      DECIMAL(8,4)    DEFAULT NULL,
            weekly_turnover     DECIMAL(8,4)    DEFAULT NULL,
            volume_mode         VARCHAR(8)      DEFAULT NULL,
            prev_20_low         DECIMAL(10,3)   DEFAULT NULL,
            week_low            DECIMAL(10,3)   DEFAULT NULL,
            distance_to_low     DECIMAL(8,4)    DEFAULT NULL,
            position_20         DECIMAL(8,4)    DEFAULT NULL,
            close_position      DECIMAL(8,4)    DEFAULT NULL,
            lower_shadow_ratio  DECIMAL(8,4)    DEFAULT NULL,
            stop_distance_pct   DECIMAL(8,4)    DEFAULT NULL,
            risk_reward_ratio   DECIMAL(8,4)    DEFAULT NULL,
            rsi14               DECIMAL(8,4)    DEFAULT NULL,
            daily_ma5           DECIMAL(10,3)   DEFAULT NULL,
            daily_ma10          DECIMAL(10,3)   DEFAULT NULL,
            daily_ma20          DECIMAL(10,3)   DEFAULT NULL,
            prev_day_high       DECIMAL(10,3)   DEFAULT NULL,
            roe                 DECIMAL(8,2)    DEFAULT NULL,
            debt_ratio          DECIMAL(8,2)    DEFAULT NULL,
            cashflow            DECIMAL(16,2)   DEFAULT NULL,
            revenue_growth      DECIMAL(8,2)    DEFAULT NULL,
            profit_growth       DECIMAL(8,2)    DEFAULT NULL,
            current_price       DECIMAL(10,3)   DEFAULT NULL,
            return_pct          DECIMAL(8,2)    DEFAULT NULL,
            max_profit_pct      DECIMAL(8,2)    DEFAULT NULL,
            max_drawdown_pct    DECIMAL(8,2)    DEFAULT NULL,
            period_high         DECIMAL(10,3)   DEFAULT NULL,
            period_low          DECIMAL(10,3)   DEFAULT NULL,
            holding_days        INT             DEFAULT 0,
            status              VARCHAR(16)     DEFAULT '持仓跟踪',
            hit_stop            TINYINT(1)      DEFAULT 0,
            hit_tp              TINYINT(1)      DEFAULT 0,
            last_tracked_at     DATETIME        DEFAULT NULL,
            created_at          DATETIME        DEFAULT CURRENT_TIMESTAMP,
            updated_at          DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_date_code (recommend_date, code),
            INDEX idx_recommend_date (recommend_date),
            INDEX idx_status (status),
            INDEX idx_signal_level (signal_level)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """

        ddl_snapshots = """
        CREATE TABLE IF NOT EXISTS performance_snapshots (
            id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            report_date     DATE            NOT NULL,
            period_days     INT             NOT NULL DEFAULT 30,
            total_signals   INT             NOT NULL DEFAULT 0,
            win_count       INT             NOT NULL DEFAULT 0,
            loss_count      INT             NOT NULL DEFAULT 0,
            win_rate        DECIMAL(6,2)    DEFAULT NULL,
            avg_return      DECIMAL(8,2)    DEFAULT NULL,
            median_return   DECIMAL(8,2)    DEFAULT NULL,
            max_win         DECIMAL(8,2)    DEFAULT NULL,
            max_loss        DECIMAL(8,2)    DEFAULT NULL,
            details_json    JSON            DEFAULT NULL,
            created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_report_date (report_date, period_days),
            INDEX idx_report_date (report_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(ddl_recommendations)
                cur.execute(ddl_snapshots)
            conn.commit()
        finally:
            conn.close()

    def save_recommendations(self, output_df: pd.DataFrame) -> bool:
        """
        批量存储今日推荐结果。
        使用 INSERT ON DUPLICATE KEY UPDATE 实现幂等。
        """
        if output_df is None or output_df.empty:
            return True

        today_str = datetime.now().strftime("%Y-%m-%d")
        conn = self._get_conn()

        try:
            with conn.cursor() as cur:
                for _, row in output_df.iterrows():
                    values = {"recommend_date": today_str}

                    for cn_col, en_col in COLUMN_MAP.items():
                        val = row.get(cn_col)
                        if val is not None and not _is_nan(val):
                            values[en_col] = _to_python_type(val)
                        else:
                            values[en_col] = None

                    # 确保必填字段
                    if not values.get("code") or not values.get("price"):
                        continue

                    columns = list(values.keys())
                    placeholders = ", ".join(["%s"] * len(columns))
                    col_str = ", ".join(columns)
                    # ON DUPLICATE KEY UPDATE: 更新除主键外的所有字段
                    updates = ", ".join([
                        f"{k} = VALUES({k})" for k in columns
                        if k not in ("recommend_date", "code")
                    ])

                    sql = (
                        f"INSERT INTO stock_recommendations ({col_str}) "
                        f"VALUES ({placeholders}) "
                        f"ON DUPLICATE KEY UPDATE {updates}"
                    )
                    cur.execute(sql, [values[c] for c in columns])

            conn.commit()
            logger.info(f"[MySQL] 保存 {len(output_df)} 条推荐记录")
            return True
        except Exception as e:
            conn.rollback()
            logger.warning(f"[MySQL] save_recommendations 失败: {e}")
            return False
        finally:
            conn.close()

    def update_tracking(self, report_df: pd.DataFrame) -> bool:
        """
        更新追踪字段（当前价格、收益率、状态等）。
        report_df 来自 weekly_performance_review() 的输出。
        """
        if report_df is None or report_df.empty:
            return True

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = self._get_conn()

        try:
            with conn.cursor() as cur:
                for _, row in report_df.iterrows():
                    code = str(row.get("代码", "")).zfill(6)
                    rec_date = row.get("推荐日期", "")

                    if not code or not rec_date:
                        continue

                    cur.execute("""
                        UPDATE stock_recommendations SET
                            current_price = %s,
                            return_pct = %s,
                            max_profit_pct = %s,
                            max_drawdown_pct = %s,
                            period_high = %s,
                            period_low = %s,
                            holding_days = %s,
                            status = %s,
                            hit_stop = %s,
                            hit_tp = %s,
                            last_tracked_at = %s
                        WHERE code = %s AND recommend_date = %s
                    """, (
                        _safe_float(row.get("当前价格")),
                        _safe_float(row.get("收益率(%)")),
                        _safe_float(row.get("最大浮盈(%)")),
                        _safe_float(row.get("最大回撤(%)")),
                        _safe_float(row.get("期间最高价")),
                        _safe_float(row.get("期间最低价")),
                        int(row.get("持仓天数", 0)) if not _is_nan(row.get("持仓天数")) else 0,
                        row.get("当前状态", "数据缺失"),
                        1 if row.get("是否触发止损") == "是" else 0,
                        1 if row.get("是否达止盈") == "是" else 0,
                        now_str,
                        code,
                        rec_date,
                    ))

            conn.commit()
            logger.info(f"[MySQL] 更新 {len(report_df)} 条追踪记录")
            return True
        except Exception as e:
            conn.rollback()
            logger.warning(f"[MySQL] update_tracking 失败: {e}")
            return False
        finally:
            conn.close()

    def get_monthly_performance(self, days: int = 30) -> Optional[Dict[str, Any]]:
        """
        查询近 N 天推荐记录，计算胜率和涨跌幅统计。

        返回:
            {
                "report_date": "2026-08-28",
                "period_days": 30,
                "total_signals": 15,
                "valid_signals": 12,
                "win_count": 8,
                "loss_count": 4,
                "win_rate": 66.67,
                "avg_return": 3.25,
                "median_return": 2.10,
                "max_win": 12.5,
                "max_loss": -6.3,
                "details": [...],
                "by_signal_level": {...}
            }
        """
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT code, name, recommend_date, price, current_price,
                           return_pct, max_profit_pct, max_drawdown_pct,
                           status, signal_level, total_score, holding_days,
                           industry, dynamic_stop, first_tp, risk_reward_ratio
                    FROM stock_recommendations
                    WHERE recommend_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                    ORDER BY recommend_date DESC, total_score DESC
                """, (days,))
                rows = cur.fetchall()

            if not rows:
                return None

            # 分离有追踪数据的和没有的
            tracked = [r for r in rows if r["return_pct"] is not None]
            total_signals = len(rows)
            valid_signals = len(tracked)

            if valid_signals == 0:
                # 有推荐但尚无追踪数据
                return {
                    "report_date": datetime.now().strftime("%Y-%m-%d"),
                    "period_days": days,
                    "total_signals": total_signals,
                    "valid_signals": 0,
                    "win_count": 0,
                    "loss_count": 0,
                    "win_rate": 0,
                    "avg_return": 0,
                    "median_return": 0,
                    "max_win": 0,
                    "max_loss": 0,
                    "details": [_format_detail(r) for r in rows],
                    "by_signal_level": {},
                }

            returns = [float(r["return_pct"]) for r in tracked]
            wins = [r for r in returns if r > 0]
            losses = [r for r in returns if r <= 0]

            stats = {
                "report_date": datetime.now().strftime("%Y-%m-%d"),
                "period_days": days,
                "total_signals": total_signals,
                "valid_signals": valid_signals,
                "win_count": len(wins),
                "loss_count": len(losses),
                "win_rate": round(len(wins) / valid_signals * 100, 2),
                "avg_return": round(sum(returns) / len(returns), 2),
                "median_return": round(statistics.median(returns), 2),
                "max_win": round(max(returns), 2) if returns else 0,
                "max_loss": round(min(returns), 2) if returns else 0,
                "details": [_format_detail(r) for r in rows],
                "by_signal_level": self._group_by_signal_level(tracked),
            }
            return stats
        except Exception as e:
            logger.warning(f"[MySQL] get_monthly_performance 失败: {e}")
            return None
        finally:
            conn.close()

    def save_performance_snapshot(self, stats: Dict[str, Any]) -> bool:
        """保存月度绩效快照"""
        conn = self._get_conn()
        try:
            # 精简 details 用于 JSON 存储
            details_for_json = []
            for d in stats.get("details", [])[:50]:  # 最多存50条
                details_for_json.append({
                    "code": d.get("code", ""),
                    "name": d.get("name", ""),
                    "return_pct": d.get("return_pct"),
                    "status": d.get("status", ""),
                    "signal_level": d.get("signal_level", ""),
                })

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO performance_snapshots
                        (report_date, period_days, total_signals, win_count, loss_count,
                         win_rate, avg_return, median_return, max_win, max_loss, details_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        total_signals = VALUES(total_signals),
                        win_count = VALUES(win_count),
                        loss_count = VALUES(loss_count),
                        win_rate = VALUES(win_rate),
                        avg_return = VALUES(avg_return),
                        median_return = VALUES(median_return),
                        max_win = VALUES(max_win),
                        max_loss = VALUES(max_loss),
                        details_json = VALUES(details_json)
                """, (
                    stats["report_date"],
                    stats["period_days"],
                    stats["total_signals"],
                    stats["win_count"],
                    stats["loss_count"],
                    stats.get("win_rate"),
                    stats.get("avg_return"),
                    stats.get("median_return"),
                    stats.get("max_win"),
                    stats.get("max_loss"),
                    json.dumps(details_for_json, ensure_ascii=False),
                ))
            conn.commit()
            logger.info("[MySQL] 保存绩效快照")
            return True
        except Exception as e:
            conn.rollback()
            logger.warning(f"[MySQL] save_performance_snapshot 失败: {e}")
            return False
        finally:
            conn.close()

    def _group_by_signal_level(self, tracked_rows: List[Dict]) -> Dict[str, Dict]:
        """按信号等级分组统计"""
        groups: Dict[str, List[float]] = {}
        for r in tracked_rows:
            level = r.get("signal_level", "UNKNOWN")
            ret = float(r["return_pct"])
            groups.setdefault(level, []).append(ret)

        result = {}
        for level, rets in groups.items():
            wins = [r for r in rets if r > 0]
            result[level] = {
                "count": len(rets),
                "win_rate": round(len(wins) / len(rets) * 100, 1) if rets else 0,
                "avg_return": round(sum(rets) / len(rets), 2) if rets else 0,
            }
        return result


# ============ 辅助函数 ============

def _is_nan(val) -> bool:
    """检查值是否为 NaN/None"""
    if val is None:
        return True
    if isinstance(val, float) and np.isnan(val):
        return True
    try:
        if pd.isna(val):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _to_python_type(val):
    """将 numpy/pandas 类型转为 Python 原生类型"""
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return float(val)
    if isinstance(val, np.bool_):
        return bool(val)
    if isinstance(val, pd.Timestamp):
        return val.strftime("%Y-%m-%d")
    return val


def _safe_float(val) -> Optional[float]:
    """安全转换为 float，失败返回 None"""
    if _is_nan(val):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _format_detail(row: Dict) -> Dict:
    """格式化查询结果行用于输出"""
    return {
        "code": row.get("code", ""),
        "name": row.get("name", ""),
        "recommend_date": str(row.get("recommend_date", "")),
        "price": float(row["price"]) if row.get("price") else None,
        "current_price": float(row["current_price"]) if row.get("current_price") else None,
        "return_pct": float(row["return_pct"]) if row.get("return_pct") is not None else None,
        "max_profit_pct": float(row["max_profit_pct"]) if row.get("max_profit_pct") is not None else None,
        "max_drawdown_pct": float(row["max_drawdown_pct"]) if row.get("max_drawdown_pct") is not None else None,
        "status": row.get("status", ""),
        "signal_level": row.get("signal_level", ""),
        "total_score": float(row["total_score"]) if row.get("total_score") is not None else None,
        "holding_days": int(row["holding_days"]) if row.get("holding_days") is not None else 0,
        "industry": row.get("industry", ""),
    }
