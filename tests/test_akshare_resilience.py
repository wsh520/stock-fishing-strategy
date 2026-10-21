"""AkShare 通道韧性与卡死股票放弃（2026-10-21）。

背景（run #111/#112/#113 实证）：
- AkShare 每个接口内部 `requests.get(..., timeout=None)` 且每次新建 Session：
  每只股票一次 TCP/TLS 握手（实测 1.63s/次 → 复用后 0.31s/次），超时缺省时单次调用
  可无限挂起；
- 东财抖动时没有通道级熔断，每只股票都要先重试再走新浪，3000 只 = 9000 次无效请求，
  日志表现为「一直重连」；
- run #111 的 600511 在取数里卡了 1750 秒以上，整条 job 被取消。

本文件锁定四条行为，全部不依赖外网：
1. 安装补丁后 requests 调用被注入默认超时，且线程内复用同一个 Session；
2. 单通道重试上限 = MAX_RETRY+1，连续失败达阈值后熔断，熔断期内不再发起请求；
3. 单只股票连续失败 STOCK_MAX_ATTEMPTS 次即放弃（记 FAIL_DATA），不阻塞整池；
4. DATA_SOURCE_PRIMARY=akshare 时取数顺序反转，且不再把「AkShare 主源」误报为数据降级。
"""
import os
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import requests

import src.akshare_resilience as res
import src.screening_engine as m
from src.screening_engine import StrategyConfig, run_concurrent_screen


def _hist_frame(rows: int = 40) -> pd.DataFrame:
    idx = pd.date_range("2026-08-01", periods=rows, freq="D")
    return pd.DataFrame({
        "日期": [d.strftime("%Y-%m-%d") for d in idx],
        "开盘": [10.0] * rows, "收盘": [10.0 + i * 0.01 for i in range(rows)],
        "最高": [10.5] * rows, "最低": [9.5] * rows,
        "成交量": [1e6] * rows, "成交额": [1e7] * rows,
        "涨跌幅": [0.0] * rows, "换手率": [1.0] * rows,
    })


def _sina_frame(rows: int = 40) -> pd.DataFrame:
    idx = pd.date_range("2026-08-01", periods=rows, freq="D")
    return pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in idx],
        "open": [10.0] * rows, "high": [10.5] * rows, "low": [9.5] * rows,
        "close": [10.0 + i * 0.01 for i in range(rows)], "volume": [1e6] * rows,
        "amount": [1e7] * rows, "turnover": [1.0] * rows,
    })


class HttpLayerTests(unittest.TestCase):
    def setUp(self):
        res.install()
        res.reset_channels()

    def test_timeout_injected_and_session_reused(self):
        fake_session = Mock()
        fake_session.request.return_value = "sentinel"
        with patch.object(res, "_session", return_value=fake_session):
            # 显式 timeout 原样保留；缺省时注入默认值
            requests.api.request("get", "https://example.test/a", timeout=5)
            requests.api.request("get", "https://example.test/b")
        calls = fake_session.request.call_args_list
        self.assertEqual(calls[0].kwargs["timeout"], 5)
        self.assertEqual(calls[1].kwargs["timeout"], res._timeout["sec"])

    def test_thread_local_session_is_stable(self):
        self.assertIs(res._session(), res._session())

    def test_configure_takes_effect_without_reinstall(self):
        res.configure(7.5)
        self.assertEqual(res._timeout["sec"], 7.5)
        self.assertFalse(res.install(), "重复 install 必须幂等")
        res.configure(res.DEFAULT_TIMEOUT_SEC)


class ChannelBreakerTests(unittest.TestCase):
    def setUp(self):
        res.reset_channels()
        self.cfg = StrategyConfig()
        self.cfg.FETCH_RETRY_BACKOFF_BASE_SEC = 0.001
        self.cfg.FETCH_RETRY_BACKOFF_MAX_SEC = 0.002
        self.cfg.FETCH_RETRY_JITTER = 0.0

    def test_retry_cap_is_max_retry_plus_one(self):
        calls = []

        def boom():
            calls.append(1)
            raise TimeoutError("boom")

        with patch.object(res.time, "sleep") as sleep:
            self.assertIsNone(res.call("eastmoney", boom, "ak_hist(TEST)", retries=2, config=self.cfg))
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_breaker_opens_after_threshold_and_skips_requests(self):
        attempts = []

        def boom():
            attempts.append(1)
            raise ConnectionError("reset by peer")

        with patch.object(res.time, "sleep"):
            for _ in range(self.cfg.AK_CHANNEL_FAIL_THRESHOLD):
                res.call("eastmoney", boom, "ak_hist(TEST)", retries=0, config=self.cfg)
        before = len(attempts)
        self.assertTrue(res.is_open("eastmoney"))
        with self.assertRaises(res.ChannelUnavailable):
            res.call("eastmoney", boom, "ak_hist(TEST)", retries=0, config=self.cfg)
        self.assertEqual(len(attempts), before, "熔断期内不应再发起请求")
        self.assertEqual(res.snapshot()["eastmoney"]["skipped"], 1)

    def test_success_resets_failure_streak(self):
        with patch.object(res.time, "sleep"):
            for _ in range(self.cfg.AK_CHANNEL_FAIL_THRESHOLD - 1):
                res.call("sina", lambda: (_ for _ in ()).throw(TimeoutError()), "x", retries=0, config=self.cfg)
            res.call("sina", lambda: "ok", "x", retries=0, config=self.cfg)
            res.call("sina", lambda: (_ for _ in ()).throw(TimeoutError()), "x", retries=0, config=self.cfg)
        self.assertFalse(res.is_open("sina"))


class FailedStockGiveUpTests(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig()
        self.cfg.MAX_WORKERS = 2
        self.cfg.STOCK_MAX_ATTEMPTS = 3
        self.cfg.FETCH_RETRY_BACKOFF_BASE_SEC = 0.001
        self.cfg.FETCH_RETRY_BACKOFF_MAX_SEC = 0.002
        self.cfg.FETCH_RETRY_JITTER = 0.0

    def test_stock_abandoned_after_three_failures(self):
        seen = []

        def screen_one(stock):
            seen.append(stock["code"])
            raise ConnectionError("卡死")

        stocks = [{"code": "600511", "name": "国药股份"}, {"code": "600000", "name": "浦发银行"}]
        results, processed, budget_hit = run_concurrent_screen(stocks, screen_one, self.cfg, Mock())

        self.assertFalse(budget_hit)
        self.assertEqual(processed, 2)
        self.assertEqual(results, [(None, "FAIL_DATA"), (None, "FAIL_DATA")])
        self.assertEqual(len(seen), 6)  # 每只 3 次尝试

    def test_stock_succeeds_after_transient_failure(self):
        state = {"600511": 0}

        def screen_one(stock):
            state[stock["code"]] += 1
            if state[stock["code"]] == 1:
                raise TimeoutError("偶发")
            return ("sig", "PASS")

        results, processed, _ = run_concurrent_screen([{"code": "600511"}], screen_one, self.cfg, Mock())
        self.assertEqual(processed, 1)
        self.assertEqual(results, [("sig", "PASS")])
        self.assertEqual(state["600511"], 2)


class PrimarySourceRoutingTests(unittest.TestCase):
    def setUp(self):
        m._PRIMARY_SOURCE = "baostock"
        m.fetch_stats.update(bs_ok=0, ak_ok=0, fail=0)

    def tearDown(self):
        m._PRIMARY_SOURCE = "baostock"
        m.fetch_stats.update(bs_ok=0, ak_ok=0, fail=0)

    def test_env_override_sets_primary(self):
        with patch.dict(os.environ, {"DATA_SOURCE_PRIMARY": "akshare"}):
            self.assertEqual(StrategyConfig().DATA_SOURCE_PRIMARY, "akshare")

    def test_invalid_primary_rejected(self):
        with self.assertRaises(ValueError):
            StrategyConfig(DATA_SOURCE_PRIMARY="tushare")

    def test_akshare_primary_tries_akshare_first(self):
        cfg = StrategyConfig(DATA_SOURCE_PRIMARY="akshare")
        frame = pd.DataFrame({"date": ["2026-10-20"], "close": [10.0], "pct_chg": [0.0]})
        with patch.object(m, "_fetch_daily_ak", return_value=frame) as ak_fetch, \
             patch.object(m, "_fetch_daily_bs", return_value=frame) as bs_fetch, \
             patch.object(m, "_bs_available", return_value=True):
            out = m._fetch_daily_dual("600000", days=30, config=cfg)
        self.assertIsNotNone(out)
        ak_fetch.assert_called_once()
        bs_fetch.assert_not_called()

    def test_baostock_primary_tries_baostock_first(self):
        cfg = StrategyConfig()
        frame = pd.DataFrame({"date": ["2026-10-20"], "close": [10.0], "pct_chg": [0.0]})
        with patch.object(m, "_fetch_daily_ak", return_value=frame) as ak_fetch, \
             patch.object(m, "_fetch_daily_bs", return_value=frame) as bs_fetch, \
             patch.object(m, "_bs_available", return_value=True):
            m._fetch_daily_dual("600000", days=30, config=cfg)
        bs_fetch.assert_called_once()
        ak_fetch.assert_not_called()

    def test_degraded_flag_uses_active_primary(self):
        m._PRIMARY_SOURCE = "akshare"
        m.fetch_stats.update(bs_ok=0, ak_ok=100, fail=0)
        self.assertFalse(m.is_data_degraded(), "AkShare 主源命中时不应误报降级")
        m.fetch_stats.update(ak_ok=0, fail=5)
        self.assertTrue(m.is_data_degraded())

    def test_eastmoney_breaker_falls_back_to_sina(self):
        cfg = StrategyConfig()
        cfg.USE_CACHE = False
        cfg.AK_CHANNEL_FAIL_THRESHOLD = 1
        with patch.object(m, "ak", Mock(stock_zh_a_daily=Mock(return_value=_sina_frame()))) as fake_ak, \
             patch.object(m, "_fetch_valuation_ak", return_value=None), \
             patch.object(res.time, "sleep"):
            res.reset_channels()
            res.record_failure("eastmoney", threshold=1, cooldown_sec=60)
            out = m._fetch_daily_ak("600000", days=30, config=cfg)
        self.assertIsNotNone(out)
        fake_ak.stock_zh_a_hist.assert_not_called()
        fake_ak.stock_zh_a_daily.assert_called_once()
        res.reset_channels()


def _valuation_frame(dates, pe=10.0, pb=1.5):
    return pd.DataFrame({"date": list(dates), "peTTM": [pe] * len(dates), "pbMRQ": [pb] * len(dates)})


class ValuationChannelTests(unittest.TestCase):
    """AkShare 日线补估值（百度 TTM）：让 AkShare 主源不再因缺 peTTM 而全员 pending。"""

    def setUp(self):
        res.reset_channels()
        self.cfg = StrategyConfig()
        self.cfg.USE_CACHE = False

    def _daily(self, dates):
        return pd.DataFrame({"date": list(dates), "close": [10.0] * len(dates), "pct_chg": [0.0] * len(dates)})

    def test_merges_pe_pb_by_date(self):
        with patch.object(m, "_fetch_valuation_ak",
                          return_value=_valuation_frame(["2026-10-19", "2026-10-20"])):
            out = m._attach_ak_valuation("600000", self._daily(["2026-10-19", "2026-10-20"]), self.cfg)
        self.assertEqual(list(out["peTTM"]), [10.0, 10.0])
        self.assertEqual(list(out["pbMRQ"]), [1.5, 1.5])
        self.assertEqual(list(out["val_carried"]), [False, False])

    def test_carries_previous_valuation_for_last_bar(self):
        daily = self._daily(["2026-10-19", "2026-10-20", "2026-10-21"])
        with patch.object(m, "_fetch_valuation_ak",
                          return_value=_valuation_frame(["2026-10-19", "2026-10-20"])), \
             patch.object(m, "_valuation_carry", {"count": 0}), \
             patch.object(m, "_valuation_carry_lock", __import__("threading").Lock()):
            out = m._attach_ak_valuation("600000", daily, self.cfg)
        self.assertEqual(list(out["peTTM"]), [10.0, 10.0, 10.0])
        self.assertTrue(bool(out["val_carried"].iloc[-1]))
        self.assertFalse(bool(out["val_carried"].iloc[-2]))

    def test_no_carry_beyond_limit(self):
        daily = self._daily(["2026-10-19", "2026-10-26"])
        with patch.object(m, "_fetch_valuation_ak", return_value=_valuation_frame(["2026-10-19"])):
            out = m._attach_ak_valuation("600000", daily, self.cfg)
        self.assertFalse(bool(out["val_carried"].iloc[-1]))
        self.assertTrue(pd.isna(out["peTTM"].iloc[-1]), "超过顺延上限必须保留缺项，不能拿过期估值放行")

    def test_missing_channel_still_adds_empty_columns(self):
        with patch.object(m, "_fetch_valuation_ak", return_value=None):
            out = m._attach_ak_valuation("600000", self._daily(["2026-10-20"]), self.cfg)
        self.assertIn("peTTM", out.columns)
        self.assertIn("pbMRQ", out.columns)
        self.assertTrue(pd.isna(out["peTTM"].iloc[-1]))

    def test_valuation_channel_unavailable_returns_none(self):
        res.record_failure("baidu", threshold=1, cooldown_sec=60)
        with patch.object(m, "ak", Mock()):
            self.assertIsNone(m._fetch_valuation_ak("600000", self.cfg))
        res.reset_channels()

    def test_cache_without_valuation_is_treated_as_miss(self):
        import shutil
        from pathlib import Path

        tmp = Path(".test-artifacts") / "cache_valuation_case"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            cfg = StrategyConfig()
            cfg.CACHE_DIR = str(tmp)
            cfg.USE_CACHE = True
            # 末 bar 必须是"今天"，否则 meta 校验会判旧缓存（quote_cache_valid），命中不了缓存
            idx = pd.date_range(end=m._beijing_now().date(), periods=40, freq="D")
            hist = _hist_frame(40).assign(日期=[d.strftime("%Y-%m-%d") for d in idx])
            fake = Mock(stock_zh_a_hist=Mock(return_value=hist))
            with patch.object(m, "ak", fake), \
                 patch.object(m, "_fetch_valuation_ak", return_value=_valuation_frame(hist["日期"])):
                first = m._fetch_daily_ak("600000", days=30, config=cfg)
                self.assertIn("peTTM", first.columns)
                m._fetch_daily_ak("600000", days=30, config=cfg)
            self.assertEqual(fake.stock_zh_a_hist.call_count, 1, "当日缓存应直接命中")
            # 模拟估值列接入前写下的旧缓存：没有 peTTM 时必须重新取数
            cached = [p for p in tmp.iterdir() if p.suffix == ".csv"][0]
            pd.DataFrame({"date": hist["日期"], "close": [10.0] * len(hist)}).to_csv(cached, index=False)
            with patch.object(m, "ak", fake), \
                 patch.object(m, "_fetch_valuation_ak", return_value=_valuation_frame(hist["日期"])):
                m._fetch_daily_ak("600000", days=30, config=cfg)
            self.assertEqual(fake.stock_zh_a_hist.call_count, 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
