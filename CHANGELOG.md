# Changelog

## 0.2.0 (2026-11-21)

### Fixed
- **False "stuck" alerts and unnecessary restarts after every (re)start.** The heartbeat job's first run happened one
  full interval (30 s) after start, because it was registered before the scheduler started. The watchdog checked
  15 s after start, found no heartbeat, and restarted a healthy bot.
  - `ai_bot`: write one heartbeat in `post_init`, before polling starts.
  - `watchdog_bot`: read the service uptime from `systemctl show` (`ActiveEnterTimestampMonotonic`) and skip the
    heartbeat check during a grace period of `HEARTBEAT_MAX_AGE` after each (re)start.
  - Verified on systemd (WSL2 Ubuntu): no false alert after a restart, and a process frozen with `SIGSTOP` is still
    detected and auto-restarted.

### Changed
- The watchdog's health check returns a typed `Health` dataclass instead of a dict.
- `/status` also shows how long each service has been running.

### Added
- Unit tests for `heartbeat_problem()` and `parse_systemctl_show()`.
- `pyproject.toml` (ruff, pytest config), `requirements-dev.txt`, GitHub Actions CI (Python 3.10–3.13).

## 0.1.0

- AI bot: Q&A and summaries over any OpenAI-compatible API, rate limit, retries, token/latency logging, heartbeat.
- Watchdog bot: `/status`, `/logs`, `/restart`, `/explain`, `/report`, state-change alerts, stuck-process detection
  with auto-restart cooldown, daily report.
- systemd units, one-command `deploy/setup.sh`, narrow sudoers rule, runbook.
