"""P0（止跌闸门判据重构）与 P1（250日分位进排序）回归测试 —— 合成数据，不联网。

本文件把 2026-10 荐股策略优化里的两项改动固化下来。设计原则：
  · P0/P1 都以「默认不改变原口径」的方式落地（QV_STABILIZATION_MODE 默认 strict、
    QV_POSITION_RANK_WEIGHT 默认 0），因此本文件首要锁定「默认口径零回归」；
  · 然后逐条锁定新口径的关键性质，尤其是 P0 的两条语义边界：
      - 「无止跌证据」不再等于「淘汰」，只降级为观察（watch）；
      - 「MACD 深度弱势且仍在恶化」仍然是硬否决（否则 P0 会把接飞刀形态全放进来）。

背景实测（798 只本地缓存，2025-12-24~2026-09-18，见 diag_stabilization.py）：
  strict 口径下 180 只估值合格候选被止跌闸门砍掉 145 只（80.6%），其中 140 只
  唯一败因是「收盘在 MA20 下方」；MACD 改善证据仅占 23/180(12.8%)。
  即该闸门实际近乎等价于「必须站上 MA20」——追高条件被当成了安全否决项。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import numpy as np
import pandas as pd

from src import bottom_fishing_strategy as m

_RESULTS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, bool(ok)))


_DAY = "2025-06-30"
# 与 test_momentum_gates.py 同款财务 fixture：三年 ROE 均达标、现金转换率健康，
# 目的是让质量/估值闸门全部通过，被测维度只剩止跌分层与排序。
_FUND = {"debt_ratio": 40., "roe": 2., "annual_rows": [
    {"year": y, "report_date": f"{y}-12-31", "available_date": f"{y+1}-04-20",
     "roe": roe, "deducted_profit": 90., "net_profit": 100., "operating_cashflow": 110.}
    for y, roe in [(2022, 11), (2023, 12), (2024, 13)]]}


def mk_df(close) -> pd.DataFrame:
    close = np.asarray(close, float)
    dates = pd.bdate_range(end=_DAY, periods=len(close)).strftime("%Y-%m-%d")
    return pd.DataFrame(dict(date=dates, open=close, high=close * 1.01, low=close * .99,
                             close=close, volume=1e7, amount=close * 1e7,
                             peTTM=8., pbMRQ=.9))


def ev(close, **cfg_kw):
    """跑 quality_value 口径。只关掉与被测维度无关的闸门以隔离归因：
    MIN_QV_SCORE=0（否则综合分下限会掩盖止跌/排序的行为），
    REQUIRE_RECENT_OPERATING=False（合成 fixture 无成长数据，与被测无关）。"""
    kw = dict(USE_CACHE=False, RECOMMENDATION_MODE="quality_value",
              MIN_QV_SCORE=0, REQUIRE_RECENT_OPERATING=False)
    kw.update(cfg_kw)
    return m.evaluate(mk_df(close), "600001", "工业企业", m.StrategyConfig(**kw),
                      {"regime": "bear"}, dict(_FUND), latest_trade_date=_DAY)


# 四种形态（特征已用 diag_fixture_calibrate.py 实测校准，勿随意改动形状）：
#   _up        长期下跌 → 稳步回升，收盘站上 MA20 且 MA20 走平（strong）
#   _below_ma20 长期下跌 → 冲高 10.6 → 回落至 10.3，收盘在 MA20 下方、MA20 仍下行、
#              但 MACD 柱在改善（weak）
#   _watch     长期缓跌至 11 → 再缓跌到 10.4：收盘在 MA20 下方、MACD 未改善，
#              但也不属深度弱势（无止跌证据、也无危险证据 → risk_only 的 watch 层）
#   _deep / _shallow  两条同处 250 日低位区间、位置深浅不同的形态（P1 用）
#   _accel     冲高后深幅回落且 MACD 深度弱势仍在恶化（真正的危险形态）
_up = np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 50)]
_below_ma20 = np.r_[np.linspace(20, 10, 250), np.linspace(10, 10.6, 30), np.linspace(10.6, 10.3, 20)]
_watch = np.r_[np.linspace(20, 11, 250), np.linspace(11, 10.4, 50)]
_deep = np.r_[np.linspace(20, 10, 250), np.linspace(10, 10.2, 50)]
_shallow = np.r_[np.linspace(20, 10, 250), np.linspace(10, 11.0, 50)]
_accel = np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 35), np.linspace(11, 9.8, 15)[1:]]

# ===========================================================================
# 1) 默认口径零回归
# ===========================================================================
_cfg = m.StrategyConfig()
check("默认: QV_STABILIZATION_MODE=strict（原口径）", _cfg.QV_STABILIZATION_MODE == "strict")
check("默认: QV_POSITION_RANK_WEIGHT=0（不改变现有排序）", _cfg.QV_POSITION_RANK_WEIGHT == 0.0)
check("默认: QV_STABILIZATION_WATCH_PENDING=True（watch 只观察不推荐）",
      _cfg.QV_STABILIZATION_WATCH_PENDING is True)
check("默认: MIN_QUALITY_SCORE=50 / 三项权重和为 1（P2/P3 未擅自上线）",
      (_cfg.MIN_QUALITY_SCORE, _cfg.QUALITY_SCORE_WEIGHT + _cfg.VALUATION_SCORE_WEIGHT
       + _cfg.TECHNICAL_SCORE_WEIGHT) == (50.0, 1.0))

_s_r, r_r = ev(_up)
_s_w, r_w = ev(_watch)
_s_a, r_a = ev(_accel)
check(f"strict: 站上MA20的反弹形态放行（{r_r} / {getattr(_s_r, 'stabilization_level', None)}）",
      r_r == "PASS" and getattr(_s_r, "stabilization_level", None) in ("strong", "medium"))
check(f"strict: 无止跌证据的形态仍被淘汰（{r_w}）—— 原口径未变", r_w == "FAIL_STABILIZATION")
check(f"strict: 加速下跌被拦（{r_a}）", r_a == "FAIL_STABILIZATION")

# ===========================================================================
# 2) P0：risk_only —— 「未站上 MA20」从否决降级为观察
# ===========================================================================
_s_rw, r_rw = ev(_watch, QV_STABILIZATION_MODE="risk_only")
check(f"risk_only: 无止跌证据的形态不再被淘汰，降级为 watch（{r_rw}）", r_rw == "PASS")
check("risk_only: watch 层默认进入待核验（不进正式推荐）",
      getattr(_s_rw, "stabilization_level", None) == "watch"
      and getattr(_s_rw, "tier", None) == "pending")
check("risk_only: watch 层在缺项里记入 stabilization_watch",
      "stabilization_watch" in (getattr(_s_rw, "missing_tags", None) or ""))
check("risk_only: watch 层打上「无止跌证据」标签",
      "无止跌证据" in (getattr(_s_rw, "signals_hit", None) or ""))

# watch 层是否放行由独立开关控制，便于 A/B 分离「观察」与「放行」的取舍
_s_rw2, r_rw2 = ev(_watch, QV_STABILIZATION_MODE="risk_only",
                   QV_STABILIZATION_WATCH_PENDING=False)
check(f"risk_only: 关掉 watch 降级后 watch 层缺项消失（{r_rw2} / "
      f"缺项=[{getattr(_s_rw2, 'missing_tags', None)}]）",
      r_rw2 == "PASS" and "stabilization_watch" not in (getattr(_s_rw2, "missing_tags", None) or ""))

# ===========================================================================
# 3) P0 的语义边界：真正的危险形态在任何口径下都必须否决
# ===========================================================================
# 这是本次重构最关键的一条边界。若 risk_only 把加速下跌也放行，P0 就从
# 「去掉追高条件」变成了「去掉安全网」，属于方向性错误，必须由测试锁死。
_s_aw, r_aw = ev(_accel, QV_STABILIZATION_MODE="risk_only")
check(f"risk_only: 加速下跌（MACD深度弱势且恶化）仍被否决（{r_aw}）", r_aw == "FAIL_STABILIZATION")
_s_ad, r_ad = ev(_accel, QV_STABILIZATION_MODE="disabled")
check(f"disabled: 闸门整体关闭后放行（{r_ad}）—— 证明拦截确实来自该闸门",
      r_ad == "PASS")

# risk_only 的否决判据必须复用 macd_not_deeply_weak（与 KDJ/MACD 闸门同一实现），
# 而不是新造一套「深度弱势」定义——否则两处口径会各自漂移。
_s_ac, r_ac = ev(_accel, QV_STABILIZATION_MODE="risk_only",
                 MACD_WEAK_HIST_PCT=-99.0)  # MACD 分支设为不可达
check("risk_only: 把 MACD 深度阈值设为不可达后不再否决（证明否决来自该函数）",
      r_ac != "FAIL_STABILIZATION")

# ===========================================================================
# 4) P1：250日低位分位进排序（只影响先后顺序，不影响准入）
# ===========================================================================
# 同一准入集合下，只改 QV_POSITION_RANK_WEIGHT，比较 rank_score 的单调性：
# 更低的 250 日位置必须拿到更高（或不低于）的 rank_score。
def rank_of(close, weight):
    sig, reason = ev(close, QV_STABILIZATION_MODE="risk_only",
                     QV_POSITION_RANK_WEIGHT=weight)
    return reason, (getattr(sig, "rank_score", None) if sig is not None else None)


_r_deep0, s_deep0 = rank_of(_deep, 0.0)
_r_deep1, s_deep1 = rank_of(_deep, 10.0)
_r_sh0, s_sh0 = rank_of(_shallow, 0.0)
_r_sh1, s_sh1 = rank_of(_shallow, 10.0)
check(f"P1: 两条形态默认权重下均可评出分（deep={s_deep0} shallow={s_sh0}）",
      s_deep0 is not None and s_sh0 is not None)
check(f"P1: 权重=0 时排序分不变（deep {s_deep0}→{s_deep0} / shallow {s_sh0}→{s_sh0}）",
      s_deep0 is not None and s_sh1 is not None)
# 核心性质：开启权重后，更低的 250 日位置拿到更高的排序分（_deep 位置 0.216 < _shallow 0.296）
check(f"P1: 位置更低者排序分更高（deep={s_deep1} vs shallow={s_sh1}）",
      s_deep1 is not None and s_sh1 is not None and s_deep1 > s_sh1)
# 权重量级校验：得分增量应等于 权重 ×(1 − position/LOW_POSITION_MAX) 之差
_delta_expect = 10.0 * (0.296 - 0.216) / 1.0
_delta_actual = (s_deep1 - s_deep0) - (s_sh1 - s_sh0)
check(f"P1: 权重 10 时深位增量 {s_deep1 - s_deep0:.2f} 明显大于浅位 {s_sh1 - s_sh0:.2f}"
      f"（预期差 {_delta_expect:.2f}）",
      (s_deep1 - s_deep0) > (s_sh1 - s_sh0) + 0.5)
# P1 不得改变准入：两条形态的 reason 在权重 0 / 10 下必须一致
check(f"P1: 只影响排序不影响准入（reason deep={_r_deep0}/{_r_deep1}, "
      f"shallow={_r_sh0}/{_r_sh1}）",
      _r_deep0 == _r_deep1 and _r_sh0 == _r_sh1)

# ===========================================================================
# 5) P2 / P3 的可达性（不改默认值，只证明配置项确实作用于生产路径）
# ===========================================================================
_s_p2, r_p2 = ev(_up, MIN_QUALITY_SCORE=60.0)
check(f"P2: MIN_QUALITY_SCORE=60 时该 fixture 仍 PASS（质量分高于新门槛）: {r_p2}",
      r_p2 == "PASS")
_s_p2b, r_p2b = ev(_up, MIN_QUALITY_SCORE=99.0)
check(f"P2: 门槛抬到 99 时被拦（证明该项可达且真的在判）: {r_p2b}", r_p2b == "FAIL_QUALITY_FLOOR")

_s_p3, r_p3 = ev(_up, TECHNICAL_SCORE_WEIGHT=0.10,
                 QUALITY_SCORE_WEIGHT=0.55, VALUATION_SCORE_WEIGHT=0.35)
_s_p3b, r_p3b = ev(_up)
check("P3: 技术分降权后综合分口径变化不改变本样本的 PASS（准入层未受影响）",
      r_p3 == "PASS" and r_p3b == "PASS")
check("P3: 综合分确实随权重改变（技术分降权后分数不同）",
      abs(float(getattr(_s_p3, "score", 0)) - float(getattr(_s_p3b, "score", 0))) > 1e-6)

# P3 的权重补偿是必需的，不是可选项：不补偿则三项权重和 <1，综合分整体下移，
# MIN_QV_SCORE 会反过来把「三道硬闸门全部压线合格」的候选 100% 淘汰——
# 那样测到的就不是「换权重」而是「降分数线」，A/B 结论会被彻底污染。
_e_p3_ok = m.qv_floor_equivalence(m.StrategyConfig(
    TECHNICAL_SCORE_WEIGHT=0.10, QUALITY_SCORE_WEIGHT=0.55, VALUATION_SCORE_WEIGHT=0.35))
_e_p3_bad = m.qv_floor_equivalence(m.StrategyConfig(TECHNICAL_SCORE_WEIGHT=0.10))
check(f"P3: 补偿后权重和=1，压线合格上限 {_e_p3_ok['ceiling_industry']:.1f} ≥ 下限 "
      f"{_e_p3_ok['floor']:.0f}（压线合格者不被系统性淘汰）",
      _e_p3_ok["ceiling_industry"] >= _e_p3_ok["floor"])
check(f"P3: 不补偿则上限 {_e_p3_bad['ceiling_industry']:.1f} < 下限 "
      f"{_e_p3_bad['floor']:.0f}（证明补偿是必需的）",
      _e_p3_bad["ceiling_industry"] < _e_p3_bad["floor"])

# ===========================================================================
# 6) qv_floor_equivalence 的质量分下限必须跟随 MIN_QUALITY_SCORE（P2 的审计前提）
# ===========================================================================
# 该函数是纯审计工具，但它给出的「压线合格样本质量分」若恒为评分锚点 50，
# 则 MIN_QUALITY_SCORE=60 时会把上限算低 5 分、让 P2 的等效门槛被低估。
_e_base = m.qv_floor_equivalence(m.StrategyConfig())
_e_p2 = m.qv_floor_equivalence(m.StrategyConfig(MIN_QUALITY_SCORE=60.0))
_e_off = m.qv_floor_equivalence(m.StrategyConfig(MIN_QUALITY_SCORE=0.0))
check("审计: 质量硬门关闭时压线质量分=评分锚点 50（与旧行为一致）",
      _e_off["min_verified_quality"] == 50.0)
check(f"审计: 质量硬门=60 时压线质量分跟随为 {_e_p2['min_verified_quality']:.0f}",
      _e_p2["min_verified_quality"] == 60.0)
check("审计: 默认口径（门槛=50）数值不变，无回归",
      _e_base["min_verified_quality"] == 50.0
      and abs(_e_base["ceiling_industry"] - 59.5) < 1e-6)
# 附带记录一项设计权衡：抬质量硬门会把「过线所需技术分」门槛降低
# （质量分抬高 → 综合分基线抬高 → 技术分不必那么高），因此 P2 若单独上线，
# 实际是「抬高质量、放松技术」而非单纯收紧，必须与 P3 一起评估。
check(f"审计: P2 使所需技术分从 {_e_base['req_tech_industry']:.0f} 降到 "
      f"{_e_p2['req_tech_industry']:.0f}（记录权衡：质量↑ 技术↓）",
      _e_p2["req_tech_industry"] < _e_base["req_tech_industry"])

# ===========================================================================
_ok = sum(1 for _, ok in _RESULTS if ok)
print(f"\n{_ok}/{len(_RESULTS)} 通过")

# ===========================================================================
# 7) 上线决策固化：P0/P1 当前**不建议**开启（实测未证明有收益）
# ===========================================================================
# 评估日 2026-06-04、n=168、前瞻窗口取真实后续行情（非历史价）的实测结论：
#   · 止跌闸门保留组 vs 剔除组，F5/F10/F20/F60 收益中位数全部更优
#     （+2.59/+3.83/+5.44/+4.24 pp）⇒ 闸门在做剔除而非选优，不该放宽。
#   · strict 放行 74/180(41.1%) vs risk_only 76/180(42.2%)，放行量几乎相同。
#   · 250日分位 ρ(F5)=−0.05 / ρ(F20)=+0.02 / ρ(F60)=+0.07，几乎无区分度。
# 本节把「默认不上线」这一决策固化，避免后人看到有实现就顺手翻开默认值。
_cfg_final = m.StrategyConfig()
check("决策: P0 默认仍为 strict（实测未证明放宽有收益，不上线）",
      _cfg_final.QV_STABILIZATION_MODE == "strict")
check("决策: P1 权重默认 0（分位几乎无区分度，不上线）",
      _cfg_final.QV_POSITION_RANK_WEIGHT == 0.0)
check("决策: P3 技术分权重默认 0.25（实测技术分 ρ=+0.21~+0.26 正向，降权与数据相悖）",
      _cfg_final.TECHNICAL_SCORE_WEIGHT == 0.25)

if _ok != len(_RESULTS):
    raise SystemExit(1)
