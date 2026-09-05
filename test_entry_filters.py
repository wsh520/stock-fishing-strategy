"""入场质量过滤器验证脚本（合成数据，不访问网络）。

场景：
A 正常抄底信号（缓跌+超卖+小阳放量反弹）→ 期望 PASS
B 与 A 相同但当日涨 8.5%（追高）→ 期望 FAIL_CHASE
C 与 A 相同但开盘跳空高开 4% → 期望 FAIL_GAP
D 陡峭下降通道中的拐头（MA20 近5日斜率 < -4%）→ 期望 FAIL_TECH（trend_turn 被门控）
E 与 A 相同但 pct_chg/open 缺失 → 期望 PASS（数据缺失放行，不误杀）
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

results.append(run_case("D 陡峭下降通道拐头", make_df(steep_closes(), last_vol_mult=2.0), "FAIL_TECH"))


def steep_with_turn_closes():
    """陡跌至 14 元后走平 3 天再 +3% 反弹：ma5_turn=True 但 MA20 斜率仍 < -4%，验证门控本身"""
    closes = [25.0]
    for _ in range(112):
        closes.append(closes[-1] * 0.99)
    closes += [closes[-1]] * 3
    closes.append(closes[-1] * 1.03)
    return np.array(closes)


results.append(run_case("D2 下降通道中ma5拐头被门控", make_df(steep_with_turn_closes(), last_vol_mult=2.0), "FAIL_TECH"))

df_e = make_df(base_closes())
df_e["pct_chg"] = np.nan
df_e["open"] = np.nan
results.append(run_case("E pct_chg/open缺失放行", df_e, "PASS"))


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

for label, ok in checks:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
results.extend(checks and [ok for _, ok in checks])

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
