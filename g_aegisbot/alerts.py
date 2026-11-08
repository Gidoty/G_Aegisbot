"""Alerts: a CRITICAL log line in the journal is useless if nobody is watching it, so the runner can also push it to a chat or
webhook. Everything here is best-effort and can never harm trading: messages go through a bounded queue to a background thread,
a failed or slow request is dropped, identical messages are not repeated within five minutes, and a flood is cut to a notice.

Configuration (all optional; without it alerts are off and the logs are unchanged):
  AEGIS_ALERT_WEBHOOK_URL        https URL that accepts a JSON POST {"text": "...", "content": "..."} (Slack, Discord and most
                                 relays), OR
  AEGIS_ALERT_TELEGRAM_BOT_TOKEN + AEGIS_ALERT_TELEGRAM_CHAT_ID    a Telegram bot and chat
  AEGIS_ALERT_MIN_LEVEL          CRITICAL (default), ERROR or WARNING
The URL, token and chat id are secrets: they are never logged, never put in an alert, and never echoed in an error.
Only the log message is sent: no traceback, no exception arguments, no environment, and anything that looks like a key or a
long token is replaced by [redacted].
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LEVELS = {"CRITICAL": logging.CRITICAL, "ERROR": logging.ERROR, "WARNING": logging.WARNING}
DEDUPE_SECONDS = 300
MAX_PER_WINDOW = 20
WINDOW_SECONDS = 600
MAX_LEN = 600
TIMEOUT = 5.0
_SECRETISH = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")
_PRIVATE_NET = re.compile(r"^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|localhost$|\[?::1\]?$)")


def redact(text: str) -> str:
    return _SECRETISH.sub("[redacted]", text)[:MAX_LEN]


class AlertHandler(logging.Handler):
    def __init__(self, send, level: int = logging.CRITICAL, *, clock=time.monotonic, queue_size: int = 100):
        super().__init__(level)
        self._send, self._clock = send, clock
        self._seen: dict[str, float] = {}
        self._sent_at: list[float] = []
        self._suppressed = 0
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="aegis-alerts", daemon=True)
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = redact(record.getMessage())
            key = hashlib.sha256(f"{record.levelno}:{text}".encode()).hexdigest()
            now = self._clock()
            with self._lock:
                if now - self._seen.get(key, -1e9) < DEDUPE_SECONDS:
                    return
                self._seen[key] = now
                self._seen = {k: t for k, t in self._seen.items() if now - t < DEDUPE_SECONDS}
                self._sent_at = [t for t in self._sent_at if now - t < WINDOW_SECONDS]
                if len(self._sent_at) >= MAX_PER_WINDOW:
                    self._suppressed += 1
                    return
                self._sent_at.append(now)
                notice = ""
                if self._suppressed:
                    notice, self._suppressed = f" ({self._suppressed} similar alerts were suppressed)", 0
            self._q.put_nowait(f"[{record.levelname}] {text}{notice}")
        except Exception:                                    # logging must never raise into the trading loop
            pass

    def _run(self) -> None:
        while True:
            text = self._q.get()
            try:
                self._send(text)
            except Exception:
                pass
            finally:
                self._q.task_done()

    def flush_pending(self, timeout: float = 5.0) -> None:
        """For tests and shutdown: wait (bounded) until queued alerts have been attempted."""
        end = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.01)


def _post(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "g-aegisbot-alerts"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:       # noqa: S310 (scheme is validated in make_sender)
        resp.read(1024)


def make_sender(env, *, allow_insecure_local: bool = False):
    """A function text -> None for the configured channel, or None when alerts are not configured or the setting is invalid.
    Never raises and never reveals the setting."""
    url = (env.get("AEGIS_ALERT_WEBHOOK_URL") or "").strip()
    token, chat = (env.get("AEGIS_ALERT_TELEGRAM_BOT_TOKEN") or "").strip(), (env.get("AEGIS_ALERT_TELEGRAM_CHAT_ID") or "").strip()
    if url:
        parts = urllib.parse.urlsplit(url)
        local = bool(_PRIVATE_NET.match(parts.hostname or ""))
        if parts.scheme != "https" and not (allow_insecure_local and parts.scheme == "http" and local):
            return None
        return lambda text: _post(url, {"text": text, "content": text})
    if token and chat:
        endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
        return lambda text: _post(endpoint, {"chat_id": chat, "text": text})
    return None


def install(env, logger: logging.Logger | None = None, *, allow_insecure_local: bool = False) -> AlertHandler | None:
    """Attach the alert handler to `logger` (the root logger by default). Returns it, or None when alerts are off."""
    sender = make_sender(env, allow_insecure_local=allow_insecure_local)
    wanted = (env.get("AEGIS_ALERT_MIN_LEVEL") or "CRITICAL").strip().upper()
    configured = bool((env.get("AEGIS_ALERT_WEBHOOK_URL") or "").strip() or (env.get("AEGIS_ALERT_TELEGRAM_BOT_TOKEN") or "").strip())
    log = logging.getLogger("aegis.alerts")
    if sender is None:
        log.warning("alerts are OFF%s", " (the alert setting is invalid: an https URL, or a Telegram token and chat id, is needed)"
                    if configured else " (no AEGIS_ALERT_* setting)")
        return None
    if wanted not in LEVELS:
        log.warning("AEGIS_ALERT_MIN_LEVEL is not one of %s; using CRITICAL", ", ".join(LEVELS))
        wanted = "CRITICAL"
    handler = AlertHandler(sender, LEVELS[wanted])
    (logger or logging.getLogger()).addHandler(handler)
    log.info("alerts are ON for %s and above", wanted)
    return handler
