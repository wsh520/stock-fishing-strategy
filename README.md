# A股量化选股策略 - 自动化执行系统

基于「周线左侧寻底 + 日线右侧确认」的量化选股策略，通过 GitHub Actions 每日自动执行，结果通过飞书机器人推送通知。支持历史回测与增量回测。

## 策略简介

5层量化过滤体系：

1. **市场环境过滤**：沪深300周线MA20斜率判断牛/熊/中性环境，熊市收紧等级阈值
2. **周线左侧寻底**：ATR归一化跌幅 + 量能模式识别 + CCI超卖确认 + MACD底背离 + 不破前低
3. **基本面防雷**：ROE/负债率/商誉/扣非利润等否决项
4. **日线右侧确认**：MA5拐头 + EMA金叉 + RSI超卖反弹 + 量价配合（至少一个信号触发）
5. **风险收益比过滤**：动态止损(ATR) + 止盈目标(MA20) + 最低1.5倍风险收益比

评分体系：周线最高70分 + 日线最高30分 = 100分，按分数分为 A/B/C/D 四个等级（D级淘汰）。

## 自动执行

| 工作流 | 触发时间 | 说明 |
|--------|----------|------|
| **Daily Stock Screening** | 交易日 15:10 (北京) | 每日选股 + 追踪 + 绩效报告 |
| **Weekly Backtest** | 每周六 16:00 (北京) | 增量回测（首次自动全量） |

两个工作流均支持手动触发 (`workflow_dispatch`)。

## 快速部署

### 1. Fork 本仓库

点击右上角 Fork 按钮将本仓库 Fork 到你的 GitHub 账号下。

### 2. 创建飞书自定义机器人

1. 打开飞书，进入你想接收通知的**群聊**
2. 点击群聊右上角 **设置 (···)** → **群机器人** → **添加机器人**
3. 选择 **自定义机器人**
4. 填写机器人名称（如：选股助手）
5. 复制生成的 **Webhook 地址**（格式：`https://open.feishu.cn/open-apis/bot/v2/hook/xxx`）
6. 点击完成

### 3. 配置 GitHub Secrets

进入 Fork 仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**：

| Name | Value | 必填 |
|------|-------|------|
| `FEISHU_WEBHOOK_URL` | 飞书 Webhook 地址 | 推荐（不配则跳过通知） |
| `MYSQL_HOST` | MySQL 主机地址 | 可选 |
| `MYSQL_PORT` | MySQL 端口 (默认 3306) | 可选 |
| `MYSQL_USER` | MySQL 用户名 | 可选 |
| `MYSQL_PASSWORD` | MySQL 密码 | 可选 |
| `MYSQL_DATABASE` | MySQL 数据库名 (默认 stock_strategy) | 可选 |

> MySQL 为可选配置。未配置时系统仅使用 CSV 存储，不影响选股和回测主流程。配置后可自动建表，支持绩效统计和增量回测续跑。

### 4. 启用 GitHub Actions

1. 进入仓库的 **Actions** 页面
2. 如果有提示，点击 **I understand my workflows, go ahead and enable them**
3. 策略将在每个交易日 15:10 自动执行

### 5. 手动测试

1. 进入 **Actions** 页面
2. 左侧选择 **Daily Stock Screening** → **Run workflow**
3. 等待执行完成，检查飞书是否收到通知

首次运行回测：

1. 左侧选择 **Weekly Backtest** → **Run workflow**
2. 模式选择 `incremental`（首次无历史记录时自动执行全量回测 20260101 至今）
3. 后续每周六自动增量续跑

## 项目结构

```
├── .github/workflows/
│   ├── daily_screen.yml          # 每日选股工作流
│   └── weekly_backtest.yml       # 每周回测工作流
├── src/
│   ├── bottom_fishing_strategy.py  # 核心策略（5层过滤 + 评分）
│   └── backtest.py               # 回测引擎（全量/增量）
├── notify/
│   └── feishu.py                 # 飞书通知模块
├── data/                         # 自动生成的数据文件
│   ├── signal_history.csv        # 推荐信号历史
│   ├── tracking_report.csv       # 追踪报告
│   ├── backtest_trades_*.csv     # 回测交易明细
│   └── backtest_summary_*.csv    # 回测统计汇总
├── db.py                         # MySQL 存储模块（可选）
├── run.py                        # 每日选股入口
├── requirements.txt              # Python 依赖
└── README.md
```

## 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 设置飞书 Webhook（可选，不设置则跳过通知）
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook-id"

# 运行每日选股
python run.py
```

直接运行策略脚本（不发送通知）：

```bash
# 仅选股
python src/bottom_fishing_strategy.py screen

# 仅追踪历史推荐
python src/bottom_fishing_strategy.py track

# 完整流程
python src/bottom_fishing_strategy.py full
```

## 回测

对历史数据批量执行选股策略，模拟每个信号的入场/退出，计算收益率统计。

```bash
# 全量回测：20260101 至今
python src/backtest.py

# 指定日期范围
python src/backtest.py --start 20260101 --end 20260831

# 增量回测：自动从上次结束日期续跑（首次等同全量）
python src/backtest.py --incremental

# 调整参数
python src/backtest.py --max-hold-weeks 6 --workers 8
```

回测输出：
- 交易明细（入场/出场日期价格、收益率、退出原因）
- 统计报告（胜率、盈亏比、平均收益、等级分布、月度分布）
- 结果保存至 CSV（`data/backtest_*`），配置 MySQL 时同时写入数据库

退出机制：
- **止损**：入场价 - ATR × 2
- **止盈**：MA20（均值回归目标）
- **到期**：最大持有4周未触发止损/止盈，按收盘价退出

增量回测自动探测上次结束日期（优先级：MySQL → CSV → 默认起始日期）。

## MySQL 存储

配置 MySQL 环境变量后，系统自动创建以下表：

| 表名 | 说明 |
|------|------|
| `recommendations` | 每日推荐信号记录 |
| `tracking` | 追踪状态（幂等更新） |
| `performance_snapshots` | 月度绩效快照 |
| `backtest_trades` | 回测交易明细 |
| `backtest_summary` | 回测统计汇总 |

未配置 MySQL 时降级为纯 CSV 存储，不影响任何功能。

## 通知样例

选股结果推送到飞书群后，消息卡片包含：
- 市场环境判断
- 推荐股票列表（代码/名称/评分/信号等级）
- 交易计划（止损价/止盈价/风险收益比）
- 周度追踪报告（胜率/平均收益/状态分布）
- 月度绩效报告（需配置 MySQL）

## 技术指标

| 指标 | 环节 | 用途 |
|------|------|------|
| MA20 | 周线 | 趋势判断 + 止盈目标 |
| ATR14 | 周线 | 跌幅标准化 + 止损缓冲 |
| MACD | 周线 | 底背离检测 |
| CCI14 | 周线 | 超卖区域确认 |
| MA5/MA10 | 日线 | 右侧确认触发 |
| EMA5/EMA10/EMA20 | 日线 | 趋势反转确认 + 金叉信号 |
| RSI14 | 日线 | 超卖/超买加减分 |

## 数据源

- **数据源**：AkShare（东方财富），单一通道，硬依赖
- **取数接口**：`stock_zh_a_hist`（个股周线/日线，前复权）、`stock_zh_index_daily`（指数）、`stock_info_a_code_name`（股票列表）、`stock_financial_analysis_indicator` / `stock_balance_sheet_by_report_em` / `stock_profit_sheet_by_report_em`（基本面）
- **失败重试**：请求异常按退避重试（个股 `MAX_RETRY`，股票列表 `LIST_MAX_RETRY`）；「返回空」视为该标的确实无数据，不重试
- **两级缓存**：内存 `CacheManager`（进程内，`CACHE_EXPIRE_HOURS`）→ `cache/` 目录磁盘 CSV/JSON
  - 行情类按交易日失效：新交易日自动重拉，同日重复运行走缓存
  - 股票列表按 `CACHE_TTL_DAYS` 失效，基本面按 `FUND_CACHE_TTL_DAYS` 失效
  - 磁盘只缓存未过滤的原始列表，过滤在返回时应用，改 config 即时生效无需清缓存
- **股票池过滤**：由 `StrategyConfig` 开关驱动（`FILTER_ST` / `EXCLUDE_DELISTING` / `EXCLUDE_BSE` / `EXCLUDE_CHINEXT` / `EXCLUDE_STAR`）

## 免责声明

本策略仅为量化研究工具，不构成投资建议。策略优化旨在从逻辑上减少低质量信号，不保证提高未来收益率或胜率。实际效果必须通过严格的样本外回测验证。投资有风险，入市需谨慎。
