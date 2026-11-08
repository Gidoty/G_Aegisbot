"""Alerts are best-effort: they must deliver a CRITICAL message, and they must never block, raise or leak."""
import http.server
import json
import logging
import threading
import time

import pytest

from g_aegisbot import alerts


class Receiver:
    def __init__(self, delay=0.0, status=200):
        self.got, outer = [], self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                time.sleep(delay)
                outer.got.append(json.loads(body))
                self.send_response(status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.close()


def logger_with(handler):
    lg = logging.getLogger(f"alerts-test-{id(handler)}")
    lg.handlers, lg.propagate = [handler], False
    lg.setLevel(logging.DEBUG)
    return lg


def test_a_critical_message_reaches_the_webhook_as_json(receiver):
    h = alerts.install({"AEGIS_ALERT_WEBHOOK_URL": receiver.url}, logging.getLogger("t1"), allow_insecure_local=True)
    logging.getLogger("t1").critical("NEW ENTRIES BLOCKED: %s", "protection failure on BTC")
    h.flush_pending()
    assert receiver.got == [{"text": "[CRITICAL] NEW ENTRIES BLOCKED: protection failure on BTC",
                             "content": "[CRITICAL] NEW ENTRIES BLOCKED: protection failure on BTC"}]
    logging.getLogger("t1").removeHandler(h)


def test_lower_levels_are_not_sent_unless_asked_for(receiver):
    h = alerts.AlertHandler(lambda t: receiver.got.append(t), logging.CRITICAL)
    lg = logger_with(h)
    lg.warning("a warning"); lg.error("an error"); lg.info("info")
    h.flush_pending()
    assert receiver.got == []
    h2 = alerts.AlertHandler(lambda t: receiver.got.append(t), logging.ERROR)
    logger_with(h2).error("an error")
    h2.flush_pending()
    assert receiver.got == ["[ERROR] an error"]


def test_identical_messages_are_not_repeated_within_five_minutes():
    sent, now = [], [0.0]
    h = alerts.AlertHandler(sent.append, clock=lambda: now[0])
    lg = logger_with(h)
    lg.critical("same"); lg.critical("same")
    h.flush_pending()
    assert sent == ["[CRITICAL] same"]
    now[0] = alerts.DEDUPE_SECONDS + 1
    lg.critical("same")
    h.flush_pending()
    assert len(sent) == 2


def test_a_flood_is_cut_to_a_notice():
    sent, now = [], [0.0]
    h = alerts.AlertHandler(sent.append, clock=lambda: now[0])
    lg = logger_with(h)
    for i in range(alerts.MAX_PER_WINDOW + 7):
        lg.critical("distinct %d", i)
    h.flush_pending()
    assert len(sent) == alerts.MAX_PER_WINDOW
    now[0] = alerts.WINDOW_SECONDS + 1
    lg.critical("after the window")
    h.flush_pending()
    assert "7 similar alerts were suppressed" in sent[-1]


def test_secrets_and_tokens_in_a_message_are_redacted_and_tracebacks_are_not_sent():
    sent = []
    h = alerts.AlertHandler(sent.append)
    lg = logger_with(h)
    secret = "sk_live_" + "A1b2C3d4" * 6
    try:
        raise RuntimeError("SECRET-IN-EXCEPTION " + secret)
    except RuntimeError:
        lg.critical("order failed with key %s", secret, exc_info=True)
    h.flush_pending()
    assert secret not in sent[0] and "SECRET-IN-EXCEPTION" not in sent[0] and "[redacted]" in sent[0]
    assert alerts.redact("x" * 5000).__len__() <= alerts.MAX_LEN


def test_a_dead_or_failing_endpoint_never_raises_into_the_caller():
    def boom(text):
        raise OSError("down")
    h = alerts.AlertHandler(boom)
    logger_with(h).critical("still fine")
    h.flush_pending()
    h2 = alerts.install({"AEGIS_ALERT_WEBHOOK_URL": "http://127.0.0.1:9/none"}, logging.getLogger("t2"), allow_insecure_local=True)
    logging.getLogger("t2").critical("nobody listens")
    h2.flush_pending(2.0)
    logging.getLogger("t2").removeHandler(h2)


def test_a_slow_endpoint_does_not_block_the_logging_call():
    slow = Receiver(delay=1.0)
    try:
        h = alerts.install({"AEGIS_ALERT_WEBHOOK_URL": slow.url}, logging.getLogger("t3"), allow_insecure_local=True)
        start = time.monotonic()
        logging.getLogger("t3").critical("quick return")
        assert time.monotonic() - start < 0.2
        logging.getLogger("t3").removeHandler(h)
    finally:
        slow.close()


def test_a_full_queue_drops_alerts_instead_of_blocking():
    gate = threading.Event()
    h = alerts.AlertHandler(lambda t: gate.wait(2), queue_size=2)
    lg = logger_with(h)
    start = time.monotonic()
    for i in range(10):
        lg.critical("n %d", i)
    assert time.monotonic() - start < 0.5
    gate.set()


def test_alerts_are_off_without_settings_or_with_an_insecure_or_incomplete_one(caplog):
    assert alerts.install({}) is None
    assert alerts.make_sender({"AEGIS_ALERT_WEBHOOK_URL": "http://example.com/x"}) is None            # plain http to the internet
    assert alerts.make_sender({"AEGIS_ALERT_WEBHOOK_URL": "ftp://example.com/x"}) is None
    assert alerts.make_sender({"AEGIS_ALERT_WEBHOOK_URL": "http://127.0.0.1:1/x"}) is None            # local http only when allowed
    assert alerts.make_sender({"AEGIS_ALERT_TELEGRAM_BOT_TOKEN": "t"}) is None                       # chat id missing
    assert alerts.make_sender({"AEGIS_ALERT_WEBHOOK_URL": "https://hooks.example.com/abc"}) is not None
    assert alerts.make_sender({"AEGIS_ALERT_TELEGRAM_BOT_TOKEN": "t", "AEGIS_ALERT_TELEGRAM_CHAT_ID": "1"}) is not None


def test_the_setting_is_never_logged(caplog):
    secret = "https://hooks.example.com/services/TOPSECRETTOKEN"
    with caplog.at_level(logging.DEBUG):
        alerts.install({"AEGIS_ALERT_WEBHOOK_URL": secret.replace("https", "http")})            # invalid on purpose
        h = alerts.install({"AEGIS_ALERT_WEBHOOK_URL": secret, "AEGIS_ALERT_MIN_LEVEL": "nonsense"}, logging.getLogger("t4"))
    assert "TOPSECRETTOKEN" not in caplog.text and "alerts are OFF" in caplog.text
    logging.getLogger("t4").removeHandler(h)


def test_the_telegram_channel_posts_to_the_bot_api(monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "_post", lambda url, payload: calls.append((url, payload)))
    send = alerts.make_sender({"AEGIS_ALERT_TELEGRAM_BOT_TOKEN": "123:abc", "AEGIS_ALERT_TELEGRAM_CHAT_ID": "42"})
    send("hello")
    assert calls == [("https://api.telegram.org/bot123:abc/sendMessage", {"chat_id": "42", "text": "hello"})]
