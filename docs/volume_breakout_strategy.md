# 放量突破选股策略（Volume Breakout）

`VolumeBreakoutConfig.RECOMMENDATION_MODE="technical"` 和 `run_breakout.py` 默认使用独立突破资格。质量入口仍为 `quality_value`，两者共用行情、市场环境、近期经营与财务risk，不能把突破描述成三年质量认证。以下是当前荐股规则；交易计划与独立回测段落保留既有算法说明，本次不修改其算法、不声称实盘或收益改善。

## 当前七层漏斗

| 层 | 证据与处理 |
|---|---|
| 1 基本面 | 初筛继承 `check_fundamentals`；已知硬伤否决。初筛缺失不能视作已核验，信号初始pending；终审补齐财务并验证多维经营和独立risk，金融专项pending |
| 2 行情 | ≥60根有效日线、近20日日均成交额≥3000万、合法且有限OHLCV/成交额/涨幅、有效交易状态；相邻bar自然日缺口>12天否决。独立日历确认应完成日，再核验指数和个股日期 |
| 3 突破 | 默认只认L1近60日高点/L2近20日高点；超越≥0.5%，前后使用同一阻力锚点。量比1.8～4、当日成交额≥1亿、60日量能分位≥80%、成交额≥前20日中位数1.5倍；阳线实体占比≥0.5、收盘/最高≥0.97、涨幅2%～7%、跳空≤2% |
| 3 整理/趋势 | 平台20日振幅≤18%、收盘std/mean≤4%，右端shift(3)避开突破污染；MA20斜率≥0、MA60斜率≥−1%、收盘站上MA20容忍2%；近期假突破否决；首日前一日RSI≤80，启用MACD/KDJ下限闸门 |
| 4 ATR | `MAX_ATR_PCT_BREAKOUT=4`%，超限 `FAIL_VOLATILE`，缺失 `FAIL_DATA`；质量入口共享上限6%，不使用旧3.33/10作为当前比较值 |
| 5 规则分 | 突破35+量能25+平台15+趋势15+动能10，总规则分限制在0～100。等级与准入仍用原规则分，B≥60，熊市提升15；子分与RS有界 |
| 6 终审 | 多维近期经营、同报告期真实公告财务risk、财务补齐与周线条件全部核验，缺项/走弱pending、失败否决；不以初筛PASS替代formal |
| 7 排序 | `ranking_score`（原规则分+有界RS）降序→突破级别降序→20日均成交额降序→代码升序；牛5、中性3、熊0。行业分散及组合层去重/每日总量上限继续生效 |

行情缓存使用CSV旁meta（版本、带时区取数时间、盘中/闭市阶段、末bar日期），mtime/touch不是时效证明。闭市不能复用盘中取得缓存。独立覆盖当日的交易日历给出北京时间应完成交易日，不能由index自身末日期证明index时效；日历或行情不明不能当已核验。

## 首日、站稳与回踩

阻力为事件日前L1/L2（L3默认关闭）。默认 `ALLOW_RESISTANCE_DROP_BREAKOUT=False`，阻力滚动下降不能被当成新突破；判断穿越时前后收盘使用同一锚点。

| 分类 | 证据 |
|---|---|
| `first_breakout` | 首日通过完整量能、形态、趋势、动能、ATR和准入闸门，记录原事件日期与阻力锚点 |
| `held_confirmed` | 在最近5日原事件基础上，之后连续2日收盘超过同一锚点0.5% |
| `retest_confirmed` | 在最近5日原事件基础上，确认日最低价触及锚点上方2%范围，收盘重新超过锚点0.5% |

确认只用事件之后已完成的行情；原事件须截断到事件日重新核验完整原闸门。任何随后收盘跌破原锚点都取消确认，不能换成后来较低阻力。确认日还检查有效行情、交易状态、当前趋势、近期假突破、跳空、MACD/KDJ、当日涨幅与ATR；距锚点超过7%不确认。原事件日期、确认日期、固定锚点分别保留。确认分类通过不等于正式推荐，仍须财务与周线终审。

## 评分与有界RS

突破35分由 `0.55×级别系数+0.45×幅度质量` 构成，L1/L2/L3系数为1/0.75/0.5。幅度质量在1%～3%最佳区间取峰，过大逐步下降，到7%归零，避免单调奖励追高。量能25分拆成放量比子分17.5与整理子分7.5；平台15、趋势15、动能10均按有界质量计算，总分限制0～100。缺证据不能产生无限或负向失控子分。

默认 `USE_BREAKOUT_RELATIVE_STRENGTH=True`，近20日同起止日期计算个股相对市场/行业的涨跌幅差，行业参照至少3个样本。`BREAKOUT_RS_MAX_ADJUSTMENT=3`、`BREAKOUT_RS_FULL_SCALE=10` 将排序调整限制在±3，缺市场/行业证据保持可用维度或中性，不设RS硬准入线。RS只改变 `ranking_score`，不反写规则分、等级或闸门。

MACD否决为柱值/收盘≤−0.5%且连续2日恶化；KDJ为K>85或K≥80且K<D。历史首次突破样本中MACD未触发的诊断，不可扩展成当前全部路径结构性不可达：新增确认日仍重新核验两项。

## 周线AND与财务终审

突破默认 `WEEKLY_MA_BOTH_REQUIRED=True`：收盘站上周线MA10（容忍2%）**且**MA10上行；另按启用开关要求周线MACD柱翻红或绿柱连续2周收窄。只用已收盘周bar，数据缺失/滞后pending。低位technical对照可用MA OR，不能继承其说明来描述突破默认。

近期经营使用最新真实公告报告的收入、合并净利、扣非、经营现金流、毛利率及净利率同期比较。单项下降容忍3%，现金流下降容忍3%且现金转换≥0.8；收入/合并净利/现金流三项同降仍failed。单季/TTM只辅助、不填核心缺失。独立财务risk要求同报告期/真实公告日/来源/合并人民币口径，负债率≤70%、商誉/净资产≤20%、扣非/合并净利≥0.5；缺商誉不能填零。金融专项pending。

详见[荐股调整说明](quality_value_recommendations.md)与[近期经营契约](recent_operating_contract.md)。年度质量真实公告优先、摘要fallback假定必须带标签的规则属于质量入口；不能将假定公告日用在突破近期经营或risk核验。

## 结果与持久化边界

`BreakoutSignal` 携带突破分类、锚点/事件/确认日期、子分、市场/行业RS和排序分。只有formal进入正式通知与推荐落库。MySQL仍用固定列白名单，新增字段目前并未全部持久化；结果对象有字段不等于数据库有完整审计证据。

突破入口复用推荐表和周度追踪链路，`strategy='volume_breakout'`，唯一键 `(rec_date,code,strategy)`；后运行入口按当日已荐股做组合去重与 `DAILY_TOTAL_MAX_PICKS=7` 合计上限。

## 参数速查（本次默认）

| 字段 | 默认 | 作用 |
|---|---|---|
| RECOMMENDATION_MODE | technical | 独立突破入口 |
| WEEKLY_MA_BOTH_REQUIRED | True | 周线MA站上且上行 |
| REQUIRE_L1_OR_L2 | True | 关闭L3 |
| ALLOW_RESISTANCE_DROP_BREAKOUT | False | 禁止阻力回落伪突破 |
| RECENT_BREAKOUT_DAYS | 5 | 原事件确认回看 |
| BREAKOUT_HOLD_DAYS | 2 | 站稳确认日数 |
| RETEST_TOLERANCE | 0.02 | 回踩接近锚点范围 |
| CONFIRM_MAX_EXTENSION_PCT | 7 | 确认日距原锚点上限（%） |
| BREAKOUT_MARGIN_OPTIMAL_LOW / HIGH | 1 / 3 | 幅度评分最佳区间（%） |
| BREAKOUT_MARGIN_SCORE_ZERO | 7 | 大幅追高幅度分归零（%） |
| BREAKOUT_RS_LOOKBACK | 20 | RS回看交易日 |
| BREAKOUT_RS_MAX_ADJUSTMENT | 3 | RS排序影响绝对上限 |
| BREAKOUT_RS_FULL_SCALE | 10 | RS满幅映射百分点 |
| BREAKOUT_INDUSTRY_MIN_PEERS | 3 | 行业参照最低样本 |
| MAX_ATR_PCT_BREAKOUT | 4 | ATR上限（%） |
| REQUIRE_FINANCIAL_RISK / REQUIRE_RECENT_OPERATING | True / True | 独立财务risk和多维经营终审 |

## 离线回归

```shell
python -B -m unittest discover -s tests -p test_recommendation_evidence_upgrades.py -v
python -B -m unittest discover -s tests -p test_financial_evidence_upgrades.py -v
python -B -m unittest discover -s tests -p test_breakout_verification_offline.py -v
python -B -m unittest discover -s tests -p test_strategy_revision.py -v
python -B test_momentum_gates.py
python -B test_breakout_ab.py
```

合成数据/mock验证规则边界与接线，不发通知、不落库；最终通过数量以实际完成日志为准，不证明收益改善。下列既有独立回测不经过 `main_breakout` 财务/周线终审，初筛pending不会自动提升formal，因此不能视为已完整验证本次荐股证据链。

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


### 三、交易计划

突破策略的风控比抄底更紧，因为突破失败的下跌通常快而深：

- **止损**：`max(突破位 Lk − 1×ATR, 现价 × (1 − FIXED_STOP_LOSS_PCT_BREAKOUT=6%))`
  - 取两者较高值，既尊重技术位又保证止损不会低于固定 6% 风险上限
  - ATR 缺失时退回固定 6%
- **止盈**：`min(现价 × (1 + FIXED_TAKE_PROFIT_PCT_BREAKOUT=15%), 现价 + ATR_TAKE_PROFIT_MULT(5) × ATR)`
  - 比抄底 10% 高，趋势启动目标盈利更大；加入 ATR 目标并取更近者，避免高波动股把止盈挂得离场太远
- **风险收益比**：`MIN_RR_RATIO_BREAKOUT` 检查已删除——止损 ≤6%/止盈 15% 下 RR ≥2.5 恒成立，检查永不触发；`rr_ratio` 仅作展示与落库，波动率风控由层 4 的 `MAX_ATR_PCT_BREAKOUT` 承担

---
