from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from app.config import config

TZ = ZoneInfo(config.tz)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now_utc().isoformat()


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def in_minutes(minutes: int) -> str:
    return iso(now_utc() + timedelta(minutes=minutes))


def in_days(days: int) -> str:
    return iso(now_utc() + timedelta(days=days))


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_now() -> datetime:
    return datetime.now(TZ)


def fmt_time(value: str | None, with_date: bool = False) -> str:
    dt = parse_iso(value)
    if not dt:
        return "—"
    local = dt.astimezone(TZ)
    return local.strftime("%d.%m %H:%M") if with_date else local.strftime("%H:%M")


def fmt_date(value: str | None) -> str:
    dt = parse_iso(value)
    return dt.astimezone(TZ).strftime("%d.%m.%Y") if dt else "—"


def h(value) -> str:
    return escape(str(value if value is not None else ""))


def fmt_num(n: int | None) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + " МЛН"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)


def season() -> str:
    return local_now().strftime("%Y-%m")


def day_key() -> str:
    return local_now().strftime("%Y-%m-%d")


def hours_between(start: int, end: int, hour: int) -> bool:
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def minutes_left(deadline_iso: str | None) -> int:
    dt = parse_iso(deadline_iso)
    if not dt:
        return 0
    return int((dt - now_utc()).total_seconds() // 60)


def mention(user: dict | None) -> str:
    if not user:
        return "—"
    if user.get("username"):
        return f"@{h(user['username'])}"
    return f'<a href="tg://user?id={user["id"]}">{h(user.get("full_name") or user["id"])}</a>'


def trunc(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
