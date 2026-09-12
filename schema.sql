-- ============================================================================
-- A股日线量化选股策略 - MySQL 持久化表结构
--
-- 两张表：
--   1. stock_recommendation  每日推荐记录（run.py 每日选股后写入）
--   2. stock_tracking        周度表现追踪（run_weekly_tracking.py 每周写入，
--                            每条推荐最多追踪 4 周 ≈ 一个月）
--
-- 使用：在 MySQL 5.7+ / 8.0 中执行本文件（先 CREATE DATABASE 并 USE）：
--   CREATE DATABASE IF NOT EXISTS stock_fishing DEFAULT CHARSET utf8mb4;
--   USE stock_fishing;
--   SOURCE schema.sql;
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 推荐记录表：每次选股输出的个股明细，(rec_date, code) 唯一防重复写入
-- 注：程序首次连接会自动建表（store/mysql_store.py 内置同款 DDL），并对存量表
--     自动 ALTER 补齐 signals_hit 等新列（幂等迁移），本文件为手动建库参考。
--     只有 rec_tier='formal' 的正式推荐会落库（待核验候选不落库、不参与追踪）。
-- ----------------------------------------------------------------------------
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
    market_env       VARCHAR(16)     NULL COMMENT '市场环境 bull/bear/neutral/unknown',
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
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci COMMENT ='每日选股推荐记录';

-- ----------------------------------------------------------------------------
-- 周度追踪表：每周对仍在追踪期内（推荐日起一个月内、最多4次）的推荐记录
-- 记录一次最新收盘价，收益值 = close_price - rec_close，收益率 = 收益值/推荐价
-- ----------------------------------------------------------------------------
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
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci COMMENT ='推荐个股周度表现追踪';

-- ----------------------------------------------------------------------------
-- 常用查询示例
-- ----------------------------------------------------------------------------
-- 某次推荐至今的每周表现：
--   SELECT t.week_no, t.close_date, t.close_price, t.return_value, t.return_pct
--   FROM stock_tracking t WHERE t.rec_id = ? ORDER BY t.week_no;
--
-- 全部推荐的最新一周表现汇总（胜率/平均收益率）：
--   SELECT r.rec_date, COUNT(*) AS picks,
--          AVG(t.return_pct) AS avg_return_pct,
--          SUM(t.return_pct > 0) / COUNT(*) * 100 AS win_rate_pct
--   FROM stock_recommendation r
--   JOIN stock_tracking t ON t.rec_id = r.id
--   WHERE t.week_no = (SELECT MAX(week_no) FROM stock_tracking WHERE rec_id = r.id)
--   GROUP BY r.rec_date ORDER BY r.rec_date DESC;
