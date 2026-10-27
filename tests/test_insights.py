import pytest

import email_templates as tpl
import insights


class FakeStore:
    def __init__(self, topics):
        self.data = {insights.TOPICS_BLOB: topics}

    def load(self, name, default):
        import copy
        return copy.deepcopy(self.data.get(name, default))

    def save(self, name, data):
        import copy
        self.data[name] = copy.deepcopy(data)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", "s3cret")
    monkeypatch.setenv("PUBLIC_URL", "https://fn.example/run")
    monkeypatch.setenv("ARTICLES_BUCKET", "b")


def make_llm(calls):
    def llm(prompt, temperature=0):
        calls.append(prompt)
        return "<think>x</think>Draft v%d" % len(calls)
    return llm


def params_from(link):
    from urllib.parse import urlparse, parse_qsl
    return dict(parse_qsl(urlparse(link).query))


def test_full_flow_generate_feedback_revise_approve_send():
    store = FakeStore([{"id": "q1", "topic": "Aerodynamic stalls"}, "Plain string topic"])
    calls, mails = [], []
    send = lambda subject, text, html: mails.append((subject, text, html)) or True

    summary = insights.prepare_insight(make_llm(calls), send, store)
    assert summary["generated"] == 2 and len(mails) == 2

    # Re-running does not regenerate or re-send reviews
    insights.prepare_insight(make_llm(calls), send, store)
    assert len(calls) == 2 and len(mails) == 2

    p = params_from(insights._link("feedback", "q1", 1))
    page, status = insights.handle_action("GET", p, store)
    assert status == 200 and "textarea" in page
    page, status = insights.handle_action("POST", {**p, "feedback": "Shorter please"}, store)
    assert status == 200

    insights.prepare_insight(make_llm(calls), send, store)
    art = store.data[insights.ARTICLES_BLOB]["q1"]
    assert art["revision"] == 2 and art["status"] == "pending"
    assert "Shorter please" in calls[-1] and "Draft v1" in calls[-1]
    assert mails[-1][0].startswith("Revised (v2)")

    # Old revision link is stale
    _, status = insights.handle_action("POST", p, store)
    assert status == 410

    approve = params_from(insights._link("approve", "q1", 2))
    insights.handle_action("GET", approve, store)
    assert store.data[insights.ARTICLES_BLOB]["q1"]["status"] == "pending"  # GET has no side effects
    insights.handle_action("POST", approve, store)

    insights.prepare_insight(make_llm(calls), send, store)
    assert insights.next_insight("a@x.com", store)["id"] == "q1"
    insights.mark_sent("q1", "a@x.com", store)
    assert insights.next_insight("a@x.com", store) is None


def test_bad_signature_and_revision_cap(monkeypatch):
    store = FakeStore([{"id": "q1", "topic": "T"}])
    insights.prepare_insight(make_llm([]), lambda *a: True, store)
    p = params_from(insights._link("approve", "q1", 1))
    p["sig"] = "bad"
    assert insights.handle_action("POST", p, store)[1] == 403

    monkeypatch.setenv("MAX_REVISIONS", "1")
    fb = params_from(insights._link("feedback", "q1", 1))
    assert insights.handle_action("POST", {**fb, "feedback": "x"}, store)[1] == 409


def test_disabled_toggle(monkeypatch):
    monkeypatch.setenv("INCLUDE_INSIGHTS", "false")
    assert not insights.insights_enabled()
    monkeypatch.setenv("INCLUDE_INSIGHTS", "true")
    assert insights.insights_enabled()


def test_templates_escape_and_optional_sections():
    insight = {"topic": "<b>T</b>", "answer": "**Bold** <script>\n\n- a\n- b"}
    text, html = tpl.render_notification("now", "Yes", "No", "Site", "https://x", insight)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<strong>Bold</strong>" in html and "<li" in html
    assert "background-image" not in html and "v:rect" not in html
    assert "Today's spotlight" in html and "Bonus read" in html

    text, html = tpl.render_notification("now", "No", "No", "Site", "https://x")
    assert "spotlight" not in html.lower() and "spotlight" not in text.lower()
    assert "background-image" not in html


def test_notification_layout_status_first_then_teaser():
    insight = {"topic": "Su-30MKI", "answer": "First hook sentence. More.\n\nSecond paragraph."}
    text, html = tpl.render_notification("now", "No", "No", "Site", "https://x", insight)
    body = html.split("</div>", 1)[1]
    assert body.index("Registration: Not Started Yet") < body.index("Today's spotlight") \
        < body.index("First hook") < body.index("Second paragraph")
    assert "Nothing to act on today" in html
    assert "First hook sentence." in html.split("</div>", 1)[0]
    assert text.index("General Visitor Registration") < text.index("Today's spotlight") \
        < text.index("First hook") < text.index("Second paragraph")


def test_local_store_via_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ARTICLES_BUCKET")
    monkeypatch.setenv("ARTICLES_DIR", str(tmp_path / "data"))
    assert insights.insights_enabled()
    store = insights.default_store()
    assert isinstance(store, insights.LocalStore)
    store.save(insights.TOPICS_BLOB, [{"id": "a", "topic": "T"}])
    insights.prepare_insight(make_llm([]), lambda *a: True, store)
    assert insights.default_store().load(insights.ARTICLES_BLOB, {})["a"]["status"] == "pending"


def test_topic_instructions_in_generation_and_revision():
    store = FakeStore([
        {"id": "a", "topic": "A", "instructions": "  Focus on history.  "},
        {"id": "b", "topic": "B"},
        {"id": "c", "topic": "C", "instructions": "   "},
        {"id": "d", "topic": "D", "instructions": 5},
    ])
    calls = []
    send = lambda *a: True
    import os
    os.environ["MAX_NEW_ARTICLES_PER_RUN"] = "10"
    try:
        insights.prepare_insight(make_llm(calls), send, store)
    finally:
        del os.environ["MAX_NEW_ARTICLES_PER_RUN"]
    assert "Focus on history." in calls[0] and "Additional instructions" in calls[0]
    assert all("Additional instructions" not in c for c in calls[1:])
    assert store.data[insights.ARTICLES_BLOB]["a"]["instructions"] == "Focus on history."

    # Revision reuses stored instructions even if topics.json no longer has them
    store.data[insights.TOPICS_BLOB] = [{"id": "a", "topic": "A"}]
    p = params_from(insights._link("feedback", "a", 1))
    insights.handle_action("POST", {**p, "feedback": "Shorter"}, store)
    insights.prepare_insight(make_llm(calls), send, store)
    assert "Focus on history." in calls[-1] and "Shorter" in calls[-1]


def test_instructions_are_capped():
    store = FakeStore([{"id": "a", "topic": "A", "instructions": "x" * 5000}])
    assert len(insights.load_topics(store)[0]["instructions"]) == insights.INSTRUCTIONS_MAX_CHARS


def approved_store(n, **extra):
    store = FakeStore([])
    store.data[insights.ARTICLES_BLOB] = {
        f"q{i}": {"topic": f"T{i}", "answer": f"A{i}", "status": "approved",
                  "approved_at": f"2026-01-0{i}", "revision": 1, **extra}
        for i in range(1, n + 1)}
    return store


def test_per_recipient_progress_and_late_joiner():
    store = approved_store(3)
    assert insights.next_insight("a@x.com", store)["id"] == "q1"
    insights.mark_sent("q1", "a@x.com", store)
    assert insights.next_insight("a@x.com", store)["id"] == "q2"
    # Late joiner starts from the beginning; the first recipient is unaffected
    assert insights.next_insight("b@y.com", store)["id"] == "q1"
    assert insights.next_insight("a@x.com", store)["id"] == "q2"
    assert insights.unsent_count(["a@x.com", "b@y.com"], store) == 3
    for q in ("q1", "q2", "q3"):
        insights.mark_sent(q, "a@x.com", store)
        insights.mark_sent(q, "b@y.com", store)
    assert insights.unsent_count(["a@x.com", "b@y.com"], store) == 0


def test_legacy_sent_migrates_to_current_recipients():
    store = approved_store(2)
    arts = store.data[insights.ARTICLES_BLOB]
    arts["q1"]["status"], arts["q1"]["sent_at"] = "sent", "2026-02-01"
    insights.prepare_insight(make_llm([]), lambda *a: True, store, recipients=["a@x.com"])
    arts = store.data[insights.ARTICLES_BLOB]
    assert arts["q1"]["status"] == "approved" and arts["q1"]["sent_to"] == {"a@x.com": "2026-02-01"}
    assert insights.next_insight("a@x.com", store)["id"] == "q2"
    assert insights.next_insight("new@z.com", store)["id"] == "q1"
