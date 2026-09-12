# A股日线量化选股策略 - 自动化执行系统

基于日线技术指标的量化选股策略，通过 GitHub Actions 自动执行：**每日选股** → **每周追踪** → **每月信号归因**。推荐记录持久化到 MySQL，结果通过飞书机器人推送。

数据层采用 Baostock 主数据源 + AkShare 备用数据源的双源架构，主源失败自动切换。

## 策略简介

4 层量化过滤体系：

1. **市场环境过滤**：沪深300日线MA20斜率判断牛/熊/中性环境；熊市不扣分定级，而是把准入门槛**上浮 10 分**（`BEAR_GRADE_BOOST`），保证展示分数与等级始终同源；数据不足时明示「未知」，不隐含为正常
2. **基本面防雷**：ROE/负债率为核心否决项（金融业——银行/保险/券商等负债率天然 80%+，按名称关键词+代码白名单识别并单独放宽阈值，避免全行业误杀）；商誉/扣非为可选否决项，主源 Baostock 不提供，**决赛圈用 AkShare 按字段补齐后复核**，补齐后四项防雷才算完整执行。财报季度按**交易所披露截止日回溯**取「最近已披露季度」（最多回溯 `FUND_QUARTER_LOOKBACK`=3 季）
3. **日线技术指标筛选**：底背离（**双低点算法**：与窗口内前一个价格低点比较 RSI/DIF，而非指标自身最小值）+ 趋势转折（MA5拐头/EMA金叉同源合并计分）+ RSI超卖反弹 + **量价质量分**（放量阳线收高位/一般放量上涨/放量冲高回落三档，见下）；流动性过滤（近20日日均成交额 ≥3000万）；入场质量否决（当日涨幅 >5% 追高否决、开盘跳空高开 >2% 否决、近5日累计涨幅 >12% 已反弹一段否决、RSI14 >60 否决、量比 >4 天量否决、MA20 近5日斜率 <-4% 的陡峭下降通道中趋势转折信号不认可、距60日高点回撤 <15% 非底部区域否决、回撤 >70% 崩盘型/价值陷阱否决）；严格确认指标（现价须落在近20日价格区间下半部（≤35%）、MACD 柱须**连续 2 日**改善、KDJ 须金叉、K≤55 且 K 值上行）；**数据时效**（个股最新K线与市场最新交易日不一致，即停牌/数据滞后 → 暂不推荐）
4. **风险收益比过滤**：固定止损止盈（止损 5% / 止盈 10%）+ 最低1.5倍风险收益比；**决赛圈周线确认**（通过日线筛选的股票按评分降序逐个拉取周线，须同时满足站上周线 MA10（容忍2%）且 MA10 上行，另须周线 MACD 企稳——柱值翻红或绿柱连续 2 周收窄；**周线 MA 与周线 MACD 两个开关独立生效**；周线数据缺失或截止过旧不足以确认 → 该股进入**待核验候选**，不占正式推荐名额；取满即止）；**行业分散**（同一行业最多推荐 2 只 `MAX_PICKS_PER_INDUSTRY`，避免 Top5 集中单一板块导致组合同涨同跌）；最终推荐上限 `MAX_PICKS`（默认 5）只，目标每日 3~5 只精推

**荐股质量分层（数据缺失不等于筛选通过）**：

| 层级 | 条件 | 处理 |
|------|------|------|
| **正式推荐**（formal） | 财务核心项（ROE+负债率）已核验、启用的周线确认已确认、行情日期与市场最新交易日一致 | 落库 MySQL、进入飞书正式名单、参与周度追踪 |
| **待核验候选**（pending） | 技术面通过，但财务或周线数据缺失（含商誉/扣非未补齐、周线滞后） | 仅随通知展示，**不落库、不参与追踪、不与正式推荐混排** |
| 暂不推荐 | 行情日期滞后（停牌/数据过期）或任一项否决条件未过 | 直接淘汰 |

评分体系：趋势转折(40) + RSI反弹(25) + 量价质量分(满分25) + 多周期共振(10) − RSI超买惩罚，满分 100；量价质量分三档：**25 分 放量企稳**（阳线、收盘位于日内区间上部 ≥60%、上影线 ≤35%）、**18 分 放量上涨**（一般）、**10 分 放量冲高回落**（收盘低于日内区间 35% 位置，只降分不否决）。按分数分为 A/B/C/D 四个等级（A≥80 / B≥60 / C≥40），**等级与展示分数同源（底背离不再升档）**；准入门槛 `MIN_PASS_GRADE` 默认 B，C 级（仅单一趋势转折信号，40 分）与 D 级均淘汰。**排序**：评分降序 → 底背离优先 → 盈亏比降序 → **股票代码升序（末级键）**，保证两次运行结果可复现，不受并发完成顺序影响。执行过程打印选股漏斗日志（各层通过率 + 行情时效剔除数 + 待核验候选数）。

## 自动执行

| 工作流 | 触发时间（北京） | 定时表达式（UTC） | 说明 |
|--------|------------------|-------------------|------|
| **Daily Stock Screening** | 交易日 21:00 | `0 13 * * 1-5` | 每日选股 → 落库 MySQL → 飞书通知 |
| **Weekly Recommendation Tracking** | 每周五 21:20 | `20 13 * * 5` | 统计追踪期内推荐的最新表现 → 落库 → 飞书汇总 |
| **Monthly Signal Attribution** | 每月 1 日 09:00 | `0 1 1 * *` | 各信号维度胜率/收益归因 → 飞书月报 |

三个工作流均支持手动触发（`workflow_dispatch`）。

- **为什么是 21:00**：Baostock 一般 17:30 起陆续更新当日数据、20:00 前完成，21:00 起跑留足安全边际。定时任务的选股**强制不使用磁盘缓存**（`--no-cache`），保证数据全部当天最新；手动触发则由 `no_cache` 勾选项决定。
- **周五 21:20 的用意**：晚于当日 21:00 的选股任务 20 分钟，确保当日新推荐已落库，再由周任务决定首期追踪。
- **缓存传递**：三个工作流共用 `actions/cache` 的 `baostock-cache-<日期>`（`cache/` 目录）。行情按「当日 mtime」判新鲜度、必然重拉，股票列表（6天）与基本面（7天）在 TTL 内直接复用。
- 每日任务结束时将 `data/` 目录上传为 artifact（构建产物预留位，当前策略不写入文件）。

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
| `FEISHU_WEBHOOK_URL` | 飞书 Webhook 地址 | 推荐（不配则跳过全部通知） |
| `MYSQL_HOST` | MySQL 主机地址 | 可选（不配则跳过落库与追踪/归因） |
| `MYSQL_PORT` | MySQL 端口，缺省 3306 | 可选 |
| `MYSQL_USER` | MySQL 用户 | 可选 |
| `MYSQL_PASSWORD` | MySQL 密码 | 可选 |
| `MYSQL_DATABASE` | 数据库名（如 `stock_fishing`） | 可选 |

> **落库为可选能力**：`MYSQL_HOST` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` 四项未配齐时，落库、周度追踪、信号归因全部静默跳过，不影响选股主流程。

### 4. 初始化数据库（可选）

程序首次连接时会自动执行 `CREATE TABLE IF NOT EXISTS`（幂等），**无需手动建表**。如需手动建库，用仓库内的 `schema.sql`：

```sql
CREATE DATABASE IF NOT EXISTS stock_fishing DEFAULT CHARSET utf8mb4;
USE stock_fishing;
SOURCE schema.sql;
```

### 5. 启用 GitHub Actions

1. 进入仓库的 **Actions** 页面
2. 如果有提示，点击 **I understand my workflows, go ahead and enable them**
3. 三个工作流将按上表时间自动执行

### 6. 手动测试

1. 进入 **Actions** 页面
2. 左侧选择 **Daily Stock Screening** → **Run workflow**
3. 等待执行完成，检查飞书是否收到通知

## 项目结构

```
├── .github/workflows/
│   ├── daily_screen.yml              # 每日选股（交易日 21:00）
│   ├── weekly_tracking.yml           # 周度追踪（周五 21:20）
│   └── monthly_attribution.yml       # 信号归因月报（每月 1 日 09:00）
├── src/
│   ├── bottom_fishing_strategy.py          # 核心策略（4层过滤 + 评分 + 双数据源）
│   ├── bottom_fishing_strategy_akshare.py  # 旧版策略备份（AkShare 数据源，未被引用）
│   └── bottom_fishing_strategy_old.py      # 更早期版本备份（未被引用）
├── store/
│   └── mysql_store.py                # MySQL 持久化：推荐落库 / 周度追踪 / 归因查询
├── notify/
│   └── feishu.py                     # 飞书通知：选股结果 / 周度追踪 / 归因月报
├── cache/                            # 自动生成的磁盘缓存（已 gitignore）
│   ├── 个股/指数行情（按交易日失效）
│   ├── 股票列表（按 CACHE_TTL_DAYS=6 天失效）
│   ├── 基本面（按 FUND_CACHE_TTL_DAYS=7 天失效，缓存名带季度标签）
│   └── 行业分类（按 INDUSTRY_CACHE_TTL_DAYS=30 天失效）
├── data/                             # artifact 上传目录（预留，当前不写入文件）
├── run.py                            # 每日选股入口（GitHub Actions 调用）
├── run_weekly_tracking.py            # 周度追踪入口
├── run_monthly_attribution.py        # 信号归因月报入口
├── test_entry_filters.py             # 入场质量过滤器单测（合成数据，不联网）
├── schema.sql                        # MySQL 表结构（文档 + 手动建库参考）
├── requirements.txt                  # Python 依赖
└── README.md
```

## 本地运行

### 安装依赖

```bash
pip install -r requirements.txt
```

### 每日选股

```bash
# 设置飞书 Webhook（可选，不设置则跳过通知）
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook-id"

# MySQL（可选，不设置则跳过落库）
export MYSQL_HOST=127.0.0.1 MYSQL_USER=root MYSQL_PASSWORD=xxx MYSQL_DATABASE=stock_fishing

# 运行每日选股（选股 → 落库 → 通知）
python run.py

# 强制跳过 cache/ 磁盘缓存，全部从数据源重拉
python run.py --no-cache
```

### 周度追踪

```bash
python run_weekly_tracking.py            # 需已配置 MySQL，否则直接退出
python run_weekly_tracking.py --no-cache
```

### 信号归因月报

```bash
python run_monthly_attribution.py            # 默认统计近 90 天
python run_monthly_attribution.py --days 30
```

### 直接运行策略脚本（不落库、不发送通知）

```bash
# 仅选股，打印明细
python src/bottom_fishing_strategy.py screen

# 完整流程（默认模式）
python src/bottom_fishing_strategy.py full
```

### 运行单元测试

两个测试均使用合成数据/打桩数据源、不访问网络，改动策略或主流程后应先跑它们：

```bash
python test_entry_filters.py    # 单只判定：15 个入场场景 + 68 项断言（分层/时效/双低点背离/量价分档/评级同源/排序可复现）
python test_main_layering.py    # main() 编排：周线路径与禁用路径下的正式推荐/待核验候选分层（13 项断言）
```

## 数据持久化

两张表，首次连接时自动创建（`CREATE TABLE IF NOT EXISTS`，幂等）：

| 表 | 写入方 | 说明 |
|----|--------|------|
| `stock_recommendation` | `run.py`（每日） | 每日推荐明细，`(rec_date, code)` 唯一，重复写入自动忽略 |
| `stock_tracking` | `run_weekly_tracking.py`（每周） | 周度表现追踪，`(rec_id, week_no)` 唯一 |

**追踪口径**：每条推荐自推荐日起最多记录 4 次周度追踪（`TRACK_MAX_WEEKS`），且超过 31 天（`TRACK_MAX_AGE_DAYS`）强制退出——双保险，防止周任务中断导致超期。收益率 =（最新收盘价 − 推荐时收盘价）/ 推荐时收盘价 × 100%。同一股票在不同日期被重复推荐，视为不同记录、各自独立追踪。

**信号归因**：`run_monthly_attribution.py` 基于两表历史数据统计各信号维度（等级 / 评分分档 / 底背离 / 市场环境 / 持有周次）的胜率与收益，用于用真实运行数据反哺信号质量评估。**这不是回测**——只统计已落库的真实推荐与真实追踪收益，因此样本需要时间积累。

> **只有 `rec_tier='formal'` 的正式推荐会落库**；待核验候选（数据缺失）不写库、不参与追踪，避免污染归因样本。表新增 `signals_hit`（入选依据）、`fund_status` / `weekly_status`（核验状态）、`rec_tier`（层级）、`missing_tags`（缺项）五列，程序首次连接会自动建表并对**存量表**按 `information_schema` 探测后 `ALTER` 补齐（幂等迁移），无需手动改表。

## 通知样例

推送到飞书群的消息卡片分三类：

| 卡片 | 触发 | 内容 |
|------|------|------|
| 选股结果 | 每日选股后 | 市场环境、正式推荐数量（低位企稳候选）、每只股票的代码/名称/评分/等级/收盘价/止损/止盈/风险收益比 + **入选依据**（触发条件）+ **核验状态**（财务/周线是否已核验、缺项）；另附**待核验候选**独立区块（说明缺什么、为何未进正式推荐）；无正式推荐时提示「今日无信号」，执行异常时推送错误信息 |
| 周度追踪 | 每周追踪后 | 追踪数量、胜率、平均收益、状态分布（第 N/4 周）及逐只明细（推荐价 → 现价 → 收益） |
| 信号归因月报 | 每月归因后 | 统计周期、已追踪推荐数、整体胜率、平均收益/平均峰值收益，以及按等级/评分分档/底背离/市场环境/持有周次的分组统计 |

## 技术指标

| 指标 | 环节 | 用途 |
|------|------|------|
| MA5/EMA5/10/20 | 日线 | 趋势转折（拐头/金叉，同源信号合并计分） |
| MA20 斜率 | 日线 | 下降通道过滤（陡峭下降中趋势转折不认可，防接飞刀） |
| 当日涨幅/跳空幅度 | 日线 | 防追高否决（涨幅 >5% / 跳空高开 >2%） |
| 近5日累计涨幅 | 日线 | 已反弹一段否决（累计 >12%，比 RSI 更灵敏） |
| RSI14 上限 / 量比上限 | 日线 | 已反弹一段否决（RSI>60）/ 天量出货否决（量比>4） |
| 60日高点回撤（下限/上限） | 日线 | 底部区域过滤（回撤 <15% 判上涨中继；>70% 判崩盘型/价值陷阱） |
| 20日区间位置 / MACD柱 / KDJ | 日线 | 严格确认：区间下半部（≤35%）+ MACD柱连续2日改善 + KDJ金叉、K≤55 且 K 上行 |
| 周线 MA10 / MACD | 周线 | 决赛圈确认（站上MA10且MA10上行 + MACD柱翻红或绿柱连收2周，仅对决赛圈拉取；两个开关独立生效；数据缺失/滞后 → 待核验候选） |
| RSI14/7/21 | 日线 | 超卖反弹 + 底背离判断（前一个价格低点当根的 RSI 对比）+ 多周期共振 |
| 成交量比 + 收盘位置/实体/上影线 | 日线 | 量价质量分（放量企稳 25 / 放量上涨 18 / 放量冲高回落 10） |
| 最新K线日期 | 日线 | 数据时效（与沪深300最新交易日不一致 → 停牌/滞后，暂不推荐） |
| 成交额(20日均值) | 日线 | 流动性过滤（<3000万 否决） |
| 行业分类 | 集中度 | 行业分散（同一行业最多 2 只，避免单一板块押注） |

## 数据源

**双数据源架构**：Baostock 为主、AkShare 为备，自动切换。

- **主源 Baostock**：`query_history_k_data_plus`（个股/指数日线，前复权）、`query_all_stock`（股票列表）、`query_stock_industry`（行业分类，用于行业分散）、`query_profit_data` / `query_balance_data`（ROE/负债率）
- **备源 AkShare**：`stock_zh_a_hist`（个股日线，东财通道；不可达时自动切 `stock_zh_a_daily` 新浪通道）、`stock_zh_index_daily`（指数）、`stock_info_a_code_name`（股票列表）、`stock_financial_analysis_indicator` / `stock_balance_sheet_by_report_em` / `stock_profit_sheet_by_report_em`（基本面，**决赛圈按字段补齐**商誉/扣非与主源缺失的核心项，只填 None 字段、不覆盖主源）
- **切换规则**：
  - 单条取数失败（异常或返回空）→ 自动用 AkShare 兜底重取
  - Baostock 连续失败达 8 次 → 熔断，本次运行后续请求直接走 AkShare（避免逐股无效重试）
  - Baostock 登录失败 → 直接降级 AkShare 跑完全程（AkShare 未安装则报错退出）
- **连接管理**：登录真实重试（检查 error_code）、查询失败自动重连、全局线程锁防止 C++ 底层 socket 多线程踩踏
- **失败重试**：请求异常按退避重试（个股 `MAX_RETRY=2`，股票列表 `LIST_MAX_RETRY=4`）；「返回空」视为该标的确实无数据，不重试
- **两级缓存**：内存 `CacheManager`（进程内，`CACHE_EXPIRE_HOURS=4` 小时）→ `cache/` 目录磁盘缓存
  - 行情类按交易日失效：新交易日自动重拉，同日重复运行走缓存
  - 股票列表按 `CACHE_TTL_DAYS=6` 天、基本面按 `FUND_CACHE_TTL_DAYS=7` 天、行业分类按 `INDUSTRY_CACHE_TTL_DAYS=30` 天失效
  - 磁盘只缓存未过滤的原始列表，过滤在返回时应用，改 config 即时生效无需清缓存
  - 两个数据源的缓存文件命名天然隔离（Baostock 带 `sh.` 前缀、AkShare 为纯 6 位数字），互不污染
- **股票池过滤**：`MAIN_BOARD_ONLY` 默认开启——白名单仅保留普通 A 股账户可直接交易的沪深主板（60/00 开头）；创业板（开户需 10 万资产）、科创板/北交所/港股通（需 50 万资产）等对个人资金有门槛的板块，以及港股/B 股等非 A 股证券全部排除。关闭该项则退回由 `FILTER_ST` / `EXCLUDE_DELISTING` / `EXCLUDE_BSE`（默认开启）与 `EXCLUDE_CHINEXT` / `EXCLUDE_STAR`（默认关闭）组合控制

## 免责声明

本策略仅为量化研究工具，不构成投资建议。策略优化旨在从逻辑上减少低质量信号，不保证提高未来收益率或胜率。实际效果必须通过严格的样本外回测验证。投资有风险，入市需谨慎。
