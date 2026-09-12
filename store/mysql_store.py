"""MySQL 持久化模块：推荐记录落库 + 周度表现追踪。

设计哲学与 notify/feishu.py 一致：
- 通过环境变量配置（MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE），
  未配置时 is_configured() 返回 False，所有写操作静默跳过，不影响选股主流程；
- pymysql 为可选依赖，未安装时同样静默跳过（CI 未装依赖不至于让选股失败）；
- 首次使用时自动执行 CREATE TABLE IF NOT EXISTS（幂等），无需手动导入 schema.sql
  （schema.sql 保留为表结构文档与手动建库参考，二者内容保持一致）。

追踪口径：
- 每条推荐记录最多追踪 TRACK_MAX_WEEKS=4 周（约一个月），且推荐日起 TRACK_MAX_AGE_DAYS=31
  天后强制退出追踪（双保险，防止周任务中断导致超期）；
- 收益值 = 最新收盘价 - 推荐时收盘价；收益率 = 收益值 / 推荐时收盘价 × 100%。

信号归因：get_attribution_rows() 返回近 N 天推荐 × 周度追踪的明细，
由 run_monthly_attribution.py 聚合为各信号维度（等级/底背离/市场环境/持有周次）的
胜率与收益统计，飞书推送月报——用真实追踪数据反哺选股信号质量评估。
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger("mysql")

try:
    import pymysql
    _PYMYSQL_AVAILABLE = True
except ImportError:
    pymysql = None
    _PYMYSQL_AVAILABLE = False

# 追踪窗口：推荐日起 31 天内、最多 4 次周度追踪（≈ 一个月）
TRACK_MAX_AGE_DAYS = 31
TRACK_MAX_WEEKS = 4

_ENV_KEYS = ("MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE")

# 与 schema.sql 保持一致（CREATE IF NOT EXISTS 幂等，每次连接执行一次即可）
_DDL = (
    """
CREATE TABLE IF NOT EXISTS stock_recommendation (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    rec_date         DATE            NOT NULL COMMENT '推荐日期（信号对应的交易日）',
    code             VARCHAR(8)      NOT NULL COMMENT '股票代码（6位数字）',
    name             VARCHAR(32)     NOT NULL COMMENT '股票名称',
    rec_close        DECIMAL(10, 3)  NOT NULL COMMENT '推荐时收盘价（元）',
    score            DECIMAL(5, 1)   NULL COMMENT '技术评分（与等级同源定级）',
    grade            VARCHAR(2)      NULL COMMENT '信号等级 A/B/C/D（原始技术分定级，无升档）',
    daily_score      DECIMAL(5, 1)   NULL COMMENT '日线基础评分',
    rsi              DECIMAL(5, 1)   NULL COMMENT 'RSI14',
    rsi7             DECIMAL(5, 1)   NULL COMMENT 'RSI7',
    rsi21            DECIMAL(5, 1)   NULL COMMENT 'RSI21',
    vol_ratio        DECIMAL(8, 2)   NULL COMMENT '量比（对20日均量）',
    turnover_ratio   DECIMAL(8, 2)   NULL COMMENT '换手比（展示用）',
    stop_loss        DECIMAL(10, 3)  NULL COMMENT '止损价（2×ATR 或固定5%）',
    take_profit      DECIMAL(10, 3)  NULL COMMENT '止盈价（固定10%）',
    rr_ratio         DECIMAL(6, 2)   NULL COMMENT '风险收益比',
    market_env       VARCHAR(16)      NULL COMMENT '市场环境 bull/bear/neutral/unknown',
    has_divergence   TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否底背离（双低点算法）1/0',
    signals_hit      VARCHAR(255)    NULL COMMENT '入选依据（实际触发的技术条件，逗号分隔）',
    fund_status      VARCHAR(16)     NULL COMMENT '财务核验 verified/partial/missing',
    weekly_status    VARCHAR(16)     NULL COMMENT '周线核验 confirmed/unverified/disabled',
    rec_tier         VARCHAR(8)      NULL COMMENT '推荐层级 formal/pending',
    missing_tags     VARCHAR(255)    NULL COMMENT '缺失项标签（逗号分隔）',
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_rec_date_code (rec_date, code),
    KEY idx_code (code),
    KEY idx_rec_date (rec_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每日选股推荐记录'
""",
    """
CREATE TABLE IF NOT EXISTS stock_tracking (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    rec_id        BIGINT UNSIGNED NOT NULL COMMENT '关联 stock_recommendation.id',
    rec_date      DATE            NOT NULL COMMENT '推荐日期（冗余，便于直接查询）',
    code          VARCHAR(8)      NOT NULL COMMENT '股票代码（冗余）',
    week_no       TINYINT         NOT NULL COMMENT '第几次追踪（1起，最多4）',
    track_date    DATE            NOT NULL COMMENT '本次追踪执行日期',
    close_date    DATE            NOT NULL COMMENT '收盘价对应的实际交易日',
    close_price   DECIMAL(10, 3)  NOT NULL COMMENT '最新收盘价（元）',
    return_value  DECIMAL(10, 3)  NOT NULL COMMENT '收益值 = close_price - 推荐时收盘价（元）',
    return_pct    DECIMAL(8, 3)   NOT NULL COMMENT '收益率 = return_value / 推荐时收盘价 × 100（%）',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_rec_week (rec_id, week_no),
    KEY idx_code_track (code, track_date),
    KEY idx_track_date (track_date),
    CONSTRAINT fk_tracking_rec FOREIGN KEY (rec_id) REFERENCES stock_recommendation (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='推荐个股周度表现追踪'
""",
)

# 存量表补列（幂等）：CREATE TABLE IF NOT EXISTS 不会给已存在的表加新列，
# 首次升级到「荐股质量分层」版本时按 information_schema 探测后 ALTER 补齐
_RECOMMENDATION_ALTERS = {
    "signals_hit": "ADD COLUMN signals_hit VARCHAR(255) NULL COMMENT '入选依据（实际触发的技术条件，逗号分隔）' AFTER has_divergence",
    "fund_status": "ADD COLUMN fund_status VARCHAR(16) NULL COMMENT '财务核验 verified/partial/missing' AFTER signals_hit",
    "weekly_status": "ADD COLUMN weekly_status VARCHAR(16) NULL COMMENT '周线核验 confirmed/unverified/disabled' AFTER fund_status",
    "rec_tier": "ADD COLUMN rec_tier VARCHAR(8) NULL COMMENT '推荐层级 formal/pending' AFTER weekly_status",
    "missing_tags": "ADD COLUMN missing_tags VARCHAR(255) NULL COMMENT '缺失项标签（逗号分隔）' AFTER rec_tier",
}

_tables_ready = False  # 进程内只确保一次建表


def is_configured() -> bool:
    """MySQL 环境变量是否配置齐全且 pymysql 可用。"""
    return _PYMYSQL_AVAILABLE and all(os.environ.get(k) for k in _ENV_KEYS)


def _parse_port() -> int:
    """解析 MYSQL_PORT：未设置或为空字符串时回退默认 3306。

    注意：GitHub Actions 引用不存在的 secret（${{ secrets.MYSQL_PORT }}）会注入
    空字符串而非缺失该变量，os.environ.get(key, default) 此时返回 ""，
    int("") 直接抛 ValueError —— 这正是线上日志
    "invalid literal for int() with base 10: ''" 的根因。
    """
    raw = (os.environ.get("MYSQL_PORT") or "").strip()
    if not raw:
        return 3306
    try:
        return int(raw)
    except ValueError:
        logger.warning("MYSQL_PORT=%r 无法解析为整数，回退默认端口 3306", raw)
        return 3306


def _connect():
    """建立连接（autocommit）。调用前需确保 is_configured()。"""
    return pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=_parse_port(),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"],
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
    )


def _ensure_tables(conn) -> None:
    global _tables_ready
    if _tables_ready:
        return
    with conn.cursor() as cur:
        for stmt in _DDL:
            cur.execute(stmt)
        # 存量表补列迁移（幂等）
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stock_recommendation'"
        )
        existing = {row[0] for row in cur.fetchall()}
        for col, alter in _RECOMMENDATION_ALTERS.items():
            if col not in existing:
                cur.execute(f"ALTER TABLE stock_recommendation {alter}")
                logger.info("stock_recommendation 已补充列: %s", col)
    _tables_ready = True


def _f(v: Any) -> Optional[float]:
    """numpy 标量安全转 Python float（PyMySQL 无法序列化 numpy 类型）；缺失返回 None。"""
    if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
        return None
    return float(v)


def save_recommendations(df: Optional[pd.DataFrame]) -> int:
    """将选股结果写入 stock_recommendation，(rec_date, code) 重复时忽略。返回新插入行数。"""
    if df is None or df.empty:
        return 0
    if not is_configured():
        logger.info("MySQL 未配置（MYSQL_HOST/USER/PASSWORD/DATABASE），跳过推荐结果落库")
        return 0

    rows = []
    for _, r in df.iterrows():
        rows.append((
            str(r["date"]), str(r["code"]), str(r["name"]), _f(r["close"]),
            _f(r.get("score")), str(r.get("grade", "") or "") or None,
            _f(r.get("daily_score")), _f(r.get("rsi")), _f(r.get("rsi7")), _f(r.get("rsi21")),
            _f(r.get("vol_ratio")), _f(r.get("turnover_ratio")),
            _f(r.get("stop_loss")), _f(r.get("take_profit")), _f(r.get("rr_ratio")),
            str(r.get("market_env", "") or "") or None,
            1 if bool(r.get("has_divergence")) else 0,
            str(r.get("signals_hit", "") or "") or None,
            str(r.get("fund_status", "") or "") or None,
            str(r.get("weekly_status", "") or "") or None,
            str(r.get("tier", "formal") or "formal") or None,
            str(r.get("missing_tags", "") or "") or None,
        ))

    sql = """
        INSERT IGNORE INTO stock_recommendation
        (rec_date, code, name, rec_close, score, grade, daily_score, rsi, rsi7, rsi21,
         vol_ratio, turnover_ratio, stop_loss, take_profit, rr_ratio, market_env, has_divergence,
         signals_hit, fund_status, weekly_status, rec_tier, missing_tags)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    try:
        conn = _connect()
        try:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                inserted = cur.executemany(sql, rows)
            logger.info("推荐结果已落库：新增 %d 条（共提交 %d 条，重复自动忽略）", inserted or 0, len(rows))
            return int(inserted or 0)
        finally:
            conn.close()
    except Exception as e:
        # 落库失败不应让选股主流程失败
        logger.warning("推荐结果落库失败（不影响选股流程）: %s", e)
        return 0


def get_active_recommendations(max_age_days: int = TRACK_MAX_AGE_DAYS,
                               max_weeks: int = TRACK_MAX_WEEKS) -> list[dict]:
    """查询仍在追踪期内的推荐记录：推荐日起 max_age_days 天内，且已追踪次数 < max_weeks。"""
    if not is_configured():
        logger.info("MySQL 未配置，无法执行周度追踪")
        return []

    sql = """
        SELECT r.id, r.rec_date, r.code, r.name, r.rec_close,
               COALESCE(MAX(t.week_no), 0) AS tracked_weeks
        FROM stock_recommendation r
        LEFT JOIN stock_tracking t ON t.rec_id = r.id
        WHERE r.rec_date >= CURDATE() - INTERVAL %s DAY
        GROUP BY r.id, r.rec_date, r.code, r.name, r.rec_close
        HAVING tracked_weeks < %s
        ORDER BY r.rec_date DESC, r.id
    """
    try:
        conn = _connect()
        try:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(sql, (int(max_age_days), int(max_weeks)))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        logger.warning("查询追踪期推荐记录失败: %s", e)
        return []


def save_tracking(rec_id: int, rec_date: Any, code: str, week_no: int,
                  close_date: str, close_price: float, rec_close: float) -> bool:
    """写入一条周度追踪记录。收益值/收益率在写入前计算；(rec_id, week_no) 重复时忽略。"""
    if not is_configured():
        return False

    return_value = round(float(close_price) - float(rec_close), 3)
    return_pct = round(return_value / float(rec_close) * 100, 3) if rec_close else 0.0
    track_date = date.today().strftime("%Y-%m-%d")
    rec_date_str = rec_date.strftime("%Y-%m-%d") if isinstance(rec_date, (date, datetime)) else str(rec_date)

    sql = """
        INSERT IGNORE INTO stock_tracking
        (rec_id, rec_date, code, week_no, track_date, close_date, close_price, return_value, return_pct)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    try:
        conn = _connect()
        try:
            conn.ping(reconnect=True)  # 行情拉取耗时后连接可能已被服务端关闭
            _ensure_tables(conn)
            with conn.cursor() as cur:
                inserted = cur.execute(sql, (
                    int(rec_id), rec_date_str, str(code), int(week_no),
                    track_date, str(close_date), round(float(close_price), 3),
                    return_value, return_pct,
                ))
            return bool(inserted)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("追踪记录落库失败(%s 第%d周): %s", code, week_no, e)
        return False


def get_attribution_rows(days: int = 90) -> list[dict]:
    """查询近 N 天内全部推荐记录的周度追踪明细（含推荐侧信号字段），供信号归因分析。

    返回逐条明细（一条 = 某推荐的第某周观测），字段：
    rec_date / code / name / grade / has_divergence / market_env / score / daily_score
    / week_no / return_pct；聚合（每条推荐的最新收益、峰值收益等）由调用方用 pandas 完成。
    仅返回至少有一次追踪记录的推荐（尚未被追踪的新推荐不参与归因）。
    """
    if not is_configured():
        logger.info("MySQL 未配置，无法执行信号归因查询")
        return []

    sql = """
        SELECT r.id, r.rec_date, r.code, r.name, r.grade, r.has_divergence, r.market_env,
               r.score, r.daily_score, t.week_no, t.return_pct
        FROM stock_recommendation r
        JOIN stock_tracking t ON t.rec_id = r.id
        WHERE r.rec_date >= CURDATE() - INTERVAL %s DAY
        ORDER BY r.id, t.week_no
    """
    try:
        conn = _connect()
        try:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(sql, (int(days),))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        logger.warning("信号归因数据查询失败: %s", e)
        return []
