# 长期优质低估低位策略 - 设计文档

## 一、策略定位

**核心定位**：为3-12个月持有周期的价值投资者筛选"处于低位且开始回升的长期优质低估股"。

### 与原策略对比

| 维度 | 原策略（quality_value） | 长期版（quality_longterm） |
|------|------------------------|--------------------------|
| **持有周期** | 10-20个交易日 | 30-90个交易日 |
| **质量要求** | 连续3年ROE中位≥10% | 连续5年ROE中位≥12%，且稳定 |
| **现金流** | 3年累计现金流/净利润≥0.8 | 5年累计现金流/净利润≥1.0 |
| **止跌判定** | 必须站上MA20或MACD改善 | 同生产 strict 口径（MA20确认或MACD改善且未创新低） |
| **底部窗口** | 低点年龄5-40天 | 低点年龄3-90天（允许充分磨底） |
| **止损/止盈** | -15% / +30% | -20% / +50% |
| **推荐数量** | 牛5/中4/熊2 | 牛3/中2/熊1（更精选） |
| **估值要求** | 行业分位≤60% | 行业分位≤50%（更便宜） |
| **投资资质** | 无专项评估 | 长期投资资质评分（60分准入） |

> **2026-10-13 口径修订说明**（此前文档描述与代码不符，已按实际生效值更正）：
>
> 1. **止跌判定回到 `strict`**。原设计写「只拒绝加速下跌」（`QV_STABILIZATION_MODE="risk_only"`），
>    已撤回。`risk_only` 把「站上 MA20」从否决降级为标签，等于**放行更多票**，与项目
>    「宁缺毋滥」的最高优先级原则冲突；基类实测也表明 strict 保留组前瞻收益更优。
>    「拒绝加速下跌」的意图改由**独立接线**的 `DETECT_ACCELERATING_DECLINE` 承担
>    （MA20 近5日跌幅 > 5% → `FAIL_ACCELERATING_DECLINE`），只拦真正危险的形态，
>    且不影响「未站上 MA20 但也无危险证据」的一批票。
> 2. **底部窗口 120 天**（不是 90）。`QV_RECENT_LOW_MAX_AGE=90` 必须严格小于
>    `QV_RECENT_LOW_WINDOW`，否则基类 `__post_init__` 校验直接抛 ValueError。
> 3. **最低质量分 60 分**（不是 65）。65 与年度资格闸门（中位ROE≥12% + 最低ROE≥8% +
>    现金转换≥0.9）重复计权，会把绝大多数已 verified 的候选二次淘汰，使策略恒 0 推荐。
> 4. **技术分门槛保持 40 分**（不是 30）。技术面在本策略中只做「不追高」的最低约束，
>    下调到 30 会让技术证据不足的票混入正式推荐。
> 5. **删除 PB 豁免类配置**（`ALLOW_PB_FALLBACK` / `PB_FALLBACK_*`）。它们从未被任何
>    代码读取，且方向是**放宽**（PE超标但PB<1.0可豁免）。
> 6. **删除** `QV_LOW_GAIN_MIN_PCT` / `QV_LOW_GAIN_MAX_PCT`：从未被读取，实际距底涨幅
>    由 `QV_LOW_GAIN_ATR_MULT` 经 ATR 自适应给出并夹在 [10%, 20%]。

---

## 二、核心改进详解

### 1. 更严格的质量筛选

#### （1）从3年延长至5年
```python
QUALITY_YEARS: int = 5  # vs 原3年
```
**理由**：
- 3年可能覆盖一个经济周期的上行段，容易将"周期性繁荣"误判为"持续优质"
- 5年能覆盖完整经济周期，真正优质的公司才能穿越周期保持盈利

#### （2）ROE门槛提升
```python
QUALITY_MEDIAN_ROE_MIN: float = 12.0  # vs 原10%
QUALITY_MIN_ROE: float = 8.0  # vs 原5%
```
**理由**：
- ROE=10%刚好覆盖无风险利率+风险溢价，缺乏超额回报空间
- ROE=12%才能在扣除通胀和风险后产生真实价值

#### （3）现金流质量提升
```python
QUALITY_CASH_CONVERSION_MIN: float = 0.9  # vs 原0.8
LONGTERM_CASH_QUALITY_MIN: float = 1.0  # 5年累计经营现金流/净利润
```

#### （4）新增：ROE稳定性检查
```python
REQUIRE_ROE_STABILITY: bool = True
MAX_ROE_CV: float = 0.5  # 变异系数（标准差/均值）上限
```

### 2. 止跌判定：与生产同源 + 独立的加速下跌否决

**核心改变**：在生产 strict 口径之上，**额外**加一条独立的否决项。

```python
QV_STABILIZATION_MODE: str = "strict"   # 与生产同源，不放宽

# 独立否决：只有这一种形态被拦（真正的「下跌仍在加速」）
if MA20近5日跌幅 < -5%:
    return None, "FAIL_ACCELERATING_DECLINE"
```

为什么不直接用 `risk_only`：它把「站上 MA20」从否决降级为标签，等于放行更多票，
与「宁缺毋滥」冲突。而「拒绝加速下跌」的意图用一个**独立、只做减法**的否决项表达更准确。

### 3. 更宽松的底部窗口

```python
QV_RECENT_LOW_MIN_AGE: int = 3   # 5→3天
QV_RECENT_LOW_MAX_AGE: int = 90  # 40→90天
QV_LOW_GAIN_ATR_MULT: float = 8.0  # 5→8倍ATR
```

**允许的底部形态**：
- **V型反转**（3-10天）：快速探底快速反弹
- **W型双底**（20-50天）：二次探底确认支撑
- **箱体磨底**（60-90天）：长期横盘充分换手

### 4. 长期投资资质评分（核心创新）

#### 五大维度评分（0-100分，门槛60分）

| 维度 | 权重 | 评分逻辑 |
|------|------|---------|
| **ROE稳定性** | 25% | CV=0→100分, CV=0.5→0分 |
| **现金流质量** | 25% | 5年累计现金流/净利润：≥1.2→100分, 0.8→0分 |
| **盈利持续性** | 20% | 5年中正增长占比：100%→100分, 60%→0分 |
| **增长质量** | 15% | ROE年增0-2pp最佳（100分），>2pp或<0扣分 |
| **估值安全边际** | 15% | PE行业分位：0%→100分, 50%→0分 |

---

## 三、完整筛选流程

```
1. 基础数据检查（数据/时效/停牌缺口）
   ↓
2. 流动性过滤（日均成交额≥5000万）
   ↓
3. 250日低位（分位≤40%）
   ↓
4. 估值闸门（行业分位≤50% 或 绝对PE≤25）
   ↓
5. 5年质量检查（与生产同源，evaluate_annual_quality）
   - 连续5年ROE中位≥12%、最低≥8%、现金转换≥0.9
   - 各年净利润与经营现金流均为正
   ↓
6. 近期经营 + 财务风险（同生产口径）
   ↓
7. 近期底部确认（3-90天，距底涨幅 ≤ ATR自适应上限[10%,20%]）
   ↓
8. 止跌确认（strict 模式：MA20确认 或 MACD改善且未创新低）
   ↓
9. 加速下跌否决（MA20近5日跌幅>5% → FAIL_ACCELERATING_DECLINE）
   ↓
10. 技术状态评分（≥40分，与生产同口径）
   ↓
11. 长期专项闸门（仅在取到年度财务后执行）
   - ROE稳定性：变异系数≤0.5
   - 盈利持续性：近5年年报的4次同比中至少3次为正
   - 5年累计经营现金流/净利润≥1.0
   - 长期投资资质综合评分≥60
   - 相对强度：近60日跑输沪深300不超过15个百分点
   ↓
12. 质量分下限（≥60）+ 波动率风控（ATR≤6%）
   ↓
13. 排序：55%质量 + 35%估值 + 10%技术
   ↓
14. 组合层：市场级熔断 → regime 数量上限（牛3/中2/熊1）→ 行业分散
   ↓
15. 落库（strategy=longterm_quality）+ 飞书通知（仅 formal）
```

> **注**：第 11 步的年度财务闸门**必须**放在「取到财务数据之后」。
> `_screen_quality_pool` 采用两趟评估——第一趟 `fund_data=None` 做廉价行情预筛，
> 通过后才取财务数据做第二趟。若在第一趟就执行年度财务闸门，此时 `annual_rows` 为空，
> 三项全部判为 insufficient，**每一只股票都会在预筛阶段被否决**，
> 财务数据永远取不到，策略恒定 0 推荐（2026-10-13 修复的正是这个缺陷）。

---

## 四、使用指南

### 基础用法
```bash
# 正常运行
python run_longterm.py

# 对比模式（同时运行原策略和长期版）
python run_longterm.py --compare

# 测试模式（不落库不通知）
python run_longterm.py --no-save --no-notify
```

### 预期效果

| 维度 | 预期变化 |
|------|---------|
| **推荐数量** | 减少40-50%（更精选） |
| **基本面质量** | 显著提升（5年穿越周期） |
| **胜率** | 预期提升10-15个百分点 |
| **持有体验** | 更好（不会因短期波动频繁止损） |
| **适用人群** | 50万以上资金，3-12个月持有周期 |

---

## 五、关键差异总结

### 设计哲学
| 原策略 | 长期版 |
|--------|--------|
| 抓短期反弹 | 长期价值投资 |
| 刚开始止跌（追求精准） | 已充分磨底（追求安全） |
| 技术面反弹 | 价值回归 |
| 止盈30%或持有20天 | 止盈50%或持有90天 |

### 核心改进点
1. ✅ 质量从3年→5年（穿越完整经济周期）
2. ✅ ROE从10%→12%且稳定（真正优质）
3. ✅ 止跌从"必须站上MA20"→"只拒绝加速下跌"（左侧定位回归）
4. ✅ 底部从5-40天→3-90天（允许充分磨底）
5. ✅ 持有从10-20天→30-90天（匹配价值回归周期）
6. ✅ 新增长期投资资质评分（量化"是否适合长期持有"）

---

## 六、风险提示

1. **数据依赖**：需要5年连续财务数据，新股/借壳股无法使用
2. **推荐频率降低**：可能连续多日无推荐（严格筛选的代价）
3. **不适合短线**：30-90天持有期，不适合快进快出
4. **机会成本**：持有期长意味着资金周转慢

---

## 七、技术实现要点

### 新增函数

```python
# 1. ROE稳定性评估
def evaluate_roe_stability(annual_rows, min_years=5) -> dict

# 2. 盈利持续性评估（N 条年报 → N-1 次同比）
def evaluate_earnings_consistency(annual_rows, min_years=5, min_positive_years=3) -> dict

# 3. 长期现金流质量评估
def evaluate_longterm_cash_quality(annual_rows, min_years=5) -> dict

# 4. 长期投资资质综合评分
def compute_longterm_quality_score(annual_rows, pe, pe_pct, config) -> dict

# 5. 加速下跌检测（独立否决项）
def _detect_accelerating_decline(technical, config) -> dict

# 6. 缩量见底检测（仅展示标签，不参与准入）
def _detect_volume_exhaustion(technical, config) -> dict

# 7. 长期版评估主函数
def evaluate_quality_longterm(daily_df, code, name, config, ...) -> tuple[Signal, str]
```

### 配置继承关系

```python
StrategyConfig (生产基础配置)
    ↓ 继承
LongTermQualityConfig (长期版配置)
    ↓ 覆盖关键参数（全部为「收紧」方向）
    - QUALITY_YEARS: 3 → 5
    - QUALITY_MEDIAN_ROE_MIN: 10 → 12
    - QUALITY_MIN_ROE: 5 → 8
    - QUALITY_CASH_CONVERSION_MIN: 0.8 → 0.9
    - MIN_QUALITY_SCORE: 0 → 60
    - VALUATION_INDUSTRY_PERCENTILE_MAX: 0.60 → 0.50
    - HISTORICAL_VALUATION_PERCENTILE_MAX: 0.60 → 0.50
    - MIN_AMOUNT: 3000万 → 5000万
    - MAX_PICKS / NEUTRAL_ / BEAR_: 5/4/2 → 3/2/1
    - HOLD_DAYS_HINT: 10~20 → 30~90
    - FIXED_STOP_LOSS_PCT: 15 → 20；FIXED_TAKE_PROFIT_PCT: 30 → 50
    - QV_RECENT_LOW_*: 60/5/40 → 120/3/90
    - QUALITY/VALUATION/TECHNICAL 权重: 0.45/0.30/0.25 → 0.55/0.35/0.10
    - 新增长期专项: REQUIRE_ROE_STABILITY / REQUIRE_EARNINGS_CONSISTENCY /
      LONGTERM_CASH_QUALITY_MIN / MIN_LONGTERM_QUALITY_SCORE /
      REQUIRE_RELATIVE_STRENGTH / DETECT_ACCELERATING_DECLINE
    - 保持与生产一致（不收紧也不放宽）:
      QV_STABILIZATION_MODE="strict"、MIN_TECHNICAL_SCORE_FORMAL=40、MAX_ATR_PCT=6
```

> **RECOMMENDATION_MODE 必须是 `"quality_value"`**：基类 `_validate_recommendation_config`
> 只接受 `{quality_value, technical}`，填 `"quality_longterm"` 会在**构造时**直接抛
> `ValueError`。本策略走的就是 quality_value 荐股资格（`evaluate_quality_longterm`
> 内部委派 `evaluate_quality_value`）；对外展示名由 `LONGTERM_LABEL` 承载。

---

## 八、后续优化方向

1. **动态持有期**：根据估值修复进度调整持有期
2. **分批建仓**：初始30%，确认趋势后加仓至70%
3. **行业轮动**：根据经济周期调整行业偏好
4. **引入基本面动量**：ROE加速改善的公司优先级更高
5. **前瞻验证**：长期口径（5年质量 + 30~90 天持有）尚未做前瞻收益实测，
   当前只验证了「逻辑自洽 + 不宽于生产口径」，收益有效性待验证

---

**总结**：长期版策略通过提高质量门槛、新增盈利持续性与现金流质量闸门、延长持有周期，
在**完全复用生产 quality_value 筛选骨架**的前提下叠加一层长期专项收紧，
把中短期价值投机重新定位为长期价值投资。所有收紧项均已接线并登记否决归因码，
不存在「配置里写了但代码从不读取」的死配置。

