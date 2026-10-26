"""MySQL 持久化模块：推荐记录落库 + 周度表现追踪。

设计哲学与 notify/feishu.py 一致：
- 通过环境变量配置（MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE），
  未配置时 is_configured() 返回 False，所有写操作静默跳过，不影响选股主流程；
- pymysql 为可选依赖，未安装时同样静默跳过（CI 未装依赖不至于让选股失败）；
- 首次使用时自动执行 CREATE TABLE IF NOT EXISTS（幂等），无需手动导入 schema.sql
  （schema.sql 保留为表结构文档与手动建库参考，二者内容保持一致）。

追踪口径：
- 固定推荐后第5/10/15/20个市场交易日；推荐后60日内允许补跑；
- 同一推荐、收盘日期唯一；历史重复保留并标记，不以错误周号判定完成；
- 收益值 = 目标日收盘价 - 推荐时收盘价；收益率 = 收益值 / 推荐价 × 100%。

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

# 查询窗口保留补跑余量；实际收益期限固定，不使用任意最新价。
# 2026-10-13 执行中期化：观测期限从「止于第20交易日」扩展到 60 个交易日
# （≈101 个日历天），故补跑窗口放到 115 天——覆盖 60 日窗口 + 长假/周任务余量。
TRACK_MAX_AGE_DAYS = 115
# week_no 语义 = 持有交易日数 / 5（1,2,3,4,8,12），上限随最长观测期限（60 日）走。
TRACK_MAX_WEEKS = 12
# 固定观测期限：5/10/15/20 为既有口径（保持历史可比），40/60 为 2026-10-13
# 执行中期化新增——生产建议持有窗口已放到 40~60 个交易日（StrategyConfig
# HOLD_DAYS_HINT_*），没有 40/60 观测则新窗口的收益无法被度量与归因。
TRACK_HORIZONS = (5, 10, 15, 20, 40, 60)

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
    stop_loss        DECIMAL(10, 3)  NULL COMMENT '止损价（max(3×ATR, 固定15%)，仅展示不参与否决）',
    take_profit      DECIMAL(10, 3)  NULL COMMENT '止盈价（固定+15%，仅展示不参与否决；突破/长期另有口径）',
    rr_ratio         DECIMAL(6, 2)   NULL COMMENT '风险收益比',
    market_env       VARCHAR(16)      NULL COMMENT '市场环境 bull/bear/neutral/unknown',
    has_divergence   TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '是否底背离（双低点算法）1/0',
    signals_hit      TEXT            NULL COMMENT '入选依据（实际触发的技术条件，逗号分隔）',
    fund_status      VARCHAR(16)     NULL COMMENT '财务核验 verified/partial/missing',
    weekly_status    VARCHAR(16)     NULL COMMENT '周线核验 confirmed/unverified/disabled',
    rec_tier         VARCHAR(8)      NULL COMMENT '推荐层级 formal/pending',
    strategy         VARCHAR(16)     NOT NULL DEFAULT 'bottom_fishing' COMMENT '策略来源 bottom_fishing/volume_breakout/longterm_quality',
    missing_tags     TEXT            NULL COMMENT '缺失项标签（逗号分隔）',
    score_weights    VARCHAR(24)     NULL COMMENT '综合分权重快照 质量/估值/技术，例 0.45/0.30/0.25',
    stop_rule        VARCHAR(32)     NULL COMMENT '止损失口径快照，例 atr3x_floor15 / fixed15',
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_rec_date_code_strategy (rec_date, code, strategy),
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
    week_no       TINYINT         NOT NULL COMMENT '第几次追踪（=持有交易日数/5：1,2,3,4,8,12）',
    track_date    DATE            NOT NULL COMMENT '本次追踪执行日期',
    close_date    DATE            NOT NULL COMMENT '收盘价对应的实际交易日',
    holding_trade_days TINYINT UNSIGNED NULL COMMENT '推荐后市场交易日数；旧记录未知为NULL',
    legacy_duplicate TINYINT NOT NULL DEFAULT 0 COMMENT '保留的历史同日重复记录',
    unique_close_date DATE GENERATED ALWAYS AS (CASE WHEN legacy_duplicate = 0 THEN close_date ELSE NULL END) STORED,
    close_price   DECIMAL(10, 3)  NOT NULL COMMENT '目标交易日收盘价（元）',
    return_value  DECIMAL(10, 3)  NOT NULL COMMENT '收益值 = close_price - 推荐时收盘价（元）',
    return_pct    DECIMAL(8, 3)   NOT NULL COMMENT '累计收益率 = return_value / 推荐时收盘价 × 100（%）',
    weekly_return_pct DECIMAL(8, 3) NULL COMMENT '周收益率 = (当日收盘价 - 5日前收盘价) / 5日前收盘价 × 100（%）',
    monthly_return_pct DECIMAL(8, 3) NULL COMMENT '月收益率 = (当日收盘价 - 20日前收盘价) / 20日前收盘价 × 100（%）',
    created_at    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    PRIMARY KEY (id),
    KEY idx_rec_week (rec_id, week_no),
    UNIQUE KEY uk_rec_close_date (rec_id, unique_close_date),
    UNIQUE KEY uk_rec_horizon (rec_id, holding_trade_days),
    KEY idx_code_track (code, track_date),
    KEY idx_track_date (track_date),
    CONSTRAINT fk_tracking_rec FOREIGN KEY (rec_id) REFERENCES stock_recommendation (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='推荐个股周度表现追踪'
""",
)

# 存量表补列（幂等）：CREATE TABLE IF NOT EXISTS 不会给已存在的表加新列，
# 首次升级到「荐股质量分层」版本时按 information_schema 探测后 ALTER 补齐
_RECOMMENDATION_ALTERS = {
    "signals_hit": "ADD COLUMN signals_hit TEXT NULL COMMENT '入选依据（实际触发的技术条件，逗号分隔）' AFTER has_divergence",
    "fund_status": "ADD COLUMN fund_status VARCHAR(16) NULL COMMENT '财务核验 verified/partial/missing' AFTER signals_hit",
    "weekly_status": "ADD COLUMN weekly_status VARCHAR(16) NULL COMMENT '周线核验 confirmed/unverified/disabled' AFTER fund_status",
    "rec_tier": "ADD COLUMN rec_tier VARCHAR(8) NULL COMMENT '推荐层级 formal/pending' AFTER weekly_status",
    "strategy": "ADD COLUMN strategy VARCHAR(16) NOT NULL DEFAULT 'bottom_fishing' COMMENT '策略来源 bottom_fishing/volume_breakout' AFTER rec_tier",
    "missing_tags": "ADD COLUMN missing_tags TEXT NULL COMMENT '缺失项标签（逗号分隔）' AFTER rec_tier",
    # 2026-10-26 新增口径快照列：同一列在不同日期代表不同规则，回看必须可判别。
    # score 权重 09-28 前为 0.50/0.35/0.15、之后 0.45/0.30/0.25；
    # stop_loss 10-07 前为纯 3×ATR、之后为 max(3×ATR,15%)。
    "score_weights": "ADD COLUMN score_weights VARCHAR(24) NULL COMMENT '综合分权重快照 质量/估值/技术，例 0.45/0.30/0.25' AFTER missing_tags",
    "stop_rule": "ADD COLUMN stop_rule VARCHAR(32) NULL COMMENT '止损失口径快照，例 atr3x_floor15 / fixed15' AFTER score_weights",
}

# ===== 文本列容量升级（幂等，非破坏，不改任何已有数据）=====
# 2026-10-13 修复生产报错：(1406, "Data too long for column 'signals_hit' at row 1")。
#
# 【根因】signals_hit / missing_tags 原为 VARCHAR(255)。荐股证据链逐年变长
# （「优质低估低位」+「自身历史PE分位16%（250个样本）」+「行业估值分位PE16%/PB27%」
# +「近期有效底部2026-09-09，距底+7.8%」+「止跌中确认」+ 六项经营证据明细
# +「右侧雏形·站上MA20（仍在MA60下方）」），单条实测已达 112~147 字符，
# 且仍在增长；一旦超过列定义（或存量库列定义短于代码 DDL），MySQL 直接
# **整批 INSERT 失败** —— 当日全部推荐一条都落不了库。
#
# 【为什么升级为 TEXT 而不是截断】入选依据是事后复盘「为什么推了这只」的
# 唯一凭据，也是周报/归因的人读证据链。截断会让最有价值的部分（经营证据明细）
# 静默丢失，而 TEXT 容量 64KB 远超需求，改动不涉及任何数据与索引。
#
# 【三重防御】1) 新建表 DDL 直接用 TEXT；2) 存量表幂等 MODIFY 到 TEXT；
# 3) 若 ALTER 因权限失败，降级为按**实际列容量**截断并 warning——
#    保证「宁可不完整也要落库」，绝不让整批推荐丢失（见 _clip_to_capacity）。
_TEXT_COLUMN_UPGRADES = {
    "signals_hit": "MODIFY COLUMN signals_hit TEXT NULL COMMENT '入选依据（实际触发的技术条件，逗号分隔）'",
    "missing_tags": "MODIFY COLUMN missing_tags TEXT NULL COMMENT '缺失项标签（逗号分隔）'",
}
_TEXT_TYPES = ("text", "mediumtext", "longtext", "tinytext")
# 列实际字符容量缓存：VARCHAR 为 CHARACTER_MAXIMUM_LENGTH；TEXT 记 None 表示不需截断。
_text_column_capacity: dict = {}

# 追踪表补列：周收益率、月收益率
_TRACKING_ALTERS = {
    "weekly_return_pct": "ADD COLUMN weekly_return_pct DECIMAL(8, 3) NULL COMMENT '周收益率 = (当日收盘价 - 5日前收盘价) / 5日前收盘价 × 100（%）' AFTER return_pct",
    "monthly_return_pct": "ADD COLUMN monthly_return_pct DECIMAL(8, 3) NULL COMMENT '月收益率 = (当日收盘价 - 20日前收盘价) / 20日前收盘价 × 100（%）' AFTER weekly_return_pct",
}

# 策略来源合法值（落库前校验，防止脏数据污染按策略分列的追踪/归因）
STRATEGY_BOTTOM_FISHING = "bottom_fishing"
STRATEGY_VOLUME_BREAKOUT = "volume_breakout"
# 长期优质低估低位（run_longterm.py）：与 bottom_fishing 同走 quality_value 荐股资格，
# 但质量/现金流/持有期口径更严（5年、ROE稳定性、30~90天），是**独立 sleeve**。
# 2026-10-13 登记：此前 run_longterm.py 传 "longterm_quality" 落库，但该值不在
# KNOWN_STRATEGIES 里 → save_recommendations 静默改写成 "bottom_fishing" 并只打一条
# warning。后果是长期策略的推荐与生产 sleeve 混在同一 strategy 下：
#   · 唯一键 (rec_date, code, strategy) 冲突 → 同日同股只能存一条，长期结果被丢弃；
#   · 周度追踪与信号归因按 strategy 分列时无法区分两种口径，胜率被混算。
# 恰好 16 字符，与 stock_recommendation.strategy VARCHAR(16) 上限一致。
STRATEGY_LONGTERM_QUALITY = "longterm_quality"
KNOWN_STRATEGIES = (STRATEGY_BOTTOM_FISHING, STRATEGY_VOLUME_BREAKOUT,
                    STRATEGY_LONGTERM_QUALITY)

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
        # ===== 文本列容量升级 + 实际容量探测（2026-10-13）=====
        # 幂等：已是 TEXT 就跳过 MODIFY。无论升级成功与否，都记录**实际**列容量
        # 供落库前截断兜底——存量库的列可能短于代码 DDL，不能假设它就是 255。
        cur.execute(
            "SELECT COLUMN_NAME, COLUMN_TYPE, CHARACTER_MAXIMUM_LENGTH "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stock_recommendation' "
            "AND COLUMN_NAME IN ('signals_hit', 'missing_tags')"
        )
        cols = {str(r[0]): (str(r[1]).lower(), r[2]) for r in cur.fetchall()}
        for col, alter in _TEXT_COLUMN_UPGRADES.items():
            if col not in cols:
                continue
            col_type, max_len = cols[col]
            if col_type in _TEXT_TYPES:
                _text_column_capacity[col] = None      # TEXT：无需截断
                continue
            _text_column_capacity[col] = int(max_len) if max_len is not None else 255
            try:
                cur.execute(f"ALTER TABLE stock_recommendation {alter}")
                _text_column_capacity[col] = None      # 升级成功 → 无需截断
                logger.info("stock_recommendation.%s 已由 %s(%s) 升级为 TEXT（证据链超长，"
                            "整批 INSERT 会失败）", col, col_type.upper(), max_len)
            except Exception as e:  # noqa: BLE001 无 ALTER 权限时降级为截断，不阻断落库
                logger.warning("升级 %s 为 TEXT 失败（将按实际容量 %s 截断后落库）: %s",
                               col, max_len, e)
        # 存量表唯一键升级（幂等）：(rec_date, code) → (rec_date, code, strategy)，
        # 允许同一股票同日被两套策略分别推荐并存；须在 strategy 列补齐之后执行。
        # 历史存量行 strategy 回填默认值 'bottom_fishing'，与原唯一键语义兼容。
        cur.execute(
            "SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stock_recommendation'"
        )
        indexes = {row[0] for row in cur.fetchall()}
        if "uk_rec_date_code" in indexes and "uk_rec_date_code_strategy" not in indexes:
            cur.execute(
                "ALTER TABLE stock_recommendation "
                "DROP INDEX uk_rec_date_code, "
                "ADD UNIQUE KEY uk_rec_date_code_strategy (rec_date, code, strategy)"
            )
            logger.info("stock_recommendation 唯一键已升级为 (rec_date, code, strategy)")
    _ensure_tracking_schema(conn)
    _tables_ready = True


def _ensure_tracking_schema(conn) -> None:
    """非破坏迁移：同日历史重复保留原值，仅最早一行参与日期唯一约束。"""
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK('stock_tracking_schema_v2', 30)")
        if cur.fetchone()[0] != 1:
            raise RuntimeError("无法获取追踪表迁移锁")
        try:
            cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stock_tracking'")
            columns = {r[0] for r in cur.fetchall()}
            additions = {
                "holding_trade_days": "TINYINT UNSIGNED NULL COMMENT '推荐后市场交易日数；旧记录未知为NULL'",
                "legacy_duplicate": "TINYINT NOT NULL DEFAULT 0 COMMENT '保留的历史同日重复记录'",
                "unique_close_date": "DATE GENERATED ALWAYS AS (CASE WHEN legacy_duplicate = 0 THEN close_date ELSE NULL END) STORED",
            }
            for name, definition in additions.items():
                if name not in columns:
                    cur.execute(f"ALTER TABLE stock_tracking ADD COLUMN {name} {definition}")
            # 补列：周收益率、月收益率
            for col, alter in _TRACKING_ALTERS.items():
                if col not in columns:
                    cur.execute(f"ALTER TABLE stock_tracking {alter}")
                    logger.info("stock_tracking 已补充列: %s", col)
            cur.execute("SELECT DISTINCT INDEX_NAME FROM information_schema.STATISTICS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stock_tracking'")
            indexes = {r[0] for r in cur.fetchall()}
            if "uk_rec_close_date" not in indexes:
                cur.execute("UPDATE stock_tracking t JOIN stock_tracking earlier "
                            "ON earlier.rec_id = t.rec_id AND earlier.close_date = t.close_date "
                            "AND earlier.id < t.id SET t.legacy_duplicate = 1")
                cur.execute("ALTER TABLE stock_tracking ADD UNIQUE KEY "
                            "uk_rec_close_date (rec_id, unique_close_date)")
            if "uk_rec_horizon" not in indexes:
                cur.execute("ALTER TABLE stock_tracking ADD UNIQUE KEY "
                            "uk_rec_horizon (rec_id, holding_trade_days)")
            if "uk_rec_week" in indexes:
                cur.execute("ALTER TABLE stock_tracking DROP INDEX uk_rec_week, "
                            "ADD KEY idx_rec_week (rec_id, week_no)")
        finally:
            cur.execute("SELECT RELEASE_LOCK('stock_tracking_schema_v2')")


def _f(v: Any) -> Optional[float]:
    """numpy 标量安全转 Python float（PyMySQL 无法序列化 numpy 类型）；缺失返回 None。"""
    if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
        return None
    return float(v)


def _opt_text(v: Any) -> Optional[str]:
    """可选短文本字段：None/NaN/空串 → NULL，避免把 'nan' 当口径写进库。

    2026-10-26 新增，用于 score_weights / stop_rule 这类口径快照列。
    `str(nan) == 'nan'` 是真值，直接 str() 会把缺失值写成字面量 'nan'，
    事后查库会误以为那是某种口径名。
    """
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "nat"}:
        return None
    return s


def _clip_to_capacity(value: Any, column: str, code: str) -> Optional[str]:
    """按**实际列容量**裁剪文本，裁剪时 warning。容量未知（未探测/TEXT）则原样返回。

    这是 1406 的最后一道防线：万一 TEXT 升级失败（库账号无 ALTER 权限），
    也要保证「单条超长」退化为「该条截断」，而不是「整批 INSERT 失败、
    当日全部推荐都丢失」。这是两种完全不同的故障等级。
    """
    s = "" if value is None else str(value).strip()
    if not s:
        return None
    limit = _text_column_capacity.get(column)
    if limit is None or len(s) <= limit:
        return s
    # 保留尾部省略号，便于识别"这是被裁剪过的"
    clipped = s[:max(1, limit - 3)] + "..."
    logger.warning("%s 超长（%d 字符 > 列容量 %d）已截断: %s。"
                   "请确认该列是否已升级为 TEXT，否则入选依据会不完整",
                   column, len(s), limit, code)
    return clipped


def _ensure_schema_cached() -> None:
    """提前确保表结构就绪，让文本列容量缓存在构建 rows 之前可用。

    save_recommendations 先构建 rows 再 _ensure_tables，而截断兜底依赖
    _ensure_tables 探测到的**实际**列容量。若顺序反了，容量缓存为空 →
    截断静默失效 → 1406 原样复现。故这里先探一次；_tables_ready 全局缓存
    保证插入阶段的再次调用是 no-op，不产生额外连接开销。
    连接失败只 warning：落库阶段本来就会再试一次。
    """
    if _tables_ready:
        return
    try:
        conn = _connect()
        try:
            _ensure_tables(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("预探测表结构失败（落库阶段会重试；文本列将不做截断）: %s", e)


def save_recommendations(df: Optional[pd.DataFrame], strategy: str = STRATEGY_BOTTOM_FISHING) -> int:
    """将选股结果写入 stock_recommendation，(rec_date, code, strategy) 重复时检查入库时间。

    重复记录处理规则：
    - 库中不存在 → 正常入库
    - 库中存在且 created_at 超过30天 → 删除旧记录后重新入库
    - 库中存在且 created_at 未超过30天 → 跳过不入库

    strategy：策略来源（bottom_fishing / volume_breakout）。同一股票同日可被两套
    策略分别推荐并存，周度追踪与归因按来源分列。返回新插入行数。
    """
    if df is None or df.empty:
        return 0
    # 未明确核验为 formal 的记录不得进入正式推荐追踪。
    if "tier" not in df.columns:
        return 0
    df = df.loc[df["tier"].eq("formal")].copy()
    if df.empty:
        return 0
    if not is_configured():
        logger.info("MySQL 未配置（MYSQL_HOST/USER/PASSWORD/DATABASE），跳过推荐结果落库")
        return 0
    if strategy not in KNOWN_STRATEGIES:
        # 2026-10-13：由「warning + 静默改写」改为直接抛错。
        # 静默改写是本项目里最难查的一类 bug：调用方以为自己的 sleeve 独立落库，
        # 实际全部并入 bottom_fishing，唯一键冲突导致结果丢失、归因口径被混算，
        # 而日志只有一行 warning，混在几千行筛选日志里根本看不见。
        # 未注册的 strategy 是**编程错误**（新增策略忘了登记），必须快速失败。
        raise ValueError(
            f"未注册的策略来源 {strategy!r}；已注册: {list(KNOWN_STRATEGIES)}。"
            f"新增策略请先在 store.mysql_store 登记常量并加入 KNOWN_STRATEGIES，"
            f"否则会被并入 {STRATEGY_BOTTOM_FISHING!r} sleeve，造成同日同股覆盖与归因混算。")

    rows = []
    keys = []  # 用于查询已存在记录的 (rec_date, code, strategy) 组合
    # 必须在构建 rows 之前完成：文本列容量缓存来自 _ensure_tables 的 information_schema 探测
    _ensure_schema_cached()
    for _, r in df.iterrows():
        rec_date_str = str(r["date"])
        code_str = str(r["code"])
        keys.append((rec_date_str, code_str, strategy))
        rows.append((
            rec_date_str, code_str, str(r["name"]), _f(r["close"]),
            _f(r.get("score")), str(r.get("grade", "") or "") or None,
            _f(r.get("daily_score")), _f(r.get("rsi")), _f(r.get("rsi7")), _f(r.get("rsi21")),
            _f(r.get("vol_ratio")), _f(r.get("turnover_ratio")),
            _f(r.get("stop_loss")), _f(r.get("take_profit")), _f(r.get("rr_ratio")),
            str(r.get("market_env", "") or "") or None,
            1 if bool(r.get("has_divergence")) else 0,
            _clip_to_capacity(r.get("signals_hit"), "signals_hit", code_str),
            str(r.get("fund_status", "") or "") or None,
            str(r.get("weekly_status", "") or "") or None,
            "formal",
            strategy,
            _clip_to_capacity(r.get("missing_tags"), "missing_tags", code_str),
            _opt_text(r.get("score_weights")),
            _opt_text(r.get("stop_rule")),
        ))

    insert_sql = """
        INSERT INTO stock_recommendation
        (rec_date, code, name, rec_close, score, grade, daily_score, rsi, rsi7, rsi21,
         vol_ratio, turnover_ratio, stop_loss, take_profit, rr_ratio, market_env, has_divergence,
         signals_hit, fund_status, weekly_status, rec_tier, strategy, missing_tags,
         score_weights, stop_rule)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    # 查询已存在记录的 created_at，判断是否超过30天
    check_sql = """
        SELECT rec_date, code, strategy, created_at
        FROM stock_recommendation
        WHERE (rec_date, code, strategy) IN ({})
    """.format(", ".join(["(%s, %s, %s)"] * len(keys)))

    delete_sql = """
        DELETE FROM stock_recommendation
        WHERE rec_date = %s AND code = %s AND strategy = %s
    """

    try:
        conn = _connect()
        try:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                # 查询已存在的记录及其入库时间
                existing_records = {}
                if keys:
                    flat_keys = []
                    for k in keys:
                        flat_keys.extend(k)
                    cur.execute(check_sql, flat_keys)
                    for row in cur.fetchall():
                        rec_date, code, strat, created_at = row
                        existing_records[(str(rec_date), str(code), str(strat))] = created_at

                # 筛选需要删除的过期记录（超过30天）
                now = datetime.now()
                expired_keys = []
                skipped_count = 0
                for key, created_at in existing_records.items():
                    if isinstance(created_at, datetime):
                        age_days = (now - created_at).days
                    else:
                        # 兼容字符串格式
                        created_dt = pd.Timestamp(created_at).to_pydatetime()
                        age_days = (now - created_dt).days
                    if age_days > 30:
                        expired_keys.append(key)
                        logger.debug("记录 %s 已过期(%d天)，将重新入库", key, age_days)
                    else:
                        skipped_count += 1
                        logger.debug("记录 %s 未过期(%d天)，跳过入库", key, age_days)

                # 删除过期记录
                for key in expired_keys:
                    cur.execute(delete_sql, key)

                # 过滤掉未过期的已存在记录，只插入新记录和已过期的记录
                rows_to_insert = []
                for i, key in enumerate(keys):
                    if key not in existing_records or key in expired_keys:
                        rows_to_insert.append(rows[i])

                inserted = 0
                if rows_to_insert:
                    inserted = cur.executemany(insert_sql, rows_to_insert)

            logger.info(
                "推荐结果已落库[%s]：新增 %d 条（共提交 %d 条，跳过 %d 条未过期记录）",
                strategy, inserted or 0, len(rows), skipped_count
            )
            return int(inserted or 0)
        finally:
            conn.close()
    except Exception as e:
        # 落库失败不应让选股主流程失败
        logger.warning("推荐结果落库失败（不影响选股流程）: %s", e)
        return 0


def get_active_recommendations(max_age_days: int = TRACK_MAX_AGE_DAYS,
                               max_weeks: int = len(TRACK_HORIZONS)) -> list[dict]:
    """查询窗口内尚未完成固定期限的推荐；返回上次日期及已观测日期/期限。

    max_weeks 形参名保留兼容，实际语义 = 需要完成的观测期限个数
    （= len(TRACK_HORIZONS)）：completed_horizons 达到全部期限即退出追踪。
    tracked_weeks 保留兼容字段，含义改为不同收盘日数，历史错计周号不占额度。
    """
    if not is_configured():
        logger.info("MySQL 未配置，无法执行周度追踪")
        return []

    sql = """
        SELECT r.id, r.rec_date, r.code, r.name, r.rec_close, r.strategy,
               COUNT(DISTINCT t.close_date) AS tracked_weeks,
               MAX(t.close_date) AS last_close_date,
               GROUP_CONCAT(DISTINCT t.close_date) AS tracked_close_dates,
               GROUP_CONCAT(DISTINCT t.holding_trade_days) AS tracked_horizons,
               COUNT(DISTINCT t.holding_trade_days) AS completed_horizons
        FROM stock_recommendation r
        LEFT JOIN stock_tracking t ON t.rec_id = r.id
        WHERE r.rec_date >= CURDATE() - INTERVAL %s DAY
        GROUP BY r.id, r.rec_date, r.code, r.name, r.rec_close, r.strategy
        HAVING completed_horizons < %s
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


def fetch_rec_codes_for_date(rec_date: str) -> set[str]:
    """查询某日已落库推荐的股票代码集合（跨全部策略），供组合层同股去重。

    后运行的策略在落库前调用：剔除当日已被其他策略推荐的个股，
    避免两套策略同日重复推荐同一标的。未配置 MySQL 或查询失败时返回空集
    （降级为不去重，与落库可选哲学一致）。
    """
    if not is_configured():
        return set()
    sql = "SELECT DISTINCT code FROM stock_recommendation WHERE rec_date = %s"
    try:
        conn = _connect()
        try:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(sql, (str(rec_date),))
                return {str(row[0]) for row in cur.fetchall()}
        finally:
            conn.close()
    except Exception as e:
        logger.warning("查询当日已推荐代码失败（降级为不去重）: %s", e)
        return set()


def fetch_recent_recommended_codes(strategy: str, since_date: str,
                                   cross_sleeve: bool = False) -> set[str]:
    """查询 since_date（含）以来该 strategy 已落库的 formal 推荐代码集合，供冷却期使用。

    【为什么需要】真实落库推荐追踪（diag_real_recs.py，2026-09-08~10-09）显示
    存在「越跌越推」循环：云天化(600096) 在 09-17/09-23/09-24 被连续推荐 3 次，
    推荐价 28.29 → 27.95 → 27.56 一路走低，而三次推荐后 F1 分别为
    -0.67% / -1.40% / -1.45%，全部为负；盐津铺子(002847)、西藏药业(600211)、
    新乳业(002946) 各被推荐 2 次。机制是「跌了 → 250日分位更低、更便宜 →
    再次满足准入 → 再推 → 再跌」。对使用者而言这是**同一只亏损票被反复推送**，
    放大「推荐后总是下跌」的体感。

    【为什么放在 store 层】策略层不直接依赖 DB，由调用方（run.py）查询后以
    「集合」形式传入，策略层只做纯过滤；测试/回测路径不传则该机制整体不生效。
    未配置 MySQL 或查询失败时返回空集并 warning（降级为不冷却，与落库可选
    哲学一致），但调用方必须把「冷却是否真的生效」打进日志——不可观测的
    风控等于没有风控。

    since_date 格式 YYYY-MM-DD，由调用方按交易日历回溯，不在此处做日期运算。

    cross_sleeve（2026-10-26 新增）：True 时**忽略 strategy 过滤**，把其他 sleeve
    近期已推出的代码一并返回，用于跨 sleeve 去重。实证依据：亚普股份(603013) 在
    10-21/22/23 连续 3 天各被 longterm_quality 推荐 1 次，10-22 当天还被
    bottom_fishing 再推 1 次 —— 同一只票 3 天写了 4 行。原因是 run_longterm.py
    从未传 recently_recommended（冷却只对 run.py 生效），且它与 run.py 各查各的
    strategy，互不可见。用户视角这是「同一只票被反复推送」，归因侧则把 1 个观点
    计成 4 个观测，胜率分布被污染。跨 sleeve 只会让结果更少，符合「只减不增」。
    """
    if not is_configured():
        return set()
    if cross_sleeve:
        sql = ("SELECT DISTINCT code FROM stock_recommendation "
               "WHERE rec_date >= %s")
        params: tuple = (str(since_date),)
    else:
        sql = ("SELECT DISTINCT code FROM stock_recommendation "
               "WHERE strategy = %s AND rec_date >= %s")
        params = (str(strategy), str(since_date))
    try:
        conn = _connect()
        try:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return {str(row[0]) for row in cur.fetchall()}
        finally:
            conn.close()
    except Exception as e:
        logger.warning("查询近期已推荐代码失败（降级为不启用冷却）: %s", e)
        return set()


def save_tracking(rec_id: int, rec_date: Any, code: str, week_no: int,
                  close_date: str, close_price: float, rec_close: float,
                  holding_trade_days: int | None = None) -> bool:
    """同推荐同收盘日幂等；可选固定期限保持旧调用兼容，未知期限不猜测。

    同时计算并存储周收益率和月收益率：
    - 周收益率 = (当日收盘价 - 5个交易日前收盘价) / 5个交易日前收盘价 × 100%
    - 月收益率 = (当日收盘价 - 20个交易日前收盘价) / 20个交易日前收盘价 × 100%
    若无法获取历史价格（如首次追踪），则对应收益率为 NULL。
    """
    if not is_configured():
        return False

    close_date = pd.Timestamp(close_date).date().isoformat()
    if (close_date <= pd.Timestamp(rec_date).date().isoformat()
            or not 1 <= int(week_no) <= TRACK_MAX_WEEKS
            or (holding_trade_days is not None and holding_trade_days != int(week_no) * 5)
            or not 0 < float(rec_close) < float('inf')
            or not 0 < float(close_price) < float('inf')):
        return False
    return_value = round(float(close_price) - float(rec_close), 3)
    return_pct = round(return_value / float(rec_close) * 100, 3) if rec_close else 0.0
    track_date = date.today().strftime("%Y-%m-%d")
    rec_date_str = rec_date.strftime("%Y-%m-%d") if isinstance(rec_date, (date, datetime)) else str(rec_date)

    # 计算周收益率和月收益率：查询该推荐的历史追踪记录
    weekly_return_pct = None
    monthly_return_pct = None
    try:
        conn = _connect()
        try:
            conn.ping(reconnect=True)
            _ensure_tables(conn)
            with conn.cursor() as cur:
                # 查询该推荐的所有历史追踪记录，按 holding_trade_days 排序
                cur.execute("""
                    SELECT holding_trade_days, close_price
                    FROM stock_tracking
                    WHERE rec_id = %s AND legacy_duplicate = 0
                    ORDER BY holding_trade_days ASC
                """, (int(rec_id),))
                history = cur.fetchall()

                # 构建 holding_trade_days -> close_price 映射
                price_map = {row[0]: float(row[1]) for row in history if row[0] is not None}

                current_holding = holding_trade_days if holding_trade_days is not None else int(week_no) * 5

                # 周收益率：5个交易日前的收盘价
                if current_holding >= 5:
                    prev_week_holding = current_holding - 5
                    if prev_week_holding in price_map:
                        prev_price = price_map[prev_week_holding]
                        if prev_price > 0:
                            weekly_return_pct = round((float(close_price) / prev_price - 1) * 100, 3)

                # 月收益率：20个交易日前的收盘价
                if current_holding >= 20:
                    prev_month_holding = current_holding - 20
                    if prev_month_holding in price_map:
                        prev_price = price_map[prev_month_holding]
                        if prev_price > 0:
                            monthly_return_pct = round((float(close_price) / prev_price - 1) * 100, 3)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("计算周/月收益率时查询历史记录失败(%s): %s", code, e)
        # 不影响主流程，继续插入记录

    sql = """
        INSERT IGNORE INTO stock_tracking
        (rec_id, rec_date, code, week_no, track_date, close_date, close_price, return_value, return_pct,
         holding_trade_days, weekly_return_pct, monthly_return_pct)
        SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        FROM DUAL WHERE NOT EXISTS (
            SELECT 1 FROM stock_tracking WHERE rec_id = %s AND close_date = %s
        )
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
                    return_value, return_pct, holding_trade_days,
                    weekly_return_pct, monthly_return_pct,
                    int(rec_id), close_date,
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
    / week_no / return_pct / weekly_return_pct / monthly_return_pct / track_date / close_date
    / holding_trade_days / holding_calendar_days；
    固定期限聚合由调用方完成，旧行 holding_trade_days=NULL，不从旧 week_no 推断。
    仅返回至少有一次追踪记录的推荐（尚未被追踪的新推荐不参与归因）。
    """
    if not is_configured():
        logger.info("MySQL 未配置，无法执行信号归因查询")
        return []

    sql = """
        SELECT r.id, r.rec_date, r.code, r.name, r.grade, r.has_divergence, r.market_env,
               r.score, r.daily_score, r.strategy, t.week_no, t.return_pct,
               t.weekly_return_pct, t.monthly_return_pct,
               t.id AS tracking_id, t.track_date, t.close_date, t.holding_trade_days,
               DATEDIFF(t.close_date, r.rec_date) AS holding_calendar_days,
               t.legacy_duplicate
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
