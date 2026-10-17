"""2026-10-15 P1 回归：锁定 QV_RECENT_LOW_MAX_AGE=40 不被放宽。

背景：10-14 曾论证「too_old 组 gain 中位仅 15.6%，放宽 max_age 是唯一杠杆」。
10-15 用 350 只真实行情（as_of 2026-10-14）复测后该论证被推翻，实测结论如下：

  too_old(age>44) 214 只，其中同时站上 MA20 且 MA20 斜率>=0 的 38 只。
  这 38 只若因放宽 max_age 被放行，其 gain 分布为：
      gain 5-15% : 12 只 (31.6%)，pos250 中位 12.1%
      gain>15%   : 26 只 (68.4%)，pos250 中位 34.4%，最高 68.5%/pos250 166.6%
      gain<5%    :  0 只
  即：放宽不会放进「创新低的票」（那些 below_ma20，已被止跌闸门先行拦掉），
      放进的是「7 月底横盘至今已累计上涨 15%+」的票，与「低位」定位冲突。

  作为对照，当前 max_age=40 下底部确认且止跌通过的 18 只，gain 中位仅 8.3%、
  pos250 中位 13.7%，gain>15% 的只有 2 只 —— 这才是策略认可的低位形态。

因此 P1 决议：不放宽 max_age。本测试锁定该口径，任何上调必须先补前瞻收益证据
（audit_predictive_power.py 按当前口径多决策日复测），不得仅凭静态分布改动。

2026-10-17 追加记录（Actions run #109 复查）：夹逼原样复现 —— 4 只
recent_bottom_unconfirmed 中 3 只 age=59（too_old，锚 2026-07-17）、1 只 age=1
（too_new，锚 2026-10-15），甜区 [5,40] 连续第 3 次零命中，当日正式推荐仍为 0。
但本轮同时确认了两件事，决议维持不变：
  1) #109 的 3 只 too_old 候选 gain 为 3.9%/4.5%/9.6%（贴着底部的亚型，
     与 10-15 样本中占多数的 gain>15% 亚型不同），然而它们分别还被累计口径
     经营恶化（公牛/小商品城）与 operating_trend（中国汽研）挡着 ——
     即使放宽 max_age，当日也最多只有中国汽研一只可能转正，单票静态证据
     不构成改产线口径的依据；
  2) 前瞻收益证据的前置条件目前离线不可达：backtest_cache/fund 的 pit 数据
     只覆盖 2026-06-04 单决策日，多决策日复测需要重新拉取基本面
     （每日约 6000 次查询，有 Baostock 配额风险），未擅自发起。
"""
import unittest

from src.screening_engine import StrategyConfig


class RecentLowWindowLockedTests(unittest.TestCase):
    def test_production_window_is_sixty_with_forty_max_age(self):
        config = StrategyConfig()
        self.assertEqual(config.QV_RECENT_LOW_WINDOW, 60)
        self.assertEqual(config.QV_RECENT_LOW_MIN_AGE, 5)
        self.assertEqual(config.QV_RECENT_LOW_MAX_AGE, 40)

    def test_invariant_holds_for_every_window_override(self):
        """基类硬校验：max_age 必须严格小于 window，且不小于 min_age。"""
        config = StrategyConfig()
        for window in (60, 90, 120):
            for max_age in (20, 40, 60, 90):
                if not 5 <= max_age < window:
                    continue
                with self.subTest(window=window, max_age=max_age):
                    tuned = StrategyConfig()
                    tuned.QV_RECENT_LOW_WINDOW = window
                    tuned.QV_RECENT_LOW_MAX_AGE = max_age
                    self.assertLessEqual(tuned.QV_RECENT_LOW_MIN_AGE, tuned.QV_RECENT_LOW_MAX_AGE)
                    self.assertLess(tuned.QV_RECENT_LOW_MAX_AGE, tuned.QV_RECENT_LOW_WINDOW)

    def test_relaxing_window_must_stay_legal(self):
        """放宽本身允许发生（配置支持），但必须仍满足不变式，避免静默失效。"""
        tuned = StrategyConfig()
        tuned.QV_RECENT_LOW_WINDOW = 120
        tuned.QV_RECENT_LOW_MAX_AGE = 90
        self.assertLess(tuned.QV_RECENT_LOW_MAX_AGE, tuned.QV_RECENT_LOW_WINDOW)
        self.assertEqual(tuned.QV_RECENT_LOW_MIN_AGE, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)