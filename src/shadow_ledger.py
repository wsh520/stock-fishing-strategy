"""影子账本：记录未进入正式推荐的 pending / near-miss 候选，供前瞻评估。

设计边界（2026-10-19，P1）：
- **不改准入**：只读取 `_screen_quality_pool` 已经产出的 `pending_rows`，不参与
  闸门、排序、通知或正式推荐落库。
- **不写 MySQL 正式表**：正式推荐仍只走 `stock_recommendation`；影子记录写
  JSONL（cache/ 持久化 + data/ 作为 CI artifact），避免污染唯一键/周跟踪。
- **可复现**：同一 (rec_date, code) 幂等，重复运行只更新最后见到的证据；
  `first_seen_at` / `seen_count` 保留，便于审计“同一天跑了几次”。
- **为前瞻证据服务**：`run_shadow_tracking.py` 读取本账本，按市场交易日计算
  F5/F10/F15/F20/F40/F60 收益；只写诊断文件，不推送飞书。

为什么需要它：项目原则是“先证据后阈值”，但 pending 不落库、周跟踪只跟 formal，
near-miss 永远拿不到前瞻证据。影子账本把这条证据链补上。
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

logger = logging.getLogger("shadow")

SCHEMA_VERSION = 1
NEAR_MISS_MAX_BLOCKERS = 2
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BEIJING = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(_BEIJING).isoformat()


def _clean_number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean_text(value, limit: int = 2000) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    return text[:limit]


def _code(value) -> Optional[str]:
    text = _clean_text(value, 16)
    if text is None:
        return None
    # pandas 可能把纯数字代码读成 600066.0；先去掉浮点尾巴，再抽取数字。
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(6) if len(digits) <= 6 else digits[-6:]


def _record_key(record: dict) -> tuple[str, str]:
    return str(record.get("rec_date") or "")[:10], str(record.get("code") or "")


def _tags(value) -> list[str]:
    return list(dict.fromkeys(
        text.strip() for text in str(value or "").replace("，", ",").split(",") if text.strip()
    ))


def _blockers(row: dict, config=None) -> list[str]:
    tags = _tags(row.get("missing_tags"))
    dual_guard = bool(getattr(config, "QV_VALUATION_DUAL_GUARD", False)) if config is not None else False
    blockers = [tag for tag in tags if dual_guard or tag != "valuation_pb"]
    return blockers or ["pending_reason_unknown"]


def _formal_codes(formal_df) -> set[str]:
    if formal_df is None or getattr(formal_df, "empty", True) or "code" not in formal_df:
        return set()
    return {str(code).zfill(6) for code in formal_df["code"].tolist() if str(code).strip()}


def build_shadow_record(row: dict, *, strategy: str = "bottom_fishing", config=None,
                        formal_codes: Optional[set[str]] = None) -> Optional[dict]:
    """把一行 pending 结果转成可持久化的影子记录；非 pending/缺日期/缺代码返回 None。"""
    if not isinstance(row, dict):
        return None
    tier = str(row.get("tier") or "pending").strip().lower()
    if tier == "formal":
        return None
    rec_date = str(row.get("date") or "")[:10]
    code = _code(row.get("code"))
    if not rec_date or code is None:
        return None
    blockers = _blockers(row, config)
    formal_codes = formal_codes or set()
    now = _now()
    record = {
        "schema": SCHEMA_VERSION,
        "rec_date": rec_date,
        "code": code,
        "name": _clean_text(row.get("name"), 80),
        "strategy": strategy,
        "tier": tier or "pending",
        "delivery_status": _clean_text(row.get("delivery_status"), 40) or "pending",
        "qualification_status": _clean_text(row.get("qualification_status"), 40),
        "score": _clean_number(row.get("score")),
        "rank_score": _clean_number(row.get("rank_score")),
        "daily_score": _clean_number(row.get("daily_score")),
        "close": _clean_number(row.get("close")),
        "pe_ttm": _clean_number(row.get("pe_ttm")),
        "pb_mrq": _clean_number(row.get("pb_mrq")),
        "position_250": _clean_number(row.get("position_250")),
        "stabilization_level": _clean_text(row.get("stabilization_level"), 40),
        "quality_status": _clean_text(row.get("quality_status"), 40),
        "financial_risk_status": _clean_text(row.get("financial_risk_status"), 40),
        "operating_status": _clean_text(row.get("operating_status"), 40),
        "operating_summary": _clean_text(row.get("operating_summary"), 1200),
        "missing_tags": _clean_text(row.get("missing_tags"), 500) or "",
        "blockers": blockers,
        "blocker_count": len(blockers),
        "is_near_miss": len(blockers) <= NEAR_MISS_MAX_BLOCKERS,
        "signals_hit": _clean_text(row.get("signals_hit"), 2000),
        "first_seen_at": now,
        "last_seen_at": now,
        "seen_count": 1,
        "run_id": _clean_text(os.environ.get("GITHUB_RUN_ID"), 40),
        "run_number": _clean_text(os.environ.get("GITHUB_RUN_NUMBER"), 40),
        "superseded_by_formal": code in formal_codes,
    }
    return record


def load_shadow_records(path: str) -> dict[tuple[str, str], dict]:
    records: dict[tuple[str, str], dict] = {}
    if not path or not os.path.exists(path):
        return records
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                key = _record_key(record)
                if key[0] and key[1]:
                    records[key] = record
    except OSError:
        logger.warning("影子账本读取失败：%s", path)
    return records


def _write_jsonl(path: str, records: Iterable[dict]) -> bool:
    if not path:
        return False
    tmp = None
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("影子账本写入失败：%s（%s）", path, exc)
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def merge_shadow_records(existing: dict[tuple[str, str], dict],
                         incoming: Iterable[dict]) -> tuple[dict[tuple[str, str], dict], int]:
    merged = dict(existing)
    changed = 0
    for record in incoming:
        key = _record_key(record)
        if not key[0] or not key[1]:
            continue
        previous = merged.get(key)
        if previous:
            record = dict(record)
            record["first_seen_at"] = previous.get("first_seen_at") or record["first_seen_at"]
            record["seen_count"] = int(previous.get("seen_count") or 0) + 1
            record["last_seen_at"] = _now()
        merged[key] = record
        changed += 1
    return merged, changed


def record_shadow_pending(pending_rows: Optional[list[dict]], formal_df=None,
                          config=None, strategy: str = "bottom_fishing",
                          data_dir: Optional[str] = None) -> tuple[int, int]:
    """幂等写入影子账本；返回 (本次写入/更新条数, 账本累计条数)。"""
    rows = [row for row in (pending_rows or []) if isinstance(row, dict)]
    if not rows:
        return 0, 0
    formal_codes = _formal_codes(formal_df)
    incoming = [record for record in (
        build_shadow_record(row, strategy=strategy, config=config, formal_codes=formal_codes)
        for row in rows
    ) if record]
    if not incoming:
        return 0, 0

    cache_path = os.path.join(config.CACHE_DIR, "shadow_recommendations.jsonl") \
        if config is not None and getattr(config, "USE_CACHE", True) else None
    data_root = data_dir or os.path.join(_PROJECT_ROOT, "data", "shadow")
    data_path = os.path.join(data_root, "shadow_recommendations.jsonl")

    existing = load_shadow_records(cache_path) if cache_path else {}
    if not existing:
        existing = load_shadow_records(data_path)
    merged, changed = merge_shadow_records(existing, incoming)
    ordered = [merged[key] for key in sorted(merged)]

    if cache_path:
        _write_jsonl(cache_path, ordered)
    _write_jsonl(data_path, ordered)
    return changed, len(merged)


def summarize_shadow_records(records: dict[tuple[str, str], dict]) -> dict:
    values = list(records.values())
    blockers = Counter(
        ",".join(record.get("blockers") or []) or "none" for record in values
    )
    return {
        "records": len(values),
        "near_miss": sum(1 for record in values if record.get("is_near_miss")),
        "superseded_by_formal": sum(1 for record in values if record.get("superseded_by_formal")),
        "by_blocker": dict(blockers.most_common()),
        "by_operating_status": dict(Counter(
            str(record.get("operating_status") or "unknown") for record in values
        ).most_common()),
        "by_stabilization": dict(Counter(
            str(record.get("stabilization_level") or "unknown") for record in values
        ).most_common()),
    }


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "win_rate": None,
                "min": None, "max": None}
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 3),
        "median": round(statistics.median(values), 3),
        "win_rate": round(sum(1 for value in values if value > 0) / len(values), 4),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def summarize_tracking(observations: Iterable[dict]) -> dict:
    """按“阻断组合 × 期限”和“near-miss × 期限”汇总前瞻收益。"""
    by_blockers: dict[tuple[str, int], list[float]] = {}
    by_near_miss: dict[tuple[bool, int], list[float]] = {}
    overall: dict[int, list[float]] = {}
    for observation in observations or []:
        value = observation.get("return_pct")
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        horizon = int(observation.get("holding_trade_days") or 0)
        if horizon <= 0:
            continue
        blockers = ",".join(observation.get("blockers") or []) or "none"
        near_miss = bool(observation.get("is_near_miss"))
        by_blockers.setdefault((blockers, horizon), []).append(value)
        by_near_miss.setdefault((near_miss, horizon), []).append(value)
        overall.setdefault(horizon, []).append(value)

    def rows(groups: dict, label_key: str) -> list[dict]:
        result = []
        for (label, horizon), values in groups.items():
            result.append({label_key: label, "holding_trade_days": horizon, **_stats(values)})
        return sorted(result, key=lambda row: (str(row[label_key]), row["holding_trade_days"]))

    return {
        "by_blockers": rows(by_blockers, "blockers"),
        "by_near_miss": [
            {"is_near_miss": label, "holding_trade_days": horizon, **_stats(values)}
            for (label, horizon), values in sorted(by_near_miss.items(),
                                                   key=lambda item: (item[0][0], item[0][1]))
        ],
        "overall": [
            {"holding_trade_days": horizon, **_stats(values)}
            for horizon, values in sorted(overall.items())
        ],
    }
