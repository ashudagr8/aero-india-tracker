import flask
import pytest
from cryptography.fernet import Fernet

import insights
import main
import maintenance
import subscribers as sub

OWNER = "owner@x.com"
SUB = "sub@x.com"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("PUBLIC_URL", "https://f/run")
    monkeypatch.setenv("ARTICLES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_EMAIL_TO", OWNER)
    monkeypatch.setenv("RUN_TOKEN", "run-tok")
    monkeypatch.setenv("MAINTENANCE_TOKEN", "mnt-tok")
    monkeypatch.setenv("SUBSCRIBER_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SUBSCRIBER_HASH_KEY", "h")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")
    monkeypatch.setattr(main, "GMAIL_ADDRESS", OWNER)
    sent = []
    monkeypatch.setattr(main, "send_email",
                        lambda to, s, t, h, headers=None: sent.append((to, s)) or True)

    class Graph:
        def invoke(self, inputs, config=None):
            return {"answer": "Verdict: No", "text_changed": False, "webpage_text": "x"}
    monkeypatch.setattr(main, "app", Graph())
    store = insights.LocalStore(str(tmp_path))
    store.save(insights.ARTICLES_BLOB, {
        "a1": {"topic": "T1", "answer": "A1", "status": "approved",
               "approved_at": "2026-01-01", "revision": 1}})
    return sent


def add_subscriber(email=SUB):
    store = insights.default_store()
    entries = store.load(sub.SUBSCRIBERS_BLOB, {})
    entries[sub.email_hash(email)] = {
        "id": "subid1", "email_enc": sub._encrypt(email), "status": "confirmed",
        "confirmed_at": "2026-01-01T00:00:00+00:00"}
    store.save(sub.SUBSCRIBERS_BLOB, entries)


def call_mnt(path="/maintenance", method="GET", token="mnt-tok", data=None):
    headers = {"X-Maintenance-Token": token} if token is not None else {}
    with flask.Flask(__name__).test_request_context(path, method=method, headers=headers,
                                                    data=data or {}):
        return main.aero_india_tracker(flask.request)


def call_run():
    with flask.Flask(__name__).test_request_context("/", headers={"X-Run-Token": "run-tok"}):
        return main.aero_india_tracker(flask.request)


# ---- module -----------------------------------------------------------------------------------

def test_state_defaults_to_not_paused(env):
    assert maintenance.state() == {"paused": False}
    assert not maintenance.is_paused()


def test_set_paused_roundtrip_with_timestamps(env):
    state = maintenance.set_paused(True)
    assert state["paused"] is True
    assert state["paused_at"] == state["updated_at"]
    assert maintenance.is_paused()
    state = maintenance.set_paused(False)
    assert state["paused"] is False
    assert state["resumed_at"] == state["updated_at"]
    assert not maintenance.is_paused()
    # persisted, with history kept
    saved = insights.default_store().load(maintenance.STATE_BLOB, {})
    assert saved["paused"] is False and saved["paused_at"] and saved["resumed_at"]


def test_set_paused_idempotent(env):
    first = maintenance.set_paused(True)
    second = maintenance.set_paused(True)
    assert second["paused"] is True and first["paused_at"]


# ---- route ------------------------------------------------------------------------------------

@pytest.mark.parametrize("token", [None, "wrong", ""])
def test_route_requires_token(env, token):
    assert call_mnt(token=token)[1] == 401
    assert call_mnt(method="POST", token=token, data={"action": "pause"})[1] == 401
    assert not maintenance.is_paused()


def test_route_disabled_when_token_unset(env, monkeypatch):
    monkeypatch.delenv("MAINTENANCE_TOKEN")
    assert call_mnt(token="")[1] == 401


def test_get_returns_status(env):
    body, status = call_mnt()
    assert status == 200
    assert body["status"] == "success" and body["maintenance"]["paused"] is False


def test_post_bad_action_rejected(env):
    assert call_mnt(method="POST", data={"action": "bogus"})[1] == 400
    assert call_mnt(method="POST")[1] == 400
    assert not maintenance.is_paused()


def test_post_wrong_method_rejected(env):
    assert call_mnt(method="PUT", data={"action": "pause"})[1] == 405


def test_pause_resume_via_route(env):
    body, status = call_mnt(method="POST", data={"action": "pause"})
    assert status == 200 and body["maintenance"]["paused"] is True
    assert call_mnt()[0]["maintenance"]["paused"] is True
    body, status = call_mnt(method="POST", data={"action": "resume"})
    assert status == 200 and body["maintenance"]["paused"] is False
    assert call_mnt()[0]["maintenance"]["paused"] is False


# ---- run behavior ------------------------------------------------------------------------------

def test_paused_run_skips_subscribers_only(env):
    add_subscriber()
    maintenance.set_paused(True)
    body, status = call_run()
    assert status == 200 and body["maintenance"] == "paused"
    recipients = [to for to, _ in env]
    assert OWNER in recipients and SUB not in recipients
    # subscriber delivery history untouched: nothing marked sent
    arts = insights.default_store().load(insights.ARTICLES_BLOB, {})
    assert SUB not in arts["a1"].get("sent_to", {})


def test_active_run_notifies_subscribers(env):
    add_subscriber()
    body, status = call_run()
    assert status == 200 and body["maintenance"] == "active"
    recipients = [to for to, _ in env]
    assert OWNER in recipients and SUB in recipients
    arts = insights.default_store().load(insights.ARTICLES_BLOB, {})
    assert SUB in arts["a1"].get("sent_to", {})


def test_resume_restores_subscriber_notifications(env):
    add_subscriber()
    maintenance.set_paused(True)
    call_run()
    assert SUB not in [to for to, _ in env]
    maintenance.set_paused(False)
    call_run()
    assert SUB in [to for to, _ in env]
