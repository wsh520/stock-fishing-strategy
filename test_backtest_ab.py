"""backtest.py `ab` 子命令的离线测试（纯函数层，不联网、不读缓存）。

`ab` 的价值在于「各变体共用同一份面板、只差被考察的那一个参数」，因此
配置解析与报告渲染这两层必须绝对可靠：一个拼错的字段名会让变体静默退回
基准（看起来像「该参数无影响」），一行缺失的警示会让非实盘口径的结论被
误当成实盘结论。这里把这些风险固化成断言。

不覆盖 `_ab_metrics` / `cmd_ab`：它们需要联网取数，由真实运行验证。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest as bt
from src.bottom_fishing_strategy import StrategyConfig

_RESULTS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, bool(ok)))


# ===========================================================================
# 1) 变体定义自检：模式名合法、变体名不重复、覆盖项均为真实字段
# ===========================================================================
cfg = StrategyConfig()
_valid_modes = {"quality_value", "technical"}
check("AB_VARIANTS: 模式键均为合法 RECOMMENDATION_MODE",
      set(bt.AB_VARIANTS) <= _valid_modes)

_dup = [m for m, v in bt.AB_VARIANTS.items() if len(v) != len(set(v))]
check("AB_VARIANTS: 同模式内变体名不重复", not _dup)

_bad_keys = []
for _mode, _variants in bt.AB_VARIANTS.items():
    for _name, _ov in _variants.items():
        for _k in _ov:
            if not hasattr(cfg, _k):
                _bad_keys.append(f"{_mode}/{_name}:{_k}")
check(f"AB_VARIANTS: 覆盖项均为真实配置字段（发现非法项 {_bad_keys}）", not _bad_keys)

check("AB_VARIANTS: 每个模式都含 baseline 作为对照基准",
      all("baseline" in v for v in bt.AB_VARIANTS.values()))
check("AB_VARIANTS: quality_value 组含生产默认口径的 baseline（覆盖项为空）",
      bt.AB_VARIANTS["quality_value"]["baseline"] == {})
check("AB_VARIANTS: 变体只改 1~2 个参数（否则无法归因）",
      all(len(v) <= 2 for variants in bt.AB_VARIANTS.values() for v in variants.values()))

# ===========================================================================
# 2) --set 解析：类型强转与非法字段
# ===========================================================================
check("coerce: bool 识别 true/false/1/0/on/off",
      bt._coerce_config_value(True, "false") is False
      and bt._coerce_config_value(True, "True") is True
      and bt._coerce_config_value(True, "1") is True
      and bt._coerce_config_value(False, "0") is False
      and bt._coerce_config_value(True, "on") is True)
check("coerce: bool 字段不会被 '0' 当成非空字符串判为 True",
      bt._coerce_config_value(True, "0") is False)
check("coerce: int / float / str 分别按原类型转换",
      bt._coerce_config_value(5, "7") == 7
      and isinstance(bt._coerce_config_value(5, "7"), int)
      and abs(bt._coerce_config_value(1.5, "2.5") - 2.5) < 1e-9
      and bt._coerce_config_value("B", "C") == "C")

_base = StrategyConfig()
check("apply: 空 --set 时原样返回（同一对象）", bt._apply_overrides(_base, None) is _base
      and bt._apply_overrides(_base, []) is _base)

_ov = bt._apply_overrides(_base, ["USE_VALUATION_FILTER=False", "MIN_PASS_GRADE=C", "MAX_PICKS=3"])
check("apply: 覆盖生效且未污染原配置",
      _ov.USE_VALUATION_FILTER is False and _ov.MIN_PASS_GRADE == "C" and _ov.MAX_PICKS == 3
      and _base.USE_VALUATION_FILTER is True and _base.MIN_PASS_GRADE == "B")

try:
    bt._apply_overrides(_base, ["NO_SUCH_FIELD=1"])
    check("apply: 未知字段报错而非静默忽略", False)
except SystemExit as e:
    check("apply: 未知字段报错而非静默忽略", "NO_SUCH_FIELD" in str(e))

check("apply: 值中含 = 时只在第一个 = 处切分",
      bt._apply_overrides(_base, ["MIN_PASS_GRADE=A=B"]).MIN_PASS_GRADE == "A=B")

# ===========================================================================
# 3) 报告渲染
# ===========================================================================
_metrics = {
    "n_formal": 12, "n_pending": 3, "n_trades": 10, "win_rate": 60.0,
    "avg_return": 3.21, "avg_peak": 5.4, "portfolio_return": 2.1,
    "max_drawdown": -1.8, "benchmark": 0.9,
    "veto_macd_weak": 7, "veto_kdj_high": 4, "n_days": 40,
}
_results = {"technical/baseline": dict(_metrics), "technical/no_kdj": dict(_metrics, n_formal=18)}
_report = bt.render_ab_report(bt.BacktestConfig(), _results)

check("报告: 含标题与回测区间", "# KDJ / MACD 闸门 A/B 对照" in _report and "2026-08-01" in _report)
check("报告: 每个变体各占一行且指标齐全",
      all(k in _report for k in ("| baseline |", "| no_kdj |", "12", "18", "60.0", "3.21")))
check("报告: 两个否决计数列都在（用于分辨差异是否来自闸门）",
      "MACD走弱否决" in _report and "KDJ高位否决" in _report)
check("报告: 必含样本量与非闸门差异的读表警示",
      "可成交" in _report and "不具统计意义" in _report and "若为 0" in _report)
check("报告: 无基准变体行时不报错",
      "benchmark" not in bt.render_ab_report(bt.BacktestConfig(), {"x/y": dict(_metrics, benchmark=None)}))
check("报告: 空结果不抛异常", isinstance(bt.render_ab_report(bt.BacktestConfig(), {}), str))

_report_ov = bt.render_ab_report(bt.BacktestConfig(), _results, ["USE_VALUATION_FILTER=False"])
check("报告: 覆盖环境参数时显式留痕（避免被当成实盘口径）",
      "非实盘默认口径" in _report_ov and "USE_VALUATION_FILTER=False" in _report_ov)
check("报告: 未覆盖时不出现该警示", "非实盘默认口径" not in _report)

# ===========================================================================
# 4) CLI 接线
# ===========================================================================
_parser = None
try:
    import argparse
    import inspect
    _src = inspect.getsource(bt.main)
    check("CLI: ab 子命令已注册且支持 --mode/--variants/--set",
          '"ab"' in _src and "--mode" in _src and "--variants" in _src and "--set" in _src)
    check("CLI: ab 与其他子命令互不干扰（不走 prefetch/run 分支）",
          'args.cmd == "ab"' in _src)
except Exception as e:  # noqa: BLE001
    check(f"CLI: 检查失败 {e}", False)

check("replay_core 已抽出（A/B 复用同一份面板的前提）", callable(getattr(bt, "replay_core", None)))

# ===========================================================================
# 汇总
# ===========================================================================
passed = sum(1 for _, ok in _RESULTS if ok)
print(f"\n{passed}/{len(_RESULTS)} 通过")
sys.exit(0 if passed == len(_RESULTS) else 1)
