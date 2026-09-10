# A股日线量化选股策略 - 自动化执行系统

基于日线技术指标的量化选股策略，通过 GitHub Actions 每日自动执行，结果通过飞书机器人推送通知。采用 Baostock 主数据源 + AkShare 备用数据源的双源架构，主源失败自动切换。

## 策略简介

4层量化过滤体系：

1. **市场环境过滤**：沪深300日线MA20斜率判断牛/熊/中性环境，熊市收紧等级阈值（+10分）；指数数据缺失（regime=unknown）时保守按熊市处理（`UNKNOWN_AS_BEAR`），避免数据源故障日满额进攻
2. **基本面防雷**：年化ROE/负债率否决项（ROE 为报告期累计值，先按季度线性年化 Q1×4/Q2×2/Q3×4/3 再与阈值比较，门槛语义全年一致；目标报告期未披露时自动逐季回退至最近已披露期，避免每季度初的披露空窗让防雷层静默失效；金融业——银行/保险/券商等负债率天然 80%+，按名称关键词+代码白名单识别并单独放宽阈值，避免全行业误杀；Baostock 无商誉与扣非数据，切至 AkShare 时自动补齐商誉/扣非否决）
3. **日线技术指标筛选**：底背离 + 趋势转折（MA5拐头/EMA金叉同源合并计分）+ RSI超卖反弹 + 量价配合；流动性过滤（近20日日均成交额 ≥500万）；入场质量否决（当日涨幅 >7% 追高否决、开盘跳空高开 >3% 否决、MA20 近5日斜率 <-4% 的陡峭下降通道中趋势转折信号不认可、RSI14 >65 已反弹一段否决、量比 >5 天量否决、距60日高点回撤 <10% 非底部区域否决）；严格确认指标（现价须在近20日价格区间下半部、MACD 柱当日须改善、KDJ 须金叉且 K≤60）
4. **波动率风控 + 交易计划**：准入门槛为显式波动率上限——ATR14 不得超过现价的 3.33%（`MAX_ATR_PCT`，与旧「最低1.5倍风险收益比」在 2×ATR 止损 + 10% 止盈下数学等价，改为显式语义，高波动股以 FAIL_VOLATILE 否决）；推荐仍随附交易计划（2×ATR 止损、ATR 缺失退回固定 5% / 止盈固定 10% / 风险收益比展示值）；**决赛圈周线确认**（通过日线筛选的股票按评分降序逐个拉取周线，须同时满足站上周线 MA10（容忍2%）且 MA10 上行，另须周线 MACD 企稳——柱值翻红或绿柱连续 2 周收窄，取满即止；**周线只用已收盘 bar**——周一至周四运行时剔除本周未完成 bar，避免确认结果周内漂移）；最终推荐上限 `MAX_PICKS`（默认 5）只，目标每日 3~5 只精推；**熊市收缩**：市场环境为 bear（含 unknown 保守视同）时推荐上限收缩为 `BEAR_MAX_PICKS`（默认 2，设 0 即熊市空仓），避免熊市满额推送变相鼓励抄底

评分体系：趋势转折(40) + RSI反弹(25) + 量价配合(25) + 多周期共振(10)，满分 100。按分数分为 A/B/C/D 四个等级（A≥80 / B≥60 / C≥40）；准入门槛 `MIN_PASS_GRADE` 默认 B，C 级（仅单一趋势转折信号，40 分）与 D 级均淘汰——推荐必须同时具备趋势转折与至少一个确认信号。**趋势转折为硬性必要条件**（`REQUIRE_TREND_TURN`，默认开）：评分存在「RSI反弹25+量价配合25+共振10=60」的无拐点旁路，且该路径恰在 MA20 陡峭下降（接飞刀）场景下成立，硬性条件在任何准入等级下都要求 trend_turn 为真，无拐点信号一律 `FAIL_NO_TREND` 淘汰。底背离个股通过准入后评级额外提升一档（仅用于展示/落库，不能绕过准入门槛）。熊市环境等级门槛 +10 分。推荐排序确定性：评分↓ → 近20日日均成交额↓ → 代码↑，并列分的截取与周线确认顺序可复现。执行过程打印选股漏斗日志（各层通过率，僵尸股淘汰 FAIL_LIQUIDITY 与波动率否决 FAIL_VOLATILE 独立归因）。

## 自动执行

| 工作流 | 触发时间 | 说明 |
|--------|----------|------|
| **Daily Stock Screening** | 交易日 21:00 (北京) | 每日选股 + 推荐落库 MySQL + 飞书通知 |
| **Weekly Recommendation Tracking** | 每周五 21:20 (北京) | 推荐个股周度表现追踪（收益值/收益率落库）+ 飞书汇总 |
| **Monthly Signal Attribution** | 每月 1 日 09:00 (北京) | 信号归因月报：按等级/评分/底背离/市场环境/持有周次统计推荐后胜率与收益 + 飞书推送 |

> 定时设在 21:00 而非收盘后立刻执行，是因为 Baostock 当日数据一般 17:30 起陆续入库、20:00 前后才完整；过早触发会静默回退到昨日数据。

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
| `MYSQL_HOST` | MySQL 主机地址 | 可选（不配则跳过落库与追踪） |
| `MYSQL_PORT` | MySQL 端口，默认 3306 | 可选 |
| `MYSQL_USER` | MySQL 用户名 | 可选 |
| `MYSQL_PASSWORD` | MySQL 密码 | 可选 |
| `MYSQL_DATABASE` | MySQL 数据库名 | 可选 |

### 4. 启用 GitHub Actions

1. 进入仓库的 **Actions** 页面
2. 如果有提示，点击 **I understand my workflows, go ahead and enable them**
3. 策略将在每个交易日 21:00 自动执行

### 5. 手动测试

1. 进入 **Actions** 页面
2. 左侧选择 **Daily Stock Screening** → **Run workflow**
3. 等待执行完成，检查飞书是否收到通知

## 项目结构

```
├── .github/workflows/
│   ├── daily_screen.yml              # 每日选股工作流
│   ├── weekly_tracking.yml           # 周度追踪工作流
│   └── monthly_attribution.yml       # 信号归因月报工作流
├── src/
│   ├── bottom_fishing_strategy.py          # 核心策略（Baostock 数据源，4层过滤 + 评分）
│   ├── bottom_fishing_strategy_akshare.py  # 旧版策略备份（AkShare 数据源，未被引用）
│   └── bottom_fishing_strategy_old.py      # 更早期版本备份（未被引用）
├── notify/
│   └── feishu.py                     # 飞书通知模块
├── store/
│   └── mysql_store.py                # MySQL 持久化模块（推荐落库 + 周度追踪读写 + 归因查询）
├── schema.sql                        # MySQL 建表脚本（表结构文档，首次连接也会自动幂等建表）
├── cache/                            # 自动生成的磁盘缓存（已 gitignore）
│   ├── 个股/指数行情（按交易日失效）
│   ├── 股票列表（按 CACHE_TTL_DAYS=6 天失效）
│   └── 基本面（按 FUND_CACHE_TTL_DAYS=7 天失效）
├── run.py                            # 每日选股入口（GitHub Actions 调用）
├── run_weekly_tracking.py            # 周度追踪入口（GitHub Actions 调用）
├── run_monthly_attribution.py        # 信号归因月报入口（GitHub Actions 调用）
├── requirements.txt                  # Python 依赖
└── README.md
```

## 本地运行

```bash
# 创建并启用项目虚拟环境（已 gitignore）
python -m venv .venv
# Windows Git Bash:
source .venv/Scripts/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 设置飞书 Webhook（可选，不设置则跳过通知）
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook-id"

# 运行每日选股（含通知）
python run.py

# 手动执行周度追踪（需先配置 MYSQL_* 环境变量，见上文）
python run_weekly_tracking.py

# 同一天二次执行默认命中 cache/ 磁盘缓存（K线/指数按当日 mtime 判定，
# 股票列表 6 天、基本面 7 天）。如需强制拉最新数据，加 --no-cache：
python run.py --no-cache
python run_weekly_tracking.py --no-cache
```

GitHub Actions 手动触发（workflow_dispatch）时也提供 `no_cache` 布尔输入，勾选后等效于本地 `--no-cache`。

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

## MySQL 持久化与周度追踪（可选）

配置 `MYSQL_*` Secrets 后自动启用，未配置则跳过且不影响选股主流程。首次连接自动幂等建表（表结构见 `schema.sql`）。

**表结构**：

| 表 | 说明 | 关键约束 |
|----|------|----------|
| `stock_recommendation` | 每日推荐记录（代码/名称/推荐收盘价/评分/等级/止损止盈/RR/底背离等全字段） | `(rec_date, code)` 唯一，重复推荐自动忽略 |
| `stock_tracking` | 周度表现追踪（第几周/最新收盘价/收益值/收益率） | `(rec_id, week_no)` 唯一，外键关联推荐记录 |

**追踪口径**（`run_weekly_tracking.py`，每周五 21:20 执行）：

- 每条推荐自推荐日起**最多追踪一个月**（最多 4 次周度记录，且 31 天后强制退出，双保险）；
- 每次记录当时最新收盘价：**收益值 = 最新收盘价 − 推荐时收盘价，收益率 = 收益值 ÷ 推荐时收盘价 × 100%**；
- 若最新收盘价仍为推荐日当天（如周五新推荐），跳过且不计入追踪次数，保证 4 次都是有效观测；
- 同一股票在不同日期被重复推荐的，视为不同推荐记录各自独立追踪；
- 追踪完成后飞书推送汇总（数量/胜率/平均收益/明细）。

本地手动执行：

```bash
export MYSQL_HOST=... MYSQL_USER=... MYSQL_PASSWORD=... MYSQL_DATABASE=...
python run_weekly_tracking.py
```

## 信号归因月报（可选，需配置 MySQL）

`run_monthly_attribution.py`（每月 1 日 09:00 北京时间，GitHub Actions `monthly_attribution.yml`）基于已落库的推荐 + 追踪数据做**信号归因**——不是回测，只用真实运行数据回答"哪类信号推荐后真的涨了"：

- **收益口径**：每条推荐取最新一次周度追踪的收益率（31 天窗口内）；峰值 = 追踪期内最高周度收益率；
- **归因维度**：信号等级（A/B）、评分分档（日线基础分）、有无底背离、推荐时市场环境（牛/熊/中性）、持有周次（第 1~4 周，观察收益随持有期的衰减）；
- 每组统计样本数 / 胜率 / 平均收益 / 平均峰值收益，飞书推送月报；
- 统计窗口默认 90 天，本地可自定义：`python run_monthly_attribution.py --days 60`。

## 技术指标

| 指标 | 环节 | 用途 |
|------|------|------|
| MA5/EMA5/10/20 | 日线 | 趋势转折（拐头/金叉，同源信号合并计分） |
| MA20 斜率 | 日线 | 下降通道过滤（陡峭下降中趋势转折不认可，防接飞刀） |
| 当日涨幅/跳空幅度 | 日线 | 防追高否决（涨幅 >7% / 跳空高开 >3%） |
| RSI14 上限 / 量比上限 | 日线 | 已反弹一段否决（RSI>65）/ 天量出货否决（量比>5） |
| 60日高点回撤 | 日线 | 底部区域过滤（回撤 <10% 判定上涨中继，不买） |
| 20日区间位置 / MACD柱 / KDJ | 日线 | 严格确认：区间下半部 + MACD柱当日改善 + KDJ金叉且K≤60 |
| ATR14/现价 | 日线 | 波动率上限准入（ATR >3.33% 现价否决高波动股）+ 止损计划（2×ATR） |
| 周线 MA10 / MACD | 周线 | 决赛圈确认（站上MA10且MA10上行 + MACD柱翻红或绿柱连收2周，仅对决赛圈拉取；只用已收盘周线，剔除周内未完成 bar） |
| RSI14/7/21 | 日线 | 超卖反弹 + 底背离判断 + 多周期共振 |
| 成交量比 | 日线 | 量价配合确认 |
| 成交额(20日均值) | 日线 | 流动性过滤（僵尸股否决） |

## 数据源

**双数据源架构**：Baostock 为主、AkShare 为备，自动切换。

- **主源 Baostock**：`query_history_k_data_plus`（个股/指数日线，前复权）、`query_all_stock`（股票列表）、`query_profit_data` / `query_balance_data`（ROE/负债率；报告期未披露时逐季回退取最近已披露期，ROE 线性年化）
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
- **股票池过滤**：`MAIN_BOARD_ONLY` 默认开启——白名单仅保留普通 A 股账户可直接交易的沪深主板（60/00 开头）；创业板（开户需 10 万资产）、科创板/北交所/港股通（需 50 万资产）等对个人资金有门槛的板块，以及港股/B 股等非 A 股证券全部排除。关闭该项则退回由 `FILTER_ST` / `EXCLUDE_DELISTING` / `EXCLUDE_BSE`（默认开启）与 `EXCLUDE_CHINEXT` / `EXCLUDE_STAR`（默认关闭）组合控制

## 免责声明

本策略仅为量化研究工具，不构成投资建议。策略优化旨在从逻辑上减少低质量信号，不保证提高未来收益率或胜率。实际效果必须通过严格的样本外回测验证。投资有风险，入市需谨慎。
