# 优质低估低位与突破荐股规则

先判断资格，再划分正式推荐与观察候选，最后排序。年度质量、近期经营、财务风险、估值与止跌各自提供证据，分数不能抵消硬伤。默认 `run.py` 使用 `quality_value`；`VolumeBreakoutConfig` 与 `run_breakout.py` 默认使用独立 `technical` 突破策略。规则分不是成功概率，本次只升级荐股证据，不修改交易、回测或追踪算法。

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
| 止跌 | `QV_STABILIZATION_MODE="strict"`；强/中确认通过，弱确认pending，无改善否决；窗口缺失不能当确认 |
| 状态技术分 | `QV_TECHNICAL_STATE_SCORING=True`，趋势40+动能30+量价30，持续状态与最近5日衰减事件共同计分；技术分<40进入pending。旧事件日评分可显式关闭状态评分后对照 |
| 额外分数线 | `MIN_QV_SCORE=0`、`MIN_QUALITY_SCORE=0`，默认关闭重复综合分/质量分硬门；年度资格与技术40门仍生效。38/50等旧值不是当前默认 |
| 行情与风控 | 独立交易日历确认北京时间应完成交易日，指数与个股必须核验；合法OHLC、成交额、停牌缺口及ATR风控仍独立生效。金融企业留专项pending |

PB/PE 是 TTM 盈利与 MRQ 净资产构成的估值盈利能力 proxy，不是真实年度 ROE，也没有实现增长可比组。年度ROE质量检查使用财报证据，不能用该proxy代替。行业内便宜也不等于绝对便宜；默认PE>50给风险提示，`INDUSTRY_PE_ABSOLUTE_CAPS` 可设置行业专属硬上限。

周期正常化估值用 `当前PE TTM × 当前归母利润TTM / 连续年度归母利润中位数`，至少3个正且有限年度样本，不以合并净利替代归母利润。合并净利TTM超过年度合并净利中位数1.5倍是独立盈利高点风险标签，默认 `CYCLICAL_PEAK_AS_PENDING=False`；它不替代正常化估值硬门。

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
| medium | MA20确认；或MACD柱连续3日改善且最近5日最低价≥其前20日最低价 | 通过止跌维度 |
| weak | 仅MACD改善、低点证据不足或仍下移 | pending |
| 无确认 | MA20与MACD均无足够改善 | `FAIL_STABILIZATION` |

`risk_only`、`disabled` 为显式对照配置，不是默认规则。`QV_ENFORCE_KDJ_MACD_VETO=False` 只关闭额外KDJ/MACD否决，不关闭strict止跌、状态技术40及近期底部检查。

综合规则分保持 `0.45×质量+0.30×估值+0.25×技术`，默认用于排序。历史估值核验后以30%权重混入估值分。三项权重之和必须为1；`describe_qv_floor()` 按实际配置解释额外分数线，不再把旧50分案例当当前准入要求。

`rank_score=score+相对沪深300强度微调（最多±3）+经结构确认横盘加分（最多5）+低位分位项（默认0）`。横盘加分须满足最近20日振幅≤12%且较前窗口不扩大、低点不下移、收盘标准差/均值≤4%，再按距250日低点30～120日插值。正式候选按rank_score、质量分、估值分降序，代码升序；加分不能挽救资格失败。市场数量收缩、急跌熔断与行业分散继续生效。

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
python -B -m unittest test_fundamental_quality test_quality_recommendations test_quality_value_hardening test_recommendation_upgrades test_minimal_quality_repairs -q
```

这些命令用于合成数据/mock离线回归，不发送通知、不连接数据库。旧规则测试通过显式配置隔离职责，不能据此推断旧配置仍是默认。最终通过数量以完成后的测试日志为准；文档不预报未完成结果，也不声称已跑实盘或收益改善。
