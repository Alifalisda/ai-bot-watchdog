import pytest

from bots.utils import (
    RateLimiter,
    count_error_lines,
    format_duration,
    head_text,
    heartbeat_problem,
    parse_csv,
    parse_hhmm,
    parse_kv_list,
    parse_systemctl_show,
    read_heartbeat_age,
    tail_text,
    write_heartbeat,
)


def test_rate_limiter_blocks_after_limit_and_recovers():
    rl = RateLimiter(max_calls=2, period=60)
    assert rl.allow("u", now=0)
    assert rl.allow("u", now=1)
    assert not rl.allow("u", now=2)
    assert rl.retry_after("u", now=2) == pytest.approx(58)
    assert rl.allow("other", now=2)  # limits are per user
    assert rl.allow("u", now=60)  # first call has expired


def test_parsers():
    assert parse_csv(" a, b,,c ") == ["a", "b", "c"]
    assert parse_kv_list("ai-bot=/var/lib/x, y=/z") == {"ai-bot": "/var/lib/x", "y": "/z"}
    assert parse_kv_list("") == {}
    with pytest.raises(ValueError):
        parse_kv_list("no-equals-sign")
    assert parse_hhmm("09:05") == (9, 5)
    with pytest.raises(ValueError):
        parse_hhmm("25:00")


def test_text_trimming():
    assert head_text("abcdef", 4) == "abc…"
    assert tail_text("abcdef", 4) == "…def"
    assert head_text("ab", 4) == "ab"


def test_count_error_lines():
    logs = "\n".join([
        "2026-11-21 INFO [ai_bot] ai_ok",
        "2026-11-21 ERROR [ai_bot] ai_unreachable",
        "Traceback (most recent call last):",
        "2026-11-21 WARNING [ai_bot] something",
        "2026-11-21 CRITICAL [ai_bot] boom",
    ])
    assert count_error_lines(logs) == 3


def test_format_duration():
    assert format_duration(5) == "5s"
    assert format_duration(125) == "2m 5s"
    assert format_duration(3 * 3600 + 60) == "3h 1m"
    assert format_duration(2 * 86400 + 3600) == "2d 1h"


def test_heartbeat_roundtrip(tmp_path):
    path = tmp_path / "hb"
    assert read_heartbeat_age(path) is None  # missing file
    write_heartbeat(path, now=1000.0)
    assert read_heartbeat_age(path, now=1030.0) == pytest.approx(30.0)
    path.write_text("garbage")
    assert read_heartbeat_age(path) is None


def test_parse_systemctl_show():
    out = "ActiveState=active\nActiveEnterTimestampMonotonic=123456\n\nnoise"
    assert parse_systemctl_show(out) == {"ActiveState": "active", "ActiveEnterTimestampMonotonic": "123456"}
    assert parse_systemctl_show("") == {}


@pytest.mark.parametrize(
    ("hb_age", "uptime", "expected"),
    [
        (10, 600, None),                    # fresh heartbeat, long-running service: healthy
        (None, 5, None),                    # just started, no heartbeat yet: grace period
        (900, 5, None),                     # just restarted, file left over from the old run: grace
        (None, 600, "no heartbeat file"),   # running for a while and never wrote one
        (None, None, "no heartbeat file"),  # uptime unknown: no grace
    ],
)
def test_heartbeat_problem(hb_age, uptime, expected):
    assert heartbeat_problem(hb_age, uptime, max_age=120) == expected


def test_heartbeat_problem_detects_stuck_process():
    problem = heartbeat_problem(hb_age=300, uptime=600, max_age=120)
    assert problem is not None and "stuck" in problem
