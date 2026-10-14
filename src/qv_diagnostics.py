"""Read-only quality-value gate telemetry; no eligibility decisions live here."""
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

_active = ContextVar("qv_gate_audit", default=None)


def mark_gate(code):
    """Record entry into a check (not proof that every subcondition was evaluated)."""
    active = _active.get()
    if active is not None:
        active.add(code)


class GateAudit:
    """Per-run, per-stock union across the cheap and enriched evaluator passes."""
    def __init__(self):
        self._lock = Lock()
        self._entered = Counter()
        self._completed = 0

    @contextmanager
    def stock(self):
        entered = set()
        token = _active.set(entered)
        try:
            yield
        finally:
            _active.reset(token)
            with self._lock:
                self._entered.update(entered)
                self._completed += 1

    def snapshot(self):
        with self._lock:
            return self._completed, dict(self._entered)


def gate_mode(code, config):
    """Configuration applicability; never infer unused gates from zero rejects."""
    not_applicable = {
        "FAIL_ACCELERATING_DECLINE", "FAIL_ROE_STABILITY_INSUFFICIENT",
        "FAIL_ROE_UNSTABLE", "FAIL_EARNINGS_INCONSISTENT",
        "FAIL_EARNINGS_DATA_INSUFFICIENT", "FAIL_LONGTERM_CASH_INSUFFICIENT",
        "FAIL_LONGTERM_CASH_POOR", "FAIL_LONGTERM_QUALITY_SCORE",
        "FAIL_RELATIVE_STRENGTH_WEAK",
    }
    if code == "ERROR":
        return "运行异常（非选股闸门）"
    if code in not_applicable:
        return "本策略不适用"
    enabled = {
        "FAIL_HALT_GAP": getattr(config, "REQUIRE_NO_HALT_GAP", True),
        "FAIL_FORWARD": getattr(config, "REQUIRE_FORWARD_CONFIRMATION", False)
            and not getattr(config, "REQUIRE_RECENT_OPERATING", True),
        "FAIL_RECENT_FUNDAMENTAL": getattr(config, "REQUIRE_RECENT_OPERATING", True),
        "FAIL_FINANCIAL_RISK": getattr(config, "REQUIRE_FINANCIAL_RISK", True),
        "FAIL_HISTORICAL_VALUATION": getattr(config, "REQUIRE_HISTORICAL_VALUATION", True),
        "FAIL_CYCLICAL_VALUATION": getattr(config, "REQUIRE_CYCLICAL_NORMALIZED_VALUATION", True),
        "FAIL_STABILIZATION": getattr(config, "QV_STABILIZATION_GATE", True)
            and getattr(config, "QV_STABILIZATION_MODE", "strict") != "disabled",
        "FAIL_MACD_WEAK": getattr(config, "QV_ENFORCE_KDJ_MACD_VETO", False),
        "FAIL_KDJ_HIGH": getattr(config, "QV_ENFORCE_KDJ_MACD_VETO", False),
        "FAIL_QV_SCORE": float(getattr(config, "MIN_QV_SCORE", 0) or 0) > 0,
        "FAIL_QUALITY_FLOOR": float(getattr(config, "MIN_QUALITY_SCORE", 0) or 0) > 0,
        "FAIL_VOLATILE": getattr(config, "QV_ATR_GUARD", True),
        "FAIL_OVEREXTENDED": getattr(config, "QV_STABILIZATION_GATE", True)
            and getattr(config, "QV_STABILIZATION_MODE", "strict") != "disabled"
            and (getattr(config, "QV_LOW_ANCHOR_MODE", "recent_structure") == "recent_structure"
                 or float(getattr(config, "QV_MAX_GAIN_FROM_250D_LOW", 0) or 0) > 0),
    }
    return "启用" if enabled.get(code, True) else "未启用"


def log_gate_audit(log, config, reason_codes, reasons, audit):
    completed, entered = audit.snapshot()
    log.info("[QV_GATE_AUDIT] 按股票合并预筛/财务复筛；进入检查不等于全部子条件完成；"
             "未进入可能由前置返回、证据/行业条件不满足造成，不推断独立贡献；审计完成=%d", completed)
    for code in reason_codes:
        mode = gate_mode(code, config)
        hits = reasons.get(code, 0)
        count = entered.get(code, 0)
        if mode == "启用":
            state = "已进入检查" if count else "未进入（前置或适用条件阻断）"
            if count and not hits:
                state += "·未否决"
        else:
            state = mode
        log.info("[QV_GATE_AUDIT] %s | 状态=%s | 进入股票数=%d | 首因否决数=%d",
                 code, state, count, hits)
