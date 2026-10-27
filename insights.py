"""Human-approved LLM insights that are appended to notification emails.

Topics live in `topics.json` and articles in `articles.json` inside a GCS bucket, so new
insights are added by editing data, not code.

Article status: pending -> approved (delivery tracked per recipient in `sent_to`)
                pending -> revision_requested -> (LLM rewrite) -> pending (revision + 1)
                pending -> abandoned (discarded)
"""
import hashlib
import hmac
import json
import os
import re
import threading
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import email_templates as tpl

# Serializes read-modify-write of articles.json within one instance. Across instances, keep the
# scheduler non-overlapping (or max instances = 1).
STATE_LOCK = threading.Lock()

TOPICS_BLOB = "topics.json"
ARTICLES_BLOB = "articles.json"
FEEDBACK_MAX_CHARS = 2000
INSTRUCTIONS_MAX_CHARS = 2000

ARTICLE_PROMPT = """You write short, engaging insights for an email read by aviation enthusiasts.

Topic: {topic}

Write a factual article about this topic of 120-180 words in plain text. Use short paragraphs separated by a blank line;
you may use "- " bullets and **bold** for key terms. No headings and no preamble. If you are unsure
of a fact, say so instead of inventing it.
{instructions_block}"""

REVISION_PROMPT = """You write short, engaging insights for an email read by aviation enthusiasts.

Topic: {topic}

Previous draft:
---
{draft}
---

The reviewer asked for these changes:
---
{feedback}
---

Rewrite the draft so it fully addresses the feedback. Keep it factual, 120-180 words, plain text with
blank-line paragraphs ("- " bullets and **bold** allowed), no headings, no preamble.
{instructions_block}"""


def _instructions_block(instructions: str) -> str:
    if not instructions:
        return ""
    return (
        "\nAdditional instructions for this topic (they guide content and angle only; the format and "
        "accuracy rules above still apply):\n---\n" + instructions + "\n---"
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def insights_enabled() -> bool:
    if os.environ.get("INCLUDE_INSIGHTS", "true").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    if not (os.environ.get("ARTICLES_DIR") or os.environ.get("ARTICLES_BUCKET")):
        print("Insights disabled: set ARTICLES_BUCKET or ARTICLES_DIR.")
        return False
    missing = [n for n in ("APPROVAL_SECRET", "PUBLIC_URL") if not os.environ.get(n)]
    if missing:
        print(f"Insights disabled: missing env vars {', '.join(missing)}.")
        return False
    return True


def _max_revisions() -> int:
    return _env_int("MAX_REVISIONS", 3)


def _clean(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- storage -------------------------------------------------------------------------------

class GcsStore:
    def __init__(self, bucket_name: str):
        from google.cloud import storage
        self._bucket = storage.Client().bucket(bucket_name)

    def load(self, name: str, default):
        blob = self._bucket.blob(name)
        if not blob.exists():
            return default
        return json.loads(blob.download_as_text())

    def save(self, name: str, data) -> None:
        self._bucket.blob(name).upload_from_string(
            json.dumps(data, indent=2, ensure_ascii=False), content_type="application/json"
        )

    def append_text(self, name: str, text: str) -> None:
        # GCS objects are immutable: read-modify-write, serialized by STATE_LOCK
        blob = self._bucket.blob(name)
        existing = blob.download_as_text() if blob.exists() else ""
        blob.upload_from_string(existing + text, content_type="text/plain; charset=utf-8")

    def read_text(self, name: str) -> str:
        blob = self._bucket.blob(name)
        return blob.download_as_text() if blob.exists() else ""


class LocalStore:
    """Same interface as GcsStore, backed by a local folder (for local runs)."""

    def __init__(self, directory: str):
        self._dir = os.path.abspath(directory)
        os.makedirs(self._dir, exist_ok=True)

    def load(self, name: str, default):
        path = os.path.join(self._dir, name)
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, name: str, data) -> None:
        path = os.path.join(self._dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def append_text(self, name: str, text: str) -> None:
        path = os.path.join(self._dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)

    def read_text(self, name: str) -> str:
        path = os.path.join(self._dir, name)
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()


def default_store():
    """ARTICLES_DIR (local folder) takes precedence over ARTICLES_BUCKET (GCS)."""
    directory = os.environ.get("ARTICLES_DIR")
    if directory:
        return LocalStore(directory)
    return GcsStore(os.environ["ARTICLES_BUCKET"])


def load_topics(store) -> list:
    """Accepts {"id", "topic", optional "instructions"} objects or plain strings."""
    topics = []
    for item in store.load(TOPICS_BLOB, []):
        if isinstance(item, str):
            item = {"topic": item}
        text = (item.get("topic") or "").strip()
        if not text:
            continue
        tid = str(item.get("id") or hashlib.sha1(text.encode()).hexdigest()[:10])
        instructions = item.get("instructions")
        instructions = instructions.strip()[:INSTRUCTIONS_MAX_CHARS] if isinstance(instructions, str) else ""
        topics.append({"id": tid, "topic": text, "instructions": instructions})
    return topics


# ---- signed links ----------------------------------------------------------------------------

def _sign(action: str, article_id: str, revision) -> str:
    msg = f"{action}:{article_id}:{revision}".encode()
    return hmac.new(os.environ["APPROVAL_SECRET"].encode(), msg, hashlib.sha256).hexdigest()


def _link(action: str, article_id: str, revision) -> str:
    query = urlencode({"action": action, "id": article_id, "rev": revision,
                       "sig": _sign(action, article_id, revision)})
    base = os.environ["PUBLIC_URL"]
    return f"{base}{'&' if '?' in base else '?'}{query}"


def _valid_sig(action, article_id, revision, sig) -> bool:
    return hmac.compare_digest(_sign(action, article_id, revision), sig or "")


# ---- recipient feedback ----------------------------------------------------------------------

def recipient_token(email: str) -> str:
    """Opaque, stable id for a recipient; the address itself never appears in links."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def _sign_recipient(token: str, article_id: str) -> str:
    msg = f"recipient-feedback:{token}:{article_id}".encode()
    return hmac.new(os.environ["APPROVAL_SECRET"].encode(), msg, hashlib.sha256).hexdigest()


def recipient_feedback_link(email: str, article_id) -> str:
    token, aid = recipient_token(email), article_id or "none"
    query = urlencode({"action": "feedback", "r": token, "a": aid,
                       "sig": _sign_recipient(token, aid)})
    base = os.environ["PUBLIC_URL"]
    return f"{base}{'&' if '?' in base else '?'}{query}"


VOTES_BLOB = "votes.json"
VOTE_LABELS = {"up": "\U0001F44D Useful", "down": "\U0001F44E Not for me"}


def _sign_vote(vote: str, token: str, article_id: str) -> str:
    msg = f"recipient-vote:{vote}:{token}:{article_id}".encode()
    return hmac.new(os.environ["APPROVAL_SECRET"].encode(), msg, hashlib.sha256).hexdigest()


def vote_link(email: str, article_id: str, vote: str) -> str:
    token = recipient_token(email)
    query = urlencode({"action": "vote", "v": vote, "r": token, "a": article_id,
                       "sig": _sign_vote(vote, token, article_id)})
    base = os.environ["PUBLIC_URL"]
    return f"{base}{'&' if '?' in base else '?'}{query}"


def handle_vote(method: str, params, recipients, store=None):
    """Returns (html, status). GET only shows a confirm form (mail scanners prefetch links); POST records."""
    vote, token, aid, sig = (params.get(k, "") for k in ("v", "r", "a", "sig"))
    if (vote not in VOTE_LABELS or not token or not aid
            or not hmac.compare_digest(_sign_vote(vote, token, aid), sig)):
        return tpl.render_page("Invalid link", "<p>This link is not valid.</p>"), 403
    email = next((r for r in recipients if recipient_token(r) == token), None)
    if not email:
        return tpl.render_page("Link no longer valid", "<p>This link is no longer active.</p>"), 410

    label = VOTE_LABELS[vote]
    if method != "POST":
        hidden = "".join(
            f'<input type="hidden" name="{k}" value="{escape(v, quote=True)}">'
            for k, v in (("action", "vote"), ("v", vote), ("r", token), ("a", aid), ("sig", sig)))
        body = (f'<p style="font-size:18px;">Your vote: <strong>{label}</strong></p>'
                f'<form method="post">{hidden}<p><label>Add a comment (optional)<br>'
                f'<textarea name="feedback" rows="4" maxlength="{FEEDBACK_MAX_CHARS}" '
                'style="width:100%;margin-top:6px;"></textarea></label></p>'
                '<button type="submit" style="background:#0b5cad;color:#fff;border:0;border-radius:6px;'
                'padding:12px 22px;font-size:15px;cursor:pointer;">Submit</button></form>')
        return tpl.render_page("Was this insight useful?", body), 200

    text = (params.get("feedback") or "").strip()
    if len(text) > FEEDBACK_MAX_CHARS:
        return tpl.render_page(
            "Too long", f"<p>Please keep comments under {FEEDBACK_MAX_CHARS} characters.</p>"), 413

    store = store or default_store()
    with STATE_LOCK:
        votes = store.load(VOTES_BLOB, {})
        votes.setdefault(aid, {})[token] = {"vote": vote, "at": _now()}
        store.save(VOTES_BLOB, votes)
        if text:
            art = store.load(ARTICLES_BLOB, {}).get(aid)
            topic = " ".join(art["topic"].split()) if art else "unknown"
            store.append_text(
                f"feedback/{token}.txt",
                f"[{_now()}] recipient={email} article={aid} topic={topic}\n[{label}] {text}\n\n")
    return tpl.render_page("Thank you", "<p>Your vote was saved.</p>"), 200


def handle_recipient_feedback(method: str, params, recipients, store=None):
    """Returns (html, status). GET renders the form; POST appends to the recipient's own file."""
    token, aid, sig = params.get("r", ""), params.get("a", ""), params.get("sig", "")
    if not token or not aid or not hmac.compare_digest(_sign_recipient(token, aid), sig):
        return tpl.render_page("Invalid link", "<p>This link is not valid.</p>"), 403
    email = next((r for r in recipients if recipient_token(r) == token), None)
    if not email:
        return tpl.render_page("Link no longer valid", "<p>This link is no longer active.</p>"), 410

    if method != "POST":
        hidden = "".join(
            f'<input type="hidden" name="{k}" value="{escape(v, quote=True)}">'
            for k, v in (("action", "feedback"), ("r", token), ("a", aid), ("sig", sig)))
        body = (f'<form method="post">{hidden}<p><label>Your feedback<br>'
                f'<textarea name="feedback" rows="6" required maxlength="{FEEDBACK_MAX_CHARS}" '
                'style="width:100%;margin-top:6px;"></textarea></label></p>'
                '<button type="submit" style="background:#0b5cad;color:#fff;border:0;border-radius:6px;'
                'padding:12px 22px;font-size:15px;cursor:pointer;">Send feedback</button></form>')
        return tpl.render_page("Send feedback", body), 200

    text = (params.get("feedback") or "").strip()
    if len(text) > FEEDBACK_MAX_CHARS:
        return tpl.render_page(
            "Too long", f"<p>Please keep feedback under {FEEDBACK_MAX_CHARS} characters.</p>"), 413
    if not text:
        return tpl.render_page("Feedback required", "<p>Please write some feedback.</p>"), 400

    store = store or default_store()
    with STATE_LOCK:
        topic = "none"
        if aid != "none":
            art = store.load(ARTICLES_BLOB, {}).get(aid)
            topic = " ".join(art["topic"].split()) if art else "unknown"
        entry = f"[{_now()}] recipient={email} article={aid} topic={topic}\n{text}\n\n"
        store.append_text(f"feedback/{token}.txt", entry)
    return tpl.render_page("Thank you", "<p>Your feedback was saved.</p>"), 200


# ---- generation / revision -------------------------------------------------------------------

def _new_article(topic: str, answer: str, instructions: str = "") -> dict:
    return {"topic": topic, "instructions": instructions, "answer": answer, "status": "pending", "revision": 1,
            "feedback": [], "history": [], "created_at": _now(), "review_sent_revision": 0}


def _revise(article: dict, call_llm) -> None:
    feedback = "\n".join(f"- {f['text']}" for f in article["feedback"])
    answer = _clean(call_llm(REVISION_PROMPT.format(
        topic=article["topic"], draft=article["answer"], feedback=feedback,
        instructions_block=_instructions_block(article.get("instructions", ""))), 0.3))
    if not answer:
        raise RuntimeError("LLM returned an empty revision.")
    article["history"].append({"revision": article["revision"], "answer": article["answer"]})
    article["answer"] = answer
    article["revision"] += 1
    article["status"] = "pending"


def _send_review(article_id: str, article: dict, send_email) -> bool:
    rev = article["revision"]
    can_revise = rev < _max_revisions()
    title, text, html = tpl.render_review(
        article,
        _link("approve", article_id, rev),
        _link("feedback", article_id, rev) if can_revise else "",
        _link("discard", article_id, rev),
    )
    prefix = "Review" if rev == 1 else f"Revised (v{rev})"
    return send_email(f"{prefix}: {article['topic'][:80]}", text, html)


def prepare_insight(call_llm, send_email, store=None, recipients=()):
    """Generate/revise drafts and send review emails. Returns a summary dict."""
    store = store or default_store()
    topics = load_topics(store)
    articles = store.load(ARTICLES_BLOB, {})
    budget = _env_int("MAX_NEW_ARTICLES_PER_RUN", 3)
    summary = {"generated": 0, "revised": 0, "reviews_sent": 0, "errors": []}
    _migrate_legacy(articles, recipients)

    def guarded(label, fn):
        try:
            fn()
            return True
        except Exception as exc:
            print(f"Insight {label} failed: {exc}")
            summary["errors"].append(f"{label}: {exc}")
            return False

    # Revisions first, then new topics, sharing one LLM budget
    for aid, art in articles.items():
        if budget <= 0:
            break
        if art["status"] == "revision_requested":
            budget -= 1
            if guarded(f"revise {aid}", lambda a=art: _revise(a, call_llm)):
                summary["revised"] += 1
    for t in topics:
        if budget <= 0:
            break
        if t["id"] not in articles:
            budget -= 1

            def generate(t=t):
                answer = _clean(call_llm(ARTICLE_PROMPT.format(
                    topic=t["topic"], instructions_block=_instructions_block(t["instructions"])), 0.3))
                if not answer:
                    raise RuntimeError("LLM returned an empty article.")
                articles[t["id"]] = _new_article(t["topic"], answer, t["instructions"])
            if guarded(f"generate {t['id']}", generate):
                summary["generated"] += 1

    for aid, art in articles.items():
        if art["status"] == "pending" and art.get("review_sent_revision") != art["revision"]:
            if _send_review(aid, art, send_email):
                art["review_sent_revision"] = art["revision"]
                summary["reviews_sent"] += 1

    store.save(ARTICLES_BLOB, articles)

    return summary


def _migrate_legacy(articles: dict, recipients) -> bool:
    """Legacy `status: sent` articles count as already delivered to the current recipients."""
    changed = False
    for art in articles.values():
        if art.get("status") == "sent":
            art["status"] = "approved"
            art.setdefault("sent_to", {}).update(
                {r: art.get("sent_at") or _now() for r in recipients})
            changed = True
    return changed


def next_insight(recipient: str, store=None):
    """Oldest approved article not yet sent to `recipient`, or None."""
    store = store or default_store()
    articles = store.load(ARTICLES_BLOB, {})
    candidates = sorted((a.get("approved_at", ""), aid) for aid, a in articles.items()
                        if a["status"] == "approved" and recipient not in a.get("sent_to", {}))
    if not candidates:
        return None
    aid = candidates[0][1]
    return {"id": aid, "topic": articles[aid]["topic"], "answer": articles[aid]["answer"]}


def mark_sent(insight_id: str, recipient: str, store=None) -> None:
    store = store or default_store()
    articles = store.load(ARTICLES_BLOB, {})
    art = articles.get(insight_id)
    if art and art["status"] == "approved":
        art.setdefault("sent_to", {})[recipient] = _now()
        store.save(ARTICLES_BLOB, articles)


def unsent_count(recipients, store=None) -> int:
    """Approved articles not yet sent to every recipient."""
    store = store or default_store()
    return sum(1 for a in store.load(ARTICLES_BLOB, {}).values()
               if a["status"] == "approved" and any(r not in a.get("sent_to", {}) for r in recipients))


def reset_delivery(email=None, store=None):
    """Clear delivery history (all recipients, or one) so articles are re-sent.

    Saves a timestamped backup first. Returns the number of articles changed,
    or None when `email` matches no delivery record.
    """
    store = store or default_store()
    articles = store.load(ARTICLES_BLOB, {})
    target = email.strip().lower() if email else None
    changed = 0
    for art in articles.values():
        sent = art.get("sent_to") or {}
        if target is None:
            if sent:
                art["sent_to"] = {}
                changed += 1
        else:
            keys = [k for k in sent if k.lower() == target]
            for k in keys:
                del sent[k]
            changed += bool(keys)
    if target is not None and not changed:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    store.save(f"articles.backup-{stamp}.json", store.load(ARTICLES_BLOB, {}))
    store.save(ARTICLES_BLOB, articles)
    return changed


# ---- daily owner digest ----------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")
DIGEST_RECENT_FEEDBACK = 5
_FEEDBACK_HEADER = re.compile(
    r"^\[(?P<ts>[^\]]+)\] recipient=(?P<email>\S+) article=(?P<aid>\S+) topic=(?P<topic>.*)$")


def low_stock_threshold() -> int:
    return _env_int("LOW_STOCK_THRESHOLD", 3)


def _to_ist(ts):
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def parse_feedback(text: str) -> list:
    """Parse a recipient feedback file into entries; text before a valid header is skipped."""
    entries, current = [], None
    for line in (text or "").splitlines():
        m = _FEEDBACK_HEADER.match(line)
        if m:
            current = {"at": _to_ist(m["ts"]), "article": m["aid"], "topic": m["topic"], "lines": []}
            entries.append(current)
        elif current is not None:
            current["lines"].append(line)
    for e in entries:
        e["text"] = "\n".join(e.pop("lines")).strip()
    return entries


def build_digest(recipients, store=None, now=None) -> dict:
    """Read-only summary of insight stock, delivery and feedback. `today` is the IST calendar day."""
    store = store or default_store()
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    today = now.date()
    articles = store.load(ARTICLES_BLOB, {})
    topics = load_topics(store)
    approved = {aid: a for aid, a in articles.items() if a.get("status") == "approved"}

    people, new_feedback = [], []
    for email in recipients:
        sent = {aid: (_to_ist(a.get("sent_to", {}).get(email))) for aid, a in approved.items()
                if email in a.get("sent_to", {})}
        entries = parse_feedback(store.read_text(f"feedback/{recipient_token(email)}.txt"))
        todays = [e for e in entries if e["at"] and e["at"].date() == today]
        last_sent = max((d for d in sent.values() if d), default=None)
        people.append({
            "email": email,
            "received": len(sent),
            "received_today": sum(1 for d in sent.values() if d and d.date() == today),
            "remaining": len(approved) - len(sent),
            "last_sent": last_sent,
            "feedback_total": len(entries),
            "feedback_today": len(todays),
            "recent_feedback": entries[-DIGEST_RECENT_FEEDBACK:][::-1],
        })
        new_feedback += [{**e, "email": email} for e in todays]

    votes = store.load(VOTES_BLOB, {})
    vote_tally = []
    for aid, per in votes.items():
        up = sum(1 for v in per.values() if v.get("vote") == "up")
        down = sum(1 for v in per.values() if v.get("vote") == "down")
        topic = " ".join(articles[aid]["topic"].split()) if aid in articles else "unknown"
        vote_tally.append({"article": aid, "topic": topic, "up": up, "down": down})

    return {
        "now": now,
        "votes": sorted(vote_tally, key=lambda v: v["topic"]),
        "threshold": low_stock_threshold(),
        "approved": len(approved),
        "pending": sum(1 for a in articles.values() if a.get("status") in ("pending", "revision_requested")),
        "undrafted": sum(1 for t in topics if t["id"] not in articles),
        "people": people,
        "new_feedback": sorted(new_feedback, key=lambda e: e["at"]),
        "low_stock": any(p["remaining"] <= low_stock_threshold() for p in people),
    }


# ---- approval web actions --------------------------------------------------------------------

def _form(action, article_id, revision, sig, button, extra="", color="#0b5cad"):
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{escape(str(v), quote=True)}">'
        for k, v in (("action", action), ("id", article_id), ("rev", revision), ("sig", sig))
    )
    return (f'<form method="post">{hidden}{extra}<button type="submit" style="background:{color};'
            f'color:#fff;border:0;border-radius:6px;padding:12px 22px;font-size:15px;cursor:pointer;">'
            f"{escape(button)}</button></form>")


def handle_action(method: str, params, store=None):
    """Returns (html, status). GET only renders a confirmation page; POST applies the change."""
    action, aid = params.get("action", ""), params.get("id", "")
    rev, sig = params.get("rev", ""), params.get("sig", "")
    if action not in ("approve", "feedback", "discard") or not _valid_sig(action, aid, rev, sig):
        return tpl.render_page("Invalid link", "<p>This link is not valid.</p>"), 403

    store = store or default_store()
    with STATE_LOCK:
        return _apply_action(method, params, store, action, aid, rev, sig)


def _apply_action(method, params, store, action, aid, rev, sig):
    articles = store.load(ARTICLES_BLOB, {})
    art = articles.get(aid)
    if not art or str(art["revision"]) != str(rev) or art["status"] != "pending":
        return tpl.render_page(
            "Link no longer valid",
            "<p>This draft was already handled or has been replaced by a newer revision.</p>"), 410

    preview = (f'<p><strong>{escape(art["topic"])}</strong></p>'
               f'<div style="background:#f3f7fc;padding:14px 18px;">{tpl.article_to_html(art["answer"])}</div>')

    if action == "feedback" and art["revision"] >= _max_revisions():
        return tpl.render_page("Revision limit reached", "<p>Please approve or discard.</p>" + preview), 409

    if method != "POST":
        if action == "approve":
            body = preview + _form(action, aid, rev, sig, "Confirm approval", color="#1b873f")
        elif action == "discard":
            body = preview + _form(action, aid, rev, sig, "Confirm discard", color="#8a94a0")
        else:
            area = ('<p><label>What should change?<br><textarea name="feedback" rows="6" required '
                    f'maxlength="{FEEDBACK_MAX_CHARS}" style="width:100%;margin-top:6px;"></textarea></label></p>')
            body = preview + _form(action, aid, rev, sig, "Send feedback", area, "#c77700")
        return tpl.render_page("Confirm", body), 200

    if action == "approve":
        art["status"], art["approved_at"] = "approved", _now()
        message = "Approved. It will be included in an upcoming notification email."
    elif action == "discard":
        art["status"] = "abandoned"
        message = "Discarded."
    else:
        text = (params.get("feedback") or "").strip()[:FEEDBACK_MAX_CHARS]
        if not text:
            return tpl.render_page("Feedback required", "<p>Please describe the changes.</p>"), 400
        art["feedback"].append({"revision": art["revision"], "text": text})
        art["status"] = "revision_requested"
        message = "Thanks. A revised draft will be emailed to you on the next run."
    store.save(ARTICLES_BLOB, articles)
    return tpl.render_page("Done", f"<p>{escape(message)}</p>"), 200
