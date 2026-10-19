"""Human-approved LLM insights that are appended to notification emails.

Topics live in `topics.json` and articles in `articles.json` inside a GCS bucket, so new
insights are added by editing data, not code.

Article status: pending -> approved -> sent
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


def prepare_insight(call_llm, send_email, store=None):
    """Generate/revise drafts, send review emails, and return the next approved insight (or None)."""
    store = store or default_store()
    topics = load_topics(store)
    articles = store.load(ARTICLES_BLOB, {})
    budget = _env_int("MAX_NEW_ARTICLES_PER_RUN", 3)
    summary = {"generated": 0, "revised": 0, "reviews_sent": 0, "errors": []}

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

    approved = sorted(((a.get("approved_at", ""), aid) for aid, a in articles.items()
                       if a["status"] == "approved"))
    insight = None
    if approved:
        aid = approved[0][1]
        insight = {"id": aid, "topic": articles[aid]["topic"], "answer": articles[aid]["answer"]}
    return insight, summary


def mark_sent(insight_id: str, store=None) -> None:
    store = store or default_store()
    articles = store.load(ARTICLES_BLOB, {})
    if insight_id in articles and articles[insight_id]["status"] == "approved":
        articles[insight_id]["status"] = "sent"
        articles[insight_id]["sent_at"] = _now()
        store.save(ARTICLES_BLOB, articles)


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
