import json
from datetime import datetime, timezone

import flask
import pytest

import email_templates
import insights
import main

A, B = "a@x.com", "b@y.com"
# 2026-03-10 10:00 IST
NOW = datetime(2026, 3, 10, 4, 30, tzinfo=timezone.utc)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("PUBLIC_URL", "https://f/run")
    monkeypatch.setenv("ARTICLES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_EMAIL_TO", f"{A},{B}")
    monkeypatch.setenv("DIGEST_TOKEN", "tok")
    monkeypatch.delenv("LOW_STOCK_THRESHOLD", raising=False)
    monkeypatch.setattr(main, "GMAIL_ADDRESS", "owner@x.com")
    store = insights.LocalStore(str(tmp_path))
    store.save(insights.TOPICS_BLOB, [{"id": f"a{i}", "topic": f"T{i}"} for i in range(1, 8)])
    arts = {f"a{i}": {"topic": f"T{i}", "answer": "x", "status": "approved",
                      "approved_at": f"2026-01-0{i}", "revision": 1, "sent_to": {}}
            for i in range(1, 5)}
    arts["a5"] = {"topic": "T5", "answer": "x", "status": "pending", "revision": 1}
    # A got a1 and a2 (a2 earlier today IST, a1 yesterday); B got a1 only
    arts["a1"]["sent_to"] = {A: "2026-03-09T05:00:00+00:00", B: "2026-03-09T05:00:00+00:00"}
    arts["a2"]["sent_to"] = {A: "2026-03-10T01:00:00+00:00"}
    store.save(insights.ARTICLES_BLOB, arts)
    return store


def add_feedback(store, email, ts, aid, topic, text):
    store.append_text(f"feedback/{insights.recipient_token(email)}.txt",
                      f"[{ts}] recipient={email} article={aid} topic={topic}\n{text}\n\n")


def test_stock_and_delivery_counts(env):
    d = insights.build_digest([A, B, "new@z.com"], env, NOW)
    assert (d["approved"], d["pending"], d["undrafted"]) == (4, 1, 2)
    a, b, new = d["people"]
    assert (a["received"], a["received_today"], a["remaining"]) == (2, 1, 2)
    assert (b["received"], b["received_today"], b["remaining"]) == (1, 0, 3)
    assert (new["received"], new["remaining"], new["last_sent"]) == (0, 4, None)
    assert a["last_sent"].strftime("%H:%M") == "06:30"


def test_ist_day_boundary(env):
    # 19:00 UTC on the 9th is 00:30 IST on the 10th: counts as today
    arts = env.load(insights.ARTICLES_BLOB, {})
    arts["a3"]["sent_to"] = {A: "2026-03-09T19:00:00+00:00"}
    env.save(insights.ARTICLES_BLOB, arts)
    assert insights.build_digest([A], env, NOW)["people"][0]["received_today"] == 2


def test_feedback_today_and_overall(env):
    add_feedback(env, A, "2026-03-09T10:00:00+00:00", "a1", "T1", "old")
    add_feedback(env, A, "2026-03-10T02:00:00+00:00", "a2", "T2", "line1\n\nline2")
    add_feedback(env, B, "2026-03-10T03:00:00+00:00", "none", "none", "hello")
    d = insights.build_digest([A, B], env, NOW)
    assert [(e["email"], e["text"]) for e in d["new_feedback"]] == [(A, "line1\n\nline2"), (B, "hello")]
    a = d["people"][0]
    assert (a["feedback_total"], a["feedback_today"]) == (2, 1)
    assert [e["text"] for e in a["recent_feedback"]] == ["line1\n\nline2", "old"]


def test_recent_feedback_capped(env):
    for i in range(7):
        add_feedback(env, A, f"2026-03-0{i + 1}T10:00:00+00:00", "a1", "T1", f"f{i}")
    a = insights.build_digest([A], env, NOW)["people"][0]
    assert a["feedback_total"] == 7
    assert [e["text"] for e in a["recent_feedback"]] == ["f6", "f5", "f4", "f3", "f2"]


def test_low_stock_flag_and_subject(env, monkeypatch):
    monkeypatch.setenv("LOW_STOCK_THRESHOLD", "1")
    assert not insights.build_digest([A, B], env, NOW)["low_stock"]
    monkeypatch.setenv("LOW_STOCK_THRESHOLD", "2")
    d = insights.build_digest([A, B], env, NOW)
    assert d["low_stock"]
    subject, text, html = email_templates.render_digest(d)
    assert subject == "Aero India | Daily Insights Digest | 10/03/26 | 2 recipients, 0 new feedback | LOW STOCK"
    assert "LOW STOCK" in text and "Low stock" in html


def test_default_threshold_is_three(env):
    d = insights.build_digest([A, B], env, NOW)
    assert d["threshold"] == 3 and d["low_stock"]


def test_empty_day_renders(env, monkeypatch):
    monkeypatch.setenv("LOW_STOCK_THRESHOLD", "1")
    d = insights.build_digest([A], env, NOW)
    subject, text, html = email_templates.render_digest(d)
    assert "0 new feedback" in subject and "LOW STOCK" not in subject
    assert "None." in text and "No feedback yet." in html


def test_html_escaped(env):
    add_feedback(env, A, "2026-03-10T02:00:00+00:00", "a1", "<b>T</b>", "<script>x</script>")
    _, text, html = email_templates.render_digest(insights.build_digest([A], env, NOW))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>T</b>" not in html


def test_long_feedback_truncated_in_history_only(env):
    add_feedback(env, A, "2026-03-09T10:00:00+00:00", "a1", "T1", "y" * 500)
    _, _, html = email_templates.render_digest(insights.build_digest([A], env, NOW))
    assert "y" * 300 + "..." in html and "y" * 301 not in html


def test_unparseable_feedback_skipped(env):
    env.append_text(f"feedback/{insights.recipient_token(A)}.txt", "garbage\n[bad] recipient\n")
    assert insights.build_digest([A], env, NOW)["people"][0]["feedback_total"] == 0


def call(path="/digest", **kw):
    with flask.Flask(__name__).test_request_context(path, **kw):
        return main.aero_india_tracker(flask.request)


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(main, "send_email", lambda to, s, t, h: out.append((to, s, t)) or True)
    return out


@pytest.mark.parametrize("kw", [{}, {"query_string": {"token": "bad"}}, {"headers": {"X-Digest-Token": "bad"}}])
def test_bad_token_rejected(env, sent, kw):
    assert call(**kw)[1] == 401
    assert sent == []


def test_missing_server_token_rejects(env, sent, monkeypatch):
    monkeypatch.delenv("DIGEST_TOKEN")
    assert call(query_string={"token": ""})[1] == 401
    assert sent == []


@pytest.mark.parametrize("kw", [{"headers": {"X-Digest-Token": "tok"}}, {"query_string": {"token": "tok"}}])
def test_digest_sent_to_owner_only_without_state_change(env, sent, kw):
    before = json.dumps(env.load(insights.ARTICLES_BLOB, {}), sort_keys=True)
    body, status = call(**kw)
    assert status == 200 and body["sent"] and body["recipients"] == 2
    assert [to for to, _, _ in sent] == ["owner@x.com"]
    assert json.dumps(env.load(insights.ARTICLES_BLOB, {}), sort_keys=True) == before
    assert A not in json.dumps(body)


def test_digest_disabled_without_insights(env, sent, monkeypatch):
    monkeypatch.setenv("INCLUDE_INSIGHTS", "false")
    assert call(headers={"X-Digest-Token": "tok"})[0]["status"] == "disabled"
    assert sent == []


def test_other_paths_still_404(env, sent):
    assert call("/other")[1] == 404
