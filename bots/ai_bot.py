"""AI assistant bot for Telegram.

Send it a question and it answers; paste a long text and it summarizes it.
Built to be run as a long-lived service: rate limiting, error handling,
token-usage logging and a heartbeat file the watchdog can check.

Run:  python -m bots.ai_bot
"""
from __future__ import annotations

import time
from pathlib import Path

from openai import APIConnectionError, APIStatusError, RateLimitError
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bots.common import env, env_int, make_ai_client, setup_logging
from bots.utils import RateLimiter, format_duration, head_text, write_heartbeat

log = setup_logging("ai_bot")

TOKEN = env("AI_BOT_TOKEN", required=True)
MODEL = env("AI_MODEL", required=True)
HEARTBEAT_FILE = Path(env("AI_BOT_HEARTBEAT_FILE", "./data/ai-bot.heartbeat"))
HEARTBEAT_INTERVAL = env_int("HEARTBEAT_INTERVAL", 30)
MAX_INPUT_CHARS = env_int("AI_MAX_INPUT_CHARS", 8000)
MAX_OUTPUT_TOKENS = env_int("AI_MAX_OUTPUT_TOKENS", 2000)  # high enough for "thinking" models

SYSTEM_PROMPT = (
    "You are a concise assistant inside a Telegram chat. "
    "If the user sends a long text or article, summarize it in 3-5 short bullet points. "
    "Otherwise answer briefly and clearly. Reply in the same language the user writes in. "
    "Use plain text, no markdown tables."
)

limiter = RateLimiter(max_calls=env_int("AI_RATE_LIMIT_PER_MIN", 5), period=60)
client = make_ai_client(required=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi! Ask me anything, or paste a long text and I'll summarize it.\n\n"
        "/ping - check that I'm alive"
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uptime = time.time() - context.bot_data["started_at"]
    await update.message.reply_text(f"pong - up {format_duration(uptime)}, model: {MODEL}")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text or ""

    if not limiter.allow(user_id):
        wait = limiter.retry_after(user_id)
        log.info("rate_limited user=%s retry_after=%.0fs", user_id, wait)
        await update.message.reply_text(f"Slow down a little - try again in {wait:.0f}s.")
        return

    if len(text) > MAX_INPUT_CHARS:
        await update.message.reply_text(
            f"That's too long ({len(text)} characters). Please send under {MAX_INPUT_CHARS}."
        )
        return

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    started = time.monotonic()
    try:
        resp = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            max_tokens=MAX_OUTPUT_TOKENS,
        )
    # The client already retried with backoff before raising any of these.
    except APIConnectionError as exc:  # includes timeouts
        log.error("ai_unreachable user=%s error=%r", user_id, exc)
        await update.message.reply_text("The AI service isn't responding right now. Please try again in a minute.")
        return
    except RateLimitError as exc:
        log.error("ai_rate_limited user=%s error=%s", user_id, head_text(str(exc), 300))
        await update.message.reply_text("The AI provider is rate-limiting me. Please try again shortly.")
        return
    except APIStatusError as exc:
        log.error("ai_status_error user=%s status=%s error=%s", user_id, exc.status_code, head_text(str(exc), 300))
        await update.message.reply_text("Something went wrong on the AI side. It has been logged.")
        return

    latency = time.monotonic() - started
    answer = (resp.choices[0].message.content or "").strip() or "(the model returned an empty reply)"
    usage = resp.usage
    log.info(
        "ai_ok user=%s model=%s in_tokens=%s out_tokens=%s latency=%.2fs",
        user_id,
        MODEL,
        getattr(usage, "prompt_tokens", "?"),
        getattr(usage, "completion_tokens", "?"),
        latency,
    )
    await update.message.reply_text(head_text(answer, 4000))


async def heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs on the same event loop as the bot: if the bot hangs, the heartbeat stops too."""
    try:
        write_heartbeat(HEARTBEAT_FILE)
    except OSError:
        log.exception("heartbeat_write_failed path=%s", HEARTBEAT_FILE)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled_error update_id=%s", getattr(update, "update_id", None), exc_info=context.error)


async def on_startup(app: Application) -> None:
    # The repeating job below does NOT fire at t=0: it is registered before the scheduler
    # starts, so its first run lands one full interval later. Write one heartbeat now so
    # the watchdog never sees a running bot without a fresh heartbeat.
    write_heartbeat(HEARTBEAT_FILE)


def main() -> None:
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)

    app = Application.builder().token(TOKEN).post_init(on_startup).build()
    app.bot_data["started_at"] = time.time()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(heartbeat, interval=HEARTBEAT_INTERVAL, first=HEARTBEAT_INTERVAL)

    log.info("ai_bot starting model=%s heartbeat=%s", MODEL, HEARTBEAT_FILE)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
