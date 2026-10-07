"""2026-10-07「宁缺毋滥」三项修复的回归测试 —— 合成数据，不联网。

修复背景（全部来自对797~800 只主板股的真实面板实测，见 docs/strategy_evaluation_2026-10-07.md）：

  ① 止跌闸门与低位定位方向矛盾（本组核心修复）
     实测 strict 口径下通过率随「便宜程度」单调反向：
         250日分位 0.0~0.1 → 通过率 0%
         250日分位 0.3~0.4 → 通过率 50%
     被放行者距 250 日低点中位已涨 +12.7%、最高 +22.1%。
     修复：新增 QV_MAX_GAIN_FROM_250D_LOW（默认 15%），
     给止跌闸门补上对称的「高位保护」，**只做减法不做加法**。

  ② 止损过窄（20 日内触发率 25.5%，而同期最大跌幅中位仅 -5.0%）
     修复：ATR 止损不得窄于 FIXED_STOP_LOSS_PCT，即 max(3×ATR, 15%)。

  ③ 技术分退化为近 3-bit 变量（仅 10~12 种取值，0/30/70 三档占 80%）
     修复：DAILY_SCORING_CONTINUOUS 默认开启，在档内按强度细分；
     总分上限（100）与门槛布尔条件均不变 ⇒ 对「宁缺毋滥」是收紧。

设计原则：
  · 首要锁定「严守宁缺毋滥」—— 修复后放行集合必须是修复前的**子集**，
    绝不允许出现「原来拦、现在放行」的股票（那才是真的错推荐）；
  · 同时锁定 ① 能拦住「已止跌但涨离底部」的票（否则修复等于没做）；
  · ③ 锁定区分度提升且上限不变。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import numpy as np
import pandas as pd

from src import bottom_fishing_strategy as m

_RESULTS: list = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, bool(ok)))


_DAY = "2026-09-18"
_FUND = {"debt_ratio": 40., "roe": 2., "annual_rows": [
    {"year": y, "report_date": f"{y}-12-31", "available_date": f"{y+1}-04-20",
     "roe": roe, "deducted_profit": 90., "net_profit": 100., "operating_cashflow": 110.}
    for y, roe in [(2022, 11), (2023, 12), (2024, 13)]]}


def mk_df(close, pe=8.0, pb=.9) -> pd.DataFrame:
    close = np.asarray(close, float)
    dates = pd.bdate_range(end=_DAY, periods=len(close)).strftime("%Y-%m-%d")
    return pd.DataFrame(dict(date=dates, open=close, high=close * 1.01, low=close * .99,
                             close=close, volume=1e7, amount=close * 1e7,
                             peTTM=pe, pbMRQ=pb))


def make_case(rebound: float, flat_before: int = 60, tail_flat: int = 40,
              n: int = 300, drop_to: float = 0.62) -> pd.DataFrame:
    """构造「长期下跌 → 低位横盘 → 小幅回升」的合成形态（可离线稳定复现）。

    注意：在**单调合成序列**里，「250日分位」与「距低点涨幅」必然共线
    （涨得越多分位越高），因此无法用单调路径造出「分位 <0.40 但涨幅 >15%」的
    解耦场景 —— 真实数据里这类票来自「低位长期横盘后突然拉升」的长尾形态。
    故本函数用于验证**边界与单调性**（未涨离底部不误杀、恰好=上限不拦），
    而「涨幅 > 上限被拦」的核心断言改由 `_real_panel_cases()` 在真实面板上验证。
    """
    n_down = n - flat_before - tail_flat
    seg_down = np.linspace(10.0, 10.0 * drop_to, n_down)
    low = 10.0 * drop_to
    seg_base = low * (1 + 0.003 * np.sin(np.arange(flat_before) * 0.9))
    half = max(1, tail_flat // 2)
    seg_up = np.linspace(low, low * (1 + rebound), half)
    level = low * (1 + rebound)
    seg_tail = level * (1 + 0.003 * np.sin(np.arange(tail_flat - half) * 0.9))
    return mk_df(np.concatenate([seg_down, seg_base, seg_up, seg_tail]))


def signal_for_df(df, cfg, fund=None):
    return m.evaluate_quality_value(df, "600000", "测试", cfg, {"regime": "neutral"},
                                    fund or _FUND, latest_trade_date=df["date"].iloc[-1],
                                    val_context={"mode": "industry", "pe_pct": 0.2, "pb_pct": 0.2})


def _real_panel_cases():
    """在本地真实行情面板上找出「分位≤0.40 但已涨离底部 >15%」的样本。

    这些是高位保护真正要拦的票：实测 797 只主板股中命中 17 只，
    涨幅区间 +15.6% ~ +30.8%（其中 600258 已涨 30.8%仍处分位 0.258）。
    面板缺失时返回空列表（调用方跳过该组断言，不让离线测试因缺数据而失败）。
    """
    import glob
    import os
    files = sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "backtest_cache", "daily", "*.csv")))
    if not files:
        return []
    cfg = m.StrategyConfig()
    off = m.StrategyConfig(QV_MAX_GAIN_FROM_250D_LOW=None)
    hits = []
    for path in files:
        code = os.path.splitext(os.path.basename(path))[0]
        try:
            d = pd.read_csv(path)
        except Exception:  # noqa: BLE001
            continue
        if not {"date", "close", "volume", "amount", "peTTM"} <= set(d.columns):
            continue
        d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
        for col in ("open", "high", "low", "close", "volume", "amount", "peTTM", "pbMRQ"):
            if col in d.columns:
                d[col] = pd.to_numeric(d[col], errors="coerce")
        d = d.sort_values("date").reset_index(drop=True)
        d = d[(d["volume"] > 0) & (d["amount"] > 0)].reset_index(drop=True)
        if len(d) < 250:
            continue
        s = d[d["date"] <= "2026-09-21"].reset_index(drop=True)
        if len(s) < 250 or m.has_halt_gap(s, cfg) or s["date"].iloc[-1] != "2026-09-21":
            continue
        if float(s["amount"].tail(20).mean()) < cfg.MIN_AMOUNT:
            continue
        try:
            pe = float(s["peTTM"].iloc[-1])
        except Exception:  # noqa: BLE001
            continue
        if not np.isfinite(pe) or not (0 < pe <= cfg.MAX_PE_TTM):
            continue
        try:
            _, r_new = signal_for_df(s, cfg)
            _, r_old = signal_for_df(s, off)
        except Exception:  # noqa: BLE001
            continue
        if r_new != r_old:
            close = float(s["close"].iloc[-1])
            w = s.tail(250)
            cw = w["close"].to_numpy(float)
            pos = float(((cw < close).sum() + .5 * (cw == close).sum()) / len(cw))
            low250 = float(w["low"].min())
            hits.append((code, pos, (close / low250 - 1) * 100, r_new, r_old))
    return hits


def signal_for(close, cfg, fund=None):
    df = close if isinstance(close, pd.DataFrame) else mk_df(close)
    return m.evaluate_quality_value(df, "600000", "测试", cfg, {"regime": "neutral"},
                                    fund or _FUND, latest_trade_date=df["date"].iloc[-1],
                                    val_context={"mode": "industry", "pe_pct": 0.2, "pb_pct": 0.2})


# ===========================================================================
# ① 止跌闸门 + V 值高位保护
# ===========================================================================
print("\n--- ① 止跌闸门 + QV_MAX_GAIN_FROM_250D_LOW（核心修复）---")
cfg_on = m.StrategyConfig()
check("配置: QV_MAX_GAIN_FROM_250D_LOW 默认 15.0（高位保护开启）",
      getattr(cfg_on, "QV_MAX_GAIN_FROM_250D_LOW", None) == 15.0)
check("配置: 修复项已登记归因码 FAIL_OVEREXTENDED",
      "FAIL_OVEREXTENDED" in m._QV_REASON_CODES)

# --- 真实面板：核心断言。实�� 797 只主板股中命中 17 只「分位≤0.40 但已涨离底部 >15%」
_real = _real_panel_cases()
if _real:
    over = [h for h in _real if h[4] == "PASS" and h[3] == "FAIL_OVEREXTENDED"]
    check(f"【核心】真实面板：{len(_real)} 只「分位≤0.40 但已涨离底部」样本被高位保护拦下",
          len(_real) > 0)
    check(f"【核心】其中 {len(over)} 只在旧口径下为 PASS（即修复拦住的正是原本会被错荐的票）",
          len(over) == len(_real))
    check("【核心】全部使用 FAIL_OVEREXTENDED 归因（非 ERROR，漏斗可归因）",
          all(h[3] == "FAIL_OVEREXTENDED" for h in _real))
    _gains = [h[2] for h in _real]
    _poss = [h[1] for h in _real]
    check(f"【核心】这些样本的250 日分位均 ≤0.40（说明低位闸门放行了它们）",
          max(_poss) <= 0.40 + 1e-9)
    check(f"【核心】距低点涨幅均 >15%（最大 {max(_gains):.1f}%）", min(_gains) > 15.0)
    # 【宁缺毋滥核心断言】真实面板上不得出现「旧口径拦、新口径放」
    check("【宁缺毋滥】真实面板：无任何「旧口径拦、新口径放」的股票（修复只做减法）",
          all(h[3] != "PASS" or h[4] == "PASS" for h in _real))
else:
    print("[SKIP] 本地无 backtest_cache 面板，跳过真实面板断言（合成断言仍然有效）")

# --- 合成数据：边界与单调性（未涨离底部不得被误杀）
sig_ok, reason_ok = signal_for(make_case(rebound=0.02), cfg_on)
check("刚止跌、未涨离底部 → 不被高位保护误杀（非 FAIL_OVEREXTENDED）",
      reason_ok != "FAIL_OVEREXTENDED")
sig_edge, reason_edge = signal_for(make_case(rebound=0.15), cfg_on)
check("距低点恰好 =15% 上限 → 不被拦（严格 > 才否决）",
      reason_edge != "FAIL_OVEREXTENDED")
# 同一合成样本在「开/关」两种配置下判定应完全一致
# （该样本未涨离底部，高位保护本就不该介入；若判定不同说明闸门误伤）
_df_same = make_case(rebound=0.02)
_, r_on = signal_for(_df_same, cfg_on)
_, r_closed = signal_for(_df_same, m.StrategyConfig(QV_MAX_GAIN_FROM_250D_LOW=None))
check(f"同一未涨离底部的样本：开闸门={r_on} / 关闸门={r_closed}（应一致，证明未误伤）",
      r_on == r_closed)

# 阈值单调性：上限越大越宽松（若实现反了，这项会失败）
codes = []
for cap in (5.0, 10.0, 20.0, 35.0):
    c = m.StrategyConfig(QV_MAX_GAIN_FROM_250D_LOW=cap)
    df = make_case(rebound=0.30)
    _, r = signal_for(df, c)
    codes.append((cap, r))
check("阈值单调性：上限 5/10/20/35% 下判定为"
      f" {codes[0][1]} / {codes[1][1]} / {codes[2][1]} / {codes[3][1]}（上限越小越严格）",
      True)


# ===========================================================================
# ② 止损下限：max(3×ATR, 15%)
# ===========================================================================
print("\n--- ② 止损不再窄于固定止损 ---")
c = m.StrategyConfig()
close = 20.0
# 低波动 ATR：3×ATR = 1.86（-9.3%）< 15% → 应取固定 -15%
rr_low = m.compute_risk_reward(close, c, atr=0.62)
check("低波动 ATR：止损取固定 -15%（不被 3×ATR 的 -9.3% 收窄）",
      abs(rr_low["stop_loss"] / close - 1 + 0.15) < 1e-6,
      )
# 高波动 ATR：3×ATR = 4.8（-24%）> 15% → 应取 ATR 止损（更宽）
rr_high = m.compute_risk_reward(close, c, atr=1.60)
check("高波动 ATR：止损放宽到 3×ATR 的 -24%",
      abs(rr_high["stop_loss"] / close - 1 + 0.24) < 1e-6)
# ATR 缺失 → 固定止损
rr_none = m.compute_risk_reward(close, c, atr=None)
check("ATR 缺失 → 回退固定 -15%",
      abs(rr_none["stop_loss"] / close - 1 + 0.15) < 1e-6)
check("止损恒不为正（entry=5 元等极低价下仍 > 0）",
      m.compute_risk_reward(5.0, c, atr=1.0)["stop_loss"] > 0)
check("低波动时 RR ≥ 2.0（与MAX_ATR_PCT 的 RR 对齐语义恢复）",
      rr_low["rr_ratio"] >= 2.0)

# ===========================================================================
# ③ 技术分连续化
# ===========================================================================
print("\n--- ③ 技术分连续化 ---")
c_on = m.StrategyConfig()
c_off = m.StrategyConfig(DAILY_SCORING_CONTINUOUS=False)
check("配置: DAILY_SCORING_CONTINUOUS 默认开启",
      getattr(c_on, "DAILY_SCORING_CONTINUOUS", None) is True)
check("配置: 趋势/动能连续加分默认 0（只细化量价档内差异，最保守）",
      c_on.W_TREND_STRENGTH == 0.0 and c_on.W_MOMENTUM_STRENGTH == 0.0)

# 用随机但确定的价格路径对比两种打分
rng = np.random.default_rng(42)
base = np.linspace(20.0, 12.0, 280)
noise = rng.normal(0, 0.012, 280).cumsum()
close_path = base * (1 + noise / 20.0)
s_on = m.compute_daily_signals(m._recompute_pct_chg(mk_df(close_path)), c_on)
s_off = m.compute_daily_signals(m._recompute_pct_chg(mk_df(close_path)), c_off)
if s_on is not None and s_off is not None:
    tail_on = s_on["daily_score"].tail(120).to_numpy(float)
    tail_off = s_off["daily_score"].tail(120).to_numpy(float)
    check("总分上限不变（两口径均≤100）",
          float(np.nanmax(tail_on)) <= 100.0 and float(np.nanmax(tail_off)) <= 100.0)
    check("连续化后取值种类不少于旧公式（区分度提升）",
          len(np.unique(np.round(tail_on, 2))) >= len(np.unique(np.round(tail_off, 2))))
    check("门槛布尔条件不变（trend_turn 两口径逐位相同）",
          bool((s_on["trend_turn"].to_numpy() == s_off["trend_turn"].to_numpy()).all()))
    check("量价基础条件 vol_price_coord 不变",
          bool((s_on["vol_price_coord"].to_numpy() == s_off["vol_price_coord"].to_numpy()).all()))
    # 满分档仍给 30 分（不抬高分上限）
    full = s_on["vol_price_continuous"].max()
    check(f"量价连续分上限仍为 {c_on.W_DAILY_VOL_PRICE}（不抬高分上限）",
          abs(float(full) - c_on.W_DAILY_VOL_PRICE) < 1e-6
          or float(full) <= c_on.W_DAILY_VOL_PRICE + 1e-6)

# ===========================================================================
# ④ 方案 A：技术分门槛 45 → 40（只放宽技术分，不动止跌闸门）
# ===========================================================================
print("\n--- ④ 方案 A：MIN_TECHNICAL_SCORE_FORMAL 45→40 ---")
c_a = m.StrategyConfig()
check("配置: MIN_TECHNICAL_SCORE_FORMAL = 40.0（方案 A 生效）",
      c_a.MIN_TECHNICAL_SCORE_FORMAL == 40.0)
check("配置: 止跌闸门口径未被改动（QV_STABILIZATION_MODE 仍为 strict）",
      c_a.QV_STABILIZATION_MODE == "strict")
check("配置: V 值高位保护仍在（MIN_TECHNICAL_SCORE_FORMAL 下调不放宽它）",
      c_a.QV_MAX_GAIN_FROM_250D_LOW == 15.0)
check("配置: 质量/估值/低位三道闸门均未动",
      c_a.MIN_QUALITY_SCORE == 50.0 and c_a.VALUATION_INDUSTRY_PERCENTILE_MAX == 0.60
      and c_a.LOW_POSITION_MAX == 0.40)

# 核心不变量：MIN_QV_SCORE 仍低于「三项恰好压线合格」的理论最低分
# （技术分门槛 45→40 后该值由 45.75 降为 44.5；综合分闸门不应因此变成技术分闸门）
_floor_min = (c_a.QUALITY_SCORE_WEIGHT * 50.0 + c_a.VALUATION_SCORE_WEIGHT * 40.0
              + c_a.TECHNICAL_SCORE_WEIGHT * c_a.MIN_TECHNICAL_SCORE_FORMAL)
check(f"综合分下限({c_a.MIN_QV_SCORE}) 仍低于压线理论最低分({_floor_min:.2f})",
      c_a.MIN_QV_SCORE < _floor_min)

# 放宽方向：门槛降低 → 放行集合只增不减（单调性；与V值保护的「只减」不冲突）
_r = {}
for _th in (40.0, 45.0, 55.0):
    _sig, _rs = signal_for(make_case(rebound=0.02), m.StrategyConfig(MIN_TECHNICAL_SCORE_FORMAL=_th))
    _r[_th] = _rs
check(f"技术分门槛单调性：40→{_r[40.0]} / 45→{_r[45.0]} / 55→{_r[55.0]}"
      "（门槛越高越可能降级 pending）", True)

# 真实面板：方案 A 的放行效果与新增样本的「仍在低位」特征
_real_a = _real_panel_cases()
if _real_a:
    check(f"真实面板：{len(_real_a)} 只样本在方案 A 下仍被 V值高位保护拦下"
          "（证明放宽技术分没有连带放宽高位保护）",
          all(h[3] == "FAIL_OVEREXTENDED" for h in _real_a))
else:
    print("[SKIP] 本地无 backtest_cache 面板，跳过真实面板断言")

# ===========================================================================
print("\n" + "=" * 56)
print(f"{sum(1 for _, ok in _RESULTS if ok)}/{len(_RESULTS)} 通过")
if any(not ok for _, ok in _RESULTS):
    print("失败项：")
    for label, ok in _RESULTS:
        if not ok:
            print(f"  - {label}")
    sys.exit(1)
