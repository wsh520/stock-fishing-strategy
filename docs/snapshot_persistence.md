# 行业估值快照：预算与持久化（2026-10-19）

## 背景

`build_industry_valuation_snapshot` 原本每次运行都遍历全 A 拉日线，构建「行业 -> 当日
PE/PB 横截面」。run #108/#109 实测冷启动 **150.1 / 225.6 分钟**，而实际筛选只要约
1.8 分钟。

2026-10-17 加了 `SNAPSHOT_TIME_BUDGET_MIN=60`：超预算就放弃快照，回退 PE 绝对阈值。
方向没错（避免 6 小时 job 被杀），但 60 分钟在 150/225 分钟面前**必然中途放弃**，
等于把生产估值口径从「行业分位 ≤60%」静默换成「PE ≤25」。定时工作流又会清理
`daily_*` 缓存，所以每次定时运行都是冷启动。

## 当前口径

1. **预算 300 分钟**（`SNAPSHOT_TIME_BUDGET_MIN`，原 60）。
   - 覆盖已验证的最坏情况 225.6 分钟；
   - 300 + 筛选 2 分钟仍低于 `daily_screen` 的 360 分钟 job 上限；
   - 若数据源更慢，300 分钟处仍会放弃并显式 warning，不会拖死 job。
2. **快照持久化**：成功构建后写入
   `cache/industry_valuation_snapshot_v1.json`，后续运行在
   `SNAPSHOT_CACHE_MAX_AGE_DAYS`（默认 7 个日历天）内直接复用。
   - 定时清理只删 `daily_*` / `weekly_*` / `index_daily_*`，不删该文件；
   - Actions cache 会随 `cache/` 一起保存/恢复；
   - 行业 PE/PB 分位是**横截面排名**，不是逐日时序信号；T-N 复用不改变“相对便宜/贵”
     的排序，但把 150~225 分钟冷启动摊薄到约每周一次。
3. **完整性优先**：缓存带 `version`、`snapshot_day`、`built_at`、`industry_digest`、
   `industry_count`。以下情况一律忽略缓存并重建：
   - 版本不兼容；
   - 行业分类 digest 变化（分组不可比）；
   - 快照日超过 `SNAPSHOT_CACHE_MAX_AGE_DAYS`；
   - JSON 损坏 / 解码为空。
4. **不将就部分横截面**：超预算仍返回空 dict，回退绝对阈值并打 warning；不会用截断
   样本计算分位（宁缺毋滥）。

## 配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `SNAPSHOT_TIME_BUDGET_MIN` | `300.0` | 分钟；`<=0` 关闭预算。长期工作流在 `run_longterm.py` 内压到 120（job 上限 180） |
| `SNAPSHOT_CACHE_MAX_AGE_DAYS` | `7.0` | 日历天；`<=0` 关闭复用 |
| `SNAPSHOT_PROGRESS_LOG_EVERY` | `200` | 进度日志间隔 |

> **长期 sleeve 的预算不同**：`daily_screen_longterm` job 上限 180 分钟，
> `run_longterm.py` 在构造配置后把 `SNAPSHOT_TIME_BUDGET_MIN` 压到 120，给筛选/取数/
> 落库/通知留约 60 分钟。冷启动若 120 分钟内未完成，仍回退绝对阈值并 warning；命中
> 快照缓存时不受影响。

## 日志

- 命中缓存：
  `行业估值快照复用缓存：快照日 2026-10-16（T-1 天），81 个行业；跳过 2963 只逐股拉取`
- 构建成功：
  `行业估值快照：81 个行业纳入横截面（行业相对估值闸门生效）...`
- 持久化：
  `行业估值快照已持久化：.../industry_valuation_snapshot_v1.json（快照日 ...，81 个行业）`
- 过期/分类变化：显式 `行业估值快照缓存已过期...重建` / `...行业分类与当前不一致...重建`
- 超预算：`行业估值快照因超过时间预算 ... 而放弃 ... 本次回退绝对估值阈值`

## 边界

- 缓存快照的 **`snapshot_day` 可能早于当日**（T-N）。估值口径仍是行业相对分位，但
  分位横截面是 T-N 的；日志会显式打印快照日，便于与当日绝对口径区分。
- 若当日是冷启动且无缓存，构建仍需 150~225 分钟；这是数据源逐股拉取的固有成本，
  持久化只保证“不是每天付一次”。
- 缓存文件在 `cache/`，本地 `--no-cache` 运行不读不写；`cache=None` 的纯函数测试
  也不碰磁盘。

## 测试

```bash
python -m unittest tests.test_snapshot_persistence -v
python -m unittest tests.test_snapshot_time_budget -v
```

覆盖：构建后持久化、命中缓存跳过逐股拉取、过期重建、行业分类变化重建、`max_age=0`
关闭复用、`cache=None` 不写盘、损坏 JSON 重建、默认预算 ≥240。
