import os
from urllib.parse import parse_qsl, urlparse

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
    monkeypatch.setattr(main, "GMAIL_ADDRESS", "owner@x.com")
    store = insights.LocalStore(str(tmp_path))
    store.save(insights.ARTICLES_BLOB, {"art1": {
        "topic": "Stalls <b>", "answer": "Body", "status": "approved",
        "approved_at": "2026-01-01", "revision": 1}})
    return tmp_path


def params(link):
    return dict(parse_qsl(urlparse(link).query))


def post(p, text):
    with flask.Flask(__name__).test_request_context("/", method="POST", data={**p, "feedback": text}):
        return main.aero_india_tracker(flask.request)


def get(p):
    with flask.Flask(__name__).test_request_context("/", query_string=p):
        return main.aero_india_tracker(flask.request)


def test_link_in_each_email_with_article_id(env, monkeypatch):
    sent = {}
    monkeypatch.setattr(main, "send_email", lambda to, s, t, h: sent.setdefault(to, t) or True)
    main.notify_all("now", "No", "No", True)
    for to in (A, B):
        assert "action=feedback" in sent[to] and "a=art1" in sent[to]
        assert to not in sent[to].split("Send feedback:")[1]
    # nothing left to send: article is none
    sent.clear()
    main.notify_all("now", "No", "No", True)
    assert "a=none" in sent[A]


def test_separate_files_and_article_capture(env):
    pa = params(insights.recipient_feedback_link(A, "art1"))
    pb = params(insights.recipient_feedback_link(B, None))
    assert get(pa)[1] == 200
    assert post(pa, "from a")[1] == 200
    assert post(pb, "from b")[1] == 200
    fa = (env / "feedback" / f"{insights.recipient_token(A)}.txt").read_text()
    fb = (env / "feedback" / f"{insights.recipient_token(B)}.txt").read_text()
    assert "recipient=a@x.com article=art1 topic=Stalls <b>" in fa and "from a" in fa
    assert "recipient=b@y.com article=none topic=none" in fb and "from a" not in fb
    assert len(os.listdir(env / "feedback")) == 2


def test_tampering_rejected(env):
    p = params(insights.recipient_feedback_link(A, "art1"))
    assert post({**p, "a": "none"}, "x")[1] == 403
    assert post({**p, "sig": "0" * 64}, "x")[1] == 403
    assert post({**p, "r": insights.recipient_token(B)}, "x")[1] == 403
    assert not (env / "feedback").exists()


def test_too_long_and_escaping(env):
    p = params(insights.recipient_feedback_link(A, "art1"))
    assert post(p, "x" * (insights.FEEDBACK_MAX_CHARS + 1))[1] == 413
    p2 = {**p, "a": 'x"><script>'}
    p2["sig"] = insights._sign_recipient(p["r"], p2["a"])
    html, status = get(p2)[:2]
    assert status == 200 and "<script>" not in html and "&lt;script&gt;" in html
