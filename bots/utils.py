"""Small pure helpers. No Telegram or network imports, so they are easy to unit-test."""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` per `period` seconds per key (e.g. user id)."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._calls: dict[object, deque[float]] = defaultdict(deque)

    def _prune(self, key: object, now: float) -> deque[float]:
        q = self._calls[key]
        while q and now - q[0] >= self.period:
            q.popleft()
        return q

    def allow(self, key: object, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self._prune(key, now)
        if len(q) >= self.max_calls:
            return False
        q.append(now)
        return True

    def retry_after(self, key: object, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        q = self._prune(key, now)
        if len(q) < self.max_calls:
            return 0.0
        return max(0.0, self.period - (now - q[0]))


def parse_csv(value: str) -> list[str]:
    """'a, b,,c' -> ['a', 'b', 'c']"""
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_kv_list(value: str) -> dict[str, str]:
    """'ai-bot=/var/lib/ai-bot/heartbeat, other=/x' -> {'ai-bot': '/var/lib/...', 'other': '/x'}"""
    result: dict[str, str] = {}
    for item in parse_csv(value):
        if "=" not in item:
            raise ValueError(f"Expected name=value, got {item!r}")
        key, val = item.split("=", 1)
        result[key.strip()] = val.strip()
    return result


def parse_hhmm(value: str) -> tuple[int, int]:
    """'09:30' -> (9, 30)"""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        raise ValueError(f"Expected HH:MM, got {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"Invalid time {value!r}")
    return hour, minute


def head_text(text: str, limit: int) -> str:
    """Keep the start of a long text (Telegram messages max out at 4096 chars)."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tail_text(text: str, limit: int) -> str:
    """Keep the end of a long text (the newest log lines are at the bottom)."""
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


_ERROR_LINE = re.compile(r"\b(ERROR|CRITICAL)\b|Traceback \(most recent call last\)")


def count_error_lines(log_text: str) -> int:
    return sum(1 for line in log_text.splitlines() if _ERROR_LINE.search(line))


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def write_heartbeat(path: Path, now: float | None = None) -> None:
    """Write the current time atomically, so a reader never sees a half-written file."""
    now = time.time() if now is None else now
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{now:.3f}")
    os.replace(tmp, path)


def read_heartbeat_age(path: Path, now: float | None = None) -> float | None:
    """Seconds since the last heartbeat, or None if the file is missing or unreadable."""
    now = time.time() if now is None else now
    try:
        return now - float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def parse_systemctl_show(output: str) -> dict[str, str]:
    """Parse `systemctl show -p A -p B` output ('A=x' per line) into a dict."""
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            result[key.strip()] = value.strip()
    return result


def heartbeat_problem(hb_age: float | None, uptime: float | None, max_age: float) -> str | None:
    """Decide whether an *active* service's heartbeat means it is stuck.

    `uptime` is how long the service has been active (None if unknown). A freshly
    (re)started service gets a grace period of `max_age` seconds: until then a missing
    or old heartbeat (left over from the previous run) is expected, not a failure.
    Without this grace the watchdog flagged every restart as "stuck" and restarted
    the bot again (see CHANGELOG.md, 0.2.0).
    """
    if uptime is not None and uptime < max_age:
        return None
    if hb_age is None:
        return "no heartbeat file"
    if hb_age > max_age:
        return f"heartbeat is {format_duration(hb_age)} old - process may be stuck"
    return None
