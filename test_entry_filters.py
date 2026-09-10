"""入场质量过滤器验证脚本（合成数据，不访问网络）。

场景：
A 正常抄底信号（缓跌+超卖+小阳放量反弹）→ 期望 PASS
B 与 A 相同但当日涨 8.5%（追高）→ 期望 FAIL_CHASE
C 与 A 相同但开盘跳空高开 4% → 期望 FAIL_GAP
D 陡峭下降通道中的拐头（MA20 近5日斜率 < -4%）→ 期望 FAIL_NO_TREND（trend_turn 被门控）
E 与 A 相同但 pct_chg/open 缺失 → 期望 PASS（数据缺失放行，不误杀）
J 评分达标但 trend_turn=False → 期望 FAIL_NO_TREND（REQUIRE_TREND_TURN 硬性拦截）
K 僵尸股（20日均成交额 <500万）→ 期望 FAIL_LIQUIDITY（独立归因，不再混入 FAIL_DATA）
L ATR >3.33% 现价（高波动）→ 期望 FAIL_VOLATILE（显式波动率上限，替代原 FAIL_RR）
另含 2026-09-10 评审修复单测：regime=unknown 视同 bear、ROE 线性年化、基本面报告期回退序列、
周线只用已收盘 bar（周内未收盘 bar 剔除）、确定性排序（评分→背离→流动性→代码）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import numpy as np
import pandas as pd

import bottom_fishing_strategy as m


def make_df(closes, last_vol_mult=3.0, last_open=None, vol_base=1_000_000):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    dates = pd.date_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    open_ = np.concatenate([[closes[0]], closes[:-1]])
    if last_open is not None:
        open_[-1] = last_open
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
    """缓跌 20→17（90天）+ 加速跌 17→14.5（25天）+ 走平 4 天 + 反弹 3.5%"""
    part1 = np.linspace(20.0, 17.0, 90)
    part2 = np.linspace(17.0, 14.5, 25)[1:]
    part3 = np.full(4, 14.5)
    closes = np.concatenate([part1, part2, part3])
    return np.append(closes, closes[-1] * 1.035)


def steep_closes():
    """连续 119 天每天 -1%，最后一天 +1% 小反弹（MA20 近5日斜率约 -5%）"""
    closes = [30.0]
    for _ in range(118):
        closes.append(closes[-1] * 0.99)
    closes.append(closes[-1] * 1.01)
    return np.array(closes)


def run_case(label, df, expect):
    sig, reason = m.evaluate(df, code="600000", name="测试", config=m.StrategyConfig(),
                             market_env={"regime": "neutral"}, fund_data=None)
    out = m.compute_daily_signals(df, m.StrategyConfig())
    last = out.iloc[-1]
    diag = (f"score={last['daily_score']:.1f} trend_turn={bool(last['trend_turn'])} "
            f"ma5_turn={bool(last['ma5_turn'])} rsi14={last['rsi14']:.1f} "
            f"vol_ratio={last['daily_vol_ratio']:.2f} ma20_slope={last['ma20_slope']:.4f}")
    ok = reason == expect
    print(f"[{'OK' if ok else 'FAIL'}] {label}: reason={reason} (期望 {expect}) | {diag}")
    return ok


results = []
df_a = make_df(base_closes())
results.append(run_case("A 正常抄底信号", df_a, "PASS"))

closes_b = base_closes()
closes_b[-1] = closes_b[-2] * 1.085
results.append(run_case("B 当日涨8.5%追高", make_df(closes_b), "FAIL_CHASE"))

df_c = make_df(base_closes(), last_open=base_closes()[-2] * 1.04)
results.append(run_case("C 跳空高开4%", df_c, "FAIL_GAP"))

results.append(run_case("D 陡峭下降通道拐头", make_df(steep_closes(), last_vol_mult=2.0), "FAIL_NO_TREND"))


def steep_with_turn_closes():
    """陡跌至 14 元后走平 3 天再 +3% 反弹：ma5_turn=True 但 MA20 斜率仍 < -4%，验证门控本身"""
    closes = [25.0]
    for _ in range(112):
        closes.append(closes[-1] * 0.99)
    closes += [closes[-1]] * 3
    closes.append(closes[-1] * 1.03)
    return np.array(closes)


results.append(run_case("D2 下降通道中ma5拐头被门控", make_df(steep_with_turn_closes(), last_vol_mult=2.0), "FAIL_NO_TREND"))

df_e = make_df(base_closes())
df_e["pct_chg"] = np.nan
df_e["open"] = np.nan
results.append(run_case("E pct_chg/open缺失放行", df_e, "PASS"))

# J 连续质量分达标但无拐点的旁路拦截：即使其他维度得分足够，
# trend_turn=False（本用例通过临时翻转末行模拟）仍须被 REQUIRE_TREND_TURN 硬性否决
_orig_cds = m.compute_daily_signals

def _cds_no_trend(df, cfg):
    out = _orig_cds(df, cfg)
    if out is not None and not out.empty:
        out.loc[out.index[-1], "trend_turn"] = False
    return out

m.compute_daily_signals = _cds_no_trend
results.append(run_case("J 无趋势转折60分旁路拦截", df_a, "FAIL_NO_TREND"))
m.compute_daily_signals = _orig_cds


def rebound_too_far_closes():
    """底部后连续 4 天 +4%：RSI 已冲高（>65），不再是底部入场点"""
    closes = base_closes()[:-1]
    for _ in range(4):
        closes = np.append(closes, closes[-1] * 1.04)
    return closes


results.append(run_case("F RSI过高(已反弹一段)", make_df(rebound_too_far_closes(), last_vol_mult=2.0), "FAIL_RSI_HIGH"))
results.append(run_case("G 天量(量比6x)", make_df(base_closes(), last_vol_mult=6.0), "FAIL_CLIMAX_VOL"))


def near_high_closes():
    """一路上涨 10→20 后小幅回调再反弹：距高点回撤 <10%，不符合抄底定位"""
    part1 = np.linspace(10.0, 20.0, 114)
    part2 = np.array([19.5, 19.2, 19.0, 18.9, 18.9])
    closes = np.concatenate([part1, part2])
    return np.append(closes, closes[-1] * 1.015)


results.append(run_case("H 非底部区域(距高点<10%)", make_df(near_high_closes(), last_vol_mult=2.0), "FAIL_NOT_BOTTOM"))


def rally_back_closes():
    """跌到15后已在20日内反弹至16附近：60日回撤够（19%），但现价位于20日区间顶部"""
    part1 = np.linspace(20.0, 17.0, 90)
    part2 = np.linspace(17.0, 15.0, 10)[1:]
    part3 = np.linspace(15.0, 15.9, 19)[1:]
    closes = np.concatenate([part1, part2, part3])
    return np.append(closes, closes[-1] * 1.01)


results.append(run_case("I 区间位置偏高(已反弹至区间顶部)", make_df(rally_back_closes(), last_vol_mult=2.0), "FAIL_POSITION"))

# ===== 判定函数隔离单测 =====
cfg = m.StrategyConfig()
out_a = m.compute_daily_signals(df_a, cfg)
checks = []
checks.append(("_range_position_ok 基准放行", m._range_position_ok(out_a, cfg) is True))
checks.append(("_range_position_ok 区间顶部否决", m._range_position_ok(m.compute_daily_signals(make_df(rally_back_closes()), cfg), cfg) is False))
checks.append(("_macd_momentum_ok 基准放行", m._macd_momentum_ok(out_a, cfg) is True))
checks.append(("_kdj_ok 基准放行", m._kdj_ok(out_a, cfg) is True))

# MACD 动能恶化：反弹后再次大跌，MACD 柱当日走低
_md = np.concatenate([np.linspace(20.0, 15.0, 110), [15.6, 15.6, 15.3]])
out_md = m.compute_daily_signals(make_df(_md), cfg)
checks.append(("_macd_momentum_ok 恶化否决", m._macd_momentum_ok(out_md, cfg) is False))

# KDJ 高位：持续上涨后在高位运行（K>60）
_kd = np.concatenate([np.linspace(10.0, 20.0, 118), [19.9, 19.8]])
out_kd = m.compute_daily_signals(make_df(_kd), cfg)
checks.append(("_kdj_ok 高位否决", m._kdj_ok(out_kd, cfg) is False))

# 周线趋势确认
_w_up = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=30, freq="W").strftime("%Y-%m-%d"),
                      "close": np.linspace(10.0, 15.0, 30)})
_w_down = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=30, freq="W").strftime("%Y-%m-%d"),
                        "close": np.linspace(10.0, 6.0, 30)})
checks.append(("周线站上MA10放行", m.check_weekly_trend(_w_up, cfg) is True))
checks.append(("周线主跌否决", m.check_weekly_trend(_w_down, cfg) is False))
checks.append(("周线数据不足放行", m.check_weekly_trend(_w_up.head(5), cfg) is True))
checks.append(("周线None放行", m.check_weekly_trend(None, cfg) is True))

# ===== 2026-09-10 评审修复验证 =====
from datetime import datetime as _dt

# K 流动性独立归因：成交额缩到千分之一（20日均额 <500万）→ FAIL_LIQUIDITY 而非 FAIL_DATA
df_k = make_df(base_closes())
df_k["amount"] = df_k["amount"] * 0.001
results.append(run_case("K 僵尸股流动性独立归因", df_k, "FAIL_LIQUIDITY"))

# L 波动率上限：末行 ATR 抬到现价 5%（> MAX_ATR_PCT=3.33）→ FAIL_VOLATILE
_orig_cds2 = m.compute_daily_signals

def _cds_high_atr(df, cfg):
    out = _orig_cds2(df, cfg)
    if out is not None and not out.empty:
        out.loc[out.index[-1], "atr"] = float(out.iloc[-1]["close"]) * 0.05
    return out

m.compute_daily_signals = _cds_high_atr
results.append(run_case("L 波动率上限否决(ATR=5%现价)", df_a, "FAIL_VOLATILE"))
m.compute_daily_signals = _orig_cds2

# M regime=unknown 保守视同 bear（UNKNOWN_AS_BEAR）：同数据同配置下结果应与 bear 一致；
# 关闭开关则应与 neutral 一致；Signal.market_env 仍保留原始 unknown 口径
_ev = lambda env, c: m.evaluate(df_a, code="600000", name="测试", config=c, market_env={"regime": env}, fund_data=None)
cfg_open = m.StrategyConfig(); cfg_open.UNKNOWN_AS_BEAR = False
_sig_unknown, r_unknown = _ev("unknown", m.StrategyConfig())
_, r_bear = _ev("bear", m.StrategyConfig())
_, r_unknown_open = _ev("unknown", cfg_open)
_, r_neutral = _ev("neutral", cfg_open)
checks.append(("unknown 与 bear 行为一致", r_unknown == r_bear))
checks.append(("unknown+关闭开关与 neutral 一致", r_unknown_open == r_neutral))
checks.append(("Signal.market_env 保留原始 unknown", (_sig_unknown is None) or (_sig_unknown.market_env == "unknown")))

# N ROE 线性年化与基本面报告期回退序列
checks.append(("ROE年化 Q1×4", abs(m._annualize_roe(5.0, 1) - 20.0) < 1e-9))
checks.append(("ROE年化 Q2×2", abs(m._annualize_roe(5.0, 2) - 10.0) < 1e-9))
checks.append(("ROE年化 Q3×4/3", abs(m._annualize_roe(5.0, 3) - 20.0 / 3) < 1e-9))
checks.append(("ROE年化 Q4原值", abs(m._annualize_roe(5.0, 4) - 5.0) < 1e-9))
checks.append(("ROE季度未知不年化", abs(m._annualize_roe(5.0, None) - 5.0) < 1e-9))
_qc = m._bs_quarter_candidates(4)
_ok_qc = (len(_qc) == 4 and all(q in (1, 2, 3, 4) for _, q in _qc)
          and all(_qc[i + 1] == (_qc[i][0] - (1 if _qc[i][1] == 1 else 0), 4 if _qc[i][1] == 1 else _qc[i][1] - 1)
                  for i in range(3)))
checks.append(("报告期回退序列逐季向前", _ok_qc))

# O 周线只用已收盘 bar：本自然周内非周五的末 bar 视为未收盘剔除；周五/周末运行保留周五 bar
_now_thu = _dt(2026, 9, 10)  # 星期四
_wk = pd.DataFrame({"date": ["2026-08-28", "2026-09-04", "2026-09-09"],  # 五、五(上周)、三(本周)
                    "close": [10.0, 10.5, 11.0]})
checks.append(("周内未收盘bar被剔除", list(m._drop_incomplete_weekly_bar(_wk, now=_now_thu)["date"]) == ["2026-08-28", "2026-09-04"]))
_wk2 = pd.DataFrame({"date": ["2026-09-04", "2026-09-11"], "close": [10.0, 10.5]})
checks.append(("周五当日bar保留", list(m._drop_incomplete_weekly_bar(_wk2, now=_dt(2026, 9, 11))["date"]) == ["2026-09-04", "2026-09-11"]))
checks.append(("周末运行保留周五bar", list(m._drop_incomplete_weekly_bar(_wk2, now=_dt(2026, 9, 12))["date"]) == ["2026-09-04", "2026-09-11"]))

# 周线确认对「周内暴跌的未收盘 bar」免疫：上升周线 + 本周中途暴跌 bar（动态取当前 ISO 周内
# 非周五日期，保证任何一天运行测试该 bar 都落在本周）→ 剔除后仍应放行
_today = pd.Timestamp(_dt.now().date())
_wd = _today.weekday()
_inc_date = (_today if _wd <= 3 else _today - pd.Timedelta(days=(_wd - 2) % 7)).strftime("%Y-%m-%d")
_w_trend = pd.DataFrame({"date": pd.date_range("2025-01-03", periods=40, freq="W-FRI").strftime("%Y-%m-%d"),
                         "close": np.linspace(10.0, 15.0, 40)})
_w_trend = pd.concat([_w_trend, pd.DataFrame({"date": [_inc_date], "close": [5.0]})], ignore_index=True)
checks.append(("周线确认忽略周内未收盘暴跌bar", m.check_weekly_trend(_w_trend, cfg) is True))

# P 确定性排序：连续评分↓ → 底背离↓ → 20日均成交额↓ → 代码↑
_sig_rows = [
    {"code": "600002", "score": 72.1, "has_divergence": False, "avg_amount": 8000.0},
    {"code": "600001", "score": 72.1, "has_divergence": True, "avg_amount": 7000.0},
    {"code": "600003", "score": 88.4, "has_divergence": False, "avg_amount": 1000.0},
    {"code": "600000", "score": 72.1, "has_divergence": False, "avg_amount": 9000.0},
]
_sorted = pd.DataFrame(_sig_rows).sort_values(m.SORT_BY, ascending=m.SORT_ASC).reset_index(drop=True)
checks.append(("确定性排序: 评分→背离→流动性→代码", list(_sorted["code"]) == ["600003", "600001", "600000", "600002"]))

# PASS 信号携带 avg_amount（万元，排序次级键 + 飞书展示）
_s_a, _ = m.evaluate(df_a, code="600000", name="测试", config=m.StrategyConfig(),
                     market_env={"regime": "neutral"}, fund_data=None)
checks.append(("PASS信号含20日均成交额(万)", _s_a is not None and _s_a.avg_amount > 0))

# Q 最近3日趋势窗口：触发日及后续两日有效，第4日不再沿用旧触发
_raw_turn = pd.Series([False, True, False, False, False])
_structure_ok = pd.Series([True] * 5)
_ma20_ok = pd.Series([True] * 5)
_recent_turn = m._recent_trend_turn(_raw_turn, _structure_ok, _ma20_ok, 3,
                                    pd.Series([11.] * 5), pd.Series([10.] * 5))
checks.append(("趋势转折最近3日内保持有效", bool(_recent_turn.iloc[3])))
checks.append(("趋势转折超过3日不沿用", not bool(_recent_turn.iloc[4])))

# R 同源动能组封顶，且连续总分严格等于各独立维度之和
_last_a = out_a.iloc[-1]
_parts_sum = sum(float(_last_a[k]) for k in ("trend_score", "momentum_score", "location_score", "volume_score", "divergence_score"))
checks.append(("动能组得分不突破25分", float(_last_a["momentum_score"]) <= cfg.W_MOMENTUM_QUALITY + 1e-9))
checks.append(("连续总分等于分项之和", abs(float(_last_a["daily_score"]) - round(_parts_sum, 1)) < 1e-9))
_df_vol_soft = make_df(base_closes(), last_vol_mult=1.3)
_out_vol_soft = m.compute_daily_signals(_df_vol_soft, cfg)
checks.append(("量价强度产生连续分差", float(_out_vol_soft.iloc[-1]["daily_score"]) != float(_last_a["daily_score"])))

# S 波谷配对底背离：第二个更低波谷的 RSI/MACD 更高，随后出现右侧确认
_n = 32
_div_frame = pd.DataFrame({
    "low": np.full(_n, 12.0),
    "rsi14": np.full(_n, 40.0),
    "macd_diff": np.zeros(_n),
})
_div_frame.loc[9:13, "low"] = [11.0, 10.0, 11.0, 11.5, 12.0]
_div_frame.loc[19:23, "low"] = [10.8, 9.8, 10.7, 11.2, 12.0]
_div_frame.loc[10, ["rsi14", "macd_diff"]] = [20.0, -1.0]
_div_frame.loc[20, ["rsi14", "macd_diff"]] = [28.0, -0.5]
_right = pd.Series(False, index=_div_frame.index)
_right.iloc[21] = True
_div_result = m._paired_bottom_divergence(_div_frame, _right, cfg)
checks.append(("波谷确认前不提前使用背离", not bool(_div_result.iloc[21])))
checks.append(("波谷配对背离确认后生效", bool(_div_result.iloc[22])))
_div_frame.loc[20, ["rsi14", "macd_diff"]] = [18.0, -1.2]
_no_div = m._paired_bottom_divergence(_div_frame, _right, cfg)
checks.append(("指标未抬高不构成底背离", not bool(_no_div.iloc[22])))

# T 兼容开关仍能从动能组移除 MACD / KDJ 成员
_cfg_no_mk = m.StrategyConfig()
_cfg_no_mk.REQUIRE_MACD_MOMENTUM = False
_cfg_no_mk.REQUIRE_KDJ_GOLDEN = False
_out_no_mk = m.compute_daily_signals(df_a, _cfg_no_mk)
checks.append(("关闭MACD/KDJ成员后仅保留RSI确认", int(_out_no_mk.iloc[-1]["momentum_confirmations"]) == 1))

# U 趋势破位撤销不可复活，独立新触发可恢复（仍保留当前结构和MA20门控）
_turns = pd.Series([True, False, False, True])
_close = pd.Series([11., 9., 11., 11.])
_low = pd.Series([10., 8., 10., 10.])
_true = pd.Series([True] * 4)
_valid = m._recent_trend_turn(_turns, _true, _true, 3, _close, _low)
checks.append(("趋势破位后旧触发不复活且新触发恢复", list(_valid) == [True, False, False, True]))
_gate = _true.copy(); _gate.iloc[3] = False
checks.append(("新触发仍受MA20门控", not m._recent_trend_turn(_turns, _true, _gate, 3, _close, _low).iloc[3]))

# V 已成立背离再次破低撤销；只能由新波谷配对恢复；长输入与各前缀严格一致
_div_frame.loc[20, ["rsi14", "macd_diff"]] = [28., -0.5]
_div_frame.loc[25, "low"] = 9.
_div_frame.loc[25, ["rsi14", "macd_diff"]] = [35., -0.2]
_right.iloc[26] = True
_broken = m._paired_bottom_divergence(_div_frame, _right, cfg)
checks.append(("背离破低撤销旧配对且新波谷确认恢复", bool(_broken.iloc[22]) and not _broken.iloc[25] and not _broken.iloc[26] and bool(_broken.iloc[27])))
_long_div = pd.concat([_div_frame, pd.DataFrame({"low": [12.] * 60, "rsi14": [40.] * 60, "macd_diff": [0.] * 60})], ignore_index=True)
_long_right = _right.reindex(_long_div.index, fill_value=False)
_long_result = m._paired_bottom_divergence(_long_div, _long_right, cfg)
checks.append(("背离所有前缀一致且历史不随总长度消失", all(
    m._paired_bottom_divergence(_long_div.iloc[:i], _long_right.iloc[:i], cfg).equals(_long_result.iloc[:i])
    for i in range(1, len(_long_div) + 1))))

_bad_pair = _div_frame.copy()
_bad_pair.loc[25, ["rsi14", "macd_diff"]] = [10., -2.]
_bad_result = m._paired_bottom_divergence(_bad_pair, _right, cfg)
checks.append(("无有效新配对时价格回升不复活旧背离", not _bad_result.iloc[25:].any()))

# W 分段smoothstep在正常量/原1.2边界/峰值/上限连续有界；缩量及下跌不给量价分
_ratios = pd.Series([1., 1.2-1e-6, 1.2, 1.2+1e-6, 2.5-1e-6, 2.5, 2.5+1e-6, 5.-1e-6, 5., 5.+1e-6])
_vq = m._volume_quality(_ratios, pd.Series(True, index=_ratios.index), cfg)
checks.append(("量价四边界连续且上限归零", _vq.iloc[0] == 0 and abs(_vq.iloc[3]-_vq.iloc[1]) < 1e-5 and abs(_vq.iloc[6]-_vq.iloc[4]) < 1e-5 and abs(_vq.iloc[7]-_vq.iloc[9]) < 1e-5 and _vq.iloc[8] == 0))
checks.append(("量价峰值为1且所有得分有界", _vq.iloc[5] == 1 and _vq.between(0, 1).all()))
checks.append(("下跌时不奖励放量", not m._volume_quality(_ratios, pd.Series(False, index=_ratios.index), cfg).any()))

# X 真实指标序列的反例：低位微升、修复后下行不确认；K>D但K向下也不确认
_mom = pd.DataFrame({"rsi14": [20., 20.1, 35., 36., 35.5], "kdj_k": [30., 29., 31., 32., 31.],
                     "kdj_d": [20.] * 5, "macd_histogram": [-1., -.9, -.8, -.7, -.6]})
_mac, _rsi, _kdj = m._momentum_members(_mom, cfg)
checks.append(("RSI需修复水平及当前改善幅度且回落失效", list(_rsi) == [False, False, True, True, False]))
checks.append(("KDJ金叉状态下K下行不确认", not _kdj.iloc[1] and not _kdj.iloc[4] and bool(_kdj.iloc[3])))
checks.append(("MACD保留当前改善定义", _mac.iloc[1:].all()))

# Y 实际OHLC计算逐前缀：不得用未来K线改写趋势、背离或总分
_full = m.compute_daily_signals(df_a, cfg)
_prefix_ok = True
for _end in range(cfg.MIN_DAYS, len(df_a) + 1):
    _prefix = m.compute_daily_signals(df_a.iloc[:_end], cfg)
    _cols = ["trend_turn", "bottom_divergence", "daily_score"]
    if not _prefix[_cols].equals(_full.iloc[:_end][_cols]):
        _prefix_ok = False
        break
checks.append(("真实OHLC趋势背离评分逐前缀一致", _prefix_ok))

for label, ok in checks:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
results.extend(checks and [ok for _, ok in checks])

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
