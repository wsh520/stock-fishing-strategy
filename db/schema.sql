-- ============================================================
-- 选股策略 MySQL 数据表
-- 适用于 MySQL 8.0+
-- ============================================================

-- 主表：存储每次推荐的完整信息 + 追踪状态
CREATE TABLE IF NOT EXISTS stock_recommendations (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

    -- 身份与信号
    recommend_date      DATE            NOT NULL COMMENT '推荐日期',
    code                CHAR(6)         NOT NULL COMMENT '股票代码',
    name                VARCHAR(32)     NOT NULL COMMENT '股票名称',
    industry            VARCHAR(64)     DEFAULT '' COMMENT '所属行业',
    signal_level        VARCHAR(32)     NOT NULL COMMENT 'STRONG_BUY/BUY_SIGNAL/RIGHT_CONFIRMED',
    market_env          VARCHAR(32)     DEFAULT '' COMMENT '市场环境',

    -- 评分
    total_score         DECIMAL(8,2)    DEFAULT 0 COMMENT '综合评分',
    daily_confirm_score DECIMAL(8,2)    DEFAULT 0 COMMENT '日线确认得分',

    -- 推荐时价格
    price               DECIMAL(10,3)   NOT NULL COMMENT '推荐时收盘价',
    weekly_ma20         DECIMAL(10,3)   DEFAULT NULL COMMENT '周线MA20',
    buy_price           DECIMAL(10,3)   DEFAULT NULL COMMENT '右侧买入价(日MA5)',
    dynamic_stop        DECIMAL(10,3)   DEFAULT NULL COMMENT '动态止损价',
    first_tp            DECIMAL(10,3)   DEFAULT NULL COMMENT '第一止盈价',
    second_tp_defense   DECIMAL(10,3)   DEFAULT NULL COMMENT '第二止盈防守线',

    -- 周线指标
    week_return         DECIMAL(8,4)    DEFAULT NULL COMMENT '本周涨跌幅(%)',
    normalized_drop     DECIMAL(8,4)    DEFAULT NULL COMMENT 'ATR归一化跌幅',
    volume_ratio        DECIMAL(8,4)    DEFAULT NULL COMMENT '周线成交量比',
    turnover_ratio      DECIMAL(8,4)    DEFAULT NULL COMMENT '周线换手比',
    weekly_turnover     DECIMAL(8,4)    DEFAULT NULL COMMENT '本周换手率(%)',
    volume_mode         VARCHAR(8)      DEFAULT NULL COMMENT '量能模式(A/B/C)',
    prev_20_low         DECIMAL(10,3)   DEFAULT NULL COMMENT '前20周低点',
    week_low            DECIMAL(10,3)   DEFAULT NULL COMMENT '本周最低价',
    distance_to_low     DECIMAL(8,4)    DEFAULT NULL COMMENT '距前低距离(%)',
    position_20         DECIMAL(8,4)    DEFAULT NULL COMMENT '20周价格位置',
    close_position      DECIMAL(8,4)    DEFAULT NULL COMMENT '本周收盘位置',
    lower_shadow_ratio  DECIMAL(8,4)    DEFAULT NULL COMMENT '下影线比例',

    -- 交易参数
    stop_distance_pct   DECIMAL(8,4)    DEFAULT NULL COMMENT '止损距离(%)',
    risk_reward_ratio   DECIMAL(8,4)    DEFAULT NULL COMMENT '风险收益比',
    rsi14               DECIMAL(8,4)    DEFAULT NULL COMMENT 'RSI14',
    daily_ma5           DECIMAL(10,3)   DEFAULT NULL,
    daily_ma10          DECIMAL(10,3)   DEFAULT NULL,
    daily_ma20          DECIMAL(10,3)   DEFAULT NULL,
    prev_day_high       DECIMAL(10,3)   DEFAULT NULL COMMENT '前一日最高价',

    -- 基本面
    roe                 DECIMAL(8,2)    DEFAULT NULL COMMENT 'ROE(%)',
    debt_ratio          DECIMAL(8,2)    DEFAULT NULL COMMENT '资产负债率(%)',
    cashflow            DECIMAL(16,2)   DEFAULT NULL COMMENT '经营现金流(万元)',
    revenue_growth      DECIMAL(8,2)    DEFAULT NULL COMMENT '营收同比(%)',
    profit_growth       DECIMAL(8,2)    DEFAULT NULL COMMENT '净利润同比(%)',

    -- 追踪状态（由绩效追踪每日更新）
    current_price       DECIMAL(10,3)   DEFAULT NULL COMMENT '当前价格',
    return_pct          DECIMAL(8,2)    DEFAULT NULL COMMENT '收益率(%)',
    max_profit_pct      DECIMAL(8,2)    DEFAULT NULL COMMENT '期间最大浮盈(%)',
    max_drawdown_pct    DECIMAL(8,2)    DEFAULT NULL COMMENT '期间最大回撤(%)',
    period_high         DECIMAL(10,3)   DEFAULT NULL COMMENT '期间最高价',
    period_low          DECIMAL(10,3)   DEFAULT NULL COMMENT '期间最低价',
    holding_days        INT             DEFAULT 0 COMMENT '持仓天数',
    status              VARCHAR(16)     DEFAULT '持仓跟踪' COMMENT '持仓跟踪/浮盈持仓/小幅浮亏/较大浮亏/已止损/已止盈',
    hit_stop            TINYINT(1)      DEFAULT 0 COMMENT '是否触发止损',
    hit_tp              TINYINT(1)      DEFAULT 0 COMMENT '是否达止盈',
    last_tracked_at     DATETIME        DEFAULT NULL COMMENT '最后追踪时间',

    -- 元数据
    created_at          DATETIME        DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME        DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- 索引
    UNIQUE KEY uk_date_code (recommend_date, code),
    INDEX idx_recommend_date (recommend_date),
    INDEX idx_status (status),
    INDEX idx_signal_level (signal_level)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='选股策略推荐记录';


-- 快照表：存储月度绩效统计
CREATE TABLE IF NOT EXISTS performance_snapshots (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    report_date     DATE            NOT NULL COMMENT '报告日期',
    period_days     INT             NOT NULL DEFAULT 30 COMMENT '统计周期(天)',
    total_signals   INT             NOT NULL DEFAULT 0 COMMENT '推荐总数',
    win_count       INT             NOT NULL DEFAULT 0 COMMENT '盈利数',
    loss_count      INT             NOT NULL DEFAULT 0 COMMENT '亏损数',
    win_rate        DECIMAL(6,2)    DEFAULT NULL COMMENT '胜率(%)',
    avg_return      DECIMAL(8,2)    DEFAULT NULL COMMENT '平均收益率(%)',
    median_return   DECIMAL(8,2)    DEFAULT NULL COMMENT '中位数收益(%)',
    max_win         DECIMAL(8,2)    DEFAULT NULL COMMENT '最大盈利(%)',
    max_loss        DECIMAL(8,2)    DEFAULT NULL COMMENT '最大亏损(%)',
    details_json    JSON            DEFAULT NULL COMMENT '个股明细(JSON)',
    created_at      DATETIME        DEFAULT CURRENT_TIMESTAMP,

    UNIQUE KEY uk_report_date (report_date, period_days),
    INDEX idx_report_date (report_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='月度绩效快照';
