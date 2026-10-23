import flask
import pytest

import insights
import main

A, B = "a@x.com", "b@y.com"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("PUBLIC_URL", "https://f/run")
    monkeypatch.setenv("ARTICLES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_EMAIL_TO", f"{A},{B}")
    monkeypatch.setenv("RESET_TOKEN", "tok")
    store = insights.LocalStore(str(tmp_path))
    arts = {
        "a1": {"topic": "T1", "answer": "x", "status": "approved", "approved_at": "2026-01-01",
               "revision": 1, "sent_to": {A: "t", B: "t"}},
        "a2": {"topic": "T2", "answer": "x", "status": "approved", "approved_at": "2026-01-02",
               "revision": 1, "sent_to": {A: "t"}},
        "a3": {"topic": "T3", "answer": "x", "status": "pending", "revision": 1},
    }
    store.save(insights.ARTICLES_BLOB, arts)
    store.append_text("feedback/x.txt", "keep")
    return store


def call(method="POST", token="tok", data=None, path="/reset"):
    app = flask.Flask(__name__)
    headers = {"X-Reset-Token": token} if token is not None else {}
    with app.test_request_context(path, method=method, headers=headers, data=data or {}):
        resp = main.aero_india_tracker(flask.request)
    body, status = resp
    return body, status


def backups(store):
    return [f for f in __import__("os").listdir(store._dir) if f.startswith("articles.backup-")]


def test_bad_or_missing_token(env):
    assert call(token="bad")[1] == 401
    assert call(token=None)[1] == 401
    assert env.load(insights.ARTICLES_BLOB, {})["a1"]["sent_to"]


def test_unset_token_disables(env, monkeypatch):
    monkeypatch.delenv("RESET_TOKEN")
    assert call(token="")[1] == 401


def test_get_rejected(env):
    assert call(method="GET")[1] == 405
    assert env.load(insights.ARTICLES_BLOB, {})["a1"]["sent_to"]


def test_reset_all(env):
    body, status = call()
    assert status == 200
    assert body == {"status": "success", "articles_reset": 2, "scope": "all"}
    arts = env.load(insights.ARTICLES_BLOB, {})
    assert all(not a.get("sent_to") for a in arts.values())
    assert arts["a1"]["status"] == "approved" and arts["a1"]["answer"] == "x"
    assert env.read_text("feedback/x.txt") == "keep"
    assert insights.next_insight(A, env)["id"] == "a1"
    assert len(backups(env)) == 1


def test_backup_restorable(env):
    original = env.load(insights.ARTICLES_BLOB, {})
    call()
    name = backups(env)[0]
    assert env.load(name, {}) == original


def test_reset_one_case_insensitive(env):
    body, status = call(data={"email": "A@X.com"})
    assert status == 200 and body["scope"] == "recipient" and body["articles_reset"] == 2
    arts = env.load(insights.ARTICLES_BLOB, {})
    assert A not in arts["a1"]["sent_to"] and B in arts["a1"]["sent_to"]
    assert not arts["a2"]["sent_to"]
    assert "a@x.com" not in str(body)


def test_unknown_email(env):
    original = env.load(insights.ARTICLES_BLOB, {})
    body, status = call(data={"email": "no@one.com"})
    assert status == 404 and body == {"status": "not_found"}
    assert env.load(insights.ARTICLES_BLOB, {}) == original
    assert not backups(env)


def test_disabled_when_insights_off(env, monkeypatch):
    monkeypatch.delenv("APPROVAL_SECRET")
    assert call()[0] == {"status": "disabled"}
