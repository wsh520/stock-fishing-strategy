"""Offline upgrades: explicit operating tolerances and optional flow evidence."""
import copy
import unittest
from unittest.mock import Mock
from src.recent_operating import evaluate_operating_trend, fetch_operating_trend


def row(period, **kwargs):
    year, month, _ = map(int, period.split("-"))
    values = dict(revenue=1000., net_profit=100., deducted_profit=90.,
                  operating_cashflow=110., gross_margin=30., net_margin=10.,
                  report_date=period, available_date=f"{year + (month == 12)}-{3 if month == 12 else month + 1:02d}-20")
    values.update(kwargs)
    return values


class OperatingUpgradesTests(unittest.TestCase):
    def evaluate(self, current=None, **kwargs):
        return evaluate_operating_trend([row("2025-03-31"), row("2026-03-31", **(current or {}))],
                                        "2026-06-12", **kwargs)

    def test_three_percent_default_and_configurable_boundary(self):
        self.assertEqual(self.evaluate(dict(revenue=970, net_profit=97, deducted_profit=87.3))["status"], "verified")
        self.assertEqual(self.evaluate(dict(net_profit=96.99))["status"], "weak")
        self.assertEqual(self.evaluate(dict(net_profit=98), yoy_decline_tolerance=0)["status"], "weak")
        self.assertEqual(self.evaluate(dict(net_profit=89))["status"], "failed")

    def test_micro_cash_decline_requires_healthy_conversion(self):
        self.assertEqual(self.evaluate(dict(operating_cashflow=109))["status"], "verified")
        self.assertEqual(self.evaluate(dict(operating_cashflow=100))["status"], "weak")
        self.assertEqual(self.evaluate(dict(operating_cashflow=109), cash_conversion_min=1.2)["status"], "weak")
        self.assertEqual(self.evaluate(dict(operating_cashflow=109), cashflow_decline_tolerance=0)["status"], "weak")

    def test_joint_decline_still_vetoes_inside_tolerance(self):
        result = self.evaluate(dict(revenue=990, net_profit=99, operating_cashflow=109))
        self.assertEqual(result["status"], "failed")
        self.assertIn("sales_profit_cash_deteriorating", result["reasons"])

    def test_missing_supplementary_evidence_does_not_force_pending(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "verified")
        self.assertIsNone(result["metrics"]["net_profit_ttm"])
        self.assertEqual(result["metrics"]["net_profit_single_quarter"], 100)
        self.assertEqual(self.evaluate(dict(net_profit=None))["status"], "missing")

    def test_latest_single_quarter_decline_is_separately_flagged(self):
        rows = [row("2025-03-31", revenue=1000, net_profit=100, deducted_profit=90, operating_cashflow=110),
                row("2025-06-30", revenue=2000, net_profit=400, deducted_profit=360, operating_cashflow=440),
                row("2025-12-31", revenue=4000, net_profit=700, deducted_profit=630, operating_cashflow=770),
                row("2026-03-31", revenue=1000, net_profit=100, deducted_profit=90, operating_cashflow=110),
                row("2026-06-30", revenue=2000, net_profit=350, deducted_profit=315, operating_cashflow=385)]
        result = evaluate_operating_trend(rows, "2026-09-01")
        # 累计净利同比为负但处于容忍区间；最新单季净利由300降至250。
        self.assertEqual(result["status"], "failed")
        self.assertIn("net_profit_latest_quarter_declining", result["reasons"])

    # ===== 单季信号不再参与 status 判定（2026-10-16口径修正）=====
    #
    # 背景（Actions run #108 实测）：2963 只 → 候选 11 → 正式 0。11 只候选的
    # operating_weak 覆盖率 100%，operating_cashflow 字段命中率同样 100%。
    # 典型误杀：国电南瑞累计经营现金流 +14.7%、青岛啤酒 +14.7%、兔宝宝 +98.3%、
    # 吉比特 +18.2%（净利 +55.6%）—— 累计全部改善，却因单季为负被判 weak；
    # 中国汽研更仅凭单季 −3.1%（刚过阈值）被判 weak。
    #
    # 根因：单季信号被 append 进 weak，而 status = "weak" if weak else "verified"
    # （任一命中即降级），于是它事实上成了硬否决 —— 与原注释承诺的
    # 「仅形成独立弱信号，不作为硬否决」自相矛盾。
    #
    # 修法：单季信号只记入 reasons / latest_quarter_signals，不再参与 status。
    # 注意单季由 YTD 差分而来（Qn = YTDn − YTD(n−1)），现金流本身强季节性，
    # 单季负增长在大盘下跌期是全市场共性、不含个股信息。

    def _quarterly_rows(self, cashflow=None, net_profit=None):
        """构造两期Q1+Q2 数据，使单季与累计方向相反。

        默认：累计现金流 +15.0% 而单季 −13.8%（复现 run #108 国电南瑞的结构）。
        """
        base_cf = dict(revenue=1000., net_profit=100., deducted_profit=90., operating_cashflow=100.)
        base_np = dict(revenue=1000., net_profit=110., deducted_profit=90., operating_cashflow=110.)
        rows = [row("2025-03-31", **(base_cf if cashflow else base_np)),
                row("2025-06-30", operating_cashflow=187., net_profit=260.),
                row("2025-12-31", operating_cashflow=400., net_profit=520.),
                row("2026-03-31", operating_cashflow=140., net_profit=130.),
                row("2026-06-30", operating_cashflow=215., net_profit=266.)]
        if cashflow:
            rows[0].update(operating_cashflow=100.)
        if net_profit:
            rows[0].update(net_profit=110.)
        return rows

    def test_single_quarter_cashflow_does_not_veto_when_cumulative_improves(self):
        result = evaluate_operating_trend(self._quarterly_rows(cashflow=True), "2026-09-01")
        metrics = result["metrics"]
        self.assertGreater(metrics["operating_cashflow_yoy"], 0.0)          # 累计为正
        self.assertLess(metrics["operating_cashflow_single_quarter_yoy"], 0.0)  # 单季为负
        # 核心断言：累计改善时，单季信号不得把 status 打成 weak。
        self.assertEqual(result["status"], "verified")
        # 但信号必须仍被记录，供describe / summarize / 人工复核使用。
        self.assertIn("operating_cashflow_latest_quarter_declining",
                      result["latest_quarter_signals"])
        self.assertIn("operating_cashflow_latest_quarter_declining", result["reasons"])

    def test_single_quarter_profit_does_not_veto_when_cumulative_improves(self):
        result = evaluate_operating_trend(self._quarterly_rows(net_profit=True), "2026-09-01")
        self.assertGreater(result["metrics"]["net_profit_yoy"], 0.0)
        self.assertLess(result["metrics"]["net_profit_single_quarter_yoy"], 0.0)
        self.assertEqual(result["status"], "verified")
        self.assertIn("net_profit_latest_quarter_declining", result["latest_quarter_signals"])

    def test_cumulative_cashflow_decline_still_vetoes(self):
        """防削弱：累计口径真的转负时，闸门必须照常生效。"""
        rows = [row("2025-03-31", operating_cashflow=100.),
                row("2025-06-30", operating_cashflow=187., net_profit=260.),
                row("2025-12-31", operating_cashflow=400., net_profit=520.),
                row("2026-03-31", operating_cashflow=70., net_profit=130.),
                row("2026-06-30", operating_cashflow=134., net_profit=266.)]
        result = evaluate_operating_trend(rows, "2026-09-01")
        self.assertLess(result["metrics"]["operating_cashflow_yoy"], -3.0)
        self.assertEqual(result["status"], "weak")
        self.assertIn("operating_cashflow_declining", result["reasons"])

    def test_single_quarter_signal_alone_cannot_make_status_weak(self):
        """单季信号必须完全无力：即使四个单季字段同时恶化，status 仍由累计口径决定。

        这是本次修复的语义边界 —— 若将来有人把单季信号重新接回 status，
        本条会立刻失败。
        """
        rows = [row("2025-03-31", operating_cashflow=100., net_profit=100.),
                row("2025-06-30", revenue=1000., net_profit=260., deducted_profit=240.,
                    operating_cashflow=187., gross_margin=30., net_margin=15.),
                row("2025-12-31", revenue=2000., net_profit=520., deducted_profit=480.,
                    operating_cashflow=400., gross_margin=30., net_margin=15.),
                row("2026-03-31", revenue=1000., net_profit=130., deducted_profit=120.,
                    operating_cashflow=140., gross_margin=30., net_margin=15.),
                row("2026-06-30", revenue=2050., net_profit=266., deducted_profit=248.,
                    operating_cashflow=215., gross_margin=30., net_margin=15.)]
        result = evaluate_operating_trend(rows, "2026-09-01")
        # 累计口径全部健康（现金流 +15.0%、净利 +2.3%），但单季现金流同比为负。
        self.assertGreater(result["metrics"]["operating_cashflow_yoy"], 0.0)
        self.assertLess(result["metrics"]["operating_cashflow_single_quarter_yoy"], 0.0)
        self.assertEqual(result["status"], "verified")
        self.assertIn("operating_cashflow_latest_quarter_declining",
                      result["latest_quarter_signals"])

    def test_cumulative_quarter_and_ttm_exact_arithmetic(self):
        rows = [row("2024-06-30", net_profit=50), row("2024-12-31", net_profit=120),
                row("2025-03-31", net_profit=40), row("2025-06-30", net_profit=70),
                row("2025-12-31", net_profit=150), row("2026-03-31", net_profit=50),
                row("2026-06-30", net_profit=90)]
        for r in rows:
            r["attributable_profit"] = r["net_profit"] * .8
        original = copy.deepcopy(rows)
        result = evaluate_operating_trend(rows, "2026-09-01")
        metrics = result["metrics"]
        self.assertEqual(metrics["net_profit_single_quarter"], 40)
        self.assertAlmostEqual(metrics["net_profit_single_quarter_yoy"], 100 / 3)
        self.assertEqual(metrics["net_profit_ttm"], 170)
        self.assertEqual(metrics["attributable_profit_ttm"], 136)
        self.assertAlmostEqual(metrics["net_profit_ttm_yoy"], (170 / 140 - 1) * 100)
        self.assertEqual(rows, original)
        rows[4]["available_date"] = "2026-09-02"
        self.assertIsNone(evaluate_operating_trend(rows, "2026-09-01")["metrics"]["net_profit_ttm"])

    def test_parameter_validation_and_fetch_forwarding(self):
        for field in ("yoy_decline_tolerance", "cashflow_decline_tolerance", "cash_conversion_min"):
            for value in (-1, float("nan"), True):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.evaluate(**{field: value})
        getter = Mock(side_effect=TimeoutError)
        result = fetch_operating_trend("600004", "2026-06-12", -10, 3, 2, getter,
                                      yoy_decline_tolerance=0, cashflow_decline_tolerance=0)
        self.assertEqual(result["status"], "missing")
        self.assertEqual(getter.call_args.kwargs["timeout"], 15)


if __name__ == "__main__":
    unittest.main()
