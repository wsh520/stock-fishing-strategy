"""Completed-market-day and quote-cache metadata, independent of providers."""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
VERSION = 1


def quote_phase(now):
    return now.strftime("%Y-%m-%d") + ("_closed" if now.hour >= 15 else "_intraday")


def latest_completed_day(calendar_rows, now):
    """Requires an exchange calendar covering today, including non-trading days."""
    today = now.date().isoformat()
    mapping = {}
    for row in calendar_rows or []:
        day = str(row.get("calendar_date", row.get("date", "")))[:10]
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        flag = str(row.get("is_trading_day", ""))
        if flag not in ("0", "1") or day in mapping:
            return None
        mapping[day] = flag
    if today not in mapping:
        return None
    limit = today if now.hour >= 15 else (now.date() - timedelta(days=1)).isoformat()
    eligible = [day for day, flag in mapping.items() if flag == "1" and day <= limit]
    return max(eligible) if eligible else None


def quote_metadata(frame, now):
    last = str(frame.iloc[-1].get("date", ""))[:10] if frame is not None and not frame.empty else None
    return dict(version=VERSION, fetched_at=now.isoformat(), phase=quote_phase(now), last_bar=last)


def quote_cache_valid(metadata, now):
    """Never infer the fetch phase from a mutable file modification timestamp."""
    if not isinstance(metadata, dict) or metadata.get("version") != VERSION:
        return False
    try:
        fetched = datetime.fromisoformat(metadata["fetched_at"])
        if fetched.tzinfo is None:
            return False
        fetched = fetched.astimezone(BEIJING)
    except (KeyError, TypeError, ValueError):
        return False
    if fetched > now or fetched.date() != now.date():
        return False
    if now.hour >= 15 and fetched.hour < 15:
        return False
    return metadata.get("phase") == quote_phase(fetched)
