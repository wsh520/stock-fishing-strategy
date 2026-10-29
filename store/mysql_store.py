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
    repeat_seq       TINYINT UNSIGNED NULL COMMENT '同一 code+strategy 的重复推荐序号（首次=1，冷却窗口内第2次=2），NULL=未知',
    superseded_by    BIGINT UNSIGNED NULL COMMENT '被哪条更新的推荐取代（旧行打废弃标签；NULL=仍是最新）。仅标注，统计不排除',
    prev_rec_date    DATE            NULL COMMENT '上一条推荐日期（跨 sleeve 冷却窗口内的「上次」，NULL=首次/非重复）',
    prev_rec_close   DECIMAL(10, 3)  NULL COMMENT '上一条推荐时的收盘价（NULL=首次/非重复）',
    created_at       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '写入时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_rec_date_code_strategy (rec_date, code, strategy),
    KEY idx_code (code),
    KEY idx_rec_date (rec_date),
    KEY idx_code_strategy_seq (code, strategy, repeat_seq)
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
    # 2026-10-26 重复推荐标注列。用户要求：进入冷却的票**可以重新推荐**，新推荐把旧行
    # 打上「废弃」标签，但统计时旧行照常计入、按多条分开统计、统计规则不变。
    # 故这两列只是**标注**，任何统计/追踪查询都不得按 superseded_by 过滤。
    "repeat_seq": "ADD COLUMN repeat_seq TINYINT UNSIGNED NULL COMMENT '同一 code+strategy 的重复推荐序号（首次=1，冷却窗口内第2次=2），NULL=未知' AFTER stop_rule",
    "superseded_by": "ADD COLUMN superseded_by BIGINT UNSIGNED NULL COMMENT '被哪条更新的推荐取代（旧行打废弃标签；NULL=仍是最新）。仅标注，统计不排除' AFTER repeat_seq",
    # 2026-10-27 新增：把「上一次推荐」的锚点真正落库（此前只在内存 DataFrame 里生成，
    # 落库时被丢弃，只留在 signals_hit 文本中，事后无法用 SQL 重建重复推荐链条）。
    # 跨 sleeve 冷却（3ccbe84）与 label 标注（7b4c20e）都需要这一环才能被验证：
    # repeat_seq 是 per-sleeve 累计序号，而卡片文案的「10日内第N次」是跨 strategy
    # 窗口口径，两者分母不同；有了 prev_rec_date/prev_rec_close 才能从任一 sleeve
    # 的行一路回溯到跨 sleeve 的上一环。
    "prev_rec_date": "ADD COLUMN prev_rec_date DATE NULL COMMENT '上一条推荐日期（跨 sleeve 冷却窗口内的「上次」，NULL=首次/非重复）' AFTER superseded_by",
    "prev_rec_close": "ADD COLUMN prev_rec_close DECIMAL(10, 3) NULL COMMENT '上一条推荐时的收盘价（NULL=首次/非重复）' AFTER prev_rec_date",
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
# stock_recommendation 的**实际存在**列集合（由 _ensure_tables 探测后填入）。
# None = 尚未探测成功。落库时据此裁剪 INSERT 列清单：任一新增列 ALTER 失败时，
# 只丢该列的标注，不让整批推荐因 Unknown column 全部落库失败
# （与「宁可少标注也要落库」一致，见 _mark_repeat_sequence 的同类兜底）。
_existing_columns: Optional[set] = None

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
    global _tables_ready, _existing_columns
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
                # 逐条 try：补列是**加性**操作，一条失败不应连带后一条也加不上。
                # 必须 logger.error 而不是 warning —— 这里失败会被 save_recommendations 的
                # 兜底 except 吞成「新增 0 条」，看起来和「今天没推荐」完全一样。
                try:
                    cur.execute(f"ALTER TABLE stock_recommendation {alter}")
                    existing.add(col)  # 成功即视为存在，供落库时裁剪 INSERT 列清单
                    logger.info("stock_recommendation 已补充列: %s", col)
                except Exception as e:  # noqa: BLE001
                    logger.error("stock_recommendation 补列 %s 失败（该列相关功能将降级）: %s "
                                 "| DDL: ALTER TABLE stock_recommendation %s", col, e, alter)
        # 落库列裁剪依据：以**补列后的真实集合**为准，未补齐的列不会进 INSERT。
        _existing_columns = set(existing)
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


def _repeat_seq_map(cur, strategy: str, keys: list[tuple[str, str, str]]) -> dict[str, int]:
    """落库**前**查询每个 code 在本 sleeve 的历史推荐条数，返回「本次是第几次」。

    【用户口径（2026-10-26）】冷却的票可以重新推荐；新推荐把旧行打「废弃」标签；
    统计时旧行仍正常计入、支持按多条分开统计、统计规则不变。要让「第 N 次推荐的
    胜率是否更差」真正可验证，必须先有 repeat_seq 这一维，否则只能靠 signals_hit
    里的人类可读文本反推。

    【两种「第 N 次」的分母不同，不要混用（2026-10-27 明确）】
      · 本函数的 repeat_seq = **同一 code+strategy 累计序号**，服务「按 sleeve
        分开统计」；A 股同股被两套 sleeve 各推一次，各自都是各自的第 N 次。
      · 卡片文案的「10 日内第 N 次」= **跨 strategy 窗口计数**，告诉使用者这只票
        整体被推了几次。
    两者都自洽，但统计时必须明确用哪个。跨 sleeve 的上一环由
    prev_rec_date / prev_rec_close 落库锚定，可从任一行回溯整条推荐链。

    必须在 INSERT 之前调用：计数含历史行但不含本次新行，+1 后即为本次序号。
    """
    codes = sorted({str(k[1]) for k in keys})
    if not codes:
        return {}
    ph = ", ".join(["%s"] * len(codes))
    cur.execute(
        f"SELECT code, COUNT(*) FROM stock_recommendation "
        f"WHERE strategy = %s AND code IN ({ph}) GROUP BY code",
        [strategy, *codes])
    prior = {str(r[0]): int(r[1]) for r in cur.fetchall()}
    return {c: prior.get(c, 0) + 1 for c in codes}


def _mark_repeat_sequence(cur, strategy: str, keys: list[tuple[str, str, str]],
                          seq_map: dict[str, int]) -> int:
    """落库**后**写 repeat_seq，并把同股更早的行标为 superseded_by=新行 id。

    只写标注列，**不做任何排除性过滤**：统计/追踪查询一律不得按 superseded_by
    过滤，否则等于把旧推荐从样本里删掉，与「统计照常」的要求相反。
    返回被标为废弃的旧行数。

    失败不抛错：存量库若 repeat_seq/superseded_by 两列未 ALTER 成功，
    标注失败不应让整批推荐落库失败（与「宁可少标注也要落库」一致）。
    """
    if not keys:
        return 0
    pair_ph = ", ".join(["(%s, %s)"] * len(keys))
    flat: list = []
    for rec_date, code, _strat in keys:
        flat.extend([str(rec_date), str(code)])
    cur.execute(
        f"SELECT id, rec_date, code FROM stock_recommendation "
        f"WHERE strategy = %s AND (rec_date, code) IN ({pair_ph})",
        [strategy, *flat])
    new_ids = {(str(r[1]), str(r[2])): int(r[0]) for r in cur.fetchall()}
    seq_updates, sup_updates = [], []
    for rec_date, code, _strat in keys:
        nid = new_ids.get((str(rec_date), str(code)))
        if nid is None:
            continue
        seq_updates.append((max(1, int(seq_map.get(str(code)) or 1)), nid))
        sup_updates.append((nid, str(code), strategy, str(rec_date), nid))
    if not seq_updates:
        return 0
    cur.executemany(
        "UPDATE stock_recommendation SET repeat_seq = %s WHERE id = %s", seq_updates)
    marked = cur.executemany(
        "UPDATE stock_recommendation SET superseded_by = %s "
        "WHERE code = %s AND strategy = %s AND rec_date < %s AND id <> %s "
        "AND superseded_by IS NULL", sup_updates)
    return int(marked or 0)


# INSERT 列顺序，与 save_recommendations 里 rows 元组**逐位对应**。
# 新增列时两处必须同步；_build_insert_sql 会在长度不一致时快速失败（编程错误）。
_INSERT_COLUMNS = (
    "rec_date", "code", "name", "rec_close", "score", "grade", "daily_score",
    "rsi", "rsi7", "rsi21", "vol_ratio", "turnover_ratio", "stop_loss",
    "take_profit", "rr_ratio", "market_env", "has_divergence", "signals_hit",
    "fund_status", "weekly_status", "rec_tier", "strategy", "missing_tags",
    "score_weights", "stop_rule", "prev_rec_date", "prev_rec_close",
)


def _build_insert_sql() -> tuple[str, list[int]]:
    """按**实际存在的列**生成 INSERT 语句，返回 (sql, 需要保留的列下标)。

    【为什么动态】此前列名硬编码在 SQL 里，任何一条补列 ALTER 失败（权限/锁/
    MySQL 版本差异）都会让 INSERT 报 Unknown column，被外层 except 吞成
    「新增 0 条」—— 当天全部推荐一条都落不了库，日志却和「今天没推荐」长得一样。
    现改为按 _existing_columns 裁剪：补列失败只丢该列的标注，推荐本身照常落库。

    _existing_columns 为 None（探测未成功）时退回全列，与改造前行为一致。
    """
    total = len(_INSERT_COLUMNS)
    present = _existing_columns
    if present is None:
        keep = list(range(total))
    else:
        keep = [i for i, c in enumerate(_INSERT_COLUMNS) if c in present]
        if not keep:
            raise ValueError("stock_recommendation 实际列集合为空，无法构建 INSERT")
    cols = ", ".join(_INSERT_COLUMNS[i] for i in keep)
    placeholders = ", ".join(["%s"] * len(keep))
    sql = (f"INSERT INTO stock_recommendation ({cols}) VALUES ({placeholders})")
    return sql, keep


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
            # 2026-10-27：重复推荐锚点（此前生成后在此处被丢弃，只留 signals_hit 文本）
            _opt_text(r.get("prev_rec_date")),
            _f(r.get("prev_rec_close")),
        ))

    insert_sql, keep_idx = _build_insert_sql()

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
                insert_keys = []
                for i, key in enumerate(keys):
                    if key not in existing_records or key in expired_keys:
                        rows_to_insert.append(rows[i])
                        insert_keys.append(key)

                # 重复推荐序号必须在 INSERT 之前取（计数含历史行、不含本次新行）
                seq_map = _repeat_seq_map(cur, strategy, insert_keys) if insert_keys else {}

                inserted = 0
                if rows_to_insert:
                    if len(rows_to_insert[0]) != len(_INSERT_COLUMNS):
                        # 编程错误：_INSERT_COLUMNS 与 rows 元组不同步，按列裁剪会错位落库
                        raise ValueError(
                            f"INSERT 列定义({len(_INSERT_COLUMNS)}) 与取值元组"
                            f"({len(rows_to_insert[0])}) 长度不一致：新增列必须同步 _INSERT_COLUMNS")
                    if len(keep_idx) != len(_INSERT_COLUMNS):
                        rows_to_insert = [tuple(row[i] for i in keep_idx)
                                          for row in rows_to_insert]
                    inserted = cur.executemany(insert_sql, rows_to_insert)

                # ===== 重复推荐标注（2026-10-26）=====
                # 新推荐照常入库；同股更早的行只打 superseded_by 标签，
                # 统计侧（select_observations / get_active_recommendations）**不过滤**。
                superseded = 0
                if inserted and insert_keys:
                    try:
                        superseded = _mark_repeat_sequence(cur, strategy, insert_keys, seq_map)
                    except Exception as me:  # noqa: BLE001
                        logger.warning("重复推荐标注失败（推荐已落库，仅缺 repeat_seq/"
                                       "superseded_by 标注）: %s", me)

            logger.info(
                "推荐结果已落库[%s]：新增 %d 条（共提交 %d 条，跳过 %d 条未过期记录，"
                "旧推荐标废弃 %d 条）",
                strategy, inserted or 0, len(rows), skipped_count, superseded
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


def fetch_rec_codes_for_date(rec_date: str,
                            exclude_strategy: Optional[str] = None) -> set[str]:
    """查询某日已落库推荐的股票代码集合（跨全部策略），供组合层同股去重。

    后运行的策略在落库前调用：剔除当日已被其他策略推荐的个股，
    避免两套策略同日重复推荐同一标的。未配置 MySQL 或查询失败时返回空集
    （降级为不去重，与落库可选哲学一致）。

    exclude_strategy（2026-10-29 新增）：排除指定的 strategy，语义是「**其他** sleeve
    当日已推的代码」。不加这个参数时，幂等重跑（手动 workflow_dispatch）会把本 sleeve
    当天已写入的记录也算进来，导致自己把自己全部拦截 → 0 推荐。
    """
    if not is_configured():
        # 死闸门原则：必须显式留痕。否则调用方只能打印「其他策略当日已推 0 只」，
        # 无法区分「daily 还没跑」「没配 DB」「查询失败」三种完全不同的情况，
        # 会把「同日去重没生效」误读成「今天没有重复票」。
        logger.info("MySQL 未配置，当日已推荐代码为空 → 跨 sleeve 同日去重不生效")
        return set()
    if exclude_strategy:
        sql = ("SELECT DISTINCT code FROM stock_recommendation "
               "WHERE rec_date = %s AND strategy <> %s")
        params: tuple = (str(rec_date), str(exclude_strategy))
    else:
        sql = "SELECT DISTINCT code FROM stock_recommendation WHERE rec_date = %s"
        params = (str(rec_date),)
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


def fetch_recent_recommendation_detail(strategy: str, since_date: str,
                                       cross_sleeve: bool = False) -> dict[str, dict]:
    """查询 since_date（含）以来已落库推荐的**明细**（冷却标注用）。

    返回 {code: {"count": 第几次, "last_date": 上次推荐日, "last_close": 上次推荐价}}。
    与 fetch_recent_recommended_codes 的差别只在「带明细」：冷却从 block 改为 label 后，
    卡片与日志要写明「10 日内第 2 次、上次 27.95 → 本次 27.56(-1.4%)」，
    否则重复推荐反而制造困惑。

    count 语义：max(窗口内推荐条数, 该行 repeat_seq)，即**累计第几次**而不是窗口内第几次——
    用窗口内计数会把「20 天前推过 1 次、今天第 2 次」标成「第 1 次」。
    repeat_seq 为 NULL（存量行/标注失败）时退化为窗口内条数，不伪造成已知。

    **不过滤 superseded_by**：被标为废弃的旧推荐仍是一次真实推荐，
    统计口径要求「旧行照常计入」，冷却判定也一样——它是「10 日内推过」的事实。

    未配置 MySQL 或查询失败返回空 dict（降级为不冷却），由调用方把生效状态打进日志。
    """
    if not is_configured():
        # 必须显式留痕：否则入口脚本会打印「已查库…命中 0 只」，
        # 让人误以为冷却真的跑了（死闸门原则）。
        logger.info("MySQL 未配置，近期推荐明细为空 → 重复推荐冷却不生效")
        return {}
    if cross_sleeve:
        inner = ("SELECT code, MAX(rec_date) AS last_date, COUNT(*) AS n "
                 "FROM stock_recommendation WHERE rec_date >= %s GROUP BY code")
        outer_where = ""
        params: tuple = (str(since_date),)
    else:
        inner = ("SELECT code, MAX(rec_date) AS last_date, COUNT(*) AS n "
                 "FROM stock_recommendation WHERE strategy = %s AND rec_date >= %s "
                 "GROUP BY code")
        outer_where = "WHERE r.strategy = %s"
        params = (str(strategy), str(since_date), str(strategy))
    sql = (f"SELECT r.code, g.n, g.last_date, r.rec_close, r.repeat_seq "
           f"FROM stock_recommendation r "
           f"JOIN ({inner}) g ON g.code = r.code AND g.last_date = r.rec_date "
           f"{outer_where} ORDER BY r.id DESC")
    try:
        conn = _connect()
        try:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                out: dict[str, dict] = {}
                for code, n, last_date, last_close, seq in cur.fetchall():
                    code = str(code)
                    if code in out:  # 跨 sleeve 时同一 code 同一天可能有多行，取最新一条
                        continue
                    window_n = int(n or 0)
                    try:
                        seq_n = int(seq) if seq is not None else 0
                    except (TypeError, ValueError):
                        seq_n = 0
                    out[code] = {
                        "count": max(window_n, seq_n, 1),
                        "last_date": str(last_date) if last_date is not None else None,
                        "last_close": float(last_close) if last_close is not None else None,
                    }
                return out
        finally:
            conn.close()
    except Exception as e:
        logger.warning("查询近期推荐明细失败（降级为不启用冷却标注）: %s", e)
        return {}


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
