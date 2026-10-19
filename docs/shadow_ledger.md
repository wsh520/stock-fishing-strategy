# 影子账本：pending / near-miss 的前瞻证据

## 为什么需要

项目原则是「先证据后阈值」，但正式推荐只保存 `tier='formal'`：

- `run.py` 只把 formal 交给 `save_recommendations`；
- `save_recommendations` 再次过滤 formal；
- 周跟踪只从 `stock_recommendation` 读正式推荐。

于是 **pending / near-miss 从未进入任何前瞻统计**。当底部年龄、经营弱等闸门把某只票挡在
formal 之外时，我们永远拿不到它的 F5/F20/F60 收益，也就无法回答「这道闸门是保护还是误杀」。
影子账本只补这条证据链，不改变任何准入。

## 边界（重要）

- **不参与准入/排序/通知**：只读取 `_screen_quality_pool` 已经产生的 `pending_rows`。
- **不写 `stock_recommendation`**：正式推荐落库逻辑完全不变，避免污染唯一键、周跟踪和归因。
- **不推送飞书**：`run_shadow_tracking.py` 只写 JSON 诊断文件。
- **不是买入建议**：影子记录是观察样本，不能当作推荐名单。

## 文件

| 文件 | 写入方 | 用途 |
|---|---|---|
| `cache/shadow_recommendations.jsonl` | `run.py` | Actions cache 持久化账本，跨运行累积 |
| `data/shadow/shadow_recommendations.jsonl` | `run.py` | 同一账本的 artifact 副本（每日工作流上传 `data/`） |
| `data/shadow/shadow_tracking.json` | `run_shadow_tracking.py` | 最新前瞻收益汇总（每周工作流 artifact） |

`cache/` 在定时工作流中只清理 `daily_*` / `weekly_*` / `index_daily_*`，**不会删除影子账本**。
周跟踪工作流复用同一个 `cache/` 前缀，因此能读到账本（当日运行未结束时可能读到上一份，
最多滞后一周；这是诊断数据，可接受）。

## 记录口径

- 幂等键：`(rec_date, code)`；同一天重复运行只更新证据，`first_seen_at` 保留，
  `seen_count` 递增。
- 只记录非 formal 行；若同一天该代码后来进入 formal，记录标 `superseded_by_formal=true`，
  周跟踪默认跳过，避免与正式追踪重复计数。
- `blockers` 与生产 pending 日志一致：默认剔除 `valuation_pb`（除非
  `QV_VALUATION_DUAL_GUARD=True`），剔除后为空则记 `pending_reason_unknown`。
- `is_near_miss = len(blockers) <= NEAR_MISS_MAX_BLOCKERS(=2)`。
- 数值字段做 `NaN/Inf -> null` 清洗；文本截断，避免 JSONL 失控。
- 字段包括：`rec_date/code/name/score/rank_score/daily_score/close/pe_ttm/pb_mrq/
  position_250/stabilization_level/quality_status/financial_risk_status/
  operating_status/operating_summary/missing_tags/blockers/blocker_count/is_near_miss/
  signals_hit/first_seen_at/last_seen_at/seen_count/run_id/run_number/superseded_by_formal`。

## 每周追踪

`run_shadow_tracking.py` 复用正式周跟踪的 `select_observations`：

- 以沪深300交易日定位推荐后第 5/10/15/20/40/60 个市场交易日；
- 收益率分母取**同一次前复权序列**的推荐日收盘价（避免除权把 10 送 10 算成 −50%）；
- 停牌/缺价跳过，不前后前填；
- 默认只追踪最近 180 个日历日内的 **near-miss**（`is_near_miss=true`，阻断≤2）；
  `--all` 可追踪全部 pending；同一代码只拉一次行情，再按各记录日期分别计算期限；
- 输出按「阻断组合 × 期限」和「near-miss × 期限」的 n / 均值 / 中位 / 胜率 / 最大最小。

手动运行：

```bash
python run_shadow_tracking.py
python run_shadow_tracking.py --ledger cache/shadow_recommendations.jsonl \
  --output data/shadow/shadow_tracking.json --max-age-days 180
python run_shadow_tracking.py --all      # 连非 near-miss 的 pending 也追踪
python run_shadow_tracking.py --no-cache
```

`weekly_tracking.yml` 在正式周跟踪之后以 `if: always()` 运行它，并上传
`data/shadow/` artifact。它不依赖 MySQL 是否配置，也不依赖正式追踪是否提前退出。

## 什么时候可以据此改闸门

不要用一两条记录改产线。建议：

1. 至少积累 30 条以上 near-miss 观测（同一阻断组合、同一期限）；
2. 与同期 formal 组、沪深300 做对照；
3. 先看 F20/F60（生产持有窗口 40~60 日），不要只看 F5；
4. 若 near-miss 组显著不差于 formal 组，再把对应闸门的例外做成**默认关闭**的配置开关，
   先影子/AB 跑一轮，再决定是否启用。

## 测试

```bash
python -m unittest tests.test_shadow_ledger -v
```

覆盖：幂等写入、`seen_count`、formal 取代、`valuation_pb` 过滤、NaN 清洗、损坏行跳过、
按阻断组合×期限汇总。
