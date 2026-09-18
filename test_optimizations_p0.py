"""P0 优化项验证脚本（合成数据，不访问网络）。

覆盖 2026-09 策略深度优化的第一批改动（A1/A2/B1/B2），全部为可离线验证的确定性断言：

A1. pct_chg 双源口径统一
    - Baostock pctChg / AkShare 东财「涨跌幅」不再被透传，一律由 qfq 收盘价自算
    - 除权除息日不再把股息除权误判为暴跌（该值直接驱动 FAIL_CHASE / FAIL_GAP 判定）
    - 磁盘缓存命中路径（_read_cache_csv 不经过 _normalize_*）同样在出口重算
    - None / 空表 / 重复应用 的安全性

A2. 停牌缺口过滤 FAIL_HALT_GAP
    - 相邻 K 线自然日间隔 > MAX_BAR_GAP_DAYS 判定为曾停牌
    - 春节（约 11 天）/ 国庆（约 8 天）长假不误杀
    - 开关关闭后放行；缺 date 列 / None 不抛异常
    - evaluate() 确实接线：同一价格序列仅重标日期制造缺口 → 由 PASS 变 FAIL_HALT_GAP

B1. 连续排序质量分 rank_score
    - 只影响顺序不影响准入：RANK_QUALITY_WEIGHT=0 时完全退回旧行为
    - 四个维度的单调性（低波动 / 深回撤 / 低区间位置 / 强动能 → 分数更高）
    - 取值有界 [0, RANK_QUALITY_WEIGHT]，NaN 不崩
    - _rank_signals 可跨分数档重排；缺 rank_score 列时自动退回 score（向后兼容）

B2. 市场环境 → 推荐数量上限 + 市场级熔断
    - resolve_max_picks 牛/中/熊/unknown 四态正确，unknown 折叠为 bear
    - market_crash_halt 触发/不触发/数据不足三态正确
    - 放量突破策略的独立口径（NEUTRAL=3 / BEAR_BREAKOUT=0）未被父类新字段污染
"""
import os
import sys
from dataclasses import asdict, replace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import numpy as np
import pandas as pd

from src import bottom_fishing_strategy as m
from src import volume_breakout_strategy as vb

_RESULTS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, bool(ok)))


def _close(a, b, tol=1e-6) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


# ===========================================================================
# 合成数据工具
# ===========================================================================

def make_df(closes, last_vol_mult=3.0, vol_base=10_000_000):
    """与 test_entry_filters.make_df 同构的日 K 合成器（额外可注入 pct_chg 噪声）。"""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    dates = pd.date_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    open_ = np.concatenate([[closes[0]], closes[:-1]])
    high = np.maximum(open_, closes) * 1.01
    low = np.minimum(open_, closes) * 0.99
    volume = np.full(n, vol_base, dtype=float)
    volume[-1] = vol_base * last_vol_mult
    amount = closes * volume
    pct_chg = pd.Series(closes).pct_change().fillna(0) * 100
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low, "close": closes,
        "volume": volume, "amount": amount, "pct_chg": pct_chg.values,
    })


def base_closes():
    """缓跌 20→17（90天）+ 加速跌 17→14.5（25天）+ 走平 4 天 + 反弹 3.5%（可通过全部闸门）。"""
    part1 = np.linspace(20.0, 17.0, 90)
    part2 = np.linspace(17.0, 14.5, 25)[1:]
    part3 = np.full(4, 14.5)
    closes = np.concatenate([part1, part2, part3])
    return np.append(closes, closes[-1] * 1.035)


_FUND_OK = {"roe": 12.0, "debt_ratio": 45.0, "goodwill_ratio": 3.0, "deducted_profit_ratio": 0.9}
_NEUTRAL = {"regime": "neutral"}


# ===========================================================================
# A1) pct_chg 双源口径统一
# ===========================================================================
print("\n--- A1: pct_chg 双源口径统一 ---")

# Baostock 形态：pctChg 被数据源污染为 999，须被自算值覆盖
_bs_raw = pd.DataFrame({
    "date": ["2025-01-02", "2025-01-03", "2025-01-06"],
    "open": ["10.0", "10.0", "11.0"], "high": ["10.5", "11.2", "11.5"],
    "low": ["9.8", "9.9", "10.8"], "close": ["10.0", "11.0", "10.5"],
    "volume": ["1000", "2000", "1500"], "amount": ["1e7", "2e7", "1.5e7"],
    "pctChg": ["999.0", "999.0", "999.0"], "turn": ["1.0", "2.0", "1.5"],
})
_bs_norm = m._normalize_bs_hist(_bs_raw.copy())
check("A1: Baostock pctChg 被自算值覆盖（+10%）", _close(_bs_norm["pct_chg"].iloc[1], 10.0, 1e-9))
check("A1: Baostock 自算第二日跌幅", _close(_bs_norm["pct_chg"].iloc[2], (10.5 / 11.0 - 1) * 100, 1e-9))
check("A1: Baostock 首行无前收为 NaN", pd.isna(_bs_norm["pct_chg"].iloc[0]))
check("A1: Baostock turn→turnover 重命名仍生效", "turnover" in _bs_norm.columns)

# AkShare 东财形态：中文列名 +「涨跌幅」被污染
_ak_raw = pd.DataFrame({
    "日期": ["2025-01-02", "2025-01-03", "2025-01-06"],
    "开盘": [10.0, 10.0, 11.0], "收盘": [10.0, 11.0, 10.5],
    "最高": [10.5, 11.2, 11.5], "最低": [9.8, 9.9, 10.8],
    "成交量": [1000, 2000, 1500], "成交额": [1e7, 2e7, 1.5e7],
    "涨跌幅": [-888.0, -888.0, -888.0], "换手率": [1.0, 2.0, 1.5],
})
_ak_norm = m._normalize_ak_hist(_ak_raw.copy())
check("A1: AkShare「涨跌幅」被自算值覆盖", _close(_ak_norm["pct_chg"].iloc[1], 10.0, 1e-9))
check("A1: AkShare 与 Baostock 同输入产出同 pct_chg",
      _close(_ak_norm["pct_chg"].iloc[2], _bs_norm["pct_chg"].iloc[2], 1e-12))

# 除权除息场景：源字段把 3% 股息除权报成 -3% 暴跌，qfq 收盘价实际是 +0.5%
_div_raw = pd.DataFrame({
    "date": ["2025-06-05", "2025-06-06"],
    "open": [20.0, 20.0], "high": [20.4, 20.5], "low": [19.8, 19.9],
    "close": [20.0, 20.1],          # qfq 序列：无除权跳空
    "volume": [1e6, 1e6], "amount": [2e7, 2e7],
    "pctChg": [0.0, -3.0],          # 不复权口径：显示为 -3% 暴跌
})
_div = m._normalize_bs_hist(_div_raw.copy())
check("A1: 除权日不再被误报为暴跌（-3% → +0.5%）", _close(_div["pct_chg"].iloc[1], 0.5, 1e-9))
check("A1: 除权日不会被 FAIL_CHASE/回撤计算误伤", float(_div["pct_chg"].iloc[1]) > 0)

# 边界与幂等
check("A1: _recompute_pct_chg(None) 返回 None", m._recompute_pct_chg(None) is None)
_empty = pd.DataFrame({"date": [], "close": []})
check("A1: 空表不抛异常", m._recompute_pct_chg(_empty) is not None and m._recompute_pct_chg(_empty).empty)
_once = m._recompute_pct_chg(make_df(base_closes()))
_twice = m._recompute_pct_chg(_once)
check("A1: 重复应用幂等", bool(np.allclose(_once["pct_chg"].fillna(0), _twice["pct_chg"].fillna(0))))
check("A1: 无 close 列时原样返回", "foo" in m._recompute_pct_chg(pd.DataFrame({"foo": [1]})).columns)

# 磁盘缓存命中路径：_read_cache_csv 不经过 _normalize_*，须在 _fetch_daily_dual 出口重算
_stale = make_df(base_closes())
_stale["pct_chg"] = -777.0                     # 模拟旧世代缓存里存的错误口径
_saved = {"ak": m._fetch_daily_ak, "circuit": m._bs_state["circuit_open"],
          "avail": m._AK_AVAILABLE, "stats": dict(m.fetch_stats)}
try:
    m._bs_state["circuit_open"] = True          # 强制走 AkShare 分支
    m._AK_AVAILABLE = True
    m._fetch_daily_ak = lambda code, days=120, config=None: _stale.copy()
    _dual = m._fetch_daily_dual("600000", 120, m.StrategyConfig(RECOMMENDATION_MODE="technical"))
finally:
    m._fetch_daily_ak = _saved["ak"]
    m._bs_state["circuit_open"] = _saved["circuit"]
    m._AK_AVAILABLE = _saved["avail"]
    m.fetch_stats.update(_saved["stats"])
check("A1: 旧世代磁盘缓存在 dual 出口被重算", _dual is not None and float(_dual["pct_chg"].iloc[-1]) > -100)
check("A1: dual 重算值等于收盘价自算值",
      _dual is not None and _close(_dual["pct_chg"].iloc[-1],
                                  (base_closes()[-1] / base_closes()[-2] - 1) * 100, 1e-9))


# ===========================================================================
# A2) 停牌缺口过滤
# ===========================================================================
print("\n--- A2: 停牌缺口过滤 FAIL_HALT_GAP ---")

cfg = m.StrategyConfig(RECOMMENDATION_MODE="technical")


def _dates_with_gap(n=80, gap_at=40, gap_days=40, end="2025-06-30"):
    """构造末根日期固定为 end、在第 gap_at 根处插入 gap_days 自然日缺口的日期序列。"""
    back = []
    cur = pd.Timestamp(end)
    # 从末根往前推：gap_at 之后的间隔为 1 天，之前遇到缺口位再加 gap_days
    for i in range(n):
        back.append(cur)
        step = 1
        if (n - 1 - i) == gap_at:      # 该根与其前一根之间存在缺口
            step += gap_days
        cur = cur - pd.Timedelta(days=step)
    return [d.strftime("%Y-%m-%d") for d in reversed(back)]


_ok_df = make_df(base_closes())
check("A2: 连续日线序列无缺口 → 不否决", m.has_halt_gap(_ok_df, cfg) is False)

_gap_df = _ok_df.copy()
_gap_df["date"] = _dates_with_gap(len(_gap_df), gap_at=40, gap_days=40)
check("A2: 40 天停牌缺口 → 否决", m.has_halt_gap(_gap_df, cfg) is True)

_spring = _ok_df.copy()
_spring["date"] = _dates_with_gap(len(_spring), gap_at=40, gap_days=10)   # 春节：相邻交易日间隔 11 天
check("A2: 春节 11 天间隔 → 不误杀", m.has_halt_gap(_spring, cfg) is False)

_national = _ok_df.copy()
_national["date"] = _dates_with_gap(len(_national), gap_at=40, gap_days=7)  # 国庆：间隔 8 天
check("A2: 国庆 8 天间隔 → 不误杀", m.has_halt_gap(_national, cfg) is False)

check("A2: 开关关闭 → 放行", m.has_halt_gap(_gap_df, replace(cfg, REQUIRE_NO_HALT_GAP=False)) is False)
check("A2: None → 放行不抛异常", m.has_halt_gap(None, cfg) is False)
check("A2: 空表 → 放行", m.has_halt_gap(pd.DataFrame(), cfg) is False)
check("A2: 缺 date 列 → 放行", m.has_halt_gap(pd.DataFrame({"close": [1.0, 2.0]}), cfg) is False)
_wide = _ok_df.copy()
_wide["date"] = _dates_with_gap(len(_wide), gap_at=40, gap_days=40)
check("A2: 阈值可配置（放宽到 60 天则放行）",
      m.has_halt_gap(_wide, replace(cfg, MAX_BAR_GAP_DAYS=60)) is False)

# evaluate() 接线验证：价格/量序列完全不变，仅重标日期制造缺口。
# 所有技术指标都是位置型（rolling/ewm），因此除日期相关的两道校验
# （REQUIRE_FRESH_DAILY / has_halt_gap）外，其余闸门行为完全一致。
_last = str(_ok_df["date"].iloc[-1])
_sig_ok, _r_ok = m.evaluate(_ok_df, code="600000", name="测试", config=cfg,
                            market_env=_NEUTRAL, fund_data=_FUND_OK, latest_trade_date=_last)
check("A2: 前置条件——原始序列可 PASS", _r_ok == "PASS")
_gap_same = _ok_df.copy()
_g_gap = _dates_with_gap(len(_gap_same), gap_at=40, gap_days=40, end=_last)
_gap_same["date"] = _g_gap
_sig_gap, _r_gap = m.evaluate(_gap_same, code="600000", name="测试", config=cfg,
                              market_env=_NEUTRAL, fund_data=_FUND_OK, latest_trade_date=_last)
check("A2: 仅注入日期缺口 → PASS 变 FAIL_HALT_GAP", _r_gap == "FAIL_HALT_GAP" and _sig_gap is None)
_sig_off, _r_off = m.evaluate(_gap_same, code="600000", name="测试",
                              config=replace(cfg, REQUIRE_NO_HALT_GAP=False),
                              market_env=_NEUTRAL, fund_data=_FUND_OK, latest_trade_date=_last)
check("A2: 开关关闭后同一缺口序列恢复 PASS", _r_off == "PASS")

# 突破策略共用同一守卫
_vbcfg = vb.VolumeBreakoutConfig(RECOMMENDATION_MODE="technical")
check("A2: 突破策略已接线 has_halt_gap", hasattr(vb, "has_halt_gap"))
check("A2: 突破策略继承 REQUIRE_NO_HALT_GAP 默认开启", getattr(_vbcfg, "REQUIRE_NO_HALT_GAP", False) is True)


# ===========================================================================
# B1) 连续排序质量分
# ===========================================================================
print("\n--- B1: 连续排序质量分 rank_score ---")

_q_base = m.compute_daily_signals(_ok_df, cfg)
_q0 = m._rank_quality_score(_q_base, cfg)
check("B1: 质量分在 [0, RANK_QUALITY_WEIGHT] 内", 0.0 <= _q0 <= cfg.RANK_QUALITY_WEIGHT)
check("B1: 权重=0 时退回旧行为（恒 0）",
      m._rank_quality_score(_q_base, replace(cfg, RANK_QUALITY_WEIGHT=0.0)) == 0.0)
check("B1: 权重=0 与权重=10 的准入判定一致",
      m.evaluate(_ok_df, config=replace(cfg, RANK_QUALITY_WEIGHT=0.0), market_env=_NEUTRAL,
                 fund_data=_FUND_OK)[1] == _r_ok)

# 单调性：逐维度隔离测试（单独打开一个权重、其余置 0），避免跨维度污染。
# 直接改 high/low 会同时影响 ATR、60日高点、区间位置三个维度，聚合断言不可靠。
def _dim(frame, **weights):
    """只保留指定维度的权重（其余归零），返回该维度的独立得分。"""
    base = {"RQ_W_LOW_VOL": 0.0, "RQ_W_DRAWDOWN": 0.0, "RQ_W_RANGE_POS": 0.0, "RQ_W_MOMENTUM": 0.0}
    base.update(weights)
    return m._rank_quality_score(m.compute_daily_signals(frame, cfg), replace(cfg, **base))


# 维度 1：低波动 —— 放大日内振幅 → ATR 上升 → 该维度得分下降
_wide_vol = _ok_df.copy()
_wide_vol["high"] = _wide_vol["high"] * 1.06
_wide_vol["low"] = _wide_vol["low"] * 0.94
_v0, _v1 = _dim(_ok_df, RQ_W_LOW_VOL=1.0), _dim(_wide_vol, RQ_W_LOW_VOL=1.0)
check(f"B1: 维度[低波动] 振幅放大 → 得分下降 ({_v0:.3f} → {_v1:.3f})", _v1 < _v0)
check("B1: 维度[低波动] ATR 超上限时归零", _v1 == 0.0)

# 维度 2：回撤深度 —— 用平滑的更深下跌序列（不制造跳空，避免污染 ATR）
_deep_closes = np.concatenate([np.linspace(20.0, 17.0, 90),
                               np.linspace(17.0, 11.0, 25)[1:], np.full(4, 11.0)])
_deep_closes = np.append(_deep_closes, _deep_closes[-1] * 1.035)
_deep_df = make_df(_deep_closes)
_d0, _d1 = _dim(_ok_df, RQ_W_DRAWDOWN=1.0), _dim(_deep_df, RQ_W_DRAWDOWN=1.0)
check(f"B1: 维度[回撤深度] 跌得更深 → 得分上升 ({_d0:.3f} → {_d1:.3f})", _d1 > _d0)
_shallow = make_df(np.linspace(20.0, 19.0, 119))          # 距高点仅回撤约 5%，低于闸门下限
check("B1: 维度[回撤深度] 回撤不足下限时归零", _dim(_shallow, RQ_W_DRAWDOWN=1.0) == 0.0)

# 维度 3：区间位置 —— 收盘推到 20 日区间最高 → 该维度归零
_hi_pos = _ok_df.copy()
_hi_pos.iloc[-1, _hi_pos.columns.get_loc("close")] = float(_hi_pos["high"].tail(20).max())
_p0, _p1 = _dim(_ok_df, RQ_W_RANGE_POS=1.0), _dim(_hi_pos, RQ_W_RANGE_POS=1.0)
check(f"B1: 维度[区间位置] 收盘推到区间顶 → 归零 ({_p0:.3f} → {_p1:.3f})", _p1 == 0.0 and _p0 > 0.0)

# 维度 4：MACD 柱改善速率 —— 默认权重 0，因为 REQUIRE_MACD_MOMENTUM 已是硬闸门，
# 通过者的 MACD 柱必然连续改善，该维度在候选集内饱和（此断言即该设计决策的证据）
_m0 = _dim(_ok_df, RQ_W_MOMENTUM=1.0)
check(f"B1: 维度[动能速率] 取值有界 [0, W] ({_m0:.3f})", 0.0 <= _m0 <= cfg.RANK_QUALITY_WEIGHT)
_worse = _ok_df.copy()
_worse.iloc[-1, _worse.columns.get_loc("close")] *= 0.97   # 末日转跌
check(f"B1: 维度[动能速率] 在通过者集合内饱和（故默认权重 0）({_m0:.3f} / {_dim(_worse, RQ_W_MOMENTUM=1.0):.3f})",
      _m0 == cfg.RANK_QUALITY_WEIGHT and _dim(_worse, RQ_W_MOMENTUM=1.0) == cfg.RANK_QUALITY_WEIGHT)
check("B1: 动能维度默认权重为 0（被硬闸门架空，不占排序预算）", cfg.RQ_W_MOMENTUM == 0.0)
check("B1: 四个维度权重合计为 1.0",
      _close(cfg.RQ_W_LOW_VOL + cfg.RQ_W_DRAWDOWN + cfg.RQ_W_RANGE_POS + cfg.RQ_W_MOMENTUM, 1.0, 1e-9))

# 聚合：三个有效维度同向改善时总分上升
_q_deep_full = m._rank_quality_score(m.compute_daily_signals(_deep_df, cfg), cfg)
_q_ok_full = m._rank_quality_score(m.compute_daily_signals(_ok_df, cfg), cfg)
check(f"B1: 聚合 深回撤序列质量分高于浅回撤 ({_q_deep_full:.3f} vs {_q_ok_full:.3f})",
      _q_deep_full > _q_ok_full)

# NaN / 异常鲁棒性
_nan_df = _q_base.copy()
_nan_df.loc[_nan_df.index[-1], "atr"] = np.nan
_nan_df.loc[_nan_df.index[-1], "macd_histogram"] = np.nan
try:
    _q_nan = m._rank_quality_score(_nan_df, cfg)
    check("B1: ATR/MACD 为 NaN 不抛异常且有界", 0.0 <= _q_nan <= cfg.RANK_QUALITY_WEIGHT)
except Exception as e:  # noqa: BLE001
    check(f"B1: ATR/MACD 为 NaN 不抛异常且有界（{e}）", False)

# evaluate() 已填充 rank_score，且准入用的 score 未被污染
check("B1: Signal 含 rank_score 字段", _sig_ok is not None and hasattr(_sig_ok, "rank_score"))
check("B1: rank_score = score + 质量分",
      _sig_ok is not None and _close(_sig_ok.rank_score, _sig_ok.score + _q0, 1e-3))
check("B1: score 与等级仍同源（准入未被质量分影响）",
      _sig_ok is not None and _sig_ok.grade == m._grade_from_score(_sig_ok.score, cfg))
check("B1: 落库字段为原有字段的超集（mysql_store 显式列不受影响）",
      {"code", "name", "date", "close", "score", "grade", "signals_hit",
       "fund_status", "weekly_status", "tier", "missing_tags"}.issubset(asdict(_sig_ok).keys()))

# _rank_signals：可跨分数档重排；缺列时退回 score
_hi_q = pd.DataFrame([
    {"code": "600001", "score": 65.0, "rank_score": 74.5, "has_divergence": False, "rr_ratio": 1.6},
    {"code": "600002", "score": 68.0, "rank_score": 68.1, "has_divergence": False, "rr_ratio": 1.6},
])
_r1 = m._rank_signals(_hi_q)
check("B1: rank_score 主导排序（65+9.5 排在 68+0.1 之前）", _r1.iloc[0]["code"] == "600001")
_legacy = pd.DataFrame([
    {"code": "600001", "score": 65.0, "has_divergence": False, "rr_ratio": 1.6},
    {"code": "000002", "score": 65.0, "has_divergence": False, "rr_ratio": 1.6},
    {"code": "000003", "score": 70.0, "has_divergence": False, "rr_ratio": 1.5},
])
_r2 = m._rank_signals(_legacy)
check("B1: 缺 rank_score 列 → 退回 score 排序（向后兼容）", _r2.iloc[0]["code"] == "000003")
check("B1: 缺 rank_score 列 → 同分按代码升序（结果可复现）", list(_r2["code"])[1:] == ["000002", "600001"])
check("B1: 无任何可用排序键时不抛异常", len(m._rank_signals(pd.DataFrame([{"x": 1}]))) == 1)


# ===========================================================================
# B2) 市场环境 → 推荐数量上限 + 市场级熔断
# ===========================================================================
print("\n--- B2: 推荐数量上限 + 市场级熔断 ---")

check("B2: 新增配置字段齐备",
      hasattr(cfg, "NEUTRAL_MAX_PICKS") and hasattr(cfg, "BEAR_MAX_PICKS")
      and hasattr(cfg, "MARKET_CRASH_HALT_PCT") and hasattr(cfg, "MARKET_CRASH_LOOKBACK"))
check("B2: 上限单调收紧 牛>中性>熊", cfg.MAX_PICKS > cfg.NEUTRAL_MAX_PICKS > cfg.BEAR_MAX_PICKS >= 0)

check("B2: bull → MAX_PICKS", m.resolve_max_picks("bull", cfg) == cfg.MAX_PICKS)
check("B2: neutral → NEUTRAL_MAX_PICKS", m.resolve_max_picks("neutral", cfg) == cfg.NEUTRAL_MAX_PICKS)
check("B2: bear → BEAR_MAX_PICKS", m.resolve_max_picks("bear", cfg) == cfg.BEAR_MAX_PICKS)
check("B2: unknown 折叠为 bear（UNKNOWN_AS_BEAR=True）",
      m.resolve_max_picks("unknown", cfg) == cfg.BEAR_MAX_PICKS)
check("B2: UNKNOWN_AS_BEAR=False 时 unknown → 中性档",
      m.resolve_max_picks("unknown", replace(cfg, UNKNOWN_AS_BEAR=False)) == cfg.NEUTRAL_MAX_PICKS)
check("B2: 非法 regime 字符串按中性处理",
      m.resolve_max_picks("garbage", cfg) == cfg.NEUTRAL_MAX_PICKS)
check("B2: None regime 按 unknown 处理",
      m.resolve_max_picks(None, cfg) == cfg.BEAR_MAX_PICKS)
_legacy_cfg = replace(cfg, NEUTRAL_MAX_PICKS=cfg.MAX_PICKS, BEAR_MAX_PICKS=cfg.MAX_PICKS)
check("B2: 两档设为 MAX_PICKS 即完全退回旧行为",
      all(m.resolve_max_picks(r, _legacy_cfg) == cfg.MAX_PICKS for r in ("bull", "neutral", "bear")))

# 市场级熔断
def _index(closes):
    return pd.DataFrame({"date": pd.date_range("2025-01-01", periods=len(closes)).strftime("%Y-%m-%d"),
                         "close": np.asarray(closes, dtype=float)})

_flat = _index(np.full(30, 4000.0))
check("B2: 指数横盘 → 不熔断", m.market_crash_halt(_flat, cfg) is None)

_crash = _index(np.concatenate([np.full(25, 4000.0), [3980.0, 3940.0, 3900.0, 3860.0, 3800.0]]))
_h = m.market_crash_halt(_crash, cfg)
check("B2: 近5日 -5% → 触发熔断", _h is not None and _h < cfg.MARKET_CRASH_HALT_PCT)
check("B2: 熔断返回实际跌幅", _h is not None and _close(_h, (3800.0 / 4000.0 - 1) * 100, 1e-9))

_mild = _index(np.concatenate([np.full(25, 4000.0), [3990.0, 3985.0, 3980.0, 3975.0, 3970.0]]))
check("B2: 近5日 -0.75% → 不熔断", m.market_crash_halt(_mild, cfg) is None)
# 阈值边界：严格小于才熔断。注意 IEEE754 下 (3840/4000-1)*100 == -4.000000000000004，
# 并非精确的 -4.0，因此「恰好等于阈值」不可用浮点构造来断言；改为分别验证
# 明确未越阈值（-3.9%）与明确越阈值（-4.1%）两侧。
_near = _index(np.concatenate([np.full(25, 4000.0), [3960.0, 3930.0, 3900.0, 3870.0, 3844.0]]))
check("B2: 近5日 -3.9% 未越阈值 → 不熔断", m.market_crash_halt(_near, cfg) is None)
_over = _index(np.concatenate([np.full(25, 4000.0), [3960.0, 3920.0, 3880.0, 3860.0, 3836.0]]))
check("B2: 近5日 -4.1% 越阈值 → 熔断", m.market_crash_halt(_over, cfg) is not None)
check("B2: 阈值可配置放宽（-4.1% 在 -5% 阈值下不熔断）",
      m.market_crash_halt(_over, replace(cfg, MARKET_CRASH_HALT_PCT=-5.0)) is None)
check("B2: 数据不足（≤LOOKBACK 根）→ 不熔断", m.market_crash_halt(_index([4000.0, 3000.0]), cfg) is None)
check("B2: None / 空表 → 不熔断", m.market_crash_halt(None, cfg) is None
      and m.market_crash_halt(pd.DataFrame(), cfg) is None)
check("B2: LOOKBACK=0 → 关闭熔断", m.market_crash_halt(_crash, replace(cfg, MARKET_CRASH_LOOKBACK=0)) is None)
check("B2: 含 NaN 的指数序列不崩",
      m.market_crash_halt(_index(np.concatenate([np.full(25, np.nan), np.full(5, 4000.0)])), cfg) is None)

# 突破策略口径未被父类新字段污染
_vb = vb.VolumeBreakoutConfig(RECOMMENDATION_MODE="technical")
check("B2: 突破策略 NEUTRAL_MAX_PICKS 仍为 3（子类覆盖生效）", _vb.NEUTRAL_MAX_PICKS == 3)
check("B2: 突破策略 BEAR_MAX_PICKS_BREAKOUT 仍为 0（熊市空仓）", _vb.BEAR_MAX_PICKS_BREAKOUT == 0)
check("B2: 突破策略 MAX_PICKS 仍为 5", _vb.MAX_PICKS == 5)
check("B2: 突破策略独立熔断参数可继承", hasattr(_vb, "MARKET_CRASH_HALT_PCT"))


# ===========================================================================
print(f"\n{sum(1 for _, ok in _RESULTS if ok)}/{len(_RESULTS)} 通过")
_fails = [lb for lb, ok in _RESULTS if not ok]
if _fails:
    print("失败项：")
    for lb in _fails:
        print(f"  - {lb}")
sys.exit(0 if all(ok for _, ok in _RESULTS) else 1)
