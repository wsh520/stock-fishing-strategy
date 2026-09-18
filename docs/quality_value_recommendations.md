# 优质低估低位荐股调整

## 目标与资格

默认 RECOMMENDATION_MODE=quality_value。bottom_fishing 与 volume_breakout 共用 evaluate_quality_value 和 _screen_quality_pool。保留 technical 模式供旧规则回归对照，不更改交易撮合。

1. 非金融企业最近连续3个可用完整年度：ROE中位数至少10%、各年至少5%、扣非净利各年为正；累计经营现金流/累计合并净利润至少0.8，累计净利润须为正。季度年化ROE不再代替年度质量。年度缺失、非有限数不能进入正式推荐。
2. 当前 PE TTM 和 PB MRQ 必须齐全、有限且为正，分别不超过25、3；即使关闭旧USE_VALUATION_FILTER，新模式也必须核验。已知估值不达标直接否决，缺项为pending。
3. 价格处于最近250根有效成交日线最高/最低区间的下40%。至少250根；最新零量/零成交额、日期滞后、不合法OHLC否决。取数窗口从120增为600自然日。中期低位不代表低估，必须同时通过前两条。
4. 保留已知商誉超限排雷及负债率<=70%核验。金融企业依旧按现有名称/代码识别，输出financial_review并留待行业专项核验，不套普通企业现金转换率。行业识别仍需后续完善，不能据此声称覆盖全部金融子行业。

## 排序与标签

score=0.50*quality_score+0.35*valuation_score+0.15*daily_score，各项0至100。质量分为连续ROE中位数/最低ROE/现金转换率分数，权重50%/25%/25%；各维度达到准入阈值为50分，默认25%/15%/1.5饱和100分。扣非盈利是硬条件，不重复计分。缺项候选分数仅用于待核验顺序。

估值分=50*(1-PE/25)+50*(1-PB/3)，仅在已满足正值与上限时计算；未引入历史分位或行业估值以避免增加数据依赖。该分数不是低估幅度或收益概率。

技术沿用既有日线计算，MA/EMA、MACD、RSI、量价提供分数，底背离及放量突破提供标签。放量突破使用旧形态检测器，仅标签，不作为第二个独立荐股资格。两个入口共享标签以免去重后丢失信息。

20日位置、短期涨幅、RSI上限、KDJ确认、周线确认、ATR与市场急跌不再作为新模式的硬闸门；大盘仅说明，推荐数固定MAX_PICKS上限，行业上限继续保留。价格跌得更深不再加分。综合分不需要达到旧技术B级门槛，等级只是展示。

## 数据与分层

年度字段来自 ak.stock_financial_abstract(symbol=六位代码)，宽表“常用指标”中净资产收益率(ROE)、扣非净利润、净利润、经营现金流量净额；ROE百分数，后三者元。净利润为合并口径，不可拿归母替代。仅12月31日完整年报。

公告日存在时优先使用；无公告日按次年5月1日可用，缺最新年度不能以旧三年替代。该保守日期门槛无法解决财报后续重述，免费摘要不是历史版本数据库。年度取数先经行情/估值/位置预筛，缓存按代码/决策日/年数隔离。

formal要求年度、负债率、PE、PB及行情日期均已核验；pending单列通知，不占formal名额、不落库。已知质量失败即使另有缺项也直接否决。所有候选核验重评后再排序/行业分散/截取，避免缺数据候选挤占前N名。

历史回测在该模式重用同一evaluate；季度缓存按as_of隔离并检查公告日，补年度数据后重新评估与排序。历史禁用无as_of的当前财务补齐。仅普通OHLCV的突破CSV不包含多年财务，默认不能生成正式交易信号；零交易不表示运行成功验证了收益。T+1等交易撮合问题不属于本次修改范围。

## 验证

- python -B -m unittest test_fundamental_quality test_quality_recommendations -v
- python -B -m unittest discover -s tests -p test_quality_backtest_persistence.py -v
- python -B test_entry_filters.py
- python -B test_main_layering.py
- python -B test_strategy_fixes.py
- python -B test_optimizations_p0.py

旧四个脚本显式设technical，验证旧对照分支；新测试验证默认规则，包括缺估值/年报只能pending、已知质量失败不可被技术分掩盖、长期低位但短期已反弹仍可推荐、两入口一致、核验后再取TopN及严格落库。全部为离线合成/真实字段fixture，不代表盈利能力验证。
