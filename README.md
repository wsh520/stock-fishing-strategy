# A股日线量化选股策略 - 自动化执行系统

基于日线技术指标的量化选股策略，通过 GitHub Actions 自动执行：**每日选股** → **每周追踪** → **每月信号归因**。推荐记录持久化到 MySQL，结果通过飞书机器人推送。

数据层采用 Baostock 主数据源 + AkShare 备用数据源的双源架构，主源失败自动切换。

**双策略并行**（共用同一套数据层 / 基本面防雷 / 市场环境 / 周线确认 / 并发筛选骨架）：

| 策略 | 定位 | 信号类型 | 推荐数量（牛/中/熊） |
|------|------|----------|----------------------|
| **抄底策略**（bottom_fishing） | 左侧逆势 | 底背离 + 趋势转折 + 超卖反弹 | 5 / 4 / 2（已确认熊市门槛另 +10 分） |
| **放量突破策略**（volume_breakout） | 右侧顺势 | 横盘末端放量突破关键阻力位 | 5 / 3 / 0（熊市空仓） |

两策略市场暴露互补；组合层做同股去重与每日总量上限（`DAILY_TOTAL_MAX_PICKS`=7）。市场环境为「未知」（指数数据缺失）时，经 `UNKNOWN_AS_BEAR` 折叠为熊市**只作用于推荐数量上限**（收缩为 2 只）；准入分数线不因「未知」上浮（门槛上浮只对已确认的 `bear` 生效）。

抄底策略另有**市场级熔断**：沪深300 近 5 个交易日累计跌幅 ≤ −4% 时本次运行直接不推荐——急跌期间全市场同步满足 RSI 超卖 / MA5 拐头 / 创新低底背离，个股级斜率过滤识别不了这种系统性风险。

## 策略一：抄底（bottom_fishing_strategy.py）

4 层量化过滤体系：

1. **市场环境过滤**：沪深300日线MA20斜率判断牛/熊/中性环境；熊市不扣分定级，而是把准入门槛**上浮 10 分**（`BEAR_GRADE_BOOST`），保证展示分数与等级始终同源；数据不足时明示「未知」（`UNKNOWN_AS_BEAR`：未知经 `_effective_regime` 折叠为熊市，作用于推荐数量上限的收缩；**门槛上浮只对已确认的 `bear` 生效**——`evaluate()` 按原始 regime 判定，与突破策略 `evaluate_breakout()` 用有效 regime 判门槛的做法不同）；**regime 滞回**（`MARKET_REGIME_HYSTERESIS`）：牛/熊/中性切换须连续 2 个交易日同向确认，避免斜率在阈值附近抖动导致 regime 逐日跳变（状态存 `cache/market_regime_state.json`，超 10 天自动重置；**确认计数每个自然日最多推进一次**——同一天内多套策略依次运行读的是同一份收盘数据，若允许重复计数会把「连续 2 个交易日」悄悄缩短为「同日翻转」）；**市场级熔断**（`MARKET_CRASH_HALT_PCT`=−4% / `MARKET_CRASH_LOOKBACK`=5）：沪深300 近 5 个交易日累计跌幅越阈 → 本次运行不推荐（数据不足不触发，避免指数缺数误判）
2. **基本面防雷**：年化ROE/负债率为核心否决项（金融业——银行/保险/券商等负债率天然 80%+，按名称关键词+代码白名单识别并单独放宽阈值）；商誉/扣非为可选否决项，主源 Baostock 不提供，**决赛圈用 AkShare 按字段补齐后复核**。**ROE 口径为线性年化**（Q1×4 / Q2×2 / Q3×4/3 / Q4×1，累计值年化后才与 `MIN_ROE` 年化阈值可比，不再随财报日历漂移）；财报季度按**交易所披露截止日回溯**取「最近已披露季度」（最多回溯 `FUND_LOOKBACK_QUARTERS`=4 季），结果带 `report_period` 标明数据实际所属报告期；基本面磁盘缓存为 `fund_v2_*`（旧 `fund_*` 单季未年化口径已隔离废弃）
3. **日线技术指标筛选**：底背离（**双低点算法**：与窗口内前一个价格低点比较 RSI/DIF，而非指标自身最小值）+ 趋势转折（MA5拐头/EMA金叉同源合并计分）+ RSI超卖反弹 + **量价质量分**（放量阳线收高位/一般放量上涨/放量冲高回落三档）；流动性过滤（近20日日均成交额 ≥3000万，独立归因 `FAIL_LIQUIDITY`）；入场质量否决（当日涨幅 >5% 追高否决、开盘跳空高开 >2% 否决、近5日累计涨幅 >12% 已反弹一段否决、RSI14 >60 否决、量比 >4 天量否决、MA20 近5日斜率 <-4% 的陡峭下降通道中趋势转折信号不认可、距60日高点回撤 <10% 非底部区域否决、回撤 >70% 崩盘型/价值陷阱否决）；严格确认指标（现价须落在近20日价格区间下半部、MACD 柱须**连续 2 日**改善、KDJ 须金叉、K≤55 且 K 值上行）；**数据时效**（个股最新K线与市场最新交易日不一致，即停牌/数据滞后 → 暂不推荐）；**停牌缺口**（相邻 K 线自然日间隔 >12 天判定为期间曾停牌 → 暂不推荐，独立归因 `FAIL_HALT_GAP`：这类股票的 20 日均量/60日高点/ATR 全部跨缺口计算，「60日高点」可能实为数月前的高点）
4. **波动率风控**：ATR 占现价百分比 >3.33% 直接否决（`MAX_ATR_PCT` / `FAIL_VOLATILE`——与旧「RR≥1.5」数学等价但语义直白，且修复了旧实现 ATR 缺失时 RR 恒 2.0 永不否决的漏洞）；止损/止盈/盈亏比（2×ATR 或固定 5% 止损、固定 10% 止盈）**仅作展示与落库，不参与否决**；**决赛圈周线确认**（须同时满足站上周线 MA10（容忍2%）且 MA10 上行，另须周线 MACD 企稳——柱值翻红或绿柱连续 2 周收窄；**只用已收盘周 bar**——末根周线落在本 ISO 周且非周五即剔除，避免半成品 bar 污染口径；周线 MA 与周线 MACD 两个开关独立生效；数据缺失/截止过旧 → 待核验候选，不占正式名额；取满即止）；**行业分散**（同一行业最多 2 只）；**推荐数量按市场环境收缩**（牛 `MAX_PICKS`=5 / 中性 `NEUTRAL_MAX_PICKS`=4 / 熊 `BEAR_MAX_PICKS`=2，未知经 `_effective_regime` 折叠为熊，统一由 `resolve_max_picks` 解析，周线确认取满即止）

**荐股质量分层（数据缺失不等于筛选通过）**：

| 层级 | 条件 | 处理 |
|------|------|------|
| **正式推荐**（formal） | 财务核心项（ROE+负债率）已核验、启用的周线确认已确认、行情日期与市场最新交易日一致 | 落库 MySQL、进入飞书正式名单、参与周度追踪 |
| **待核验候选**（pending） | 技术面通过，但财务或周线数据缺失（含商誉/扣非未补齐、周线滞后） | 仅随通知展示，**不落库、不参与追踪、不与正式推荐混排** |
| **波动率观察池**（volatile） | 前置各层已通过（抄底：技术分已达准入线；突破：突破/量能/形态/趋势已过），**仅 ATR 占现价百分比超上限**被否决 | 仅日志 + 飞书**高风险观察池**区块（标注风险等级与风险提示），**不落库、不参与追踪、不是买入建议** |
| 暂不推荐 | 行情日期滞后（停牌/数据过期）或任一项否决条件未过 | 直接淘汰 |

评分体系：趋势转折(40) + RSI反弹(25) + 量价质量分(满分25) + 多周期共振(10)，满分 100；量价质量分三档：**25 分 放量企稳**（阳线、收盘位于日内区间上部 ≥60%、上影线 ≤35%）、**18 分 放量上涨**（一般）、**10 分 放量冲高回落**（收盘低于日内区间 35% 位置，只降分不否决）。按分数分为 A/B/C/D 四个等级（A≥80 / B≥60 / C≥40），**等级与展示分数同源（底背离不升档）**；准入门槛 `MIN_PASS_GRADE` 默认 B。**排序**：主键 `rank_score`（= 技术分 + 连续质量分）降序 → 底背离优先 → 盈亏比降序 → **股票代码升序（末级键）**，保证两次运行结果可复现。**连续质量分**（`RANK_QUALITY_WEIGHT`=10，置 0 可完全关闭）只用于打破布尔闸门造成的分数并列（技术分实际只落在 {60,65,68,75,83,90,93,100} 等离散值上），**不参与任何门槛判定**——「哪些股票通过」与引入前完全一致，变的只是通过者之间的先后：低波动 0.35 + 回撤深度 0.35 + 20 日区间位置 0.30（MACD 动能维度权重默认 0，因前置闸门已保证通过者恒为满分），四路取值全部复用已算出的列，零额外取数。执行过程输出选股漏斗日志（各层通过率 + 时效剔除数 + 停牌缺口剔除数 + 待核验候选数 + 取数来源统计）。**波动率风控否决明细**（`[VOLATILE]` 区块）会逐只打印：代码/名称/技术分/等级/收盘/ATR%与上限及倍数/风险档/RSI/量比/已达标项，最多 20 只，超出提示剩余条数；若该层当天零淘汰且日线无通过标的，则改为直接点出主要拦截层（避免「为什么没推荐」无从复核）。

> **保底观察候选（fallback）默认关闭**（`ENABLE_DAILY_FALLBACK=False`）：与「宁可少荐」哲学一致，不再每天硬推一只低置信度票；置 True 可恢复旧行为（候选明确标记 `tier=fallback`，仅作观察，不应直接实盘）。

## 策略二：放量突破（volume_breakout_strategy.py）

捕捉「横盘整理末端 → 放量突破关键阻力位」的趋势启动点。骨架、数据层、基本面防雷、市场环境、决赛圈周线确认全部继承抄底策略，只替换日线信号与评估层（七层漏斗）：

1. **基本面防雷**（继承 `check_fundamentals`）
2. **流动性 & 数据完整性**（近 20 日日均成交额 ≥ `MIN_AMOUNT`）
3. **日线技术面（核心）**：
   - 三级阻力位：**L1=近 60 日最高 / L2=近 20 日最高 / L3=MA60**（`REQUIRE_L1_OR_L2` 默认关闭 L3，避免「低位整理股反弹站上 MA60」被误判为突破）
   - **放量四重确认**：量比 1.8–4.0 + 当日成交额 ≥1 亿 + 成交量处于自身 60 日 80% 分位以上 + 当日成交额 ≥ 前 20 日成交额中位数 ×1.5
   - **K 线形态防假突破**：阳线实体占比 ≥0.5、收盘/最高 ≥0.97、涨幅 2%–7%、跳空 ≤2%（旧 `MAX_GAP_UP_PCT`）
   - **突破前平台整理**：近 20 日振幅 ≤18%、收盘离散度（std/mean）≤4%（窗口右端 `shift(3)` 避开突破 bar 污染）
   - **趋势背景**：MA20 上行、MA60 走平或上行、现价站上 MA20
   - **近期假突破过滤**：近 10 日无「突破 L1 后 3 日内跌回」记录（事件锚定突破日 L1；**向量化实现**，区间差分替代三重循环）
   - 突破**前一日** RSI ≤80（突破日天然推高 RSI，检查当日会误杀正常突破）
4. **波动率风控**：ATR ≤4.0% 现价（比抄底 3.33% 略放宽）
5. **综合评分与等级**：突破强度(35，**0.55×级别 + 0.45×幅度** 加法混合——替代旧纯乘法，旧公式下 L2/L3 级别系数把幅度分压得极低、B 级准入事实上只推 L1) + 量能质量(25，0.7×放量 + 0.3×整理充分度) + 平台整理(15) + 趋势背景(15) + 动能确认(10)；B 级 ≥60 准入，熊市 +15
6. **决赛圈周线确认**（继承 `check_weekly_trend` + `check_weekly_macd`，同样只用已收盘周 bar）
7. **排序截取**：评分↓ → 突破级别↓ → 20日均成交额↓ → 代码↑；牛 5 / 中性 3 / **熊 0（直接空仓）**

**交易计划**（仅展示与落库）：止损 = max(突破位 − 1×ATR, 现价 × (1 − 6%))（保证最大亏损上限；技术位不低于执行价时才采用技术位，否则回退固定 6%），止盈 = min(现价 × 1.15, 现价 + 5×ATR)（固定目标与 ATR 目标取更近者）。`MIN_RR_RATIO_BREAKOUT` 检查已删除（止损≤6%/止盈15% 下 RR≥2.5 恒成立，检查永不触发）。

**盘中剔除用北京时间**：15:00（东八区）前运行剔除当日未收盘 bar，防止盘中漂移；GitHub Actions 为 UTC，用本地时区会把「当日已收盘 bar」误剔除导致信号系统性滞后一天（已修复）。

```bash
python run_breakout.py                # 放量突破选股 → 组合层去重/总量上限 → 落库 → 飞书通知
python run_breakout.py --no-cache     # 跳过磁盘缓存
python run_breakout.py --no-save      # 仅通知，不落库
python run_breakout.py --no-notify    # 仅落库，不通知
```

## 运行保障

- **统一 logging**：全项目 `%(asctime)s [%(levelname)s] %(name)s` 格式，入口 `run.py` / `run_breakout.py` / `run_weekly_tracking.py` 与策略 `__main__` 各自 `_setup_logging()`
- **防假死**：模块级 `socket.setdefaulttimeout(20)`——数据源底层 socket 默认无超时，曾致持有锁线程永久阻塞、全池假死跑满 6h 被取消
- **共享并发筛选骨架** `run_concurrent_screen`（两策略同一模板）：线程池 + 心跳看门狗（每 3 分钟心跳，连续 4 分钟无完成打印在途股票代码定位卡点）+ 时间预算（`SCREEN_TIME_BUDGET_MIN`=240 分钟，超时取消未完成任务、按已完成结果出报告，漏斗统计按已处理数计）
- **取数来源统计**：`fetch_stats`（Baostock 命中 / AkShare 兜底 / 双源均失败），漏斗末尾汇总
- **确定性排序**：抄底策略主键 `rank_score`、末级键股票代码升序（突破策略为 评分→级别→20日均额→代码），两次运行结果可复现，不受并发完成顺序影响

## 自动执行

| 工作流 | 触发时间（北京） | 定时表达式（UTC） | 说明 |
|--------|------------------|-------------------|------|
| **Daily Stock Screening** | 交易日 21:00 | `0 13 * * 1-5` | 抄底策略选股 → 落库 → 飞书通知；随后**同一 runner 内**顺序执行放量突破策略（复用前者的当日行情缓存）→ 组合层去重/总量上限 → 落库 → 飞书通知 |
| **Weekly Recommendation Tracking** | 每周五 21:20 | `20 13 * * 5` | 统计追踪期内推荐的最新表现 → 落库 → 飞书汇总（按策略分列） |
| **Monthly Signal Attribution** | 每月 1 日 09:00 | `0 1 1 * *` | 各信号维度胜率/收益归因 → 飞书月报 |

三个工作流均支持手动触发（`workflow_dispatch`）；`Daily Stock Screening` 额外支持以下勾选项：

| 勾选项 | 默认 | 作用 |
|--------|------|------|
| `reuse_cache` | **开** | 手动运行时复用「最近一次收盘快照」——把恢复回来的行情缓存标记为今日有效，**完全不拉行情**，几分钟跑完一轮（详见下方缓存策略） |
| `purge_cache` | 关 | 手动运行时也清除行情缓存、强制重新拉取，用于复现定时运行的取数行为 |
| `no_cache` | 关 | 两套策略都跳过 `cache/` 读写（既不读也不写，本次不共享） |
| `no_save` | 关 | 两套策略都跳过 MySQL 落库（纯测试运行，避免测试推荐与晚间正式结果混在同一天） |
| `no_notify` | 关 | 两套策略都跳过飞书通知 |
| `skip_breakout` | 关 | 只跑抄底，跳过放量突破 |
| `breakout_no_save` / `breakout_no_notify` | 关 | 只针对突破策略跳过落库 / 通知（细粒度控制，与 `no_save` / `no_notify` 取或） |

- **为什么是 21:00**：Baostock 一般 17:30 起陆续更新当日数据、20:00 前完成，21:00 起跑留足安全边际。
- **两套策略为什么合并进同一个 job**：抄底与突破的数据层是同一组函数（`get_daily_data` / `get_fundamentals` / `get_stock_list` 由突破模块直接导入），`DAILY_BARS` / `WEEKLY_BARS` / `ADJUST` / `CACHE_DIR` 也完全一致，因此**缓存文件名逐字相同**（`daily_sh.600000_last120_qfq.csv`）。放在同一个 job 里顺序执行，二者共用同一个 `cache/` 目录：抄底全量拉取并写入当天行情，突破随后直接读同一份文件，**几乎零重拉**。相比两条独立流水线，既省掉约 3000 次日线 + 数千次基本面查询（这些查询被全局锁 `bs_lock` 串行化，正是运行时长的大头），也天然保证顺序——组合层去重由**后运行者**执行（`fetch_rec_codes_for_date` 剔除当日已被抄底推荐的个股，并按 `DAILY_TOTAL_MAX_PICKS` 默认 7 截取合计上限），不再依赖 cron 时间差。
- **`if: always()` 的用意**：突破步骤带 `if: always()`，抄底失败/异常时突破仍会执行——两套策略是独立信号源，不应互相阻塞；抄底未写出的缓存由突破自行补拉（慢但结果正确）。
- **缓存策略：定时「必新」、手动「复用」两套通道**。定时运行**使用**磁盘缓存（不再强制 `--no-cache`）——这是跨策略共享的前提；为消除「恢复回来的行情缓存是否可信」这一隐患（其新鲜度依赖文件 mtime，而复用与否取决于 `actions/cache` 是否保留 mtime），定时运行会先执行 `Purge restored quote cache`，**一律删除** `daily_*` / `weekly_*` / `index_daily_*`：这三类由抄底重新全量拉取写入，突破复用的是**本次运行刚写下的**文件。保留 `stock_list`（6天）/ `fund_v2`（7天）/ `industry`（30天）/ `market_regime_state.json`——它们是真正时间不敏感的数据，用 TTL 判定、与 mtime 无关，跨天复用正确且必要（基本面每天约 6000 次查询正是靠 7 天 TTL 才不必重跑）。
  - **手动运行走另一个分支**：`Purge restored quote cache` 只在 `schedule` 或勾选 `purge_cache` 时执行；否则由 `Reuse last close snapshot` 步骤把行情缓存 `touch` 成今天，让 `_cache_fresh_today` 判定为有效 —— 白天测试**一个行情请求都不发**。这不是「拿旧数据凑数」：Baostock 当日数据 17:30 后才陆续入库，17:00 前全量拉取拿回的本来也就是「昨收为止」的同一批 K 线，与缓存内容一致；周线剔除（`_drop_incomplete_weekly_bar`）等逻辑判的是 bar 自身的 `date` 列而非 mtime，因此结果与全量拉取逐字相同。
  - 因此缓存 key 带 run id（`baostock-cache-<UTC 日期>-<run_id>`，`restore-keys` 为「当天前缀 → 全局前缀」）：`actions/cache` 的 key 不可变、同 key 二次保存会被跳过，若只用日期做 key，「当天第一次运行」（常是上午的手动测试，行情只到昨收）写下的缓存会锁死当天，21:00 定时任务重新拉取的当日最新行情反而存不进去，晚上再手动运行就会命中那份「收盘前」的旧快照。
- **regime 滞回状态跨策略共享**：`cache/market_regime_state.json` 也在这份缓存里，因此两套策略读到**同一个已确认 regime**（此前两条独立流水线各维护一条状态链，可能互相不一致）。为避免「一天内运行两次 = 确认计数推进两次」把 `MARKET_REGIME_CONFIRM_DAYS=2` 悄悄退化为「同日翻转」，`_apply_regime_hysteresis` 已改为**每个自然日最多推进一次确认计数**（依据状态文件自身的 `updated` 日期，对「一天跑几次」完全鲁棒）。
- **周五 21:20 的用意**：晚于当日 21:00 的选股任务 20 分钟，确保当日新推荐已落库，再由周任务决定首期追踪。
- 缓存传递：`Daily Stock Screening` 与 `Weekly Recommendation Tracking` 使用 `actions/cache` 的 `cache/` 目录，共用前缀 `baostock-cache-<日期>`（每日任务另带 `run_id` 后缀，周任务仍按日期存取，靠 `restore-keys` 前缀命中当天最新一份）；归因月报只查 MySQL 不拉行情，因此不涉及缓存。每日任务结束时上传 artifact（`data/` 与 `cache/market_regime_state.json`，`if-no-files-found: ignore`）便于回溯 regime 滞回状态。

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

程序首次连接时会自动执行 `CREATE TABLE IF NOT EXISTS`（幂等），**无需手动建表**。存量表会自动幂等迁移：补齐 `signals_hit` / `strategy` 等新列，并把旧唯一键 `(rec_date, code)` 升级为 `(rec_date, code, strategy)`（历史行 `strategy` 回填默认值 `'bottom_fishing'`）。如需手动建库，用仓库内的 `schema.sql`：

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
2. 左侧选择 **Daily Stock Screening** → **Run workflow**（一次运行会依次执行抄底与放量突破两套策略）
3. 只想验证突破的信号质量、暂不写库时，勾选 `breakout_no_save`；只想跑抄底时勾选 `skip_breakout`
4. 等待执行完成，检查飞书是否收到通知

## 项目结构

```
├── .github/workflows/
│   ├── daily_screen.yml              # 每日选股（交易日 21:00，单 job 顺序跑抄底 + 放量突破，共享 cache/）
│   ├── weekly_tracking.yml           # 周度追踪（周五 21:20）
│   └── monthly_attribution.yml       # 信号归因月报（每月 1 日 09:00）
├── src/
│   ├── bottom_fishing_strategy.py          # 策略一：抄底（4层过滤 + 评分 + 双数据源 + 共享筛选骨架）
│   ├── volume_breakout_strategy.py         # 策略二：放量突破（7层漏斗，继承策略一数据层/骨架）
│   ├── volume_breakout_backtest.py         # 突破策略离线回测（本地 CSV，不联网）
│   ├── bottom_fishing_strategy_akshare.py  # 旧版策略备份（AkShare 数据源，未被引用）
│   └── bottom_fishing_strategy_old.py      # 更早期版本备份（未被引用）
├── store/
│   └── mysql_store.py                # MySQL 持久化：推荐落库 / 周度追踪 / 归因查询（含幂等迁移）
├── notify/
│   └── feishu.py                     # 飞书通知：选股结果（按策略区分卡片）/ 周度追踪（按策略分列）/ 归因月报
├── cache/                            # 自动生成的磁盘缓存（已 gitignore）
│   ├── 个股/指数行情（按交易日失效）
│   ├── 股票列表（按 CACHE_TTL_DAYS=6 天失效）
│   ├── 基本面（按 FUND_CACHE_TTL_DAYS=7 天失效，fund_v2_*；主源缓存名带起始季度标签，ROE 年化口径）
│   ├── 行业分类（按 INDUSTRY_CACHE_TTL_DAYS=30 天失效）
│   └── market_regime_state.json      # regime 滞回状态（超 10 天自动重置）
├── data/                             # artifact 上传目录（预留，当前不写入文件）
├── run.py                            # 每日选股入口（抄底策略，GitHub Actions 调用）
├── run_breakout.py                   # 放量突破选股入口（含组合层去重/总量上限）
├── run_weekly_tracking.py            # 周度追踪入口
├── run_monthly_attribution.py        # 信号归因月报入口
├── backtest.py                       # 抄底策略历史回测（逐日重放，复用实盘纯函数避免未来函数；prefetch/run/all 三种模式）
├── docs/                             # 策略设计文档
│   ├── volume_breakout_strategy.md   # 放量突破策略设计说明（分层漏斗 + 参数速查）
│   └── TOP5改进说明.md               # 荐股质量分层/底背离修正等改进落地记录
├── test_entry_filters.py             # 入场质量过滤器单测（合成数据，不联网）
├── test_main_layering.py             # main() 编排分层回归测试（打桩数据源，不联网）
├── test_strategy_fixes.py            # 2026-09 双策略评审修复项回归测试（合成数据，不联网）
├── test_optimizations_p0.py          # P0 优化项验证（pct_chg 口径统一 / 停牌缺口 / 排序质量分 / 数量上限与熔断）
├── schema.sql                        # MySQL 表结构（文档 + 手动建库参考）
├── requirements.txt                  # Python 依赖
└── README.md
```

## 本地运行

### 安装依赖

```bash
pip install -r requirements.txt
```

### 每日选股（抄底策略）

```bash
# 设置飞书 Webhook（可选，不设置则跳过通知）
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/your-webhook-id"

# MySQL（可选，不设置则跳过落库）
export MYSQL_HOST=127.0.0.1 MYSQL_USER=root MYSQL_PASSWORD=xxx MYSQL_DATABASE=stock_fishing

# 运行每日选股（选股 → 落库 → 通知）
python run.py

# 强制跳过 cache/ 磁盘缓存，全部从数据源重拉
python run.py --no-cache

# 纯测试运行：不写 MySQL、不推飞书（避免测试推荐与晚间正式结果混在同一天）
python run.py --no-save --no-notify
```

> `--no-save` 的存在理由：`stock_recommendation` 以 `(rec_date, code, strategy)` 为唯一键且用 `INSERT IGNORE` 写入，**不会清掉当天旧行**——白天测试写进去的推荐会与晚间正式结果并存在同一天，污染周度追踪与归因，并影响晚间突破策略的同股去重。

### 放量突破选股

```bash
python run_breakout.py                # 选股 → 组合层去重/总量上限 → 落库 → 通知
python run_breakout.py --no-cache     # 强制重拉
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

### 历史回测（抄底策略，可选）

`backtest.py` 逐日重放抄底策略并统计月度收益，复用实盘同一套纯函数（`evaluate` / `compute_daily_signals` / 周线确认 / 风控 / 排序 / 行业分散）保证口径一致，且刻意规避未来函数（个股日线截断到当日、周线由日线重采样、市场环境取截至当日的沪深300）：

```bash
python backtest.py prefetch [--workers 6] [--limit N]   # 预取行情面板到磁盘缓存（可断点续跑）
python backtest.py run [--limit N] [--max-hold-days X]   # 逐日重放 + 收益模拟 + 月度统计 + 报告
python backtest.py all                                   # prefetch 后直接 run
```

收益模拟为「信号日收盘生成、次日开盘买入」，持有期内用盘中高低价模拟触发 +10% 止盈 / −5% 止损（同根 K 线两者都触及时按止损保守处理）。需联网预取数据；`src/volume_breakout_backtest.py` 则是突破策略的离线事件回测（本地 CSV，不联网）。

### 直接运行策略脚本（不落库、不发送通知）

```bash
# 抄底策略：仅选股，打印明细
python src/bottom_fishing_strategy.py screen

# 突破策略：仅选股，打印明细
python src/volume_breakout_strategy.py screen
```

### 运行单元测试

四个测试均使用合成数据/打桩数据源、不访问网络，改动策略或主流程后应先跑它们（当前全部通过，合计 224 项断言）：

```bash
python test_entry_filters.py      # 单只判定：15 个入场场景 + 分层/时效/双低点背离/量价分档/评级同源/排序可复现（68 项断言）
python test_main_layering.py      # main() 编排：周线路径与禁用路径下的正式推荐/待核验候选分层（15 项断言）
python test_strategy_fixes.py     # 2026-09 修复回归：配置/ROE年化/流动性与波动率归因/波动率观察池采集与渲染/周线已收盘bar/regime滞回（含每自然日最多推进一次）/北京时区/向量化对拍/突破评分校准（66 项断言）
python test_optimizations_p0.py   # P0 优化项：pct_chg 双源口径统一/停牌缺口过滤/排序质量分 rank_score/推荐数量上限与市场级熔断（75 项断言）
```

## 数据持久化

两张表，首次连接时自动创建（`CREATE TABLE IF NOT EXISTS`，幂等）：

| 表 | 写入方 | 说明 |
|----|--------|------|
| `stock_recommendation` | `run.py` / `run_breakout.py` | 每日推荐明细，`(rec_date, code, strategy)` 唯一，重复写入自动忽略 |
| `stock_tracking` | `run_weekly_tracking.py`（每周） | 周度表现追踪，`(rec_id, week_no)` 唯一 |

**strategy 来源字段**：`stock_recommendation.strategy` 区分 `bottom_fishing`（抄底）/ `volume_breakout`（突破）。同一股票同日可被两套策略分别推荐并存（组合层去重默认已拦截，唯一键兜底）；**周度追踪**按来源分列统计（飞书卡片分列 + 逐只 `[抄底]/[突破]` 标签）；**信号归因**的查询已带出 `strategy` 字段，但月报当前仍按整体聚合、尚未按策略分组（后续可在此基础上加一层 groupby）。存量表由程序自动幂等迁移（补列 + 唯一键升级，历史行回填 `'bottom_fishing'`）。

**追踪口径**：每条推荐自推荐日起最多记录 4 次周度追踪（`TRACK_MAX_WEEKS`），且超过 31 天（`TRACK_MAX_AGE_DAYS`）强制退出——双保险，防止周任务中断导致超期。收益率 =（最新收盘价 − 推荐时收盘价）/ 推荐时收盘价 × 100%。同一股票在不同日期被重复推荐，视为不同记录、各自独立追踪。

**信号归因**：`run_monthly_attribution.py` 基于两表历史数据统计各信号维度（等级 / 评分分档 / 底背离 / 市场环境 / 持有周次）的胜率与收益，用于用真实运行数据反哺信号质量评估。**这不是回测**——只统计已落库的真实推荐与真实追踪收益，因此样本需要时间积累。

> **只有 `rec_tier='formal'` 的正式推荐会落库**；待核验候选（数据缺失）不写库、不参与追踪，避免污染归因样本。

## 通知样例

推送到飞书群的消息卡片分三类：

| 卡片 | 触发 | 内容 |
|------|------|------|
| 选股结果 | 每日选股后（抄底/突破各自独立卡片，标题与单票格式按策略区分） | 市场环境、正式推荐数量（低位企稳候选 / 放量突破候选）、每只股票的代码/名称/评分/等级/收盘价/止损/止盈/风险收益比 + 入选依据 / 突破级别与幅度 + 核验状态；另附**待核验候选**与**⚠️ 波动率风控否决（高风险观察池）**两个独立区块（后者逐只展示风险等级、ATR%/上限/倍数与风险提示，最多 5 只，并明确「不构成买入建议」）；无正式推荐时提示「今日无信号」（波动率观察池非空时标题附带条数），执行异常时推送错误信息 |
| 周度追踪 | 每周追踪后 | 追踪数量、胜率、平均收益、状态分布（第 N/4 周）、**按策略分列**（抄底/突破各自数量/胜率/平均收益）及逐只明细（带 [抄底]/[突破] 标签） |
| 信号归因月报 | 每月归因后 | 统计周期、已追踪推荐数、整体胜率、平均收益/平均峰值收益，以及按等级/评分分档/底背离/市场环境/持有周次的分组统计 |

## 技术指标

| 指标 | 环节 | 用途 |
|------|------|------|
| MA5/EMA5/10/20 | 日线 | 趋势转折（拐头/金叉，同源信号合并计分） |
| MA20 斜率 | 日线 | 下降通道过滤（陡峭下降中趋势转折不认可，防接飞刀）；突破策略要求 MA20 上行 |
| 当日涨幅/跳空幅度 | 日线 | 防追高否决（抄底：涨幅 >5% / 跳空 >2%；突破：涨幅 >7% 或 <2% / 跳空 >2%） |
| 近5日累计涨幅 | 日线 | 已反弹一段否决（累计 >12%，比 RSI 更灵敏） |
| RSI14 上限 / 量比上限 | 日线 | 已反弹一段否决（RSI>60）/ 天量出货否决（量比>4）；突破策略检查**前一日** RSI ≤80 |
| 60日高点回撤（下限/上限） | 日线 | 底部区域过滤（回撤 <10% 判上涨中继；>70% 判崩盘型/价值陷阱） |
| 20日区间位置 / MACD柱 / KDJ | 日线 | 严格确认：区间下半部 + MACD柱连续2日改善 + KDJ金叉、K≤55 且 K 上行 |
| ATR% | 日线 | 波动率风控（抄底 ≤3.33% / 突破 ≤4.0%，超限否决 FAIL_VOLATILE） |
| 周线 MA10 / MACD | 周线 | 决赛圈确认（站上MA10且MA10上行 + MACD柱翻红或绿柱连收2周；**只用已收盘周 bar**；数据缺失/滞后 → 待核验候选） |
| RSI14/7/21 | 日线 | 超卖反弹 + 底背离判断（前一个价格低点当根的 RSI 对比）+ 多周期共振 |
| 成交量比 + 收盘位置/实体/上影线 | 日线 | 量价质量分（放量企稳 25 / 放量上涨 18 / 放量冲高回落 10） |
| 三级阻力位（60日高/20日高/MA60） | 日线（突破） | 突破级别判定（L1/L2/L3，REQUIRE_L1_OR_L2 默认关闭 L3） |
| 量比/成交额/量能分位/额比 | 日线（突破） | 放量四重确认（1.8–4.0 倍 + ≥1亿 + 60日80%分位 + ≥中位×1.5） |
| 平台振幅/收盘离散度 | 日线（突破） | 突破前整理充分度（振幅 ≤18%、std/mean ≤4%，shift(3) 避污染） |
| 最新K线日期 | 日线 | 数据时效（与沪深300最新交易日不一致 → 停牌/滞后，暂不推荐 FAIL_STALE） |
| 相邻K线自然日间隔 | 日线 | 停牌缺口过滤（>12 天 → FAIL_HALT_GAP，滚动指标跨缺口失真） |
| ATR% / 回撤深度 / 20日区间位置 | 日线（排序） | 连续质量分 `rank_score`（仅决定同批通过者先后，不改准入；`RANK_QUALITY_WEIGHT`=10 可置 0） |
| 成交额(20日均值) | 日线 | 流动性过滤（<3000万 否决，独立归因 FAIL_LIQUIDITY） |
| 沪深300 MA20 斜率 | 市场环境 | 牛/熊/中性 regime（含连续 2 日确认的滞回机制） |
| 沪深300 近5日累计涨跌幅 | 市场环境 | 市场级熔断（≤ −4% → 本次运行不推荐，抄底策略） |
| 行业分类 | 集中度 | 行业分散（同一行业最多 2 只，避免单一板块押注） |

## 数据源

**双数据源架构**：Baostock 为主、AkShare 为备，自动切换。

- **主源 Baostock**：`query_history_k_data_plus`（个股/指数日线，前复权）、`query_all_stock`（股票列表）、`query_stock_industry`（行业分类，用于行业分散）、`query_profit_data` / `query_balance_data`（ROE/负债率）
- **备源 AkShare**：`stock_zh_a_hist`（个股日线，东财通道；不可达时自动切 `stock_zh_a_daily` 新浪通道）、`stock_zh_index_daily`（指数）、`stock_info_a_code_name`（股票列表）、`stock_financial_analysis_indicator` / `stock_balance_sheet_by_report_em` / `stock_profit_sheet_by_report_em`（基本面，**决赛圈按字段补齐**商誉/扣非与主源缺失的核心项，只填 None 字段、不覆盖主源）
- **切换规则**：
  - 单条取数失败（异常或返回空）→ 自动用 AkShare 兜底重取
  - Baostock 连续失败达 8 次 → 熔断，本次运行后续请求直接走 AkShare（避免逐股无效重试）
  - Baostock 登录失败 → 直接降级 AkShare 跑完全程（AkShare 未安装则报错退出）
- **连接管理**：登录真实重试（检查 error_code）、查询失败自动重连、全局线程锁防止 C++ 底层 socket 多线程踩踏；模块级 `socket.setdefaulttimeout(20)` 防假死
- **失败重试**：请求异常按退避重试（个股 `MAX_RETRY=2`，股票列表 `LIST_MAX_RETRY=4`）；「返回空」视为该标的确实无数据，不重试
- **两级缓存**：内存 `CacheManager`（进程内，`CACHE_EXPIRE_HOURS=4` 小时）→ `cache/` 目录磁盘缓存
  - 行情类按交易日失效：新交易日自动重拉，同日重复运行走缓存
  - 股票列表按 `CACHE_TTL_DAYS=6` 天、基本面按 `FUND_CACHE_TTL_DAYS=7` 天（`fund_v2_*`；主源缓存名带起始季度标签，报告期滚动后自动失效，备用源为按 TTL 失效）、行业分类按 `INDUSTRY_CACHE_TTL_DAYS=30` 天失效
  - 磁盘只缓存未过滤的原始列表，过滤在返回时应用，改 config 即时生效无需清缓存
  - 两个数据源的缓存文件命名天然隔离（Baostock 带 `sh.` 前缀、AkShare 为纯 6 位数字），互不污染
- **股票池过滤**：`MAIN_BOARD_ONLY` 默认开启——白名单仅保留普通 A 股账户可直接交易的沪深主板（60/00 开头）；创业板（开户需 10 万资产）、科创板/北交所/港股通（需 50 万资产）等对个人资金有门槛的板块，以及港股/B 股等非 A 股证券全部排除。关闭该项则退回由 `FILTER_ST` / `EXCLUDE_DELISTING` / `EXCLUDE_BSE`（默认开启）与 `EXCLUDE_CHINEXT` / `EXCLUDE_STAR`（默认关闭）组合控制

## 免责声明

本策略仅为量化研究工具，不构成投资建议。策略优化旨在从逻辑上减少低质量信号，不保证提高未来收益率或胜率。实际效果必须通过严格的样本外回测验证。投资有风险，入市需谨慎。
