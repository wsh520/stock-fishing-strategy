"""Baostock 取数退避与登录单飞（2026-10-17，run #109 实证）。

背景：run #109 行业估值快照阶段 Baostock 每约 10-15 分钟一轮异常
（timed out / utf-8 脏数据 / 接收数据异常），每轮之后 4 个工作线程在 bs_lock 上
串行重登（每次约 4 秒），且 _fetch_with_retry 的退避是线性 0.5/1.0/1.5s、
无抖动、多线程节拍同步 —— 同步重试叠加已过载的服务端，把一次抖动放大成一轮。

本文件锁定：
1. 退避为指数（base*2^attempt 夹在 max 内）+ 均匀抖动，参数同源
   StrategyConfig.FETCH_RETRY_*（无第二份字面量）；
2. 登录单飞：连接置断后并发进入 _bs_login 的多个线程只触发一次 bs.login()。
"""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from src.screening_engine import (StrategyConfig, _bs_login, _bs_state,
                                  _fetch_with_retry, _retry_backoff_seconds)
import src.screening_engine as m


class RetryBackoffTests(unittest.TestCase):
    def test_exponential_growth_with_cap_and_jitter(self):
        cfg = StrategyConfig()
        cfg.FETCH_RETRY_BACKOFF_BASE_SEC = 1.0
        cfg.FETCH_RETRY_BACKOFF_MAX_SEC = 4.0
        cfg.FETCH_RETRY_JITTER = 0.5
        seen = [_retry_backoff_seconds(attempt, cfg) for _ in range(40) for attempt in range(4)]
        for value in seen:
            self.assertGreater(value, 0.0)
            self.assertLessEqual(value, 4.0 * 1.5 + 1e-9)  # cap×(1+jitter)
        # 指数形态：同 attempt 的样本必须覆盖 [base*2^a×(1-j), base*2^a×(1+j)]
        for attempt, center in enumerate((1.0, 2.0, 4.0, 4.0)):
            samples = [_retry_backoff_seconds(attempt, cfg) for _ in range(200)]
            self.assertGreaterEqual(min(samples), center * 0.5 - 1e-9)
            self.assertLessEqual(max(samples), center * 1.5 + 1e-9)

    def test_class_defaults_are_single_source(self):
        """config 缺省时读 StrategyConfig 类默认，不复制字面量。"""
        cfg = StrategyConfig()
        with patch.object(m.time, "sleep") as sleep:
            calls = []

            def flaky():
                calls.append(1)
                if len(calls) < 2:
                    raise TimeoutError("boom")
                return "ok"

            self.assertEqual(_fetch_with_retry(flaky, 2, "label"), "ok")
        self.assertEqual(sleep.call_count, 1)
        delay = sleep.call_args.args[0]
        base = StrategyConfig.FETCH_RETRY_BACKOFF_BASE_SEC
        jitter = StrategyConfig.FETCH_RETRY_JITTER
        self.assertGreaterEqual(delay, base * (1 - jitter) - 1e-9)
        self.assertLessEqual(delay, base * (1 + jitter) + 1e-9)

    def test_retry_count_and_final_failure(self):
        with patch.object(m.time, "sleep") as sleep:
            calls = []

            def always_fail():
                calls.append(1)
                raise TimeoutError("boom")

            self.assertIsNone(_fetch_with_retry(always_fail, 2, "label"))
        self.assertEqual(len(calls), 3)          # 1 次首发 + 2 次重试
        self.assertEqual(sleep.call_count, 2)    # 最后一次失败不再睡

    def test_success_never_sleeps(self):
        with patch.object(m.time, "sleep") as sleep:
            self.assertEqual(_fetch_with_retry(lambda: "ok", 3, "label"), "ok")
        sleep.assert_not_called()

    def test_invalid_backoff_config_is_rejected(self):
        for field, bad in (("FETCH_RETRY_BACKOFF_BASE_SEC", 0.0),
                           ("FETCH_RETRY_BACKOFF_MAX_SEC", -1.0),
                           ("FETCH_RETRY_JITTER", 1.5)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                StrategyConfig(**{field: bad})


class LoginSingleFlightTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(_bs_state)
        self._saved_gate = m._bs_login_gate
        _bs_state.update(logged_in=False, consecutive_failures=0, circuit_open=False)

    def tearDown(self):
        _bs_state.clear()
        _bs_state.update(self._saved)
        m._bs_login_gate = self._saved_gate

    def test_concurrent_logins_trigger_one_bs_login(self):
        """连接置断后 4 线程并发进入 _bs_login：只允许一次真实 bs.login()。"""
        login_calls = []

        def fake_login():
            login_calls.append(1)
            time.sleep(0.05)  # 让其他线程有机会在门外堆积
            return Mock(error_code="0", error_msg="")

        results = []
        threads = [threading.Thread(target=lambda: results.append(_bs_login(max_retry=2)))
                   for _ in range(4)]
        with patch.object(m, "bs", Mock(login=fake_login)):
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
        self.assertEqual(len(login_calls), 1)
        self.assertEqual(results, [True] * 4)
        self.assertTrue(_bs_state["logged_in"])

    def test_gate_releases_after_failed_login(self):
        """登录最终失败时门锁必须释放，后续调用仍可重试（不死锁）。"""
        with patch.object(m, "bs", Mock(login=Mock(return_value=Mock(error_code="1", error_msg="x")))), \
             patch.object(m.time, "sleep"):
            self.assertFalse(_bs_login(max_retry=2))
            self.assertFalse(m._bs_login_gate.locked())
            self.assertFalse(_bs_login(max_retry=1))


if __name__ == "__main__":
    unittest.main()
