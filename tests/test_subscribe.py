from datetime import timedelta
from urllib.parse import parse_qsl, urlparse

import flask
import pytest
from cryptography.fernet import Fernet

import insights
import main
import subscribers as sub

OWNER = "owner@x.com"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("PUBLIC_URL", "https://f/run")
    monkeypatch.setenv("ARTICLES_DIR", str(tmp_path))
    monkeypatch.setenv("NOTIFY_EMAIL_TO", OWNER)
    monkeypatch.setenv("SUBSCRIBER_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SUBSCRIBER_HASH_KEY", "h")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")
    monkeypatch.setenv("MAX_SUBSCRIBERS", "100")
    monkeypatch.setattr(main, "GMAIL_ADDRESS", OWNER)
    sent = []
    monkeypatch.setattr(main, "send_email", lambda to, s, t, h: sent.append((to, s, t)) or True)
    monkeypatch.setattr(sub, "verify_turnstile", lambda token, ip="": token == "ok")
    return sent


def submit(email, token="ok", **extra):
    data = {"email": email, "cf-turnstile-response": token, **extra}
    with flask.Flask(__name__).test_request_context("/subscribe", method="POST", data=data):
        return main.aero_india_tracker(flask.request)


def get(query=None):
    with flask.Flask(__name__).test_request_context("/subscribe", query_string=query or {}):
        return main.aero_india_tracker(flask.request)


def link_params(text):
    url = next(w for w in text.split() if w.startswith("https://"))
    return dict(parse_qsl(urlparse(url).query))


def test_form_has_turnstile(env):
    html, status = get()
    assert status == 200 and "cf-turnstile" in html and 'data-sitekey="site"' in html


def test_form_explains_service_and_limit(env, monkeypatch):
    monkeypatch.setenv("MAX_SUBSCRIBERS", "50")
    html, _ = get()
    for needle in ("Service Details", "Aviation Snippets", "Tracker Alerts", "Passion Project",
                   "100% Free &amp; Clean", "Capped Space", "Temporary",
                   "Wednesday, Friday, and Sunday", "amazing flying machine",
                   "50 curated subscriber slots", "one week after Aero India 2027"):
        assert needle in html


def test_form_landing_page_design(env):
    html, status = get()
    assert status == 200
    for needle in ("Aero India 2027 Tracker", "Get notified as visitor registration opens",
                   "Includes weekly aircraft deep-dives", "rgba(255,255,255,.97)",
                   "backdrop-filter:blur", "background-jet.jpg", "linear-gradient",
                   "@media (max-width:400px)", "turnstile-wrap", "width:100%",
                   "info-section", "info-title", "Email Address",
                   "Subscribe to Updates &amp; Stories"):
        assert needle in html


def test_background_image_served():
    with flask.Flask(__name__).test_request_context("/background-jet.jpg"):
        body, status, headers = main.aero_india_tracker(flask.request)
    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert len(body) > 1000


def test_message_pages_share_background(env):
    html, status = submit("new@x.com")
    assert status == 200
    for needle in ("background-jet.jpg", "linear-gradient", "backdrop-filter:blur",
                   "rgba(255,255,255,.97)"):
        assert needle in html


@pytest.mark.parametrize("missing", ["TURNSTILE_SECRET_KEY", "TURNSTILE_SITE_KEY", "SUBSCRIBER_ENC_KEY"])
def test_fails_closed_without_keys(env, monkeypatch, missing):
    monkeypatch.delenv(missing)
    assert get()[1] == 503
    assert submit("a@x.com")[1] == 503
    assert env == []


@pytest.mark.parametrize("token", ["bad", ""])
def test_failed_turnstile_sends_nothing(env, tmp_path, token):
    assert submit("a@x.com", token=token)[1] == 400
    assert env == []
    assert insights.LocalStore(str(tmp_path)).load(sub.SUBSCRIBERS_BLOB, {}) == {}


def test_verify_turnstile_network_error(monkeypatch):
    import requests as rq

    def boom(*a, **k):
        raise rq.ConnectionError()
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")
    monkeypatch.setattr(sub.requests, "post", boom)
    assert sub.verify_turnstile("tok") is False
    assert sub.verify_turnstile("") is False


def test_full_flow_pending_then_confirmed(env, tmp_path):
    body, status = submit("A@X.com ")
    assert status == 200 and "confirmation email" in body
    assert len(env) == 1 and env[0][0] == "a@x.com"
    assert main.notify_recipients() == [OWNER]
    raw = (tmp_path / sub.SUBSCRIBERS_BLOB).read_text()
    assert "a@x.com" not in raw

    p = link_params(env[0][2])
    assert get(p)[1] == 200
    assert main.notify_recipients() == [OWNER, "a@x.com"]
    assert get(p)[1] == 200


def test_bad_signature_does_not_confirm(env):
    submit("a@x.com")
    p = link_params(env[0][2])
    p["sig"] = "0" * 64
    assert get(p)[1] == 400
    assert main.notify_recipients() == [OWNER]


def test_throttle_one_email_per_24h_and_same_response(env):
    first = submit("a@x.com")
    second = submit("A@x.com")
    assert first == second
    assert len(env) == 1


def test_throttle_expires_after_24h(env):
    store = insights.default_store()
    now = sub._now()
    assert sub.request_subscription("a@x.com", main.send_email, store=store, now=now) == sub.SENT
    assert sub.request_subscription("a@x.com", main.send_email, store=store,
                                    now=now + timedelta(hours=23)) == sub.THROTTLED
    assert sub.request_subscription("a@x.com", main.send_email, store=store,
                                    now=now + timedelta(hours=25)) == sub.SENT
    assert len(env) == 2


def test_throttle_applies_to_confirmed_and_owner(env):
    submit("a@x.com")
    sub.confirm(link_params(env[0][2])["id"], link_params(env[0][2])["sig"])
    store = insights.default_store()
    later = sub._now() + timedelta(hours=25)
    assert sub.request_subscription("a@x.com", main.send_email, store=store, now=later) == sub.ALREADY
    assert sub.request_subscription("a@x.com", main.send_email, store=store, now=later) == sub.THROTTLED
    r = lambda: sub.request_subscription(OWNER, main.send_email, [OWNER], store=store)
    assert r() == sub.ALREADY and r() == sub.THROTTLED
    assert sum(1 for m in env if m[0] == OWNER) == 1


def test_pending_expires_and_confirm_link_dies(env):
    store = insights.default_store()
    now = sub._now()
    sub.request_subscription("a@x.com", main.send_email, store=store, now=now)
    p = link_params(env[0][2])
    assert sub.confirm(p["id"], p["sig"], store=store, now=now + timedelta(hours=25)) == "expired"


def test_limit_and_closed(env, monkeypatch):
    monkeypatch.setenv("MAX_SUBSCRIBERS", "1")
    submit("a@x.com")
    submit("b@x.com")
    assert [m[0] for m in env] == ["a@x.com"]
    monkeypatch.setenv("MAX_SUBSCRIBERS", "0")
    submit("c@x.com")
    assert len(env) == 1


def test_invalid_email_and_honeypot(env):
    assert submit("not-an-email")[1] == 400
    assert submit("a@x.com", website="spam")[1] == 200
    assert env == []


def test_key_rotation(env, monkeypatch):
    submit("a@x.com")
    sub.confirm(link_params(env[0][2])["id"], link_params(env[0][2])["sig"])
    old = Fernet.generate_key().decode()
    current = __import__("os").environ["SUBSCRIBER_ENC_KEY"]
    monkeypatch.setenv("SUBSCRIBER_ENC_KEY", f"{old},{current}")
    assert sub.confirmed_emails() == ["a@x.com"]


# ---- unsubscribe ---------------------------------------------------------------------------

def subscribe_and_confirm(email="a@x.com"):
    out = []
    sub.request_subscription(email, lambda to, s, t, h: out.append(t) or True)
    p = link_params(out[0])
    sub.confirm(p["id"], p["sig"])


def unsub_link(text):
    url = next(w for w in text.split() if "/unsubscribe?" in w)
    return dict(parse_qsl(urlparse(url).query))


def post_unsub(p, **kw):
    with flask.Flask(__name__).test_request_context("/unsubscribe", method="POST", query_string=p, **kw):
        return main.aero_india_tracker(flask.request)


def get_unsub(p):
    with flask.Flask(__name__).test_request_context("/unsubscribe", query_string=p):
        return main.aero_india_tracker(flask.request)


def notification_for(addr, monkeypatch):
    captured = []
    monkeypatch.setattr(main, "send_email",
                        lambda to, s, t, h, headers=None: captured.append((to, t, h, headers)) or True)
    main.send_result_email(addr, "now", "Yes", "No")
    return captured[0]


def test_footer_link_and_headers_for_subscriber_only(env, monkeypatch):
    subscribe_and_confirm()
    to, text, html, headers = notification_for("a@x.com", monkeypatch)
    assert "/unsubscribe?" in text and "Unsubscribe</a>" in html
    assert "a@x.com" not in text
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert headers["List-Unsubscribe"].startswith("<https://f/run/unsubscribe?")
    _, text, _, headers = notification_for(OWNER, monkeypatch)
    assert "unsubscribe" not in text.lower() and headers is None


def test_unsubscribe_get_does_not_delete_post_does(env, monkeypatch):
    subscribe_and_confirm()
    _, text, _, _ = notification_for("a@x.com", monkeypatch)
    p = unsub_link(text)
    body, status = get_unsub(p)
    assert status == 200 and "<form" in body
    assert main.notify_recipients() == [OWNER, "a@x.com"]
    assert post_unsub(p, data={"List-Unsubscribe": "One-Click"})[1] == 200
    assert main.notify_recipients() == [OWNER]
    assert post_unsub(p)[1] == 200


def test_unsubscribe_bad_signature(env, monkeypatch):
    subscribe_and_confirm()
    _, text, _, _ = notification_for("a@x.com", monkeypatch)
    p = unsub_link(text)
    p["sig"] = "0" * 64
    assert get_unsub(p)[1] == 400 and post_unsub(p)[1] == 400
    assert main.notify_recipients() == [OWNER, "a@x.com"]


def test_resubscribe_after_unsubscribe(env, monkeypatch):
    subscribe_and_confirm()
    _, text, _, _ = notification_for("a@x.com", monkeypatch)
    post_unsub(unsub_link(text))
    subscribe_and_confirm()
    assert main.notify_recipients() == [OWNER, "a@x.com"]


def test_unsubscribe_clears_delivery_history(env, tmp_path, monkeypatch):
    store = insights.LocalStore(str(tmp_path))
    store.save(insights.ARTICLES_BLOB, {"x": {"status": "approved", "sent_to": {"a@x.com": "t", OWNER: "t"}}})
    subscribe_and_confirm()
    _, text, _, _ = notification_for("a@x.com", monkeypatch)
    post_unsub(unsub_link(text))
    assert store.load(insights.ARTICLES_BLOB, {})["x"]["sent_to"] == {OWNER: "t"}


def admin(token, email, monkeypatch=None):
    with flask.Flask(__name__).test_request_context(
            "/unsubscribe-admin", method="POST", data={"email": email},
            headers={"X-Unsubscribe-Token": token}):
        return main.aero_india_tracker(flask.request)


def test_admin_removal_requires_token(env, monkeypatch):
    monkeypatch.setenv("UNSUBSCRIBE_ADMIN_TOKEN", "tok")
    subscribe_and_confirm()
    assert admin("wrong", "a@x.com")[1] == 401
    assert main.notify_recipients() == [OWNER, "a@x.com"]
    assert admin("tok", "A@x.com")[1] == 200
    assert main.notify_recipients() == [OWNER]
    assert admin("tok", "a@x.com")[1] == 404
