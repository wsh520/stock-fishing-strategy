"""KDJ / MACD 下限闸门（荐股控制）回归测试 —— 合成数据，不联网。

覆盖 2026-09 P0-1「把 KDJ/MACD 从纯评分项升级为荐股控制闸门」这项改动：

1. 配置层：新增字段的默认值与归属（StrategyConfig 定义、突破策略继承）
2. 纯函数层：macd_not_deeply_weak / kdj_not_overheated 的边界与缺数据行为
3. 突破策略接线：既有 PASS 形态不回归；KDJ 高位形态由 PASS 变 FAIL_KDJ_HIGH，
   且关掉开关即恢复 PASS（证明拦截确实来自新闸门而非数据抖动）
4. 突破策略的重要事实：FAIL_MACD_WEAK 在「首次站上阻力位」口径下结构性不可达
   —— 本文件把该结论固化为扫描断言，一旦突破口径被放宽就会失败并提示复核
5. quality_value 接线：默认关闭时技术面不新增否决（健康形态照常 PASS）；开启后深度弱势被否决。
   注：本节的合成 fixture 不含成长数据，在 FORWARD_MISSING_AS_PENDING=True（当前默认）下
   tier 为 pending —— 断言按此默认锁定，并用 missing_tags=="forward" 证明无其它缺项

关键设计依据（本文件即证据）：
  - MACD 柱度量「加速度」而非趋势，匀速上行的柱值必然向 0 收敛递减。
    只用「柱值递减」会把「长期下跌 → 稳步回升」这整类健康形态判为走弱，
    因此闸门改为（深度弱势 AND 仍在恶化）双条件。
  - 匀速上行时 9 日 RSV 稳定在 ~0.72，K 与 D 在 70 附近交替领先，
    「K<D 且 K>60」会在约半数交易日误杀，因此死叉阈值必须贴近区间顶部。
"""
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import numpy as np
import pandas as pd

from src import bottom_fishing_strategy as m
from src import volume_breakout_strategy as vb
from src.bottom_fishing_strategy import kdj_not_overheated, macd_not_deeply_weak

_RESULTS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, bool(ok)))


cfg = m.StrategyConfig(RECOMMENDATION_MODE="technical")
vcfg = vb.VolumeBreakoutConfig(RECOMMENDATION_MODE="technical")
_NEUTRAL = {"regime": "neutral"}
_FUND_OK = {"roe": 12.0, "debt_ratio": 45.0}

# ===========================================================================
# 1) 配置层
# ===========================================================================
check("配置: QV_ENFORCE_KDJ_MACD_VETO 默认关闭（生产口径不变）",
      getattr(cfg, "QV_ENFORCE_KDJ_MACD_VETO", None) is False)
check("配置: 突破策略两个开关默认开启",
      getattr(vcfg, "REQUIRE_BR_MACD_NOT_WEAK", None) is True
      and getattr(vcfg, "REQUIRE_BR_KDJ_NOT_HIGH", None) is True)
check("配置: 阈值由 StrategyConfig 定义并被突破策略继承",
      (cfg.MACD_WEAK_DAYS, cfg.MACD_WEAK_HIST_PCT, cfg.KDJ_K_HARD_MAX, cfg.KDJ_DEAD_CROSS_K)
      == (vcfg.MACD_WEAK_DAYS, vcfg.MACD_WEAK_HIST_PCT, vcfg.KDJ_K_HARD_MAX, vcfg.KDJ_DEAD_CROSS_K))
check("配置: 死叉阈值贴近 K 上限（设在顶部区域而非中位）",
      cfg.KDJ_K_HARD_MAX - cfg.KDJ_DEAD_CROSS_K <= 10.0 and cfg.KDJ_DEAD_CROSS_K > 60.0)

# ===========================================================================
# 2) 纯函数：macd_not_deeply_weak
# ===========================================================================
def macd_df(hist, close=10.0):
    return pd.DataFrame({"close": [close] * len(hist), "macd_histogram": hist})


check("MACD: 深度弱势且连续恶化 → False",
      macd_not_deeply_weak(macd_df([-0.10, -0.11, -0.12]), 2, -0.5) is False)
check("MACD: 深度弱势但已改善 → True（不拦正在修复的标的）",
      macd_not_deeply_weak(macd_df([-0.12, -0.11, -0.12]), 2, -0.5) is True)
check("MACD: 匀速上行的正柱递减 → True（本次改动要修掉的误杀）",
      macd_not_deeply_weak(macd_df([0.0088, 0.00817, 0.00759]), 2, -0.5) is True)
check("MACD: 轻度为负但未达深度阈值 → True",
      macd_not_deeply_weak(macd_df([-0.03, -0.038, -0.041]), 2, -0.5) is True)
check("MACD: 深度弱势但只恶化 1 日（days=2）→ True",
      macd_not_deeply_weak(macd_df([-0.12, -0.10, -0.11]), 2, -0.5) is True)
check("MACD: NaN → True（缺数据放行，不误杀）",
      macd_not_deeply_weak(macd_df([-0.10, np.nan, -0.12]), 2, -0.5) is True)
check("MACD: 长度不足 → True", macd_not_deeply_weak(macd_df([-0.10]), 2, -0.5) is True)
check("MACD: 空表 / None / 缺列 → True",
      macd_not_deeply_weak(pd.DataFrame(), 2, -0.5) is True
      and macd_not_deeply_weak(None, 2, -0.5) is True
      and macd_not_deeply_weak(pd.DataFrame({"close": [10.0] * 3}), 2, -0.5) is True)
check("MACD: 收盘价非法（0/负/NaN）→ True",
      macd_not_deeply_weak(macd_df([-0.10, -0.11, -0.12], close=0.0), 2, -0.5) is True
      and macd_not_deeply_weak(macd_df([-0.10, -0.11, -0.12], close=np.nan), 2, -0.5) is True)

# ===========================================================================
# 3) 纯函数：kdj_not_overheated
# ===========================================================================
def kdj_df(k, d):
    return pd.DataFrame({"kdj_k": [k], "kdj_d": [d]})


check("KDJ: K 超硬上限 → False", kdj_not_overheated(kdj_df(90.0, 80.0), 85.0, 80.0) is False)
check("KDJ: K 等于上限（不含）→ True", kdj_not_overheated(kdj_df(85.0, 80.0), 85.0, 80.0) is True)
check("KDJ: 顶部区域死叉 → False", kdj_not_overheated(kdj_df(82.0, 84.0), 85.0, 80.0) is False)
check("KDJ: K=80 恰好到死叉阈值 → False", kdj_not_overheated(kdj_df(80.0, 83.0), 85.0, 80.0) is False)
check("KDJ: 70 附近 K 略低于 D → True（匀速上行的交替领先噪声，不拦）",
      kdj_not_overheated(kdj_df(71.2, 71.3), 85.0, 80.0) is True)
check("KDJ: 低位死叉 → True（下跌末段常态，不拦）",
      kdj_not_overheated(kdj_df(18.0, 25.0), 85.0, 80.0) is True)
check("KDJ: 金叉中位 → True", kdj_not_overheated(kdj_df(60.0, 50.0), 85.0, 80.0) is True)
check("KDJ: NaN / 空表 / None / 缺列 → True",
      kdj_not_overheated(kdj_df(np.nan, 50.0), 85.0, 80.0) is True
      and kdj_not_overheated(pd.DataFrame(), 85.0, 80.0) is True
      and kdj_not_overheated(None, 85.0, 80.0) is True
      and kdj_not_overheated(pd.DataFrame({"kdj_k": [90.0]}), 85.0, 80.0) is True)

# ===========================================================================
# 4) 突破策略接线
# ===========================================================================
def brk_df(breakout_close: float, n: int = 100) -> pd.DataFrame:
    """与 test_strategy_fixes.make_l2_breakout_df 同构：平台 + 末端放量突破 20 日新高。"""
    closes = np.full(n, 10.0)
    closes[45:50] = 11.0            # L1 抬高到 11.055，末日只可能是 L2 突破
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs, lows = closes * 1.005, closes * 0.995
    volume = np.full(n, 12_000_000.0)
    opens[-1] = 10.02
    closes[-1] = breakout_close
    highs[-1] = breakout_close * 1.005
    lows[-1] = 10.0
    volume[-1] = 30_000_000.0
    dates = pd.date_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates, "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volume, "amount": closes * volume,
        "pct_chg": (pd.Series(closes).pct_change().fillna(0) * 100).values,
    })


def evaluate_brk(df, config):
    return vb.evaluate_breakout(df, code="600000", name="测试", config=config,
                                market_env=_NEUTRAL, fund_data=_FUND_OK)


_sig_a, _r_a = evaluate_brk(brk_df(10.25), vcfg)
check(f"突破接线: 既有 L2 突破形态仍 PASS（无回归）: {_r_a}",
      _r_a == "PASS" and _sig_a is not None)


def kdj_high_df(n: int = 110) -> pd.DataFrame:
    """合法 L2 突破（首次站上 20 日阻力位）但 KDJ 已在高位。

    形态来源：对「先行冲高 → 回落 → 末日再突破」做过参数扫描，
    peak=10.8 / dip_r=0.985 / final=3% 是命中 K>85 的确定组合之一。
    注意 brk_l2 = above_l2 & ~above_l2.shift(1)：只有「先跌破再重新站上」
    才算法突破，所以走弱的动能必须由**前一段冲高后回落**来制造。
    """
    closes = np.full(n, 10.0)
    closes[45:50] = 11.0
    peak, dip = 10.8, 10.8 * 0.985
    closes[n - 11:n - 5] = np.linspace(10.0, peak, 6)
    closes[n - 5:n - 1] = np.linspace(peak, dip, 4)
    closes[-1] = dip * 1.03
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs, lows = closes * 1.005, closes * 0.995
    volume = np.full(n, 12_000_000.0)
    opens[-1] = closes[-2]
    highs[-1] = closes[-1] * 1.005
    lows[-1] = closes[-1] * 0.995
    volume[-1] = 30_000_000.0
    dates = pd.date_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates, "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volume, "amount": closes * volume,
        "pct_chg": (pd.Series(closes).pct_change().fillna(0) * 100).values,
    })


_hdf = kdj_high_df()
_out_h = vb.compute_breakout_signals(_hdf, vcfg)
_k = float(_out_h["kdj_k"].iloc[-1])
check(f"突破接线: 构造形态的 K 值确实超上限（K={_k:.1f}）", _k > vcfg.KDJ_K_HARD_MAX)

_sig_h, _r_h = evaluate_brk(_hdf, vcfg)
check(f"突破接线: KDJ 高位触发 FAIL_KDJ_HIGH: {_r_h}", _r_h == "FAIL_KDJ_HIGH" and _sig_h is None)

_sig_h2, _r_h2 = evaluate_brk(_hdf, replace(vcfg, REQUIRE_BR_KDJ_NOT_HIGH=False))
check(f"突破接线: 关掉 KDJ 开关即恢复 PASS（证明拦截来自闸门）: {_r_h2}",
      _r_h2 == "PASS" and _sig_h2 is not None)

_sig_h3, _r_h3 = evaluate_brk(_hdf, replace(vcfg, KDJ_K_HARD_MAX=95.0))
check(f"突破接线: 放宽 K 上限同样恢复 PASS: {_r_h3}", _r_h3 == "PASS" and _sig_h3 is not None)

# ---------------------------------------------------------------------------
# FAIL_MACD_WEAK 在突破策略中结构性不可达 —— 固化为扫描断言
# ---------------------------------------------------------------------------
# 推导：hist_t = 0.8×(DIF_t − DEA_{t−1}) ⇒ hist 下降 ⟺ ΔDIF_t < hist_{t−1}/4。
# 突破日的 close 必须 > 前 20 日最高价×1.005 且涨幅 ≥ MIN_BREAKOUT_PCT，
# 这个上跳必然使 ΔDIF_t > 0，而横盘阶段 hist_{t−1} ≤ 0 → 不等式永不成立。
# 因此新增的 MACD 闸门在突破策略里只是一道「口径被放宽后的保险丝」，
# 真正的控制力来自 KDJ 高位闸门与前序的趋势/RSI 层。
_scan_hits = []
for _peak in (10.4, 10.6, 10.8, 11.0):
    for _dr in (0.955, 0.975, 0.99):
        for _fr in (0.021, 0.03, 0.04):
            _c = np.full(110, 10.0)
            _c[45:50] = 11.0
            _dip = _peak * _dr
            if _dip * (1 + _fr) <= _peak * 1.005 * 1.005:
                continue
            _c[99:105] = np.linspace(10.0, _peak, 6)
            _c[105:109] = np.linspace(_peak, _dip, 4)
            _c[-1] = _dip * (1 + _fr)
            _o = np.concatenate([[_c[0]], _c[:-1]])
            _scan = pd.DataFrame({
                "date": pd.date_range("2025-01-01", periods=110).strftime("%Y-%m-%d"),
                "open": _o, "high": _c * 1.005, "low": _c * 0.995, "close": _c,
                "volume": np.r_[np.full(109, 12_000_000.0), 30_000_000.0],
                "amount": _c * 12_000_000.0,
                "pct_chg": (pd.Series(_c).pct_change().fillna(0) * 100).values})
            _scan.loc[_scan.index[-1], "amount"] = _c[-1] * 30_000_000.0
            _, _rs = evaluate_brk(_scan, vcfg)
            if _rs == "FAIL_MACD_WEAK":
                _scan_hits.append((_peak, _dr, _fr))
check(f"突破事实: 参数扫描 {len(_scan_hits)} 个形态命中 FAIL_MACD_WEAK（预期 0，"
      f"命中则说明突破口径被放宽，需重新评估该闸门）", not _scan_hits)

# ===========================================================================
# 5) quality_value 接线
# ===========================================================================
_QV_DAY = "2025-06-30"


def qv_df(close) -> pd.DataFrame:
    close = np.asarray(close, float)
    dates = pd.bdate_range(end=_QV_DAY, periods=len(close)).strftime("%Y-%m-%d")
    return pd.DataFrame(dict(date=dates, open=close, high=close * 1.01, low=close * .99,
                             close=close, volume=1e7, amount=close * 1e7,
                             peTTM=12., pbMRQ=1.2))


def qv_fund() -> dict:
    return {"debt_ratio": 40., "roe": 2., "annual_rows": [
        {"year": y, "report_date": f"{y}-12-31", "available_date": f"{y+1}-04-20",
         "roe": roe, "deducted_profit": 90., "net_profit": 100., "operating_cashflow": 110.}
        for y, roe in [(2022, 11), (2023, 12), (2024, 13)]]}


def qv_evaluate(close, veto: bool):
    # 本节只验证 KDJ/MACD 下限闸门，关闭两个与之无关的闸门以隔离被测维度：
    #   · MIN_QV_SCORE=0 —— 样本分数（~53）会触发 FAIL_QV_SCORE 掩盖真正的归因；
    #   · QV_STABILIZATION_GATE=False —— 止跌确认闸门现为全环境生效（库级默认开启），
    #     深度回落形态会被它先拦成 FAIL_STABILIZATION。它自身的行为在下方有专节断言。
    return m.evaluate(qv_df(close), "600001", "工业企业",
                      m.StrategyConfig(USE_CACHE=False, QV_ENFORCE_KDJ_MACD_VETO=veto,
                                       MIN_QV_SCORE=0, QV_STABILIZATION_GATE=False),
                      {"regime": "bear"}, qv_fund(), latest_trade_date=_QV_DAY)


# 长期下跌 → 稳步匀速回升：hist 为正但递减，K/D 在 70 附近交替领先
_healthy = np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 50)]
_healthy_tech = m.compute_daily_signals(m._recompute_pct_chg(qv_df(_healthy).copy()),
                                        m.StrategyConfig(USE_CACHE=False))
_h3 = [round(float(x), 5) for x in _healthy_tech["macd_histogram"].iloc[-3:].tolist()]
check(f"QV 事实: 标准健康形态的 MACD 柱为正且递减 {_h3}（只判递减会误杀）",
      _h3[0] > _h3[1] > _h3[2] > 0)
check("QV 事实: 该形态 K/D 在 70 附近交替领先（死叉阈值不能放中位）",
      abs(float(_healthy_tech["kdj_k"].iloc[-1]) - float(_healthy_tech["kdj_d"].iloc[-1])) < 2.0)

_sig_off, _r_off = qv_evaluate(_healthy, veto=False)
# 该 fixture 的财务字段不含 forward_ni_yoy（成长数据），而当前默认
# FORWARD_MISSING_AS_PENDING=True —— 缺失会记「forward」缺项并降级为待核验，
# 因此 tier 是 pending 而非 formal。用 missing_tags 精确等于 "forward" 反向锁住
# 「除此之外没有别的缺项」，证明该形态在财务/估值/行情日期上都是齐的。
_off_tier = getattr(_sig_off, "tier", None)
_off_missing = getattr(_sig_off, "missing_tags", None)
check(f"QV: 默认关闭时标准形态仍 PASS（技术面不否决）；成长数据缺失按当前默认降级为待核验: "
      f"{_r_off} / tier={_off_tier} / 缺项=[{_off_missing}]",
      _r_off == "PASS" and _off_tier == "pending" and _off_missing == "forward")
_sig_on, _r_on = qv_evaluate(_healthy, veto=True)
check(f"QV: 开启否决后该健康形态仍 PASS（未误杀匀速上行）: {_r_on}", _r_on == "PASS")

# 冲高后深幅回落 → hist 深度为负且继续恶化
_weak = np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 35), np.linspace(11, 9.8, 15)[1:]]
flat = np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 35), np.linspace(11, 10.7, 15)[1:]]
_sig_w0, _r_w0 = qv_evaluate(_weak, veto=False)
check(f"QV: 深度回落形态在默认口径下 PASS（技术面不否决）: {_r_w0}", _r_w0 == "PASS")
_sig_w1, _r_w1 = qv_evaluate(_weak, veto=True)
check(f"QV: 开启否决后深度弱势被拦: {_r_w1}", _r_w1 == "FAIL_MACD_WEAK" and _sig_w1 is None)
_sig_f, _r_f = qv_evaluate(flat, veto=True)
check(f"QV: 轻度回落未被误杀（未达深度阈值）: {_r_f}", _r_f == "PASS")

# QV 的 KDJ 分支可被真实触发：把 K 上限压到远低于该形态的 K 值即可
# （同时把 MACD 深度阈值设到不可达，确保拦截确实归因于 KDJ 而非 MACD）
_kv = float(_healthy_tech["kdj_k"].iloc[-1])
_qv_low = m.StrategyConfig(USE_CACHE=False, QV_ENFORCE_KDJ_MACD_VETO=True,
                           MACD_WEAK_HIST_PCT=-99.0, KDJ_K_HARD_MAX=_kv - 1)
_sig_k, _r_k = m.evaluate(qv_df(_healthy), "600001", "工业企业", _qv_low,
                          {"regime": "bear"}, qv_fund(), latest_trade_date=_QV_DAY)
check(f"QV: KDJ 上限分支可被真实触发（K={_kv:.1f} > 上限 {_kv - 1:.1f}）: {_r_k}",
      _r_k == "FAIL_KDJ_HIGH" and _sig_k is None)

# 死叉分支同样可被真实触发：K 已到顶部区域且 K<D
_qv_dc = m.StrategyConfig(USE_CACHE=False, QV_ENFORCE_KDJ_MACD_VETO=True,
                          MACD_WEAK_HIST_PCT=-99.0, KDJ_DEAD_CROSS_K=70.0)
_, _r_dc = m.evaluate(qv_df(_healthy), "600001", "工业企业", _qv_dc,
                      {"regime": "bear"}, qv_fund(), latest_trade_date=_QV_DAY)
_kd_pair = (float(_healthy_tech["kdj_k"].iloc[-1]), float(_healthy_tech["kdj_d"].iloc[-1]))
check(f"QV: KDJ 顶部死叉分支可被真实触发（K/D={_kd_pair}）: {_r_dc}",
      _r_dc == "FAIL_KDJ_HIGH")

# ===========================================================================
# 5b) 止跌确认闸门（QV_STABILIZATION_GATE）—— 全市场环境生效
# ===========================================================================
# 该闸门前身为 QV_BEAR_TIMING_GATE（仅熊市生效），现扩展到所有市场环境。
# 本节锁定：默认开启；牛/中/熊/unknown 均生效；显式关闭即恢复放行。
_bear_default = m.StrategyConfig()
check(f"止跌确认闸门: 库级默认已开启，实际={_bear_default.QV_STABILIZATION_GATE}",
      _bear_default.QV_STABILIZATION_GATE is True)

_gate_cfg = m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0)
_, _r_bear = m.evaluate(qv_df(_weak), "600001", "工业企业", _gate_cfg,
                        {"regime": "bear"}, qv_fund(), latest_trade_date=_QV_DAY)
check(f"止跌确认闸门: 熊市下未止跌形态被拦: {_r_bear}", _r_bear == "FAIL_STABILIZATION")

_, _r_unknown = m.evaluate(qv_df(_weak), "600001", "工业企业", _gate_cfg,
                           {"regime": "unknown"}, qv_fund(), latest_trade_date=_QV_DAY)
check(f"止跌确认闸门: unknown 同样被拦（全环境生效）: {_r_unknown}",
      _r_unknown == "FAIL_STABILIZATION")

_, _r_bull_gate = m.evaluate(qv_df(_weak), "600001", "工业企业", _gate_cfg,
                             {"regime": "bull"}, qv_fund(), latest_trade_date=_QV_DAY)
check(f"止跌确认闸门: 牛市同样生效（全环境）: {_r_bull_gate}", _r_bull_gate == "FAIL_STABILIZATION")

_, _r_neutral_gate = m.evaluate(qv_df(_weak), "600001", "工业企业", _gate_cfg,
                                {"regime": "neutral"}, qv_fund(), latest_trade_date=_QV_DAY)
check(f"止跌确认闸门: 中性市同样生效: {_r_neutral_gate}", _r_neutral_gate == "FAIL_STABILIZATION")

_, _r_bear_off = m.evaluate(qv_df(_weak), "600001", "工业企业",
                            m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0,
                                             QV_STABILIZATION_GATE=False),
                            {"regime": "bear"}, qv_fund(), latest_trade_date=_QV_DAY)
check(f"止跌确认闸门: 显式关闭即恢复放行（证明拦截来自该闸门）: {_r_bear_off}",
      _r_bear_off == "PASS")

# 向后兼容：旧配置名 QV_BEAR_TIMING_GATE=False 等价于 QV_STABILIZATION_GATE=False
_, _r_legacy_off = m.evaluate(qv_df(_weak), "600001", "工业企业",
                              m.StrategyConfig(USE_CACHE=False, MIN_QV_SCORE=0,
                                               QV_BEAR_TIMING_GATE=False),
                              {"regime": "bear"}, qv_fund(), latest_trade_date=_QV_DAY)
check(f"止跌确认闸门: 旧配置名 QV_BEAR_TIMING_GATE=False 兼容迁移: {_r_legacy_off}",
      _r_legacy_off == "PASS")

# ===========================================================================
# 6) 为什么新闸门没有加进抄底策略的 technical 路径
# ===========================================================================
# evaluate()（technical）已有一层「严格确认」：_macd_momentum_ok 要求柱值**连续改善**、
# _kdj_ok 要求**金叉(K>D) 且 K≤55 且 K 上行**。两者在新下限闸门的对应维度上**严格更强**：
#   - KDJ：既有层要求 K>D（排除死叉分支）且 K≤55（远低于 85 上限）→ 新闸门两个分支都不可达；
#   - MACD：既有层要求改善（排除恶化）→ 新闸门不可达。
# 因此若把新闸门也接进 technical 路径，它只会是「被前置闸门架空的死代码」——本仓库
# 对这种结构有明确先例（MIN_RR_RATIO_BREAKOUT / DAILY_RSI_OVERBOUGHT_PENALTY 已被删除）。
# 新闸门的有效落点是：放量突破策略 + quality_value 模式的显式开关。
_cfg_t = m.StrategyConfig(RECOMMENDATION_MODE="technical")
_kdj_strong = kdj_df(55.0, 40.0)          # 既有层允许的最松情形：K=55（上限）、K>D
check("覆盖关系: 既有 KDJ 确认通过时，新 KDJ 闸门必然通过（新闸门在 technical 路径不可达）",
      kdj_not_overheated(_kdj_strong, _cfg_t.KDJ_K_HARD_MAX, _cfg_t.KDJ_DEAD_CROSS_K) is True)
_macd_strong = macd_df([-0.5, -0.3, -0.1])   # 既有层要求连续改善：柱值在升
check("覆盖关系: 既有 MACD 确认通过时，新 MACD 闸门必然通过",
      macd_not_deeply_weak(_macd_strong, _cfg_t.MACD_WEAK_DAYS, _cfg_t.MACD_WEAK_HIST_PCT) is True)
check("覆盖关系: 既有 KDJ 层上限 55 < 新硬上限 85（量化「严格更强」）",
      _cfg_t.KDJ_K_MAX < _cfg_t.KDJ_K_HARD_MAX)
check("覆盖关系: technical 路径的 evaluate 未接线新闸门（避免死代码）",
      "FAIL_KDJ_HIGH" not in __import__("inspect").getsource(m.evaluate)
      and "FAIL_MACD_WEAK" not in __import__("inspect").getsource(m.evaluate))

# ===========================================================================
# 汇总
# ===========================================================================
passed = sum(1 for _, ok in _RESULTS if ok)
print(f"\n{passed}/{len(_RESULTS)} 通过")
sys.exit(0 if passed == len(_RESULTS) else 1)
