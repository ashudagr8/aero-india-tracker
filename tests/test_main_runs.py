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
    sent = []
    monkeypatch.setattr(main, "send_email", lambda s, t, h: sent.append(s) or True)

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
