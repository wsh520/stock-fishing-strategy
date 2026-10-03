"""P4（估值权重再分配）与两个坑的硬防护回归测试 —— 纯计算/合成数据，不联网。

本文件固化 2026-10 荐股策略优化第三轮的两件事：

1) **P4 估值权重再分配套件**（`P4_VALUATION_WEIGHT` / `P4_TECHNICAL_WEIGHT`）。
   背景实测（评估日 2026-06-04，n=168，前瞻窗口取真实后续行情，见 diag_pe_value.py）：
     · ρ(PE, valuation_score) = −1.0000 → **完全共线**，
       所以「给 PE 加一层独立排序权重」是重复计算，已被否决；
     · PE 是全部因子里唯一有区分度的：ρ = −0.44(F5) / −0.25(F10) / −0.07(F20)；
     · 但区分度只在 F5 显著、F20 衰减到噪声，且样本单一时点、处于下跌区间
       → **默认不上线**（`P4_*` 默认 None），只作为预注册套件供 A/B 使用。

2) **两个坑的硬防护**（本文件主体）：
   · 坑 1「综合分三项权重之和失衡」：现由 `_validate_score_weights` 在
     `__post_init__` 里**抛 ValueError**，并提供 P4 套件让质量权重自动补差。
   · 坑 2「抬质量硬门连带放松技术面」：由 `qv_floor_equivalence` 新增的
     `req_tech_anchor_only` + `describe_qv_floor` 的显式警告暴露出来。

设计原则沿用 test_stabilization_p0_p1.py：先锁「默认口径零回归」，
再逐条锁新口径的**性质**（而不是具体数值），最后固化「不上线」这一决策。
"""
import dataclasses
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from src import bottom_fishing_strategy as m
from src.volume_breakout_strategy import VolumeBreakoutConfig

# backtest.py 在模块顶层 import baostock；这里只取 AB_VARIANTS 这份纯数据，
# 导入失败时退化为空字典，让「变体已注册」那几条断言显式 FAIL 而不是整文件崩掉。
try:
    from backtest import AB_VARIANTS as _AB_VARIANTS
except Exception as _e:  # noqa: BLE001
    _AB_VARIANTS = {}
    print(f"[WARN] 无法导入 backtest.AB_VARIANTS（{_e}），变体注册断言将失败")

_RESULTS: list[tuple[str, bool]] = []


def ov_text(ov: dict) -> str:
    return "{" + ", ".join(f"{k}={v}" for k, v in ov.items()) + "}"


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, bool(ok)))


def w(c) -> tuple[float, float, float]:
    return (round(float(c.QUALITY_SCORE_WEIGHT), 6),
            round(float(c.VALUATION_SCORE_WEIGHT), 6),
            round(float(c.TECHNICAL_SCORE_WEIGHT), 6))


def total(c) -> float:
    q, v, t = w(c)
    return round(q + v + t, 6)


# ===========================================================================
# 1) 默认口径零回归：P4 未设置时，一切与本轮改动前完全一致
# ===========================================================================
print("\n=== 1) 默认口径零回归 ===")
_c0 = m.StrategyConfig()
check("默认权重仍为 0.45 / 0.30 / 0.25（与 P4 引入前一致）", w(_c0) == (0.45, 0.30, 0.25))
check("默认权重和为 1.0", total(_c0) == 1.0)
check("P4_VALUATION_WEIGHT 默认为 None（未启用）",
      _c0.P4_VALUATION_WEIGHT is None)
check("P4_TECHNICAL_WEIGHT 默认为 None（未启用）",
      _c0.P4_TECHNICAL_WEIGHT is None)
# 默认口径下 qv_floor_equivalence 的关键数字不得因本轮改动而漂移
_e0 = m.qv_floor_equivalence(_c0)
check(f"默认口径压线质量分仍为 50（评分锚点，未被质量硬门抬高）："
      f"{_e0['min_verified_quality']}",
      _e0["min_verified_quality"] == 50.0)
check(f"默认口径压线上限仍为 59.5：{_e0['ceiling_industry']}",
      abs(_e0["ceiling_industry"] - 59.5) < 1e-6)
check(f"默认口径过线所需技术分仍为 62：{_e0['req_tech_industry']:.2f}",
      abs(_e0["req_tech_industry"] - 62.0) < 1e-6)
check("默认口径 req_tech_anchor_only == req_tech_industry（无连带效应）",
      abs(_e0["req_tech_anchor_only"] - _e0["req_tech_industry"]) < 1e-9)
_d0 = m.describe_qv_floor(_c0)
check(f"默认口径 describe 不追加「连带放松技术面」警告（避免刷屏）",
      "连带放松技术面" not in _d0)

# ===========================================================================
# 2) 坑 1 防护：权重和 ≠ 1.0 必须快速失败，而不是静默错配
# ===========================================================================
print("\n=== 2) 坑 1 防护：权重和硬校验 ===")


def _raises(kw: dict) -> tuple[bool, str]:
    try:
        m.StrategyConfig(**kw)
    except ValueError as e:
        return True, str(e)
    return False, ""


_ok, _msg = _raises(dict(TECHNICAL_SCORE_WEIGHT=0.10))
check("单独把 TECHNICAL 降到 0.10（和=0.85）被拦截", _ok)
check(f"拦截信息点明真实和：{_msg.splitlines()[0] if _msg else ''}",
      _ok and "0.8500" in _msg)
check("拦截信息解释了「MIN_QV_SCORE 是绝对分数线」的根因",
      _ok and "绝对分数线" in _msg)
check("拦截信息给出两条修法（手动补差 / 用 P4 套件）",
      _ok and "P4" in _msg and "手动把差额补给另外两项" in _msg)

_ok2, _msg2 = _raises(dict(QUALITY_SCORE_WEIGHT=0.50, VALUATION_SCORE_WEIGHT=0.30,
                          TECHNICAL_SCORE_WEIGHT=0.30))
check("三项手动合计 1.10（>1）同样被拦截", _ok2 and "1.1000" in _msg2)

_ok3, _ = _raises(dict(QUALITY_SCORE_WEIGHT=0.0, VALUATION_SCORE_WEIGHT=0.0,
                      TECHNICAL_SCORE_WEIGHT=0.0))
check("权重全为 0（显式关闭综合分）被拦截，避免 MIN_QV_SCORE 失去意义", _ok3)

_ok4, _ = _raises(dict(QUALITY_SCORE_WEIGHT=0.55, VALUATION_SCORE_WEIGHT=0.35,
                      TECHNICAL_SCORE_WEIGHT=0.10))
check("手动补差到和=1.0（0.55/0.35/0.10）被放行（这正是错误信息里给的修法）", not _ok4)
_c_manual = m.StrategyConfig(QUALITY_SCORE_WEIGHT=0.55, VALUATION_SCORE_WEIGHT=0.35,
                             TECHNICAL_SCORE_WEIGHT=0.10)
check("放行后权重确为 0.55 / 0.35 / 0.10", w(_c_manual) == (0.55, 0.35, 0.10))

# 浮点容差：三项 1/3 各写 0.333333 应放行（1e-6 容差内）
_ok5, _ = _raises(dict(QUALITY_SCORE_WEIGHT=1 / 3, VALUATION_SCORE_WEIGHT=1 / 3,
                      TECHNICAL_SCORE_WEIGHT=1 / 3))
check("三项各 1/3（和≈1.0，浮点误差 3e-16）在容差内被放行", not _ok5)

# 复制 + 改字段（backtest.py 的 asdict 回灌路径）同样受校验保护
_c_copy = m.StrategyConfig()
try:
    _d = dataclasses.asdict(_c_copy)
    _d["TECHNICAL_SCORE_WEIGHT"] = 0.10
    m.StrategyConfig(**_d)
    _ok6 = False
except ValueError:
    _ok6 = True
check("asdict 回灌路径（backtest.py 常用）同样受校验保护", _ok6)

# ===========================================================================
# 3) P4 套件：质量权重自动补差，调用方不必心算
# ===========================================================================
print("\n=== 3) P4 预注册套件自动补差 ===")
_cases = [
    # (P4_VALUATION, P4_TECHNICAL, 期望的 质量/估值/技术)
    (0.45, 0.10, (0.45, 0.45, 0.10)),
    (0.40, 0.20, (0.40, 0.40, 0.20)),
    (0.50, 0.10, (0.40, 0.50, 0.10)),
    (0.40, 0.10, (0.50, 0.40, 0.10)),
    (0.35, 0.25, (0.40, 0.35, 0.25)),
]
for _pv, _pt, _exp in _cases:
    _c = m.StrategyConfig(P4_VALUATION_WEIGHT=_pv, P4_TECHNICAL_WEIGHT=_pt)
    check(f"P4(V={_pv}, T={_pt}) → 质量/估值/技术 = "
          f"{_exp[0]}/{_exp[1]}/{_exp[2]}，和={total(_c)}", w(_c) == _exp and total(_c) == 1.0)

# 4 个已注册到 backtest.py AB_VARIANTS 的 P4 变体，必须能被构造出来且和为 1.0
for _name in ("p4_v45_t10", "p4_v40_t20", "p4_v50_t10", "p4_v40_t10"):
    _ov = _AB_VARIANTS["quality_value"][_name]
    _c = m.StrategyConfig(**_ov)
    check(f"A/B 变体 {_name} {ov_text(_ov)} 构造成功且权重和=1.0（{total(_c)}）",
          total(_c) == 1.0)

# 单边设置：只给估值目标值，技术沿用当前值
_c_v_only = m.StrategyConfig(P4_VALUATION_WEIGHT=0.40)
check("只设 P4_VALUATION_WEIGHT=0.40 → 技术沿用 0.25，质量补差到 0.35",
      w(_c_v_only) == (0.35, 0.40, 0.25))
_c_t_only = m.StrategyConfig(P4_TECHNICAL_WEIGHT=0.10)
check("只设 P4_TECHNICAL_WEIGHT=0.10 → 估值沿用 0.30，质量补差到 0.60",
      w(_c_t_only) == (0.60, 0.30, 0.10))

# 无解（估值+技术 > 1）必须被拦截，而不是静默产生负质量权重
_ok7, _msg7 = _raises(dict(P4_VALUATION_WEIGHT=0.60, P4_TECHNICAL_WEIGHT=0.50))
check("P4 无解（V=0.60 + T=0.50 > 1）被拦截", _ok7 and "无解" in _msg7)
check(f"拦截信息给出负质量权重预览：{_msg7.splitlines()[0] if _msg7 else ''}",
      _ok7 and "-0.1000" in _msg7)
_ok8, _ = _raises(dict(P4_VALUATION_WEIGHT=0.60, P4_TECHNICAL_WEIGHT=0.40))
check("P4 恰好和为 1.0（质量=0）被放行（边界合法，质量权重不为负）", not _ok8)

# 字符串入参（A/B 的 _coerce_config_value 对 Optional[float] 会返回原字符串）
_c_str = m.StrategyConfig(P4_VALUATION_WEIGHT="0.45", P4_TECHNICAL_WEIGHT="0.10")
check("P4 接受字符串入参（backtest.py --set 路径）并正确转换为浮点",
      w(_c_str) == (0.45, 0.45, 0.10))

# P4 生效后审计数字应随之变化。注意方向：估值权重↑ 是从**技术权重**那里挪来的，
# 而 tw×100 这一项在压线上限里占的绝对值最大（0.25×100=25 → 0.10×100=10），
# 所以上限不升反降：59.5 → 50.5。两个数字指向同一个结论 —— P4 是**显著收紧**。
# 50.5 只比下限 50 高 0.5 分，意味着「三道硬闸门全部压线合格」的候选在 P4 下
# 几乎不可能靠综合分过线，MIN_QV_SCORE 开始接近成为主导闸门（记账式提醒）。
_e_p4 = m.qv_floor_equivalence(m.StrategyConfig(P4_VALUATION_WEIGHT=0.45,
                                              P4_TECHNICAL_WEIGHT=0.10))
check(f"P4(V=0.45,T=0.10) 压线上限从 59.5 降到 {_e_p4['ceiling_industry']}"
      f"（技术权重 0.25→0.10 使 tw×100 项从 25 降到 10）",
      abs(_e_p4["ceiling_industry"] - 50.5) < 1e-6)
check(f"P4 下限 50 距压线上限仅 {_e_p4['ceiling_industry'] - 50.0:.1f} 分"
      f"（MIN_QV_SCORE 接近成为主导闸门，上线前需重新审视分数线）",
      0 < _e_p4["ceiling_industry"] - 50.0 < 1.0)
check(f"P4 使过线所需技术分从 62 升到 {_e_p4['req_tech_industry']:.2f}"
      f"（技术面显著收紧 —— 这是 P4 的实际效果，不是副作用）",
      _e_p4["req_tech_industry"] > _e0["req_tech_industry"] + 20)

# ===========================================================================
# 4) 坑 2 防护：抬质量硬门连带放松技术面，必须被显式暴露
# ===========================================================================
print("\n=== 4) 坑 2 防护：抬质量硬门的连带效应可见 ===")
_e_q60 = m.qv_floor_equivalence(m.StrategyConfig(MIN_QUALITY_SCORE=60.0))
check(f"MIN_QUALITY_SCORE=60 时压线质量分跟随为 60：{_e_q60['min_verified_quality']}",
      _e_q60["min_verified_quality"] == 60.0)
check(f"req_tech_anchor_only 保持评分锚点 50 对应的 "
      f"{_e_q60['req_tech_anchor_only']:.0f}（不随硬门抬高）",
      abs(_e_q60["req_tech_anchor_only"] - _e0["req_tech_industry"]) < 1e-6)
_relax = _e_q60["req_tech_anchor_only"] - _e_q60["req_tech_industry"]
check(f"连带放松幅度可量化：{_relax:.0f} 分"
      f"（{_e_q60['req_tech_anchor_only']:.0f} → {_e_q60['req_tech_industry']:.0f}）",
      abs(_relax - 18.0) < 1e-6)
_d_q60 = m.describe_qv_floor(m.StrategyConfig(MIN_QUALITY_SCORE=60.0))
check("MIN_QUALITY_SCORE=60 时 describe 显式打出「连带放松技术面」警告",
      "连带放松技术面" in _d_q60)
check("警告文案点明「本项非单纯收紧」", "非单纯收紧" in _d_q60)
check("警告文案提示要同时收紧技术须配技术分降权", "技术分降权" in _d_q60)
print(f"    日志样例：{_d_q60}")

# P2 + P3 组合：质量门 60 + 技术分降权到 0.10（手动补差保持和=1.0）
_e_p2p3 = m.qv_floor_equivalence(m.StrategyConfig(
    MIN_QUALITY_SCORE=60.0, QUALITY_SCORE_WEIGHT=0.55,
    VALUATION_SCORE_WEIGHT=0.35, TECHNICAL_SCORE_WEIGHT=0.10))
_relax2 = _e_p2p3["req_tech_anchor_only"] - _e_p2p3["req_tech_industry"]
check(f"P2+P3 组合的放松幅度 {(_e_p2p3['req_tech_anchor_only'] - _e_p2p3['req_tech_industry']):.0f} 分"
      f"> 单独 P2 的 {_relax:.0f} 分（技术降权放大了连带效应）", _relax2 > _relax)
_d_p2p3 = m.describe_qv_floor(m.StrategyConfig(
    MIN_QUALITY_SCORE=60.0, QUALITY_SCORE_WEIGHT=0.55,
    VALUATION_SCORE_WEIGHT=0.35, TECHNICAL_SCORE_WEIGHT=0.10))
check("P2+P3 组合同样打出警告", "连带放松技术面" in _d_p2p3)

# 技术权重为 0 时不能算出「所需技术分」，需优雅降级
_e_t0 = m.qv_floor_equivalence(m.StrategyConfig(QUALITY_SCORE_WEIGHT=0.70,
                                              VALUATION_SCORE_WEIGHT=0.30,
                                              TECHNICAL_SCORE_WEIGHT=0.0))
check("技术权重=0 时 req_tech 为 None（不可达/不适用）",
      _e_t0["req_tech_industry"] is None)
check("技术权重=0 时 req_tech_anchor_only 也为 None（不误报连带效应）",
      _e_t0["req_tech_anchor_only"] is None)
_d_t0 = m.describe_qv_floor(m.StrategyConfig(QUALITY_SCORE_WEIGHT=0.70,
                                            VALUATION_SCORE_WEIGHT=0.30,
                                            TECHNICAL_SCORE_WEIGHT=0.0))
check("技术权重=0 时 describe 降级为「技术分权重为 0」且不误报警告",
      "技术分权重为 0" in _d_t0 and "连带放松技术面" not in _d_t0)

# ===========================================================================
# 5) 继承链不回归：VolumeBreakoutConfig(asdict(config)) 仍能构造
# ===========================================================================
print("\n=== 5) 继承链不回归 ===")
try:
    _cb = VolumeBreakoutConfig(**dataclasses.asdict(m.StrategyConfig()))
    _ok9 = total(_cb) == 1.0
except Exception as _e:  # noqa: BLE001
    _ok9, _cb = False, None
    print(f"    异常：{_e}")
check("VolumeBreakoutConfig(**asdict(StrategyConfig())) 构造成功且权重和=1.0", _ok9)
if _cb is not None:
    check("VolumeBreakoutConfig 继承 P4 字段（默认 None，未启用）",
          getattr(_cb, "P4_VALUATION_WEIGHT", None) is None)
    check("VolumeBreakoutConfig 权重与父类一致（0.45/0.30/0.25）",
          w(_cb) == (0.45, 0.30, 0.25))

# ===========================================================================
# 6) 上线决策固化：P4 当前**不建议**开启（区分度只在 F5 显著，F20 衰减到噪声）
# ===========================================================================
print("\n=== 6) 上线决策固化 ===")
# 评估日 2026-06-04、n=168、前瞻窗口取真实后续行情（非历史价）的实测结论：
#   · ρ(PE, valuation_score) = −1.0000 → 加独立排序权重= 重复计算，已否决；
#   · ρ(PE, 未来收益) = −0.44(F5) / −0.25(F10) / −0.07(F20) / −0.09(F60)；
#   · Top15 权重敏感性：现状(0.45/0.30/0.25) F5=+1.67 / F20=−3.01；
#     提高估值权重(0.45/0.45/0.10) F5=+2.37 / F20=−3.33 —— 短窗改善、长窗略差；
#   · 样本仅 168 只、单一时点、处于全市场下跌区间，无法区分
#     「便宜股真的更好」与「超跌反弹」。
# 本节把「默认不上线」固化，避免后人看到有实现就顺手翻开默认值。
_cf = m.StrategyConfig()
check("决策: P4_VALUATION_WEIGHT 默认 None（区分度未证明，不上线）",
      _cf.P4_VALUATION_WEIGHT is None)
check("决策: P4_TECHNICAL_WEIGHT 默认 None（同上）", _cf.P4_TECHNICAL_WEIGHT is None)
check("决策: 实际权重默认仍为 0.45 / 0.30 / 0.25", w(_cf) == (0.45, 0.30, 0.25))
check("决策: 不存在「PE 独立排序权重」配置项（ρ=-1.0 共线，加了是重复计算）",
      not any("PE" in f.name.upper() and "SORT" in f.name.upper()
              or "PE_RANK" in f.name.upper()
              for f in dataclasses.fields(m.StrategyConfig)))
check("决策: 4 个 P4 A/B 变体已注册（供 backtest.py ab --variants 使用）",
      all(n in _AB_VARIANTS["quality_value"]
          for n in ("p4_v45_t10", "p4_v40_t20", "p4_v50_t10", "p4_v40_t10")))

_ok = sum(1 for _, ok in _RESULTS if ok)
print(f"\n{_ok}/{len(_RESULTS)} 通过")
if _ok != len(_RESULTS):
    raise SystemExit(1)
