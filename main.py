import os
import re
import csv
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


# Cloud Functions require writing local files to the ephemeral /tmp directory
SCRAPED_TEXT_FILE = "/tmp/scraped_text.txt"
CACHED_ANSWER_FILE = "/tmp/llm_answer.txt"
RUN_LOG_FILE = "/tmp/run_log.csv"

IST = ZoneInfo("Asia/Kolkata")
WEBSITE_NAME = "Aero India Official Visitor Registration Page"
WEBSITE_URL = "https://aeroindia.gov.in/visit"

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL_TO = os.environ.get("NOTIFY_EMAIL_TO", GMAIL_ADDRESS)
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

def invoke_openrouter(prompt: str, temperature: float = 0) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter.")
    if not OPENROUTER_MODEL:
        raise RuntimeError("OPENROUTER_MODEL is required when LLM_PROVIDER=openrouter.")
    with OpenRouter(api_key=OPENROUTER_API_KEY) as client:
        completion = client.chat.send(
            model=OPENROUTER_MODEL,
            models=OPENROUTER_FALLBACK_MODELS or None,  # OpenRouter falls back in order if the primary fails
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
    return completion.choices[0].message.content or ""

def call_llm(prompt: str, temperature: float = 0) -> str:
    if LLM_PROVIDER == "openrouter":
        return invoke_openrouter(prompt, temperature)
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

def send_email(subject: str, text: str, html: str) -> bool:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or not NOTIFY_EMAIL_TO:
        print("Email not sent: Environment credentials missing.")
        return False

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = GMAIL_ADDRESS
    message["To"] = NOTIFY_EMAIL_TO
    message.attach(MIMEText(text, "plain", "utf-8"))
    message.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            smtp.send_message(message)
        print(f"Email notification sent to {NOTIFY_EMAIL_TO}.")
        return True
    except Exception as exc:
        print(f"Email notification failed: {exc}")
        return False

def send_result_email(run_time: str, verdict: str, content_changed: str, insight: dict | None = None) -> bool:
    status = "Started Now" if verdict.lower() == "yes" else "Not Started"
    subject = f"Aero India General Visitor Registration: {status}"
    text, html = email_templates.render_notification(
        run_time, verdict, content_changed, WEBSITE_NAME, WEBSITE_URL,
        insight=insight,
    )
    return send_email(subject, text, html)

# HTTP entry point for Google Cloud Functions Framework
@functions_framework.http
def aero_india_tracker(request):
    # Browsers also request /favicon.ico etc.; those must not trigger a run (or send emails)
    if request.path not in ("", "/"):
        return "Not found", 404

    if request.args.get("action") or request.form.get("action"):
        try:
            html, status = insights.handle_action(request.method, request.values)
        except Exception as e:
            print(f"Approval action failed: {e}")
            html, status = email_templates.render_page("Error", "<p>Something went wrong.</p>"), 500
        return html, status, {"Content-Type": "text/html; charset=utf-8"}

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

        insight, insight_summary = None, None
        # One run at a time per instance, so overlapping requests cannot draft or send twice
        with insights.STATE_LOCK:
            if insights.insights_enabled():
                try:
                    insight, insight_summary = insights.prepare_insight(call_llm, send_email)
                except Exception as e:
                    # Insights must never block the registration notification
                    print(f"Insights step failed: {e}")
                    insight_summary = {"errors": [str(e)]}

            sent = send_result_email(run_time, verdict, content_changed, insight)
            if sent and insight:
                try:
                    insights.mark_sent(insight["id"])
                except Exception as e:
                    print(f"Failed to mark insight sent: {e}")

        return {
            "status": "success",
            "run_time": run_time,
            "verdict": verdict,
            "content_changed": content_changed,
            "answer": result["answer"],
            "insight_included": insight["id"] if insight and sent else None,
            "insights": insight_summary,
        }, 200

    except Exception as e:
        print(f"Pipeline execution crashed: {str(e)}")
        return {"status": "error", "message": str(e)}, 500
