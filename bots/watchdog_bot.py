"""Watchdog bot: monitors systemd services and lets the admin manage them from Telegram.

- /status, /logs, /restart, /explain (AI reads the logs), /report
- Alerts when a service goes down or recovers
- Detects a *stuck* process (systemd says "active" but the heartbeat stopped)
  and restarts it automatically, with a cooldown so it can't loop
- Daily summary at a fixed time

Only the chat in ADMIN_CHAT_ID can use it. Needs Linux with systemd.

Run:  python -m bots.watchdog_bot
"""
from __future__ import annotations

import asyncio
import html
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dtime
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from bots.common import env, env_bool, env_int, make_ai_client, setup_logging
from bots.utils import (
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
)

log = setup_logging("watchdog")

TOKEN = env("WATCHDOG_BOT_TOKEN", required=True)
ADMIN_CHAT_ID = env_int("ADMIN_CHAT_ID", 0)
SERVICES = parse_csv(env("WATCHED_SERVICES", "ai-bot"))
HEARTBEATS = {name: Path(p) for name, p in parse_kv_list(env("HEARTBEAT_FILES", "")).items()}
CHECK_INTERVAL = env_int("CHECK_INTERVAL", 60)
HEARTBEAT_MAX_AGE = env_int("HEARTBEAT_MAX_AGE", 120)
AUTO_RESTART_ON_STALE = env_bool("AUTO_RESTART_ON_STALE", True)
AUTO_RESTART_COOLDOWN = env_int("AUTO_RESTART_COOLDOWN", 600)
REPORT_HOUR, REPORT_MINUTE = parse_hhmm(env("DAILY_REPORT_TIME", "09:00"))
TZ = ZoneInfo(env("TIMEZONE", "Asia/Jakarta"))
SYSTEMCTL = env("SYSTEMCTL_PATH", "/usr/bin/systemctl")
JOURNALCTL = env("JOURNALCTL_PATH", "/usr/bin/journalctl")
AI_MODEL = env("AI_MODEL")

ai_client = make_ai_client(required=False)

EXPLAIN_PROMPT = (
    "You are a senior DevOps engineer. Below are recent logs from a Linux systemd service. "
    "Explain in plain English the most likely cause of any errors, then suggest 1-3 concrete fixes. "
    "Maximum 10 short lines, plain text. If the logs look healthy, say so."
)


# ---------- shell helpers ----------

async def run_cmd(*args: str, timeout: float = 20) -> tuple[int, str]:
    """Run a command without a shell (arguments are never interpreted). Returns (exit_code, output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except FileNotFoundError:
        return 127, f"command not found: {args[0]}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timed out after {timeout:.0f}s"
    return proc.returncode, out.decode(errors="replace").strip()


@dataclass(frozen=True)
class Health:
    name: str
    state: str                  # systemd ActiveState: active / inactive / failed / activating ...
    uptime: float | None        # seconds since the service entered "active"
    hb_age: float | None        # seconds since the last heartbeat, None if missing
    problems: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not self.problems

    @property
    def stuck(self) -> bool:
        # "active" but unhealthy = running yet frozen. systemd won't restart this on its own.
        return self.state == "active" and bool(self.problems)


async def check_service(name: str) -> Health:
    _, out = await run_cmd(SYSTEMCTL, "show", name, "-p", "ActiveState", "-p", "ActiveEnterTimestampMonotonic")
    props = parse_systemctl_show(out)
    state = props.get("ActiveState") or "unknown"

    uptime = None
    entered_us = props.get("ActiveEnterTimestampMonotonic", "")
    if state == "active" and entered_us.isdigit() and int(entered_us) > 0:
        # Both systemd and time.monotonic() use CLOCK_MONOTONIC on Linux.
        uptime = time.monotonic() - int(entered_us) / 1_000_000

    hb_path = HEARTBEATS.get(name)
    hb_age = read_heartbeat_age(hb_path) if hb_path else None

    problems: list[str] = []
    if state != "active":
        problems.append(f"systemd state is '{state}'")
    elif hb_path is not None:
        problem = heartbeat_problem(hb_age, uptime, HEARTBEAT_MAX_AGE)
        if problem:
            problems.append(problem)

    return Health(name, state, uptime, hb_age, tuple(problems))


def status_line(h: Health) -> str:
    icon = "✅" if h.healthy else "❌"
    detail = h.state
    if h.uptime is not None:
        detail += f" for {format_duration(h.uptime)}"
    if h.hb_age is not None:
        detail += f", heartbeat {format_duration(h.hb_age)} ago"
    line = f"{icon} {h.name} - {detail}"
    if h.problems:
        line += "\n    ⚠️ " + "; ".join(h.problems)
    return line


async def restart_service(name: str) -> tuple[bool, str]:
    # Allowed by a narrow sudoers rule, see deploy/sudoers-ai-bots
    rc, out = await run_cmd("sudo", "-n", SYSTEMCTL, "restart", name, timeout=60)
    return rc == 0, out


# ---------- access control ----------

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if not ADMIN_CHAT_ID or chat is None or chat.id != ADMIN_CHAT_ID:
            log.warning(
                "unauthorized chat=%s user=%s command=%s",
                chat.id if chat else None,
                update.effective_user.id if update.effective_user else None,
                update.message.text if update.message else None,
            )
            return
        return await func(update, context)

    return wrapper


async def send_admin(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if ADMIN_CHAT_ID:
        await context.bot.send_message(ADMIN_CHAT_ID, text)


async def service_arg(update: Update, context: ContextTypes.DEFAULT_TYPE, usage: str) -> str | None:
    if not context.args:
        await update.message.reply_text(f"Usage: {usage}\nServices: {', '.join(SERVICES)}")
        return None
    name = context.args[0]
    if name not in SERVICES:  # whitelist: never pass arbitrary names to systemctl
        await update.message.reply_text(f"Unknown service '{name}'. Watched: {', '.join(SERVICES)}")
        return None
    return name


# ---------- commands ----------

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open to everyone, so you can find the value for ADMIN_CHAT_ID during setup."""
    await update.message.reply_text(f"Your chat ID is {update.effective_chat.id}")


@admin_only
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Watchdog commands:\n"
        "/status - health of all services\n"
        "/logs <service> [lines] - recent log lines\n"
        "/restart <service> - restart a service\n"
        "/explain <service> - AI explains recent errors\n"
        "/report - send the daily report now\n"
        f"\nWatching: {', '.join(SERVICES)}"
    )


@admin_only
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    results = await asyncio.gather(*(check_service(s) for s in SERVICES))
    await update.message.reply_text("\n".join(status_line(h) for h in results))


@admin_only
async def logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = await service_arg(update, context, "/logs <service> [lines]")
    if not name:
        return
    lines = 30
    if len(context.args) > 1 and context.args[1].isdigit():
        lines = min(int(context.args[1]), 200)
    _, out = await run_cmd(JOURNALCTL, "-u", name, "-n", str(lines), "--no-pager", "-o", "short-iso")
    body = html.escape(tail_text(out or "(no log lines)", 3500))
    await update.message.reply_text(f"<b>{html.escape(name)}</b>\n<pre>{body}</pre>", parse_mode=ParseMode.HTML)


@admin_only
async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = await service_arg(update, context, "/restart <service>")
    if not name:
        return
    log.info("manual_restart service=%s by_user=%s", name, update.effective_user.id)
    await update.message.reply_text(f"Restarting {name}...")
    ok, out = await restart_service(name)
    if not ok:
        await update.message.reply_text(f"❌ Restart failed:\n{head_text(out, 1000)}")
        return
    await asyncio.sleep(5)  # give it a moment to start and write a heartbeat
    await update.message.reply_text(status_line(await check_service(name)))


@admin_only
async def explain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = await service_arg(update, context, "/explain <service>")
    if not name:
        return
    if ai_client is None or not AI_MODEL:
        await update.message.reply_text("AI is not configured (set AI_API_KEY and AI_MODEL in .env).")
        return
    _, out = await run_cmd(JOURNALCTL, "-u", name, "-n", "80", "--no-pager", "-o", "short-iso")
    try:
        resp = await ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {"role": "system", "content": EXPLAIN_PROMPT},
                {"role": "user", "content": f"Service: {name}\n\n{tail_text(out, 6000)}"},
            ],
            max_tokens=2000,
        )
        answer = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # the watchdog must keep running even if the AI call fails
        log.error("explain_failed service=%s error=%r", name, exc)
        answer = f"AI call failed: {exc.__class__.__name__}. Check the logs with /logs {name}."
    await update.message.reply_text(head_text(f"🧠 {name}\n\n{answer}", 4000))


async def build_report() -> str:
    lines = [f"📊 Daily report - {datetime.now(TZ):%Y-%m-%d %H:%M %Z}"]
    for name in SERVICES:
        h = await check_service(name)
        _, restarts = await run_cmd(SYSTEMCTL, "show", "-p", "NRestarts", "--value", name)
        _, day_logs = await run_cmd(
            JOURNALCTL, "-u", name, "--since", "24 hours ago", "--no-pager", "-o", "cat", timeout=40
        )
        lines.append(
            f"{status_line(h)}\n"
            f"    auto-restarts by systemd: {restarts or '?'} · error lines in 24h: {count_error_lines(day_logs)}"
        )
    return "\n".join(lines)


@admin_only
async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(await build_report())


# ---------- scheduled jobs ----------

async def periodic_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    last_healthy: dict = context.bot_data.setdefault("last_healthy", {})
    last_auto_restart: dict = context.bot_data.setdefault("last_auto_restart", {})

    for name in SERVICES:
        try:
            h = await check_service(name)
            was_healthy = last_healthy.get(name, True)
            last_healthy[name] = h.healthy

            # Alert only when the state changes, so a long outage doesn't spam the chat.
            if was_healthy and not h.healthy:
                log.warning("service_down service=%s problems=%s", name, list(h.problems))
                await send_admin(context, f"🚨 {name} is unhealthy\n{status_line(h)}")
            elif not was_healthy and h.healthy:
                log.info("service_recovered service=%s", name)
                await send_admin(context, f"✅ {name} recovered\n{status_line(h)}")

            if h.stuck and AUTO_RESTART_ON_STALE:
                now = time.time()
                if now - last_auto_restart.get(name, 0) >= AUTO_RESTART_COOLDOWN:
                    last_auto_restart[name] = now
                    log.warning("auto_restart service=%s reason=stale_heartbeat", name)
                    ok, out = await restart_service(name)
                    msg = (
                        f"🔁 Auto-restarted {name} (it was running but its heartbeat had stopped)."
                        if ok
                        else f"❌ Tried to auto-restart {name} but failed:\n{head_text(out, 800)}"
                    )
                    await send_admin(context, msg)
        except Exception:
            log.exception("check_failed service=%s", name)


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_admin(context, await build_report())


async def on_startup(app: Application) -> None:
    if ADMIN_CHAT_ID:
        await app.bot.send_message(ADMIN_CHAT_ID, f"👀 Watchdog started. Watching: {', '.join(SERVICES)}")
    else:
        log.warning("ADMIN_CHAT_ID is not set: send /myid to this bot, put the number in .env, then restart.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled_error update_id=%s", getattr(update, "update_id", None), exc_info=context.error)


def main() -> None:
    app = Application.builder().token(TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler(["start", "help"], help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(CommandHandler("explain", explain))
    app.add_handler(CommandHandler("report", report))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(periodic_check, interval=CHECK_INTERVAL, first=15)
    app.job_queue.run_daily(daily_report_job, time=dtime(REPORT_HOUR, REPORT_MINUTE, tzinfo=TZ))

    log.info("watchdog starting services=%s interval=%ss", SERVICES, CHECK_INTERVAL)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
