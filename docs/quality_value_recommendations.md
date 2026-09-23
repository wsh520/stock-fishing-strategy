# 优质低估低位荐股调整

## 目标与资格

默认 RECOMMENDATION_MODE=quality_value（**优质低估低位入口 run.py**）。bottom_fishing 的 quality_value 路径使用 evaluate_quality_value 和 _screen_quality_pool。**放量突破入口 run_breakout.py 已改为独立的 technical 模式**（#1），跑自己的七层突破漏斗，不再委派 quality_value 资格；两入口的重叠由组合层同日去重、合计上限 DAILY_TOTAL_MAX_PICKS 约束。保留 technical 模式供旧规则回归对照（该口径对外称「低位企稳」，才是真正的技术抄底），不更改交易撮合。

1. 非金融企业最近连续3个可用完整年度：ROE中位数至少10%、各年至少5%、扣非净利各年为正；默认还要求每年合并净利润和经营现金流均为正；累计经营现金流/累计合并净利润至少0.8。季度年化ROE不再代替年度质量。年度缺失、非有限数不能进入正式推荐。
2. 估值（#4a 行业相对 PE）：默认 USE_INDUSTRY_RELATIVE_VALUATION=True，个股 **PE** 在其所属行业当日横截面的分位 ≤ VALUATION_INDUSTRY_PERCENTILE_MAX(0.60) 才算便宜；行业数据缺失或行业内可比样本 < VALUATION_INDUSTRY_MIN_PEERS(5) 时自动回退绝对 PE 上限（0<PE TTM≤25）。PE 必须有限且为正，≤0 直接否决，缺失为 pending。横截面快照由 build_industry_valuation_snapshot 在筛选前用全池日线构建一次（复用缓存、不额外取数）。
   **PB 职责（本轮调整）**：PB 不再单独否决——略高于绝对上限(3.0)或行业分位上限只作**异常识别与风险说明**，并继续展示；`PB≤0`（净资产为负/数据异常）仍按异常否决；PB 缺失只标注缺项 `valuation_pb`，不伪装成已核验，也不因缺少这一**辅助**指标把其他核心证据完整的股票降为 pending。**注意这不是「所有高 PB 无条件通过」**：年度盈利质量、PE、近期业绩、低位、止跌条件仍须各自通过。
3. 价格处于最近250根有效成交日线最高/最低区间的下40%。至少250根；最新零量/零成交额、日期滞后、不合法OHLC否决。取数窗口从120增为600自然日。中期低位不代表低估，必须同时通过前两条。**停牌缺口**：相邻 K 线自然日间隔 > MAX_BAR_GAP_DAYS(12) 判定期间曾停牌 → FAIL_HALT_GAP 否决（沿用 REQUIRE_NO_HALT_GAP；K 线跨缺口时全部滚动指标失真）。
4. 前瞻确认（#4b）：REQUIRE_FORWARD_CONFIRMATION=True 时，用 Baostock query_growth_data 最新报告期净利润同比(YOYNI)做刹车，同比 < FORWARD_NI_YOY_MIN(**-10%**) → FAIL_FORWARD 否决（对治「trailing 年报漂亮、当年正在崩」的价值陷阱；**恰好等于 -10% 不触发**）；同比落在 `[FORWARD_NI_YOY_MIN, FORWARD_NI_YOY_WARN)`（即 **[-10%, 0%)**）不否决，只在决策简报打「业绩下滑预警」黄标。成长数据缺失、季度无效、过旧或在决策日之后 → 记「forward」缺项 → pending（FORWARD_MISSING_AS_PENDING=True，**当前默认**，与《最小修复说明》一致）；显式置 False 可恢复「缺失放行」（刹车仅在数据可得时生效）。报告期门槛：1–4 月可使用上一年三季报或已披露年报，5–8 月至少当年 Q1，9–10 月至少 Q2，11–12 月至少 Q3。仅主源 Baostock 提供，AkShare 兜底日按缺失处理。
5. 综合分下限（#3）：MIN_QV_SCORE=60，综合分 < 60 的 formal 候选否决（FAIL_QV_SCORE）。仅对「将要成为正式推荐」（missing 为空）的候选生效——pending 的估值分因数据缺失被记为 0、综合分被人为压低，对其套下限无意义。设 0 关闭。**资格判定始终使用不含相对强度微调的综合分**。
6. **止跌确认闸门（本轮调整）**：`QV_STABILIZATION_GATE`（库级默认 True，前身为仅熊市生效的 `QV_BEAR_TIMING_GATE`）。**全市场环境**的正式推荐须满足其一：当日收盘价 ≥ MA20，或 MACD 柱连续 `MACD_MOMENTUM_DAYS`(2) 日改善；否则 FAIL_STABILIZATION。**不额外要求 RSI 反弹、KDJ 金叉或 MA60 站稳**。用于准入的指标数据不足（MA20 不可得 / MACD 柱含 NaN）**不视为已确认**。旧配置名 `QV_BEAR_TIMING_GATE` 经 `__setattr__`/`__post_init__` 写穿迁移，构造传入与构造后赋值都继续生效，不会静默失去作用。
7. **相对强度（本轮新增，仅排序与提示）**：`relative_strength` = 同一起止日期下「个股区间涨跌幅 − 沪深300区间涨跌幅」（百分点，`RS_LOOKBACK`=60）。指数日线复用主流程 `get_index_daily` 结果，按**共有交易日**对齐、只用决策日及之前数据。**不设硬性准入线**：以 `RS_WEIGHT`(=3.0) 为上限调整 `rank_score`（正式候选间小幅排序），明显跑输（≤ -10pp）时打风险提示标签；指数缺失/对齐不足 → 字段为 None、排序中性，不给「强势/弱势」结论。
8. 保留已知商誉超限排雷及负债率<=70%核验。金融企业依旧按现有名称/代码识别，输出financial_review并留待行业专项核验，不套普通企业现金转换率与前瞻确认。行业识别仍需后续完善，不能据此声称覆盖全部金融子行业。

## 市场级刹车（#2）

此前 quality_value 在 main() 提前 return，绕过了组合层风控（急跌中反而出票更多）。现已把以下两项移到 quality_value 分支之前、两种模式共用：

- 急跌熔断：沪深300 近 MARKET_CRASH_LOOKBACK(5) 个交易日累计跌幅 ≤ MARKET_CRASH_HALT_PCT(-4%) → 本次运行不推荐。
- 推荐数量按 regime 收缩：牛 MAX_PICKS(5) / 中性 NEUTRAL_MAX_PICKS(4) / 熊 BEAR_MAX_PICKS(2)，由 resolve_max_picks 解析后作为 max_picks 传入 _screen_quality_pool 截取；unknown 经 _effective_regime 折叠为熊。

quality_value 现在默认增加 ATR/收盘价波动率硬闸门：`QV_ATR_GUARD=True`，上限复用 `MAX_ATR_PCT=3.33%`；ATR 缺失视为不可核验（`FAIL_DATA`），超限为 `FAIL_VOLATILE`，可用 `QV_ATR_GUARD=False` 回退旧口径。20日位置、短期涨幅、RSI上限、KDJ确认、周线确认仍只作用于 technical 路径。`QV_VALUATION_DUAL_GUARD` 默认关闭；显式开启后 PB 高于绝对/行业上限会否决，PB 缺失会进入 pending。

## 排序与标签

score=0.50*quality_score+0.35*valuation_score+0.15*daily_score，各项0至100。质量分为连续ROE中位数/最低ROE/现金转换率分数，权重50%/25%/25%；各维度达到准入阈值为50分，默认25%/15%/1.5饱和100分。扣非盈利是硬条件，不重复计分。

**估值分（本轮重构，仅 PE）**：估值分以**实际用于准入的 PE 口径**为准，行业分位与绝对 PE 两条口径在各自准入上限处**锚定相同分值**：行业 PE 分位 = 0.60（=闸门上限）→ 40 分（`100×(1-0.60)`）；绝对 PE = 25（=MAX_PE_TTM 上限）→ 同样 40 分（`40 + 60×(1-pe/25)`）；更便宜时连续增加，限制在 0~100。这样同一只「压线合格」的股票不会因为数据源或行业样本状态变化而被截然不同的评分规则处理。PE 缺失 → 0 分。**PB 不参与估值评分**（继续展示并用于异常识别/风险说明），避免通过评分重新制造高 PB 硬否决。缺项候选分数仅用于待核验顺序。

技术沿用既有日线计算，MA/EMA、MACD、RSI、量价提供分数，底背离提供标签。入选依据另会附加「行业估值分位PE../PB..」「当年净利±..%」（对应数据可得时），便于人工裁量。价格跌得更深不再加分。等级只是展示，不再有旧技术B级准入门槛（准入由 MIN_QV_SCORE 综合分下限承担）。

## 数据与分层

年度字段来自 ak.stock_financial_abstract(symbol=六位代码)，宽表“常用指标”中净资产收益率(ROE)、扣非净利润、净利润、经营现金流量净额；ROE百分数，后三者元。净利润为合并口径，不可拿归母替代。仅12月31日完整年报。

公告日存在时优先使用；无公告日按次年5月1日可用，缺最新年度不能以旧三年替代。该保守日期门槛无法解决财报后续重述，免费摘要不是历史版本数据库。年度取数先经行情/估值/位置预筛，缓存按代码/决策日/年数隔离。

formal要求年度、负债率、PE及行情日期均已核验；pending单列通知，不占formal名额、不落库。默认 **PB 不再是 formal 的必备核验项**：PB 缺失只记 `valuation_pb` 缺项并继续展示，不把其他核心证据完整的股票降为 pending（PE 缺失仍为 pending）；显式开启 `QV_VALUATION_DUAL_GUARD` 后，PB 缺失会 pending、高 PB 会否决。已知质量失败即使另有缺项也直接否决。所有候选核验重评后再排序/行业分散/截取，避免缺数据候选挤占前N名。

历史回测在该模式重用同一evaluate；季度缓存按as_of隔离并检查公告日，补年度数据后重新评估与排序。历史禁用无as_of的当前财务补齐。仅普通OHLCV的突破CSV不包含多年财务，默认不能生成正式交易信号；零交易不表示运行成功验证了收益。T+1等交易撮合问题不属于本次修改范围。

## 运行口径与可观测性（2026-09 补强）

三项针对「口径说不清、容易被误读」的改造：

**1. 库级口径 = 生产口径（消除口径分裂）**
早期该开关（当时名为仅熊市生效的 `QV_BEAR_TIMING_GATE`）库级 `False`、仅 `run.py` 覆盖为 `True`，导致任何用 `StrategyConfig()` 默认值跑的实验（`backtest.py ab`、单测、临时脚本）都跑在一个生产并不存在的策略上，A/B 结论无法采信。现生产口径已收进库级默认，`run.py` 不再单独赋值。
**本轮进一步把该闸门从「仅熊市」扩展到「全市场环境」并更名为 `QV_STABILIZATION_GATE`**：旧名 `QV_BEAR_TIMING_GATE` 通过 `__setattr__`/`__post_init__` **写穿迁移**——`StrategyConfig(QV_BEAR_TIMING_GATE=False)` 与 `config.QV_BEAR_TIMING_GATE = False` 两种写法都继续生效（后者若只靠 `__post_init__` 会静默失效，等于旧配置被悄悄改义）；`QV_BEAR_TIMING_GATE=None`（未指定）不覆盖新配置项。需要关掉时显式 `config.QV_STABILIZATION_GATE = False`。

**2. 综合分下限的等效门槛量化公开**
`MIN_QV_SCORE=60` 严格强于三道硬闸门之和——评分口径使「恰好压线达标」的公司拿不到 60 分：

| 评分项 | 在闸门恰好压线处的取值 |
|---|---|
| 质量分（0.50） | 三档同时压线的**最小可达值 = 50**（`_dimension_score` 在阈值处给 50；如 3 年 ROE=(5,10,10) + 现金转换 0.8 → 实测 `verified` 而 `quality_score=50`） |
| 估值分（0.35，仅 PE） | 行业口径分位上限 0.60 处 = **40**；绝对口径 PE=25 上限处**同样** = **40**（锚点对齐，见 `_valuation_pe_score`）；PB 不参与估值评分 |

因此压线合格候选的综合分上限两种口径都是 **54.0**，均 < 60。反解过线所需技术分（v=40）：质量分 50 时不可达（≥140）；质量分 70 时需 ≥73；质量分 90 时需 ≥7。
副作用：PE 逐 bar 缺失（AkShare 兜底日）估值分为 0 且候选降为 pending → formal 会归零，与 `FORWARD_MISSING_AS_PENDING=True` 是两个独立叠加的零推荐机制。
实现：`qv_floor_equivalence()` / `describe_qv_floor()` 随配置实时算出门槛并打进漏斗日志；算术固化为 `test_recommendation_upgrades.TestScoreFloorEquivalence` 与 `test_quality_value_hardening.TestValuationScoring`。

**3. 降级与口径回退显式声明**
- **数据源降级**（Baostock 熔断、全程 AkShare）：零推荐时卡片标题写「数据源不足，本次无正式推荐」而非「今日无信号」，正文点明「原因是数据源，不是市场」。
- **估值口径回退**：策略层 `_tag_valuation_mode` 把实际口径逐行写入 `valuation_mode` 列，`run.py` 汇总透传；为 `absolute`/`mixed` 时卡片声明本次为绝对阈值回退 / 混合口径，并提示不可与行业口径结果直接比较。
- **标题口径名**：`bottom_fishing` 这个落库值覆盖 quality_value 与 technical 两种口径，零推荐时无数据可推断，故由调用方显式传 `recommendation_mode`；标题显示为【优质低估低位】/【低位企稳】/【放量突破】，不再出现【抄底策略】优质低估低位荐股 这类自相矛盾的组合。
- **单票标签与操作计划**：卡片在渲染前按 `tier` 过滤，非 `formal` 一律不渲染，故卡片上的每一只都是**正式推荐**；标签由「XX候选」改为「**正式推荐 · XX口径**」（「候选」在中文里偏「备选／仅供参考」，与 formal 的真实含义相反），并给每只票新增**操作计划**块：建仓区间＝信号日收盘价 ×(1 ± `ENTRY_BAND_PCT`=1.5%)（次日开盘执行，高于上沿不追）、止损、止盈、盈亏比、建议持有 `HOLD_DAYS_HINT_MIN~MAX`＝10~20 个交易日（与追踪窗口 5/10/15/20 交易日对齐）。全部为展示项，不参与准入、排序与否决；两套策略共用 `describe_trade_plan`。

> 说明：`evaluate_quality_value` 已接入 ATR 波动率闸门，并可把超限标的写入 `volatile_out` 观察池；该闸门会减少正式推荐数，启停应通过 A/B 回测验证。

## 验证

- python -B -m unittest test_fundamental_quality test_quality_recommendations -v
- python -B -m unittest discover -s tests -p test_quality_backtest_persistence.py -v
- python -B -m unittest test_recommendation_upgrades -v   # 四项升级（#1~#4）专用回归
- python -B -m unittest test_quality_value_hardening -v   # 本轮六项口径加固专用回归（45 项，离线）
- python -B test_entry_filters.py
- python -B test_main_layering.py
- python -B test_strategy_fixes.py
- python -B test_optimizations_p0.py
- python -B test_momentum_gates.py
- python -B test_breakout_ab.py

旧脚本显式设technical，验证旧对照分支；新测试验证默认规则，包括缺估值/年报只能pending、已知质量失败不可被技术分掩盖、长期低位但短期已反弹仍可推荐、核验后再取TopN及严格落库。test_recommendation_upgrades 覆盖四项升级：综合分下限（仅 formal 生效、pending 不受其累）、前瞻确认（恶化否决/健康放行并打标签/缺失行为可配/边界等于阈值放行）、行业相对估值（分位计算、回退条件、行业便宜放行而绝对贵、行业贵否决而绝对便宜、负PE仍否决、快照构建与空行业回退）、突破独立（technical 不委派 quality_value、run_breakout 强制 technical）、市场级刹车（急跌熔断在筛选前拦截、熊/牛 regime 数量收缩、_screen_quality_pool 按 max_picks 截取）。

test_quality_value_hardening 覆盖本轮六项调整的边界：止跌确认闸门（全环境一致 / 站上 MA20 或 MACD 连续改善各可独立满足 / 不要求 MA60·RSI·KDJ / 数据不足不视为已确认 / 显式关闭 / 旧配置名构造与赋值双向写穿）；净利同比 -10% 边界（恰好 -10% 放行、-10.01 否决、黄标区间 [-10%,0%)、缺失与过旧报告期为 pending、technical 路径不受该门槛影响）；PE/PB 职责（PE 超限与非正仍否决、PE 缺失 pending、PB 偏高不再否决、PB 非正仍异常、PB 缺失只标注不降级、行业 pb_pct 超限不否决、technical 的 PB 否决未受影响）；估值评分（两口径压线锚点均为 40、更便宜单调递增且有界、PE 缺失为 0、PB 不参与评分、下限 60 仍生效）；相对强度（对齐后等于超额收益百分点、只取共有日期、缺失/不足为 None 且排序中性、影响被限制在 ±RS_WEIGHT 且不改变资格判定、指数数据在主流程与两阶段筛选中的透传）；停牌缺口（quality_value 路径否决、长假不误杀、开关与阈值可配、technical 守卫行为不变）。

全部为离线合成 fixture：不访问网络、不连接数据库、不做全市场荐股、不发送通知。**这些测试只证明「逻辑按要求实现」，不构成荐股胜率提高的证据**；胜率变化须由 `backtest.py ab --mode quality_value` 之类的样本内/样本外回测另行评估，且仍不等于未来实盘收益。

注：test_quality_recommendations 的合格样本 fixture 已上调（年度 ROE 18/20/22、PE8/PB0.9），使其综合分稳过生产默认下限 MIN_QV_SCORE=60；test_momentum_gates 的 quality_value 节关闭该下限（MIN_QV_SCORE=0）以隔离 KDJ/MACD 闸门归因。
