import main
import pytest


class FakeSMTP:
    messages = []
    fail_for = set()

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a):
        pass

    def send_message(self, m):
        if m["To"] in self.fail_for:
            raise RuntimeError("boom")
        self.messages.append(m)


def setup(monkeypatch, to):
    FakeSMTP.messages, FakeSMTP.fail_for = [], set()
    monkeypatch.setattr(main, "GMAIL_ADDRESS", "owner@x.com")
    monkeypatch.setattr(main, "GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("NOTIFY_EMAIL_TO", to)
    monkeypatch.setattr(main.smtplib, "SMTP_SSL", FakeSMTP)


def test_parse(monkeypatch):
    setup(monkeypatch, " a@x.com, ,B@x.com,a@X.com,")
    assert main.notify_recipients() == ["a@x.com", "B@x.com"]


def test_default_to_sender(monkeypatch):
    setup(monkeypatch, "")
    monkeypatch.delenv("NOTIFY_EMAIL_TO")
    assert main.notify_recipients() == ["owner@x.com"]


def test_private_messages(monkeypatch):
    setup(monkeypatch, "a@x.com,b@y.com")
    assert main.notify_all("now", "No", "No", False)[:2] == (2, 2)
    msgs = FakeSMTP.messages
    assert [m["To"] for m in msgs] == ["a@x.com", "b@y.com"]
    for m, other in zip(msgs, ["b@y.com", "a@x.com"]):
        assert other not in m.as_string()
        assert m["Cc"] is None and m["Bcc"] is None


def test_partial_failure(monkeypatch, capsys):
    setup(monkeypatch, "a@x.com,b@y.com")
    FakeSMTP.fail_for = {"a@x.com"}
    assert main.notify_all("now", "No", "No", False)[:2] == (1, 2)
    assert [m["To"] for m in FakeSMTP.messages] == ["b@y.com"]
    assert "@" not in capsys.readouterr().out


def test_review_only_to_owner(monkeypatch):
    setup(monkeypatch, "a@x.com,b@y.com")
    assert main.send_review_email("s", "t", "h")
    assert [m["To"] for m in FakeSMTP.messages] == ["owner@x.com"]


@pytest.mark.parametrize(
    ("verdict", "status"),
    [
        ("Yes", "Started Now"),
        ("No", "Not Started Yet"),
        ("Unknown", "Status Unclear"),
    ],
)
@pytest.mark.parametrize("with_insight", [False, True])
def test_notification_subject_reports_insight_and_registration(
    monkeypatch, verdict, status, with_insight
):
    setup(monkeypatch, "a@x.com")
    monkeypatch.setattr(main.insights, "insights_enabled", lambda: False)

    insight = {"id": "article-1", "topic": "Topic", "answer": "Body"} if with_insight else None
    assert main.send_result_email("a@x.com", "now", verdict, "No", insight)

    prefix = "Aero India | "
    if with_insight:
        prefix += "New Insight Available | "
    assert FakeSMTP.messages[0]["Subject"] == f"{prefix}Registration: {status}"
