"""
策略层：在最新一根周K线上判定「下跌趋势中的恐慌盘（放量放换手大跌但守住前低）」。

evaluate(df) 为纯函数，便于单测。输入为 data.get_weekly 返回的英文列 DataFrame。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd

import config

# 输出展示列 -> 中文表头（供 notify 使用）
DISPLAY_COLS = [
    ("code", "代码"),
    ("name", "名称"),
    ("date", "周日期"),
    ("close", "收盘"),
    ("pct_chg", "周涨跌%"),
    ("turnover", "换手%"),
    ("turnover_ratio", "换手比"),
    ("vol_ratio", "量比"),
    ("prior_low", "前低"),
    ("week_low", "本周低"),
    ("amount", "成交额"),
]

TITLE = "周线恐慌盘选股结果"
PREFIX = "pick"
SORT_BY = ["pct_chg", "vol_ratio"]
SORT_ASC = [True, False]


@dataclass
class Signal:
    code: str
    name: str
    date: str          # 本周日期
    close: float
    pct_chg: float     # 本周涨跌幅 %
    turnover: float    # 本周换手率 %
    vol_ratio: float   # 本周量 / 前N周均量
    turnover_ratio: float  # 本周换手 / 前N周均换手
    ma: float          # TREND_MA 周均线
    prior_low: float   # 前 M 周最低价
    week_low: float    # 本周最低价
    amount: float      # 本周成交额

    def to_dict(self) -> dict:
        return asdict(self)


def describe(r: dict) -> str:
    """把一条信号(Signal.to_dict())格式化成逐条分析报告(飞书 lark_md)。"""
    amount_yi = r["amount"] / 1e8
    above_low = (r["week_low"] / r["prior_low"] - 1) * 100 if r["prior_low"] else 0.0
    return "\n".join([
        f"**{r['name']} {r['code']}**  {r['date']}  收盘 {r['close']}",
        f"周涨跌 **{r['pct_chg']}%** ｜ 换手 {r['turnover']}%({r['turnover_ratio']}x) "
        f"｜ 量比 {r['vol_ratio']} ｜ 成交额 {amount_yi:.2f}亿",
        f"- 下跌趋势: 收盘 {r['close']} < 20周线 {r['ma']}",
        f"- 放量: 量比 {r['vol_ratio']} ≥ {config.VOL_RATIO}",
        f"- 放换手: 换手比 {r['turnover_ratio']} ≥ {config.TURNOVER_RATIO}",
        f"- 恐慌杀跌: {r['pct_chg']}% ≤ {config.PANIC_PCT}%",
        f"- 不破前低: 本周低 {r['week_low']} ≥ 前低 {r['prior_low']}（高于前低 {above_low:.1f}%）",
    ])


_NEED_COLS = {"close", "high", "low", "volume", "pct_chg", "turnover", "amount", "date"}


def compute_signals(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """向量化计算每一周是否满足策略条件（回测与实盘共用同一套逻辑）。

    返回在原 df 基础上追加以下列的副本：
      ma, ma_prev, vol_base, vol_ratio, to_base, turnover_ratio, prior_low, signal
    其中 signal 为布尔列，表示「该周」是否触发信号。
    数据不足或缺列返回 None。

    条件（作用在「当周」）：
      1. 下跌趋势: close < MA(TREND_MA) 且 MA 向下 (MA < MA[-TREND_SLOPE_LOOKBACK])
      2. 成交量放大: 本周量 >= VOL_RATIO × 前 VOL_LOOKBACK 周均量 (不含本周)
      3. 换手率放大: 本周换手 >= TURNOVER_RATIO × 前 TURNOVER_LOOKBACK 周均换手 (不含本周)
      4. 恐慌杀跌: 本周涨跌幅 <= PANIC_PCT (默认 -5, 即跌幅 >= 5%)
      5. 不破前低: 本周最低 >= 前 PRIOR_LOW_LOOKBACK 周最低 (不含本周)
      过滤: 本周成交额 >= MIN_AMOUNT, 且位置 >= MIN_WEEKS (剔除次新)
    """
    if df is None or df.empty:
        return None
    if not _NEED_COLS.issubset(df.columns):
        return None
    if len(df) < config.MIN_WEEKS:
        return None

    out = df.copy().reset_index(drop=True)

    # 1) 下跌趋势
    out["ma"] = out["close"].rolling(config.TREND_MA).mean()
    out["ma_prev"] = out["ma"].shift(config.TREND_SLOPE_LOOKBACK)
    trend_ok = (out["close"] < out["ma"]) & (out["ma"] < out["ma_prev"])

    # 2) 成交量放大 (前 N 周均量, 不含本周 => shift(1) 后 rolling)
    out["vol_base"] = out["volume"].shift(1).rolling(config.VOL_LOOKBACK).mean()
    out["vol_ratio"] = out["volume"] / out["vol_base"]
    vol_ok = (out["vol_base"] > 0) & (out["vol_ratio"] >= config.VOL_RATIO)

    # 3) 换手率放大 (前 N 周均换手, 不含本周)
    out["to_base"] = out["turnover"].shift(1).rolling(config.TURNOVER_LOOKBACK).mean()
    out["turnover_ratio"] = out["turnover"] / out["to_base"]
    to_ok = (out["to_base"] > 0) & (out["turnover_ratio"] >= config.TURNOVER_RATIO)

    # 4) 恐慌杀跌 (默认 -5, 即跌幅 >= 5%)
    panic_ok = out["pct_chg"] <= config.PANIC_PCT

    # 5) 不破前低 (前 M 周最低, 不含本周)
    out["prior_low"] = out["low"].shift(1).rolling(config.PRIOR_LOW_LOOKBACK).min()
    not_break = out["low"] >= out["prior_low"]

    # 过滤
    amount_ok = out["amount"] >= config.MIN_AMOUNT
    not_newly = pd.Series(out.index >= config.MIN_WEEKS, index=out.index)

    out["signal"] = (
        trend_ok & vol_ok & to_ok & panic_ok & not_break & amount_ok & not_newly
    ).fillna(False)

    return out


def evaluate(df: pd.DataFrame, code: str = "", name: str = "") -> Optional[Signal]:
    """满足全部条件返回最新一周的 Signal，否则返回 None。实盘选股入口。"""
    out = compute_signals(df)
    if out is None or out.empty:
        return None

    cur = out.iloc[-1]
    if not bool(cur["signal"]):
        return None

    return Signal(
        code=code,
        name=name,
        date=pd.to_datetime(cur["date"]).strftime("%Y-%m-%d"),
        close=round(float(cur["close"]), 2),
        pct_chg=round(float(cur["pct_chg"]), 2),
        turnover=round(float(cur["turnover"]), 2),
        vol_ratio=round(float(cur["vol_ratio"]), 2),
        turnover_ratio=round(float(cur["turnover_ratio"]), 2),
        ma=round(float(cur["ma"]), 2),
        prior_low=round(float(cur["prior_low"]), 2),
        week_low=round(float(cur["low"]), 2),
        amount=float(round(float(cur["amount"]), 0)),
    )
