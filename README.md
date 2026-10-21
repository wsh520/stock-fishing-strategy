# A股日线量化选股策略 - 自动化执行系统

以**基本面优秀、估值便宜、价格处于中长期低位**为目标的荐股系统，通过 GitHub Actions 自动执行：**每日选股** → **每周追踪** → **每月信号归因**。推荐记录持久化到 MySQL，结果通过飞书机器人推送。

当前共三条 sleeve：**优质低估低位**（`run.py`，生产默认 `quality_value`）、**放量突破**（`run_breakout.py`，强制 `technical`）、**长期优质低估低位**（`run_longterm.py`，落库 `longterm_quality`，2026-10-13 修复后可用）。三条共用同一份数据层与筛选骨架，资格口径各自独立。

## 当前荐股运行口径

**最高原则：宁缺毋滥**——宁可当日不推荐，也不要错推荐。任何口径改动都必须满足「放行集合只减不增」；收紧优先于放宽；**不可观测的风控等于没有风控**（长期零命中的死闸门比没有闸门更危险，它给出虚假安全感，例如 `MAX_ATR_PCT` 原为 10%，而主板池超限占比实测 0.000%）。

当前规则见[荐股调整说明](docs/quality_value_recommendations.md)、[近期经营数据契约](docs/recent_operating_contract.md)、[本次证据升级边界](docs/recommendation_evidence_upgrade.md)、[长期策略设计](docs/longterm_quality_strategy.md)及[「宁缺毋滥」修复实测记录](docs/strategy_evaluation_2026-10-07.md)。先核验资格与分层，再排序；缺失或走弱证据留观察，只有formal进入正式名单。规则分只用于排序，不作为收益改善的证据。

> **真实推荐追踪结论（2026-10-13，本项目首次用落库数据评价自己）**
>
> `diag_real_recs.py` 把 2026-09-08~10-09 的 40 条落库推荐逐条对本地前复权行情做了前瞻统计（31 条 bottom_fishing 正式推荐可追踪）：**F5 中位 −1.87%、下跌占比 76.5%（13/17）**，F1 下跌占比 55.2%，10 日窗口最低价中位 −2.39%（买入后先浮亏是常态）。同期沪深300 是**上涨**的（09-16→09-21 +1.32%、09-17→09-21 +1.78%），因此这不是大盘拖累，而是策略自身跑输。
>
> 归因到三件事：**(1)** 48% 的推荐（15/31）自带「左侧·MA20下方待企稳」标签，该组 F5 **全部下跌**、中位 −3.67%、胜率 0% —— 未企稳被当成已确认放行，现要求 medium 必须站上 MA20（10-13 收紧）；**(2)** 越跌越推闭环（云天化连推 3 次、推荐价 28.29→27.95→27.56，每次推荐后首日收益全负），现由重复推荐冷却打断（10-13 收紧）；**(3)** 执行参数不对称（止损触发率约为止盈的 5.7 倍）属交易执行层，10-13 **已按「执行中期化」处置**：持有 10~20 → 40~60 个交易日、止盈 +30% → +15%、止损不动、追踪口径加 40/60——依据是 strict 幸存者 F20 −1.40% 而 F60 +3.55%（`audit_current_strict.out`，边缘在 60 日）+ 31880 次随机起点实测（+30% 止盈在 20 日窗口触发率仅 5.9%）。准入闸门一律未动。
>
> 证据缺口须一并记住：行情缓存截至 2026-09-30，F10 仅 1 个样本；9 月那批记录的 `daily_score=0` 是**旧口径历史数据**，不代表现行准入（现行代码落库与准入同用 `qv_daily_score` 状态分）。

## 当前默认：优质低估低位荐股

`StrategyConfig.RECOMMENDATION_MODE="quality_value"` 是 `run.py` 默认：连续年度质量+近期经营与财务risk+相对及自身历史估值+250日低位+近期底部与止跌。低位是相对自身价格序列的位置。`VolumeBreakoutConfig` 与 `run_breakout.py` 默认 `technical`，提供独立右侧突破信号。`LongTermQualityConfig` 与 `run_longterm.py` 是第三条 sleeve（对外「长期优质低估低位」，落库 `strategy="longterm_quality"`）：复用 quality_value 全部资格闸门，在其上叠加 5 年质量、ROE 稳定性、盈利持续性、5 年现金流、资质评分、相对强度硬门与加速下跌否决等收紧项（详见「策略三」一节与[设计文档](docs/longterm_quality_strategy.md)）。

| 必选维度 | 当前默认规则 |
|---|---|
| 年度质量（非金融） | 连续3个可用完整年度：ROE中位数≥10%、每年ROE≥5%、扣非/合并净利润/经营现金流每年为正，三年现金流合计/合并净利合计≥0.8；缺年度不以更旧年度顶替 |
| 相对PE估值 | 默认行业PE分位≤60%，优先PB/PE盈利能力proxy在自身0.5～2倍区间的可比组（至少10样本）；不足回退整个行业，行业PE样本仍不足回退 `0<PE TTM≤25`。PB/PE不是实际年度ROE，也不是增长可比组 |
| PB职责 | PB≤0否决；默认高PB只提示风险、不单独评分或否决，缺PB不单独pending。PB可参与proxy构组并间接影响PE分位；显式双护栏可恢复PB硬门 |
| 自身历史PE | `REQUIRE_HISTORICAL_VALUATION=True`：决策日前最多250根行情中至少120个正且有限PE样本，当前PE中秩分位≤0.60；超限否决，缺证据pending；当前日不计入历史样本 |
| 周期行业 | 实际编码B06/B07/B08/B09/B10/B11、C25/C26/C30/C31/C32、G55及名称识别；关闭相对估值仍分类，未知行业pending。归母利润正常化PE默认≤25，至少3年归母利润证据与归母TTM；不得混合合并净利口径 |
| 近期经营 | 最新真实公告报告与去年同周期比较六维指标；同比单项下降容忍3%，现金流下降容忍3%且现金转换≥0.8；收入/合并净利/现金流三项同降仍failed。单季/TTM只作辅助，不能补核心缺失 |
| 财务risk | `REQUIRE_FINANCIAL_RISK=True`：同报告期/真实公告日/来源/合并人民币口径，负债率≤70%、商誉/净资产≤20%、扣非/合并净利≥0.5；缺核心字段pending，已知硬伤否决，缺商誉不能填零 |
| 价格位置 | 至少250根有效日线，收盘价序列中秩分位≤40%；同价取中间名次，取数窗口600自然日 |
| 近期底部 | 默认 `QV_LOW_ANCHOR_MODE="recent_structure"`：近60日收盘低点年龄5～40个交易日；距底上限 `5×ATR/现价` 限制在10%～20%（`recommendation_factors.assess_low_structure`），未确认pending、超限否决（`FAIL_OVEREXTENDED`）。`legacy` 模式改用250日盘中极值 + `QV_MAX_GAIN_FROM_250D_LOW`=15% 判定，同一否决码 |
| 止跌与技术40 | 默认strict：MA20确认且站上MA60为strong；**medium 必须站上MA20**（`QV_STABILIZATION_MEDIUM_REQUIRES_MA20=True`，2026-10-13收紧）——旧式左侧通道（MACD连续3日改善且近5日不创新低）不再构成medium，降级为weak只观察；无确认否决。默认状态技术分趋势40+动能30+量价30，持续状态与最近5日衰减事件计分，技术<40（`MIN_TECHNICAL_SCORE_FORMAL`）为pending。`QV_STABILIZATION_MODE` 可切 `risk_only`（只否决MACD深度弱势且恶化，「站上MA20」降级为标签）与 `disabled`；**默认仍为 strict** |
| 重复推荐冷却 | `QV_REPEAT_COOLDOWN_DAYS=10`（2026-10-13）：同一股票在冷却期内已落库推荐过则**不再重复推送**，移入待核验（可见、不丢弃、不落库）。集合由 `run.py` 查库（`store.fetch_recent_recommended_codes`）经 `main(recently_recommended=…)` 传入，**策略层不碰 DB**；过滤走纯函数 `_apply_repeat_cooldown`。DB未配置时不生效，但 run.py 与筛选层**两处都显式打印状态**——不可观测的风控等于没有风控。设为0可关闭 |
| 额外分数线 | `MIN_QV_SCORE=0`、`MIN_QUALITY_SCORE=0`，默认关闭重复综合分/质量分硬门；旧38/50不是当前规则。年度资格、止跌与技术40仍必须核验 |
| 执行口径（交易计划） | **纯展示，不参与准入/排序/否决**（2026-10-13 执行中期化）：持有 **40~60** 个交易日（原 10~20）、止盈固定 **+15%**（原 +30%）、止损 `max(3×ATR, 15%)` 不变、建仓区间＝信号日收盘 ±`ENTRY_BAND_PCT`=1.5% 次日开盘执行。依据：strict 幸存者 F20 −1.40% 而 F60 +3.55%（`audit_current_strict.out`）+31880 次随机起点实测（+30% 止盈 20 日触发率仅 5.9%）。突破 sleeve 为短线口径（持有 10~20、止盈 min(+15%,+5×ATR)）已显式覆写，不随本改动变长 |
| 行情时效与缓存 | 北京时间盘中/闭市阶段用行情 `.meta.json` 记录版本、取数时间、阶段和末bar日期；touch/mtime不能证明新鲜。独立覆盖当日的交易日历给出应完成交易日，指数必须核验，不能由index自身证明index时效 |
| 风控与分层 | 合法OHLC、流动性、相邻bar自然日缺口>12天、ATR上限（共享 `MAX_ATR_PCT=6`，突破4）独立生效；金融专项pending。只有formal进入正式通知与推荐落库 |

**quality_value 准入闸门实际执行顺序**（`evaluate_quality_value`，前一闸门否决即返回归因码，不进入后续判断）：

```
数据校验/时效 → 流动性(3000万) → 停牌缺口(>12天) → 250日低位(≤40%)
  → PE估值(行业分位≤60%，样本不足回退绝对≤25) → PB异常(PB≤0否决/偏高仅标签) → 自身历史PE分位(≤60%) → 行业PE绝对上限
  → 财务risk → 近期经营(六维) → 年度质量(3年) → 净利同比(仅legacy)
  → 技术分计算 → KDJ/MACD下限否决(可选，默认关) → 周期正常化(归母PE≤25)
  → 止跌确认(strict，medium须站上MA20) → 高位保护(近期底部结构)
  → 质量分下限 → 技术分<40(降pending) → 综合分下限 → ATR风控(≤6%)
```

**重复推荐冷却不在上述闸门链内**：它在 `_screen_quality_pool` 排序完成、最终分层之后执行，只把冷却期内已推过的票从 `formal` 移入 `pending_out`，**不改任何闸门判定**，因此放行集合只减不增。

新增否决码必须同步登记到 `_QV_REASON_CODES`，否则漏斗日志会把它误算进 `ERROR`、无法归因（`FAIL_OVEREXTENDED` 就是 2026-10-07 补登记的例子）。该元组包含**含 0 计数**的完整闸门集——长期为 0 的闸门即对区分候选无贡献，是数据驱动瘦身的审计对象。

综合规则分仍为45%质量+30%估值+25%技术，默认用于排序；已核验历史PE以30%权重混入估值分。`rank_score` 再加入近60日相对沪深300强度有界微调（最多±3）与经结构确认的横盘加分（最多5），低位分位额外排序权重默认0。按排序分、质量、估值降序及代码升序排列，排序不能抵消资格硬伤。`describe_qv_floor()` 按实际配置解释显式开启的分数线；权重和必须为1。

> **`P4_VALUATION_WEIGHT` / `P4_TECHNICAL_WEIGHT` 是预注册的重分配套件，默认全 0 = 不改变现状，保留仅为复现实验，请勿启用**。2026-10-03 实测结论与直觉相反：把技术权重 0.25 降到 0.10、估值升到 0.45，实际效果是**大幅放宽**（过线 46 只 → 118 只，+2.6 倍），因为被砍掉的 `0.25×技术分` 是「接近 0 的大头」，换来的 `0.45×估值分` 却是约 30 分的实数；新增的 72 只技术分≈0、PE 中位 12.73 反而比保留组更贵，前瞻 F5/F20/F60 **三个窗口全部更差**。判定权重影响必须用真实分布逐档模拟（`diag_p4_feasibility.py`），只看 `qv_floor_equivalence()` 的理论天花板会得出完全相反的结论。
>
> 同理，`QV_POSITION_RANK_WEIGHT`（250日分位进排序）实测 ρ(F5)=−0.05 / ρ(F10)=+0.12，与未来收益几乎无关，故默认 0。

年度质量优先原始新浪摘要真实公告日；摘要fallback无公告日时保守按次年5月1日可用，显式记录 `availability_assumed` 与公告日假定标签。真实晚公告不被假定覆盖，原始已知失败不被fallback抹掉。近期经营及财务risk必须有真实公告日。免费接口不是历史财报修订版本库，不保证历史证据完整。

**三条 sleeve 并行**：共用数据层、财务risk、近期经营与市场风控，信号资格保持独立。`run.py` 与 `run_breakout.py` 在同一 GitHub job 内顺序执行（突破为后运行者，执行组合层去重与合计上限）；`run_longterm.py` 是独立工作流，只通过「配置类 `LongTermQualityConfig` + evaluator `evaluate_quality_longterm`」两个参数注入长期口径，并发骨架、漏斗日志、pending/volatile 分层、行业去重、落库与通知全部复用 src 层同一份实现（`_screen_quality_pool` 的 `evaluator` 形参就是为多口径预留的正式入口）。

| 策略 | 定位 | 信号类型 | 推荐数量（牛/中/熊） |
|---|---|---|---|
| 优质低估低位（run.py，quality_value） | 中长期低位 | 年度质量+经营/risk+相对及历史估值+周期正常化+近期底部+止跌（medium须站上MA20）与状态技术40 + 重复推荐冷却 | 5 / 4 / 2 + 急跌熔断 |
| 突破（run_breakout.py，technical） | 右侧顺势 | 首日突破、固定原阻力锚点站稳/回踩确认+防雷/经营/risk+周线MA AND；不等同于三年质量认证 | 5 / 3 / 0 |
| 长期优质低估低位（run_longterm.py，longterm_quality） | 长期价值（3~12个月） | quality_value 全部门闸门 + 5年质量(中位ROE≥12/最低≥8/现金转换≥0.9) + ROE稳定性(CV≤0.5) + 盈利持续性(5年中≥3年正增长) + 5年累计现金流/净利≥1.0 + 资质评分≥60 + 行业PE分位≤50% + 流动性5000万 + 相对强度≥−15pp + 加速下跌否决 | 3 / 2 / 1 |

突破周线默认 `WEEKLY_MA_BOTH_REQUIRED=True`：站上MA10（容忍2%）且MA10上行，并按开关检查已收盘周MACD。最近5日确认源于通过完整原事件闸门的突破，固定原锚点；任一随后收盘跌破锚点取消确认。站稳需2日超过锚点0.5%，回踩触及锚点上方2%范围并收盘重新超过0.5%；确认日距锚点超过7%不确认。突破子分和总规则分有界，幅度1%～3%最佳、到7%降至零；市场/行业20日RS最多±3只调整排序，不改原规则等级或资格。

组合层同股去重与每日合计上限 `DAILY_TOTAL_MAX_PICKS=7` 仍由后运行突破入口执行（`run.py` 与 `run_breakout.py` 之间）。长期 sleeve 在独立工作流运行，**不参与**该合计上限，只受自身 牛3/中2/熊1 的数量收缩约束；三者落库值不同（`bottom_fishing` / `volume_breakout` / `longterm_quality`），同股同日可按策略分别入库。市场级急跌熔断为沪深300近5日累计≤−8%；市场regime双指标、滞回、行业分散与推荐量收缩仍沿用既有配置。未知市场数据不能作为已核验环境。

新risk、历史PE、周期、底部、突破分类、子分与RS字段在结果对象存在，不代表全部落库；MySQL目前使用固定列白名单，并未完整保存全部新增证据。文本列 `signals_hit` / `missing_tags` 为 **TEXT**（非 `VARCHAR(N)`），理由见「数据持久化」章节的超长修复说明。

以下“历史技术对照”四层技术过滤是**历史technical对照说明**，不是当前quality_value默认，也不是突破当前默认；其中旧阈值与实验结论只适用于其历史配置。当前突破规则见“策略二”和[突破设计](docs/volume_breakout_strategy.md)。交易/回测/追踪章节描述既有行为，本次荐股证据调整没有自动补齐历史核验或证明业绩改善。

## 历史技术对照：低位企稳（screening_engine.py）

> **模块名与口径的关系**：`screening_engine.py` 承载两种资格判定，对外称谓按**实际生效口径**给出——
> `quality_value`（run.py默认）= **优质低估低位**；以下内容记录旧technical低位企稳配置（底背离、RSI超卖、回撤深度）。历史阈值不能直接当作当前运行值；突破technical是独立规则。

> **模块名 ≠ 落库 strategy 值（重要）**：模块已于 2026-10-15 由 `bottom_fishing_strategy.py` 更名为 `screening_engine.py`（现名更贴合其实际职责：选股引擎而非单一「底背离」策略）。但**数据库 `stock_recommendation.strategy` 列的值仍是 `bottom_fishing`，且不可改**——该值参与唯一键 `uk_rec_date_code_strategy (rec_date, code, strategy)`，改名会造成历史推荐行与新行分裂成两个 strategy sleeve，周度追踪与信号归因按 strategy 分列时胜率被拆开重算。2026-10-13 已发生过一次「传入值不在 `KNOWN_STRATEGIES` 里被静默改写、导致长期 sleeve 推荐与生产 sleeve 撞唯一键而丢失」的事故。因此：`src/screening_engine.py` 是**代码模块名**，`bottom_fishing` 是**持久化枚举值**，两者刻意保持不同。

4 层量化过滤体系：

1. **市场环境过滤**：沪深300日线MA20斜率判断牛/熊/中性环境；熊市不扣分定级，而是把准入门槛**上浮 10 分**（`BEAR_GRADE_BOOST`），保证展示分数与等级始终同源；数据不足时明示「未知」（`UNKNOWN_AS_BEAR`：未知经 `_effective_regime` 折叠为熊市，作用于推荐数量上限的收缩；**门槛上浮只对已确认的 `bear` 生效**——`evaluate()` 按原始 regime 判定，与突破策略 `evaluate_breakout()` 用有效 regime 判门槛的做法不同）；**regime 双指标确认**（`MARKET_REGIME_DUAL_INDICATOR`，见当前默认的市场风控说明）；**regime 滞回**（`MARKET_REGIME_HYSTERESIS`）：牛/熊/中性切换须连续 2 个交易日同向确认，避免斜率在阈值附近抖动导致 regime 逐日跳变（状态存 `cache/market_regime_state.json`，超 10 天自动重置；**确认计数每个自然日最多推进一次**——同一天内多套策略依次运行读的是同一份收盘数据，若允许重复计数会把「连续 2 个交易日」悄悄缩短为「同日翻转」）；**市场级熔断**（`MARKET_CRASH_HALT_PCT`=−8% / `MARKET_CRASH_LOOKBACK`=5）：沪深300 近 5 个交易日累计跌幅越阈 → 本次运行不推荐（数据不足不触发，避免指数缺数误判）
2. **基本面防雷**：年化ROE/负债率为核心否决项（金融业——银行/保险/券商等负债率天然 80%+，按名称关键词+代码白名单识别并单独放宽阈值）；商誉/扣非为可选否决项，主源 Baostock 不提供，**technical 路径的决赛圈与放量突破策略的终审会用 AkShare 按字段补齐后复核**（`_fill_optional_fundamentals`，只填 None 字段、不覆盖主源）。当时quality_value尚未接入本次独立risk；其“商誉为空不否决”的旧处理不能用作当前正式核验依据，当前规则以上表为准。**ROE 口径为线性年化**（Q1×4 / Q2×2 / Q3×4/3 / Q4×1，累计值年化后才与 `MIN_ROE` 年化阈值可比，不再随财报日历漂移）；财报季度按**交易所披露截止日回溯**取「最近已披露季度」（最多回溯 `FUND_LOOKBACK_QUARTERS`=4 季），结果带 `report_period` 标明数据实际所属报告期；基本面磁盘缓存为 `fund_v2_*`（旧 `fund_*` 单季未年化口径已隔离废弃）
3. **日线技术指标筛选**：底背离（**双低点算法**：与窗口内前一个价格低点比较 RSI/DIF，而非指标自身最小值）+ 趋势转折（MA5拐头/EMA金叉同源合并计分）+ RSI超卖反弹 + **量价质量分**（放量阳线收高位/一般放量上涨/放量冲高回落三档）；流动性过滤（近20日日均成交额 ≥3000万，独立归因 `FAIL_LIQUIDITY`）；入场质量否决（当日涨幅 >5% 追高否决、开盘跳空高开 >2% 否决、近5日累计涨幅 >12% 已反弹一段否决、RSI14 >60 否决、量比 >4 天量否决、MA20 近5日斜率 <-4% 的陡峭下降通道中趋势转折信号不认可、距60日高点回撤 <10% 非底部区域否决、回撤 >70% 崩盘型/价值陷阱否决）；严格确认指标（现价须落在近20日价格区间下半部、MACD 柱须**连续 `MACD_MOMENTUM_DAYS`=3 日**改善（与止跌确认闸门共用同一参数，改一处即两处生效）、KDJ 须金叉、K≤55 且 K 值上行）；**数据时效**（个股最新K线与市场最新交易日不一致，即停牌/数据滞后 → 暂不推荐）；**停牌缺口**（相邻 K 线自然日间隔 >12 天判定为期间曾停牌 → 暂不推荐，独立归因 `FAIL_HALT_GAP`：这类股票的 20 日均量/60日高点/ATR 全部跨缺口计算，「60日高点」可能实为数月前的高点）
4. **波动率风控**：ATR 占现价百分比 >6% 直接否决（`MAX_ATR_PCT` / `FAIL_VOLATILE`——由原 10% 下调：主板 799 只 ATR% 最大仅 8.94%，10% 阈值的超限占比为 **0.000%**，是「永不触发的死闸门」，给出虚假安全感；下调后拦掉全市场约 6.8%）；ATR 缺失为 `FAIL_DATA`；`QV_ATR_GUARD=True` 时 quality_value 路径独立生效；止损/止盈/盈亏比**仅作展示与落库，不参与否决**，且 ATR 止损**不得窄于固定 15%**（`compute_risk_reward` 取 `max(3×ATR, 15%)`：主板 ATR% 中位 3.08% 下纯 3×ATR≈−9.2%，20 日内触发率 25.5%，而同期 20 日最大跌幅中位仅 −5.0% —— 会把「本该拿住的价值修复」误杀成「交易失败」；加下限后触发率降至 9.6%，波动大时由 3×ATR 接管并放宽）；**决赛圈周线确认**（`REQUIRE_WEEKLY_TREND`：默认 `WEEKLY_MA_BOTH_REQUIRED=False`，即「收盘站上周线 MA10（容忍 `WEEKLY_TOLERANCE`=2%）」或「MA10 上行」**满足其一**即可——真·低位买点常出现在周线 MA10 尚未上行时，双条件会把目标 setup 全滤掉；设 True 恢复「站上**且**上行」严格口径。**且**须周线 MACD 企稳（`REQUIRE_WEEKLY_MACD_STABLE`：柱值翻红或绿柱连续 2 周收窄）。两个开关独立生效、任一启用即拉周线；**只用已收盘周 bar**——末根周线落在本 ISO 周且非周五即剔除，避免半成品 bar 污染口径（判断基准为北京时间）；数据缺失/截止过旧 → 待核验候选，不占正式名额；取满即止）；**行业分散**（同一行业最多 2 只）；**推荐数量按市场环境收缩**（牛 `MAX_PICKS`=5 / 中性 `NEUTRAL_MAX_PICKS`=4 / 熊 `BEAR_MAX_PICKS`=2，未知经 `_effective_regime` 折叠为熊，统一由 `resolve_max_picks` 解析，周线确认取满即止）

**历史技术分层（当前formal还须满足开头表格中的经营/risk等证据）**：

| 层级 | 条件 | 处理 |
|------|------|------|
| **正式推荐**（formal） | 财务核心项（ROE+负债率）已核验、启用的周线确认已确认、行情日期与市场最新交易日一致 | 落库 MySQL、进入飞书正式名单、参与周度追踪 |
| **待核验候选**（pending） | 技术面通过，但财务或周线数据缺失（含商誉/扣非未补齐、周线滞后、成长数据缺失） | **不落库、不参与追踪、不进飞书卡片**，仅在 CI 日志打印计数与前 10 只明细 |
| **波动率观察池**（volatile） | 前置各层已通过（technical：技术分已达准入线；突破：突破/量能/形态/趋势已过；quality_value：质量/估值/低位已过），**仅 ATR 占现价百分比超上限**被否决 | **仅 CI 日志逐只留档**（含风险等级与风险提示；**不推送飞书**），**不落库、不参与追踪、不是买入建议** |
| 暂不推荐 | 行情日期滞后（停牌/数据过期）或任一项否决条件未过 | 直接淘汰 |

历史technical技术评分：趋势40 + 动能30 + 量价30，满分100。RSI/MACD/KDJ在动能组内取最高，共振不再重复加分。`DAILY_SCORING_MODE="legacy"` 或显式自定义旧权重时使用旧兼容公式。

> **档内连续化默认开启**（`DAILY_SCORING_CONTINUOUS=True`）：旧公式的组分项都是布尔/三档判断，主板 797 只实测 `daily_score` 只有 **12 种取值**（0 分占 31.8%、30 占 25.3%、70 占 23.3%，三档合计 80%）——号称 0~100 的评分实际是近 3-bit 变量，使 `MIN_TECHNICAL_SCORE_FORMAL` 与等级分档失去区分度。开启后按强度细分档内差异（量价组用收盘位置/实体占比/上影线占比连续映射，替换三档离散值），**三组上限不变（40/30/30，总分仍封顶 100），门槛布尔条件也完全不变** ⇒ 只细分档内差异、不制造新通过者，对「宁缺毋滥」是收紧。趋势与动能组的连续加分上限 `W_TREND_STRENGTH` / `W_MOMENTUM_STRENGTH` **默认 0**（最保守：不给这两组加分，避免抬高分总数）。置 False 逐字退回纯布尔求和。

按分数分为 A/B/C/D 四个等级（A≥80 / B≥60 / C≥40），**等级与展示分数同源（底背离不升档）**；准入门槛 `MIN_PASS_GRADE` 默认 B。**排序**（technical 路径）：主键 `rank_score`（= 技术分 + 连续质量分）降序 → 底背离优先 → 盈亏比降序 → **股票代码升序（末级键）**。quality_value 路径改用 **排序分 `rank_score` → 质量分 → 估值分 → 股票代码升序**（`rank_score` = 综合分 + 相对强度有界微调 + 横盘筑底加分，见上文「当前默认」一节）。两条路径都以股票代码为末级键，保证两次运行结果可复现、不受并发完成顺序影响。**连续质量分**（`RANK_QUALITY_WEIGHT`=10，置 0 可完全关闭）只用于打破布尔闸门造成的分数并列，**不参与任何门槛判定**——「哪些股票通过」与引入前完全一致，变的只是通过者之间的先后：低波动 0.35 + 回撤深度 0.35 + 20 日区间位置 0.30（MACD 动能维度权重默认 0，因前置闸门已保证通过者恒为满分），四路取值全部复用已算出的列，零额外取数。执行过程输出选股漏斗日志（各层通过率 + 时效剔除数 + 停牌缺口剔除数 + 待核验候选数 + 取数来源统计）。**波动率风控否决明细**（`[VOLATILE]` 区块）会逐只打印：代码/名称/技术分/等级/收盘/ATR%与上限及倍数/风险档/RSI/量比/已达标项，最多 20 只，超出提示剩余条数；若该层当天零淘汰且日线无通过标的，则改为直接点出主要拦截层（避免「为什么没推荐」无从复核）。

> **保底观察候选（fallback）默认关闭**（`ENABLE_DAILY_FALLBACK=False`）：与「宁可少荐」哲学一致，不再每天硬推一只低置信度票；置 True 可恢复旧行为（候选明确标记 `tier=fallback`，仅作观察，不应直接实盘）。

## 策略二：放量突破（volume_breakout_strategy.py）

捕捉「横盘整理末端 → 放量突破关键阻力位」的趋势启动点。复用数据层、市场环境与筛选骨架，独立日线信号和评估，并覆写突破周线AND默认（七层漏斗）：

1. **基本面防雷**（继承 `check_fundamentals`）；终审核验最新真实公告多维近期经营与独立财务risk，恶化否决、走弱/缺失pending，金融专项pending
2. **流动性 & 数据完整性**（近 20 日日均成交额 ≥ `MIN_AMOUNT`）
3. **日线技术面（核心）**：
   - 三级阻力位：**L1=近 60 日最高 / L2=近 20 日最高 / L3=MA60**；`REQUIRE_L1_OR_L2=True`（默认）= 只认 L1/L2，L3 判定被整体置 False，避免「低位整理股反弹站上 MA60」被误判为突破；置 False 才启用 L3
   - **放量四重确认**：量比 1.8–4.0 + 当日成交额 ≥1 亿 + 成交量处于自身 60 日 80% 分位以上 + 当日成交额 ≥ 前 20 日成交额中位数 ×1.5
   - **K 线形态防假突破**：阳线实体占比 ≥0.5、收盘/最高 ≥0.97、涨幅 2%–7%、跳空 ≤2%（旧 `MAX_GAP_UP_PCT`）
   - **突破前平台整理**：近 20 日振幅 ≤18%、收盘离散度（std/mean）≤4%（窗口右端 `shift(3)` 避开突破 bar 污染）
   - **趋势背景**：MA20 上行、MA60 走平或上行、现价站上 MA20
   - **近期假突破过滤**：近 10 日无「突破 L1 后 3 日内跌回」记录（事件锚定突破日 L1；**向量化实现**，区间差分替代三重循环）
   - 首日之外可确认最近5日原事件的站稳/回踩：固定原阻力锚点，任何随后收盘跌破取消，不能把阻力回落当新突破
   - 突破**前一日** RSI ≤80（突破日天然推高 RSI，检查当日会误杀正常突破）
   - **KDJ/MACD 下限否决**（`FAIL_MACD_WEAK` / `FAIL_KDJ_HIGH`）：把这两个指标从纯评分项升级为硬闸门。此前它们只贡献 `W_MOMENTUM_BR`=10 分（占满分 10%），分档失守仍可守住 B 级 60 分准入线，对「推荐与否」没有约束力。闸门只拦两种形态——**MACD 柱深度弱势且仍在恶化**（柱值/收盘 ≤ −0.5% 且连续 2 日递减，双条件 AND）与 **KDJ 已在区间顶部**（K > 85，或 K ≥ 80 且 K < D）；刻意不要求金叉（突破日 RSV 直接打到 100、K 单日跳升，金叉常滞后 1–2 日，硬金叉会系统性漏掉「窄幅整理后首根放量阳线」）。历史首次突破样本中的MACD不可达结论只适用于当时配置；新增站稳/回踩确认日仍重新核验MACD/KDJ，不能宣称当前全部路径不可触发。
4. **波动率风控**：ATR ≤4.0% 现价（共享质量侧上限为6%，突破独立阈值）
5. **综合评分与等级**：突破强度(35，**0.55×级别 + 0.45×有界幅度（1%～3%最佳，7%降至零）** 加法混合——替代旧纯乘法，旧公式下 L2/L3 级别系数把幅度分压得极低、B 级准入事实上只推 L1) + 量能质量(25，0.7×放量 + 0.3×整理充分度) + 平台整理(15) + 趋势背景(15) + 动能确认(10)；B 级 ≥60 准入，熊市 +15
6. **决赛圈周线确认**：默认MA10站上且上行（AND），另检查启用的周线MACD，只用已收盘周bar；缺失/滞后pending
7. **排序截取**：ranking_score（原规则分+有界RS）↓ → 突破级别↓ → 20日均成交额↓ → 代码↑；牛5 / 中性3 / 熊0

**交易计划**（仅展示与落库）：止损 = max(突破位 − 1×ATR, 现价 × (1 − 6%))（保证最大亏损上限；技术位不低于执行价时才采用技术位，否则回退固定 6%），止盈 = min(现价 × 1.15, 现价 + 5×ATR)（固定目标与 ATR 目标取更近者）。`MIN_RR_RATIO_BREAKOUT` 检查已删除（止损≤6%/止盈15% 下 RR≥2.5 恒成立，检查永不触发）。

**盘中剔除用北京时间**：15:00（东八区）前运行剔除当日未收盘 bar，防止盘中漂移；GitHub Actions 为 UTC，用本地时区会把「当日已收盘 bar」误剔除导致信号系统性滞后一天（已修复）。

```bash
python run_breakout.py                # 放量突破选股 → 组合层去重/总量上限 → 落库 → 飞书通知
python run_breakout.py --no-cache     # 跳过磁盘缓存
python run_breakout.py --no-save      # 仅通知，不落库
python run_breakout.py --no-notify    # 仅落库，不通知
```

## 策略三：长期优质低估低位（run_longterm.py）

定位：为 **3~12 个月持有周期**的价值投资者筛选「长期优质 + 低估 + 低位」的股票。资格判定复用 `evaluate_quality_value` 的全部门闸门（流动性/低位/估值/止跌/ATR），在其上追加长期专项闸门；**准入方向以收紧为主**，底部窗口与周期估值参数按更长的持有周期做了适配（见下表，均为显式配置项而非隐式行为）：

| 维度 | 生产 quality_value | 长期 sleeve |
|---|---|---|
| 年度质量 | 连续 3 年：ROE 中位≥10%、每年≥5%、现金转换≥0.8 | 连续 **5 年**：中位≥12%、最低≥8%、现金转换≥0.9 |
| ROE 稳定性 | 无 | **新增**：变异系数 CV≤0.5（`FAIL_ROE_UNSTABLE` / `FAIL_ROE_STABILITY_INSUFFICIENT`） |
| 盈利持续性 | 无 | **新增**：5 条年报 4 次同比中至少 3 次为正（`FAIL_EARNINGS_INCONSISTENT` / `FAIL_EARNINGS_DATA_INSUFFICIENT`） |
| 现金流质量 | 3 年合计现金转换≥0.8 | **新增**：5 年累计经营现金流/净利润≥1.0（`FAIL_LONGTERM_CASH_POOR` / `FAIL_LONGTERM_CASH_INSUFFICIENT`） |
| 资质评分 | 无 | **新增**：ROE稳定性25% + 现金流25% + 盈利持续20% + 增长质量15% + 估值边际15%，≥60 准入（`FAIL_LONGTERM_QUALITY_SCORE`） |
| 相对强度 | 仅排序微调（±3）与跑输提示 | **新增硬门槛**：近 60 日跑输沪深300 不超过 15pp（`FAIL_RELATIVE_STRENGTH_WEAK`；数据不可计算不否决） |
| 估值 | 行业 PE 分位≤60%、自身历史 PE 分位≤60% | 均收紧至 ≤50% |
| 流动性 | 3000 万 | 5000 万 |
| 止跌确认 | strict（medium 须站上 MA20） | **同生产 strict**（不切 risk_only——那等于放行更多票） |
| 加速下跌 | 无 | **新增否决**：MA20 近 5 日跌幅 >5%（`FAIL_ACCELERATING_DECLINE`，两趟评估都执行） |
| 缩量见底 | 无 | **仅展示标签**（近 5 日均量/近 20 日均量<0.5），不参与准入与排序 |
| 周期正常化PE | 归母利润正常化 PE≤25 | ≤35（周期底部反转期给估值空间；配套的 `CYCLICAL_PB_FALLBACK` 即 PB<0.8 **仅打标签**，不放宽任何准入） |
| 技术分门槛 / ATR | ≥40 / ≤6% | 与生产一致（40 / 6%） |
| 推荐数量 | 牛5 / 中4 / 熊2 | 牛3 / 中2 / 熊1 |
| 交易计划 | 止损 max(3×ATR,15%)、止盈 **+15%**（2026-10-13 执行中期化，原 +30%）、持有 **40~60 日**（原 10~20 日） | 止损 max(4×ATR,20%)、止盈 50%、持有 **30~90 日**（经 `Signal.holding_days_hint_min/max` 正式字段传递） |
| 底部窗口 | 低点年龄 5~40 日、距底上限 clip(5×ATR,10%,20%) | 年龄 3~90 日、窗口 120 日、距底上限 clip(8×ATR,10%,20%) |
| 综合分权重 | 0.45 质量 + 0.30 估值 + 0.25 技术 | 0.55 / 0.35 / 0.10（质量和为 1.0 由基类硬校验） |
| 质量分下限 | 0（关闭） | **60**（真实的额外收紧；注意别再上调到 65——与年度资格重复计权会恒 0 推荐） |

**架构约束（2026-10-13 修复）**：`run_longterm.py` 与 `run.py` 同构（四步流水线：市场环境 → 筛选 → 落库 → 通知），长期口径只通过「`LongTermQualityConfig` + `evaluate_quality_longterm`」两个参数注入，**不要**再写独立编排层。曾因独立复刻编排层与 src 真实接口全面脱节（导入不存在的函数、错误的调用签名）而完全不可用；也因 `_screen_quality_pool` 是两趟评估（第一趟 `fund_data=None` 做廉价行情预筛）而年度财务闸门误在预筛趟执行，导致每只股票 `insufficient`、恒 0 推荐——依赖年度财务的闸门必须判 `fund_data is not None`。

**市场熔断与数量收缩**：`_screen_quality_pool` 不含这两步组合层风控，`run_longterm.py` 在筛选后显式补齐（先熔断后限量，顺序与 `main()` 一致）。

```bash
python run_longterm.py                    # 长期优质低估低位选股 → 落库 → 飞书通知
python run_longterm.py --no-cache         # 跳过磁盘缓存
python run_longterm.py --no-save          # 不落库（测试用）
python run_longterm.py --no-notify        # 不推送飞书（测试用）
python run_longterm.py --compare          # 对比模式：同时跑生产 quality_value 口径，仅打印差异（不落库不推送）
```

## 运行保障

- **统一 logging**：全项目 `%(asctime)s [%(levelname)s] %(name)s` 格式，入口 `run.py` / `run_breakout.py` / `run_longterm.py` / `run_weekly_tracking.py` / `run_monthly_attribution.py` 与策略 `__main__` 各自 `_setup_logging()`
- **防假死**：模块级 `socket.setdefaulttimeout(20)`——数据源底层 socket 默认无超时，曾致持有锁线程永久阻塞、全池假死跑满 6h 被取消
- **AkShare 通道韧性（2026-10-21）**：`src/akshare_resilience.py` 为所有 AkShare 接口补四层保护——线程内复用 keep-alive 连接（实测同一批股票 1.63s/次 → 0.31s/次）、调用方未传 `timeout` 时注入 20 秒（接口自带 `timeout=None`，而 `socket.setdefaulttimeout` 对 requests 无效）、东财/新浪按通道熔断（连续 3 次失败后冷却 120 秒，期间直接走备用通道，不再逐只重试死连接）、单通道有限重试（`MAX_RETRY+1` 次，指数退避+抖动）。运行末尾打印 `AkShare 通道: eastmoney 成功N/失败N/跳过N`。详见[通道韧性说明](docs/akshare_resilience.md)
- **卡死股票放弃**：`run_concurrent_screen` 对单只股票最多尝试 `STOCK_MAX_ATTEMPTS`（默认 3）次，连续失败即放弃并记 `FAIL_DATA`，不再占住线程池——run #111 的 600511 曾卡住 1750 秒以上，直到整条 job 被取消；看门狗告警同时打印在途股票已等待秒数
- **主数据源可切换**：`DATA_SOURCE_PRIMARY`（默认 `baostock`，可设 `akshare`）决定先走哪个源、另一个作兜底；同名环境变量可覆盖配置。Actions 里两种切换方式：仓库变量 `DATA_SOURCE_PRIMARY`（长期生效），或手动触发时勾选 `data_source_primary`（只覆盖本次，优先级更高，不必改仓库配置）
- **AkShare 主源的估值补全（2026-10-21）**：AkShare 日线只有 OHLCV，缺 `peTTM/pbMRQ` 会让估值闸门记缺项、候选全转 pending。新增百度估值通道逐票补 `市盈率(TTM)/市净率`（口径与 Baostock 一致，实测 600000/600036/601318/000001/600519 五只 PE、PB 逐位吻合），并按时序并入当日日线，历史分位（`HISTORICAL_VALUATION_*`）与行业估值快照随之可用。末 bar 估值缺失时**有界顺延**上一交易日（≤3 个自然日，逐票记 `val_carried` 并汇总 `估值顺延 N 只`）。20 只实测：PE 覆盖 20/20、历史分位可核验 19/20、0.49 秒/只（4 线程折算全市场约 6 分钟）。详见[通道韧性说明](docs/akshare_resilience.md)
- **共享并发筛选骨架** `run_concurrent_screen`（三条 sleeve 同一模板，长期 sleeve 经 `_screen_quality_pool` 的 `evaluator` 参数复用）：线程池 + 心跳看门狗（每 1 分钟心跳，连续 4 分钟无完成打印在途股票代码定位卡点；`WATCHDOG_TICK_SEC`/`WATCHDOG_IDLE_WARN_SEC` 可调）+ 时间预算（`SCREEN_TIME_BUDGET_MIN`=240 分钟，超时取消未完成任务、按已完成结果出报告，漏斗统计按已处理数计）
- **行业估值快照进度可观测**：`build_industry_valuation_snapshot` 用独立线程池遍历全 A 拉日线构建横截面，冷启动 cache 全 miss 时曾是 1~2h+ 静默黑洞；现补开始/每 `SNAPSHOT_PROGRESS_LOG_EVERY`（默认 200）只进度（带命中数与 ETA）/成功汇总（含耗时）三段日志，异常路径也带已完成数，纯观测不改口径
- **行业估值快照持久化（2026-10-19）**：成功构建的快照写入 `cache/industry_valuation_snapshot_v1.json`，在 `SNAPSHOT_CACHE_MAX_AGE_DAYS`（默认 7 个日历天）内直接复用；定时清理只删 `daily_*`/`weekly_*`/`index_daily_*`，不删该文件。`SNAPSHOT_TIME_BUDGET_MIN` 从 60 提到 300 分钟，覆盖 #108/#109 已验证的 150.1/225.6 分钟冷启动；超预算仍放弃快照并回退绝对 PE（显式 warning），不会使用部分横截面。详见[快照持久化说明](docs/snapshot_persistence.md)
- **影子账本（2026-10-19）**：`run.py` 把 pending / near-miss 写入 `cache/shadow_recommendations.jsonl`（Actions cache 持久化）与 `data/shadow/`（artifact），不通知、不写 `stock_recommendation` 正式表；`run_shadow_tracking.py` 每周按市场交易日计算 F5/F10/F15/F20/F40/F60，写 `data/shadow/shadow_tracking.json`。详见[影子账本说明](docs/shadow_ledger.md)
- **取数来源统计**：`fetch_stats`（Baostock 命中 / AkShare 兜底 / 双源均失败），漏斗末尾汇总
- **待核验淘汰归因**（`_screen_quality_pool`）：这是「今天为什么没推荐」的第一现场，逐层给出可复核证据：
  - `[QV_PENDING]` 逐只打印——行情日期、**阻断原因**、原因说明（中文）、全部标签、综合分/技术分（对照正式门槛）、止跌层级、年度质量/财务风险/近期经营/周期风险各自状态；
  - `[QV_PENDING_EVIDENCE]` 打印该股近期经营与财务risk 的**证据摘要**（报告期/公告日/来源/缺项/失败原因），用于区分「数据取不到」与「数据取到了但不合格」；
  - `[QV_PENDING_SUMMARY]` 三种口径汇总——阻断原因覆盖（可多项，数量不可相加）、**唯一阻断原因**（仅被这一项阻断）、阻断组合（互斥，合计等于待核验数）。
  - PB 缺项默认只提示不算阻断（`QV_VALUATION_DUAL_GUARD=False` 时从 blockers 中剔除），避免把「PB 偏高/缺失」误读成落选原因；若剔除后无任何阻断项，显式记为 `pending_reason_unknown` 而非静默通过。
- **确定性排序**：优质低估低位策略主键 `rank_score`（quality_value 下追加 质量分→估值分 二级键）、末级键股票代码升序（突破策略为 排序分→级别→20日均额→代码），两次运行结果可复现，不受并发完成顺序影响

## 自动执行

| 工作流 | 触发时间（北京） | 定时表达式（UTC） | 说明 |
|--------|------------------|-------------------|------|
| **Daily Stock Screening** | 交易日 21:00 | `0 13 * * 1-5` | 优质低估低位策略选股 → 落库 → 飞书通知；随后**同一 runner 内**顺序执行放量突破策略（复用前者的当日行情缓存）→ 组合层去重/总量上限 → 落库 → 飞书通知 |
| **Daily Stock Screening (Long-term Quality)** | 交易日 21:30 | `30 13 * * 1-5` | 长期优质低估低位选股 → 落库（`strategy=longterm_quality`）→ 飞书通知；比主策略晚 30 分钟避免 Baostock 取数冲突，行情缓存用独立 key 前缀 `longterm-baostock-cache-`（同样带 `run_id`） |
| **Weekly Recommendation Tracking** | 每周五 21:20 | `20 13 * * 5` | 按沪深300交易日定位推荐后第5/10/15/20/40/60个市场交易日并观测（115 日内可补跑）→ 落库 → 飞书汇总（按期限与策略分列）；随后运行影子账本追踪（`run_shadow_tracking.py`，只写诊断文件、不通知） |
| **Monthly Signal Attribution** | 每月 1 日 09:00 | `0 1 1 * *` | 各信号维度胜率/收益归因（总体仅取「推荐后20交易日」，未满者不计入并在卡片报数）→ 飞书月报 |

**Job 超时预算**（GitHub 托管 runner 单 job 硬上限 6 小时）：`daily_screen` 设 `timeout-minutes: 360`（两套策略共享这一份预算）、`daily_screen_longterm` 180、`weekly_tracking` 120、`monthly_attribution` 30。策略内部另有 `SCREEN_TIME_BUDGET_MIN`=240 分钟的软预算：超时取消未完成任务、按已完成结果出报告，漏斗统计按已处理数计——两层预算独立，软预算先于硬超时生效，保证报告能落盘。

四个工作流均支持手动触发（`workflow_dispatch`）；`Daily Stock Screening` 额外支持以下勾选项：

| 勾选项 | 默认 | 作用 |
|--------|------|------|
| `reuse_cache` | **开** | 手动运行尝试复用行情缓存；仍须meta阶段与独立交易日历核验，touch不能将旧快照变成有效闭市行情（详见下方缓存策略） |
| `purge_cache` | 关 | 手动运行时也清除行情缓存、强制重新拉取，用于复现定时运行的取数行为 |
| `no_cache` | 关 | 两套策略都跳过 `cache/` 读写（既不读也不写，本次不共享） |
| `no_save` | 关 | 两套策略都跳过 MySQL 落库（纯测试运行，避免测试推荐与晚间正式结果混在同一天） |
| `no_notify` | 关 | 两套策略都跳过飞书通知 |
| `skip_breakout` | 关 | 只跑优质低估低位，跳过放量突破 |
| `breakout_no_save` / `breakout_no_notify` | 关 | 只针对突破策略跳过落库 / 通知（细粒度控制，与 `no_save` / `no_notify` 取或） |
| `data_source_primary` | `inherit` | 主数据源：`inherit` 跟随仓库变量 `DATA_SOURCE_PRIMARY`；`akshare` / `baostock` 只覆盖本次运行。解析结果经 `GITHUB_ENV` 注入，`run.py` 与 `run_breakout.py` 用同一个值 |

- **为什么是 21:00**：Baostock 一般 17:30 起陆续更新当日数据、20:00 前完成，21:00 起跑留足安全边际。
- **两套策略为什么合并进同一个 job**：优质低估低位与放量突破的数据层是同一组函数（`get_daily_data` / `get_fundamentals` / `get_stock_list` 由突破模块直接导入），`DAILY_BARS` / `WEEKLY_BARS` / `ADJUST` / `CACHE_DIR` 也完全一致，因此**缓存文件名逐字相同**（`daily_sh.600000_last120_qfq.csv`）。放在同一个 job 里顺序执行，二者共用同一个 `cache/` 目录：优质低估低位侧全量拉取并写入当天行情，突破随后直接读同一份文件，**几乎零重拉**。相比两条独立流水线，既省掉约 3000 次日线 + 数千次基本面查询（这些查询被全局锁 `bs_lock` 串行化，正是运行时长的大头），也天然保证顺序——组合层去重由**后运行者**执行（`fetch_rec_codes_for_date` 剔除当日已被优质低估低位推荐的个股，并按 `DAILY_TOTAL_MAX_PICKS` 默认 7 截取合计上限），不再依赖 cron 时间差。
- **`if: always()` 的用意**：突破步骤带 `if: always()`，优质低估低位侧失败/异常时突破仍会执行——两套策略是独立信号源，不应互相阻塞；前者未写出的缓存由突破自行补拉（慢但结果正确）。
- **行情缓存以meta与交易日历核验**：行情CSV旁的 `.meta.json` 记录版本、带时区取数时间、盘中/闭市阶段与末bar日期。闭市运行不能复用当天盘中取得的缓存；无meta、跨日、未来取数时间等不视为新鲜。独立覆盖当前日期的交易日历确定应完成交易日，再检查指数与个股末bar日期；不能用index自身末日期证明index时效。日历缺失或指数落后时保留未核验处理。
  - 定时任务仍按工作流清理恢复的行情缓存以重取，保留股票列表、财务、行业及regime状态按各自TTL管理；两个入口共享本次运行已核验缓存。
  - 手动 `Inspect restored quote cache (manual runs)` 只检查并保留缓存，不修改行情或meta时间；数据层仍验证meta、取数阶段与交易日，不保证零行情请求、几分钟完成或与重拉结果逐字相同。关闭 `reuse_cache`、勾选 `purge_cache` 或定时执行都会清理恢复的行情CSV及meta，触发重取。
  - 缓存key仍带run id，避免同日期不可变缓存key锁住当天较早快照；key和文件mtime都不是行情时效证据。
- **regime 滞回状态跨策略共享**：`cache/market_regime_state.json` 也在这份缓存里，因此两套策略读到**同一个已确认 regime**（此前两条独立流水线各维护一条状态链，可能互相不一致）。为避免「一天内运行两次 = 确认计数推进两次」把 `MARKET_REGIME_CONFIRM_DAYS=2` 悄悄退化为「同日翻转」，`_apply_regime_hysteresis` 已改为**每个自然日最多推进一次确认计数**（依据状态文件自身的 `updated` 日期，对「一天跑几次」完全鲁棒）。
- **周五 21:20 的用意**：晚于当日 21:00 的选股任务 20 分钟，确保当日新推荐已落库，再由周任务决定首期追踪。
- 缓存传递：`Daily Stock Screening` 与 `Weekly Recommendation Tracking` 使用 `actions/cache` 的 `cache/` 目录，共用前缀 `baostock-cache-<日期>`（每日任务另带 `run_id` 后缀，周任务仍按日期存取，靠 `restore-keys` 前缀命中当天最新一份）；`Daily Stock Screening (Long-term Quality)` 用独立前缀 `longterm-baostock-cache-`（只存不依赖主策略运行结果，带 `run_id`）；归因月报只查 MySQL 不拉行情，因此不涉及缓存。每日任务结束时上传 artifact（`data/` 与 `cache/market_regime_state.json`，`if-no-files-found: ignore`）便于回溯 regime 滞回状态。

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
| `FEISHU_WEBHOOK_URL` | 飞书 Webhook 地址（**生产机器人**，定时任务使用） | 推荐（不配则跳过全部通知） |
| `FEISHU_WEBHOOK_URL_TEST` | 飞书 Webhook 地址（**测试机器人**，GitHub 页面手动运行时使用） | 可选（不配则手动运行静默跳过通知） |
| `MYSQL_HOST` | MySQL 主机地址 | 可选（不配则跳过落库与追踪/归因） |
| `MYSQL_PORT` | MySQL 端口，缺省 3306 | 可选 |
| `MYSQL_USER` | MySQL 用户 | 可选 |
| `MYSQL_PASSWORD` | MySQL 密码 | 可选 |
| `MYSQL_DATABASE` | 数据库名（如 `stock_fishing`） | 可选 |

> **落库为可选能力**：`MYSQL_HOST` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` 四项未配齐时，落库、周度追踪、信号归因全部静默跳过，不影响选股主流程。

> **生产/测试机器人分流**：`daily_screen.yml` 按触发方式选 webhook——`schedule`（定时）用 `FEISHU_WEBHOOK_URL`，`workflow_dispatch`（手动）用 `FEISHU_WEBHOOK_URL_TEST`，避免白天反复测试的消息刷屏生产群。周度追踪与月度归因只有定时触发，统一用生产机器人。

### 4. 初始化数据库（可选）

程序首次连接时会自动执行 `CREATE TABLE IF NOT EXISTS`（幂等），**无需手动建表**。存量表会自动幂等迁移：推荐表补齐 `signals_hit` / `strategy` 等新列，并把旧唯一键 `(rec_date, code)` 升级为 `(rec_date, code, strategy)`（历史行 `strategy` 回填默认值 `'bottom_fishing'`）；追踪表由 `_ensure_tracking_schema` 补 `holding_trade_days` / `legacy_duplicate` / `unique_close_date` / `weekly_return_pct` / `monthly_return_pct` 列，旧 `(rec_id, week_no)` 唯一索引降为普通索引，改为 `(rec_id, holding_trade_days)` 与生成列 `(rec_id, unique_close_date)` 双唯一键（迁移用数据库锁串行化，历史重复行仅标记、不删除）。**不要对存在历史重复的旧追踪表直接加 `(rec_id, close_date)` UNIQUE**。如需手动建库，用仓库内的 `schema.sql`：

```sql
CREATE DATABASE IF NOT EXISTS stock_fishing DEFAULT CHARSET utf8mb4;
USE stock_fishing;
SOURCE schema.sql;
```

### 5. 启用 GitHub Actions

1. 进入仓库的 **Actions** 页面
2. 如果有提示，点击 **I understand my workflows, go ahead and enable them**
3. 四个工作流将按上表时间自动执行

### 6. 手动测试

1. 进入 **Actions** 页面
2. 左侧选择 **Daily Stock Screening** → **Run workflow**（一次运行会依次执行优质低估低位与放量突破两套策略）
3. 只想验证突破的信号质量、暂不写库时，勾选 `breakout_no_save`；只想跑优质低估低位时勾选 `skip_breakout`
4. 等待执行完成，检查飞书是否收到通知

## 项目结构

```
├── .github/workflows/
│   ├── daily_screen.yml              # 每日选股（交易日 21:00，单 job 顺序跑优质低估低位 + 放量突破，共享 cache/）
│   ├── daily_screen_longterm.yml     # 长期策略选股（交易日 21:30，独立缓存 key longterm-baostock-cache-）
│   ├── weekly_tracking.yml           # 周度追踪（周五 21:20）
│   └── monthly_attribution.yml       # 信号归因月报（每月 1 日 09:00）
├── src/
│   ├── screening_engine.py                # 选股引擎（quality_value 默认口径 + technical 口径 + 数据层 + 评分 + 共享筛选骨架）
│   ├── volume_breakout_strategy.py        # 策略二：放量突破（7层漏斗，继承引擎数据层/骨架）
│   ├── quality_longterm_strategy.py        # 策略三：长期优质低估低位（LongTermQualityConfig + evaluate_quality_longterm，经 evaluator 参数注入 _screen_quality_pool）
│   ├── fundamental_quality.py              # 年度质量核验（新浪原始接口优先，AkShare摘要fallback）
│   ├── financial_risk.py                   # 财务risk 独立证据（新浪原始资产负债表：负债率/商誉/扣非，同报告期同口径）
│   ├── recent_operating.py                 # 近期经营证据（新浪原始财务摘要 gjzb，六维同期比较，保留真实公告日）
│   ├── recommendation_factors.py           # 纯点时证据：周期行业分类 / 自身历史PE分位 / 近期底部结构 / 盈利正常化估值 / 技术状态分（零取数）
│   ├── market_data_integrity.py            # 已完成交易日与行情缓存元数据核验（与数据源无关，独立日历覆盖当日）
│   ├── volume_breakout_backtest.py         # 突破策略离线回测（本地 CSV，不联网；含闸门 A/B 与漏斗归因）
│   ├── screening_engine_akshare.py       # 旧版备份（AkShare 数据源，未被引用）
│   └── screening_engine_old.py            # 更早期版本备份（未被引用）
├── store/
│   └── mysql_store.py                # MySQL 持久化：推荐落库 / 固定期限追踪 / 归因查询（含幂等迁移）
├── notify/
│   └── feishu.py                     # 飞书通知：选股结果（按策略前缀区分卡片）/ 周度追踪（按策略分列）/ 归因月报
├── cache/                            # 自动生成的磁盘缓存（已 gitignore）
│   ├── daily_*/weekly_*/index_daily_*  # 个股与指数行情（meta阶段及交易日历核验）
│   ├── stock_list*.csv               # 股票列表（按 CACHE_TTL_DAYS=6 天失效）
│   ├── fund_v2_*                     # 基本面（按 FUND_CACHE_TTL_DAYS=7 天失效；主源名带起始季度标签）
│   ├── annual_quality_v3_*           # 年度质量（按代码/决策日/年数隔离；v3=公告日假定标记入版本号）
│   ├── growth_v1_*                   # 成长数据 query_growth_data（按代码/季度隔离）
│   ├── industry_bs.json              # 行业分类（按 INDUSTRY_CACHE_TTL_DAYS=30 天失效）
│   └── market_regime_state.json      # regime 滞回状态（超 10 天自动重置）
├── data/                             # artifact 上传目录（预留，当前不写入文件）
├── run.py                            # 每日选股入口（优质低估低位 quality_value；止跌确认闸门已是库级默认且全环境生效）
├── run_breakout.py                   # 放量突破入口（强制 technical；含组合层去重/总量上限/暴露提示）
├── run_longterm.py                   # 长期优质低估低位入口（LongTermQualityConfig + evaluate_quality_longterm；--compare 可对照生产口径）
├── run_weekly_tracking.py            # 固定期限追踪入口（5/10/15/20/40/60 交易日）
├── run_monthly_attribution.py        # 信号归因月报入口（总体口径固定 20 交易日）
├── backtest.py                       # 历史回测：逐日重放并复用实盘纯函数避免未来函数（prefetch/run/all/ab；ab 可切 technical 对照）
├── docs/                             # 策略设计文档
│   ├── volume_breakout_strategy.md   # 放量突破策略设计说明（分层漏斗 + 独立回测 + 参数速查）
│   ├── quality_value_recommendations.md  # 优质低估低位荐股资格/排序/分层与验证清单
│   ├── longterm_quality_strategy.md     # 长期优质低估低位策略设计（与生产口径差异表 + 2026-10-13 口径修订说明）
│   ├── recent_operating_contract.md      # 真实公告经营证据与同期/辅助趋势契约
│   ├── recommendation_evidence_upgrade.md # 本次默认与证据/持久化边界
│   ├── minimal_repairs.md            # 荐股逻辑最小修复（核验/成交模拟/追踪与归因口径）
│   ├── strategy_evaluation_2026-10-07.md # 「宁缺毋滥」三项修复的前瞻实测与口径翻转记录
│   ├── GitHub_Actions执行方案.md         # 长期策略独立/合并执行方案说明
│   ├── 导入错误修复记录.md               # GitHub Actions 导入错误修复记录（历史）
│   ├── 改进总结.md                       # 长期策略改进总结（历史）
│   └── TOP5改进说明.md               # 荐股质量分层/底背离修正等改进落地记录
├── audit_*.py / diag_*.py            # 离线审计与诊断脚本（漏斗归因、闸门健康度、待核验原因、
│                                     #   预测力、排名分档、阈值扫描、生产漏斗复现、fixture 校准等）
├── tests/                            # unittest 测试（T+1 成交模拟 / 核验离线 / 持久化 / 追踪幂等 / 证据升级）
│   ├── test_backtest_execution.py
│   ├── test_breakout_verification_offline.py
│   ├── test_quality_backtest_persistence.py
│   ├── test_tracking_idempotency.py
│   ├── test_recommendation_evidence_upgrades.py
│   ├── test_financial_evidence_upgrades.py
│   ├── test_financial_source_compatibility.py  # 新浪原始接口与AkShare摘要的字段/口径兼容
│   ├── test_recent_operating.py
│   ├── test_strategy_revision.py
│   ├── test_breakout_upgrades.py
│   ├── test_operating_upgrades.py
│   ├── test_recommendation_display_upgrades.py
│   ├── test_longterm_quality.py            # 长期 sleeve 评估函数单测（ROE稳定性/盈利持续/现金流/资质评分）
│   ├── test_longterm_pipeline_offline.py   # 长期 sleeve 离线端到端回归（全打桩，锁定构造/两趟评估/收紧断言）
│   ├── test_stab_cooldown_20261013.py      # 2026-10-13 两项收紧回归（medium须站上MA20 + 重复推荐冷却）
│   ├── test_mysql_text_columns.py          # 文本列 TEXT 化回归（DDL/幂等MODIFY/截断兜底/预探测顺序）
│   └── test_bottom_diagnostics.py     # 待核验/淘汰归因诊断日志
├── test_entry_filters.py             # 入场质量过滤器单测（合成数据，不联网）
├── test_main_layering.py             # main() 编排分层回归测试（打桩数据源，不联网）
├── test_strategy_fixes.py            # 2026-09 双策略评审修复项回归测试（合成数据，不联网）
├── test_optimizations_p0.py          # P0 优化项验证（pct_chg 口径统一 / 停牌缺口 / 排序质量分 / 数量上限与熔断）
├── test_momentum_gates.py            # KDJ/MACD 下限闸门回归（纯函数边界 / 两策略接线 / MACD 不可达扫描断言）
├── test_recommendation_upgrades.py   # 荐股升级项回归（综合分下限 / 维度短板门槛 / 前瞻确认 / 行业相对估值 / 突破独立 / 市场级刹车）
├── test_quality_value_hardening.py   # quality_value 口径加固回归（止跌全环境/净利同比边界/PE·PB 职责/估值评分锚点/相对强度/停牌缺口/横盘筑底加分）
├── test_strictness_fixes.py            # 「宁缺毋滥」三项修复回归（V值高位保护只做减法/止损下限/技术分连续化）
├── test_stabilization_p0_p1.py        # 止跌闸门与前瞻优化项回归
├── test_score_weight_p4.py            # 评分权重套件校验（含 P4 预注册组合的无解保护）
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

> `--no-save` 的存在理由：`stock_recommendation` 以 `(rec_date, code, strategy)` 为唯一键，重复写入时**未超过 30 天的已有记录会被跳过**（超过 30 天才删除重入），因此**不会清掉当天旧行**——白天测试写进去的推荐会与晚间正式结果并存在同一天，污染周度追踪与归因，并影响晚间突破策略的同股去重。

### 放量突破选股

```bash
python run_breakout.py                # 选股 → 组合层去重/总量上限 → 落库 → 通知
python run_breakout.py --no-cache     # 强制重拉
```

### 长期优质低估低位选股

```bash
python run_longterm.py                # 选股 → 落库（strategy=longterm_quality）→ 通知
python run_longterm.py --no-cache     # 强制重拉
python run_longterm.py --compare      # 同时跑生产 quality_value 口径并打印差异（不落库不推送）
```

### 固定期限追踪（周任务）

```bash
python run_weekly_tracking.py            # 需已配置 MySQL，否则直接退出
python run_weekly_tracking.py --no-cache
```

按沪深300交易日定位推荐后第 5/10/15/20/40/60 个市场交易日并逐期限观测（115 自然日内可补跑，停牌/缺价不前填）。

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

收益模拟为「信号日收盘生成、次日开盘买入」，持有期内用盘中高低价模拟触发止盈/止损（默认 +30% / −15% 固定或 3×ATR 动态；同根 K 线两者都触及时按止损保守处理）。需联网预取数据；`src/volume_breakout_backtest.py` 则是突破策略的离线事件回测（本地 CSV，不联网）。

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

变体定义见 `backtest.py` 的 `AB_VARIANTS`：

- **`quality_value`（生产默认）**：`baseline` / `qv_veto` / `qv_veto_d1` / `qv_veto_d3`（后三者回答「要不要在生产默认模式接入 KDJ/MACD 否决」）；`p0_risk_only` / `p0_watch_pass` / `p0_off`（止跌闸门判据三档对照，量化闸门本身的净贡献）；`p1_pos5` / `p1_pos10`（250日分位进排序）；`p2_q60`（质量硬门上调）；`p3_tech10`（技术降权、差额还给质量/估值）；`p4_*`（估值/技术权重再分配套件）。
- **`technical`（对照）**：`baseline` / `no_kdj` / `kdj_k70` / `macd_d1`（回答「闸门调松的边际影响」）。

两条路径的差异本身就是结论来源：`technical` 下 KDJ/MACD 是硬闸门、可直接调松调严；`quality_value` 默认「技术面不作否决」，闸门需显式开启 `QV_ENFORCE_KDJ_MACD_VETO`。报告写入 `backtest_output/ab_report.md`。

> **`p3_tech10` 的坑**：单纯下调技术权重会使综合分整体下移，`MIN_QV_SCORE` 变成更严的门槛——测到的其实是「降分」而非「换权重」。必须把差额同时还给质量/估值，保持三者和为 1。P4 套件同理（`_validate_score_weights` 会硬校验权重和）。

> **⚠️ 覆盖范围边界**：`backtest.py ab` 的两个模式都走 **screening_engine** 的 `evaluate()`（见 `_screen_day`），因此它**无法**验证放量突破策略层 3.8 的两个闸门。突破策略的闸门 A/B 与漏斗归因请用 `src/volume_breakout_backtest.py`（下节）。

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
python src/screening_engine.py screen

# 突破策略：仅选股，打印明细
python src/volume_breakout_strategy.py screen
```

### 离线审计与诊断脚本

`audit_*.py`（9 个）/ `diag_*.py`（10 个），**全部只读、不接入生产流程、不改产线口径**：用项目真实函数 + 本地缓存面板（`cache/`、`backtest_cache/`）复现漏斗、检验前瞻收益、扫描阈值。它们的用途是**为「策略评价」提供可复现的量化证据，而不是引用文档宣称**；结论落地前必须按「改产线口径前先 A/B」的约定走一遍对照。

```bash
python diag_real_recs.py        # 真实落库推荐逐条追踪（需把推荐记录同步进脚本内 RECS 表）
python diag_stab_ma20_ab.py     # 对同一批真实推荐做新旧口径 A/B
python diag_longterm_e2e.py     # 长期 sleeve 与生产 sleeve 端到端对照（离线）
```

| 脚本 | 回答的问题 |
|---|---|
| `diag_real_recs.py` | **真实落库推荐到底赚没赚**：把已落库的推荐逐条对本地行情算 F1/F3/F5/F10、窗口最低价、止损止盈触发，并按位置标签/技术分/市场环境分组。产物 `audit_real_recs_out.csv` 可直接回答「今天为什么没推 X」 |
| `diag_stab_ma20_ab.py` | 对**同一批真实推荐**重放 `evaluate`，两口径只差一个开关，验证收紧的净效应与「只减不增」 |
| `diag_longterm_e2e.py` | 长期 sleeve 与生产 sleeve **端到端对照**（离线，全打桩）：确认长期口径可构造、可评估、收紧项真实生效 |
| `audit_daily_output.py` | 多决策日实测：实际每天产出几只？瓶颈在哪一层？ |
| `audit_eval_funnel.py` | 各闸门在真实数据上的通过率与相互关系 |
| `audit_gate_health.py` | 哪些闸门是真起作用、哪些已成**死闸门**（阈值 vs 主板真实分布） |
| `audit_live_trading.py` | 实盘口径三问：信号可产出性 / 定位自洽性（是否在追高）/ 执行参数可行性 |
| `audit_pending_reason.py` | 「PASS 但降级 pending」的确切缺项分布 |
| `audit_predictive_power.py` | 各闸门的前瞻收益检验（cross-sectional IC），多决策日 × 多窗口 |
| `audit_production_funnel.py` | 按**生产口径**（行业内分位）重算漏斗，给出可安全修复的空间 |
| `audit_rank_tiers.py` | 按 `rank_score` 取前 N 只的质量分层（为「每天 2~3 只」找依据） |
| `audit_threshold_scan.py` | 技术分门槛 30/35/38/40/42/45 逐档扫描及新增放行样本特征 |
| `diag_funnel.py` / `diag_stabilization.py` | 闸门淘汰分布；strict 口径内部死因结构拆解 |
| `diag_p0_modes.py` | strict / risk_only / disabled 三口径对比 + 前瞻收益 |
| `diag_p4_feasibility.py` / `diag_pe_value.py` | 权重再分配与 PE 区分度的真实分布模拟 |
| `diag_predictive.py` | 技术分/止跌闸门是否真能预测后续表现（按分组比未来收益） |
| `diag_fixture_calibrate.py` | 测试 fixture 校准 |

> ⚠️ **单决策日样本的教训**：`diag_predictive.py` 曾因前瞻收益索引 bug 得出完全无效的因子相关性结论（提交 `22bd3fd` 修正）。项目内早期 ρ 结论多来自 n=168、单一决策日（2026-06-04）且处于全市场下跌区间，**不可直接外推**；判断权重或阈值影响必须用上述多决策日脚本重算。

### 运行单元测试

以下为离线回归命令。合成数据/mock验证规则行为，不发送通知、不连接数据库；旧规则测试显式隔离兼容配置。最终通过数量以完成后的日志为准。

> **必须离线**：调用 `main()` / `_screen_quality_pool` 等入口时，须把 `get_daily_data`、`get_fundamentals`、`enrich_annual_fundamentals`、`enrich_forward_growth`、`get_stock_list`、`get_stock_industry`、`build_industry_valuation_snapshot`、`_bs_login` 全部打桩，否则会真联网（BaoStock 会打印 "login success!"）。假 `evaluate` 的签名要与真函数同步（含 `index_df` 关键字），否则 `_screen_*` 内的异常会被吞成 `ERROR` 归因、测试通过但结论无效。

```bash
python -B -m unittest discover -s tests -p test_recommendation_evidence_upgrades.py -v
python -B -m unittest discover -s tests -p test_financial_evidence_upgrades.py -v
python -B -m unittest discover -s tests -p test_financial_source_compatibility.py -v
python -B -m unittest discover -s tests -p test_recent_operating.py -v
python -B -m unittest discover -s tests -p test_breakout_upgrades.py -v
python -B -m unittest discover -s tests -p test_operating_upgrades.py -v
python -B -m unittest discover -s tests -p test_recommendation_display_upgrades.py -v
python -B -m unittest discover -s tests -p test_bottom_diagnostics.py -v
python -B -m unittest discover -s tests -p test_strategy_revision.py -v
python -B -m unittest discover -s tests -p test_breakout_verification_offline.py -v
python -B -m unittest test_fundamental_quality test_quality_recommendations test_quality_value_hardening test_recommendation_upgrades test_minimal_quality_repairs -q
python -B test_entry_filters.py
python -B test_main_layering.py
python -B test_strategy_fixes.py
python -B test_optimizations_p0.py
python -B test_momentum_gates.py
python -B test_score_weight_p4.py
python -B test_stabilization_p0_p1.py
python -B test_strictness_fixes.py
python -B test_backtest_ab.py
python -B test_breakout_ab.py
python -B tests/test_stab_cooldown_20261013.py
python -B tests/test_mysql_text_columns.py
python -B tests/test_longterm_quality.py
python -B tests/test_longterm_pipeline_offline.py
python -B -m unittest tests.test_backtest_execution tests.test_quality_backtest_persistence tests.test_tracking_idempotency
```

> `tests/test_stab_cooldown_20261013.py`（18 项）锁定 medium 须站上 MA20 与重复推荐冷却，其 A/B 部分**读 `cache/daily_*_qfq.csv` 真实行情**——合成 fixture 复现不出「MACD改善 + 无新低 + 仍在 MA20 下方」这个三条件交集，收紧类验证要上真实行情。
> `tests/test_mysql_text_columns.py`（24 项）全离线不连库，锁定 DDL 为 `TEXT`、幂等 `MODIFY`、截断兜底行为，以及「容量探测必须早于构建 rows」这个顺序不变量。
> `tests/test_longterm_pipeline_offline.py` 全离线（baostock / akshare / MySQL / 飞书 / requests 全部打桩，桩被误调用会直接抛错），锁定长期 sleeve 的三个曾致命问题不再复发：导入期 ImportError、`LongTermQualityConfig` 构造即 ValueError、年度财务闸门在预筛趟（`fund_data=None`）执行导致恒 0 推荐；同时以断言守护「长期口径相对生产全部是收紧」。端到端对照另可跑 `python diag_longterm_e2e.py`。

## 数据持久化

两张表，首次连接时自动创建（`CREATE TABLE IF NOT EXISTS`，幂等）。推荐写入使用固定列白名单；本次新增risk、历史PE、正常化PE、近期底部、突破分类/锚点/子分/RS等字段并未全部保存，结果对象不等于完整数据库审计记录：

| 表 | 写入方 | 说明 |
|----|--------|------|
| `stock_recommendation` | `run.py` / `run_breakout.py` / `run_longterm.py` | 每日推荐明细，`(rec_date, code, strategy)` 唯一（`uk_rec_date_code_strategy`）；重复写入时已有记录**入库未超 30 天则跳过、超过 30 天则删除旧行重新入库**（支持过期数据刷新，替代旧 `INSERT IGNORE`）。`save_recommendations` 只接受已注册 strategy（`bottom_fishing` / `volume_breakout` / `longterm_quality`） |
| `stock_tracking` | `run_weekly_tracking.py`（每周） | 固定期限表现追踪，`(rec_id, holding_trade_days)` 唯一（`uk_rec_horizon`）+ `(rec_id, unique_close_date)` 唯一（`uk_rec_close_date`，生成列，历史重复行映射 NULL 后不参与约束）；`week_no` 原唯一索引已降为普通索引。除累计收益率 `return_pct` 外，另存**期收益率** `weekly_return_pct` / `monthly_return_pct`（见下文「期收益率」） |

**strategy 来源字段**：`stock_recommendation.strategy` 区分 `bottom_fishing`（优质低估低位，含 technical 抄底对照口径）/ `volume_breakout`（放量突破）/ `longterm_quality`（长期优质低估低位，恰好 16 字符 = `VARCHAR(16)` 上限）。`save_recommendations` 只接受这三个**已注册值**（`KNOWN_STRATEGIES`），传入未注册策略直接抛错——避免第三拼写变体把推荐写进错误的 sleeve、造成同日同股覆盖与归因混算。同一股票同日可被多套策略分别推荐并存（`run.py` 与 `run_breakout.py` 之间组合层去重默认已拦截，唯一键兜底；长期 sleeve 为独立工作流，不参与合计上限 `DAILY_TOTAL_MAX_PICKS=7`）；**周度追踪**按来源分列统计（飞书卡片分列 + 逐只 `[优质低估低位]`/`[放量突破]`/`[长期优质低估低位]` 标签）；**信号归因**按 `strategy × holding_trade_days` 分列统计持有期效应，并额外给出 `by_strategy` 分组。存量表由程序自动幂等迁移：推荐表补 `signals_hit` / `strategy` 等新列并把旧唯一键 `(rec_date, code)` 升级为 `(rec_date, code, strategy)`（历史行回填 `'bottom_fishing'`）；追踪表由 `_ensure_tracking_schema` 补 `holding_trade_days` / `legacy_duplicate` / `unique_close_date` / `weekly_return_pct` / `monthly_return_pct`，旧行保留原周号、日期与价格，同 `rec_id + close_date` 仅最早一行 `legacy_duplicate=0`，其余标 1（迁移用数据库锁串行化）。

**文本列超长修复（2026-10-13）**：生产曾报 `(1406, "Data too long for column 'signals_hit' at row 1")`。根因是荐股证据链持续变长（历史PE分位 + 行业分位 + 近期有效底部 + 六项经营证据明细，实测单条 112~225 字符且仍在增长），而 `signals_hit` / `missing_tags` 原为 `VARCHAR(255)`。**关键是 MySQL 1406 会让整批 INSERT 失败——任一条超长，当日全部推荐都落不了库**（全有全无，不是单条丢失）。因此三重修复：新建表 DDL 直接用 `TEXT`；存量表由 `_TEXT_COLUMN_UPGRADES` 幂等 `MODIFY` 到 `TEXT`（非破坏，不动数据与索引）；若 ALTER 因账号权限失败，降级为按 information_schema 探测到的**真实列容量**截断（`_clip_to_capacity`，保留尾部 `...` 并 warning），保证「宁可不完整也要落库」。探测与容量缓存必须在构建 `rows` **之前**完成（`_ensure_schema_cached`），否则截断静默失效、1406 原样复现。

查询列实际类型：
```sql
SELECT COLUMN_NAME, COLUMN_TYPE, CHARACTER_MAXIMUM_LENGTH
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stock_recommendation'
  AND COLUMN_NAME IN ('signals_hit', 'missing_tags');
```

**追踪口径**：以沪深300行情日期定位**推荐后第 5 / 10 / 15 / 20 / 40 / 60 个市场交易日**（`TRACK_HORIZONS`，2026-10-13 执行中期化新增 40/60——生产建议持有窗口已放到 40~60 个交易日，没有 40/60 观测则新窗口的收益无法被度量），每个期限各观测一次；允许 115 自然日内补跑（`TRACK_MAX_AGE_DAYS`，覆盖 60 个交易日≈101 日历天 + 长假余量）。**目标日停牌或无有效报价则跳过，不以前后日期前填、也不替换为最新价**。

**期收益率（`weekly_return_pct` / `monthly_return_pct`）**：`return_pct` 是**相对推荐日**的累计收益率；这两列是 `save_tracking` 落库时顺带计算的**环比期收益率**——周收益率 =（本次观测收盘价 − 同推荐 **5 个交易日前**那次追踪观测的收盘价）/ 该收盘价 × 100%，月收益率同理对 **20 个交易日前**的观测。基准价只取同推荐**已落库**的历史观测行（`legacy_duplicate=0`），对应历史观测不存在即为 NULL、不猜测：第 5 日观测无第 0 日追踪行，周收益率为 NULL。2026-10-13 新增 40/60 期限后：第 40 日观测的月收益率基准是第 20 日观测（存在 ⇒ 可算），第 60 日观测的月收益率基准是第 40 日观测（存在 ⇒ 可算）——这两列的「恒为 NULL」状态随期限表扩展自然解除；周收益率对 40/60 仍为 NULL（35/55 日前无观测）。两列仅落库并由 `get_attribution_rows` 返回，飞书卡片不渲染。

**收益率分母（复权基准对齐）**：`close` 统一取**前复权**（`ADJUST="qfq"`，以最新交易日为锚点），因此追踪期内一旦除权除息，历史价格会被整体重算。收益率因此固定为 **（目标日收盘价 − 同一次拉取序列里的推荐日收盘价）/ 同序列推荐日收盘价 × 100%**：分母**不能**用落库的「推荐日当时」价格——那会把 10 送 10 的含权收益（本应 0%）算成 −50%。基准分叉时日志逐条留痕（`复权基准已变`），卡片上的「推荐价/现价」也统一为同一前复权序列，用户可自行核算。

同一推荐同一收盘日、同一期限双重幂等；`holding_trade_days` 为 NULL 的历史行不参与固定期限归因（其中未知期限不猜测、不混入新口径）。同一股票在不同日期被重复推荐，视为不同记录、各自独立追踪。

**统计单位**：追踪报告的一行 = 一次「推荐 × 固定期限」观测，**不是一只股票**——同一只股票会因 5/10/15/20/40/60 各到期一次而出现多行。周报卡片因此按**「条观测」**计数，并把胜率/平均收益标注为**混合口径**（不同成熟度直接平均只能当概览）并**按期限分列**（同一期限才可比）。

**信号归因**：`run_monthly_attribution.py` 基于两表历史数据统计各信号维度（等级 / 评分分档 / 底背离 / 市场环境 / 策略 × 持有期限 / 策略）的胜率与收益，用于用真实运行数据反哺信号质量评估。**总体口径暂只取「推荐后 20 交易日」观测**（与历史月报可比；生产持有窗口 2026-10-13 起为 40~60 个交易日，待 40/60 观测积累 2~3 个月后应把总体口径改到 60 并同步卡片文案），**未满 20 交易日的条数在卡片上显式报出**（否则用户会以为「本期只有这么点推荐」）；持有期限维度按 5/10/15/20/40/60 交易日分别统计——**「20 日收割 vs 40/60 日收割」的收益差就在这里直接可见**，是执行中期化效果的度量口径。**平均峰值 = 该推荐在全部固定期限观测中的最高收益率**（不是每日最大浮盈、也不是区间最高价）；按期限分组的那一列是**组内极值**，卡片以「最高收益」显式标注，避免被读成均值。`market_env` / `grade` 缺失的推荐归入「未知」档而不是被 groupby 静默丢弃，**各分组样本数之和恒等于总数**。**这不是回测**——只统计已落库的真实推荐与真实追踪收益，因此样本需要时间积累。

> **只有 `rec_tier='formal'` 的正式推荐会落库**；待核验候选（数据缺失）不写正式表、不参与固定期限追踪，避免污染归因样本。2026-10-19 起 pending 另写**影子账本**（`cache/shadow_recommendations.jsonl` + `data/shadow/`，JSONL，不是 MySQL），只供 `run_shadow_tracking.py` 做前瞻诊断，不通知、不进入正式追踪/归因。

## 通知样例

推送到飞书群的消息卡片分三类。**卡片标题按「本次实际生效的口径名」取名**（`【优质低估低位】` / `【放量突破】` / `【长期优质低估低位】` / technical 对照口径显示 `【低位企稳】`），不再出现【抄底策略】优质低估低位荐股 这类自相矛盾的组合——`bottom_fishing` 这个落库值覆盖 quality_value 与 technical 两种口径，标题只用数据推断会误标，故由调用方显式传 `recommendation_mode`；长期 sleeve 的口径名另由 `LongTermQualityConfig.LONGTERM_LABEL` 承载（与 `RECOMMENDATION_MODE` 解耦：前者决定怎么称呼，后者决定走哪条资格）；**每策略最多展示 Top-5**（`NOTIFY_TOP_PER_STRATEGY=5`，与策略层牛市上限 `MAX_PICKS` 对齐 ⇒ 策略层放几只就展示几只，「落库集合 == 可见集合」，不会出现已落库却看不到的票）；**pending 不渲染**（`pending` 参数仅为兼容旧调用签名而保留）。

> **卡片上的都是「正式推荐」，不是「候选」**：`notify_screening_result` 在渲染前会按 `tier` 过滤掉全部非 `formal` 标的，因此卡片里出现的每一只都满足正式层级；是否落库及参与追踪仍取决于MySQL配置与 `--no-save`。单票标签原写作「XX候选」，因中文里「候选」天然偏「备选／仅供参考」而与实际含义相反，现统一改为「**正式推荐 · XX口径**」；同时每只票都渲染**操作计划**块（建仓区间 / 止损 / 止盈 / 建议持有周期，见 `describe_trade_plan`），三条 sleeve 共用同一格式（长期口径的持有周期读信号行内的 `holding_days_hint_min/max`，即 30~90 天）。

**两种「容易误读」的情形都会显式声明**：

- **数据源降级**：零推荐须区分市场无合格信号与数据覆盖不足。行情/估值仍可能因主源失败而缺失；近期经营由独立新浪接口取数，失败或字段不足时明确待核验，不能据此声称市场没有机会。
- **估值口径回退**：策略层把实际口径逐行写入 `valuation_mode` 列（`_tag_valuation_mode`），`run.py` 汇总后透传；为 `absolute`/`mixed` 时卡片声明「**本次为绝对阈值回退 / 混合口径**」并提示不可与行业口径结果直接比较（绝对口径系统性偏向低 PE/PB 的银行、地产、周期）。

| 卡片 | 触发 | 内容 |
|------|------|------|
| 选股结果 | 每日选股后（三个入口各自独立卡片，标题按实际口径名取名，单票格式按策略区分） | 市场环境、正式推荐数量（均为**正式推荐** · 优质低估低位／放量突破／长期优质低估低位）+ **名单口径说明**、每只股票的代码/名称/「正式推荐 · 口径」标签/评分/等级/收盘价/入选依据（优质低估低位含行业估值分位、当年净利同比；突破含级别与幅度）+ **操作计划**（建仓区间＝信号日收盘价 ×(1 ± `ENTRY_BAND_PCT`=1.5%)，次日开盘执行；止损；止盈；盈亏比；建议持有 `HOLD_DAYS_HINT_MIN~MAX`＝40~60 个交易日（2026-10-13 执行中期化，原 10~20；长期口径 30~90、突破 10~20，均读信号行内字段））+ 核验状态；quality_value 卡片另附**决策简报**（信心/今日触发/看多/风险/失效价）；**只发通过全部筛选闸门的正式推荐**——波动率否决（观察池）等未通过筛选的对象一律不上卡片，仅在 CI 日志留档；无正式推荐时提示「今日无信号」，数据源降级时标题改为「数据源不足，本次无正式推荐」，执行异常时推送错误信息 |
| 周度追踪 | 每周追踪后 | 追踪**观测条数**（一行=一次「推荐×期限」观测，非一只股票；跨 5/10/15/20/40/60 交易日）、**混合口径**胜率/平均收益 + **按期限分列**（同一期限才可比）+ **按策略分列**（优质低估低位/放量突破/长期优质低估低位各自条数/胜率/平均收益）及逐只明细（带 [优质低估低位]/[放量突破]/[长期优质低估低位] 与期限标签） |
| 信号归因月报 | 每月归因后 | 统计周期（含「推荐后20交易日」口径说明）、已统计推荐数（**仅含已满 20 交易日**）与未计入条数、整体胜率、平均收益/平均峰值收益，以及按等级/评分分档/底背离/市场环境/策略×持有期限的分组统计（缺失维度归入「未知」档，分组样本数之和等于总数） |

## 指标速查（当前默认与技术对照分开）

未标quality_value或突破的技术抄底项仅供technical低位企稳对照，不是当前质量入口资格。

| 指标 | 环节 | 用途 |
|------|------|------|
| MA5/EMA5/10/20 | 日线 | 趋势转折（拐头/金叉，同源信号合并计分） |
| MA20 斜率 | 日线 | 下降通道过滤（陡峭下降中趋势转折不认可，防接飞刀）；突破策略要求 MA20 上行 |
| 当日涨幅/跳空幅度 | 日线 | 防追高否决（technical：涨幅 >5% / 跳空 >2%；突破：涨幅 >7% 或 <2% / 跳空 >2%） |
| 近5日累计涨幅 | 日线 | 已反弹一段否决（累计 >12%，比 RSI 更灵敏） |
| RSI14 上限 / 量比上限 | 日线 | 已反弹一段否决（RSI>60）/ 天量出货否决（量比>4）；突破策略检查**前一日** RSI ≤80 |
| 60日高点回撤（下限/上限） | 日线 | 底部区域过滤（回撤 <10% 判上涨中继；>70% 判崩盘型/价值陷阱） |
| 20日区间位置 / MACD柱 / KDJ | 日线 | 严格确认：区间下半部 + MACD柱连续 `MACD_MOMENTUM_DAYS`=3 日改善 + KDJ金叉、K≤55 且 K 上行 |
| 收盘位置/实体占比/上影线占比 | 日线（评分） | 量价组**档内连续化**（`DAILY_SCORING_CONTINUOUS=True` 默认开启）：替换三档离散值，组上限仍 30 分、门槛布尔条件不变，只细分同档内差异 |
| MACD 柱深度弱势 / KDJ 高位 | 日线 | **荐股控制闸门**（`FAIL_MACD_WEAK` / `FAIL_KDJ_HIGH`）：柱值/收盘 ≤−0.5% 且连续 2 日递减（双条件 AND）／K >85 或 K ≥80 且 K<D。突破策略默认开启（`REQUIRE_BR_MACD_NOT_WEAK` / `REQUIRE_BR_KDJ_NOT_HIGH`）；quality_value 模式由 `QV_ENFORCE_KDJ_MACD_VETO` 控制（默认关闭额外否决；strict止跌、状态技术40与近期底部仍独立生效） |
| MACD 柱连续改善 + 站上 MA20 | 日线 | **止跌确认闸门**（`FAIL_STABILIZATION`，`QV_STABILIZATION_GATE` **库级默认开启**）：**全市场环境**按strict强/中/弱判定；**medium 必须站上 MA20**（`QV_STABILIZATION_MEDIUM_REQUIRES_MA20=True`，2026-10-13）——未站上 MA20 的左侧通道降为 weak→pending；弱确认pending，无确认否决，数据不足不视为确认。旧名 `QV_BEAR_TIMING_GATE` 自动迁移（含构造后赋值） |
| 近 60 个交易日个股 vs 沪深300 区间涨跌幅差 | 日线 + 指数 | 相对强度（`relative_strength`，百分点）：生产 quality_value 仅作正式候选间**有界排序微调**（±`RS_WEIGHT`=3）与跑输风险提示（≤−10pp 打标签），无硬性准入线；**长期 sleeve 升级为硬门槛**（`REQUIRE_RELATIVE_STRENGTH`，近 60 日跑输超过 15pp → `FAIL_RELATIVE_STRENGTH_WEAK`；指数数据不可计算时不否决）；按共有交易日对齐，指数缺失为 None（排序中性） |
| 现价 vs MA20/MA60 + 距 250 日低点天数与幅度 | 日线（展示） | P0 择时读数：右侧·站上MA20/MA60 / 右侧雏形 / 左侧·MA20下方 / ⚠左侧·仍在下跌（MACD深度走弱）；**标签本身不参与否决与排序**，但 2026-10-13 起「左侧·MA20下方」的票已无法拿到 medium，因而不再进入正式推荐（实测该组 15 只 F5 全部下跌、中位 −3.67%、胜率 0%，同期沪深300 上涨） |
| 近60日收盘低点年龄 + 距底涨幅 vs 5×ATR | 日线 | **高位保护**（`FAIL_OVEREXTENDED`）：与止跌闸门正交——止跌管「有没有止跌」，本项管「止跌后是否已涨离底部」。低点年龄须 5～40 交易日，距底涨幅上限 `clip(5×ATR/现价, 10%, 20%)`；未确认→pending，超限→否决。只做减法，放行集合 ⊆ 原集合。长期 sleeve 放宽为年龄 3～90 日、窗口 120 日、上限 `clip(8×ATR/现价, 10%, 20%)` |
| 落库推荐历史（同 strategy） | 库 | **重复推荐冷却**（`QV_REPEAT_COOLDOWN_DAYS`=10）：在 `_screen_quality_pool` 排序完成、最终分层**之后**执行，把冷却期内已落库推荐过的票从 `formal` 移入 `pending_out`（可见、不丢弃、不落库）。**不在闸门链内，不改任何闸门判定**；DB 未配置/查询失败时整体不生效，但筛选层每次显式打印是否生效（死闸门原则） |
| MA20 近 5 日斜率 | 日线（长期 sleeve） | 加速下跌否决（`DETECT_ACCELERATING_DECLINE`，MA20 近 5 日跌幅 >5% → `FAIL_ACCELERATING_DECLINE`）：长期策略唯一新增的技术面否决项，只拦「下跌仍在加速」的接飞刀形态，两趟评估都执行；数据缺失不否决 |
| ROE 变异系数 / 5 年现金流 / 盈利持续性 / 资质评分 | 年度财务（长期 sleeve） | 长期专项闸门（均在拿到年度财务的第二趟执行）：ROE 稳定性 CV≤0.5、5 年累计经营现金流/净利润≥1.0、5 条年报 4 次同比至少 3 次为正、资质评分≥60（ROE稳定25% + 现金流25% + 盈利持续20% + 增长质量15% + 估值边际15%） |
| ATR% | 日线 | 波动率风控（quality_value ≤6% / technical ≤6%——两路径共用 `MAX_ATR_PCT`；突破 ≤4.0% 独立阈值，超限否决 FAIL_VOLATILE；ATR 缺失为 FAIL_DATA。阈值 10% 曾是死闸门，超限占比 0.000%，已下调） |
| ATR 止损下限 | 日线（展示） | 止损 = `max(3×ATR, 固定15%)`（`compute_risk_reward`）：ATR 只在比固定止损更宽时接管，波动小不被正常波动震出；仅影响展示与落库，不参与准入/排序/否决 |
| 止盈目标 / 持有周期 | 日线（展示） | 生产（2026-10-13 中期化）：止盈固定 **+15%**、持有 **40~60** 个交易日（原 +30% / 10~20）；突破 = `min(+15%, +5×ATR)`、持有 10~20；长期 = +50%、持有 30~90（读信号行内 `holding_days_hint_min/max`）。**均为执行口径，不参与任何准入/排序/否决**；度量口径同步为周追踪第 5/10/15/20/40/60 个交易日 |
| 周线 MA10 / MACD | 周线 | 决赛圈确认（突破默认「站上MA10（容忍2%）**且**MA10上行」，`WEEKLY_MA_BOTH_REQUIRED=True`；低位technical对照可用OR；另 MACD柱翻红或绿柱连收2周；**只用已收盘周 bar**；数据缺失/滞后 → 待核验候选） |
| RSI14/7/21 | 日线 | 超卖反弹 + 底背离判断（前一个价格低点当根的 RSI 对比）+ 多周期共振 |
| 成交量比 + 收盘位置/实体/上影线 | 日线 | 量价质量分（放量企稳 25 / 放量上涨 18 / 放量冲高回落 10） |
| 三级阻力位（60日高/20日高/MA60） | 日线（突破） | 突破级别判定（L1/L2/L3；`REQUIRE_L1_OR_L2=True` 默认只认 L1/L2，L3 整体关闭） |
| 量比/成交额/量能分位/额比 | 日线（突破） | 放量四重确认（1.8–4.0 倍 + ≥1亿 + 60日80%分位 + ≥中位×1.5） |
| 平台振幅/收盘离散度 | 日线（突破） | 突破前整理充分度（振幅 ≤18%、std/mean ≤4%，shift(3) 避污染） |
| 最新K线日期 | 日线 | 数据时效（独立交易日历先核验应完成交易日与指数，再检查个股日期；不能以index自证index时效，滞后归因FAIL_STALE） |
| 相邻K线自然日间隔 | 日线 | 停牌缺口过滤（>12 天 → FAIL_HALT_GAP，滚动指标跨缺口失真）；**quality_value 与 technical 均已接入**，同一归因码 |
| ATR% / 回撤深度 / 20日区间位置 | 日线（排序） | 连续质量分（technical 路径的 `rank_score` 组成；仅决定同批通过者先后，不改准入；`RANK_QUALITY_WEIGHT`=10 可置 0） |
| 成交额(20日均值) | 日线 | 流动性过滤（<3000万 否决，独立归因 FAIL_LIQUIDITY） |
| 沪深300 MA20 斜率 + MA60 趋势 | 市场环境 | 牛/熊/中性 regime（`MARKET_REGIME_DUAL_INDICATOR` 默认开启：斜率与 MA60 趋势须同向，否则降级中性；另含连续 2 日确认的滞回机制） |
| 沪深300 近5日累计涨跌幅 | 市场环境 | 市场级熔断（≤ −8% → 本次运行不推荐；优质低估低位与长期策略入口均生效，长期入口在筛选后显式补齐该步） |
| 行业分类 | 集中度 | 行业分散（同一行业最多 2 只，避免单一板块押注） |
| PE 行业内分位 / 绝对 PE 上限 | 估值（口径） | 行业相对估值回退绝对 PE 上限时逐行标注 `valuation_mode`，飞书卡片显式声明本次口径（PB默认不单独评分或高值否决，但参与盈利能力proxy构组；PB≤0仍否决） |

## 数据源

**双数据源架构**：Baostock 为主、AkShare 为备，自动切换。

- **主源 Baostock**：`query_history_k_data_plus`（个股/指数日线，前复权）、`query_all_stock`（股票列表）、`query_stock_industry`（行业分类，用于行业分散与行业相对估值快照）、`query_profit_data` / `query_balance_data`（ROE/负债率）、`query_growth_data`（最新报告期净利润同比 YOYNI，用于前瞻确认）
- **备源 AkShare**：`stock_zh_a_hist`（个股日线，东财通道；不可达时自动切 `stock_zh_a_daily` 新浪通道）、`stock_zh_index_daily`（指数）、`stock_info_a_code_name`（股票列表）、`stock_financial_analysis_indicator` / `stock_balance_sheet_by_report_em` / `stock_profit_sheet_by_report_em`（基本面，**technical 与突破路径的决赛圈按字段补齐**商誉/扣非与主源缺失的核心项，只填 None 字段、不覆盖主源）
- **年度质量**：新浪原始接口优先（保留真实公告日），AkShare `stock_financial_abstract` 为摘要fallback；保留ROE、扣非、合并净利、经营现金流与归母利润。无公告日fallback显式标 `availability_assumed`（保守次年5月1日），不覆盖真实晚公告或原始硬伤。
- **近期经营与财务risk**：**新浪原始财务接口**（`quotes.sina.cn/.../getFinanceReport2022`，`gjzb` 摘要 / `fzb` 资产负债表）保留真实公告日、来源、报告期与合并人民币口径。摘要接口 `stock_financial_abstract` 会丢弃 `publish_date`，故只作 fallback 而非主源。缺商誉是**未知**、绝不按零处理；单季/TTM 为辅助证据，risk 不跨期或跨公告日拼接。免费接口不是历史财报修订版本库，不保证历史证据完整。
- **切换规则**：
  - 单条取数失败（异常或返回空）→ 自动用 AkShare 兜底重取
  - Baostock 连续失败达 8 次 → 熔断，本次运行后续请求直接走 AkShare（避免逐股无效重试）
  - Baostock 登录失败 → 直接降级 AkShare 跑完全程（AkShare 未安装则报错退出）
- **连接管理**：登录真实重试（检查 error_code）、查询失败自动重连、全局线程锁防止 C++ 底层 socket 多线程踩踏；模块级 `socket.setdefaulttimeout(20)` 防假死
- **失败重试**：请求异常按退避重试（个股 `MAX_RETRY=2`，股票列表 `LIST_MAX_RETRY=4`）；「返回空」视为该标的确实无数据，不重试
- **两级缓存**：内存 `CacheManager`（进程内，`CACHE_EXPIRE_HOURS=4` 小时）→ `cache/` 目录磁盘缓存
  - 行情类按meta取数日期/阶段核验，闭市不能复用盘中缓存；核验逻辑在 `market_data_integrity.py`（与数据源无关）：`quote_phase` 以北京时间 15:00 划分盘中/闭市，`latest_completed_day` **要求交易日历覆盖当天**（含非交易日）且无重复/无非法标记，否则返回 `None`（宁可不判，也不猜）。**绝不由文件 mtime 反推取数阶段**。
  - 股票列表按 `CACHE_TTL_DAYS=6` 天、基本面按 `FUND_CACHE_TTL_DAYS=7` 天（`fund_v2_*`；主源缓存名带起始季度标签，报告期滚动后自动失效，备用源为按 TTL 失效）、行业分类按 `INDUSTRY_CACHE_TTL_DAYS=30` 天失效；年度质量 `annual_quality_v3_<code>_<决策日>_<年数>.json`、成长 `growth_v1_<code>_<年>Q<季>.json`、近期经营 `recent_operating_v2_<code>_<决策日>_*.json`、财务risk `financial_risk_v2_<code>_<决策日>_<阈值组合>.json` 均按 7 天 TTL，且各自按决策日/季度/阈值隔离——**决策日进入缓存键**，避免「新披露的报告被更早那次运行的旧缓存挡住」
  - 磁盘只缓存未过滤的原始列表，过滤在返回时应用，改 config 即时生效无需清缓存
  - 两个数据源的缓存文件命名天然隔离（Baostock 带 `sh.` 前缀、AkShare 为纯 6 位数字），互不污染
- **股票池过滤**：`MAIN_BOARD_ONLY` 默认开启——白名单仅保留普通 A 股账户可直接交易的沪深主板（60/00 开头）；创业板（开户需 10 万资产）、科创板/北交所/港股通（需 50 万资产）等对个人资金有门槛的板块，以及港股/B 股等非 A 股证券全部排除。关闭该项则退回由 `FILTER_ST` / `EXCLUDE_DELISTING` / `EXCLUDE_BSE`（默认开启）与 `EXCLUDE_CHINEXT` / `EXCLUDE_STAR`（默认关闭）组合控制

## 免责声明

本策略仅为量化研究工具，不构成投资建议。策略优化旨在从逻辑上减少低质量信号，不保证提高未来收益率或胜率。实际效果必须通过严格的样本外回测验证。投资有风险，入市需谨慎。
