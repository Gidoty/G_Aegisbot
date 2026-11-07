"""Operator command line for G-Aegisbot.

    python -m g_aegisbot.admin <command> [options]

Secrets are never accepted as command-line arguments (they would land in shell history and the process
list) and are never printed. API keys are read from a hidden prompt, or from stdin when piped.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from g_aegisbot import audit
from g_aegisbot.services import extend_subscription, live_switch_problems
from g_aegisbot.config import DEFAULT_COINS, DEFAULT_LEVERAGE, MAX_LEVERAGE, MIN_LEVERAGE
from g_aegisbot.db.models import UserProfile
from g_aegisbot.db.repository import ProfileLimitError, ProfileRepository
from g_aegisbot.db.trading_models import OpenTrade
from g_aegisbot.engine.controls import GLOBAL, ControlStore
from g_aegisbot.security.crypto import SecretBox, generate_key


class AdminError(Exception):
    """A problem the operator can fix. Printed without a traceback."""


def _money(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number")
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m g_aegisbot.admin", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create missing tables (safe to repeat)")
    sub.add_parser("gen-key", help="print a new encryption key for AEGIS_MASTER_KEYS")
    sub.add_parser("migrate", help="bring the database schema up to date (required on Postgres)")
    sub.add_parser("list", help="one line per profile")
    sub.add_parser("status", help="kill-switch state, profiles, open trades")
    sub.add_parser("rotate-keys", help="re-encrypt every stored API key with the newest master key")
    a = sub.add_parser("audit", help="recent administrative actions")
    a.add_argument("--limit", type=int, default=20)

    c = sub.add_parser("create-user", help="add a profile")
    c.add_argument("user")
    c.add_argument("--admin", action="store_true", help="administrator: exempt from the subscription check")
    c.add_argument("--mode", choices=["testnet", "live"], default="testnet")
    c.add_argument("--cap", type=_money, help="max wallet cap in USDT (live accounts stay blocked until this is set)")
    c.add_argument("--balance", type=_money, help="paper-trading balance in USDT")
    c.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE)
    c.add_argument("--coins", nargs="+", metavar="SYMBOL")
    c.add_argument("--days", type=int, default=0, help="activate a subscription for this many days")

    s = sub.add_parser("show", help="show one profile (no secrets)")
    s.add_argument("user")

    k = sub.add_parser("set-keys", help="store the user's Binance API key and secret (hidden prompt)")
    k.add_argument("user")
    k.add_argument("--verify", action="store_true",
                   help="ask Binance what the key can do, and refuse keys that can withdraw")

    for name, arg, kw in (("set-cap", "usdt", {"type": _money}), ("set-balance", "usdt", {"type": _money}),
                          ("set-leverage", "x", {"type": int})):
        x = sub.add_parser(name)
        x.add_argument("user")
        x.add_argument(arg, **kw)

    m = sub.add_parser("set-mode", help="switch between paper (testnet) and live trading")
    m.add_argument("user")
    m.add_argument("mode", choices=["testnet", "live"])
    m.add_argument("--yes-real-money", action="store_true", help="required to switch an account to live")

    co = sub.add_parser("set-coins", help="replace the pairs this user trades (max 15)")
    co.add_argument("user")
    co.add_argument("symbols", nargs="+")

    e = sub.add_parser("extend", help="add subscription days (starts from now if expired)")
    e.add_argument("user")
    e.add_argument("--days", type=int, required=True)
    ex = sub.add_parser("expire", help="end a subscription now (open positions keep being managed)")
    ex.add_argument("user")

    kill = sub.add_parser("kill", help="EMERGENCY: stop new trades, optionally close everything")
    kill.add_argument("--user", help="limit to one account (default: everyone)")
    kill.add_argument("--flatten", action="store_true", help="also close all positions and cancel their stops")
    kill.add_argument("--reason", default="", help="shown in the logs")
    bl = sub.add_parser("bind-login", help="attach a Supabase user id to a profile (required for admin profiles)")
    bl.add_argument("user", help="the profile (user id) to bind")
    bl.add_argument("supabase_user_id", help="the user's id from Supabase: Authentication > Users > User UID")

    sub.add_parser("pending-entries", help="list entry orders whose outcome is unresolved (they block that user's new trades)")
    re_ = sub.add_parser("resolve-entry", help="reconcile ONE unresolved entry order against the exchange and, only if the "
                                               "evidence allows and you confirm, resolve it (audited)")
    re_.add_argument("user")
    re_.add_argument("client_order_id", help="the exact client order id shown by pending-entries")
    re_.add_argument("--symbol", required=True, help="the exact symbol of that order, e.g. BTC/USDT:USDT")
    re_.add_argument("--reason", help="why you accept the outcome (at least 12 characters, no secrets); required with --confirm")
    re_.add_argument("--confirm", action="store_true",
                     help="I have read the evidence above and accept this outcome. Without it nothing is changed")

    res = sub.add_parser("resume", help="lift a pause or flatten")
    res.add_argument("--user", help="limit to one account (default: the global switch)")
    return p


class Admin:
    def __init__(self, session_factory, box: SecretBox | None, *, out=print, ask_secret=getpass.getpass,
                 stdin=sys.stdin, verify=None, now=None, actor: str | None = None, adapter_factory=None):
        self.adapter_factory = adapter_factory       # profile -> exchange adapter used for reconciliation (tests inject)
        self.sf, self.box, self.out, self.ask_secret, self.stdin = session_factory, box, out, ask_secret, stdin
        self.verify, self.now = verify, now or (lambda: datetime.now(timezone.utc))
        self.actor = actor or os.environ.get("USER") or "cli"
        self.controls = ControlStore(session_factory)

    # ------------------------------------------------------------ helpers
    def _repo(self, session) -> ProfileRepository:
        return ProfileRepository(session, self.box)

    def _need_box(self) -> SecretBox:
        if self.box is None:
            raise AdminError("AEGIS_MASTER_KEYS is not set. Run 'gen-key' and put the result in .env.")
        return self.box

    def _get(self, session, user: str) -> UserProfile:
        p = self._repo(session).get(user)
        if p is None:
            raise AdminError(f"No profile named '{user}'. Use 'list' to see profiles.")
        return p

    def _audit(self, action: str, target: str | None = None, detail: str | None = None) -> None:
        audit.record(self.sf, self.actor, action, target, detail)

    # ------------------------------------------------------------ commands
    def init_db(self, a) -> None:
        from g_aegisbot.db import init_db
        engine = self.sf.kw["bind"]
        if engine.dialect.name != "sqlite":
            raise AdminError("init-db is for local SQLite only. On Postgres run 'migrate', which applies the "
                             "versioned schema and the row-level-security lockdown.")
        init_db(engine)
        self.out("Database tables are in place.")

    def migrate(self, a) -> None:
        from g_aegisbot.migrate import upgrade
        rev = upgrade(self.sf.kw["bind"])
        self.out(f"Database schema is at revision {rev}.")

    def gen_key(self, a) -> None:
        self.out(generate_key())
        self.out("Put this in .env as AEGIS_MASTER_KEYS=<key>. Keep a copy somewhere safe: without it the stored "
                 "API keys cannot be decrypted. To rotate, put the new key first and the old one after a comma, "
                 "then run 'rotate-keys'.")

    def create_user(self, a) -> None:
        fields = {"trading_mode": a.mode, "leverage": a.leverage}
        if a.cap is not None:
            fields["max_wallet_cap_usdt"] = a.cap
        if a.balance is not None:
            fields["virtual_balance"] = a.balance
        if a.coins:
            fields["active_coins"] = a.coins
        if a.mode == "live" and a.cap is None:
            self.out("Note: live accounts have a wallet cap of 0 until you run set-cap, so they cannot trade yet.")
        try:
            with self.sf() as s:
                repo = self._repo(s)
                if repo.get(a.user):
                    raise AdminError(f"Profile '{a.user}' already exists.")
                p = repo.create(a.user, is_admin=a.admin, **fields)
                if a.days:
                    repo.set_subscription(p, "active", self.now() + timedelta(days=a.days))
        except ProfileLimitError as exc:
            raise AdminError(str(exc))
        except ValueError as exc:
            raise AdminError(str(exc))
        self._audit("create-user", a.user, f"mode={a.mode} admin={a.admin} days={a.days}")
        self.out(f"Created '{a.user}' ({a.mode}). Next: set-keys (live only), set-cap, extend --days N.")

    def list(self, a) -> None:
        with self.sf() as s:
            rows = self._repo(s).list_all()
            self.out(f"{'USER':<18}{'MODE':<9}{'LEV':<5}{'CAP USDT':<12}{'KEYS':<8}{'SUBSCRIPTION':<22}ROLE")
            for p in rows:
                self.out(f"{p.user_id:<18}{p.trading_mode:<9}{p.leverage:<5}{float(p.max_wallet_cap_usdt):<12.2f}"
                         f"{'stored' if p.has_credentials else 'none':<8}{self._sub_text(p):<22}"
                         f"{'admin' if p.is_admin else 'user'}")
            self.out(f"{len(rows)} profile(s)")

    def _sub_text(self, p: UserProfile) -> str:
        if p.is_admin:
            return "n/a (admin)"
        if p.subscription_is_active(self.now()):
            return f"active to {p.subscription_expires_at:%Y-%m-%d}"
        return "expired"

    def show(self, a) -> None:
        with self.sf() as s:
            p = self._get(s, a.user)
            t = p.daily_loss_tracker or {}
            self.out(f"user:          {p.user_id}{' (admin)' if p.is_admin else ''}")
            self.out(f"mode:          {p.trading_mode}   leverage {p.leverage}x   market {p.market_type}")
            self.out(f"wallet cap:    {float(p.max_wallet_cap_usdt):,.2f} USDT   paper balance {float(p.virtual_balance):,.2f}")
            self.out(f"api keys:      {'stored (encrypted)' if p.has_credentials else 'not stored'}")
            self.out(f"subscription:  {self._sub_text(p)}")
            self.out(f"pairs ({len(p.active_coins)}): {', '.join(p.active_coins)}")
            self.out(f"today:         start equity {t.get('start_equity')}, pnl {t.get('pnl')}, "
                     f"breaker {'TRIPPED' if t.get('breaker_tripped') else 'ok'} ({t.get('date')})")
            ctrl = self.controls.effective(p.user_id)
            if ctrl.paused:
                self.out(f"kill switch:   {'FLATTEN' if ctrl.flatten else 'PAUSED'} ({ctrl.describe()})")

    def set_keys(self, a) -> None:
        box = self._need_box()
        if self.stdin is not None and not getattr(self.stdin, "isatty", lambda: True)():
            key, secret = self.stdin.readline().strip(), self.stdin.readline().strip()
        else:
            key, secret = self.ask_secret("API key: ").strip(), self.ask_secret("API secret: ").strip()
        if not key or not secret:
            raise AdminError("Both the API key and the secret are required.")
        with self.sf() as s:
            self._get(s, a.user)                               # fail before asking Binance
        if a.verify:
            check = (self.verify or _default_verify)(key, secret)
            for w in check.warnings:
                self.out(f"Warning: {w}")
            if not check.ok:
                for pr in check.problems:
                    self.out(f"REFUSED: {pr}")
                raise AdminError("The key was not stored.")
            self.out("Key check passed: reading and futures enabled, withdrawals disabled.")
        else:
            self.out("Warning: the key was not verified. Re-run with --verify to confirm withdrawals are disabled.")
        with self.sf() as s:
            p = self._get(s, a.user)
            ProfileRepository(s, box).set_credentials(p, key, secret)
        self._audit("set-keys", a.user, "verified" if a.verify else "not verified")
        self.out(f"Stored encrypted API keys for '{a.user}'.")

    def _update(self, user: str, action: str, detail: str, fn) -> None:
        try:
            with self.sf() as s:
                p = self._get(s, user)
                fn(p)
                s.commit()
        except ValueError as exc:
            raise AdminError(str(exc))
        self._audit(action, user, detail)
        self.out(f"{action}: {user} {detail}")

    def set_cap(self, a) -> None:
        self._update(a.user, "set-cap", f"-> {a.usdt} USDT", lambda p: setattr(p, "max_wallet_cap_usdt", a.usdt))

    def set_balance(self, a) -> None:
        self._update(a.user, "set-balance", f"-> {a.usdt} USDT (paper)", lambda p: setattr(p, "virtual_balance", a.usdt))

    def set_leverage(self, a) -> None:
        if not MIN_LEVERAGE <= a.x <= MAX_LEVERAGE:
            raise AdminError(f"Leverage must be between {MIN_LEVERAGE} and {MAX_LEVERAGE}.")
        self._update(a.user, "set-leverage", f"-> {a.x}x", lambda p: setattr(p, "leverage", a.x))

    def set_coins(self, a) -> None:
        self._update(a.user, "set-coins", f"-> {len(a.symbols)} pairs", lambda p: setattr(p, "active_coins", a.symbols))

    def set_mode(self, a) -> None:
        if a.mode == "live":
            with self.sf() as s:
                p = self._get(s, a.user)
                hints = {"no API keys stored": "no API keys stored (run set-keys --verify)",
                         "wallet cap is 0": "wallet cap is 0 (run set-cap)",
                         "real-money confirmation missing": "add --yes-real-money to confirm this account will trade real funds"}
                problems = [hints[x] for x in live_switch_problems(p, a.yes_real_money)]
            if problems:
                raise AdminError("Cannot switch to live: " + "; ".join(problems) + ".")
            self.out("This account will trade REAL funds. The Binance account must be in One-way position mode, "
                     "and the key must not allow withdrawals.")
        self._update(a.user, "set-mode", f"-> {a.mode}", lambda p: setattr(p, "trading_mode", a.mode))

    def extend(self, a) -> None:
        if a.days <= 0:
            raise AdminError("--days must be positive.")

        self._update(a.user, "extend", f"+{a.days} days", lambda p: extend_subscription(p, a.days, self.now()))

    def expire(self, a) -> None:
        def apply(p):
            p.subscription_status, p.subscription_expires_at = "expired", None
        self._update(a.user, "expire", "now", apply)

    def kill(self, a) -> None:
        scope = a.user or GLOBAL
        if a.user:
            with self.sf() as s:
                self._get(s, a.user)
        self.controls.set(scope, paused=True, flatten=a.flatten, reason=a.reason or None, by=self.actor)
        self._audit("kill-flatten" if a.flatten else "kill-pause", scope, a.reason)
        who = a.user or "ALL accounts"
        if a.flatten:
            self.out(f"FLATTEN set for {who}. The runner closes positions and cancels stops within about a minute, "
                     "and nothing re-opens until you run 'resume'. The runner must be running.")
        else:
            self.out(f"PAUSE set for {who}. No new trades. Open positions keep their stops. 'resume' lifts it.")

    def resume(self, a) -> None:
        scope = a.user or GLOBAL
        self.controls.clear(scope)
        self._audit("resume", scope)
        self.out(f"Cleared the switch for {a.user or 'all accounts (global)'}. Trading continues on the next check.")

    def status(self, a) -> None:
        active = self.controls.active()
        self.out("KILL SWITCH: " + ("; ".join(f"{c.scope}={'FLATTEN' if c.flatten else 'PAUSED'}"
                                                f"{' (' + c.reason + ')' if c.reason else ''}" for c in active)
                                    if active else "off"))
        with self.sf() as s:
            n = s.scalar(select(func.count()).select_from(OpenTrade)) or 0
        self.out(f"Open managed trades: {n}")
        self.list(a)

    def rotate_keys(self, a) -> None:
        box = self._need_box()
        count = 0
        with self.sf() as s:
            for p in self._repo(s).list_all():
                if p.has_credentials:
                    p.encrypted_api_key = box.rotate(p.encrypted_api_key)
                    p.encrypted_api_secret = box.rotate(p.encrypted_api_secret)
                    box.decrypt(p.encrypted_api_key), box.decrypt(p.encrypted_api_secret)   # prove they still open
                    count += 1
            s.commit()
        self._audit("rotate-keys", None, f"{count} profile(s)")
        self.out(f"Re-encrypted {count} profile(s) with the newest key. You may now remove old keys from AEGIS_MASTER_KEYS.")

    def bind_login(self, a) -> None:
        """Operator-side binding. Needs shell access to the server (it is not reachable from the API), so it is as
        trusted as create-user. It never overwrites: a bound profile, or an id already bound to another profile, is
        refused; the id must be a UUID; every bind is audited. It cannot check that the UID exists in Supabase, so
        copy it from the dashboard."""
        import uuid

        from g_aegisbot.api.identity import claim_profile
        try:
            sub = str(uuid.UUID(a.supabase_user_id.strip()))          # canonical lowercase form, as in the token's `sub`
        except ValueError:
            raise AdminError("the Supabase user id must be a UUID: copy the User UID from Supabase > Authentication > Users")
        with self.sf() as s:
            p = self._get(s, a.user)
            pid, bound, name = p.id, p.auth_user_id, p.user_id
            other = s.scalar(select(UserProfile.user_id).where(UserProfile.auth_user_id == sub))
        if bound:
            raise AdminError(f"'{name}' is already bound to a login; refusing to overwrite it.")
        if other:
            raise AdminError(f"that Supabase user id is already bound to '{other}'.")
        if not claim_profile(self.sf, pid, sub):
            raise AdminError("could not bind (another change got there first); nothing was changed.")
        self._audit("bind-login", name, f"auth_user_id={sub}")
        self.out(f"'{name}' is now bound to Supabase user {sub}.")
        self.out("Note: this tool cannot check that the UID exists in Supabase (that needs a privileged service-role key, "
                 "which this server deliberately does not hold). Confirm it in Authentication > Users.")

    # ------------------------------------------------------------ unresolved entry orders
    def pending_entries(self, a) -> None:
        from g_aegisbot.engine import journal as jr
        rows = jr.read_unresolved(self.sf)
        if not rows:
            self.out("No unresolved entry orders.")
            return
        now = self.now()
        for uid, pe in rows:
            age = now - pe.submitted_at
            self.out(f"{uid:<24} {pe.client_id:<38} {pe.symbol:<22} sent {pe.submitted_at:%Y-%m-%d %H:%M:%S}Z "
                     f"({int(age.total_seconds() // 60)} min ago){'  DAMAGED RECORD' if pe.corrupt else ''}")
        self.out(f"{len(rows)} unresolved. Each blocks new trades for its user. Reconcile one with "
                 "'resolve-entry <user> <client order id> --symbol <symbol>'.")

    async def _gather_for(self, profile, symbol: str, client_id: str, submitted_at):
        """Build the exchange adapter for this profile, collect the evidence, always close the adapter."""
        from g_aegisbot.engine.reconcile import DEFAULT_MARGIN, gather_evidence
        if self.adapter_factory is not None:
            adapter = self.adapter_factory(profile)
        elif profile.trading_mode == "live":
            from g_aegisbot.exchange import BinanceFuturesAdapter
            box = self._need_box()
            adapter = BinanceFuturesAdapter.from_profile(profile, box.decrypt(profile.encrypted_api_key),
                                                         box.decrypt(profile.encrypted_api_secret))
        else:
            from g_aegisbot.engine.paper import PaperExchange
            adapter = PaperExchange(self.sf, profile.user_id, None)          # paper ledger: the database is the exchange
        try:
            if getattr(adapter, "exchange", None) is not None and hasattr(adapter.exchange, "load_markets") \
                    and not getattr(adapter, "ledger", False):
                await adapter.exchange.load_markets()
            return await gather_evidence(adapter, symbol, client_id, submitted_at, self.now(), DEFAULT_MARGIN)
        finally:
            await adapter.close()

    def resolve_entry(self, a) -> None:
        """Operator resolution of ONE unresolved entry order.

        It always re-reads the exchange and prints what it found. It refuses whenever the evidence is incomplete,
        contradictory or shows exposure. Even with complete evidence nothing changes without --confirm and a reason, and
        a reason can never substitute for evidence. A resolution is appended to the audit log with the operator, the
        time, the reason and a digest of the evidence; the original intent is never touched or removed."""
        from g_aegisbot.engine import journal as jr
        from g_aegisbot.engine.reconcile import decide
        with self.sf() as s:
            profile = self._get(s, a.user)
            s.expunge(profile)
        intent = jr.find_intent(self.sf, profile.user_id, a.client_order_id)
        if intent is None:
            raise AdminError(f"'{profile.user_id}' has no entry order with client id '{a.client_order_id}'. "
                             "Use 'pending-entries' for the exact id.")
        done = jr.find_resolution(self.sf, intent)
        if done is not None:
            raise AdminError(f"that entry order was already resolved by {done.actor} at {done.at:%Y-%m-%d %H:%M:%S}Z; "
                             "nothing to do.")
        if intent.detail and intent.detail != a.symbol:
            raise AdminError(f"symbol mismatch: that order was sent for {intent.detail}, not {a.symbol}.")
        submitted = intent.at if intent.at.tzinfo else intent.at.replace(tzinfo=timezone.utc)
        ev = asyncio.run(self._gather_for(profile, a.symbol, a.client_order_id, submitted))
        dec = decide(ev)
        self.out("Reconciliation evidence (read from the exchange just now):")
        for line in ev.lines():
            self.out("  " + line)
        self.out(f"Verdict: {dec.outcome.upper()}")
        for reason in dec.reasons:
            self.out(f"  - {reason}")

        def refuse(why: str) -> None:
            self._audit("entry-resolve-refused", a.client_order_id, f"{why}; sha={ev.digest()}"[:400])
            raise AdminError(f"REFUSED. {why} The block is unchanged.")

        if dec.outcome == "blocked":
            refuse("The exchange evidence is incomplete, contradictory or shows exposure, so it cannot be cleared.")
        if dec.outcome == "filled" and ev.position_qty > 0 and not ev.stops_valid:
            refuse("The order filled and the position is open without a valid full-size protective stop; protect or close "
                   "the position first (the runner does this on its next check).")
        if not a.confirm:
            self.out("Nothing was changed. If you have read the evidence and accept this outcome, run again with "
                     "--confirm --reason '<why>'.")
            return
        reason = (a.reason or "").strip()
        if len(reason) < 12:
            raise AdminError("--confirm needs --reason of at least 12 characters. A reason is recorded; it never replaces evidence.")
        body = f"operator {self.actor}: {dec.outcome}; sha={ev.digest()}; reason: {reason[:90]}; {ev.summary()}"
        # one atomic conditional INSERT, bound to the original intent row, user and symbol: two operators racing, or a
        # repeated command, cannot write a second resolution
        if not jr.resolve_once(self.sf, intent, symbol=a.symbol, actor=self.actor, body=body):
            raise AdminError("that entry order was resolved by someone else while you were reading the evidence; nothing "
                             "was written.")
        self.out(f"Resolved. Recorded as the operator '{self.actor}' with the evidence digest {ev.digest()}. "
                 "The original entry record is kept; a running bot picks the resolution up on its next check.")

    def audit_cmd(self, a) -> None:
        for r in audit.recent(self.sf, a.limit):
            self.out(f"{r.at:%Y-%m-%d %H:%M:%S}  {r.actor:<10} {r.action:<14} {r.target or '-':<14} {r.detail or ''}")


def _default_verify(key: str, secret: str):
    from g_aegisbot.exchange.key_check import check_api_key
    return asyncio.run(check_api_key(key, secret))


COMMANDS = {
    "init-db": "init_db", "migrate": "migrate", "gen-key": "gen_key", "create-user": "create_user", "list": "list", "show": "show",
    "set-keys": "set_keys", "set-cap": "set_cap", "set-balance": "set_balance", "set-leverage": "set_leverage",
    "set-coins": "set_coins", "set-mode": "set_mode", "extend": "extend", "expire": "expire", "kill": "kill",
    "resume": "resume", "status": "status", "rotate-keys": "rotate_keys", "audit": "audit_cmd", "bind-login": "bind_login",
    "pending-entries": "pending_entries", "resolve-entry": "resolve_entry",
}


def run(argv: list[str], admin: Admin) -> int:
    args = build_parser().parse_args(argv)
    try:
        getattr(admin, COMMANDS[args.cmd])(args)
        return 0
    except AdminError as exc:
        admin.out(f"Error: {exc}")
        return 1


def main(argv: list[str] | None = None) -> None:
    from g_aegisbot.config import get_settings
    from g_aegisbot.db import get_engine, get_session_factory, init_db

    from g_aegisbot.migrate import ensure_schema

    engine = get_engine()
    if not (argv or sys.argv[1:] or [""])[0] == "migrate":
        ensure_schema(engine)
    settings = get_settings()
    box = SecretBox(settings.master_keys) if settings.master_keys else None
    sys.exit(run(sys.argv[1:] if argv is None else argv, Admin(get_session_factory(engine), box)))


if __name__ == "__main__":
    main()
