# 准确性与可操作性改进（2026-10-24）

> 定位：主人确认「每天一两只正是所需要，精不在多」。本文记录两件事：
> ① 用唯一有 pit 基本面的决策日（2026-06-04）做「放宽 operating_weak」的前瞻实验，
> 回答准确性能否靠放宽闸门提升；② 本次上线的可操作性增强（纯展示层）。
> 所有改动不动任何准入、排序、否决口径。

## 一、背景：产出 0~2 只/天，pending 池卡在哪

GitHub Actions 最近五次运行（#114~#118，详见 CI 日志）合计仅 3 只正式推荐。
影子账本（run #118 工件，159 条 / 73 只独特股票）显示 near-miss 67 条，阻断原因
前几位：stabilization_weak + recent_bottom_unconfirmed + technical_weak 三连（14 只）、
operating_weak 单独（9 只）、operating_weak 组合（合计 20/30 出现在 10-24 待核验池）。

问题：若放宽「近期经营走弱」（operating_weak）闸门，放进来的票历史表现如何？

## 二、前瞻实验：放宽 operating_weak 是否有据（决策日 2026-06-04）

脚本：`diag_operating_relax_20261024.py`（只读；明细 `data/diag_operating_relax_20260604.csv`）。

方法（与 diag_pe_value / audit_live_trading 同口径，铁律全保留）：
- 800 面板逐只前移评估日（EVAL_OFFSET=75），断言落点=2026-06-04；
- 第一轮 `evaluate(fund_data=None)` 走价格层筛 → 幸存 29 只（当日市场环境 neutral）；
- 幸存者补当时已披露证据：年度财务取 `annual_quality_v1` 缓存（公告日 ≤ 决策日），
  近期经营走 `fetch_operating_trend(code, 决策日)`（as-of 过滤在模块内）；
- 第二轮完整重评 → formal / pending + missing_tags；
- 前瞻收益取 eval_i 之后真实行情（F5/F20/F60）；EVAL_OFFSET=0 自检通过（前瞻全 None）。

### 结果

| 分组 | n | F5 中位 | F20 中位 | F60 中位 | F20 胜率 |
|---|---|---|---|---|---|
| operating=weak | 10 | −3.60% | **−6.38%** | −1.11% | 10% |
| operating=verified | 3 | −0.95% | −3.82% | +4.18% | 0% |
| operating=missing | 5 | +1.42% | −1.02% | +2.56% | 40% |
| 价格层幸存全体（25 只可前瞻） | 25 | −0.62% | −3.62% | +3.19% | 32% |

三个关键发现：

1. **经营闸门方向正确，不放宽**：operating=weak 组在 F5/F20/F60 三个期限全面
   差于 verified 组（F20 −6.38% vs −3.82%，胜率 10% vs 0%——verified 组 F20 也弱，
   但 weak 组明显更差）。F60 上 verified +4.18% vs weak −1.11%，差距 5.3pp。
2. **「仅 operating_weak 被挡」= 0 只**：当日 3 只 operating=weak 且到达 PASS 的票
   （600486/600741/600941）全部同时带 financial_risk / industry_classification /
   stabilization_weak 等标签，从未单独被经营闸门挡住。放宽 operating_weak 一条
   在该决策日增量恰好为 0——真正的瓶颈是证据缺项集群，不是经营单闸门。
3. **硬否决（FAIL_RECENT_FUNDAMENTAL）的 10 只是真恶化，不是数据缺失**：
   净利同比中位 −15.8%（最差 −83.9%），其中 601020 净利同比 −83.9% 对应 F5 −17.6%
   ——闸门拦掉的确实是接飞刀。当日 F60 全体中位 +3.19%（胜率 64%），与
   audit_current_strict 的「strict 幸存者 F60 +3.55%」互相印证。

### 边界

- 单决策日、价格层幸存仅 29 只、经营数据接口可用率约 70%（失败记 missing/failed，
  不伪造）。结论是「没有放宽的证据」，不等于「有坚决不放松的强证据」；
  多决策日前瞻证据仍不可达（pit 基本面仅 06-04 单日），与 10-15 归因的结论一致：
  **零推荐主因是市场，不是管道，更不是闸门过严**。

## 三、可操作性增强（本次上线，纯展示）

`src/screening_engine.py`，全部只影响飞书卡片文案，不新增 Signal 字段、不取数、
不参与准入/排序/否决；缺字段的旧行整块不渲染（向后兼容）：

1. **执行规则**（quality_value 分支补齐）：次日开盘落在区间内按计划执行；高于上沿
   视为追高、放弃本轮；低于下沿按失效重估而非「越低越买」。此前仅 technical/突破
   分支渲染该规则，两套策略口径由此一致（与 `ENTRY_BAND_PCT` 注释同源）。
2. **仓位档位提示**（`_position_hint`）：按 ATR% 三档——≤3% 低波档标准仓、
   3~4.5% 中波档半仓、>4.5% 至风控上限 6% 观察档。依据低波异象（RQ_W_LOW_VOL=0.35
   的实证：ATR% 越低未来收益越好、止损越不易被扫）。阈值 `POSITION_ATR_LOW_PCT /
   POSITION_ATR_MID_PCT` 可配置；卡片注明「风险管理参考，非投资建议」。
3. **复核节点**：买入后第 5/10/15/20/40/60 个交易日对照周度追踪复盘
   （与 `store.mysql_store.TRACK_HORIZONS` 同源，惰性 import）；止跌或经营证据
   反转时立即重估，不必等到复核窗口。
4. **持有期观察清单**（`_observation_lines`）：从行内已有字段派生——
   「继续持有信号」（止跌强/中确认的延续条件、经营证据延续条件、守住底部结构）
   与「减仓离场信号」（止跌弱确认的减仓线、经营继续走弱的离场线）。
   与既有「失效价」互补：失效价管价格底线，观察清单管证据反转。

## 四、测试与回归

- `tests/test_recommendation_display_upgrades.py`：17/17 通过（新增 5 个用例：
  执行规则/复核节点、仓位三档、观察清单渲染条件、quality 卡片端到端、legacy 不变）。
- 全量 `unittest discover -s tests`：305 个测试，仅 1 失败 + 2 loader error，
  均为存量（stash 比对确认与本次改动无关；2 个 loader error 是 import 时
  sys.exit 被 discover 误报的已知问题）。
- 根目录脚本：`test_recommendation_upgrades` 10 失败、`test_strategy_fixes` 77/78，
  与 2026-10-17 基线完全一致，无回归。

## 五、后续建议（按优先级）

| 优先级 | 事项 | 依据 |
|---|---|---|
| P0 | 修复 AkShare eastmoney 通道（CI 每次运行熔断 14~16 次） | 数据管道退化会同时伤害市场判定与估值口径（#115 已预演） |
| P1 | 冷却期敏感性回测（10 日 vs 5 日） | #116 有 3 只资格全被冷却压成 0 正式，是现成样本 |
| P1 | 影子账本 10-26 后补跑 `run_shadow_tracking.py`，积累 operating_weak 分组前瞻证据 | 账本 10-19 起，F5 窗口 10-26 起成熟 |
| P2 | 多决策日 pit 基本面重建，把经营闸门实验从单日扩展到多日 | 本文实验的最大边界 |
