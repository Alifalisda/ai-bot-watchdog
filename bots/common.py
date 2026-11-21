"""Shared config, logging and AI client setup for both bots."""
from __future__ import annotations

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and not value:
        sys.exit(f"Missing required setting {name}. Add it to your .env file (see .env.example).")
    return value


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"Setting {name} must be a whole number, got {raw!r}.")


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def setup_logging(name: str) -> logging.Logger:
    """Log to stdout. Under systemd this ends up in journalctl automatically."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
    )
    # httpx logs every Telegram poll request (and the bot token in the URL). Keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger(name)


def make_ai_client(required: bool = True):
    """Client for any OpenAI-compatible API (OpenAI, Gemini, Groq, OpenRouter, ...)."""
    api_key = env("AI_API_KEY")
    if not api_key:
        if required:
            sys.exit("Missing AI_API_KEY in .env.")
        return None
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=api_key,
        base_url=env("AI_BASE_URL") or None,
        timeout=env_int("AI_TIMEOUT_SECONDS", 30),
        max_retries=env_int("AI_MAX_RETRIES", 2),  # retries with backoff on timeouts / 429 / 5xx
    )
