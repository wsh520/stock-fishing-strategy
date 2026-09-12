"""入场质量过滤器验证脚本（合成数据，不访问网络）。

说明：make_df 的成交额按真实主板个股量级构造（约 1~2 亿元/日），以匹配 MIN_AMOUNT
流动性下限。此前用 1500 万/日的量级会让所有用例都被流动性层拦掉，测不到真正的入场
质量逻辑。

场景：
A  正常抄底信号（缓跌+超卖+小阳放量反弹）                    → PASS
B  与 A 相同但当日涨 8.5%（追高）                             → FAIL_CHASE
C  与 A 相同但开盘跳空高开 4%                                 → FAIL_GAP
D  陡峭下降通道中的拐头（MA20 近5日斜率 < -4%）               → FAIL_MACD_MOM（动能层先拦截）
D2 下降通道中 ma5 拐头（MA20 斜率仍 < -4%，验证门控本身）      → FAIL_TECH
E  与 A 相同但 pct_chg/open 缺失                              → PASS（缺失放行，不误杀）
F  底部后连续 4 天 +4%：近 5 日累计 +17%（已反弹一段）         → FAIL_RECENT_RALLY
F2 连续 20 日小阳推升：RSI 冲高但近 5 日涨幅仅 +5%             → FAIL_RSI_HIGH
G  天量（量比 6x）                                            → FAIL_CLIMAX_VOL
H  非底部区域（距 60 日高点回撤 < 15%）                        → FAIL_NOT_BOTTOM
I  已反弹至 20 日区间顶部（回撤够但位置高）                    → FAIL_POSITION
K  60 日回撤 74%（崩盘型/价值陷阱）                           → FAIL_DEEP_CRASH
L  末端冲高 3.5% 后次日回落 3.5%：MACD 柱当日转弱              → FAIL_MACD_MOM
M  末端冲高 3.5% 后次日浅回落 3.0%：MACD 柱仍升但 KDJ 掉头     → FAIL_KDJ
"""
import os
import sys
from dataclasses import replace
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import numpy as np
import pandas as pd

import bottom_fishing_strategy as m


def make_df(closes, last_vol_mult=3.0, last_open=None, vol_base=10_000_000,
            last_high=None, last_low=None):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    dates = pd.date_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    open_ = np.concatenate([[closes[0]], closes[:-1]])
    if last_open is not None:
        open_[-1] = last_open
    high = np.maximum(open_, closes) * 1.01
    low = np.minimum(open_, closes) * 0.99
    if last_high is not None:
        high[-1] = last_high
    if last_low is not None:
        low[-1] = last_low
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


def steep_with_turn_closes():
    """陡跌至 14 元后走平 3 天再 +3% 反弹：ma5_turn=True 但 MA20 斜率仍 < -4%，验证门控本身"""
    closes = [25.0]
    for _ in range(112):
        closes.append(closes[-1] * 0.99)
    closes += [closes[-1]] * 3
    closes.append(closes[-1] * 1.03)
    return np.array(closes)


def rebound_too_far_closes():
    """底部后连续 4 天 +4%：近 5 日累计涨幅 +17%，已明显反弹一段"""
    closes = base_closes()[:-1]
    for _ in range(4):
        closes = np.append(closes, closes[-1] * 1.04)
    return closes


def rsi_high_closes():
    """底部后连续 20 天 +1%：RSI 已冲高，但近 5 日累计仅 +5%（不触发 recent_rally 分支）"""
    closes = base_closes()[:-1]
    for _ in range(20):
        closes = np.append(closes, closes[-1] * 1.01)
    return closes


def near_high_closes():
    """一路上涨 10→20 后小幅回调再反弹：距 60 日高点回撤 <10%，不符合抄底定位"""
    part1 = np.linspace(10.0, 20.0, 114)
    part2 = np.array([19.5, 19.2, 19.0, 18.9, 18.9])
    closes = np.concatenate([part1, part2])
    return np.append(closes, closes[-1] * 1.015)


def rally_back_closes():
    """跌到 15 后已在 20 日内反弹至 15.2 附近：60 日回撤够（约 16%），但现价位于 20 日区间顶部"""
    part1 = np.linspace(20.0, 17.0, 90)
    part2 = np.linspace(17.0, 15.0, 10)[1:]
    part3 = np.linspace(15.0, 15.2, 19)[1:]
    closes = np.concatenate([part1, part2, part3])
    return np.append(closes, closes[-1] * 1.01)


def deep_crash_closes():
    """高位横盘 70 天后连续 60 个交易日累计约 -74%：60 日回撤超上限，属崩盘型标的"""
    closes = [40.0] * 70
    for _ in range(60):
        closes.append(closes[-1] * 0.978)
    return np.array(closes)


def macd_weak_closes():
    """末端冲高 3.5% 后次日回落 3.5%：MACD 柱当日较昨日转弱"""
    return np.append(base_closes(), base_closes()[-1] * 0.965)


def kdj_weak_closes():
    """末端冲高 3.5% 后次日浅回落 3.0%：MACD 柱仍在上升，但 KDJ 动能掉头"""
    return np.append(base_closes(), base_closes()[-1] * 0.97)


def run_case(label, df, expect):
    sig, reason = m.evaluate(df, code="600000", name="测试", config=m.StrategyConfig(),
                             market_env={"regime": "neutral"}, fund_data=None)
    out = m.compute_daily_signals(df, m.StrategyConfig())
    if out is None or out.empty:
        diag = "compute_daily_signals=None（数据不足/流动性未达下限）"
    else:
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

results.append(run_case("D 陡峭下降通道拐头(MACD动能层先拦)", make_df(steep_closes(), last_vol_mult=2.0), "FAIL_MACD_MOM"))
results.append(run_case("D2 下降通道中ma5拐头被门控", make_df(steep_with_turn_closes(), last_vol_mult=2.0), "FAIL_TECH"))

df_e = make_df(base_closes())
df_e["pct_chg"] = np.nan
df_e["open"] = np.nan
results.append(run_case("E pct_chg/open缺失放行", df_e, "PASS"))

results.append(run_case("F 近5日累计涨幅过大", make_df(rebound_too_far_closes(), last_vol_mult=2.0), "FAIL_RECENT_RALLY"))
results.append(run_case("F2 RSI过高(已反弹一段)", make_df(rsi_high_closes(), last_vol_mult=2.0), "FAIL_RSI_HIGH"))
results.append(run_case("G 天量(量比6x)", make_df(base_closes(), last_vol_mult=6.0), "FAIL_CLIMAX_VOL"))
results.append(run_case("H 非底部区域(距高点回撤<15%)", make_df(near_high_closes(), last_vol_mult=2.0), "FAIL_NOT_BOTTOM"))
results.append(run_case("I 区间位置偏高(已反弹至区间顶部)", make_df(rally_back_closes(), last_vol_mult=2.0), "FAIL_POSITION"))
results.append(run_case("K 回撤过深(60日-74%)", make_df(deep_crash_closes(), last_vol_mult=2.0), "FAIL_DEEP_CRASH"))
results.append(run_case("L MACD柱当日转弱", make_df(macd_weak_closes(), last_vol_mult=2.0), "FAIL_MACD_MOM"))
results.append(run_case("M KDJ动能掉头", make_df(kdj_weak_closes(), last_vol_mult=2.0), "FAIL_KDJ"))

# ===== 判定函数隔离单测 =====
cfg = m.StrategyConfig()
out_a = m.compute_daily_signals(df_a, cfg)
checks = []
checks.append(("_range_position_ok 基准放行", m._range_position_ok(out_a, cfg) is True))
checks.append(("_range_position_ok 区间顶部否决", m._range_position_ok(m.compute_daily_signals(make_df(rally_back_closes()), cfg), cfg) is False))
checks.append(("_macd_momentum_ok 基准放行", m._macd_momentum_ok(out_a, cfg) is True))
checks.append(("_kdj_ok 基准放行", m._kdj_ok(out_a, cfg) is True))

# MACD 动能：默认要求连续 2 日柱值递增（MACD_MOMENTUM_DAYS=2）
checks.append(("_macd_momentum_ok 连续改善放行",
               m._macd_momentum_ok(pd.DataFrame({"macd_histogram": [-0.9, -0.5, -0.1]}), cfg) is True))
checks.append(("_macd_momentum_ok 单日反抽后转弱否决",
               m._macd_momentum_ok(pd.DataFrame({"macd_histogram": [-0.5, 0.35, 0.30]}), cfg) is False))

# MACD 动能恶化：反弹后再次大跌，MACD 柱当日走低
_md = np.concatenate([np.linspace(20.0, 15.0, 110), [15.6, 15.6, 15.3]])
out_md = m.compute_daily_signals(make_df(_md), cfg)
checks.append(("_macd_momentum_ok 恶化否决", m._macd_momentum_ok(out_md, cfg) is False))

# KDJ 动能：K>D 且 K≤KDJ_K_MAX，并要求 K 较昨日上行
checks.append(("_kdj_ok K上行放行",
               m._kdj_ok(pd.DataFrame({"kdj_k": [30.0, 32.0, 34.0], "kdj_d": [28.0, 28.5, 29.0]}), cfg) is True))
checks.append(("_kdj_ok K掉头否决",
               m._kdj_ok(pd.DataFrame({"kdj_k": [30.0, 32.0, 31.0], "kdj_d": [28.0, 28.5, 29.0]}), cfg) is False))
checks.append(("_kdj_ok K<D否决",
               m._kdj_ok(pd.DataFrame({"kdj_k": [30.0, 28.0], "kdj_d": [31.0, 29.0]}), cfg) is False))

# KDJ 高位：持续上涨后在高位运行（K>55）
_kd = np.concatenate([np.linspace(10.0, 20.0, 118), [19.9, 19.8]])
out_kd = m.compute_daily_signals(make_df(_kd), cfg)
checks.append(("_kdj_ok 高位否决", m._kdj_ok(out_kd, cfg) is False))

# 财报季度候选：按披露截止日回溯（原实现按"当前季度"取数，2-4 月/7 月必然取空）
checks.append(("_quarter_candidates 4月→上年Q4起",
               m._quarter_candidates(datetime(2026, 4, 20), 3) == [(2025, 4), (2025, 3), (2025, 2)]))
checks.append(("_quarter_candidates 5月→当年Q1起",
               m._quarter_candidates(datetime(2026, 5, 6), 3) == [(2026, 1), (2025, 4), (2025, 3)]))
checks.append(("_quarter_candidates 7月→当年Q1起（原实现会取空）",
               m._quarter_candidates(datetime(2026, 7, 15), 3)[0] == (2026, 1)))
checks.append(("_quarter_candidates 9月→当年Q2起",
               m._quarter_candidates(datetime(2026, 9, 11), 3)[0] == (2026, 2)))
checks.append(("_quarter_candidates 11月→当年Q3起",
               m._quarter_candidates(datetime(2026, 11, 3), 3)[0] == (2026, 3)))

# 周线趋势确认
_w_up = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=30, freq="W").strftime("%Y-%m-%d"),
                      "close": np.linspace(10.0, 15.0, 30)})
_w_down = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=30, freq="W").strftime("%Y-%m-%d"),
                        "close": np.linspace(10.0, 6.0, 30)})
checks.append(("周线站上MA10放行", m.check_weekly_trend(_w_up, cfg) is True))
checks.append(("周线主跌否决", m.check_weekly_trend(_w_down, cfg) is False))
checks.append(("周线数据不足放行", m.check_weekly_trend(_w_up.head(5), cfg) is True))
checks.append(("周线None放行", m.check_weekly_trend(None, cfg) is True))

# ===========================================================================
# TOP5 荐股质量改进验证
# ===========================================================================
_FUND_OK = {"roe": 12.0, "debt_ratio": 45.0, "goodwill_ratio": 3.0, "deducted_profit_ratio": 0.9}
_NEUTRAL = {"regime": "neutral"}
_A_DATE = str(df_a["date"].iloc[-1])

# --- 1) 数据缺失分层：财务缺失不等于通过（技术面达标 → 待核验候选，不占正式推荐） ---
_sig_nofund, _r_nofund = m.evaluate(df_a, code="600000", name="测试", config=cfg,
                                    market_env=_NEUTRAL, fund_data=None)
checks.append(("分层: 无财务数据仍技术达标(PASS)", _r_nofund == "PASS"))
checks.append(("分层: 无财务数据 tier=pending", getattr(_sig_nofund, "tier", "") == "pending"))
checks.append(("分层: 无财务数据 fund_status=missing", getattr(_sig_nofund, "fund_status", "") == "missing"))
checks.append(("分层: missing_tags 含 fund", "fund" in str(getattr(_sig_nofund, "missing_tags", ""))))

_sig_ok, _r_ok = m.evaluate(df_a, code="600000", name="测试", config=cfg,
                            market_env=_NEUTRAL, fund_data=_FUND_OK)
checks.append(("分层: 核心财务齐备 tier=formal", _r_ok == "PASS" and getattr(_sig_ok, "tier", "") == "formal"))
# 主源 Baostock 的典型返回：只有 ROE+负债率，商誉/扣非缺失 → 仍为 formal，但标注缺项
_sig_core, _r_core = m.evaluate(df_a, code="600000", name="测试", config=cfg,
                                market_env=_NEUTRAL, fund_data={"roe": 12.0, "debt_ratio": 45.0})
checks.append(("分层: core齐备但可选项缺失仅记标签",
               _r_core == "PASS" and getattr(_sig_core, "tier", "") == "formal"
               and "fund_goodwill" in str(getattr(_sig_core, "missing_tags", ""))
               and "fund_deducted" in str(getattr(_sig_core, "missing_tags", ""))))

_sig_partial, _r_partial = m.evaluate(df_a, code="600000", name="测试", config=cfg,
                                      market_env=_NEUTRAL, fund_data={"roe": 12.0})
checks.append(("分层: 核心项缺一 tier=pending",
               _r_partial == "PASS" and getattr(_sig_partial, "tier", "") == "pending"))

# --- 2) 数据时效：最新K线须与市场最新交易日一致，停牌/滞后 → 暂不推荐 ---
_, _r_fresh = m.evaluate(df_a, code="600000", name="测试", config=cfg,
                         market_env=_NEUTRAL, fund_data=None, latest_trade_date=_A_DATE)
checks.append(("时效: 日期一致放行", _r_fresh == "PASS"))
_, _r_stale = m.evaluate(df_a, code="600000", name="测试", config=cfg,
                         market_env=_NEUTRAL, fund_data=None, latest_trade_date="2099-12-31")
checks.append(("时效: 停牌/滞后 FAIL_STALE", _r_stale == "FAIL_STALE"))

# --- 3) 底背离修正：指标比较锚定在「前一个价格低点」当根，而非指标自身最小值 ---
def _div_frame(closes, rsis, confirm_at_end=True):
    n = len(closes)
    confirm = np.zeros(n, dtype=bool)
    if confirm_at_end:
        confirm[-1] = True  # MA5拐头确认（合成）
    return pd.DataFrame({
        "close": np.asarray(closes, dtype=float),
        "rsi14": np.asarray(rsis, dtype=float),
        "macd_diff": np.zeros(n, dtype=float),
        "macd_golden_cross": np.zeros(n, dtype=bool),
        "rsi_rebound": np.zeros(n, dtype=bool),
        "ma5_turn": confirm,
    })

# 真背离：低点 10(RSI 20) → 反弹 → 新低 9.8(RSI 26.5)，两低点间隔足够
_closes_t = [12, 12, 12, 12, 12, 10, 10, 10, 10.5, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11, 11,
             10.8, 10.5, 10.3, 9.95, 9.8, 9.8]
_rsis_t = [50, 50, 50, 50, 50, 20, 20, 20, 30, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45, 45,
           40, 36, 33, 30, 26.5, 26.5]
_out_t = None  # 底背离单测直接调用纯函数 _divergence_confirmed，不需要完整信号帧
_div_true = m._divergence_confirmed(_div_frame(_closes_t, _rsis_t), cfg)
checks.append(("底背离: 双低点价格降低+指标抬高 → 认定", bool(_div_true.iloc[-1]) is True))

# 假背离（旧口径会误报）：价格低点处 RSI=30，窗口中段某日 RSI 低至 15（并非价格低点），
# 今日新低 RSI=26 > 15+2（旧逻辑误报），但 26 < 30（新逻辑不认定）
_closes_f = [12, 12, 12, 12, 12, 10, 10, 10, 10.4, 11, 11, 10.2, 11, 11, 11, 11, 11, 11, 10.8, 10.5,
             10.3, 9.95, 9.8, 9.8, 9.8, 9.8]
_rsis_f = [50, 50, 50, 50, 50, 30, 30, 30, 28, 45, 45, 15, 45, 45, 45, 45, 45, 45, 40, 36,
           34, 32, 30, 28, 27, 26]
_div_false = m._divergence_confirmed(_div_frame(_closes_f, _rsis_f), cfg)
checks.append(("底背离: 指标最低点不在价格低点 → 不认定（修正误报）", bool(_div_false.iloc[-1]) is False))

# --- 4) 量价质量分：收高位阳线满分；冲高回落降档 ---
_q_a = m.compute_daily_signals(df_a, cfg).iloc[-1]
checks.append(("量价: 放量阳线收高位=满分25", float(_q_a["vol_price_quality"]) == cfg.W_DAILY_VOL_PRICE))
checks.append(("量价: 标签=放量企稳", str(_q_a["vol_price_label"]) == "放量企稳"))

_closes_v = base_closes()
_closes_v[-1] = _closes_v[-2] * 1.01      # 收盘仍略高于昨收（满足基础条件）
_prev = _closes_v[-2]
_df_v = make_df(_closes_v, last_vol_mult=3.0, last_open=_prev * 1.03,
                last_high=_prev * 1.06, last_low=_prev * 0.995)
_q_v = m.compute_daily_signals(_df_v, cfg).iloc[-1]
checks.append(("量价: 冲高回落降档=10分", float(_q_v["vol_price_quality"]) == cfg.W_DAILY_VOL_PRICE_WEAK))
checks.append(("量价: 标签=放量冲高回落", str(_q_v["vol_price_label"]) == "放量冲高回落"))

# --- 5) 评级统一：等级=原始技术分定级（分数与等级同源），熊市只抬门槛不扣展示分 ---
_sig_neu, _r_neu = m.evaluate(df_a, code="600000", name="测试", config=cfg,
                              market_env=_NEUTRAL, fund_data=_FUND_OK)
_sig_bear, _r_bear = m.evaluate(df_a, code="600000", name="测试", config=cfg,
                                market_env={"regime": "bear"}, fund_data=_FUND_OK)
checks.append(("评级: 中性通过", _r_neu == "PASS"))
checks.append(("评级: 熊市高分仍通过", _r_bear == "PASS"))
checks.append(("评级: 熊市等级与展示分数同源",
               _r_bear == "PASS" and _sig_bear.grade == m._grade_from_score(_sig_bear.score, cfg)))

# 恰好 60 分：中性达 B 级门槛放行、熊市门槛 +10=70 被否决（体现"门槛上浮"替代"扣分定级"）
_cfg60 = replace(m.StrategyConfig(), W_DAILY_TREND_TURN=60.0, W_DAILY_RSI_REBOUND=0.0,
                 DAILY_MULTI_RESONANCE_BONUS=0.0, DAILY_VOL_EXPAND=999.0)
_sig60, _r60n = m.evaluate(df_a, code="600000", name="测试", config=_cfg60,
                           market_env=_NEUTRAL, fund_data=_FUND_OK)
_, _r60b = m.evaluate(df_a, code="600000", name="测试", config=_cfg60,
                      market_env={"regime": "bear"}, fund_data=_FUND_OK)
checks.append(("评级: 60分中性达B门槛放行", _r60n == "PASS" and _sig60.score == 60.0 and _sig60.grade == "B"))
checks.append(("评级: 熊市60分<70被否决", _r60b == "FAIL_TECH"))

# --- 6) 排序可复现：同分/同背离/同盈亏比时按股票代码升序（末级键） ---
_rank_rows = pd.DataFrame([
    {"code": "600001", "score": 65.0, "has_divergence": False, "rr_ratio": 1.6},
    {"code": "000002", "score": 65.0, "has_divergence": False, "rr_ratio": 1.6},
    {"code": "000003", "score": 70.0, "has_divergence": False, "rr_ratio": 1.5},
])
_ranked = m._rank_signals(_rank_rows)
checks.append(("排序: 高分在前", _ranked.iloc[0]["code"] == "000003"))
checks.append(("排序: 全同级按代码升序（可复现）", list(_ranked["code"])[1:] == ["000002", "600001"]))

# --- 7) 周线数据充分性 / 时效（数据缺失 → 不可确认 → 待核验候选） ---
checks.append(("周线: None数据不足→不可确认", m._weekly_trend_data_ok(None, cfg) is False))
checks.append(("周线: 长度不足→不可确认", m._weekly_trend_data_ok(_w_up.head(5), cfg) is False))
checks.append(("周线: 数据充分→可确认", m._weekly_trend_data_ok(_w_up, cfg) is True))
checks.append(("周线MACD: None→不可确认", m._weekly_macd_data_ok(None, cfg) is False))
checks.append(("周线MACD: 30根不足→不可确认", m._weekly_macd_data_ok(_w_up, cfg) is False))
_w_long = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=40, freq="W").strftime("%Y-%m-%d"),
                        "close": np.linspace(10.0, 15.0, 40)})
checks.append(("周线MACD: 充分(≥26+9根)→可确认", m._weekly_macd_data_ok(_w_long, cfg) is True))
checks.append(("周线时效: 覆盖当周→新鲜", m._weekly_fresh_enough(_w_up, "2025-01-26") is True))
checks.append(("周线时效: 截止过旧→不新鲜", m._weekly_fresh_enough(_w_up, "2026-09-11") is False))

# --- 8) 三维展示（describe / describe_pending） ---
_desc = m.describe(_sig_ok.to_dict())
checks.append(("describe: 含基础评分等级", "评分:" in _desc and "级" in _desc))
checks.append(("describe: 含入选依据", "入选依据" in _desc))
checks.append(("describe: 含核验状态", "核验: 财务已核验" in _desc))
_desc_p = m.describe_pending({"name": "测试", "code": "600000", "score": 65.0, "grade": "B",
                              "fund_status": "missing", "weekly_status": "unverified",
                              "missing_tags": "fund"})
checks.append(("describe_pending: 说明财务/周线待核验", "财务" in _desc_p and "周线" in _desc_p))

for label, ok in checks:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
results.extend([ok for _, ok in checks])

print(f"\n{sum(results)}/{len(results)} 通过")
sys.exit(0 if all(results) else 1)
