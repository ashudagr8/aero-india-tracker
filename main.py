import os
import re
import csv
import hmac
import random
import time
import smtplib
import requests
from datetime import datetime, timedelta, time as dtime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from typing import TypedDict
import functions_framework
from langchain_ollama import ChatOllama
from langgraph.graph import StateGraph, START, END
from openrouter import OpenRouter
from langchain_core.messages import HumanMessage
import email_templates
import insights
import subscribers

# Cloud Run's newer base images block os.utime() on the anonymous (unlinked) temp file
# gunicorn's gthread worker heartbeats on, crashing every worker with PermissionError.
# Tolerate the failure; the heartbeat is best-effort for an ephemeral container.
try:
    from gunicorn.workers import workertmp as _gworkertmp

    _original_notify = _gworkertmp.WorkerTmp.notify

    def _notify_tolerant(self):
        try:
            _original_notify(self)
        except OSError:
            pass

    _gworkertmp.WorkerTmp.notify = _notify_tolerant
except ImportError:
    pass


# Cloud Functions require writing local files to the ephemeral /tmp directory
SCRAPED_TEXT_FILE = "/tmp/scraped_text.txt"
BACKGROUND_IMAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "background-jet.jpg")
CACHED_ANSWER_FILE = "/tmp/llm_answer.txt"
RUN_LOG_FILE = "/tmp/run_log.csv"

IST = ZoneInfo("Asia/Kolkata")
WEBSITE_NAME = "Aero India Official Visitor Registration Page"
WEBSITE_URL = "https://aeroindia.gov.in/visit"

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama").strip().lower()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL")
OPENROUTER_FALLBACK_MODELS = [
    m.strip() for m in os.environ.get("OPENROUTER_FALLBACK_MODELS", "").split(",") if m.strip()
]
print (f'LLM_PROVIDER: {LLM_PROVIDER}')

class GraphState(TypedDict):
    url: str
    question: str
    webpage_text: str
    answer: str
    text_changed: bool

def scrape_webpage_node(state: GraphState) -> dict:
    print("--- SCRAPING WEBPAGE ---")
    url = state["url"]
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        for script in soup(["script", "style"]):
            script.extract()
            
        cleaned_text = soup.get_text(separator="\n")
        lines = (line.strip() for line in cleaned_text.splitlines())
        chunks = (phrase for phrase in lines if phrase)
        final_text = "\n".join(chunks)
    except Exception as e:
        raise RuntimeError(f"Failed to retrieve webpage content: {e}") from e

    prev_text = ""
    if os.path.exists(SCRAPED_TEXT_FILE):
        with open(SCRAPED_TEXT_FILE, "r", encoding="utf-8") as f:
            prev_text = f.read()

    # Treat a missing/empty cached answer as a change so the LLM always runs when needed
    cached_answer_ok = (
        os.path.exists(CACHED_ANSWER_FILE) and os.path.getsize(CACHED_ANSWER_FILE) > 0
    )
    text_changed = prev_text != final_text or not cached_answer_ok

    return {"webpage_text": final_text, "text_changed": text_changed}

def route_on_change(state: GraphState) -> str:
    return "changed" if state["text_changed"] else "unchanged"

def use_cached_answer_node(state: GraphState) -> dict:
    print("--- NO CHANGE DETECTED, REUSING CACHED ANSWER ---")
    cached_answer = ""
    if os.path.exists(CACHED_ANSWER_FILE):
        with open(CACHED_ANSWER_FILE, "r", encoding="utf-8") as f:
            cached_answer = f.read()
    return {"answer": cached_answer}

def create_llm(temperature: float = 0):
    if LLM_PROVIDER == "ollama":
        return ChatOllama(
            base_url=os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
            model=os.environ.get("OLLAMA_MODEL", "qwen3:4b"),
            temperature=temperature,
        )
    raise ValueError("LLM_PROVIDER must be 'ollama' or 'openrouter'.")

def invoke_openrouter(prompt: str, temperature: float = 0, top_p=None,
                      frequency_penalty=None, presence_penalty=None) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter.")
    if not OPENROUTER_MODEL:
        raise RuntimeError("OPENROUTER_MODEL is required when LLM_PROVIDER=openrouter.")
    extra = {k: v for k, v in (("top_p", top_p), ("frequency_penalty", frequency_penalty),
                               ("presence_penalty", presence_penalty)) if v is not None}
    with OpenRouter(api_key=OPENROUTER_API_KEY) as client:
        completion = client.chat.send(
            model=OPENROUTER_MODEL,
            models=OPENROUTER_FALLBACK_MODELS or None,  # OpenRouter falls back in order if the primary fails
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            **extra,
        )
    return completion.choices[0].message.content or ""

def call_llm(prompt: str, temperature: float = 0, **sampling) -> str:
    if LLM_PROVIDER == "openrouter":
        return invoke_openrouter(prompt, temperature, **sampling)
    return create_llm(temperature).invoke([HumanMessage(content=prompt)]).content

def parse_verdict(answer: str) -> str:
    answer = re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S)
    match = re.search(r"Verdict:\s*\**\s*(Yes|No|Unknown)", answer, re.I)
    return match.group(1).capitalize() if match else "Unknown"

def answer_question_node(state: GraphState) -> dict:
    print("--- GENERATING ANSWER ---")
    webpage_text = state["webpage_text"]
    question = state["question"]
    
    prompt = f"""
    You are an assistant parsing a specific webpage text context to answer a user's question.
    
    Webpage Context:
    ---
    {webpage_text}
    ---
    
    User Question: {question}
    
    Provide a direct, factual answer based strictly on the provided Webpage Context. If the context does not contain the answer, say "Information not found".
    Begin your reply with exactly one line in the form "Verdict: Yes" or "Verdict: No" (use "Verdict: Unknown" if the context lacks the answer), followed by brief explanation in the next two lines only.
    """
    answer = call_llm(prompt)

    # Persist cache and scraped text only after a valid answer, so failures are retried next run
    if parse_verdict(answer) != "Unknown":
        with open(CACHED_ANSWER_FILE, "w", encoding="utf-8") as f:
            f.write(answer)
        with open(SCRAPED_TEXT_FILE, "w", encoding="utf-8") as f:
            f.write(webpage_text)
    else:
        print("Answer has no valid verdict; not caching so the next run retries.")

    return {"answer": answer}

# Construct and Compile Graph
workflow = StateGraph(GraphState)
workflow.add_node("scrape_webpage", scrape_webpage_node)
workflow.add_node("answer_question", answer_question_node)
workflow.add_node("use_cached_answer", use_cached_answer_node)

workflow.add_edge(START, "scrape_webpage")
workflow.add_conditional_edges(
    "scrape_webpage",
    route_on_change,
    {"changed": "answer_question", "unchanged": "use_cached_answer"}
)
workflow.add_edge("answer_question", END)
workflow.add_edge("use_cached_answer", END)
app = workflow.compile()

def configured_recipients() -> list[str]:
    raw = os.environ.get("NOTIFY_EMAIL_TO", GMAIL_ADDRESS) or ""
    return [a.strip() for a in raw.split(",") if a.strip()]

def notify_recipients() -> list[str]:
    extra = []
    try:
        extra = subscribers.confirmed_emails()
    except Exception as exc:
        print(f"Subscriber load failed: {type(exc).__name__}")
    seen, result = set(), []
    for addr in configured_recipients() + extra:
        addr = addr.strip()
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            result.append(addr)
    return result

def send_email(to: str, subject: str, text: str, html: str, headers: dict | None = None) -> bool:
    """Send one message addressed only to `to` (no Cc/Bcc)."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or not to:
        print("Email not sent: Environment credentials missing.")
        return False

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = GMAIL_ADDRESS
    message["To"] = to
    for name, value in (headers or {}).items():
        message[name] = value
    message.attach(MIMEText(text, "plain", "utf-8"))
    message.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            smtp.send_message(message)
        return True
    except Exception as exc:
        print(f"Email notification failed: {type(exc).__name__}")
        return False

def send_review_email(subject: str, text: str, html: str) -> bool:
    return send_email(GMAIL_ADDRESS, subject, text, html)

def unsubscribe_headers(url: str) -> dict:
    return {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}

def send_result_email(to: str, run_time: str, verdict: str, content_changed: str, insight: dict | None = None) -> bool:
    enabled = insights.insights_enabled()
    feedback_url = (insights.recipient_feedback_link(to, insight["id"] if insight else None)
                    if enabled else None)
    up_url = down_url = None
    if enabled and insight:
        up_url = insights.vote_link(to, insight["id"], "up")
        down_url = insights.vote_link(to, insight["id"], "down")
    unsub_url = None
    try:
        unsub_url = subscribers.unsubscribe_link(to)
    except Exception as exc:
        print(f"Unsubscribe link failed: {type(exc).__name__}")
    subject = email_templates.notification_subject(verdict, bool(insight))
    text, html = email_templates.render_notification(
        run_time, verdict, content_changed, WEBSITE_NAME, WEBSITE_URL,
        insight=insight, feedback_url=feedback_url, vote_up_url=up_url, vote_down_url=down_url,
        unsubscribe_url=unsub_url,
    )
    if unsub_url:
        return send_email(to, subject, text, html, headers=unsubscribe_headers(unsub_url))
    return send_email(to, subject, text, html)

def notify_all(run_time: str, verdict: str, content_changed: str, use_insights: bool) -> tuple[int, int, list]:
    """One private message per recipient, each with its own next insight. Returns (sent, total, insight ids)."""
    recipients = notify_recipients()
    sent, included = 0, []
    for to in recipients:
        insight = None
        if use_insights:
            try:
                insight = insights.next_insight(to)
            except Exception as e:
                print(f"Next insight lookup failed: {type(e).__name__}")
        if send_result_email(to, run_time, verdict, content_changed, insight):
            sent += 1
            if insight:
                included.append(insight["id"])
                try:
                    insights.mark_sent(insight["id"], to)
                except Exception as e:
                    print(f"Failed to mark insight sent: {type(e).__name__}")
    print(f"Email notification sent to {sent} of {len(recipients)} recipients.")
    return sent, len(recipients), included

def send_digest() -> dict:
    """Build and email the owner digest. Read-only with respect to insight state."""
    with insights.STATE_LOCK:
        digest = insights.build_digest(notify_recipients())
    subject, text, html = email_templates.render_digest(digest)
    return {
        "status": "success",
        "sent": send_review_email(subject, text, html),
        "recipients": len(digest["people"]),
        "new_feedback": len(digest["new_feedback"]),
        "low_stock": digest["low_stock"],
    }

NEUTRAL_MSG = ("If this address can be added, a confirmation email is on its way. "
               "Open the link in it to activate your subscription.")

def serve_background_image():
    try:
        with open(BACKGROUND_IMAGE_FILE, "rb") as f:
            data = f.read()
    except OSError:
        return "Not found", 404
    return data, 200, {"Content-Type": "image/jpeg", "Cache-Control": "public, max-age=86400"}

def _client_ip(request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "")

def handle_subscribe(request):
    page = email_templates.render_page
    if not subscribers.enabled():
        return page("Subscriptions unavailable", "<p>Subscriptions are currently unavailable.</p>"), 503
    try:
        if request.method == "GET":
            if request.args.get("action") == "confirm":
                result = subscribers.confirm(request.args.get("id", ""), request.args.get("sig", ""))
                messages = {"confirmed": "Your subscription is active.",
                            "full": "Sorry, subscriptions are full right now.",
                            "expired": "This link has expired. Please subscribe again.",
                            "invalid": "This link is not valid."}
                return page("Subscription", f"<p>{messages[result]}</p>"), (200 if result == "confirmed" else 400)
            return email_templates.render_subscribe_form(subscribers.turnstile_site_key(), limit=subscribers.max_subscribers()), 200
        if request.method != "POST":
            return "Method not allowed", 405
        if request.form.get("website"):
            return page("Check your inbox", f"<p>{NEUTRAL_MSG}</p>"), 200
        if not subscribers.verify_turnstile(request.form.get("cf-turnstile-response", ""), _client_ip(request)):
            return email_templates.render_subscribe_form(
                subscribers.turnstile_site_key(), "Verification failed. Please try again.", subscribers.max_subscribers()), 400
        outcome = subscribers.request_subscription(
            request.form.get("email", ""), send_email, extra_recipients=configured_recipients())
        if outcome == subscribers.INVALID:
            return email_templates.render_subscribe_form(
                subscribers.turnstile_site_key(), "Please enter a valid email address.", subscribers.max_subscribers()), 400
        return page("Check your inbox", f"<p>{NEUTRAL_MSG}</p>"), 200
    except Exception as exc:
        print(f"Subscribe failed: {type(exc).__name__}")
        return page("Error", "<p>Something went wrong. Please try again later.</p>"), 500

def handle_unsubscribe(request):
    page = email_templates.render_page
    if not subscribers.enabled():
        return page("Unavailable", "<p>This page is currently unavailable.</p>"), 503
    sub_id, sig = request.args.get("id", ""), request.args.get("sig", "")
    if not subscribers.valid_unsubscribe(sub_id, sig):
        return page("Unsubscribe", "<p>This link is not valid.</p>"), 400
    try:
        if request.method == "GET":
            return email_templates.render_unsubscribe_confirm(""), 200
        if request.method != "POST":
            return "Method not allowed", 405
        subscribers.unsubscribe(sub_id, sig)
        return page("Unsubscribed", "<p>You have been unsubscribed and will receive no more emails.</p>"), 200
    except Exception as exc:
        print(f"Unsubscribe failed: {type(exc).__name__}")
        return page("Error", "<p>Something went wrong. Please try again later.</p>"), 500

def handle_unsubscribe_admin(request):
    if request.method != "POST":
        return {"status": "method_not_allowed"}, 405
    expected = os.environ.get("UNSUBSCRIBE_ADMIN_TOKEN", "")
    supplied = request.headers.get("X-Unsubscribe-Token", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        return {"status": "unauthorized"}, 401
    if not subscribers.enabled():
        return {"status": "disabled"}, 200
    email = (request.values.get("email") or "").strip()
    if not email:
        return {"status": "bad_request"}, 400
    try:
        removed = subscribers.admin_remove(email)
    except Exception as exc:
        print(f"Admin unsubscribe failed: {type(exc).__name__}")
        return {"status": "error"}, 500
    return {"status": "removed" if removed else "not_found"}, 200 if removed else 404

# HTTP entry point for Google Cloud Functions Framework
@functions_framework.http
def aero_india_tracker(request):
    if request.path == "/digest":
        expected = os.environ.get("DIGEST_TOKEN", "")
        supplied = request.headers.get("X-Digest-Token") or request.args.get("token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return {"status": "unauthorized"}, 401
        if not insights.insights_enabled():
            return {"status": "disabled"}, 200
        try:
            return send_digest(), 200
        except Exception as e:
            print(f"Digest failed: {type(e).__name__}")
            return {"status": "error"}, 500

    if request.path == "/reset":
        if request.method != "POST":
            return {"status": "method_not_allowed"}, 405
        expected = os.environ.get("RESET_TOKEN", "")
        supplied = request.headers.get("X-Reset-Token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return {"status": "unauthorized"}, 401
        if not insights.insights_enabled():
            return {"status": "disabled"}, 200
        email = (request.values.get("email") or "").strip() or None
        try:
            with insights.STATE_LOCK:
                count = insights.reset_delivery(email)
        except Exception as e:
            print(f"Reset failed: {type(e).__name__}")
            return {"status": "error"}, 500
        if count is None:
            return {"status": "not_found"}, 404
        return {"status": "success", "articles_reset": count,
                "scope": "recipient" if email else "all"}, 200

    if request.path == "/background-jet.jpg":
        return serve_background_image()

    if request.path == "/subscribe":
        return handle_subscribe(request)

    if request.path == "/unsubscribe":
        return handle_unsubscribe(request)

    if request.path == "/unsubscribe-admin":
        return handle_unsubscribe_admin(request)

    # Browsers also request /favicon.ico etc.; those must not trigger a run (or send emails)
    if request.path not in ("", "/"):
        return "Not found", 404

    if request.args.get("action") or request.form.get("action"):
        try:
            if request.values.get("action") == "vote" and request.values.get("r"):
                html, status = insights.handle_vote(
                    request.method, request.values, notify_recipients())
            elif request.values.get("action") == "feedback" and request.values.get("r"):
                html, status = insights.handle_recipient_feedback(
                    request.method, request.values, notify_recipients())
            else:
                html, status = insights.handle_action(request.method, request.values)
        except Exception as e:
            print(f"Approval action failed: {e}")
            html, status = email_templates.render_page("Error", "<p>Something went wrong.</p>"), 500
        return html, status, {"Content-Type": "text/html; charset=utf-8"}

    expected_run = os.environ.get("RUN_TOKEN", "")
    supplied_run = request.headers.get("X-Run-Token", "")
    if not expected_run or not hmac.compare_digest(supplied_run, expected_run):
        return {"status": "unauthorized"}, 401

    try:
        inputs = {
            "url": WEBSITE_URL,
            "question": "Please check if registration to visit aero india show as general visitor has started ?"
        }

        config = {"recursion_limit": 10}
        result = app.invoke(inputs, config=config)

        # Parse Verdict (tolerate blank lines, markdown, <think> blocks)
        verdict = parse_verdict(result.get("answer", ""))
        if verdict == "Unknown":
            print(f"Verdict unknown. Raw answer: {result.get('answer')!r}")
            print(f"Scraped text (first 500 chars): {result.get('webpage_text', '')[:500]!r}")
        content_changed = "Yes" if result.get("text_changed") else "No"
        ist_time = datetime.now(ZoneInfo("Asia/Kolkata"))
        run_time = ist_time.strftime("%d/%m/%y %I:%M %p IST")

        insight_summary = None
        # One run at a time per instance, so overlapping requests cannot draft or send twice
        with insights.STATE_LOCK:
            if insights.insights_enabled():
                try:
                    insight_summary = insights.prepare_insight(call_llm, send_review_email, recipients=notify_recipients())
                except Exception as e:
                    # Insights must never block the registration notification
                    print(f"Insights step failed: {e}")
                    insight_summary = {"errors": [str(e)]}

            sent_count, total, included = notify_all(
                run_time, verdict, content_changed, insights.insights_enabled())

        return {
            "status": "success",
            "run_time": run_time,
            "verdict": verdict,
            "content_changed": content_changed,
            "answer": result["answer"],
            "insights_included": len(included),
            "recipients": {"sent": sent_count, "total": total},
            "insights": insight_summary,
        }, 200

    except Exception as e:
        print(f"Pipeline execution crashed: {str(e)}")
        return {"status": "error", "message": str(e)}, 500
