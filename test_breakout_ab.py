"""突破策略 KDJ/MACD 闸门 A/B 与漏斗诊断的离线测试。

覆盖 src/volume_breakout_backtest.py 新增的 GATE_VARIANTS / _technical_cfg /
funnel_counts / gate_ab / render_gate_ab 与 --funnel / --ab-gates CLI。

同时**显式固化**一个已发现的既有缺陷（见最后一节）：evaluate_breakout 从不把
Signal.tier 从默认 "pending" 提升为 "formal"，而 _candidate_events 只收
tier=="formal"，导致该独立回测模块在任何输入下都不可能成交。
不修它，是因为修它会改变行为，而本项目约定「改产线口径前先 A/B」。
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import volume_breakout_backtest as vbbt
from src.volume_breakout_strategy import VolumeBreakoutConfig, evaluate_breakout

_RESULTS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, bool(ok)))


def breakout_df(n: int = 100, breakout_close: float = 10.25) -> pd.DataFrame:
    """平台 + 末根放量突破 20 日新高（与 test_momentum_gates.brk_df 同构，已验证 PASS）。"""
    closes = np.full(n, 10.0)
    closes[45:50] = 11.0
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs, lows = closes * 1.005, closes * 0.995
    volume = np.full(n, 12_000_000.0)
    opens[-1] = 10.02
    closes[-1] = breakout_close
    highs[-1] = breakout_close * 1.005
    lows[-1] = 10.0
    volume[-1] = 30_000_000.0
    return pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volume, "amount": closes * volume,
        "pct_chg": (pd.Series(closes).pct_change().fillna(0) * 100).values,
    })


DF = breakout_df()

# ===========================================================================
# 1) GATE_VARIANTS 与 _technical_cfg
# ===========================================================================
check("GATE_VARIANTS: 含 gates_on / gates_off",
      {"gates_on", "gates_off"} <= set(vbbt.GATE_VARIANTS))
check("GATE_VARIANTS: gates_on 为空（代表生产默认，不覆盖任何字段）",
      vbbt.GATE_VARIANTS["gates_on"] == {})
check("GATE_VARIANTS: gates_off 同时关掉两个闸门",
      vbbt.GATE_VARIANTS["gates_off"].get("REQUIRE_BR_KDJ_NOT_HIGH") is False
      and vbbt.GATE_VARIANTS["gates_off"].get("REQUIRE_BR_MACD_NOT_WEAK") is False)

_valid_fields = set(vars(VolumeBreakoutConfig()).keys())
_all_keys = {k for ov in vbbt.GATE_VARIANTS.values() for k in ov}
check(f"GATE_VARIANTS: 覆盖字段全部是 VolumeBreakoutConfig 的真实字段 {sorted(_all_keys)}",
      _all_keys <= _valid_fields)

check("_technical_cfg: 强制 RECOMMENDATION_MODE=technical",
      vbbt._technical_cfg().RECOMMENDATION_MODE == "technical")
check("_technical_cfg: 默认两个闸门都开（生产默认口径）",
      vbbt._technical_cfg().REQUIRE_BR_KDJ_NOT_HIGH is True
      and vbbt._technical_cfg().REQUIRE_BR_MACD_NOT_WEAK is True)
check("_technical_cfg: 可单独翻转一个开关",
      vbbt._technical_cfg({"REQUIRE_BR_KDJ_NOT_HIGH": False}).REQUIRE_BR_KDJ_NOT_HIGH is False
      and vbbt._technical_cfg({"REQUIRE_BR_KDJ_NOT_HIGH": False}).REQUIRE_BR_MACD_NOT_WEAK is True)
check("_technical_cfg: 不污染类默认值",
      VolumeBreakoutConfig().RECOMMENDATION_MODE == "quality_value")

# ===========================================================================
# 2) funnel_counts
# ===========================================================================
reasons = vbbt.funnel_counts({"600000": DF}, regime="bull", tail_bars=10 ** 6)
# 评估窗口 = MIN_DAYS-1 .. len-2（末根不评估，因为需要「次日」可成交）
_expected_evals = len(DF) - VolumeBreakoutConfig().MIN_DAYS
check(f"funnel_counts: 评估次数 = {_expected_evals}（面板 {len(DF)} 根 - MIN_DAYS）",
      sum(reasons.values()) == _expected_evals)
check("funnel_counts: 计数全部落在 FAIL_*/PASS 归因码上",
      all(r == "PASS" or str(r).startswith("FAIL_") for r in reasons))
check("funnel_counts: 平台期绝大多数日子不构成突破（FAIL_NO_BREAKOUT 占多数）",
      reasons.get("FAIL_NO_BREAKOUT", 0) > _expected_evals * 0.7)
check("funnel_counts: 空输入返回空 Counter",
      len(vbbt.funnel_counts({}, regime="bull")) == 0)

# ===========================================================================
# 3) gate_ab
# ===========================================================================
rows = vbbt.gate_ab({"600000": DF}, vbbt.BacktestConfig(regime="bull"), tail_bars=10 ** 6)
check("gate_ab: 默认跑全部 4 个变体",
      set(rows) == set(vbbt.GATE_VARIANTS))
check("gate_ab: 每个变体都返回 run_backtest 的统计字段",
      all({"signals", "trades", "win_rate_pct", "return_pct", "pnl", "final_equity"} <= set(s)
          for s in rows.values()))
check("gate_ab: 可只跑指定变体",
      set(vbbt.gate_ab({"600000": DF}, tail_bars=10 ** 6, variants=["gates_on", "gates_off"]))
      == {"gates_on", "gates_off"})

try:
    vbbt.gate_ab({"600000": DF}, variants=["nope"])
    _raised = False
except ValueError:
    _raised = True
check("gate_ab: 未知变体抛 ValueError", _raised)

# ===========================================================================
# 4) render_gate_ab
# ===========================================================================
md = vbbt.render_gate_ab(rows, data_note="1 只 × 100 根")
check("render_gate_ab: 表格包含全部变体名",
      all(name in md for name in vbbt.GATE_VARIANTS))
check("render_gate_ab: 含「全为 0 时不可用来判断闸门有效性」的口径提示",
      "不能" in md and "漏斗" in md)
check("render_gate_ab: 声明 technical 口径（quality_value 下闸门不参与资格）",
      "technical" in md and "quality_value" in md)
check("render_gate_ab: 样本说明透传", "1 只 × 100 根" in md)

# ===========================================================================
# 5) CLI
# ===========================================================================
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    DF.to_csv(tdp / "600000.csv", index=False)
    # 预取会为无数据标的写一个只含 date 列的空标记文件
    (tdp / "999999.csv").write_text("date\r\n", encoding="utf-8")

    got = vbbt.load_price_files(td)
    check("load_price_files: 目录级加载跳过空标记文件（不整体失败）", set(got) == {"600000"})

    bad = tdp / "bad.csv"
    bad.write_text("date\r\n", encoding="utf-8")
    try:
        vbbt.load_price_files(str(bad))
        _raised2 = False
    except Exception:
        _raised2 = True
    check("load_price_files: 显式指定坏文件仍然报错（不静默返回空）", _raised2)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vbbt.main(["--input", td, "--ab-gates", "--max-stocks", "1", "--tail-bars", "100",
                   "--regime", "bull"])
    out = buf.getvalue()
    check("CLI --ab-gates（不带值）: 打印全部变体对照表",
          all(name in out for name in vbbt.GATE_VARIANTS) and "#" in out)

    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        vbbt.main(["--input", td, "--funnel", "--max-stocks", "1", "--tail-bars", "100"])
    out2 = buf2.getvalue()
    check("CLI --funnel: 打印否决原因分布", "[漏斗]" in out2 and "FAIL_" in out2)

    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        vbbt.main(["--input", td, "--max-stocks", "1", "--tail-bars", "100"])
    check("CLI 默认: 保持原有的 JSON 回测输出（无回归）", '"summary"' in buf3.getvalue())

_buf4 = io.StringIO()
try:
    with contextlib.redirect_stdout(_buf4):
        vbbt.main(["--help"])
except SystemExit:
    pass
_help = _buf4.getvalue()
check("CLI: --funnel / --ab-gates / --max-stocks / --tail-bars 均已注册",
      all(f in _help for f in ("--funnel", "--ab-gates", "--max-stocks", "--tail-bars")))

# ===========================================================================
# 6) 已知缺陷固化：独立突破回测恒不成交
# ===========================================================================
sig, reason = evaluate_breakout(DF, code="600000", name="测试",
                                config=vbbt._technical_cfg(), market_env={"regime": "bull"},
                                fund_data={"roe": 12.0, "debt_ratio": 45.0},
                                latest_trade_date="2025-04-10")
check(f"缺陷固化: 完美 L2 突破形态 evaluate_breakout 判 PASS（reason={reason}）",
      reason == "PASS" and sig is not None)
check(f"缺陷固化: 但 tier 仍为 Signal 默认值 'pending'（策略从不提升为 formal）"
      f"，实际 tier={getattr(sig, 'tier', None)}",
      getattr(sig, "tier", None) == "pending")

_events = vbbt._candidate_events({"600000": DF}, vbbt._technical_cfg(), "bull")
check(f"缺陷固化: _candidate_events 收 0 个事件（只收 tier=='formal'）→ 该模块恒不成交，"
      f"实际收 {len(_events)} 个",
      len(_events) == 0)

_sum, _tr = vbbt.run_backtest({"600000": DF}, vbbt.BacktestConfig(regime="bull"),
                              vbbt._technical_cfg())
check(f"缺陷固化: run_backtest 信号/成交均为 0（signals={_sum['signals']}, "
      f"trades={_sum['trades']}）",
      _sum["signals"] == 0 and _sum["trades"] == 0)

# ===========================================================================
# 汇总
# ===========================================================================
_passed = sum(1 for _l, ok in _RESULTS if ok)
print(f"\n{_passed}/{len(_RESULTS)} 通过")
sys.exit(0 if _passed == len(_RESULTS) else 1)
