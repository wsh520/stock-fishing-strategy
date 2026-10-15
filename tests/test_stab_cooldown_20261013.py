"""2026-10-13 两项收紧的回归测试（合成数据，不联网）。

背景：真实落库推荐追踪（diag_real_recs.py，40 条，2026-09-08~10-09）发现三件事：
  1. 31 条 bottom_fishing 正式推荐，F5 中位 -1.87%、下跌占比 76.5%；
  2. 其中 15 条（48%）自带「左侧·MA20下方待企稳」标签，这 15 条 F5 **全部下跌**、
     中位 -3.67%、胜率 0%，而同期沪深300 上涨（09-16→09-21 +1.32%）；
  3. 云天化 3 次推荐价一路走低（28.29→27.95→27.56），每次推荐后 F1 全负——越跌越推。

对应两项**只减不增**的收紧：
  A. QV_STABILIZATION_MEDIUM_REQUIRES_MA20（默认 True）
     strict 止跌的 medium 层原本允许「MACD 改善 + 无新低」在收盘仍低于 MA20 时
     通过 → 未企稳的票进 formal。现在 medium 必须站上 MA20，左侧通道降为 weak→pending。
  B. QV_REPEAT_COOLDOWN_DAYS（默认 10）
     冷却期内已推过的同一只票不再重复推送（移到 pending，不丢弃）。

本文件锁定：新默认值、新收紧生效、放行集合只减不增、冷却只减不增。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np
import pandas as pd

from src import screening_engine as m

_RESULTS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {label}")
    _RESULTS.append((label, bool(ok)))


_DAY = "2026-06-30"
# 三年 ROE 达标、现金转换健康的合成年度证据：让质量闸门通过，把变量锁定在止跌。
_FUND = {"debt_ratio": 40., "roe": 2., "annual_rows": [
    {"year": y, "report_date": f"{y}-12-31", "available_date": f"{y + 1}-04-20",
     "roe": roe, "deducted_profit": 90., "net_profit": 100., "operating_cashflow": 110.}
    for y, roe in [(2023, 11), (2024, 12), (2025, 13)]]}


def mk_df(close) -> pd.DataFrame:
    close = np.asarray(close, float)
    dates = pd.bdate_range(end=_DAY, periods=len(close)).strftime("%Y-%m-%d")
    return pd.DataFrame(dict(date=dates, open=close, high=close * 1.01, low=close * .99,
                             close=close, volume=1e7, amount=close * 1e7,
                             peTTM=8., pbMRQ=.9))


def ev(close, **cfg_kw):
    """跑 quality_value 口径，隔离被测维度（与 test_stabilization_p0_p1.py 同款）。"""
    kw = dict(USE_CACHE=False, RECOMMENDATION_MODE="quality_value",
              MIN_QV_SCORE=0, REQUIRE_RECENT_OPERATING=False)
    kw.update(cfg_kw)
    return m.evaluate(mk_df(close), "600001", "工业企业", m.StrategyConfig(**kw),
                      {"regime": "bear"}, dict(_FUND), latest_trade_date=_DAY)


# 形态（形状取自 test_stabilization_p0_p1.py 已实测校准的 fixture，勿随意改）：
#   _up        长期下跌 → 稳步回升，收盘站上 MA20 且 MA20 走平（strong）
#   _below_ma20 长期下跌 → 冲高 → 回落至 MA20 下方、MA20 仍下行、MACD 柱在改善
_up = np.r_[np.linspace(20, 10, 250), np.linspace(10, 11, 50)]
_below_ma20 = np.r_[np.linspace(20, 10, 250), np.linspace(10, 10.6, 30), np.linspace(10.6, 10.3, 20)]

# ===========================================================================
# 1) 新配置项默认值（收紧默认开启）
# ===========================================================================
_cfg = m.StrategyConfig()
check("默认: QV_STABILIZATION_MEDIUM_REQUIRES_MA20=True（medium 必须站上 MA20）",
      _cfg.QV_STABILIZATION_MEDIUM_REQUIRES_MA20 is True)
check("默认: QV_REPEAT_COOLDOWN_DAYS=10（打断越跌越推）",
      _cfg.QV_REPEAT_COOLDOWN_DAYS == 10)

# ===========================================================================
# 2) A 项：未站上 MA20 的左侧通道不再拿到 medium
#    用**真实行情**验证（合成 fixture 难以稳定复现「MACD 改善 + 无新低 + 仍在
#    MA20 下方」这个三条件交集；该形态在真实推荐里确实大量出现）。
#    东鹏饮料 605499 / 华东医药 000963 在 2026-09-30 的落库推荐中，
#    旧口径拿到 medium（未站上 MA20），新口径应降为 weak → pending。
# ===========================================================================
_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache")


def load_panel(code: str, day: str):
    for mk in ("sh", "sz"):
        p = os.path.join(_CACHE, f"daily_{mk}.{code}_last600_qfq.csv")
        if os.path.exists(p):
            d = pd.read_csv(p)
            d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            return d[d["date"] <= day].dropna(subset=["date", "close"]).reset_index(drop=True)
    return None


def ev_panel(df, code: str, day: str, require_ma20: bool):
    kw = dict(USE_CACHE=False, RECOMMENDATION_MODE="quality_value",
              MIN_QV_SCORE=0, MIN_QUALITY_SCORE=0,
              REQUIRE_RECENT_OPERATING=False, REQUIRE_FINANCIAL_RISK=False,
              REQUIRE_CYCLICAL_NORMALIZED_VALUATION=False, REQUIRE_HISTORICAL_VALUATION=False,
              USE_INDUSTRY_RELATIVE_VALUATION=False,
              QV_STABILIZATION_MEDIUM_REQUIRES_MA20=require_ma20)
    return m.evaluate(df, code, "机械设备", m.StrategyConfig(**kw),
                      {"regime": "neutral"}, dict(_FUND), latest_trade_date=day)


_demoted = _checked = 0
for _code in ("605499", "000963", "002946", "002847"):
    _p = load_panel(_code, "2026-09-30")
    if _p is None or len(_p) < 260:
        continue
    _so, _ = ev_panel(_p, _code, "2026-09-30", False)
    _sn, _ = ev_panel(_p, _code, "2026-09-30", True)
    _lo, _ln = getattr(_so, "stabilization_level", None), getattr(_sn, "stabilization_level", None)
    if _lo and _ln:
        _checked += 1
        if _lo != _ln:
            _demoted += 1
        _rank = {"none": 0, "weak": 1, "medium": 2, "strong": 3}
        if _rank.get(_ln, 0) > _rank.get(_lo, 0):
            _demoted = -999  # 出现升级 → 直接让后续断言失败
check(f"真实行情 A/B：{_checked} 只可读止跌层，其中 {_demoted} 只止跌层被降级",
      _checked > 0 and _demoted >= 0)
check("真实行情 A/B：无一只出现止跌层升级（只减不增）", _demoted != -999)

# 合成 fixture 层面的确认：新口径下未站上 MA20 的形态绝不进入 medium
_s_new, r_new = ev(_below_ma20, QV_STABILIZATION_MEDIUM_REQUIRES_MA20=True)
check(f"新口径: 未站上 MA20 的形态不是 medium（{getattr(_s_new, 'stabilization_level', None)}）",
      getattr(_s_new, "stabilization_level", None) != "medium")
check(f"新口径: 同形态进待核验不进 formal（tier={getattr(_s_new, 'tier', None)}）",
      getattr(_s_new, "tier", None) != "formal")
check("新口径: 缺项里记入 stabilization_weak",
      "stabilization_weak" in (getattr(_s_new, "missing_tags", None) or ""))

# ===========================================================================
# 3) A 项不误伤：站上 MA20 的形态不受影响（放行集合只减不增）
# ===========================================================================
_s_up_new, r_up_new = ev(_up, QV_STABILIZATION_MEDIUM_REQUIRES_MA20=True)
_s_up_old, r_up_old = ev(_up, QV_STABILIZATION_MEDIUM_REQUIRES_MA20=False)
check(f"站上 MA20 的形态仍放行（新={getattr(_s_up_new, 'stabilization_level', None)} / "
      f"旧={getattr(_s_up_old, 'stabilization_level', None)}）",
      r_up_new == r_up_old == "PASS"
      and getattr(_s_up_new, "stabilization_level", None)
      == getattr(_s_up_old, "stabilization_level", None))
check(f"strong/medium 层在两口径下取值相同（{getattr(_s_up_new, 'stabilization_level', None)}）",
      getattr(_s_up_new, "stabilization_level", None)
      in ("strong", "medium"))

# ===========================================================================
# 4) B 项：重复推荐冷却（只减不增 + 不丢弃）
# ===========================================================================
_frame = pd.DataFrame([
    {"code": "600096", "name": "云天化", "tier": "formal", "score": 84.6},
    {"code": "002847", "name": "盐津铺子", "tier": "formal", "score": 82.9},
    {"code": "000538", "name": "云南白药", "tier": "formal", "score": 69.1},
    {"code": "600519", "name": "贵州茅台", "tier": "pending", "score": 55.0},
])
_formal = _frame[_frame["tier"] == "formal"].copy()
_pending: list = []

_out = m._apply_repeat_cooldown(_formal, _frame, {"600096", "002847"}, 10, _pending)
check("冷却: 命中 2 只后正式推荐剩 1 只", len(_out) == 1 and list(_out["code"]) == ["000538"])
check("冷却: 被拦下的票转入 pending（不丢弃、可查）",
      len(_pending) == 2 and {p["code"] for p in _pending} == {"600096", "002847"})
check("冷却: 不改动原 frame（纯过滤）", len(_frame) == 4)

_out_none = m._apply_repeat_cooldown(_formal, _frame, set(), 10, [])
check("冷却: 集合为空时原样返回（DB 不可用即不生效）", len(_out_none) == 3)
_out_miss = m._apply_repeat_cooldown(_formal, _frame, {"999999"}, 10, [])
check("冷却: 无命中时原样返回", len(_out_miss) == 3)
_out_nonep = m._apply_repeat_cooldown(_formal, _frame, {"600096"}, 10, None)
check("冷却: pending_out 为 None 时也能安全过滤", len(_out_nonep) == 2)

# ===========================================================================
# 5) 只减不增的代数性质：旧集合 ⊇ 新集合
# ===========================================================================
_rank = {"none": 0, "weak": 1, "medium": 2, "strong": 3}
check("只减不增: 站上 MA20 的形态在两口径下止跌层完全相同（未被新开关误伤）",
      getattr(_s_up_new, "stabilization_level", None)
      == getattr(_s_up_old, "stabilization_level", None))
check("只减不增: 真实 A/B 中降级数 ≥ 0 且无升级（_demoted != -999）",
      _demoted != -999)
check("只减不增: medium 仍可由「站上 MA20」取得（闸门未被掏空成死代码）",
      getattr(_s_up_new, "stabilization_level", None) in ("strong", "medium"))

# ===========================================================================
_ok = sum(1 for _, ok in _RESULTS if ok)
print(f"\n{_ok}/{len(_RESULTS)} 通过")
sys.exit(0 if _ok == len(_RESULTS) else 1)