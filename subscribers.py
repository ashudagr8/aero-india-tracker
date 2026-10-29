"""Self-service email subscriptions: double opt-in, Turnstile-protected, throttled per address.

`subscribers.json` is keyed by HMAC-SHA256(lowercased email); addresses are Fernet-encrypted at rest.
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

import email_templates as tpl
import insights

SUBSCRIBERS_BLOB = "subscribers.json"
PENDING_TTL = timedelta(hours=24)
THROTTLE_WINDOW = timedelta(hours=24)
MAX_EMAIL_LEN = 254
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                       r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")

# Outcomes (internal only; the user always gets the same neutral page for these)
SENT, THROTTLED, ALREADY, FULL, SEND_FAILED, INVALID = (
    "sent", "throttled", "already", "full", "send_failed", "invalid")


def max_subscribers() -> int:
    try:
        return max(0, int(os.environ.get("MAX_SUBSCRIBERS", "50")))
    except ValueError:
        return 50


def turnstile_site_key() -> str:
    return os.environ.get("TURNSTILE_SITE_KEY", "")


def enabled() -> bool:
    """Fails closed: every required secret and a store must be configured."""
    required = ("SUBSCRIBER_ENC_KEY", "SUBSCRIBER_HASH_KEY", "APPROVAL_SECRET",
                "TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY")
    if not all(os.environ.get(k) for k in required):
        return False
    if not (os.environ.get("ARTICLES_DIR") or os.environ.get("ARTICLES_BUCKET")):
        return False
    return os.environ.get("PUBLIC_URL", "").startswith("https://")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> bool:
    return 0 < len(email) <= MAX_EMAIL_LEN and bool(_EMAIL_RE.match(email))


def email_hash(email: str) -> str:
    key = os.environ["SUBSCRIBER_HASH_KEY"].encode()
    return hmac.new(key, normalize_email(email).encode(), hashlib.sha256).hexdigest()


def _fernet():
    from cryptography.fernet import Fernet, MultiFernet  # lazy: a missing library must not stop the tracker
    keys = [k.strip() for k in os.environ["SUBSCRIBER_ENC_KEY"].split(",") if k.strip()]
    return MultiFernet([Fernet(k) for k in keys])


def _encrypt(email: str) -> str:
    return _fernet().encrypt(email.encode()).decode()


def _decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


# ---- Turnstile ---------------------------------------------------------------------------------

def verify_turnstile(token: str, remote_ip: str = "") -> bool:
    secret = os.environ.get("TURNSTILE_SECRET_KEY", "")
    if not secret or not token:
        return False
    data = {"secret": secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        resp = requests.post(TURNSTILE_URL, data=data, timeout=5)
        return resp.status_code == 200 and resp.json().get("success") is True
    except Exception as exc:
        print(f"Turnstile verification failed: {type(exc).__name__}")
        return False


# ---- signed confirmation links -----------------------------------------------------------------

def _sign(purpose: str, sub_id: str) -> str:
    msg = f"{purpose}:{sub_id}".encode()
    return hmac.new(os.environ["APPROVAL_SECRET"].encode(), msg, hashlib.sha256).hexdigest()


def confirm_link(sub_id: str) -> str:
    query = urlencode({"action": "confirm", "id": sub_id, "sig": _sign("confirm", sub_id)})
    return f"{os.environ['PUBLIC_URL'].rstrip('/')}/subscribe?{query}"


# ---- state -------------------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _within(ts, window, now) -> bool:
    parsed = _parse(ts)
    return parsed is not None and now - parsed < window


def _purge(entries: dict, now: datetime) -> bool:
    """Drops expired pending sign-ups and expired throttle-only reservations."""
    changed = False
    for key, rec in list(entries.items()):
        if rec["status"] == "pending":
            expired = not _within(rec.get("created_at"), PENDING_TTL, now)
        elif rec["status"] == "reserved":
            expired = not _within(rec.get("last_sent_at"), THROTTLE_WINDOW, now)
        else:
            expired = False
        if expired:
            del entries[key]
            changed = True
    return changed


def _active_count(entries: dict) -> int:
    return sum(1 for r in entries.values() if r["status"] in ("pending", "confirmed"))


def confirmed_emails(store=None) -> list[str]:
    if not enabled():
        return []
    store = store or insights.default_store()
    emails = []
    for rec in store.load(SUBSCRIBERS_BLOB, {}).values():
        if rec.get("status") != "confirmed":
            continue
        try:
            emails.append(_decrypt(rec["email_enc"]))
        except Exception as exc:
            print(f"Subscriber decrypt failed: {type(exc).__name__}")
    return emails


def counts(store=None) -> dict:
    store = store or insights.default_store()
    entries = store.load(SUBSCRIBERS_BLOB, {})
    return {"confirmed": sum(1 for r in entries.values() if r["status"] == "confirmed"),
            "pending": sum(1 for r in entries.values() if r["status"] == "pending"),
            "limit": max_subscribers()}


# ---- subscribe / confirm -----------------------------------------------------------------------

def _confirmation_email(link: str):
    subject = "Confirm your Aero India tracker subscription"
    text = f"Confirm your subscription: {link}\n\nIf you did not ask for this, ignore this email."
    body = (f"<p>Please confirm your subscription to the Aero India tracker updates.</p>"
            f"{tpl._button('Confirm subscription', link)}"
            "<p>If you did not ask for this, ignore this email; nothing will be sent to you.</p>")
    return subject, text, tpl._wrap(subject, tpl._header("Confirm subscription", ""), body,
                                    "You received this because this address was entered on our subscribe page.")


def _already_email():
    subject = "You are already subscribed to the Aero India tracker"
    text = "This address is already subscribed. No action is needed."
    body = "<p>This address is already subscribed. No action is needed.</p>"
    return subject, text, tpl._wrap(subject, tpl._header("Already subscribed", ""), body,
                                    "You received this because this address was entered on our subscribe page.")


def request_subscription(email: str, send_email, extra_recipients=(), store=None, now=None) -> str:
    """Returns an internal outcome; the HTTP layer shows the same neutral page for all but INVALID."""
    email = normalize_email(email)
    if not validate_email(email):
        return INVALID
    store = store or insights.default_store()
    now = now or _now()
    key = email_hash(email)
    owner_addresses = {a.strip().lower() for a in extra_recipients}

    with insights.STATE_LOCK:
        entries = store.load(SUBSCRIBERS_BLOB, {})
        changed = _purge(entries, now)
        rec = entries.get(key)

        if rec and _within(rec.get("last_sent_at"), THROTTLE_WINDOW, now):
            outcome = THROTTLED
        elif rec and rec["status"] == "confirmed" or email in owner_addresses:
            rec = rec or entries.setdefault(key, {"id": secrets.token_urlsafe(16), "status": "reserved",
                                                  "created_at": now.isoformat()})
            subject, text, html = _already_email()
            if send_email(email, subject, text, html):
                rec["last_sent_at"] = now.isoformat()
                outcome = ALREADY
            else:
                outcome = SEND_FAILED
            changed = True
        elif _active_count(entries) >= max_subscribers():
            outcome = FULL
        else:
            sub_id = (rec or {}).get("id") or secrets.token_urlsafe(16)
            subject, text, html = _confirmation_email(confirm_link(sub_id))
            if send_email(email, subject, text, html):
                entries[key] = {"id": sub_id, "email_enc": _encrypt(email), "status": "pending",
                                "created_at": now.isoformat(), "last_sent_at": now.isoformat()}
                outcome = SENT
                changed = True
            else:
                outcome = SEND_FAILED

        if changed:
            store.save(SUBSCRIBERS_BLOB, entries)
    return outcome


def confirm(sub_id: str, sig: str, store=None, now=None) -> str:
    """Returns 'confirmed', 'invalid', 'expired' or 'full'. Idempotent."""
    if not sub_id or not hmac.compare_digest(_sign("confirm", sub_id), sig or ""):
        return "invalid"
    store = store or insights.default_store()
    now = now or _now()
    with insights.STATE_LOCK:
        entries = store.load(SUBSCRIBERS_BLOB, {})
        changed = _purge(entries, now)
        rec = next((r for r in entries.values() if r["id"] == sub_id), None)
        if rec is None:
            result = "expired"
        elif rec["status"] == "confirmed":
            result = "confirmed"
        elif rec["status"] != "pending":
            result = "invalid"
        elif sum(1 for r in entries.values() if r["status"] == "confirmed") >= max_subscribers():
            result = "full"
        else:
            rec["status"] = "confirmed"
            rec["confirmed_at"] = now.isoformat()
            result, changed = "confirmed", True
        if changed:
            store.save(SUBSCRIBERS_BLOB, entries)
    return result


# ---- unsubscribe -------------------------------------------------------------------------------

def unsubscribe_link(email: str, store=None) -> str | None:
    """Per-recipient link (opaque id + HMAC, no email). None for non-subscribers such as the owner."""
    if not enabled():
        return None
    store = store or insights.default_store()
    rec = store.load(SUBSCRIBERS_BLOB, {}).get(email_hash(email))
    if not rec or rec.get("status") != "confirmed":
        return None
    query = urlencode({"id": rec["id"], "sig": _sign("unsub", rec["id"])})
    return f"{os.environ['PUBLIC_URL'].rstrip('/')}/unsubscribe?{query}"


def valid_unsubscribe(sub_id: str, sig: str) -> bool:
    return bool(sub_id) and hmac.compare_digest(_sign("unsub", sub_id), sig or "")


def unsubscribe(sub_id: str, sig: str, store=None) -> bool:
    """Hard-deletes the record and the recipient's delivery history. Idempotent; False on a bad signature."""
    if not valid_unsubscribe(sub_id, sig):
        return False
    store = store or insights.default_store()
    with insights.STATE_LOCK:
        entries = store.load(SUBSCRIBERS_BLOB, {})
        key, rec = next(((k, r) for k, r in entries.items() if r["id"] == sub_id), (None, None))
        if rec is None:
            return True
        email = None
        if rec.get("email_enc"):
            try:
                email = _decrypt(rec["email_enc"])
            except Exception as exc:
                print(f"Subscriber decrypt failed: {type(exc).__name__}")
        del entries[key]
        store.save(SUBSCRIBERS_BLOB, entries)
        if email:
            insights.forget_recipient(email, store)
    return True


def admin_remove(email: str, store=None) -> bool:
    """Removes a subscriber by address (admin route). Returns whether one existed."""
    store = store or insights.default_store()
    key = email_hash(email)
    with insights.STATE_LOCK:
        entries = store.load(SUBSCRIBERS_BLOB, {})
        if key not in entries:
            return False
        del entries[key]
        store.save(SUBSCRIBERS_BLOB, entries)
        insights.forget_recipient(normalize_email(email), store)
    return True
