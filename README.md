# A股日线量化选股策略 - 自动化执行系统

以**基本面优秀、估值便宜、价格处于中长期低位**为目标的荐股系统，通过 GitHub Actions 自动执行：**每日选股** → **每周追踪** → **每月信号归因**。推荐记录持久化到 MySQL，结果通过飞书机器人推送。

## 最小修复后的运行口径

本轮修复说明及验证范围见 [最小修复说明](docs/minimal_repairs.md)。以下旧章节中与该说明冲突的描述以修复说明为准：成长数据默认缺失待核验（`FORWARD_MISSING_AS_PENDING=True`）；行业估值评分使用行业分位；突破须完成财务、行情与周线终审才成为 formal；追踪采用固定 5/10/15/20 交易日。另外，择时读数与决策简报为**纯展示**（不参与否决/排序/落库），熊市止跌闸门只在**生产入口 `run.py`** 开启。

## 当前默认：优质低估低位荐股

`StrategyConfig.RECOMMENDATION_MODE="quality_value"` 为**优质低估低位入口（run.py）**的默认资格判定——它选的是「多年质量好 + 相对低估 + 价格处 250 日低位」，**不是技术性抄底**。这里的“低股价”指相对自身250个有效交易日区间的位置，不是每股面值低。**放量突破入口（run_breakout.py）已改为独立的 `technical` 模式**（见下文「双策略并行」），不再与它共用同一资格判定。

| 必选维度 | 默认规则 |
|---|---|
| 多年质量（非金融） | 最近连续3个可用完整年度：ROE中位数≥10%、每年ROE≥5%、每年扣非净利润>0、三年经营现金流合计/合并净利润合计≥0.8（分母须>0） |
| 估值（行业相对，#4a） | 默认 `USE_INDUSTRY_RELATIVE_VALUATION=True`：个股 PE/PB 在其**所属行业当日横截面**的分位 ≤60%（`VALUATION_INDUSTRY_PERCENTILE_MAX`）才算便宜；行业数据缺失或行业内可比样本 <5（`VALUATION_INDUSTRY_MIN_PEERS`）时**自动回退**绝对阈值 0<PE TTM≤25、0<PB MRQ≤3。两种口径下 PE/PB≤0 一律否决、缺任一项只列待核验 |
| 前瞻确认（#4b） | `REQUIRE_FORWARD_CONFIRMATION=True`：Baostock `query_growth_data` 最新报告期净利润同比 < `FORWARD_NI_YOY_MIN`(-30%) → `FAIL_FORWARD` 否决（对治「trailing 年报漂亮、当年正在崩」的价值陷阱）。报告期须有效且不晚于决策日（1–4 月允许上年三季报/已披露年报，5–8 月至少当年 Q1，9–10 月至少 Q2，11–12 月至少 Q3）；缺失、季度无效或过旧 → 记 `forward` 缺项降级为**待核验**（`FORWARD_MISSING_AS_PENDING=True`，与 [最小修复说明](docs/minimal_repairs.md) 一致；显式设 False 可恢复「缺失放行」）。同比落在 `[FORWARD_NI_YOY_MIN, FORWARD_NI_YOY_WARN)` 即 [-30%, -10%) 时**不否决**，仅在决策简报风险项打「业绩下滑预警」黄标 |
| 价格位置 | 至少250根有效日线，(现价−250日最低)/(250日最高−最低)≤40%；取数窗口600自然日 |
| 综合分下限（#3） | `MIN_QV_SCORE=60`：综合分（0.50×质量+0.35×估值+0.15×技术）<60 的 **formal 候选**否决（`FAIL_QV_SCORE`），弱市自然少推/不推；仅对 formal 生效，pending 不受其累；设 0 关闭 |
| 熊市止跌闸门（P1） | `QV_BEAR_TIMING_GATE`：**库级默认 `True`，与生产口径一致**（此前库级 `False`、仅 `run.py` 覆盖为 `True`，会让任何用默认 config 跑的实验——`backtest.py ab`、单测——都跑在一个生产并不存在的策略上，A/B 结论无法采信；现覆盖点已消除，`run.py` 不再单独赋值）。熊市（含 unknown 折叠）的正式推荐必须另有最低止跌证据——「现价站上 MA20」或「MACD 柱连续 `MACD_MOMENTUM_DAYS` 日改善」——否则 `FAIL_BEAR_TIMING` 否决。牛/中性完全不受影响。用于修复「突破策略熊市空仓、优质低估低位侧却仍在纯左侧接飞刀」的组合暴露失衡 |
| 择时读数 + 决策简报（P0/P5，纯展示） | `SURFACE_TIMING_READ=True`：为每条推荐附「左侧/右侧 + 是否仍在下跌」标签（用 `TIMING_MA_LONG`=60 的均线区分站上/跌破，MACD 深度走弱判「仍在下跌」）与决策简报（信心分档/今日触发/看多理由/主要风险/失效价）。**不参与任何否决、排序与落库**，只把择时判断显式交回人工 |
| 基础核验 | 最新行情与指数基准日一致、负债率已核验且≤70%、已知商誉超限否决；金融股独立待专项核验 |

### ⚠️ 综合分下限的实际门槛（比它看起来严得多）

`MIN_QV_SCORE=60` 不是「温和兜底」——它**严格强于上表三道硬闸门之和**。原因是评分口径决定了「恰好压线达标」的公司拿不到 60 分：

| 评分项 | 在「闸门恰好压线」处的取值 |
|---|---|
| 质量分（权重 0.50） | 三档同时压线的**最小可达值 = 50**（`_dimension_score` 在阈值处给 50 分。如 3 年 ROE=(5,10,10)：中位 10=阈值、最低 5=阈值、现金转换 0.8=阈值 → 实测 `status=verified` 而 `quality_score=50`） |
| 估值分（权重 0.35） | 行业口径在分位上限 0.60 处 = **40**；绝对口径在 PE=25 / PB=3 上限处 = **0** |

于是「压线合格」候选的综合分上限只有 **54.0（行业口径）/ 40.0（绝对口径）**，两者都低于 60 → **压线合格者 100% 被淘汰，硬闸门的阈值形同虚设**。反解过线所需（`0.5q + 0.35v + 0.15t ≥ 60`）：

| 质量分 | 行业口径（估值分 40） | 绝对口径（估值分 0） |
|---|---|---|
| 50（压线下限） | 技术分 ≥140 → **不可达** | 技术分 ≥233 → **不可达** |
| 70 | 技术分 ≥73 | 技术分 ≥167 → **不可达** |
| 90 | 技术分 ≥7 | 技术分 =100（**必须满分**） |

**直接后果**：PE/PB 逐 bar 缺失的日子（AkShare 兜底）估值分为 0，formal 几乎必然归零——这与 `FORWARD_MISSING_AS_PENDING=True` 是**两个独立叠加**的零推荐机制。运行时可在漏斗日志里核对：`describe_qv_floor()` 会随配置实时算出门槛并打印；该算术已固化为 `test_recommendation_upgrades.TestScoreFloorEquivalence`，改动评分权重或闸门值会立即失败。调低该值前务必先跑 `python backtest.py ab --mode quality_value`。

**市场级刹车（#2，已对 quality_value 生效）**：此前 quality_value 在 `main()` 提前 return，绕过了组合层风控；现已把**急跌熔断**（沪深300 近5日累计 ≤ `MARKET_CRASH_HALT_PCT`(-4%) → 当日不推荐）与**推荐数量按 regime 收缩**（牛 `MAX_PICKS`=5 / 中性 `NEUTRAL_MAX_PICKS`=4 / 熊 `BEAR_MAX_PICKS`=2，由 `resolve_max_picks` 解析）移到分支之前，两种模式共用。ATR 交易风险门槛仍只作用于 technical 路径。

**regime 双指标确认（P2）**：`MARKET_REGIME_DUAL_INDICATOR=True` 时，在沪深300 MA20 斜率之外叠加第二指标——长期均线 `MARKET_MA_LONG`(=60) 趋势：只有「斜率看多**且**现价与 MA20 均在 MA60 上方」才判 `bull`，「斜率看空且均在 MA60 下方」才判 `bear`，两者不同向一律降级 `neutral`（描述串会显式标注「斜率判 x，MA60 未同向确认，降级中性」）。这是对 regime 抖动的治本手段（滞回只是补丁）；指数样本不足 `MARKET_MA_LONG` 时自动退回单指标，口径与改动前一致。置 False 恢复纯 MA20 斜率判据。

**可选的 KDJ/MACD 下限否决**：默认关闭（`QV_ENFORCE_KDJ_MACD_VETO=False`），保持「技术面不作否决」的现有口径逐字节不变。显式开启后，`FAIL_MACD_WEAK`（MACD 柱深度弱势且仍在恶化）与 `FAIL_KDJ_HIGH`（KDJ 已在区间顶部）会成为资格判定的一道硬闸门，与放量突破策略共用同一套阈值与实现。**切换前必须先用 `python backtest.py ab --mode quality_value` 取得样本内证据**：该闸门修掉过两次已实测的误杀（匀速上行的柱值递减、「K 略低于 D」的交替领先噪声），说明这类「下限」阈值极易误伤健康形态，不能凭直觉设定。

排序为 **50%质量分 + 35%估值分 + 15%技术分**；`_rank_signals` 在 quality_value 路径下实际使用的排序键为 **综合分（`rank_score` 即该综合分）降序 → 质量分降序 → 估值分降序 → 股票代码升序（末级键）**，与并发完成顺序无关、结果可复现。质量分使用ROE稳定性及现金转换率的连续值；估值分是在绝对上限内按PE/PB线性评分（行业相对模式下钳到 0~100），不声称是历史估值分位。技术信号（金叉、背离、量价、放量突破）仅作排序/标签，缺少金叉、短期涨幅高、20日位置高、周线尚未企稳不会否决符合必选维度的公司。取消奖励更深回撤的排序偏好。行业分散（每行业≤2只）继续有效。

年度数据来自AkShare新浪财务摘要的准确字段（ROE%、扣非净利、合并净利、经营现金流净额，金额元）。有公告日时按公告日可用；摘要不提供公告日时保守按次年5月1日可用。缺最新年度不能用更旧年度顶替；取数失败/NaN/金融专项未核验均为pending。该接口不是历史财报修订版本库，历史报告仍有重述数据局限。

只有显式`formal`会落库；**pending（待核验候选）不进落库、不进追踪、也不进飞书卡片**——数据不全的标的既不构成推荐，也不该被误读为「备选」，仅在 CI 日志里打印计数与前 10 只明细。年度缓存按代码、决策日期和年数隔离（`annual_quality_v1_<code>_<决策日>_<年数>.json`），成长数据按代码/季度缓存（`growth_v1_<code>_<年>Q<季>.json`）；行业相对估值在筛选前用全池日线构建一次「行业 PE/PB 横截面快照」（复用缓存、不额外取数），前瞻确认对通过预筛的候选逐只查 `query_growth_data`，首次运行会增加候选财务/成长请求。

> **回测口径提示**：`backtest.py` 的历史面板通常不含逐 bar PE/PB 与成长数据，且行业快照/前瞻确认依赖当日横截面与主源——因此回测中估值闸门会回退绝对阈值、前瞻确认默认不触发、综合分下限仍生效。要让行业相对估值在回测中生效，须用含 PE/PB 的 Baostock 面板重跑 prefetch。

详细规则与测试见 [荐股调整说明](docs/quality_value_recommendations.md)。以下旧技术策略章节仅描述显式设置`RECOMMENDATION_MODE="technical"`时的对照行为（**放量突破入口现即跑此模式**）；交易回测/追踪的原有局限并未因本次荐股调整自动消失。

数据层采用 Baostock 主数据源 + AkShare 备用数据源的双源架构，主源失败自动切换。

**双策略并行**（共用同一套数据层 / 基本面防雷 / 市场环境 / 周线确认 / 并发筛选骨架）：

| 策略 | 定位 | 信号类型 | 推荐数量（牛/中/熊） |
|------|------|----------|----------------------|
| **优质低估低位入口**（run.py，`quality_value`） | 优质低估低位 | 多年质量 + 行业相对低估值 + 250日低位 + 综合分≥60（技术面仅排序/标签） | 5 / 4 / 2 + 急跌熔断 |
| **突破入口**（run_breakout.py，`technical`） | 右侧顺势 | 横盘末端放量突破关键阻力位（独立七层漏斗 + 决赛圈周线确认） | 5 / 3 / 0（熊市空仓） |

**两个入口现在是真正独立的信号源**（#1）：优质低估低位入口跑 `quality_value` 资格，突破入口跑自己的 `technical` 七层漏斗。此前突破入口也用 `quality_value`，会委派同一套资格判定、产出与前者完全相同，再经组合层去重后突破卡片恒为空——等于花双份成本拿一份结果；改为 `technical` 后突破基于放量形态独立出票。组合层做同股去重与每日总量上限（`DAILY_TOTAL_MAX_PICKS`=7，后运行的突破剔除当日已被优质低估低位推荐的个股）。市场环境为「未知」（指数数据缺失）时，经 `UNKNOWN_AS_BEAR` 折叠为熊市**只作用于推荐数量上限**（优质低估低位侧收缩为 2 只、突破空仓）；准入分数线不因「未知」上浮（门槛上浮只对已确认的 `bear` 生效，且仅 technical 路径有准入分数线）。

**市场级熔断**（`MARKET_CRASH_HALT_PCT`=−4% / `MARKET_CRASH_LOOKBACK`=5）：沪深300 近 5 个交易日累计跌幅越阈 → 优质低估低位入口本次运行直接不推荐——急跌期间全市场同步满足 RSI 超卖 / MA5 拐头 / 创新低底背离，个股级斜率过滤识别不了这种系统性风险。该熔断现置于 `main()` 的 quality_value 分支之前，对默认模式同样生效（#2）；突破入口在熊市本就空仓（`BEAR_MAX_PICKS_BREAKOUT`=0）。

## 策略一：优质低估低位 / 低位企稳（bottom_fishing_strategy.py）

> **模块名与口径的关系**：`bottom_fishing_strategy.py` 承载两种资格判定，对外称谓按**实际生效口径**给出——
> `quality_value`（生产默认，run.py）= **优质低估低位**；`technical`（对照/旧规则）= **低位企稳**（才是真正的技术抄底：底背离、RSI 超卖、回撤深度）。下面的 4 层过滤描述的是 **technical 对照口径**，见本节开头的口径说明。

4 层量化过滤体系：

1. **市场环境过滤**：沪深300日线MA20斜率判断牛/熊/中性环境；熊市不扣分定级，而是把准入门槛**上浮 10 分**（`BEAR_GRADE_BOOST`），保证展示分数与等级始终同源；数据不足时明示「未知」（`UNKNOWN_AS_BEAR`：未知经 `_effective_regime` 折叠为熊市，作用于推荐数量上限的收缩；**门槛上浮只对已确认的 `bear` 生效**——`evaluate()` 按原始 regime 判定，与突破策略 `evaluate_breakout()` 用有效 regime 判门槛的做法不同）；**regime 双指标确认**（`MARKET_REGIME_DUAL_INDICATOR`，见上文「regime 双指标确认（P2）」）；**regime 滞回**（`MARKET_REGIME_HYSTERESIS`）：牛/熊/中性切换须连续 2 个交易日同向确认，避免斜率在阈值附近抖动导致 regime 逐日跳变（状态存 `cache/market_regime_state.json`，超 10 天自动重置；**确认计数每个自然日最多推进一次**——同一天内多套策略依次运行读的是同一份收盘数据，若允许重复计数会把「连续 2 个交易日」悄悄缩短为「同日翻转」）；**市场级熔断**（`MARKET_CRASH_HALT_PCT`=−4% / `MARKET_CRASH_LOOKBACK`=5）：沪深300 近 5 个交易日累计跌幅越阈 → 本次运行不推荐（数据不足不触发，避免指数缺数误判）
2. **基本面防雷**：年化ROE/负债率为核心否决项（金融业——银行/保险/券商等负债率天然 80%+，按名称关键词+代码白名单识别并单独放宽阈值）；商誉/扣非为可选否决项，主源 Baostock 不提供，**technical 路径的决赛圈与放量突破策略的终审会用 AkShare 按字段补齐后复核**（`_fill_optional_fundamentals`，只填 None 字段、不覆盖主源）。注意 **quality_value 路径不做这一步**：它由 `evaluate_quality_value` 直接跑年度质量核验（AkShare `stock_financial_abstract`），商誉为空时不否决、只按缺项计。**ROE 口径为线性年化**（Q1×4 / Q2×2 / Q3×4/3 / Q4×1，累计值年化后才与 `MIN_ROE` 年化阈值可比，不再随财报日历漂移）；财报季度按**交易所披露截止日回溯**取「最近已披露季度」（最多回溯 `FUND_LOOKBACK_QUARTERS`=4 季），结果带 `report_period` 标明数据实际所属报告期；基本面磁盘缓存为 `fund_v2_*`（旧 `fund_*` 单季未年化口径已隔离废弃）
3. **日线技术指标筛选**：底背离（**双低点算法**：与窗口内前一个价格低点比较 RSI/DIF，而非指标自身最小值）+ 趋势转折（MA5拐头/EMA金叉同源合并计分）+ RSI超卖反弹 + **量价质量分**（放量阳线收高位/一般放量上涨/放量冲高回落三档）；流动性过滤（近20日日均成交额 ≥3000万，独立归因 `FAIL_LIQUIDITY`）；入场质量否决（当日涨幅 >5% 追高否决、开盘跳空高开 >2% 否决、近5日累计涨幅 >12% 已反弹一段否决、RSI14 >60 否决、量比 >4 天量否决、MA20 近5日斜率 <-4% 的陡峭下降通道中趋势转折信号不认可、距60日高点回撤 <10% 非底部区域否决、回撤 >70% 崩盘型/价值陷阱否决）；严格确认指标（现价须落在近20日价格区间下半部、MACD 柱须**连续 2 日**改善、KDJ 须金叉、K≤55 且 K 值上行）；**数据时效**（个股最新K线与市场最新交易日不一致，即停牌/数据滞后 → 暂不推荐）；**停牌缺口**（相邻 K 线自然日间隔 >12 天判定为期间曾停牌 → 暂不推荐，独立归因 `FAIL_HALT_GAP`：这类股票的 20 日均量/60日高点/ATR 全部跨缺口计算，「60日高点」可能实为数月前的高点）
4. **波动率风控**：ATR 占现价百分比 >3.33% 直接否决（`MAX_ATR_PCT` / `FAIL_VOLATILE`——与旧「RR≥1.5」数学等价但语义直白，且修复了旧实现 ATR 缺失时 RR 恒 2.0 永不否决的漏洞）；止损/止盈/盈亏比（2×ATR 或固定 5% 止损、固定 10% 止盈）**仅作展示与落库，不参与否决**；**决赛圈周线确认**（`REQUIRE_WEEKLY_TREND`：默认 `WEEKLY_MA_BOTH_REQUIRED=False`，即「收盘站上周线 MA10（容忍 `WEEKLY_TOLERANCE`=2%）」或「MA10 上行」**满足其一**即可——真·低位买点常出现在周线 MA10 尚未上行时，双条件会把目标 setup 全滤掉；设 True 恢复「站上**且**上行」严格口径。**且**须周线 MACD 企稳（`REQUIRE_WEEKLY_MACD_STABLE`：柱值翻红或绿柱连续 2 周收窄）。两个开关独立生效、任一启用即拉周线；**只用已收盘周 bar**——末根周线落在本 ISO 周且非周五即剔除，避免半成品 bar 污染口径（判断基准为北京时间）；数据缺失/截止过旧 → 待核验候选，不占正式名额；取满即止）；**行业分散**（同一行业最多 2 只）；**推荐数量按市场环境收缩**（牛 `MAX_PICKS`=5 / 中性 `NEUTRAL_MAX_PICKS`=4 / 熊 `BEAR_MAX_PICKS`=2，未知经 `_effective_regime` 折叠为熊，统一由 `resolve_max_picks` 解析，周线确认取满即止）

**荐股质量分层（数据缺失不等于筛选通过）**：

| 层级 | 条件 | 处理 |
|------|------|------|
| **正式推荐**（formal） | 财务核心项（ROE+负债率）已核验、启用的周线确认已确认、行情日期与市场最新交易日一致 | 落库 MySQL、进入飞书正式名单、参与周度追踪 |
| **待核验候选**（pending） | 技术面通过，但财务或周线数据缺失（含商誉/扣非未补齐、周线滞后、成长数据缺失） | **不落库、不参与追踪、不进飞书卡片**，仅在 CI 日志打印计数与前 10 只明细 |
| **波动率观察池**（volatile） | 前置各层已通过（technical：技术分已达准入线；突破：突破/量能/形态/趋势已过），**仅 ATR 占现价百分比超上限**被否决 | 仅日志 + 飞书**高风险观察池**区块（标注风险等级与风险提示），**不落库、不参与追踪、不是买入建议**。⚠️ **仅 technical 与突破路径产生该池**：`evaluate_quality_value` 既无 ATR 否决层、也不接收 `volatile_out`，因此生产默认口径（quality_value）下该池恒为空 |
| 暂不推荐 | 行情日期滞后（停牌/数据过期）或任一项否决条件未过 | 直接淘汰 |

评分体系：趋势转折(40) + RSI反弹(25) + 量价质量分(满分25) + 多周期共振(10)，满分 100；量价质量分三档：**25 分 放量企稳**（阳线、收盘位于日内区间上部 ≥60%、上影线 ≤35%）、**18 分 放量上涨**（一般）、**10 分 放量冲高回落**（收盘低于日内区间 35% 位置，只降分不否决）。按分数分为 A/B/C/D 四个等级（A≥80 / B≥60 / C≥40），**等级与展示分数同源（底背离不升档）**；准入门槛 `MIN_PASS_GRADE` 默认 B。**排序**（technical 路径）：主键 `rank_score`（= 技术分 + 连续质量分）降序 → 底背离优先 → 盈亏比降序 → **股票代码升序（末级键）**。quality_value 路径改用 **综合分 → 质量分 → 估值分 → 股票代码升序**。两条路径都以股票代码为末级键，保证两次运行结果可复现、不受并发完成顺序影响。**连续质量分**（`RANK_QUALITY_WEIGHT`=10，置 0 可完全关闭）只用于打破布尔闸门造成的分数并列（技术分实际只落在 {60,65,68,75,83,90,93,100} 等离散值上），**不参与任何门槛判定**——「哪些股票通过」与引入前完全一致，变的只是通过者之间的先后：低波动 0.35 + 回撤深度 0.35 + 20 日区间位置 0.30（MACD 动能维度权重默认 0，因前置闸门已保证通过者恒为满分），四路取值全部复用已算出的列，零额外取数。执行过程输出选股漏斗日志（各层通过率 + 时效剔除数 + 停牌缺口剔除数 + 待核验候选数 + 取数来源统计）。**波动率风控否决明细**（`[VOLATILE]` 区块）会逐只打印：代码/名称/技术分/等级/收盘/ATR%与上限及倍数/风险档/RSI/量比/已达标项，最多 20 只，超出提示剩余条数；若该层当天零淘汰且日线无通过标的，则改为直接点出主要拦截层（避免「为什么没推荐」无从复核）。

> **保底观察候选（fallback）默认关闭**（`ENABLE_DAILY_FALLBACK=False`）：与「宁可少荐」哲学一致，不再每天硬推一只低置信度票；置 True 可恢复旧行为（候选明确标记 `tier=fallback`，仅作观察，不应直接实盘）。

## 策略二：放量突破（volume_breakout_strategy.py）

捕捉「横盘整理末端 → 放量突破关键阻力位」的趋势启动点。骨架、数据层、基本面防雷、市场环境、决赛圈周线确认全部继承抄底策略，只替换日线信号与评估层（七层漏斗）：

1. **基本面防雷**（继承 `check_fundamentals`）
2. **流动性 & 数据完整性**（近 20 日日均成交额 ≥ `MIN_AMOUNT`）
3. **日线技术面（核心）**：
   - 三级阻力位：**L1=近 60 日最高 / L2=近 20 日最高 / L3=MA60**；`REQUIRE_L1_OR_L2=True`（默认）= 只认 L1/L2，L3 判定被整体置 False，避免「低位整理股反弹站上 MA60」被误判为突破；置 False 才启用 L3
   - **放量四重确认**：量比 1.8–4.0 + 当日成交额 ≥1 亿 + 成交量处于自身 60 日 80% 分位以上 + 当日成交额 ≥ 前 20 日成交额中位数 ×1.5
   - **K 线形态防假突破**：阳线实体占比 ≥0.5、收盘/最高 ≥0.97、涨幅 2%–7%、跳空 ≤2%（旧 `MAX_GAP_UP_PCT`）
   - **突破前平台整理**：近 20 日振幅 ≤18%、收盘离散度（std/mean）≤4%（窗口右端 `shift(3)` 避开突破 bar 污染）
   - **趋势背景**：MA20 上行、MA60 走平或上行、现价站上 MA20
   - **近期假突破过滤**：近 10 日无「突破 L1 后 3 日内跌回」记录（事件锚定突破日 L1；**向量化实现**，区间差分替代三重循环）
   - 突破**前一日** RSI ≤80（突破日天然推高 RSI，检查当日会误杀正常突破）
   - **KDJ/MACD 下限否决**（`FAIL_MACD_WEAK` / `FAIL_KDJ_HIGH`）：把这两个指标从纯评分项升级为硬闸门。此前它们只贡献 `W_MOMENTUM_BR`=10 分（占满分 10%），分档失守仍可守住 B 级 60 分准入线，对「推荐与否」没有约束力。闸门只拦两种形态——**MACD 柱深度弱势且仍在恶化**（柱值/收盘 ≤ −0.5% 且连续 2 日递减，双条件 AND）与 **KDJ 已在区间顶部**（K > 85，或 K ≥ 80 且 K < D）；刻意不要求金叉（突破日 RSV 直接打到 100、K 单日跳升，金叉常滞后 1–2 日，硬金叉会系统性漏掉「窄幅整理后首根放量阳线」）。**实测结论：MACD 侧在突破策略中结构性不可达**——`brk_l2` 只认「首次站上阻力位」那天，该日必然 ≥2% 涨幅使 DIF 上行，而柱值下降需 `ΔDIF < hist/4`，横盘阶段 `hist ≤ 0`，不等式永不成立（已固化为参数扫描回归断言）。因此**突破策略的实际新增控制力来自 KDJ 高位闸门**，MACD 侧保留为「突破口径被放宽」时的保险丝。
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
- **共享并发筛选骨架** `run_concurrent_screen`（两策略同一模板）：线程池 + 心跳看门狗（每 1 分钟心跳，连续 4 分钟无完成打印在途股票代码定位卡点；`WATCHDOG_TICK_SEC`/`WATCHDOG_IDLE_WARN_SEC` 可调）+ 时间预算（`SCREEN_TIME_BUDGET_MIN`=240 分钟，超时取消未完成任务、按已完成结果出报告，漏斗统计按已处理数计）
- **行业估值快照进度可观测**：`build_industry_valuation_snapshot` 用独立线程池遍历全 A 拉日线构建横截面，冷启动 cache 全 miss 时曾是 1~2h+ 静默黑洞；现补开始/每 `SNAPSHOT_PROGRESS_LOG_EVERY`（默认 200）只进度（带命中数与 ETA）/成功汇总（含耗时）三段日志，异常路径也带已完成数，纯观测不改口径
- **取数来源统计**：`fetch_stats`（Baostock 命中 / AkShare 兜底 / 双源均失败），漏斗末尾汇总
- **确定性排序**：优质低估低位策略主键 `rank_score`（quality_value 下追加 质量分→估值分 二级键）、末级键股票代码升序（突破策略为 评分→级别→20日均额→代码），两次运行结果可复现，不受并发完成顺序影响

## 自动执行

| 工作流 | 触发时间（北京） | 定时表达式（UTC） | 说明 |
|--------|------------------|-------------------|------|
| **Daily Stock Screening** | 交易日 21:00 | `0 13 * * 1-5` | 优质低估低位策略选股 → 落库 → 飞书通知；随后**同一 runner 内**顺序执行放量突破策略（复用前者的当日行情缓存）→ 组合层去重/总量上限 → 落库 → 飞书通知 |
| **Weekly Recommendation Tracking** | 每周五 21:20 | `20 13 * * 5` | 按沪深300交易日定位推荐后第5/10/15/20个市场交易日并观测（60 日内可补跑）→ 落库 → 飞书汇总（按策略分列） |
| **Monthly Signal Attribution** | 每月 1 日 09:00 | `0 1 1 * *` | 各信号维度胜率/收益归因 → 飞书月报 |

三个工作流均支持手动触发（`workflow_dispatch`）；`Daily Stock Screening` 额外支持以下勾选项：

| 勾选项 | 默认 | 作用 |
|--------|------|------|
| `reuse_cache` | **开** | 手动运行时复用「最近一次收盘快照」——把恢复回来的行情缓存标记为今日有效，**完全不拉行情**，几分钟跑完一轮（详见下方缓存策略） |
| `purge_cache` | 关 | 手动运行时也清除行情缓存、强制重新拉取，用于复现定时运行的取数行为 |
| `no_cache` | 关 | 两套策略都跳过 `cache/` 读写（既不读也不写，本次不共享） |
| `no_save` | 关 | 两套策略都跳过 MySQL 落库（纯测试运行，避免测试推荐与晚间正式结果混在同一天） |
| `no_notify` | 关 | 两套策略都跳过飞书通知 |
| `skip_breakout` | 关 | 只跑优质低估低位，跳过放量突破 |
| `breakout_no_save` / `breakout_no_notify` | 关 | 只针对突破策略跳过落库 / 通知（细粒度控制，与 `no_save` / `no_notify` 取或） |

- **为什么是 21:00**：Baostock 一般 17:30 起陆续更新当日数据、20:00 前完成，21:00 起跑留足安全边际。
- **两套策略为什么合并进同一个 job**：优质低估低位与放量突破的数据层是同一组函数（`get_daily_data` / `get_fundamentals` / `get_stock_list` 由突破模块直接导入），`DAILY_BARS` / `WEEKLY_BARS` / `ADJUST` / `CACHE_DIR` 也完全一致，因此**缓存文件名逐字相同**（`daily_sh.600000_last120_qfq.csv`）。放在同一个 job 里顺序执行，二者共用同一个 `cache/` 目录：优质低估低位侧全量拉取并写入当天行情，突破随后直接读同一份文件，**几乎零重拉**。相比两条独立流水线，既省掉约 3000 次日线 + 数千次基本面查询（这些查询被全局锁 `bs_lock` 串行化，正是运行时长的大头），也天然保证顺序——组合层去重由**后运行者**执行（`fetch_rec_codes_for_date` 剔除当日已被优质低估低位推荐的个股，并按 `DAILY_TOTAL_MAX_PICKS` 默认 7 截取合计上限），不再依赖 cron 时间差。
- **`if: always()` 的用意**：突破步骤带 `if: always()`，优质低估低位侧失败/异常时突破仍会执行——两套策略是独立信号源，不应互相阻塞；前者未写出的缓存由突破自行补拉（慢但结果正确）。
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

程序首次连接时会自动执行 `CREATE TABLE IF NOT EXISTS`（幂等），**无需手动建表**。存量表会自动幂等迁移：推荐表补齐 `signals_hit` / `strategy` 等新列，并把旧唯一键 `(rec_date, code)` 升级为 `(rec_date, code, strategy)`（历史行 `strategy` 回填默认值 `'bottom_fishing'`）；追踪表由 `_ensure_tracking_schema` 补 `holding_trade_days` / `legacy_duplicate` / `unique_close_date` 列，旧 `(rec_id, week_no)` 唯一索引降为普通索引，改为 `(rec_id, holding_trade_days)` 与生成列 `(rec_id, unique_close_date)` 双唯一键（迁移用数据库锁串行化，历史重复行仅标记、不删除）。**不要对存在历史重复的旧追踪表直接加 `(rec_id, close_date)` UNIQUE**。如需手动建库，用仓库内的 `schema.sql`：

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
2. 左侧选择 **Daily Stock Screening** → **Run workflow**（一次运行会依次执行优质低估低位与放量突破两套策略）
3. 只想验证突破的信号质量、暂不写库时，勾选 `breakout_no_save`；只想跑优质低估低位时勾选 `skip_breakout`
4. 等待执行完成，检查飞书是否收到通知

## 项目结构

```
├── .github/workflows/
│   ├── daily_screen.yml              # 每日选股（交易日 21:00，单 job 顺序跑优质低估低位 + 放量突破，共享 cache/）
│   ├── weekly_tracking.yml           # 周度追踪（周五 21:20）
│   └── monthly_attribution.yml       # 信号归因月报（每月 1 日 09:00）
├── src/
│   ├── bottom_fishing_strategy.py          # 策略一：优质低估低位+低位企稳（quality_value / technical 双口径 · 数据层 · 评分 · 共享筛选骨架）
│   ├── volume_breakout_strategy.py         # 策略二：放量突破（7层漏斗，继承策略一数据层/骨架）
│   ├── fundamental_quality.py              # 年度质量核验（AkShare 新浪财务摘要，独立于策略与缓存代码）
│   ├── volume_breakout_backtest.py         # 突破策略离线回测（本地 CSV，不联网；含闸门 A/B 与漏斗归因）
│   ├── bottom_fishing_strategy_akshare.py  # 旧版策略备份（AkShare 数据源，未被引用）
│   └── bottom_fishing_strategy_old.py      # 更早期版本备份（未被引用）
├── store/
│   └── mysql_store.py                # MySQL 持久化：推荐落库 / 固定期限追踪 / 归因查询（含幂等迁移）
├── notify/
│   └── feishu.py                     # 飞书通知：选股结果（按策略前缀区分卡片）/ 周度追踪（按策略分列）/ 归因月报
├── cache/                            # 自动生成的磁盘缓存（已 gitignore）
│   ├── daily_*/weekly_*/index_daily_*  # 个股与指数行情（按交易日失效）
│   ├── stock_list*.csv               # 股票列表（按 CACHE_TTL_DAYS=6 天失效）
│   ├── fund_v2_*                     # 基本面（按 FUND_CACHE_TTL_DAYS=7 天失效；主源名带起始季度标签）
│   ├── annual_quality_v1_*           # 年度质量（按代码/决策日/年数隔离）
│   ├── growth_v1_*                   # 成长数据 query_growth_data（按代码/季度隔离）
│   ├── industry_bs.json              # 行业分类（按 INDUSTRY_CACHE_TTL_DAYS=30 天失效）
│   └── market_regime_state.json      # regime 滞回状态（超 10 天自动重置）
├── data/                             # artifact 上传目录（预留，当前不写入文件）
├── run.py                            # 每日选股入口（优质低估低位 quality_value；熊市止跌闸门已是库级默认）
├── run_breakout.py                   # 放量突破入口（强制 technical；含组合层去重/总量上限/暴露提示）
├── run_weekly_tracking.py            # 固定期限追踪入口（5/10/15/20 交易日）
├── run_monthly_attribution.py        # 信号归因月报入口（总体口径固定 20 交易日）
├── backtest.py                       # 历史回测：逐日重放并复用实盘纯函数避免未来函数（prefetch/run/all/ab；ab 可切 technical 对照）
├── docs/                             # 策略设计文档
│   ├── volume_breakout_strategy.md   # 放量突破策略设计说明（分层漏斗 + 独立回测 + 参数速查）
│   ├── quality_value_recommendations.md  # 优质低估低位荐股资格/排序/分层与验证清单
│   ├── minimal_repairs.md            # 荐股逻辑最小修复（核验/成交模拟/追踪与归因口径）
│   └── TOP5改进说明.md               # 荐股质量分层/底背离修正等改进落地记录
├── tests/                            # 第二批 unittest 测试（T+1 成交模拟 / 核验离线 / 持久化 / 追踪幂等）
│   ├── test_backtest_execution.py
│   ├── test_breakout_verification_offline.py
│   ├── test_quality_backtest_persistence.py
│   └── test_tracking_idempotency.py
├── test_entry_filters.py             # 入场质量过滤器单测（合成数据，不联网）
├── test_main_layering.py             # main() 编排分层回归测试（打桩数据源，不联网）
├── test_strategy_fixes.py            # 2026-09 双策略评审修复项回归测试（合成数据，不联网）
├── test_optimizations_p0.py          # P0 优化项验证（pct_chg 口径统一 / 停牌缺口 / 排序质量分 / 数量上限与熔断）
├── test_momentum_gates.py            # KDJ/MACD 下限闸门回归（纯函数边界 / 两策略接线 / MACD 不可达扫描断言）
├── test_recommendation_upgrades.py   # 荐股四项升级回归（综合分下限 / 前瞻确认 / 行业相对估值 / 突破独立 / 市场级刹车）
├── test_backtest_ab.py               # backtest ab 子命令离线测试（变体定义自检 / --set 解析 / 报告渲染）
├── test_breakout_ab.py               # 突破闸门 A/B 与漏斗诊断离线测试（含「独立回测恒不成交」缺陷固化）
├── test_fundamental_quality.py       # 年度质量评估器单测
├── test_quality_recommendations.py   # 默认荐股口径单测
├── test_minimal_quality_repairs.py   # 最小修复口径回归（流动性/估值评分/成长报告期/突破核验/通知过滤）
├── schema.sql                        # MySQL 表结构（文档 + 手动建库参考）
├── requirements.txt                  # Python 依赖
└── README.md
```

## 本地运行

### 安装依赖

```bash
pip install -r requirements.txt
```

### 每日选股（优质低估低位）

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

### 固定期限追踪（周任务）

```bash
python run_weekly_tracking.py            # 需已配置 MySQL，否则直接退出
python run_weekly_tracking.py --no-cache
```

按沪深300交易日定位推荐后第 5/10/15/20 个市场交易日并逐期限观测（60 自然日内可补跑，停牌/缺价不前填）。

### 信号归因月报

```bash
python run_monthly_attribution.py            # 默认统计近 90 天
python run_monthly_attribution.py --days 30
```

### 历史回测（可选）

`backtest.py` 逐日重放策略并统计月度收益，复用实盘同一套纯函数（`evaluate` / `compute_daily_signals` / 周线确认 / 风控 / 排序 / 行业分散）保证口径一致，且刻意规避未来函数（个股日线截断到当日、周线由日线重采样、市场环境取截至当日的沪深300）。默认跑 `quality_value`（与生产一致），`ab` 子命令可切 `technical` 做对照：

```bash
python backtest.py prefetch [--workers 6] [--limit N]   # 预取行情面板到磁盘缓存（可断点续跑）
python backtest.py run [--limit N] [--max-hold-days X]   # 逐日重放 + 收益模拟 + 月度统计 + 报告
python backtest.py all                                   # prefetch 后直接 run
```

收益模拟为「信号日收盘生成、次日开盘买入」，持有期内用盘中高低价模拟触发 +10% 止盈 / −5% 止损（同根 K 线两者都触及时按止损保守处理）。需联网预取数据；`src/volume_breakout_backtest.py` 则是突破策略的离线事件回测（本地 CSV，不联网）。

### KDJ / MACD 闸门 A/B 对照（`ab`）

调整 KDJ/MACD 闸门参数前应先用样本内对照确认方向，不要拍脑袋改实盘。`ab` 子命令对同一份行情面板跑多组配置并输出横向对照表：**数据只加载一次、所有变体共用**，因此各变体之间的差异全部来自被改动的策略参数，不含数据抖动。

```bash
python backtest.py ab                                   # 两个模式全部变体
python backtest.py ab --mode quality_value              # 只看生产默认模式
python backtest.py ab --mode technical --variants baseline,no_kdj,kdj_k70,macd_d1
python backtest.py ab --limit 300 --start 2026-06-01    # 冒烟：小股票池
# 用 --set 固定与考察目标无关的「环境」参数（可多次），使变体之间只剩目标差异：
python backtest.py ab --mode technical --variants baseline,no_kdj \
  --set USE_VALUATION_FILTER=False --set MIN_PASS_GRADE=C
```

> `--set` 覆盖的是 A/B 的**基准配置**（所有变体共享）。覆盖后报告首行会打印「⚠️ 已覆盖环境参数，非实盘默认口径」，避免把冒烟结论误当成实盘结论。它解决的实际问题：本策略在部分区间/股票池上每日正式推荐可能为 0，此时所有变体都是 0 笔交易、对照表毫无信息量；固定住准入门槛后仍能观察闸门对信号集的影响（但结论只适用于该环境口径）。

变体定义见 `backtest.py` 的 `AB_VARIANTS`：`quality_value` 模式为 `baseline` / `qv_veto` / `qv_veto_d1` / `qv_veto_d3`（后者回答「要不要在生产默认模式接入 KDJ/MACD 否决」）；`technical` 模式为 `baseline` / `no_kdj` / `kdj_k70` / `macd_d1`（回答「闸门调松的边际影响」）。报告写入 `backtest_output/ab_report.md`。

> **⚠️ 覆盖范围边界**：`backtest.py ab` 的两个模式都走 **bottom_fishing_strategy** 的 `evaluate()`（见 `_screen_day`），因此它**无法**验证放量突破策略层 3.8 的两个闸门。突破策略的闸门 A/B 与漏斗归因请用 `src/volume_breakout_backtest.py`（下节）。

### 突破策略闸门 A/B 与漏斗归因

`src/volume_breakout_backtest.py` 是突破策略的离线事件回测（吃本地 CSV，不联网），可直接指向预取缓存：

```bash
# 漏斗归因：定位「0 信号」卡在哪一层（先跑这个，再谈闸门有效性）
python -m src.volume_breakout_backtest --input backtest_cache/daily --funnel \
  --max-stocks 80 --tail-bars 180 --regime bull

# 闸门 A/B：gates_on / gates_off / kdj_off / macd_off
python -m src.volume_breakout_backtest --input backtest_cache/daily --ab-gates \
  --max-stocks 80 --tail-bars 180 --regime bull
```

- `--max-stocks` / `--tail-bars` 用于控制耗时（突破判定逐日重算指标，全量 798 只 × 全历史会非常慢）。
- A/B 强制 `RECOMMENDATION_MODE="technical"`：`quality_value` 下 `evaluate_breakout` 会先委派 `evaluate_quality_value`（需多年财务数据），OHLCV 面板必然在层 1 失败 → 恒 0 信号，对照表没有意义。
- 该模块只把 `tier == "formal"` 的信号计为可成交事件。**当前已知缺陷**：`evaluate_breakout()` 返回的 `BreakoutSignal` 在构造时就显式写死 `tier="pending"`（`formal` 提升发生在 `main_breakout()` 的决赛圈终审——市场日期、财务核心项、补齐后的否决项、周线数据充分性/新鲜度与条件、行业名额全部通过才置 `formal`），而独立回测直接调用 `evaluate_breakout`、不经过该路径，所以该模块目前恒不成交——该事实已固化为 `test_breakout_ab.py` 最后一节的断言；修复会改变行为，按「改产线口径前先 A/B」的约定未擅自修改。

**读表须知**：`可成交` 笔数低于 30 时，胜率与平均收益的差异不具统计意义，只可作方向性参考；必须同时看 `MACD走弱否决` / `KDJ高位否决` 两列——若为 0，说明差异并非来自闸门，不能据此判定闸门有效。

### 直接运行策略脚本（不落库、不发送通知）

```bash
# 优质低估低位策略：仅选股，打印明细
python src/bottom_fishing_strategy.py screen

# 突破策略：仅选股，打印明细
python src/volume_breakout_strategy.py screen
```

### 运行单元测试

以下测试均使用合成数据/打桩数据源、不访问网络，改动策略或主流程后应先跑它们（**当前全部通过**）：

```bash
python test_entry_filters.py      # 单只判定：15 个入场场景 + 分层/时效/双低点背离/量价分档/评级同源/排序可复现（68 项断言）
python test_main_layering.py      # main() 编排：周线路径与禁用路径下的正式推荐/待核验候选分层（15 项断言）
python test_strategy_fixes.py     # 2026-09 修复回归：配置/ROE年化/流动性与波动率归因/波动率观察池采集与渲染/周线已收盘bar/regime滞回（含每自然日最多推进一次）/北京时区/向量化对拍/突破评分校准 + **数据源降级措辞、估值口径声明与 valuation_mode 逐行标注**（76 项断言；飞书相关 14 项在缺 requests 时自动跳过）
python test_optimizations_p0.py   # P0 优化项：pct_chg 双源口径统一/停牌缺口过滤/排序质量分 rank_score/推荐数量上限与市场级熔断（75 项断言）
python test_momentum_gates.py     # KDJ/MACD 下限闸门：纯函数边界与缺数据行为/突破接线（既有形态无回归 + KDJ 高位拦截可开关复现）/FAIL_MACD_WEAK 结构性不可达的扫描断言/quality_value 接线（技术面不新增否决 + 深度弱势被拦 + 健康匀速上行不误杀 + 成长数据缺失按默认降级 pending 且无其它缺项）/**熊市止跌闸门（库级默认已开启 · 熊市与 unknown 均拦 · 牛市不生效 · 显式关闭即放行）**/「新闸门在抄底 technical 路径会被既有严格确认层架空」的覆盖关系断言（45 项断言）
python -B -m unittest test_recommendation_upgrades -v  # 荐股四项升级（#1~#4）：综合分下限(仅formal)/前瞻确认(恶化否决·缺失可配)/行业相对估值(分位·回退·快照)/突破独立(technical不委派)/市场级刹车(急跌熔断·regime收缩·max_picks截取) + **综合分下限等效门槛固化（压线质量分 50、两口径综合分上限 54/40、过线所需技术分表）**（26 项，离线）
python test_backtest_ab.py        # backtest ab 子命令离线测试：变体定义自检（模式名/重名/字段拼写）--set 类型强转与非法字段报错/报告渲染与警示留痕/CLI 接线（24 项断言，不联网）
python test_breakout_ab.py        # 突破策略闸门 A/B 与漏斗诊断：GATE_VARIANTS 字段自检/_technical_cfg 不污染默认值/funnel_counts 计数守恒/gate_ab 变体与非法名报错/报告渲染口径/CLI 四开关/空标记文件跳过与坏文件仍报错/「tier 从不提升为 formal → 独立回测恒不成交」的缺陷固化（30 项断言，不联网）
python -m unittest test_fundamental_quality test_quality_recommendations   # 年度质量与默认荐股口径（26 + 12 = 38 项）
python -m unittest test_minimal_quality_repairs   # 最小修复口径：流动性边界 / 估值评分 / 成长报告期 / 突破核验与通知过滤（7 项）
python -m unittest tests.test_backtest_execution tests.test_breakout_verification_offline tests.test_quality_backtest_persistence tests.test_tracking_idempotency   # T+1 成交模拟(12) / 突破核验离线(10) / 优质低估低位回测持久化(5) / 固定期限追踪幂等(10)
```

## 数据持久化

两张表，首次连接时自动创建（`CREATE TABLE IF NOT EXISTS`，幂等）：

| 表 | 写入方 | 说明 |
|----|--------|------|
| `stock_recommendation` | `run.py` / `run_breakout.py` | 每日推荐明细，`(rec_date, code, strategy)` 唯一（`uk_rec_date_code_strategy`），`INSERT IGNORE` 重复写入自动忽略 |
| `stock_tracking` | `run_weekly_tracking.py`（每周） | 固定期限表现追踪，`(rec_id, holding_trade_days)` 唯一（`uk_rec_horizon`）+ `(rec_id, unique_close_date)` 唯一（`uk_rec_close_date`，生成列，历史重复行映射 NULL 后不参与约束）；`week_no` 原唯一索引已降为普通索引 |

**strategy 来源字段**：`stock_recommendation.strategy` 区分 `bottom_fishing`（优质低估低位，含 technical 抄底对照口径）/ `volume_breakout`（放量突破）。同一股票同日可被两套策略分别推荐并存（组合层去重默认已拦截，唯一键兜底）；**周度追踪**按来源分列统计（飞书卡片分列 + 逐只 `[优质低估低位]/[放量突破]` 标签）；**信号归因**按 `strategy × holding_trade_days` 分列统计持有期效应，并额外给出 `by_strategy` 分组。存量表由程序自动幂等迁移：推荐表补 `signals_hit` / `strategy` 等新列并把旧唯一键 `(rec_date, code)` 升级为 `(rec_date, code, strategy)`（历史行回填 `'bottom_fishing'`）；追踪表由 `_ensure_tracking_schema` 补 `holding_trade_days` / `legacy_duplicate` / `unique_close_date`，旧行保留原周号、日期与价格，同 `rec_id + close_date` 仅最早一行 `legacy_duplicate=0`，其余标 1（迁移用数据库锁串行化）。

**追踪口径**：以沪深300行情日期定位**推荐后第 5 / 10 / 15 / 20 个市场交易日**（`TRACK_HORIZONS`），每个期限各观测一次；允许 60 自然日内补跑（`TRACK_MAX_AGE_DAYS`），观测期限仍止于第 20 交易日（`TRACK_MAX_WEEKS`=4）。**目标日停牌或无有效报价则跳过，不以前后日期前填、也不替换为最新价**。收益率 =（目标日收盘价 − 推荐时收盘价）/ 推荐时收盘价 × 100%。同一推荐同一收盘日、同一期限双重幂等；`holding_trade_days` 为 NULL 的历史行不参与固定期限归因（其中未知期限不猜测、不混入新口径）。同一股票在不同日期被重复推荐，视为不同记录、各自独立追踪。

**信号归因**：`run_monthly_attribution.py` 基于两表历史数据统计各信号维度（等级 / 评分分档 / 底背离 / 市场环境 / 持有周次（策略×期限）/ 策略）的胜率与收益，用于用真实运行数据反哺信号质量评估。**总体口径只取「推荐后 20 交易日」观测**（不混合不同成熟度），各策略另按 5/10/15/20 交易日分别统计；平均峰值为已完成 20 日推荐在固定期限观测中的最高收益，**不代表每日最大浮盈**。**这不是回测**——只统计已落库的真实推荐与真实追踪收益，因此样本需要时间积累。

> **只有 `rec_tier='formal'` 的正式推荐会落库**；待核验候选（数据缺失）不写库、不参与追踪，避免污染归因样本。

## 通知样例

推送到飞书群的消息卡片分三类。**卡片标题按「本次实际生效的口径名」取名**（`【优质低估低位】` / `【放量突破】` / technical 对照口径显示 `【低位企稳】`），不再出现【抄底策略】优质低估低位荐股 这类自相矛盾的组合——`bottom_fishing` 这个落库值覆盖 quality_value 与 technical 两种口径，标题只用数据推断会误标，故由调用方显式传 `recommendation_mode`；**每策略最多展示 Top-3**（`NOTIFY_TOP_PER_STRATEGY=3`，在策略层 `MAX_PICKS` 截取之上再做一次通知口径收敛，宁缺毋滥）；**pending 不渲染**（`pending` 参数仅为兼容旧调用签名而保留）。

> **卡片上的都是「正式推荐」，不是「候选」**：`notify_screening_result` 在渲染前会按 `tier` 过滤掉全部非 `formal` 标的，因此卡片里出现的每一只都已落库、并被周度追踪统计战绩。单票标签原写作「XX候选」，因中文里「候选」天然偏「备选／仅供参考」而与实际含义相反，现统一改为「**正式推荐 · XX口径**」；同时每只票都渲染**操作计划**块（建仓区间 / 止损 / 止盈 / 建议持有周期，见 `describe_trade_plan`），两套策略共用同一格式。

**两种「容易误读」的情形都会显式声明**：

- **数据源降级**（Baostock 熔断、全程走 AkShare）：零推荐时标题直接写「**数据源不足，本次无正式推荐**」而不是「今日无信号」，正文点明「原因是数据源，不是市场」——AkShare 逐 bar 无 peTTM/pbMRQ、也无成长数据通道，估值闸门与前瞻确认会双双记缺项，formal 因此为 0。
- **估值口径回退**：策略层把实际口径逐行写入 `valuation_mode` 列（`_tag_valuation_mode`），`run.py` 汇总后透传；为 `absolute`/`mixed` 时卡片声明「**本次为绝对阈值回退 / 混合口径**」并提示不可与行业口径结果直接比较（绝对口径系统性偏向低 PE/PB 的银行、地产、周期）。

| 卡片 | 触发 | 内容 |
|------|------|------|
| 选股结果 | 每日选股后（两个入口各自独立卡片，标题按实际口径名取名，单票格式按策略区分） | 市场环境、正式推荐数量（均为**正式推荐** · 优质低估低位／放量突破）+ **名单口径说明**、每只股票的代码/名称/「正式推荐 · 口径」标签/评分/等级/收盘价/入选依据（优质低估低位含行业估值分位、当年净利同比；突破含级别与幅度）+ **操作计划**（建仓区间＝信号日收盘价 ×(1 ± `ENTRY_BAND_PCT`=1.5%)，次日开盘执行；止损；止盈；盈亏比；建议持有 `HOLD_DAYS_HINT_MIN~MAX`＝10~20 个交易日）+ 核验状态；quality_value 卡片另附**决策简报**（信心/今日触发/看多/风险/失效价）；另附**⚠️ 波动率风控否决（高风险观察池）**独立区块（逐只展示风险等级、ATR%/上限/倍数与风险提示，最多 5 只，并明确「不构成买入建议」；该区块仅 technical/突破路径会有内容）；无正式推荐时提示「今日无信号」（波动率观察池非空时标题附带条数），数据源降级时标题改为「数据源不足，本次无正式推荐」，执行异常时推送错误信息 |
| 周度追踪 | 每周追踪后 | 追踪数量、胜率、平均收益、状态分布（`推荐后N交易日`，N∈{5,10,15,20}）、**按策略分列**（优质低估低位/放量突破各自数量/胜率/平均收益）及逐只明细（带 [优质低估低位]/[放量突破] 标签） |
| 信号归因月报 | 每月归因后 | 统计周期（含「推荐后20交易日」口径说明）、已追踪推荐数、整体胜率、平均收益/平均峰值收益，以及按等级/评分分档/底背离/市场环境/持有周次（策略×期限）的分组统计 |

## 技术指标

| 指标 | 环节 | 用途 |
|------|------|------|
| MA5/EMA5/10/20 | 日线 | 趋势转折（拐头/金叉，同源信号合并计分） |
| MA20 斜率 | 日线 | 下降通道过滤（陡峭下降中趋势转折不认可，防接飞刀）；突破策略要求 MA20 上行 |
| 当日涨幅/跳空幅度 | 日线 | 防追高否决（technical：涨幅 >5% / 跳空 >2%；突破：涨幅 >7% 或 <2% / 跳空 >2%） |
| 近5日累计涨幅 | 日线 | 已反弹一段否决（累计 >12%，比 RSI 更灵敏） |
| RSI14 上限 / 量比上限 | 日线 | 已反弹一段否决（RSI>60）/ 天量出货否决（量比>4）；突破策略检查**前一日** RSI ≤80 |
| 60日高点回撤（下限/上限） | 日线 | 底部区域过滤（回撤 <10% 判上涨中继；>70% 判崩盘型/价值陷阱） |
| 20日区间位置 / MACD柱 / KDJ | 日线 | 严格确认：区间下半部 + MACD柱连续2日改善 + KDJ金叉、K≤55 且 K 上行 |
| MACD 柱深度弱势 / KDJ 高位 | 日线 | **荐股控制闸门**（`FAIL_MACD_WEAK` / `FAIL_KDJ_HIGH`）：柱值/收盘 ≤−0.5% 且连续 2 日递减（双条件 AND）／K >85 或 K ≥80 且 K<D。突破策略默认开启（`REQUIRE_BR_MACD_NOT_WEAK` / `REQUIRE_BR_KDJ_NOT_HIGH`）；quality_value 模式由 `QV_ENFORCE_KDJ_MACD_VETO` 控制（默认关闭，保持「技术面不作否决」口径） |
| MACD 柱连续改善 + 站上 MA20 | 日线 | **熊市止跌闸门**（`FAIL_BEAR_TIMING`，`QV_BEAR_TIMING_GATE` **库级默认开启 = 生产口径**）：熊市（含 unknown 折叠）的 formal 推荐须满足其一，否则否决 |
| 现价 vs MA20/MA60 + 距 250 日低点天数与幅度 | 日线（展示） | P0 择时读数：右侧·站上MA20/MA60 / 右侧雏形 / 左侧·MA20下方 / ⚠左侧·仍在下跌（MACD深度走弱）；**仅标签，不参与否决与排序** |
| ATR% | 日线 | 波动率风控（technical ≤3.33% / 突破 ≤4.0%，超限否决 FAIL_VOLATILE；quality_value 无此层，故其观察池恒为空） |
| 周线 MA10 / MACD | 周线 | 决赛圈确认（默认「站上MA10（容忍2%）**或** MA10 上行」满足其一即可，`WEEKLY_MA_BOTH_REQUIRED=False`；另 MACD柱翻红或绿柱连收2周；**只用已收盘周 bar**；数据缺失/滞后 → 待核验候选） |
| RSI14/7/21 | 日线 | 超卖反弹 + 底背离判断（前一个价格低点当根的 RSI 对比）+ 多周期共振 |
| 成交量比 + 收盘位置/实体/上影线 | 日线 | 量价质量分（放量企稳 25 / 放量上涨 18 / 放量冲高回落 10） |
| 三级阻力位（60日高/20日高/MA60） | 日线（突破） | 突破级别判定（L1/L2/L3；`REQUIRE_L1_OR_L2=True` 默认只认 L1/L2，L3 整体关闭） |
| 量比/成交额/量能分位/额比 | 日线（突破） | 放量四重确认（1.8–4.0 倍 + ≥1亿 + 60日80%分位 + ≥中位×1.5） |
| 平台振幅/收盘离散度 | 日线（突破） | 突破前整理充分度（振幅 ≤18%、std/mean ≤4%，shift(3) 避污染） |
| 最新K线日期 | 日线 | 数据时效（与沪深300最新交易日不一致 → 停牌/滞后，暂不推荐 FAIL_STALE） |
| 相邻K线自然日间隔 | 日线 | 停牌缺口过滤（>12 天 → FAIL_HALT_GAP，滚动指标跨缺口失真） |
| ATR% / 回撤深度 / 20日区间位置 | 日线（排序） | 连续质量分 `rank_score`（仅决定同批通过者先后，不改准入；`RANK_QUALITY_WEIGHT`=10 可置 0） |
| 成交额(20日均值) | 日线 | 流动性过滤（<3000万 否决，独立归因 FAIL_LIQUIDITY） |
| 沪深300 MA20 斜率 + MA60 趋势 | 市场环境 | 牛/熊/中性 regime（`MARKET_REGIME_DUAL_INDICATOR` 默认开启：斜率与 MA60 趋势须同向，否则降级中性；另含连续 2 日确认的滞回机制） |
| 沪深300 近5日累计涨跌幅 | 市场环境 | 市场级熔断（≤ −4% → 本次运行不推荐，优质低估低位入口） |
| 行业分类 | 集中度 | 行业分散（同一行业最多 2 只，避免单一板块押注） |
| PE/PB 行业内分位 / 绝对阈值 | 估值（口径） | 行业相对估值回退绝对阈值时逐行标注 `valuation_mode`，飞书卡片显式声明本次口径 |

## 数据源

**双数据源架构**：Baostock 为主、AkShare 为备，自动切换。

- **主源 Baostock**：`query_history_k_data_plus`（个股/指数日线，前复权）、`query_all_stock`（股票列表）、`query_stock_industry`（行业分类，用于行业分散与行业相对估值快照）、`query_profit_data` / `query_balance_data`（ROE/负债率）、`query_growth_data`（最新报告期净利润同比 YOYNI，用于前瞻确认）
- **备源 AkShare**：`stock_zh_a_hist`（个股日线，东财通道；不可达时自动切 `stock_zh_a_daily` 新浪通道）、`stock_zh_index_daily`（指数）、`stock_info_a_code_name`（股票列表）、`stock_financial_analysis_indicator` / `stock_balance_sheet_by_report_em` / `stock_profit_sheet_by_report_em`（基本面，**technical 与突破路径的决赛圈按字段补齐**商誉/扣非与主源缺失的核心项，只填 None 字段、不覆盖主源）
- **AkShare 独立通道（年度质量）**：`src/fundamental_quality.py` 的 `fetch_annual_quality` 走 `stock_financial_abstract`（新浪财务摘要宽表「常用指标」）取 ROE / 扣非净利 / 合并净利 / 经营现金流净额，供 quality_value 的「连续 3 个完整年度」质量核验——**不依赖 Baostock**，也**没有 AkShare 兜底之外的替代源**（取数失败按缺证据处理，绝不当作通过）
- **切换规则**：
  - 单条取数失败（异常或返回空）→ 自动用 AkShare 兜底重取
  - Baostock 连续失败达 8 次 → 熔断，本次运行后续请求直接走 AkShare（避免逐股无效重试）
  - Baostock 登录失败 → 直接降级 AkShare 跑完全程（AkShare 未安装则报错退出）
- **连接管理**：登录真实重试（检查 error_code）、查询失败自动重连、全局线程锁防止 C++ 底层 socket 多线程踩踏；模块级 `socket.setdefaulttimeout(20)` 防假死
- **失败重试**：请求异常按退避重试（个股 `MAX_RETRY=2`，股票列表 `LIST_MAX_RETRY=4`）；「返回空」视为该标的确实无数据，不重试
- **两级缓存**：内存 `CacheManager`（进程内，`CACHE_EXPIRE_HOURS=4` 小时）→ `cache/` 目录磁盘缓存
  - 行情类按交易日失效：新交易日自动重拉，同日重复运行走缓存
  - 股票列表按 `CACHE_TTL_DAYS=6` 天、基本面按 `FUND_CACHE_TTL_DAYS=7` 天（`fund_v2_*`；主源缓存名带起始季度标签，报告期滚动后自动失效，备用源为按 TTL 失效）、行业分类按 `INDUSTRY_CACHE_TTL_DAYS=30` 天失效；年度质量 `annual_quality_v1_<code>_<决策日>_<年数>.json` 与成长 `growth_v1_<code>_<年>Q<季>.json` 同样按 7 天 TTL，且各自按决策日/季度隔离
  - 磁盘只缓存未过滤的原始列表，过滤在返回时应用，改 config 即时生效无需清缓存
  - 两个数据源的缓存文件命名天然隔离（Baostock 带 `sh.` 前缀、AkShare 为纯 6 位数字），互不污染
- **股票池过滤**：`MAIN_BOARD_ONLY` 默认开启——白名单仅保留普通 A 股账户可直接交易的沪深主板（60/00 开头）；创业板（开户需 10 万资产）、科创板/北交所/港股通（需 50 万资产）等对个人资金有门槛的板块，以及港股/B 股等非 A 股证券全部排除。关闭该项则退回由 `FILTER_ST` / `EXCLUDE_DELISTING` / `EXCLUDE_BSE`（默认开启）与 `EXCLUDE_CHINEXT` / `EXCLUDE_STAR`（默认关闭）组合控制

## 免责声明

本策略仅为量化研究工具，不构成投资建议。策略优化旨在从逻辑上减少低质量信号，不保证提高未来收益率或胜率。实际效果必须通过严格的样本外回测验证。投资有风险，入市需谨慎。
