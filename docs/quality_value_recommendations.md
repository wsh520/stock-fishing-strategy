# 优质低估低位与突破荐股规则

先判断资格，再划分正式推荐与观察候选，最后排序。年度质量、近期经营、财务风险、估值与止跌各自提供证据，分数不能抵消硬伤。默认 `run.py` 使用 `quality_value`；`VolumeBreakoutConfig` 与 `run_breakout.py` 默认使用独立 `technical` 突破策略。规则分不是成功概率。资格证据的升级不修改交易执行；**交易计划与追踪口径属执行层，2026-10-13 按「执行中期化」单独调整（见下节），不改任何准入闸门**。

## 当前默认资格

| 维度 | 当前默认与处理 |
|---|---|
| 年度质量（非金融） | 连续3个可用完整年度，ROE中位数≥10%、每年ROE≥5%、每年扣非、合并净利润及经营现金流均为正；三年累计经营现金流/累计合并净利润≥0.8。缺失不能拿更旧年度顶替，季度年化ROE不能替代年度质量 |
| 近期经营 | `REQUIRE_RECENT_OPERATING=True`；最新已公告报告与去年同报告期比较收入、合并净利、扣非、经营现金流、毛利率及净利率。verified通过该维度，weak/missing留pending，failed否决 |
| 财务风险 | `REQUIRE_FINANCIAL_RISK=True`；同报告期、同真实公告日、同来源及合并人民币口径计算负债率≤70%、商誉/净资产≤20%、扣非/合并净利≥0.5。核心金额、公告日或来源缺失为pending；已知硬伤不能被缺项掩盖 |
| 相对估值 | 默认同行业 PE 分位≤60%，优先使用 PB/PE 盈利能力 proxy 在个股0.5～2倍区间的可比组，至少10个样本；组不足回退整个行业，行业PE样本仍不足回退 `0<PE TTM≤25`。明确记录实际口径 |
| PB职责 | PB≤0否决；默认高PB只作异常与风险说明，不直接否决、不单独进入估值评分。PB缺失不单独降级；`QV_VALUATION_DUAL_GUARD=True` 可显式恢复PB硬门。PB用于构造proxy可比组，可能间接改变PE分位 |
| 自身历史估值 | `REQUIRE_HISTORICAL_VALUATION=True`；当前PE对决策日前最多250根行情的正且有限PE样本取中秩分位，至少120个样本，分位≤0.60；超限否决，缺证据pending。当前日不计入历史样本 |
| 周期行业 | 按实际行业编码及名称分类；包括B06/B07/B08/B09/B10/B11、C25/C26/C30/C31/C32、G55。关闭相对估值仍分类，行业未知留pending。默认要求归母利润正常化PE≤25，缺证据pending |
| 中长期位置 | 至少250根有效日线，当前收盘在250日收盘序列中秩分位≤40%；同价按中间名次计，不把完全横盘误判成最低位 |
| 近期底部 | `QV_LOW_ANCHOR_MODE="recent_structure"`；近60日收盘最低点年龄5～40个交易日，距底上限为 `5×ATR/现价`，限制在10%～20%。底部未确认pending，涨离上限否决；250日盘中极值距离用于诊断，固定15%仅属legacy模式 |
| 止跌 | `QV_STABILIZATION_MODE="strict"`；强/中确认通过，弱确认pending，无改善否决；窗口缺失不能当确认。**medium 必须站上 MA20**（`QV_STABILIZATION_MEDIUM_REQUIRES_MA20=True`，2026-10-13）：原左侧通道（MACD改善+无新低）不再构成 medium，降级为 weak→pending |
| 重复推荐 | `QV_REPEAT_COOLDOWN_DAYS=10` + `QV_REPEAT_COOLDOWN_MODE`（2026-10-13 引入；**2026-10-26 改为跨 sleeve + 默认 `label`**）：`label` 命中后**照常推荐并落库**，只写 `repeat_count`/`prev_rec_date`/`prev_rec_close` 并在 `signals_hit` 追加「重复推荐(10日内第3次，上次10-23@27.95→本次27.56(-1.4%))」，落库 `repeat_seq`、旧行 `superseded_by` 打废弃标签；`block` 为旧行为（移入待核验，可见不丢弃、不推送不落库）；`off` 不判定。明细由入口脚本查库经 `store.fetch_recent_recommendation_detail(..., cross_sleeve=True)` 传入，`run.py` 与 `run_longterm.py` 共用同一份明细，策略层不碰 DB；未配置 DB 时不生效且会显式打印状态与生效动作。**`label` 是刻意放宽**（`block` 让票「消失」，既分不清是冷却还是闸门否决，也无法统计第 N 次推荐的表现），补偿是可见性 + `repeat_seq` 可统计，且**统计口径不变、旧行照常计入**。改为跨 sleeve 的实证：亚普股份 10-21/22/23 各被 `longterm_quality` 推 1 次、10-22 又被 `bottom_fishing` 推 1 次，同一只票 3 天 4 行 |
| 状态技术分 | `QV_TECHNICAL_STATE_SCORING=True`，趋势40+动能30+量价30，持续状态与最近5日衰减事件共同计分；技术分<40进入pending。旧事件日评分可显式关闭状态评分后对照 |
| 额外分数线 | `MIN_QV_SCORE=0`、`MIN_QUALITY_SCORE=0`，默认关闭重复综合分/质量分硬门；年度资格与技术40门仍生效。38/50等旧值不是当前默认 |
| 行情与风控 | 独立交易日历确认北京时间应完成交易日，指数与个股必须核验；合法OHLC、成交额、停牌缺口及ATR风控仍独立生效。金融企业留专项pending |

PB/PE 是 TTM 盈利与 MRQ 净资产构成的估值盈利能力 proxy，不是真实年度 ROE，也没有实现增长可比组。年度ROE质量检查使用财报证据，不能用该proxy代替。行业内便宜也不等于绝对便宜；默认PE>50给风险提示，`INDUSTRY_PE_ABSOLUTE_CAPS` 可设置行业专属硬上限。

周期正常化估值用 `当前PE TTM × 当前归母利润TTM / 连续年度归母利润中位数`，至少3个正且有限年度样本，不以合并净利替代归母利润。合并净利TTM超过年度合并净利中位数1.5倍是独立盈利高点风险标签，默认 `CYCLICAL_PEAK_AS_PENDING=False`；它不替代正常化估值硬门。

**周期行业误判豁免（2026-10-26）**：`classify_cyclical_industry` 依赖行业名，而 Baostock `query_stock_industry` 只给证监会大类——`C26化学原料和化学制品制造业` 逐字相同地覆盖云天化（磷肥）与珀莱雅（化妆品）、登康口腔（牙膏）。两只消费股因此被误加周期正常化检查，并把归因里的「周期组」均值从 −4.62% 抬到 −2.44%。现由 `CYCLICAL_EXEMPT_CODES=("603605","001328")` 按代码豁免；豁免判断在 `missing` 之后，行业缺失仍判 missing 并照常打 `industry_classification`。新增标的只能人工维护：**宁可漏豁免（继续按周期收紧），也不放宽行业判定口径**。

## 经营、公告日与风险证据

收入/净利/扣非单项同比下降不超过3%时可通过单项趋势检查；现金流下降不超过3%且当前累计现金流/合并净利≥0.8时可通过现金流下降检查。当前现金流为负仍进入观察。收入、合并净利和经营现金流三项同降仍直接failed，即使每项都落在容忍范围内。当前收入/净利/扣非非正，或净利/扣非同比<-10%，直接否决；同比恰好-10%为观察。毛利率下降超过3个百分点、净利率下降超过2个百分点也进入观察。

近期经营只接受 `报告日≤真实公告日≤决策日`，同时核对股票、决策日、最新报告期及去年同期基准。单季金额由累计差分推导，TTM由上年全年+当年累计−上年同期推导；仅作辅助证据，不能填补六项核心缺失，也不直接改变近期经营状态。归母TTM另供周期正常化估值使用。细节见[近期经营数据契约](recent_operating_contract.md)。

年度质量优先读取原始新浪摘要真实公告日；数据不足时使用AkShare摘要fallback。fallback缺公告日时保守假定次年5月1日可用，并保留 `availability_assumed` 和“公告日假定”标签；真实晚公告不能被假定日期覆盖，原始已知失败不能被fallback抹掉。近期经营与财务风险不采用该公告日假定。免费接口不是历史财报修订版本库，历史证据仍受重述和覆盖不足限制。

财务风险不把缺商誉填零，不拼接不同报告期/公告日/来源/口径。既有按字段补齐的技术财务数据不能代替独立risk证据。金融企业不强套普通企业现金流和利润率规则，保持专项待核验。

只有显式关闭 `REQUIRE_RECENT_OPERATING`，质量路径才恢复旧Baostock单项前瞻净利兼容分支；其报告期验证不能称为真实公告日验证。

## 止跌、分数与排序

| strict等级 | 证据 | 处理 |
|---|---|---|
| strong | 收盘≥MA20且MA20近5日不下行，并站上MA60 | 通过止跌维度 |
| medium | **必须站上 MA20**（收盘≥MA20 且 MA20 近5日不下行）；或站上MA60且近5日未创新低 | 通过止跌维度 |
| weak | 仅MACD改善、低点证据不足、仍下移，或 MACD 改善但**收盘仍在 MA20 下方** | pending |
| 无确认 | MA20与MACD均无足够改善 | `FAIL_STABILIZATION` |

**2026-10-13 收紧说明**：旧口径的 medium 有一条不要求站上 MA20 的左侧通道
（`_ma20_ok or (_macd_improving and _no_new_low)`）。真实落库推荐追踪
（`diag_real_recs.py`，2026-09-08~10-09，31 条正式推荐）显示：48% 的推荐自带
「左侧·MA20下方待企稳」标签，这 15 只 F5 **全部下跌**、中位 -3.67%、胜率 0%，
而同期沪深300 上涨（09-16→09-21 +1.32%）。即「尚未企稳」被当成已确认放行，
是推荐后下跌的第一大来源。现默认要求 medium 必须站上 MA20，该通道降级为
weak→pending；放行集合只减不增（真实 A/B：8 只降级、0 只升级，见
`diag_stab_ma20_ab.py`）。设为 `False` 可回到旧口径做对照。

`risk_only`、`disabled` 为显式对照配置，不是默认规则。`QV_ENFORCE_KDJ_MACD_VETO=False` 只关闭额外KDJ/MACD否决，不关闭strict止跌、状态技术40及近期底部检查。

综合规则分保持 `0.45×质量+0.30×估值+0.25×技术`，默认用于排序。历史估值核验后以30%权重混入估值分。三项权重之和必须为1；`describe_qv_floor()` 按实际配置解释额外分数线，不再把旧50分案例当当前准入要求。

`rank_score=score+相对沪深300强度微调（最多±3）+经结构确认横盘加分（最多5）+低位分位项（默认0）`。横盘加分须满足最近20日振幅≤12%且较前窗口不扩大、低点不下移、收盘标准差/均值≤4%，再按距250日低点30～120日插值。正式候选按rank_score、质量分、估值分降序，代码升序；加分不能挽救资格失败。市场数量收缩、急跌熔断与行业分散继续生效。

## 执行口径：交易计划与追踪（2026-10-13 执行中期化）

**性质：纯执行/展示口径，零准入改动**——推荐集合与改动前完全一致，对「宁缺毋滥」零风险。

| 项 | 原口径 | 现行口径 | 依据 |
|---|---|---|---|
| 建议持有周期 | 10~20 个交易日 | **40~60 个交易日** | strict 幸存者（`audit_current_strict.out`，14 决策日×799 面板，19 条观测）F20 **−1.40%** 而 F60 **+3.55%**——边缘在 60 个交易日，旧计划在第 20 日就收割 |
| 止盈目标 | 固定 +30% | 固定 **+15%** | 主板 31,880 次「随机起点持有 20 日」：+30% 触发率仅 **5.9%**、−15% 止损触发率 10.9%、期内最大有利波动中位 +6.4%（`audit_live_trading`）——+15% 在 40~60 日窗口才是可达目标 |
| 止损 | `max(3×ATR, 15%)` | **不变** | 10-07 修复（触发率 25.5%→9.6%）；窗口拉长只增触发概率，15% 下限的必要性更强 |
| 建仓区间 | 信号日收盘 ±1.5%，次日开盘执行 | 不变 | — |
| 追踪期限 | 5/10/15/20 个交易日 | **+40/60**（补跑窗口 60→115 日历天） | 没有 40/60 观测，新窗口的收益无法被度量与归因 |

 sleeve 边界：**突破**是 2~5 日短线口径，`VolumeBreakoutConfig` 已显式覆写回持有 10~20（`BreakoutSignal` 写入行内字段），不随基类变长；**长期 sleeve** 维持 30~90、止盈 +50%，其上限仍长于生产。`rr_ratio` 是计划参数的代数恒等式（低波动时 15%/15%=1.0），不是成功概率，卡片已加注。

回归锁定：`test_quality_recommendations.py::test_execution_midterm_caliber_locked`（口径值 + 止损语义 + 追踪期限 + 突破/长期覆写）；`test_strictness_fixes.py`（止损下限与 RR 结构断言已随止盈口径校正）。

## 突破终审与展示

突破默认 `technical`，形态、量能、趋势及ATR保持独立；周线默认 `WEEKLY_MA_BOTH_REQUIRED=True`，站上MA10（容忍2%）且MA10上行，并按启用开关核验周线MACD，只用已收盘周bar。该AND默认不能与低位技术对照的OR混写。

突破分首日 `first_breakout`、站稳 `held_confirmed`、回踩 `retest_confirmed`。最近5日的确认必须源于通过完整原事件闸门的突破，固定原事件阻力锚点，任何之后收盘跌破锚点都取消确认；站稳需2日收盘持续超过锚点0.5%，回踩需触及锚点上方2%范围且收盘重新超过0.5%。首日、确认日和锚点分别记录，不把滚动阻力下降当新突破。

突破子分与总体0～100规则分有界，幅度分在1%～3%最佳区间取峰，追高至7%降至零。20日市场/行业RS最多±3分，仅影响 `ranking_score`，数据不足中性；行业参照至少3个样本。原规则分仍用于等级与准入。终审补齐财务并重新验证近期经营、独立财务risk、金融专项及周线；初筛pending不自动成为formal。该路径不要求连续三年质量认证。

只有formal进入正式通知与落库。新证据与子分存在于信号/结果对象，不等于全部入库：MySQL仍使用固定列白名单，并未完整持久化历史PE、risk、正常化PE、底部锚点、突破分类、RS及所有子分。详见[本次证据升级边界](recommendation_evidence_upgrade.md)。

## 离线回归命令

```shell
python -B -m unittest discover -s tests -p test_recommendation_evidence_upgrades.py -v
python -B -m unittest discover -s tests -p test_financial_evidence_upgrades.py -v
python -B -m unittest discover -s tests -p test_recent_operating.py -v
python -B -m unittest discover -s tests -p test_strategy_revision.py -v
python -B -m unittest discover -s tests -p test_breakout_verification_offline.py -v
python tests/test_stab_cooldown_20261013.py
python -B -m unittest test_fundamental_quality test_quality_recommendations test_quality_value_hardening test_recommendation_upgrades test_minimal_quality_repairs -q
```

这些命令用于合成数据/mock离线回归，不发送通知、不连接数据库。旧规则测试通过显式配置隔离职责，不能据此推断旧配置仍是默认。最终通过数量以完成后的测试日志为准；文档不预报未完成结果，也不声称已跑实盘或收益改善。

`tests/test_stab_cooldown_20261013.py` 锁定 2026-10-13 两项收紧（medium 需站上 MA20、
重复推荐冷却），其中 A/B 部分读取 `cache/daily_*_qfq.csv` 真实行情；缺少缓存时
该项会跳过并如实反映，其余断言仍生效。

## 真实推荐追踪（2026-10-13 新增，只读诊断）

```shell
python diag_real_recs.py        # 40 条落库推荐逐条算 F1/F3/F5/F10、止损止盈触发、分组归因
python diag_stab_ma20_ab.py     # 对同一批真实推荐重放 evaluate，A/B 两口径只差新开关
```

产物 `audit_real_recs_out.csv` 可直接用于回答「今天为什么没推 X」与
「推荐后实际涨跌」两类问题。已知口径边界：行情缓存截至 2026-09-30，
F10 样本极少；10-08 之后的推荐暂无后续行情。
