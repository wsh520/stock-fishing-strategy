## 放量突破选股策略（Volume Breakout）设计

### 独立回测

可使用 `src/volume_breakout_backtest.py` 对导出的历史日线 CSV 做事件回测，不依赖 Baostock 或 AkShare。输入可以是单个 CSV，也可以是包含多个股票 CSV 的目录。CSV 至少需要 `date,open,high,low,close,volume` 列；`amount`、`pct_chg` 缺失时会由脚本计算，中文列名也会自动识别。每个收盘日只使用当时及之前的数据计算信号，实际在下一交易日开盘成交。

```powershell
.\.venv\Scripts\python.exe -m src.volume_breakout_backtest `
  --input .\data\daily `
  --initial-capital 100000 `
  --position-size 0.2 `
  --max-positions 5 `
  --fee-rate 0.0003 `
  --stamp-duty 0.0005 `
  --slippage-bps 10 `
  --max-holding-days 20 `
  --regime bull `
  --output .\backtest_result.json
```

输出 JSON 包含 `summary`（最终权益、收益率、交易数、胜率等）和 `trades`（每笔交易的入场/出场日期、价格、盈亏及退出原因）。止盈止损使用策略配置中的固定比例；买入和卖出分别计手续费与印花税，并按滑点基点调整成交价。单根 K 线同时触及止盈和止损时采用止损优先的保守假设。仍持仓的头寸按最后收盘价计入最终权益，并在 `open_positions` 中单独列出。

本策略与 `bottom_fishing_strategy.py`（抄底）并列，作为项目的第二支日线选股信号。整体骨架、数据层、基本面防雷、市场环境、决赛圈周线确认、漏斗日志、`--no-cache`、时间预算、看门狗、`Signal` 数据结构全部沿用现有实现，只替换「日线信号 + 评估」层。

设计目标：在**牛市/中性市**捕捉「横盘整理末端 → 放量突破关键阻力位」的趋势启动点；在**熊市**大幅收缩或直接空仓，避免"突破即诱多"。

---

### 一、策略定位与抄底策略的差异

| 维度 | bottom_fishing（抄底） | volume_breakout（放量突破） |
| --- | --- | --- |
| 入场时机 | 下跌末端、底背离、超卖反弹 | 整理末端、放量突破关键位 |
| 位置偏好 | 近 20 日区间下半部（低位） | 近 60 日新高附近（相对高位） |
| 量能要求 | 温和放量（1.0–2.5 倍最佳） | 明显放量（1.8–4.0 倍，历史分位数及成交额比率达标） |
| 趋势背景 | MA20 不下行即可 | MA20 上行、MA60 走平或多头 |
| 止损设置 | 2×ATR（较宽，容忍底部震荡） | 突破位下方 1×ATR（较紧，破位即撤） |
| 止盈设置 | 固定 +10% | 固定 +15%（趋势延续目标更大） |
| 熊市行为 | BEAR_MAX_PICKS=2 收缩 | BEAR_MAX_PICKS=0 直接空仓 |
| 最大波动率 | ATR ≤ 3.33% 现价 | ATR ≤ 4.0% 现价（突破股波动天然更大） |

**核心分歧点**：抄底买"位置低"，突破买"动能强"。抄底允许 MA20 微跌，突破必须 MA20 上行；抄底容忍 RSI 到 65，突破检查突破前一日 RSI ≤ 80（突破日本身就会推高 RSI）；抄底怕"追高"，突破本身就是追高——但用「近期无假突破 + 平台整理充分 + 上影线短」来过滤真正的追高陷阱。

---

### 二、五层过滤漏斗

沿用 `evaluate()` 的原因码风格，每层单独归因，日志漏斗可看到各层淘汰量。

#### 层 1｜基本面防雷（继承）
直接调用 `check_fundamentals(fund_data, config, code, name)`。ROE 年化 ≥ 5%、非金融负债率 ≤ 70%、金融负债率 ≤ 97%、商誉/净资产 ≤ 20%、扣非净利/净利 ≥ 0.5。数据缺失放行不误杀。失败码 `FAIL_FUND`。

#### 层 2｜流动性 & 数据完整性（继承）
- 近 20 日日均成交额 ≥ 5000 万（`MIN_AMOUNT`），僵尸股淘汰，失败码 `FAIL_LIQUIDITY`
- K 线数不足 `MIN_DAYS=60` 直接 `FAIL_DATA`

#### 层 3｜日线技术面（本策略核心）

**3.1 关键阻力位识别**

对每只股票动态计算三级阻力位（不含当日）：

- **L1** = 近 `BREAKOUT_LOOKBACK_HIGH=60` 日最高价 — 长期新高突破，最强信号
- **L2** = 近 `BREAKOUT_LOOKBACK_PLATFORM=20` 日最高价 — 平台突破，中等信号
- **L3** = MA60 均线 — 长期均线突破，最弱信号（用于低位整理后的均线站上）

要求：`close > Lk × (1 + BREAKOUT_MIN_MARGIN=0.005)` 至少对某个 k ∈ {1,2,3} 成立，且 `Lk` 有效（前 N 日数据齐全）。突破级别越高得分越高（L1>L2>L3）。失败码 `FAIL_NO_BREAKOUT`。

**3.2 放量确认**

- 量比 `daily_vol_ratio` ≥ `VOLUME_BREAKOUT_MIN=1.8`（放量下限）
- 量比 ≤ `VOLUME_BREAKOUT_MAX=4.0`（防天量出货）
- 突破日成交额 ≥ `MIN_BREAKOUT_AMOUNT=1e8`（1 亿，大资金介入门槛；与 MIN_AMOUNT 独立，MIN_AMOUNT 是 20 日均值）

失败码 `FAIL_VOL_INSUFFICIENT`（放量不足）/ `FAIL_CLIMAX_VOL`（天量）。

**3.3 K 线形态过滤（防假突破）**

- **阳线实体占比**：`(close − open) / (high − low)` ≥ `MIN_BODY_RATIO=0.5`（避免长上影假突破）
- **收盘接近最高**：`close / high` ≥ `MIN_CLOSE_TO_HIGH=0.97`（避免尾盘跳水）
- **当日涨幅下限**：`pct_chg` ≥ `MIN_BREAKOUT_PCT=2.0`%（有效突破幅度）
- **当日涨幅上限**：`pct_chg` ≤ `MAX_BREAKOUT_PCT=7.0`%（涨停/接近涨停买不进，且次日易回调）
- **跳空高开上限**：`open / prev_close − 1` ≤ `MAX_GAP_UP_PCT=3.0`%（大幅跳空追买风险大，继承）

失败码 `FAIL_FAKE_BREAKOUT`（上影长/尾盘跳水）/ `FAIL_CHASE`（追高）/ `FAIL_GAP`（跳空）。

**3.4 突破前平台整理确认**

要求突破发生前有可辨识的横盘整理，避免"单边上涨末端的最后一冲"被误判为突破：

- 近 `PLATFORM_LOOKBACK=20` 日振幅 `(max_high − min_low) / min_low` ≤ `MAX_PLATFORM_RANGE=0.18`（18%）
- **平台期收盘价离散度**：`std(close) / mean(close)` ≤ `MAX_PLATFORM_TIGHTNESS=0.04`（4%），窗口右端点固定在 `shift(PLATFORM_SHIFT=3)` 处，确保度量的是「突破发生前」的整理质量而非突破后的波动

> 设计变更记录：初版用「短期 ATR / 长期 ATR ≤ 1.0」做波动率压缩门槛，合成数据测试发现突破本身会抬高 ATR，多日突破行情下该比值恒 >1，正常突破被误杀。改为 platform_tightness（std/mean），窗口右端点 shift(3) 避开突破 bar 污染。`atr_compression` 字段保留供诊断展示，不再参与否决。

失败码 `FAIL_NO_PLATFORM`（振幅过大）/ `FAIL_PLATFORM_LOOSE`（离散度过大）。

**3.5 趋势背景**

- MA20 上行：`ma20 / ma20.shift(MA20_TREND_LOOKBACK=5) − 1` ≥ `MA20_TREND_MIN_SLOPE_BREAKOUT=0.0`（突破策略比抄底严格，抄底是 −0.04）
- MA60 走平或上行：`ma60_slope` ≥ `MA60_TREND_MIN_SLOPE=-0.01`
- 现价站上 MA20：`close ≥ ma20 × 0.98`（容忍 2%）

失败码 `FAIL_TREND_DOWN`。

**3.6 近期假突破过滤**

近 `FAILED_BREAKOUT_LOOKBACK=10` 日内，若曾出现"收盘突破 L1 后 1～3 个交易日内跌回突破日 L1 下方"，则本次突破视为重复诱多，直接否决。失败码 `FAIL_RECENT_FAILED_BREAKOUT`。

**3.7 RSI 上限（检查突破前一日）**

`rsi14.shift(1)` ≤ `DAILY_RSI_ENTRY_MAX_BREAKOUT=80`。检查的是**突破前一日**的 RSI，而非突破日：

- 突破日单日大涨会把 RSI14 从 ~50 推到 80+（极度窄幅整理后甚至到 95+），这是突破信号的预期行为，不应拦截
- 若突破前 RSI 已 >80，说明股票已连涨多日、突破偏晚（追高风险），否决
- 若突破前 RSI 正常、仅因突破日单日大涨而飙高，放行

> 设计变更记录：初版检查突破日 RSI ≤ 75，合成数据测试发现极度窄幅整理后单日突破会把 RSI 推到 98，合法 L1 突破（评分 85.5 A级）被误杀。改为检查 shift(1) 并放宽到 80。

失败码 `FAIL_RSI_HIGH`。

#### 层 4｜波动率风控

ATR / 现价 ≤ `MAX_ATR_PCT_BREAKOUT=4.0`%。突破股天然波动更大，比抄底 3.33% 略放宽。ATR 缺失放行。失败码 `FAIL_VOLATILE`。

#### 层 5｜综合评分与等级

评分维度（合计 100 分，`W_*` 与抄底风格对齐）：

- **W_BREAKOUT=35**：突破级别（L1=1.0 / L2=0.75 / L3=0.5）× 突破幅度 smoothstep（0.5%–5% 从 0 到 1）
- **W_VOLUME=25**：`0.7 × 放量质量 + 0.3 × 整理质量`（加权加法，避免单维度归零拖垮整体）。放量质量 = 量比 smoothstep（1.8–2.5 上升到峰、2.5–4.0 平滑下降、>4 归零）；整理质量 = platform_tightness 反向 smoothstep（std/mean 越小分越高）。初版用乘法 `vol_quality × compression_quality`，合成数据测试发现多日突破行情下 compression_quality 恒为 0（突破本身抬高 ATR），导致 volume_br_score 整体归零，改为加权加法
- **W_PATTERN=15**：平台振幅越小分越高（0–18% 反向 smoothstep）+ 波动率压缩比
- **W_TREND=15**：MA20 斜率 + MA60 斜率 + 多头排列加分（close > MA20 > MA60）
- **W_MOMENTUM=10**：MACD 金叉/柱体放大 + KDJ 未高位钝化（K ≤ 80）+ RSI 未在超买

等级门槛 `MIN_PASS_GRADE="B"`（≥60 分）；熊市 `BEAR_GRADE_BOOST=15`（比抄底 10 更严，因为突破在熊市胜率显著更低）。

#### 层 6｜决赛圈周线确认（继承）

复用 `check_weekly_trend` + `check_weekly_macd`：
- 收盘站上周线 MA10（容忍 2%）且 MA10 上行
- 周线 MACD 柱翻红或绿柱连续 2 周收窄
- `WEEKLY_REQUIRE_CLOSED_BAR=True` 剔除本周未收盘 bar，避免周内漂移

#### 层 7｜排序截取

`SORT_BY = ["score", "breakout_level", "avg_amount", "code"]`，`SORT_ASC = [False, False, False, True]`。突破级别作为次级键，评分并列时优先 L1（60 日新高）。截取前 `MAX_PICKS` 只：

- 牛市 5 只
- 中性市 3 只（`NEUTRAL_MAX_PICKS`）
- 熊市 0 只（`BEAR_MAX_PICKS_BREAKOUT=0`，直接空仓）

---

### 三、交易计划

突破策略的风控比抄底更紧，因为突破失败的下跌通常快而深：

- **止损**：`max(突破位 Lk − 1×ATR, 现价 × (1 − FIXED_STOP_LOSS_PCT_BREAKOUT=6%))`
  - 取两者较高值，既尊重技术位又保证止损不会低于固定 6% 风险上限
  - ATR 缺失时退回固定 6%
- **止盈**：`现价 × (1 + FIXED_TAKE_PROFIT_PCT_BREAKOUT=15%)`
  - 比抄底 10% 高，趋势启动目标盈利更大
- **风险收益比**：要求 `MIN_RR_RATIO_BREAKOUT=1.5`，避免止损过宽而收益不足的交易计划

---

### 四、与现有基础设施的对接

**复用的函数/常量**（直接 `from src.bottom_fishing_strategy import ...`）：

- 数据层：`get_daily_data`、`get_index_daily`、`get_fundamentals`、`get_stock_list`、`_fetch_weekly_dual`
- 市场环境：`get_market_environment`、`compute_market_environment`、`_effective_regime`
- 基本面：`check_fundamentals`、`_is_financial_stock`
- 周线确认：`check_weekly_trend`、`check_weekly_macd`、`_drop_incomplete_weekly_bar`
- 缓存：`CacheManager`
- Baostock 生命周期：`_bs_login`、`_bs_logout`、`_bs_state`
- 统计：`fetch_stats`（Baostock/AkShare 兜底计数）

**独立的部分**：

- `VolumeBreakoutConfig`：继承 `StrategyConfig` 全部字段，新增/覆盖突破专用阈值
- `compute_breakout_signals(df, config)`：核心信号计算，替代 `compute_daily_signals`
- `evaluate_breakout(...)`：评估函数，替代 `evaluate`
- `main_breakout(...)`：编排函数，替代 `main`
- `BreakoutSignal` dataclass：与 `Signal` 字段兼容（保证 `save_recommendations` 直接可用），额外携带 `breakout_level`、`breakout_margin`、`platform_range` 用于排序与展示

**MySQL 落库**：`run_breakout.py` 当前复用现有 `stock_recommendation` 表和周度追踪链路。该表唯一键是 `(rec_date, code)`，若同一天同一只股票同时被抄底与突破策略选中，后写入的一条会由 `INSERT IGNORE` 跳过；若需要分别统计两种策略，应后续增加 `strategy` 字段并同步迁移追踪查询。

---

### 五、参数速查表（VolumeBreakoutConfig 新增/覆盖字段）

| 字段 | 值 | 说明 |
| --- | --- | --- |
| BREAKOUT_LOOKBACK_HIGH | 60 | L1 长期新高回看窗口（交易日） |
| BREAKOUT_LOOKBACK_PLATFORM | 20 | L2 平台突破回看窗口 |
| BREAKOUT_MA_LONG | 60 | L3 长期均线周期 |
| BREAKOUT_MIN_MARGIN | 0.005 | 突破关键位的最小超越幅度（0.5%） |
| VOLUME_BREAKOUT_MIN | 1.8 | 突破日量比下限 |
| VOLUME_BREAKOUT_PEAK | 2.5 | 量能评分峰值 |
| VOLUME_BREAKOUT_MAX | 4.0 | 量能评分归零点（>4 视为过度放量） |
| MIN_BREAKOUT_AMOUNT | 1e8 | 突破日成交额下限（元） |
| MIN_BODY_RATIO | 0.5 | 阳线实体占全天振幅比例下限 |
| MIN_CLOSE_TO_HIGH | 0.97 | 收盘/最高 下限（防尾盘跳水） |
| MIN_BREAKOUT_PCT | 2.0 | 突破日最小涨幅（%） |
| MAX_BREAKOUT_PCT | 7.0 | 突破日最大涨幅（%，防追高） |
| PLATFORM_LOOKBACK | 20 | 平台整理判定窗口 |
| PLATFORM_SHIFT | 3 | 平台度量右端点偏移（避开突破 bar 污染） |
| MAX_PLATFORM_RANGE | 0.18 | 平台期最大振幅（18%） |
| MAX_PLATFORM_TIGHTNESS | 0.04 | 平台期收盘价 std/mean 上限（4%） |
| ATR_COMPRESSION_LOOKBACK_SHORT | 10 | 短期 ATR 均值窗口（仅诊断，不参与否决） |
| ATR_COMPRESSION_LOOKBACK_LONG | 30 | 长期 ATR 均值窗口（仅诊断） |
| ATR_COMPRESSION_RATIO | 1.5 | 短期/长期 ATR 上限（仅诊断，突破本身会抬高 ATR） |
| MA20_TREND_MIN_SLOPE_BREAKOUT | 0.0 | MA20 斜率下限（突破比抄底严） |
| MA60_TREND_MIN_SLOPE | -0.01 | MA60 斜率下限 |
| MA60_TREND_LOOKBACK | 20 | MA60 独立趋势回看窗口 |
| FAILED_BREAKOUT_LOOKBACK | 10 | 近期假突破回看窗口 |
| FAILED_BREAKOUT_CONFIRM_DAYS | 3 | 突破后确认失败的交易日窗口 |
| DAILY_RSI_ENTRY_MAX_BREAKOUT | 80.0 | RSI 入场上限（检查突破前一日 shift(1)，突破日 RSI 天然飙高不拦截） |
| MAX_ATR_PCT_BREAKOUT | 4.0 | 波动率上限（%，比抄底 3.33 放宽） |
| FIXED_STOP_LOSS_PCT_BREAKOUT | 6.0 | 固定止损（%，ATR 缺失时用） |
| FIXED_TAKE_PROFIT_PCT_BREAKOUT | 15.0 | 固定止盈（%） |
| ATR_STOP_MULT_BREAKOUT | 1.0 | 突破位下方 ATR 止损倍数 |
| MIN_RR_RATIO_BREAKOUT | 1.5 | 计划收益风险比最低要求 |
| ADAPTIVE_VOLUME_LOOKBACK | 60 | 个股历史量能分位数窗口 |
| MIN_VOLUME_PERCENTILE | 0.80 | 突破日最低历史量能分位数 |
| MIN_AMOUNT_RATIO | 1.5 | 突破日成交额/前20日中位数最低比率 |
| BEAR_MAX_PICKS_BREAKOUT | 0 | 熊市直接空仓 |
| NEUTRAL_MAX_PICKS | 3 | 中性市推荐上限 |
| BEAR_GRADE_BOOST_BREAKOUT | 15.0 | 熊市评分门槛提升（比抄底 10 严） |
| W_BREAKOUT | 35.0 | 突破强度权重 |
| W_VOLUME_BR | 25.0 | 量能质量权重 |
| W_PATTERN | 15.0 | 平台整理权重 |
| W_TREND_BR | 15.0 | 趋势背景权重 |
| W_MOMENTUM_BR | 10.0 | 动能确认权重 |

---

### 六、验证方式

1. **单元冒烟**：`python -c "from src.volume_breakout_strategy import main_breakout; print(main_breakout())"`（在缓存有效的交易日应能返回 DataFrame 或 None）
2. **与抄底结果对比**：同一天跑 `run.py` 和 `run_breakout.py`，检查两个策略的推荐集是否有交集、交集股票的评分差异（交集股票往往是"底部放量突破"的双信号，值得重点关注）
3. **历史回溯**（可选）：把 `_window_dates` 临时改为固定历史日期，跑 2024/2025 年几个典型突破行情日，人工核对推荐结果
4. **漏斗日志**：观察 `FAIL_NO_BREAKOUT`、`FAIL_VOL_INSUFFICIENT`、`FAIL_NO_PLATFORM`、`FAIL_FAKE_BREAKOUT` 的淘汰占比，判断参数是否过严或过松

---

### 七、已知局限

- **L3（MA60 突破）** 在低位整理股上容易触发，但这类"突破"往往只是反弹，胜率显著低于 L1/L2。可通过 `W_BREAKOUT` 权重差异让 L3 评分自然偏低，也可用 `REQUIRE_L1_OR_L2=True` 开关直接关闭 L3。
- **平台整理判定** 只看振幅和 ATR 压缩，不识别形态学（杯柄、双底、三角形收敛）。后续可引入 `scipy.signal.find_peaks` 做形态识别。
- **突破日成交额下限 1 亿** 对小盘股偏严，`MAIN_BOARD_ONLY=True` 下大部分主板股能满足，但若开启创业板需下调或按流通市值自适应。
- **熊市空仓（BEAR_MAX_PICKS_BREAKOUT=0）** 会错过熊市末期的"反转突破"，但那类信号本身胜率不高，用空仓规避是合理代价。若需捕捉反转，可加"熊市末端豁免"开关（指数 RSI < 30 且斜率拐头时恢复 1 只）。
