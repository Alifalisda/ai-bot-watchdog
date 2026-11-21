# Runbook

Short, practical notes for whoever is on duty. All commands run on the server over SSH.

## Where things are

| What | Where |
|---|---|
| Code | `/opt/ai-bots/bots/` |
| Settings and secrets | `/opt/ai-bots/.env` (mode 600, never commit) |
| Services | `ai-bot.service`, `watchdog-bot.service` |
| Logs | `journalctl -u ai-bot` / `journalctl -u watchdog-bot` |
| AI bot heartbeat | `/var/lib/ai-bot/heartbeat` (Unix timestamp, updated every 30s) |

## Everyday commands

```bash
systemctl status ai-bot watchdog-bot           # running? since when?
journalctl -u ai-bot -n 100 --no-pager         # last 100 lines
journalctl -u ai-bot -f                        # follow live (Ctrl+C to stop)
journalctl -u ai-bot --since "1 hour ago" | grep -E "ERROR|Traceback"
sudo systemctl restart ai-bot
```

From Telegram: `/status`, `/logs ai-bot 50`, `/restart ai-bot`, `/explain ai-bot`.

## When something breaks

### "🚨 ai-bot is unhealthy: systemd state is 'activating' or 'failed'"
The process keeps crashing. `activating` means systemd is in its 5-second wait before the next restart; `failed` means it has given up.
1. `journalctl -u ai-bot -n 80 --no-pager` (or `/explain ai-bot`) and find the first `Traceback` or `ERROR`.
2. `Missing required setting ...` → fix `/opt/ai-bots/.env`, then restart.
3. `ModuleNotFoundError` → reinstall dependencies: `sudo bash deploy/setup.sh`.
4. `telegram.error.InvalidToken` → the token was revoked or mistyped. Get a new one from @BotFather.
5. Restart: `sudo systemctl reset-failed ai-bot && sudo systemctl restart ai-bot`.

### "🔁 Auto-restarted ai-bot (heartbeat had stopped)"
The process was running but frozen. The watchdog already restarted it.
- Once: nothing to do. Note it in the change log below.
- Repeatedly: check logs from just before the freeze. Also check memory (`free -h`) and disk (`df -h`): a full disk stops the heartbeat file from being written.

### Users say the bot replies "The AI service isn't responding"
The bot is fine; the AI provider isn't.
1. `journalctl -u ai-bot | grep ai_ | tail` → look for `ai_unreachable`, `ai_rate_limited` or `ai_status_error status=...`.
2. `401/403` → the API key is wrong or expired. `404` → the model name in `AI_MODEL` no longer exists. `429` → you've hit the quota. Wait, or lower usage.
3. Check the provider's status page.

### The watchdog itself is down
You won't get alerts, so check it after any server reboot:
`systemctl status watchdog-bot`. It sends "👀 Watchdog started" to the admin chat every time it starts.

## Demo: test the stuck-process detection

```bash
sudo systemctl kill -s SIGSTOP ai-bot   # freezes the process; systemd still says "active"
systemctl is-active ai-bot              # -> active (systemd doesn't notice!)
# wait ~2-3 minutes: the watchdog alerts, then auto-restarts it
```

## Change log

Write one line each time you change something on the server.

| Date | Who | What changed | Why |
|---|---|---|---|
| 2026-11-21 | Alifalisda | Initial deploy on WSL2 Ubuntu (systemd) | – |
| 2026-11-21 | Alifalisda | Deployed 0.2.0: heartbeat on startup + watchdog grace period | False "stuck" alert and needless restart after every restart (see CHANGELOG.md) |
