"""P0/P1/P2 修复项验证脚本（合成数据，不访问网络）。

2026-09-15 双策略评审后的修复回归测试，覆盖：
1.  配置层：新增字段存在（SCREEN_TIME_BUDGET_MIN/PROGRESS_LOG_EVERY/MAX_ATR_PCT/
    FUND_LOOKBACK_QUARTERS=4/UNKNOWN_AS_BEAR/WEEKLY_REQUIRE_CLOSED_BAR/
    MARKET_REGIME_HYSTERESIS/DAILY_TOTAL_MAX_PICKS），死配置删除
    （MIN_RR_RATIO/DAILY_RSI_OVERBOUGHT_PENALTY/MIN_RR_RATIO_BREAKOUT），
    fallback 默认关闭
2.  ROE 线性年化因子（Q1×4/Q2×2/Q3×4/3/Q4×1）
3.  流动性否决独立归因 FAIL_LIQUIDITY（不再混入 FAIL_DATA）
4.  波动率否决 FAIL_VOLATILE 替代旧 FAIL_RR；compute_risk_reward 不再返回 passes
5.  周线未收盘 bar 剔除（本周内非周五的末根周线被剔除，上周五保留）
6.  regime 滞回（切换须连续 2 个交易日确认，且每自然日最多推进一次计数，
    使同日内多策略依次运行不会把「连续 N 日」缩短为「同日确认」；unknown 沿用已确认状态）
7.  突破策略盘中剔除用北京时间（当日 bar 的处理与北京时刻一致）
8.  volume_percentile 向量化结果与 rolling.apply 参考实现逐点一致（含 NaN）
9.  假突破过滤向量化结果与原三重循环参考实现逐点一致
10. 突破评分重校准：浅幅度 L2 突破在新公式下可通过 B 级准入（旧纯乘法公式不能）
11. 突破策略 RR 下限检查已删除（深幅度 L2/L1 不受摆设检查拦截）
12. 波动率风控否决明细采集（FAIL_VOLATILE → volatile_out）：技术分/等级/ATR%与上限/风险档
    /风险提示/已达标项，日志逐只打印（`[VOLATILE]` 区块）与飞书高风险观察池共用同一结构；
    不传 volatile_out 时行为与旧版完全一致（向后兼容）；两策略共用同一记录与渲染实现
"""
import json
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import numpy as np
import pandas as pd

from src import bottom_fishing_strategy as m
from src import volume_breakout_strategy as vb

_RESULTS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, ok))


# ===========================================================================
# 1) 配置层
# ===========================================================================
cfg = m.StrategyConfig()
vcfg = vb.VolumeBreakoutConfig()

check("配置: SCREEN_TIME_BUDGET_MIN 存在", getattr(cfg, "SCREEN_TIME_BUDGET_MIN", None) == 240.0)
check("配置: PROGRESS_LOG_EVERY 存在", getattr(cfg, "PROGRESS_LOG_EVERY", None) == 500)
check("配置: MAX_ATR_PCT=3.33", getattr(cfg, "MAX_ATR_PCT", None) == 3.33)
check("配置: FUND_LOOKBACK_QUARTERS=4", getattr(cfg, "FUND_LOOKBACK_QUARTERS", None) == 4)
check("配置: 旧 FUND_QUARTER_LOOKBACK 已移除", not hasattr(cfg, "FUND_QUARTER_LOOKBACK"))
check("配置: UNKNOWN_AS_BEAR 显式存在", getattr(cfg, "UNKNOWN_AS_BEAR", None) is True)
check("配置: WEEKLY_REQUIRE_CLOSED_BAR 默认开", getattr(cfg, "WEEKLY_REQUIRE_CLOSED_BAR", None) is True)
check("配置: MARKET_REGIME_HYSTERESIS 默认开", getattr(cfg, "MARKET_REGIME_HYSTERESIS", None) is True)
check("配置: MARKET_REGIME_CONFIRM_DAYS=2", getattr(cfg, "MARKET_REGIME_CONFIRM_DAYS", None) == 2)
check("配置: DAILY_TOTAL_MAX_PICKS=7", getattr(cfg, "DAILY_TOTAL_MAX_PICKS", None) == 7)
check("配置: ENABLE_DAILY_FALLBACK 默认关闭", getattr(cfg, "ENABLE_DAILY_FALLBACK", None) is False)
check("配置: MIN_RR_RATIO 已删除", not hasattr(cfg, "MIN_RR_RATIO"))
check("配置: DAILY_RSI_OVERBOUGHT_PENALTY 已删除", not hasattr(cfg, "DAILY_RSI_OVERBOUGHT_PENALTY"))
check("配置: MIN_RR_RATIO_BREAKOUT 已删除", not hasattr(vcfg, "MIN_RR_RATIO_BREAKOUT"))
check("配置: 突破策略继承新运行保障字段",
      getattr(vcfg, "SCREEN_TIME_BUDGET_MIN", None) == 240.0
      and getattr(vcfg, "PROGRESS_LOG_EVERY", None) == 500)
check("模块: fetch_stats 存在且三键齐全",
      all(k in m.fetch_stats for k in ("bs_ok", "ak_ok", "fail")))
check("模块: run_concurrent_screen 共享骨架存在", callable(m.run_concurrent_screen))
check("模块: socket 全局超时已设置",
      __import__("socket").getdefaulttimeout() == m.SOCKET_TIMEOUT)

# ===========================================================================
# 2) ROE 线性年化
# ===========================================================================
check("ROE年化: Q1×4", m._annualize_roe(2.0, 1) == 8.0)
check("ROE年化: Q2×2", m._annualize_roe(3.0, 2) == 6.0)
check("ROE年化: Q3×4/3", abs(m._annualize_roe(4.5, 3) - 6.0) < 1e-9)
check("ROE年化: Q4×1", m._annualize_roe(6.0, 4) == 6.0)

# ===========================================================================
# 3) 流动性否决独立归因 FAIL_LIQUIDITY
# ===========================================================================
def make_df(closes, last_vol_mult=3.0, vol_base=10_000_000, hi_mult=1.01, lo_mult=0.99,
            volatile_span=None):
    """合成日 K。volatile_span=(start,end,hi,lo) 给指定区间单独的振幅（构造高 ATR）。"""
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    dates = pd.date_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    open_ = np.concatenate([[closes[0]], closes[:-1]])
    high = np.maximum(open_, closes) * hi_mult
    low = np.minimum(open_, closes) * lo_mult
    if volatile_span is not None:
        s, e, vhi, vlo = volatile_span
        high[s:e] = np.maximum(open_[s:e], closes[s:e]) * vhi
        low[s:e] = np.minimum(open_[s:e], closes[s:e]) * vlo
    volume = np.full(n, vol_base, dtype=float)
    volume[-1] = vol_base * last_vol_mult
    amount = closes * volume
    pct_chg = pd.Series(closes).pct_change().fillna(0) * 100
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low, "close": closes,
        "volume": volume, "amount": amount, "pct_chg": pct_chg.values,
    })


def base_closes():
    """缓跌 20→17（90天）+ 加速跌 17→14.5（25天）+ 走平 4 天 + 反弹 3.5%（与单测 A 同构）"""
    part1 = np.linspace(20.0, 17.0, 90)
    part2 = np.linspace(17.0, 14.5, 25)[1:]
    closes = np.concatenate([part1, part2, np.full(4, 14.5)])
    return np.append(closes, closes[-1] * 1.035)


_FUND_OK = {"roe": 12.0, "debt_ratio": 45.0, "goodwill_ratio": 3.0, "deducted_profit_ratio": 0.9}
_NEUTRAL = {"regime": "neutral"}

df_a = make_df(base_closes())
_, r_ok = m.evaluate(df_a, code="600000", name="测试", config=cfg, market_env=_NEUTRAL, fund_data=_FUND_OK)
check("流动性: 正常标的归因不是 FAIL_LIQUIDITY", r_ok != "FAIL_LIQUIDITY")

df_low = make_df(base_closes(), vol_base=1_000_000)  # 日均成交额 ≈ 1450 万 < 3000 万
_, r_liq = m.evaluate(df_low, code="600000", name="测试", config=cfg, market_env=_NEUTRAL, fund_data=_FUND_OK)
check("流动性: 僵尸股 FAIL_LIQUIDITY（不再混入 FAIL_DATA）", r_liq == "FAIL_LIQUIDITY")

# ===========================================================================
# 4) 波动率否决 FAIL_VOLATILE 替代 FAIL_RR
# ===========================================================================
# 中部 30 根 ±6% 宽幅震荡抬高 ATR（末端 20 根保持正常振幅，避免触发区间位置否决）
df_volatile = make_df(base_closes(), volatile_span=(60, 90, 1.07, 0.93))
_, r_vol = m.evaluate(df_volatile, code="600000", name="测试", config=cfg,
                      market_env=_NEUTRAL, fund_data=_FUND_OK)
_out_vol = m.compute_daily_signals(df_volatile, cfg)
_atr_pct = float(_out_vol["atr"].iloc[-1]) / float(_out_vol["close"].iloc[-1]) * 100
check(f"波动率: 高 ATR({_atr_pct:.2f}%) 否决 FAIL_VOLATILE",
      _atr_pct > cfg.MAX_ATR_PCT and r_vol == "FAIL_VOLATILE")

rr = m.compute_risk_reward(10.0, cfg, atr=0.2)
check("RR: compute_risk_reward 不再返回 passes", "passes" not in rr)
check("RR: 止损止盈仍计算", rr["stop_loss"] > 0 and rr["take_profit"] > 10.0 and rr["rr_ratio"] > 0)

# ===========================================================================
# 4b) 波动率风控否决明细采集（日志逐只打印 + 飞书高风险观察池）
# ===========================================================================
_vol_rows: list[dict] = []
_, r_vol2 = m.evaluate(df_volatile, code="600000", name="波动测试", config=cfg,
                       market_env=_NEUTRAL, fund_data=_FUND_OK, volatile_out=_vol_rows)
check("观察池: 传入 volatile_out 后被否决标的被记录且归因不变",
      r_vol2 == "FAIL_VOLATILE" and len(_vol_rows) == 1)
if _vol_rows:
    _vr = _vol_rows[0]
    check("观察池: 记录含代码/名称/现价/ATR%/上限/倍数",
          _vr["code"] == "600000" and _vr["name"] == "波动测试" and _vr["close"] > 0
          and _vr["atr_pct"] > cfg.MAX_ATR_PCT and _vr["atr_limit"] == cfg.MAX_ATR_PCT
          and abs(_vr["atr_ratio"] - _vr["atr_pct"] / cfg.MAX_ATR_PCT) < 0.02)
    check("观察池: 风险等级与风险提示已生成",
          _vr["risk_level"] in ("轻度超限", "偏高", "高", "极高")
          and "ATR" in _vr["risk_note"] and "止损" in _vr["risk_note"])
    check("观察池: 记录含评分/等级/RSI/量比等展示字段",
          _vr["score"] > 0 and _vr["grade"] in ("A", "B", "C", "D")
          and _vr["rsi"] is not None and _vr["vol_ratio"] is not None)

# 未传 volatile_out 时不产生任何副作用（旧调用签名完全兼容）
_, r_vol3 = m.evaluate(df_volatile, code="600000", name="测试", config=cfg,
                       market_env=_NEUTRAL, fund_data=_FUND_OK)
check("观察池: 不传 volatile_out 时行为与旧版一致", r_vol3 == "FAIL_VOLATILE")

# 通过的标的不会被误记入观察池
_pass_rows: list[dict] = []
_, r_pass = m.evaluate(df_a, code="600000", name="测试", config=cfg,
                       market_env=_NEUTRAL, fund_data=_FUND_OK, volatile_out=_pass_rows)
check("观察池: 通过前置层的正常标的不会进入观察池", r_pass == "PASS" and not _pass_rows)

# 风险分档阈值（3.33% 为抄底上限）
check("风险档: 2.0× 及以上为极高", m._atr_risk_level(7.0, 3.33) == "极高")
check("风险档: 1.5× 为高", m._atr_risk_level(5.0, 3.33) == "高")
check("风险档: 1.2× 为偏高", m._atr_risk_level(4.0, 3.33) == "偏高")
check("风险档: 略超上限为轻度超限", m._atr_risk_level(3.4, 3.33) == "轻度超限")

# 展示排序：技术分降序 → ATR% 降序（只影响展示顺序，这些标的都已被否决）
_ordered = m.sort_volatile([
    {"code": "600003", "score": 70.0, "atr_pct": 5.0},
    {"code": "600001", "score": 82.0, "atr_pct": 4.0},
    {"code": "600002", "score": 82.0, "atr_pct": 6.0},
])
check("观察池排序: 技术分降序、同分按 ATR% 降序",
      [r["code"] for r in _ordered] == ["600002", "600001", "600003"])

# 日志输出：逐只打印（用内存 handler 捕获，避免污染测试输出）
import io
import logging as _logging

_buf = io.StringIO()
_h = _logging.StreamHandler(_buf)
_logger = _logging.getLogger("test.volatile")
_logger.addHandler(_h)
_logger.setLevel(_logging.INFO)
m.log_volatile_rejects(_vol_rows * 3, _logger, top=2)
_log_text = _buf.getvalue()
check("日志: 打印 [VOLATILE] 区块并逐只输出明细",
      "[VOLATILE] 波动率风控否决 3 只" in _log_text and _log_text.count("  · ") == 2)
check("日志: 超出上限时提示剩余条数", "其余 1 只" in _log_text)

# 飞书区块：由 notify.feishu 渲染「高风险观察池」，必须显式标注风险且不落库
try:
    from notify.feishu import _volatile_elements

    _vdf = pd.DataFrame(_vol_rows)
    _elems = _volatile_elements(_vdf, strategy="bottom_fishing")
    _blob = json.dumps(_elems, ensure_ascii=False)
    check("飞书: 生成波动率观察池区块并声明不构成买入建议",
          any("波动率风控否决 1 只" in str(e.get("content", "")) for e in _elems)
          and "不构成买入建议" in _blob)
    check("飞书: 区块内含风险等级与风险提示",
          "高风险观察" in _blob and "风险:" in _blob and "ATR" in _blob)
    check("飞书: 无数据时不产生空区块", _volatile_elements(None) == [] and _volatile_elements(pd.DataFrame()) == [])

    # 并发完成顺序不确定，渲染时必须统一排序，使卡片 Top-N 与日志 [VOLATILE] 区块一致
    _low = dict(_vol_rows[0]); _low.update({"code": "600009", "name": "低分股", "score": 30.0})
    _high = dict(_vol_rows[0]); _high.update({"code": "600010", "name": "高分股", "score": 95.0})
    _elems_sorted = _volatile_elements(pd.DataFrame([_low, _high]), strategy="bottom_fishing")
    _first_stock = str(_elems_sorted[2].get("content", ""))
    check("飞书: 卡片顺序按技术分降序（与日志一致）",
          "高分股" in _first_stock and "低分股" not in _first_stock)
except ImportError as _e:  # requests 等依赖缺失的环境：跳过并明确提示
    print(f"[SKIP] 飞书区块渲染测试（依赖缺失: {_e}）")

# ===========================================================================
# 5) 周线未收盘 bar 剔除
# ===========================================================================
_today = m._beijing_now().date()
_w_up = pd.DataFrame({
    "date": pd.date_range(end=pd.Timestamp(_today) - pd.Timedelta(days=7), periods=30, freq="W-FRI").strftime("%Y-%m-%d"),
    "close": np.linspace(10.0, 15.0, 30),
})
# 追加一根「本周内、非周五」的半成品周线（日期=今天；若今天恰为周五则用周四）
_incomplete_date = _today if _today.weekday() != 4 else (_today - timedelta(days=1))
_w_with_incomplete = pd.concat([
    _w_up,
    pd.DataFrame({"date": [str(_incomplete_date)], "close": [99.0]}),  # 异常高价，不剔除会显著拉偏 MA10
], ignore_index=True)

dropped = m._drop_incomplete_weekly_bar(_w_with_incomplete, cfg)
if _incomplete_date.isocalendar()[:2] == _today.isocalendar()[:2] and _incomplete_date.weekday() != 4:
    check("周线: 本周内非周五的末根被剔除", len(dropped) == 30)
else:  # 今天周五且测试日期为周四：落在本周且非周五 → 仍剔除
    check("周线: 本周内非周五的末根被剔除", len(dropped) == 30)

# 上周五（已收盘）必须保留
_last_fri = _today - timedelta(days=(_today.weekday() - 4) % 7 or 7)
_w_last_fri = pd.concat([
    _w_up,
    pd.DataFrame({"date": [str(_last_fri)], "close": [15.2]}),
], ignore_index=True)
if _last_fri.isocalendar()[:2] != _today.isocalendar()[:2] or _last_fri.weekday() == 4:
    check("周线: 已收盘周五 bar 保留", len(m._drop_incomplete_weekly_bar(_w_last_fri, cfg)) == 31)

# 关闭开关后不剔除
cfg_no_closed = replace(cfg, WEEKLY_REQUIRE_CLOSED_BAR=False)
check("周线: 开关关闭后不剔除", len(m._drop_incomplete_weekly_bar(_w_with_incomplete, cfg_no_closed)) == 31)

# ===========================================================================
# 6) regime 滞回
# ===========================================================================
with tempfile.TemporaryDirectory() as tmpdir:
    cfg_h = replace(cfg, CACHE_DIR=tmpdir, MARKET_REGIME_HYSTERESIS=True, MARKET_REGIME_CONFIRM_DAYS=2)
    _state_path = os.path.join(tmpdir, "market_regime_state.json")

    def _next_day() -> None:
        """把滞回状态的 updated 回拨 1 天，模拟「隔日再运行一次」。

        确认计数按自然日推进（防止一天内多次运行——如一个 workflow 顺序跑抄底 +
        突破——把「连续 N 个交易日」悄悄缩短为「同日确认」），所以同进程内连续调用
        不再等价于连续多日；必须显式改写 updated 才能真正模拟跨日。
        """
        with open(_state_path, encoding="utf-8") as fh:
            st = json.load(fh)
        st["updated"] = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        with open(_state_path, "w", encoding="utf-8") as fh:
            json.dump(st, fh, ensure_ascii=False)

    r1 = m._apply_regime_hysteresis({"regime": "bull", "description": "d1"}, cfg_h)
    check("滞回: 首次出现直接采用", r1["regime"] == "bull")

    _next_day()
    r2 = m._apply_regime_hysteresis({"regime": "bear", "description": "d2"}, cfg_h)
    check("滞回: 切换首日维持原 regime", r2["regime"] == "bull" and r2.get("regime_raw") == "bear")

    # 同一自然日内的重复运行不得推进确认计数（否则两套策略顺序跑一次即翻转）
    r2b = m._apply_regime_hysteresis({"regime": "bear", "description": "d2-same-day"}, cfg_h)
    check("滞回: 同日重复运行不推进计数", r2b["regime"] == "bull")

    _next_day()
    r3 = m._apply_regime_hysteresis({"regime": "bear", "description": "d3"}, cfg_h)
    check("滞回: 连续 2 个交易日确认后翻转", r3["regime"] == "bear")

    r4 = m._apply_regime_hysteresis({"regime": "unknown", "description": "d4"}, cfg_h)
    check("滞回: unknown 沿用已确认状态", r4["regime"] == "bear")

    _next_day()
    r5 = m._apply_regime_hysteresis({"regime": "neutral", "description": "d5"}, cfg_h)
    check("滞回: 新方向首日仍维持", r5["regime"] == "bear")

    _next_day()
    r6 = m._apply_regime_hysteresis({"regime": "neutral", "description": "d6"}, cfg_h)
    check("滞回: 新方向连续确认后翻转", r6["regime"] == "neutral")

# ===========================================================================
# 7) 突破策略盘中剔除用北京时间
# ===========================================================================
def make_breakout_df(n=100):
    closes = np.full(n, 10.0)
    dates = pd.date_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    volume = np.full(n, 12_000_000.0)
    return pd.DataFrame({
        "date": dates, "open": closes.copy(), "high": closes * 1.005, "low": closes * 0.995,
        "close": closes, "volume": volume, "amount": closes * volume,
        "pct_chg": np.zeros(n),
    })


df_bj = make_breakout_df()
df_bj.loc[df_bj.index[-1], "date"] = str(m._beijing_now().date())
out_bj = vb.compute_breakout_signals(df_bj, vcfg)
_now_bj = m._beijing_now()
if _now_bj.hour < 15:
    check("盘中剔除: 北京 15:00 前剔除当日 bar", out_bj is not None and len(out_bj) == 99)
else:
    check("盘中剔除: 北京 15:00 后保留当日 bar", out_bj is not None and len(out_bj) == 100)

# ===========================================================================
# 8) volume_percentile 向量化 == rolling.apply 参考实现（对拍策略真实输出）
# ===========================================================================
rng = np.random.default_rng(42)
vol_series = rng.uniform(1e6, 5e7, size=130)
vol_series[10] = np.nan
vol_series[80] = np.nan
_w = vcfg.ADAPTIVE_VOLUME_LOOKBACK + 1
ref = pd.Series(vol_series).rolling(_w).apply(lambda x: float(np.mean(x[:-1] <= x[-1])), raw=True).to_numpy()
_closes_vp = np.linspace(9.0, 10.0, 130)
_df_vp = pd.DataFrame({
    "date": pd.date_range("2025-01-01", periods=130).strftime("%Y-%m-%d"),
    "open": _closes_vp, "high": _closes_vp * 1.01, "low": _closes_vp * 0.99,
    "close": _closes_vp, "volume": vol_series, "amount": _closes_vp * np.nan_to_num(vol_series, nan=1e7),
    "pct_chg": pd.Series(_closes_vp).pct_change().fillna(0).to_numpy() * 100,
})
_out_vp = vb.compute_breakout_signals(_df_vp, vcfg)
vec = _out_vp["volume_percentile"].to_numpy()
_both_valid = ~(np.isnan(ref) | np.isnan(vec))
check("量能分位: 向量化与参考实现逐点一致（含 NaN 位置）",
      bool((np.isnan(ref) == np.isnan(vec)).all()) and
      bool(np.allclose(ref[_both_valid], vec[_both_valid], atol=1e-12)))

# ===========================================================================
# 9) 假突破过滤向量化 == 原三重循环参考实现
# ===========================================================================
def _failed_breakout_reference(out: pd.DataFrame, config) -> pd.Series:
    """原 O(n²) 三重循环参考实现（逐字保留旧语义用于对拍）。"""
    broke_l1 = out["breakout_l1"].astype(int)
    failed_pattern = pd.Series(False, index=out.index)
    lookback = config.FAILED_BREAKOUT_LOOKBACK
    for i in range(len(out)):
        start = max(0, i - lookback)
        for j in range(start, i):
            if not broke_l1.iloc[j]:
                continue
            anchor = out["level_l1"].iloc[j]
            if pd.isna(anchor):
                continue
            end = min(len(out), j + config.FAILED_BREAKOUT_CONFIRM_DAYS + 1)
            for k in range(j + 1, end):
                if k <= i and out["close"].iloc[k] < float(anchor) and i - k < lookback:
                    failed_pattern.iloc[i] = True
                    break
            if failed_pattern.iloc[i]:
                break
    return failed_pattern


# 构造含多次 L1 突破与跌回的序列：台阶式上行 + 两次假突破回落
_n = 110
_cl = np.full(_n, 10.0)
_cl[20:30] = 10.6   # 第一次冲高（L1 突破）后回落 → 假突破
_cl[30:50] = 10.1
_cl[55:70] = 11.0   # 第二次冲高
_cl[70:90] = 10.4
_cl[99] = 11.3      # 末端真突破
_dates = pd.date_range("2025-01-01", periods=_n).strftime("%Y-%m-%d")
_df_fb = pd.DataFrame({
    "date": _dates, "open": _cl.copy(), "high": _cl * 1.01, "low": _cl * 0.99,
    "close": _cl, "volume": np.full(_n, 2e7), "amount": _cl * 2e7,
    "pct_chg": pd.Series(_cl).pct_change().fillna(0).to_numpy() * 100,
})
_out_fb = vb.compute_breakout_signals(_df_fb, vcfg)
_ref_fb = _failed_breakout_reference(_out_fb, vcfg)
check("假突破过滤: 向量化与参考实现逐点一致",
      bool((_out_fb["recent_failed_breakout"].to_numpy() == _ref_fb.to_numpy()).all()))
check("假突破过滤: 确实识别出至少一段抑制区间", bool(_out_fb["recent_failed_breakout"].any()))

# ===========================================================================
# 10) 突破评分重校准：浅幅度 L2 可通过 B 级准入
# ===========================================================================
def make_l2_breakout_df(breakout_close: float):
    """60 日窗口内有一段更高的历史平台（L1 够不着），近 20 日窄幅整理后放量突破 20 日新高。"""
    n = 100
    closes = np.full(n, 10.0)
    # 历史高位平台必须落在末端 L1 窗口（days 39-98）内，否则被误判为 L1 突破
    closes[45:50] = 11.0  # L1 = 11.055，突破日够不到 → 只计 L2（20 日新高 10.05）
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = closes * 1.005
    lows = closes * 0.995
    volume = np.full(n, 12_000_000.0)
    # 突破日：放量 2.5 倍、阳线实体、收近最高
    opens[-1] = 10.02
    closes[-1] = breakout_close
    highs[-1] = breakout_close * 1.005
    lows[-1] = 10.0
    volume[-1] = 30_000_000.0
    dates = pd.date_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    pct = pd.Series(closes).pct_change().fillna(0) * 100
    return pd.DataFrame({
        "date": dates, "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volume, "amount": closes * volume, "pct_chg": pct.values,
    })


sig_l2, r_l2 = vb.evaluate_breakout(make_l2_breakout_df(10.25), code="600000", name="测试",
                                    config=vcfg, market_env=_NEUTRAL, fund_data=_FUND_OK)
check(f"校准: 浅幅度 L2 突破 PASS（旧乘法公式得分不足 B 级）: reason={r_l2}",
      r_l2 == "PASS" and sig_l2 is not None and sig_l2.breakout_level == 2 and sig_l2.score >= 60)

sig_l2d, r_l2d = vb.evaluate_breakout(make_l2_breakout_df(10.45), code="600000", name="测试",
                                      config=vcfg, market_env=_NEUTRAL, fund_data=_FUND_OK)
check(f"校准: 深幅度 L2 突破 PASS: reason={r_l2d}",
      r_l2d == "PASS" and sig_l2d is not None and sig_l2d.breakout_level == 2)

# 旧公式对照（证明校准确有必要，而非测试数据过松）：
_l2_margin = (10.25 / (10.0 * 1.005) - 1) * 100
_mf = vb._smoothstep(_l2_margin, vcfg.BREAKOUT_MIN_MARGIN * 100, 5.0)
_old_score = vcfg.W_BREAKOUT * 0.75 * _mf
_new_score = vcfg.W_BREAKOUT * (0.55 * 0.75 + 0.45 * _mf)
check("校准: 浅幅度 L2 旧公式突破分 <8（被压制）", _old_score < 8.0)
check("校准: 浅幅度 L2 新公式突破分 >15（可达成 B 级）", _new_score > 15.0)

# ===========================================================================
# 11) 突破策略同样采集「波动率风控否决」明细（共用同一记录结构）
# ===========================================================================
# 突破策略的波动率层位于评分定级之前，故收紧 ATR 上限来触发该分支
_vol_bo: list[dict] = []
_vcfg_tight = replace(vcfg, MAX_ATR_PCT_BREAKOUT=0.5)
_sig_bo, r_bo = vb.evaluate_breakout(make_l2_breakout_df(10.25), code="600000", name="测试",
                                     config=_vcfg_tight, market_env=_NEUTRAL, fund_data=_FUND_OK,
                                     volatile_out=_vol_bo)
check("突破观察池: 波动率否决被记录且归因不变",
      r_bo == "FAIL_VOLATILE" and _sig_bo is None and len(_vol_bo) == 1)
check("突破观察池: 记录含上限/倍数/风险等级",
      bool(_vol_bo) and _vol_bo[0]["atr_limit"] == 0.5 and _vol_bo[0]["atr_ratio"] > 1.0
      and _vol_bo[0]["risk_level"] in ("轻度超限", "偏高", "高", "极高"))
check("突破观察池: 记录标注已通过的突破/量能条件",
      bool(_vol_bo) and "突破 L2" in _vol_bo[0]["signals_hit"] and "放量" in _vol_bo[0]["signals_hit"])

# 未传 volatile_out 时突破策略行为不变
_sig_bo2, r_bo2 = vb.evaluate_breakout(make_l2_breakout_df(10.25), code="600000", name="测试",
                                       config=_vcfg_tight, market_env=_NEUTRAL, fund_data=_FUND_OK)
check("突破观察池: 不传 volatile_out 时行为与旧版一致", r_bo2 == "FAIL_VOLATILE")

# ===========================================================================
# 汇总
# ===========================================================================
passed = sum(1 for _, ok in _RESULTS if ok)
print(f"\n{passed}/{len(_RESULTS)} 通过")
sys.exit(0 if passed == len(_RESULTS) else 1)
