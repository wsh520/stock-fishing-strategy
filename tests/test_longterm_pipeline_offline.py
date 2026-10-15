#!/usr/bin/env python3
"""长期策略离线回归（2026-10-13）。

修复前的三个致命问题都会让本脚本立刻失败或输出 0：
  1. notify.feishu.send_stock_recommendations 不存在 → run_longterm 导入期 ImportError；
  2. LongTermQualityConfig 构造即 ValueError（RECOMMENDATION_MODE 非法 + max_age==window）；
  3. evaluate_quality_longterm 在预筛趟（fund=None）就执行年度财务闸门
     → 每只股票 FAIL_ROE_STABILITY_INSUFFICIENT → 长期策略恒 0 推荐。

全部离线：baostock / akshare / MySQL / 飞书 / requests 全部打桩，
不产生任何网络请求（桩若被误调用会直接抛错，不会静默走网络）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd


def _qualified_bars(n=340, start=60.0, seed=7):
    """构造一段**能通过长期策略全部门闸门**的日线（端到端诊断用）。

    形态：长期缓跌（60→20，250日分位落在低位区）→ 末段筑底 → 温和回升，
    收盘站上 MA20/MA60 且 MA20 走平，符合 strict 止跌口径的 strong 层。
    振幅刻意压低（ATR% 远低于 6% 上限），成交额足够（> 5000 万长期门槛）。
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-02", periods=n)
    t = np.arange(n)
    # 0~322：每日 -0.42% 缓跌；323~340：筑底后温和回升（+0.18%/日）
    # 底部刻意做在倒数 18 根 bar：既要满足 min_age≥3（已止跌），
    # 又要落在 max_age 窗口内且 gain_pct 未超 ATR 允许幅度（距底过久会被
    # V值高位保护判为 FAIL_OVEREXTENDED —— 那是真实闸门，不是 bug）。
    drift = np.where(t < 323, -0.0042, 0.0018)
    close = start * np.exp(np.cumsum(drift))
    # 叠加一个确定性的 U 形底部噪声，避免随机游走把底部抹平
    close = close * (1 + 0.012 * np.sin(t / 9.0))
    close = np.maximum(close, 1.0)
    volume = rng.integers(4_000_000, 9_000_000, n).astype(float)
    return pd.DataFrame({
        "date": idx.strftime("%Y-%m-%d"),
        "open": close * (1 + rng.normal(0, 0.002, n)),
        "high": close * 1.006,
        "low": close * 0.994,
        "close": close,
        "volume": volume,
        "amount": close * volume,
        "turn": rng.uniform(1.0, 4.0, n),
        "pctChg": rng.normal(0, 0.6, n),
        "peTTM": 14.0,
        "pbMRQ": 1.6,
    })


def _qualified_index(n=340, seed=11):
    """同步的沪深300：缓跌后企稳（与个股同向，保证相对强度为正）。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-02", periods=n)
    t = np.arange(n)
    drift = np.where(t < 323, -0.0018, 0.0016)
    close = 4000.0 * np.exp(np.cumsum(drift + rng.normal(0, 0.004, n)))
    return pd.DataFrame({"date": idx.strftime("%Y-%m-%d"), "close": close})


def _bars(n=320, start=20.0, seed=7):
    """构造一段确定性的日线：长期缓跌后企稳回升（供形态类单测使用）。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-02", periods=n)
    t = np.arange(n)
    # 前 70% 缓跌（年化约 -25%），后 30% 温和回升
    drift = np.where(t < n * 0.7, -0.0016, 0.0016)
    close = start * np.exp(np.cumsum(drift + rng.normal(0, 0.012, n)))
    close = np.maximum(close, 1.0)
    return pd.DataFrame({
        "date": idx.strftime("%Y-%m-%d"),
        "open": close * (1 + rng.normal(0, 0.004, n)),
        "high": close * 1.012,
        "low": close * 0.988,
        "close": close,
        "volume": rng.integers(2_000_000, 6_000_000, n).astype(float),
        "amount": close * rng.integers(2_000_000, 6_000_000, n).astype(float),
        "turn": rng.uniform(1.0, 5.0, n),
        "pctChg": rng.normal(0, 1.2, n),
        "peTTM": 14.0,
        "pbMRQ": 1.6,
    })


def _fundamentals():
    """5 年稳定优质年报（中位 ROE 约 13%、累计现金流/利润 > 1）。"""
    rows = []
    for i, roe in enumerate([12.4, 13.1, 12.8, 13.5, 13.0]):
        profit = 100.0 * (1.12 ** i)
        rows.append({
            "year": 2020 + i, "roe": roe,
            "net_profit": profit, "operating_cashflow": profit * 1.15,
            "deducted_profit": profit * 0.92,
            "report_date": f"{2020+i}-12-31",
            "available_date": f"{2021+i}-04-20",
            "availability_assumed": False,
        })
    return rows


class LongTermOfflineTest(unittest.TestCase):

    def setUp(self):
        import src.screening_engine as bfs
        self.bfs = bfs
        self.daily = _bars()
        self.index = _bars(n=320, start=4000.0, seed=11)[["date", "close"]].copy()
        self.fund = {"annual_rows": _fundamentals()}

    # ---------- 1) 构造与导入 ----------
    def test_config_constructs(self):
        """修复 #2：LongTermQualityConfig 必须能构造。"""
        from src.quality_longterm_strategy import LongTermQualityConfig
        cfg = LongTermQualityConfig()
        self.assertIn(cfg.RECOMMENDATION_MODE, {"quality_value", "technical"})
        # 基类硬校验：min_age <= max_age < window
        self.assertLess(cfg.QV_RECENT_LOW_MAX_AGE, cfg.QV_RECENT_LOW_WINDOW)
        self.assertAlmostEqual(
            cfg.QUALITY_SCORE_WEIGHT + cfg.VALUATION_SCORE_WEIGHT + cfg.TECHNICAL_SCORE_WEIGHT,
            1.0, places=6)

    def test_entry_script_imports(self):
        """修复 #1：run_longterm 必须能导入（notify 函数名已对齐）。"""
        import run_longterm
        from notify.feishu import notify_screening_result
        self.assertTrue(callable(notify_screening_result))
        self.assertTrue(callable(run_longterm.main))

    def test_config_subclass_constructs_other_config(self):
        """回归：子类配置 asdict 后仍能构造 VolumeBreakoutConfig / StrategyConfig。

        修复前 LongTermQualityConfig 多出 22 个字段，
        `VolumeBreakoutConfig(**asdict(config))` 必抛 TypeError，
        导致 evaluate_quality_value 对**每只**股票崩溃。
        """
        import dataclasses as dc
        from src.screening_engine import _config_kwargs
        from src.quality_longterm_strategy import LongTermQualityConfig
        from src.volume_breakout_strategy import VolumeBreakoutConfig
        cfg = LongTermQualityConfig()
        # 修复前 asdict 的 22 个子类专有字段会让这里抛 TypeError。
        vb = VolumeBreakoutConfig(**_config_kwargs(
            VolumeBreakoutConfig, cfg, RECOMMENDATION_MODE="technical"))
        self.assertEqual(vb.RECOMMENDATION_MODE, "technical")
        # 子类覆盖的口径必须被继承，而不是退回基类默认
        self.assertEqual(vb.MAX_PICKS, cfg.MAX_PICKS)

    # ---------- 2) 核心逻辑 ----------
    def test_earnings_consistency_not_always_insufficient(self):
        """修复 #3 的一半：5 条年报必须能算出 4 次同比，而不是恒 insufficient。"""
        from src.quality_longterm_strategy import evaluate_earnings_consistency
        rows = [{"net_profit": 100 * 1.1 ** i} for i in range(5)]
        res = evaluate_earnings_consistency(rows, 5, 3)
        self.assertEqual(res["status"], "verified")
        self.assertEqual(res["positive_growth_years"], 4)
        self.assertEqual(res["total_years"], 4)

    def test_prescreen_pass_does_not_require_fundamentals(self):
        """修复 #3 的另一半：预筛趟（fund_data=None）不得因年度财务缺失而全池否决。"""
        from src.quality_longterm_strategy import (
            LongTermQualityConfig, evaluate_quality_longterm)
        cfg = LongTermQualityConfig(USE_CACHE=False, REQUIRE_RECENT_OPERATING=False,
                                    REQUIRE_FINANCIAL_RISK=False, REQUIRE_FORWARD_CONFIRMATION=False)
        sig, reason = evaluate_quality_longterm(
            self.daily, "600000", "测试", cfg, market_env={"regime": "neutral"},
            fund_data=None, latest_trade_date=str(self.daily["date"].iloc[-1]))
        # 关键：不能是年度财务类否决码
        self.assertNotIn(reason, (
            "FAIL_ROE_STABILITY_INSUFFICIENT", "FAIL_EARNINGS_DATA_INSUFFICIENT",
            "FAIL_LONGTERM_CASH_INSUFFICIENT"),
            f"预筛趟被年度财务闸门否决：{reason}")
        if sig is None:
            self.assertTrue(reason.startswith("FAIL_"), f"非预期 reason: {reason}")

    def test_accelerating_decline_rejects(self):
        """新接线的加速下跌否决：MA20 近5日跌幅 > 5% 必须被拦。"""
        from src.quality_longterm_strategy import (
            LongTermQualityConfig, _detect_accelerating_decline)
        cfg = LongTermQualityConfig()
        n = 40
        idx = pd.bdate_range("2025-01-01", periods=n)
        # 加速下跌：价格以每日 -2% 衰减
        close = 100 * np.exp(np.cumsum(np.full(n, -0.02)))
        tech = pd.DataFrame({"date": idx.strftime("%Y-%m-%d"), "close": close,
                             "ma20": pd.Series(close).rolling(20, min_periods=1).mean()})
        res = _detect_accelerating_decline(tech, cfg)
        self.assertTrue(res["checked"])
        self.assertTrue(res["accelerating"], res)
        # 数据不足不误杀
        self.assertFalse(_detect_accelerating_decline(tech.head(3), cfg)["accelerating"])

    def test_volume_exhaustion_is_label_only(self):
        """缩量检测只产标签，不得改变准入结果。"""
        from src.quality_longterm_strategy import (
            LongTermQualityConfig, _detect_volume_exhaustion)
        cfg = LongTermQualityConfig()
        n = 40
        vol = np.concatenate([np.full(35, 5_000_000.0), np.full(5, 1_000_000.0)])
        tech = pd.DataFrame({"date": pd.bdate_range("2025-01-01", periods=n).strftime("%Y-%m-%d"),
                             "volume": vol})
        res = _detect_volume_exhaustion(tech, cfg)
        self.assertTrue(res["exhausted"], res)

    # ---------- 3) 口径收紧（宁缺毋滥）----------
    def test_longterm_is_stricter_than_production(self):
        """长期口径必须在关键维度上不宽于生产口径。"""
        from src.screening_engine import StrategyConfig
        from src.quality_longterm_strategy import LongTermQualityConfig
        lt, prod = LongTermQualityConfig(), StrategyConfig()
        self.assertGreaterEqual(lt.QUALITY_YEARS, prod.QUALITY_YEARS)
        self.assertGreaterEqual(lt.QUALITY_MEDIAN_ROE_MIN, prod.QUALITY_MEDIAN_ROE_MIN)
        self.assertGreaterEqual(lt.QUALITY_MIN_ROE, prod.QUALITY_MIN_ROE)
        self.assertLessEqual(lt.VALUATION_INDUSTRY_PERCENTILE_MAX, prod.VALUATION_INDUSTRY_PERCENTILE_MAX)
        self.assertGreaterEqual(lt.MIN_AMOUNT, prod.MIN_AMOUNT)
        self.assertLessEqual(lt.MAX_PICKS, prod.MAX_PICKS)
        self.assertGreaterEqual(lt.MIN_QUALITY_SCORE, prod.MIN_QUALITY_SCORE)
        self.assertGreaterEqual(lt.MIN_TECHNICAL_SCORE_FORMAL, prod.MIN_TECHNICAL_SCORE_FORMAL)
        # 持有期关系（2026-10-13 生产中期化后）：生产 40~60、长期 30~90——两者的区分
        # 已从「持有更长」转为「质量门槛更高 + 最大持有期更长」（长期仍覆盖最长期限）。
        self.assertGreaterEqual(lt.HOLD_DAYS_HINT_MAX, prod.HOLD_DAYS_HINT_MAX)
        # 止跌口径必须与生产同为 strict（risk_only 会放行更多票）
        self.assertEqual(lt.QV_STABILIZATION_MODE, prod.QV_STABILIZATION_MODE)

    def test_reason_codes_registered(self):
        """新增否决码必须登记进漏斗归因表，否则日志算成 ERROR。"""
        from src.screening_engine import _QV_REASON_CODES
        for code in ("FAIL_ACCELERATING_DECLINE", "FAIL_ROE_UNSTABLE",
                     "FAIL_EARNINGS_INCONSISTENT", "FAIL_LONGTERM_CASH_POOR",
                     "FAIL_LONGTERM_QUALITY_SCORE", "FAIL_RELATIVE_STRENGTH_WEAK"):
            self.assertIn(code, _QV_REASON_CODES, f"{code} 未登记，漏斗日志会失真")

    # ---------- 4) 落库与展示口径 ----------
    def test_strategy_registered(self):
        """longterm_quality 必须已注册，否则落库被静默并入 bottom_fishing。"""
        from store.mysql_store import KNOWN_STRATEGIES, STRATEGY_LONGTERM_QUALITY
        self.assertIn(STRATEGY_LONGTERM_QUALITY, KNOWN_STRATEGIES)
        self.assertLessEqual(len(STRATEGY_LONGTERM_QUALITY), 16)  # VARCHAR(16)

    def test_unknown_strategy_raises(self):
        """未注册 strategy 必须快速失败，不能静默改写并入 bottom_fishing。

        注意 save_recommendations 会先检查 tier / MySQL 配置，故本用例同时提供
        一行 formal 记录并临时声明 MySQL 已配置，确保真正走到 strategy 校验分支。
        """
        from unittest import mock
        from store.mysql_store import save_recommendations
        df = pd.DataFrame([{"code": "600000", "tier": "formal"}])
        with mock.patch("store.mysql_store.is_configured", return_value=True):
            with self.assertRaises(ValueError) as ctx:
                save_recommendations(df, strategy="not_a_strategy")
        self.assertIn("not_a_strategy", str(ctx.exception))

    def test_holding_days_carried_in_signal(self):
        """持有期必须落在 Signal 正式字段上（会进 to_dict → 卡片），不是临时属性。"""
        import dataclasses as dc
        from src.screening_engine import Signal
        names = {f.name for f in dc.fields(Signal)}
        self.assertIn("holding_days_hint_min", names)
        self.assertIn("holding_days_hint_max", names)
        sig = Signal(code="1", name="x", date="2026-01-01", close=10, score=60, grade="B",
                     daily_score=40, rsi=50, rsi7=50, rsi21=50, vol_ratio=1, turnover_ratio=1,
                     stop_loss=9, take_profit=12, rr_ratio=2, market_env="neutral",
                     has_divergence=False, holding_days_hint_min=30, holding_days_hint_max=90)
        row = sig.to_dict()
        self.assertEqual((row["holding_days_hint_min"], row["holding_days_hint_max"]), (30, 90))

    def test_trade_plan_uses_row_holding_days(self):
        """卡片渲染必须用行内的 30~90 天，而不是回落成生产的 40~60 天。"""
        from src.screening_engine import describe_trade_plan
        row = {"code": "600000", "close": 10.0, "stop_loss": 8.0, "take_profit": 15.0,
               "rr_ratio": 1.875, "quality_score": 70.0, "position_250": 0.2,
               "holding_days_hint_min": 30, "holding_days_hint_max": 90}
        text = "\n".join(describe_trade_plan(row))
        self.assertIn("30~90", text)
        # 无行内字段时才回落到 config 默认（2026-10-13 起为生产中期化口径 40~60）
        fallback = "\n".join(describe_trade_plan(
            {k: v for k, v in row.items() if not k.startswith("holding_days")}))
        self.assertIn("40~60", fallback)

    def test_notify_labels_longterm(self):
        """飞书卡片标题与分组标签必须区分长期 sleeve。"""
        from notify.feishu import _STRATEGY_ROLE_ZH, _STRATEGY_ZH
        self.assertIn("longterm_quality", _STRATEGY_ROLE_ZH)
        self.assertIn("longterm_quality", _STRATEGY_ZH)
        self.assertNotEqual(_STRATEGY_ROLE_ZH["longterm_quality"],
                            _STRATEGY_ROLE_ZH["bottom_fishing"])

    def test_dead_config_removed(self):
        """死配置（从未被任何代码读取）不得继续留在配置里制造虚假安全感。"""
        from src.quality_longterm_strategy import LongTermQualityConfig
        import dataclasses as dc
        names = {f.name for f in dc.fields(LongTermQualityConfig)}
        for dead in ("ALLOW_PB_FALLBACK", "PB_FALLBACK_THRESHOLD", "PB_FALLBACK_PE_MAX",
                     "QV_LOW_GAIN_MIN_PCT", "QV_LOW_GAIN_MAX_PCT"):
            self.assertNotIn(dead, names, f"{dead} 是从未被读取的死配置")
        # 新增的两个必须真的被接线
        self.assertIn("DETECT_ACCELERATING_DECLINE", names)
        self.assertIn("DETECT_VOLUME_EXHAUSTION", names)

    # ---------- 5) 入口脚本 main() 全路径（2026-10-14 生产事故回归）----------
    def _run_main_with_screen_result(self, screen_result):
        """以桩数据跑 run_longterm.main() 全流程，返回 (save_mock, notify_mock)。

        覆盖取数/筛选/熔断/降级探测/落库/通知全部外部依赖，不联网、不连库。
        """
        import run_longterm
        from unittest.mock import MagicMock, patch
        import pandas as pd

        index_df = pd.DataFrame({"date": ["2026-10-09", "2026-10-12", "2026-10-13"],
                                 "close": [3900.0, 3950.0, 4000.0]})
        with patch.object(self.bfs, "get_market_environment",
                          return_value={"regime": "bull", "description": "偏多（测试）"}), \
             patch.object(self.bfs, "get_index_daily", return_value=index_df), \
             patch.object(self.bfs, "latest_completed_trade_day", return_value="2026-10-13"), \
             patch.object(self.bfs, "market_crash_halt", return_value=None), \
             patch.object(self.bfs, "is_data_degraded", return_value=False), \
             patch.object(self.bfs, "_screen_quality_pool", return_value=screen_result), \
             patch.object(run_longterm, "save_recommendations", MagicMock(return_value=1)) as save_mock, \
             patch.object(run_longterm, "notify_screening_result", MagicMock()) as notify_mock:
            # 不缺省 --no-save/--no-notify：必须真的走到落库与通知两步
            run_longterm.main([])
        return save_mock, notify_mock

    def _screen_frame(self):
        import pandas as pd
        return pd.DataFrame([{
            "code": "600001", "name": "测试票", "date": "2026-10-13", "close": 12.34,
            "score": 71.5, "rank_score": 74.2, "grade": "B", "daily_score": 55.0,
            "signals_hit": "长期优质低估低位,止跌中确认", "tier": "formal",
            "holding_days_hint_min": 30, "holding_days_hint_max": 90,
            "valuation_mode": "industry",
        }])

    def test_main_survives_dataframe_truthiness(self):
        """2026-10-14 生产事故回归：筛选产出 DataFrame 时 main() 不得因真值歧义崩溃。

        原写法 `output_df or pd.DataFrame()` 对任何 DataFrame（含空表）求真值，
        pandas 抛 ValueError，且崩点在落库与通知**之前**——当日推荐全丢。
        DataFrame 只允许 `is None` / `.empty` 判断。
        """
        # 1) 非空 DataFrame（事故形态：必须跑完并落库/通知）
        save_mock, notify_mock = self._run_main_with_screen_result(self._screen_frame())
        save_mock.assert_called_once()
        self.assertEqual(save_mock.call_args.kwargs.get("strategy"), "longterm_quality")
        notify_mock.assert_called_once()
        # 2) 空 DataFrame（全部 pending 时 _screen_quality_pool 的合法返回）
        save_mock, notify_mock = self._run_main_with_screen_result(self._screen_frame().iloc[0:0])
        save_mock.assert_called_once()
        notify_mock.assert_called_once()
        # 3) None（熔断/无候选）：不得崩，通知仍须发出（今日无正式推荐卡片）
        save_mock, notify_mock = self._run_main_with_screen_result(None)
        save_mock.assert_called_once()
        notify_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
