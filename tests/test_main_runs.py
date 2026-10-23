import threading
import time

import flask
import pytest

import insights
import main
from tests.test_insights import FakeStore


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("PUBLIC_URL", "https://f/run")
    monkeypatch.setenv("ARTICLES_DIR", str(tmp_path))
    store = insights.LocalStore(str(tmp_path))
    store.save(insights.TOPICS_BLOB, [{"id": f"t{i}", "topic": f"T{i}"} for i in range(5)])
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "a@x.com")
    monkeypatch.setattr(main, "GMAIL_ADDRESS", "owner@x.com")
    sent = []
    monkeypatch.setattr(main, "send_email", lambda to, s, t, h: sent.append(s) or True)

    def slow_llm(prompt, temperature=0):
        time.sleep(0.05)
        return "Draft"
    monkeypatch.setattr(main, "call_llm", slow_llm)

    class Graph:
        def invoke(self, inputs, config=None):
            return {"answer": "Verdict: No", "text_changed": False, "webpage_text": "x"}
    monkeypatch.setattr(main, "app", Graph())
    return sent


def call(path="/"):
    with flask.Flask(__name__).test_request_context(path):
        return main.aero_india_tracker(flask.request)


def test_favicon_does_not_run(setup):
    assert call("/favicon.ico")[1] == 404
    assert setup == []


def test_first_run_sends_three_reviews_and_one_notification(setup):
    call()
    assert len([s for s in setup if s.startswith("Review")]) == 3
    assert len(setup) == 4


def test_concurrent_runs_do_not_duplicate(setup):
    threads = [threading.Thread(target=call) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    reviews = [s for s in setup if s.startswith("Review")]
    # 2 runs: 3 drafts, then the remaining 2; each draft reviewed exactly once
    assert len(reviews) == len(set(reviews)) == 5


def test_per_recipient_insights_and_failed_send_retries(setup, monkeypatch):
    monkeypatch.setenv("NOTIFY_EMAIL_TO", "a@x.com,b@y.com")
    store = insights.LocalStore(insights.os.environ["ARTICLES_DIR"])
    store.save(insights.ARTICLES_BLOB, {
        f"t{i}": {"topic": f"T{i}", "answer": f"A{i}", "status": "approved",
                  "approved_at": f"2026-01-0{i}", "revision": 1} for i in range(1, 3)})
    delivered, fail = [], {"b@y.com"}

    def fake(to, s, t, h):
        if to in fail:
            return False
        if to == "owner@x.com":
            return True
        delivered.append((to, "A1" in t, "A2" in t))
        return True
    monkeypatch.setattr(main, "send_email", fake)
    out = call()[0]
    assert out["recipients"] == {"sent": 1, "total": 2}
    assert delivered == [("a@x.com", True, False)]
    fail.clear()
    call()
    # a@ moves on to article 2; b@ retries article 1 after the failure
    assert delivered[1:] == [("a@x.com", False, True), ("b@y.com", True, False)]
