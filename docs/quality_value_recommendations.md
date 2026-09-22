# 优质低估低位荐股调整

## 目标与资格

默认 RECOMMENDATION_MODE=quality_value（**优质低估低位入口 run.py**）。bottom_fishing 的 quality_value 路径使用 evaluate_quality_value 和 _screen_quality_pool。**放量突破入口 run_breakout.py 已改为独立的 technical 模式**（#1），跑自己的七层突破漏斗，不再委派 quality_value 资格；两入口的重叠由组合层同日去重、合计上限 DAILY_TOTAL_MAX_PICKS 约束。保留 technical 模式供旧规则回归对照（该口径对外称「低位企稳」，才是真正的技术抄底），不更改交易撮合。

1. 非金融企业最近连续3个可用完整年度：ROE中位数至少10%、各年至少5%、扣非净利各年为正；累计经营现金流/累计合并净利润至少0.8，累计净利润须为正。季度年化ROE不再代替年度质量。年度缺失、非有限数不能进入正式推荐。
2. 估值（#4a 行业相对）：默认 USE_INDUSTRY_RELATIVE_VALUATION=True，个股 PE/PB 在其所属行业当日横截面的分位 ≤ VALUATION_INDUSTRY_PERCENTILE_MAX(0.60) 才算便宜；行业数据缺失或行业内可比样本 < VALUATION_INDUSTRY_MIN_PEERS(5) 时自动回退绝对阈值（0<PE TTM≤25、0<PB MRQ≤3）。两种口径下 PE/PB 必须齐全、有限、为正，≤0 直接否决，缺项为 pending。横截面快照由 build_industry_valuation_snapshot 在筛选前用全池日线构建一次（复用缓存、不额外取数）。
3. 价格处于最近250根有效成交日线最高/最低区间的下40%。至少250根；最新零量/零成交额、日期滞后、不合法OHLC否决。取数窗口从120增为600自然日。中期低位不代表低估，必须同时通过前两条。
4. 前瞻确认（#4b）：REQUIRE_FORWARD_CONFIRMATION=True 时，用 Baostock query_growth_data 最新报告期净利润同比(YOYNI)做刹车，同比 < FORWARD_NI_YOY_MIN(-30%) → FAIL_FORWARD 否决（对治「trailing 年报漂亮、当年正在崩」的价值陷阱）；同比落在 [FORWARD_NI_YOY_MIN, FORWARD_NI_YOY_WARN)（即 [-30%, -10%)）不否决，只在决策简报打「业绩下滑预警」黄标。成长数据缺失、季度无效、过旧或在决策日之后 → 记「forward」缺项 → pending（FORWARD_MISSING_AS_PENDING=True，**当前默认**，与《最小修复说明》一致）；显式置 False 可恢复「缺失放行」（刹车仅在数据可得时生效）。报告期门槛：1–4 月可使用上一年三季报或已披露年报，5–8 月至少当年 Q1，9–10 月至少 Q2，11–12 月至少 Q3。仅主源 Baostock 提供，AkShare 兜底日按缺失处理。
5. 综合分下限（#3）：MIN_QV_SCORE=60，综合分 < 60 的 formal 候选否决（FAIL_QV_SCORE）。仅对「将要成为正式推荐」（missing 为空）的候选生效——pending 的估值分因数据缺失被记为 0、综合分被人为压低，对其套下限无意义。设 0 关闭。
6. 保留已知商誉超限排雷及负债率<=70%核验。金融企业依旧按现有名称/代码识别，输出financial_review并留待行业专项核验，不套普通企业现金转换率与前瞻确认。行业识别仍需后续完善，不能据此声称覆盖全部金融子行业。

## 市场级刹车（#2）

此前 quality_value 在 main() 提前 return，绕过了组合层风控（急跌中反而出票更多）。现已把以下两项移到 quality_value 分支之前、两种模式共用：

- 急跌熔断：沪深300 近 MARKET_CRASH_LOOKBACK(5) 个交易日累计跌幅 ≤ MARKET_CRASH_HALT_PCT(-4%) → 本次运行不推荐。
- 推荐数量按 regime 收缩：牛 MAX_PICKS(5) / 中性 NEUTRAL_MAX_PICKS(4) / 熊 BEAR_MAX_PICKS(2)，由 resolve_max_picks 解析后作为 max_picks 传入 _screen_quality_pool 截取；unknown 经 _effective_regime 折叠为熊。

ATR 交易风险门槛、20日位置、短期涨幅、RSI上限、KDJ确认、周线确认仍只作用于 technical 路径，不作为 quality_value 的硬闸门。

## 排序与标签

score=0.50*quality_score+0.35*valuation_score+0.15*daily_score，各项0至100。质量分为连续ROE中位数/最低ROE/现金转换率分数，权重50%/25%/25%；各维度达到准入阈值为50分，默认25%/15%/1.5饱和100分。扣非盈利是硬条件，不重复计分。估值分在绝对上限内按PE/PB线性评分，行业相对模式下「行业内便宜但绝对 PE/PB 偏高」的票估值分会被钳到 [0,100]（不为负）。缺项候选分数仅用于待核验顺序。

技术沿用既有日线计算，MA/EMA、MACD、RSI、量价提供分数，底背离提供标签。入选依据另会附加「行业估值分位PE../PB..」「当年净利±..%」（对应数据可得时），便于人工裁量。价格跌得更深不再加分。等级只是展示，不再有旧技术B级准入门槛（准入由 MIN_QV_SCORE 综合分下限承担）。

## 数据与分层

年度字段来自 ak.stock_financial_abstract(symbol=六位代码)，宽表“常用指标”中净资产收益率(ROE)、扣非净利润、净利润、经营现金流量净额；ROE百分数，后三者元。净利润为合并口径，不可拿归母替代。仅12月31日完整年报。

公告日存在时优先使用；无公告日按次年5月1日可用，缺最新年度不能以旧三年替代。该保守日期门槛无法解决财报后续重述，免费摘要不是历史版本数据库。年度取数先经行情/估值/位置预筛，缓存按代码/决策日/年数隔离。

formal要求年度、负债率、PE、PB及行情日期均已核验；pending单列通知，不占formal名额、不落库。已知质量失败即使另有缺项也直接否决。所有候选核验重评后再排序/行业分散/截取，避免缺数据候选挤占前N名。

历史回测在该模式重用同一evaluate；季度缓存按as_of隔离并检查公告日，补年度数据后重新评估与排序。历史禁用无as_of的当前财务补齐。仅普通OHLCV的突破CSV不包含多年财务，默认不能生成正式交易信号；零交易不表示运行成功验证了收益。T+1等交易撮合问题不属于本次修改范围。

## 运行口径与可观测性（2026-09 补强）

三项针对「口径说不清、容易被误读」的改造：

**1. 库级口径 = 生产口径（消除口径分裂）**
`QV_BEAR_TIMING_GATE` 此前库级 `False`、仅 `run.py` 覆盖为 `True`，导致任何用 `StrategyConfig()` 默认值跑的实验（`backtest.py ab`、单测、临时脚本）都跑在一个生产并不存在的策略上，A/B 结论无法采信。现该开关的库级默认值改为 `True`，`run.py` 不再单独赋值；需要关掉时显式 `config.QV_BEAR_TIMING_GATE = False`。行为影响：**生产结果不变**（run.py 本就是 True），变的是测试与回测现在与生产同源。

**2. 综合分下限的等效门槛量化公开**
`MIN_QV_SCORE=60` 严格强于三道硬闸门之和——评分口径使「恰好压线达标」的公司拿不到 60 分：

| 评分项 | 在闸门恰好压线处的取值 |
|---|---|
| 质量分（0.50） | 三档同时压线的**最小可达值 = 50**（`_dimension_score` 在阈值处给 50；如 3 年 ROE=(5,10,10) + 现金转换 0.8 → 实测 `verified` 而 `quality_score=50`） |
| 估值分（0.35） | 行业口径分位上限 0.60 处 = **40**；绝对口径 PE=25/PB=3 上限处 = **0** |

因此压线合格候选的综合分上限仅 **54.0 / 40.0**（行业 / 绝对），均 < 60。反解过线所需技术分：质量分 50 时两口径都不可达（≥140 / ≥233）；质量分 70 时行业口径需 ≥73、绝对口径 ≥167（不可达）；质量分 90 时绝对口径需 =100（必须满分）。
副作用：PE/PB 缺失（AkShare 兜底日）估值分为 0 → formal 几乎必然归零，与 `FORWARD_MISSING_AS_PENDING=True` 是两个独立叠加的零推荐机制。
实现：`qv_floor_equivalence()` / `describe_qv_floor()` 随配置实时算出门槛并打进漏斗日志；算术固化为 `test_recommendation_upgrades.TestScoreFloorEquivalence`。**本次未改数值**，只量化公开。

**3. 降级与口径回退显式声明**
- **数据源降级**（Baostock 熔断、全程 AkShare）：零推荐时卡片标题写「数据源不足，本次无正式推荐」而非「今日无信号」，正文点明「原因是数据源，不是市场」。
- **估值口径回退**：策略层 `_tag_valuation_mode` 把实际口径逐行写入 `valuation_mode` 列，`run.py` 汇总透传；为 `absolute`/`mixed` 时卡片声明本次为绝对阈值回退 / 混合口径，并提示不可与行业口径结果直接比较。
- **标题口径名**：`bottom_fishing` 这个落库值覆盖 quality_value 与 technical 两种口径，零推荐时无数据可推断，故由调用方显式传 `recommendation_mode`；标题显示为【优质低估低位】/【低位企稳】/【放量突破】，不再出现【抄底策略】优质低估低位荐股 这类自相矛盾的组合。
- **单票标签与操作计划**：卡片在渲染前按 `tier` 过滤，非 `formal` 一律不渲染，故卡片上的每一只都是**正式推荐**；标签由「XX候选」改为「**正式推荐 · XX口径**」（「候选」在中文里偏「备选／仅供参考」，与 formal 的真实含义相反），并给每只票新增**操作计划**块：建仓区间＝信号日收盘价 ×(1 ± `ENTRY_BAND_PCT`=1.5%)（次日开盘执行，高于上沿不追）、止损、止盈、盈亏比、建议持有 `HOLD_DAYS_HINT_MIN~MAX`＝10~20 个交易日（与追踪窗口 5/10/15/20 交易日对齐）。全部为展示项，不参与准入、排序与否决；两套策略共用 `describe_trade_plan`。

> 已知未处理项：`evaluate_quality_value` 无 ATR 否决层、也不接收 `volatile_out`，因此**生产默认口径下波动率观察池恒为空**（`run.py` 仍在收集与推送该池）。这是行为缺口而非文档问题，改动会改变推荐结果，按「改产线口径前先 A/B」的约定未擅自修改。

## 验证

- python -B -m unittest test_fundamental_quality test_quality_recommendations -v
- python -B -m unittest discover -s tests -p test_quality_backtest_persistence.py -v
- python -B -m unittest test_recommendation_upgrades -v   # 本次四项升级（#1~#4）专用回归
- python -B test_entry_filters.py
- python -B test_main_layering.py
- python -B test_strategy_fixes.py
- python -B test_optimizations_p0.py
- python -B test_momentum_gates.py
- python -B test_breakout_ab.py

旧脚本显式设technical，验证旧对照分支；新测试验证默认规则，包括缺估值/年报只能pending、已知质量失败不可被技术分掩盖、长期低位但短期已反弹仍可推荐、核验后再取TopN及严格落库。test_recommendation_upgrades 覆盖四项升级：综合分下限（仅 formal 生效、pending 不受其累）、前瞻确认（恶化否决/健康放行并打标签/缺失行为可配/边界等于阈值放行）、行业相对估值（分位计算、回退条件、行业便宜放行而绝对贵、行业贵否决而绝对便宜、负PE仍否决、快照构建与空行业回退）、突破独立（technical 不委派 quality_value、run_breakout 强制 technical）、市场级刹车（急跌熔断在筛选前拦截、熊/牛 regime 数量收缩、_screen_quality_pool 按 max_picks 截取）。全部为离线合成/真实字段fixture，不代表盈利能力验证。

注：test_quality_recommendations 的合格样本 fixture 已上调（年度 ROE 18/20/22、PE8/PB0.9），使其综合分稳过生产默认下限 MIN_QV_SCORE=60；test_momentum_gates 的 quality_value 节关闭该下限（MIN_QV_SCORE=0）以隔离 KDJ/MACD 闸门归因。
