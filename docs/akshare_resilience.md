# AkShare 通道韧性（2026-10-21）

## 背景

run #111/#112/#113 的日志里，AkShare 备用通道的问题不是"偶尔失败"，而是三个叠加的结构性缺陷：

1. **每次调用新建 Session**：AkShare 1.18 的接口实现是 `requests.get(url, ..., timeout=None)`，
   `requests.get` 每次新建连接。全市场 3000 只股票 = 3000 次 TCP/TLS 握手。
   本机实测同一批 8 只股票：原始实现 **1.63 秒/次**，复用连接后 **0.31 秒/次**。
2. **`timeout` 缺省为 `None`**：服务端不响应时该线程可以无限期卡在 recv 上。
   `socket.setdefaulttimeout(20)` 对 requests 无效（urllib3 会显式把 socket 超时设成 None）。
   run #111 的 600511 就卡在这种状态 1750 秒以上，直到整条 job 被取消。
3. **没有通道级熔断**：东财抖动时，每只股票都要先重试 `MAX_RETRY` 次再走新浪，
   3000 只 = 9000 次无效请求；日志表现为「一直重连」，实际是把一次故障放大成全轮。

## 当前行为（`src/akshare_resilience.py`）

| 层 | 行为 | 参数 |
|---|---|---|
| 连接复用 | 线程内复用 keep-alive `requests.Session`（连接池 8/32），只补 `requests` 与 `curl_cffi`，幂等安装 | — |
| 强制超时 | 调用方未传 `timeout` 时注入默认值；显式传入的 timeout 原样保留 | `AK_HTTP_TIMEOUT_SEC`=20 |
| 通道熔断 | 同一通道连续失败达阈值后冷却，冷却期内**直接跳过**该通道（抛 `ChannelUnavailable`，调用方换备用通道），冷却后放行一次探活 | `AK_CHANNEL_FAIL_THRESHOLD`=3、`AK_CHANNEL_COOLDOWN_SEC`=120 |
| 有限重试 | 单通道最多 `MAX_RETRY+1` 次，指数退避 + 抖动（与 `FETCH_RETRY_*` 同源） | `MAX_RETRY`=2 |

通道划分（按接口主机）：

| 通道 | 接口 | 备用 |
|---|---|---|
| `eastmoney` | 个股日线/周线（`stock_zh_a_hist`）、资产负债表、利润表、股票列表 | `sina` |
| `sina` | 新浪日线（`stock_zh_a_daily`）、指数日线、财务指标、交易日历 | `eastmoney`（仅日线有东财→新浪的隐含顺序） |
| `baidu` | 百度股市通估值（`stock_zh_valuation_baidu`）：逐票 PE(TTM)/PB 历史，供 AkShare 主源补估值列 | 无（缺失则按缺项处理，不放行） |

熔断计数按「一次调用」计（不是按 HTTP 尝试次数）：3 只股票连续失败即判东财不可用，
之后 120 秒内所有股票直接走新浪，恢复后自动重新探活。成功一次即清零连续失败计数。

## 卡死股票放弃（`run_concurrent_screen`）

- 单只股票最多尝试 `STOCK_MAX_ATTEMPTS`（默认 3）次，退避后重试；
- 连续 3 次失败 → 放弃该股、记 `FAIL_DATA`（进否决归因，可查），不再占用线程池；
- 运行末尾打印 `本次放弃 N 只连续 3 次取数失败的个股（记 FAIL_DATA）：代码…`；
- 看门狗告警同时打印在途股票已等待秒数（`600511(1830s)`），定位卡点不再靠猜。

「3 次」与超时是配套的：没有强制超时，卡死的调用永远轮不到第 2 次重试；
没有重试与放弃，超时只会把整轮结论一起拖没。

## 估值补全（AkShare 主源可用性的前提）

AkShare 日线只有 OHLCV，没有 `peTTM/pbMRQ`。缺这两列时估值闸门只能记缺项，候选全部
转 pending、formal 恒为 0——这是「AkShare 只能当兜底」的真正原因，连接问题只是第一层。

`_fetch_daily_ak` 现在多走一步「估值通道」：

1. 逐票调 `stock_zh_valuation_baidu(symbol, indicator, period="近三年")`，取
   `市盈率(TTM)` 与 `市净率`，按 `date` 左连接进日线；
2. 口径核对（2026-10-20 实测）：600000 `6.35/0.43`、600036 `7.07/0.94`、601318
   `5.82/0.90`、000001 `5.11/0.47`、600519 `20.45/6.63`，与 Baostock 的
   `peTTM/pbMRQ` 逐位一致（百度保留两位小数）；
3. 历史序列直接满足 `HISTORICAL_VALUATION_LOOKBACK=250 / MIN_SAMPLES=120`，行业估值
   快照（只用末 bar）也照常工作；
4. **有界顺延**：百度 TTM 常比东财行情晚一个交易日发布（16:10 实测东财已有当日 bar、
   百度最新只到 T-1）。末 bar 缺估值时沿用最近有效值，条件是与末 bar 相距 ≤3 个自然日，
   逐票记 `val_carried=True`，运行末尾汇总 `估值顺延 N 只`；超过 3 天不顺延，宁可
   缺项 pending，也不用过期估值放行；
5. 估值通道不可用时补空列（`peTTM/pbMRQ` 全 NaN）并写进缓存——否则「缓存缺 peTTM」
   会让每次调用都重拉日线，百度熔断期间放大成成倍的东财请求。

代价（20 只实测）：PE 覆盖 20/20、历史分位可核验 19/20、0.49 秒/只，4 线程折算全市场
约 6 分钟；对比 Baostock 主源要过一次 `bs_lock` 串行登录与逐票查询。

## 主数据源切换

```text
DATA_SOURCE_PRIMARY = baostock   # 默认：Baostock 优先，AkShare 兜底
DATA_SOURCE_PRIMARY = akshare    # 反转：AkShare 优先，Baostock 兜底
```

- 代码里改配置，或设同名环境变量（优先级更高）；
- GitHub Actions 两种切换方式，优先级 **手动勾选项 > 仓库变量 > 默认值**：
  - 仓库变量 `Settings → Secrets and variables → Actions → Variables → DATA_SOURCE_PRIMARY`：
    长期生效（定时任务也跟着切）；
  - 手动触发时的勾选项 `data_source_primary`（`inherit`/`baostock`/`akshare`）：只覆盖
    本次运行，`inherit` 表示跟随仓库变量。适合"试一次 AkShare 又不想改仓库配置"；
- 两个策略步骤共用同一解析结果：`Resolve data source primary` 步骤把最终值写入
  `GITHUB_ENV`，`run.py` 与 `run_breakout.py` 读同一个值，不会一半 A 源一半 B 源；
  页面会打印 `::notice::主数据源=akshare（来源：手动触发勾选项 inputs.data_source_primary）`；
- 切到 AkShare 主源时**不预登 Baostock**，省掉每次约 4 秒的登录成本；只有 AkShare
  全程失败时才在路由处懒登录兜底；
- `is_data_degraded()` 已按当前主源判定：AkShare 主源命中时不会因为 `bs_ok=0` 误报降级。

## 日志

```text
数据源: AkShare（主数据源，备用: Baostock；连接复用 + 强制超时 + 通道熔断已启用）
AkShare 通道 eastmoney 连续失败 3 次，熔断 120 秒：期间跳过该通道改用备用通道
日线取数来源: Baostock 0 只，AkShare 兜底 2966 只，双源均失败 0 只，估值顺延 2874 只，估值缺项 0 只
AkShare 通道: eastmoney 成功2966/失败0/跳过0，sina 成功0/失败0/跳过0，baidu 成功5932/失败0/跳过0
本次放弃 4 只连续 3 次取数失败的个股（记 FAIL_DATA）：600511,000963,...
```

## 边界

- **不改变任何选股口径**：只影响"同一接口怎么发请求、失败后怎么办"，以及 AkShare
  主源补齐 Baostock 本来就有的估值列；闸门、排序、分层、冷却全部不动。
- 估值口径差异只有两处：百度保留两位小数（对 `MAX_PE_TTM=25`/`MAX_PB_MRQ=3` 无实质影响），
  以及末 bar 有界顺延（≤3 天，逐票可查）。超过 3 天不做顺延。
- 熔断状态是**进程内**的，跨运行不持久；每次 `main()` 开始重置通道状态。
- 线程内 Session 不跨线程共享（4 个筛选线程 = 4 条 keep-alive 连接），避免共享 cookie/状态。
- 若 AkShare 未来改用 `curl_cffi` 以外的客户端，超时注入需要跟着补一条补丁。

## 测试

```bash
python -m unittest tests.test_akshare_resilience -v   # 20 项，无外网依赖
```

覆盖：超时注入与 Session 复用、重试次数上限、熔断开合与跳过、成功清零、单只股票放弃
（3 次后记 FAIL_DATA）、瞬时失败重试成功、主源路由反转、降级标记按主源判定、估值列
合并与有界顺延、超限不顺延、估值通道熔断、旧缓存（缺 peTTM）判为未命中。
