# A股日线量化选股策略 - 自动化执行系统

基于日线技术指标的量化选股策略，通过 GitHub Actions 每日自动执行，结果通过飞书机器人推送通知。采用 Baostock 主数据源 + AkShare 备用数据源的双源架构，主源失败自动切换。

## 策略简介

4层量化过滤体系：

1. **市场环境过滤**：沪深300日线MA20斜率判断牛/熊/中性环境，熊市收紧等级阈值（+10分）
2. **基本面防雷**：ROE/负债率否决项（金融业——银行/保险/券商等负债率天然 80%+，按名称关键词+代码白名单识别并单独放宽阈值，避免全行业误杀；Baostock 无商誉与扣非数据，切至 AkShare 时自动补齐商誉/扣非否决）
3. **日线技术指标筛选**：底背离 + 趋势转折（MA5拐头/EMA金叉同源合并计分）+ RSI超卖反弹 + 量价配合；流动性过滤（近20日日均成交额 ≥500万）；入场质量否决（当日涨幅 >7% 追高否决、开盘跳空高开 >3% 否决、MA20 近5日斜率 <-4% 的陡峭下降通道中趋势转折信号不认可）
4. **风险收益比过滤**：固定止损止盈（止损 5% / 止盈 10%）+ 最低1.5倍风险收益比

评分体系：趋势转折(40) + RSI反弹(25) + 量价配合(25) + 多周期共振(10) − RSI超买惩罚，满分 100。按分数分为 A/B/C/D 四个等级（A≥80 / B≥60 / C≥40）；准入门槛 `MIN_PASS_GRADE` 默认 B，C 级（仅单一趋势转折信号，40 分）与 D 级均淘汰——推荐必须同时具备趋势转折与至少一个确认信号；底背离个股通过准入后评级额外提升一档（仅用于展示/排序，不能绕过准入门槛）。熊市环境等级门槛 +10 分。执行过程打印选股漏斗日志（各层通过率）。

## 自动执行

| 工作流 | 触发时间 | 说明 |
|--------|----------|------|
| **Daily Stock Screening** | 交易日 15:10 (北京) | 每日选股 + 飞书通知 |

支持手动触发 (`workflow_dispatch`)。

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

### 4. 启用 GitHub Actions

1. 进入仓库的 **Actions** 页面
2. 如果有提示，点击 **I understand my workflows, go ahead and enable them**
3. 策略将在每个交易日 15:10 自动执行

### 5. 手动测试

1. 进入 **Actions** 页面
2. 左侧选择 **Daily Stock Screening** → **Run workflow**
3. 等待执行完成，检查飞书是否收到通知

## 项目结构

```
├── .github/workflows/
│   └── daily_screen.yml              # 每日选股工作流
├── src/
│   ├── bottom_fishing_strategy.py          # 核心策略（Baostock 数据源，4层过滤 + 评分）
│   ├── bottom_fishing_strategy_akshare.py  # 旧版策略备份（AkShare 数据源，未被引用）
│   └── bottom_fishing_strategy_old.py      # 更早期版本备份（未被引用）
├── notify/
│   └── feishu.py                     # 飞书通知模块
├── cache/                            # 自动生成的磁盘缓存（已 gitignore）
│   ├── 个股/指数行情（按交易日失效）
│   ├── 股票列表（按 CACHE_TTL_DAYS=6 天失效）
│   └── 基本面（按 FUND_CACHE_TTL_DAYS=7 天失效）
├── run.py                            # 每日选股入口（GitHub Actions 调用）
├── requirements.txt                  # Python 依赖
└── README.md
```

## 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 设置飞书 Webhook（可选，不设置则跳过通知）
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook-id"

# 运行每日选股（含通知）
python run.py
```

直接运行策略脚本（不发送通知）：

```bash
# 仅选股，打印明细
python src/bottom_fishing_strategy.py screen

# 完整流程（默认模式）
python src/bottom_fishing_strategy.py full
```

## 通知样例

选股结果推送到飞书群后，消息卡片包含：
- 市场环境判断
- 推荐股票列表（代码/名称/评分/信号等级）
- 交易计划（止损价/止盈价/风险收益比）

## 技术指标

| 指标 | 环节 | 用途 |
|------|------|------|
| MA5/EMA5/10/20 | 日线 | 趋势转折（拐头/金叉，同源信号合并计分） |
| MA20 斜率 | 日线 | 下降通道过滤（陡峭下降中趋势转折不认可，防接飞刀） |
| 当日涨幅/跳空幅度 | 日线 | 防追高否决（涨幅 >7% / 跳空高开 >3%） |
| RSI14/7/21 | 日线 | 超卖反弹 + 底背离判断 + 多周期共振 |
| 成交量比 | 日线 | 量价配合确认 |
| 成交额(20日均值) | 日线 | 流动性过滤（僵尸股否决） |

## 数据源

**双数据源架构**：Baostock 为主、AkShare 为备，自动切换。

- **主源 Baostock**：`query_history_k_data_plus`（个股/指数日线，前复权）、`query_all_stock`（股票列表）、`query_profit_data` / `query_balance_data`（ROE/负债率）
- **备源 AkShare**：`stock_zh_a_hist`（个股日线，东财通道；不可达时自动切 `stock_zh_a_daily` 新浪通道）、`stock_zh_index_daily`（指数）、`stock_info_a_code_name`（股票列表）、`stock_financial_analysis_indicator` / `stock_balance_sheet_by_report_em` / `stock_profit_sheet_by_report_em`（基本面，额外补齐商誉/扣非）
- **切换规则**：
  - 单条取数失败（异常或返回空）→ 自动用 AkShare 兜底重取
  - Baostock 连续失败达 8 次 → 熔断，本次运行后续请求直接走 AkShare（避免逐股无效重试）
  - Baostock 登录失败 → 直接降级 AkShare 跑完全程（AkShare 未安装则报错退出）
- **连接管理**：登录真实重试（检查 error_code）、查询失败自动重连、全局线程锁防止 C++ 底层 socket 多线程踩踏
- **失败重试**：请求异常按退避重试（个股 `MAX_RETRY=2`，股票列表 `LIST_MAX_RETRY=4`）；「返回空」视为该标的确实无数据，不重试
- **两级缓存**：内存 `CacheManager`（进程内，`CACHE_EXPIRE_HOURS=4` 小时）→ `cache/` 目录磁盘缓存
  - 行情类按交易日失效：新交易日自动重拉，同日重复运行走缓存
  - 股票列表按 `CACHE_TTL_DAYS=6` 天失效，基本面按 `FUND_CACHE_TTL_DAYS=7` 天失效
  - 磁盘只缓存未过滤的原始列表，过滤在返回时应用，改 config 即时生效无需清缓存
  - 两个数据源的缓存文件命名天然隔离（Baostock 带 `sh.` 前缀、AkShare 为纯 6 位数字），互不污染
- **股票池过滤**：由 `StrategyConfig` 开关驱动（`FILTER_ST` / `EXCLUDE_DELISTING` / `EXCLUDE_BSE` 默认开启；`EXCLUDE_CHINEXT` / `EXCLUDE_STAR` 默认关闭）

## 免责声明

本策略仅为量化研究工具，不构成投资建议。策略优化旨在从逻辑上减少低质量信号，不保证提高未来收益率或胜率。实际效果必须通过严格的样本外回测验证。投资有风险，入市需谨慎。
