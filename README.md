# Aero India Registration Tracker

An HTTP-triggered Google Cloud Function that watches the Aero India visitor registration page (`https://aeroindia.gov.in/visit`). It asks an LLM whether general-visitor registration has started and emails you the result. Call it on a schedule (for example with Cloud Scheduler). The LLM is only called again when the page text has changed.

## Table of Contents

* [Architecture](#architecture)
* [Insights (human-approved articles)](#insights-human-approved-articles)
* [Environment variables](#environment-variables)
* [Directory contents](#directory-contents)
* [Notes](#notes)
* [Getting started with VS Code](#getting-started-with-vs-code)

## Architecture

```
HTTP request -> aero_india_tracker() -> LangGraph app -> parse verdict -> email -> JSON response
```

### LangGraph

State (`GraphState`): `url`, `question`, `webpage_text`, `answer`, `text_changed`.

```
START -> scrape_webpage --(changed)---> answer_question ----+
                        \-(unchanged)-> use_cached_answer ---+-> END
```

| Node | Description |
|---|---|
| `scrape_webpage` | Fetches the page with `requests`, strips script and style tags with BeautifulSoup and normalizes the text. Compares it with the cached copy in `/tmp/scraped_text.txt`. Sets `text_changed` if the text differs or no cached answer exists. |
| `route_on_change` (conditional edge) | Routes to `answer_question` when changed, otherwise to `use_cached_answer`. |
| `answer_question` | Sends the page text and question to the LLM, which replies starting with `Verdict: Yes/No/Unknown`. Uses OpenRouter (with fallback models) or Ollama. Caches the answer and page text only when the verdict is not `Unknown`, so failed runs are retried. |
| `use_cached_answer` | Returns the answer from `/tmp/llm_answer.txt`. |

After the graph runs, `parse_verdict` extracts Yes, No or Unknown (ignoring `<think>` blocks). `send_result_email` sends a Gmail SMTP message (HTML with a plain-text alternative, status badge and a button to the registration page) with the subject "Started Now" or "Not Started". The function returns JSON with `status`, `run_time` (IST), `verdict`, `content_changed` and `answer`. Any exception returns HTTP 500 with an error message.

## Insights (human-approved articles)

Optional "Insight of the day" section in the notification email. You define topics, the LLM writes an article on each, and **you approve each article** before it is emailed. Adding an article needs no code change.

1. Create a Cloud Storage bucket and upload a `topics.json` (see `topics.example.json`, 10 seed topics). Entries are `{"id": "...", "topic": "...", "instructions": "..."}` or plain strings. Append new topics at any time. `instructions` is optional (see below).
2. Each run, the LLM drafts articles for topics that have no article yet (at most `MAX_NEW_ARTICLES_PER_RUN` per run) and emails you a review message with **Approve**, **Request changes** and **Discard** buttons. State is kept in `articles.json` in the same bucket.
3. The buttons open a confirmation page on the function URL (`PUBLIC_URL`); nothing changes until you press the confirm button, so email link scanners cannot approve by accident. Links are HMAC-signed (`APPROVAL_SECRET`) and tied to the draft revision, so links to an older draft stop working.
4. **Request changes** asks for feedback. On the next run the LLM rewrites the draft using the topic, the previous draft and your feedback, then re-sends a review email ("Revised (v2)"). After `MAX_REVISIONS` revisions only Approve/Discard remain.
5. Approved articles are added to the notification email one per email, oldest approval first, then marked `sent`.

**Per-topic instructions.** The optional `instructions` field is extra guidance for that topic only, e.g. `"Focus on its development history."`. It is appended to the built-in prompt, which still enforces length, format and accuracy rules, so instructions steer content and angle but cannot change the output format. It is capped at 2000 characters and is also used when the LLM rewrites the draft after your feedback. The text is stored with the article when it is first drafted, so editing `instructions` in `topics.json` later does not regenerate an existing article. To redraft, use **Request changes**, or delete the entry from `articles.json`.

Article status: `pending` -> `approved` -> `sent`; `pending` -> `revision_requested` -> `pending` (next revision); `pending` -> `abandoned`.

Set `INCLUDE_INSIGHTS=false` to turn the whole feature off (no drafting, no review emails, no insight section). It is also skipped, with a log message, if neither `ARTICLES_BUCKET` nor `ARTICLES_DIR` is set, or if `APPROVAL_SECRET` or `PUBLIC_URL` is missing. A failure in the insights step never blocks the registration notification. Requests to paths other than `/` (such as a browser's `/favicon.ico`) return 404 and do nothing, and runs are serialized within an instance so overlapping requests cannot draft or email twice. Across several instances, keep the scheduler non-overlapping or set max instances to 1.

The service account needs read/write access to the bucket (for example `roles/storage.objectUser`). The function URL must be reachable from your browser for the approval links.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `LLM_PROVIDER` | No (default `ollama`) | `ollama` or `openrouter` |
| `OPENROUTER_API_KEY` | If `openrouter` | OpenRouter API key |
| `OPENROUTER_MODEL` | If `openrouter` | Primary model ID, e.g. `anthropic/claude-sonnet-4.6` |
| `OPENROUTER_FALLBACK_MODELS` | No | Comma-separated fallback model IDs, tried in order, e.g. `openai/gpt-5.4-mini,google/gemini-2.5-flash` |
| `OLLAMA_BASE_URL` | No (default `http://host.docker.internal:11434`) | Ollama server (local runs only) |
| `OLLAMA_MODEL` | No (default `qwen3:4b`) | Ollama model name |
| `GMAIL_ADDRESS` | For email | Sender Gmail address |
| `GMAIL_APP_PASSWORD` | For email | Gmail app password |
| `NOTIFY_EMAIL_TO` | No (defaults to `GMAIL_ADDRESS`) | Recipient address |
| `INCLUDE_INSIGHTS` | No (default `true`) | `false` disables the insights feature |
| `ARTICLES_BUCKET` | For insights (or `ARTICLES_DIR`) | Bucket holding `topics.json` and `articles.json` |
| `ARTICLES_DIR` | Local runs only | Local folder holding the same two files instead of a bucket, e.g. `./.local-articles`. Takes precedence over `ARTICLES_BUCKET`. Not suitable for Cloud Run (ephemeral disk). |
| `APPROVAL_SECRET` | For insights | Secret used to sign approval links |
| `PUBLIC_URL` | For insights | Public URL of this function, used in approval links |
| `MAX_NEW_ARTICLES_PER_RUN` | No (default `3`) | Max LLM drafts/revisions per run |
| `MAX_REVISIONS` | No (default `3`) | Max versions of one article before only approve/discard |

If the Gmail variables are missing, the function still runs but sends no email.

Example for Cloud Run (the `^@^` prefix changes the separator so commas in the value are kept):

```
gcloud run services update aero-india-tracker \
  --region asia-southeast2 \
  --update-env-vars "^@^LLM_PROVIDER=openrouter@OPENROUTER_MODEL=<model>@OPENROUTER_FALLBACK_MODELS=<model1>,<model2>"
```

## Directory contents

* `main.py` - the function code and LangGraph workflow
* `insights.py` - topic/article storage, LLM drafting and revision, signed approval actions
* `email_templates.py` - HTML and plain-text email templates
* `topics.example.json` - 10 seed topics to upload as `topics.json`
* `tests/` - unit tests (`python -m pytest tests`)
* `requirements.txt` - pinned dependencies
* `.gcloudignore` - keeps `.venv`, caches and editor files out of the deploy bundle
* `.vscode/launch.json` - Cloud Code deploy and local debug configurations

## Notes

* The cache files live in `/tmp`, which is per-instance and cleared on cold starts, so the change detection resets then. To persist them, mount a Cloud Storage bucket as a Cloud Run volume and point the file paths at it.
* `openrouter` pins `pydantic<2.13`, so `requirements.txt` pins `pydantic==2.12.5`.


## Getting started with VS Code

### Prerequisites

1. A Google Cloud project with billing enabled, and a Google account with permission to deploy to it.
1. Enable these APIs: Cloud Functions, Cloud Build, Artifact Registry, Cloud Run and Cloud Logging.
1. [VS Code](https://code.visualstudio.com/) with the [Cloud Code extension](https://cloud.google.com/code/docs/vscode/install) and the Python and Python Debugger extensions.
1. Python 3.12 or newer. The function is configured for `python314` in `.vscode/launch.json`, so use a version supported by your dependencies.
1. The [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), signed in with `gcloud auth login` and `gcloud config set project <PROJECT_ID>`.
1. For the Ollama provider only: a local [Ollama](https://ollama.com/) server with the model pulled (for example `ollama pull qwen3:4b`).

### Set up the local environment

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Select `.venv` as the interpreter in VS Code (Command Palette > **Python: Select Interpreter**).

Set the environment variables from the table above in your shell before launching. For example, with OpenRouter:

```bash
export LLM_PROVIDER=openrouter
export OPENROUTER_API_KEY=<your key>
export OPENROUTER_MODEL=<model id>
export OPENROUTER_FALLBACK_MODELS=<model1>,<model2>   # optional
export GMAIL_ADDRESS=<sender>                          # optional, enables email
export GMAIL_APP_PASSWORD=<app password>
```

Do not commit keys. `.env` files are already in `.gitignore`.

To try insights locally without a bucket, use a folder:

```bash
mkdir -p .local-articles && cp topics.example.json .local-articles/topics.json
export ARTICLES_DIR=./.local-articles
export APPROVAL_SECRET=any-long-random-string
export PUBLIC_URL=http://localhost:8000
```

### Run and debug locally

Two configurations are defined in `.vscode/launch.json`:

* **Python: Run Functions Framework** - starts the function at `http://localhost:8000` using the `functions_framework` module under the debugger. Start it from **Run and Debug** (F5), then call it:
  ```bash
  curl http://localhost:8000
  ```
* **Deploy Cloud Function** - the Cloud Code deploy configuration (see below).

Local runs write the cache files to `/tmp`. Delete `/tmp/scraped_text.txt` and `/tmp/llm_answer.txt` to force a fresh LLM call.

### Deploy with Cloud Code

1. Open the **Cloud Code** view and expand **Cloud Functions**.
1. Right-click the function (or run **Deploy Cloud Function** from Run and Debug) and select **Deploy function**.
1. Pick the project, region (`asia-southeast2` in `launch.json`) and runtime.
1. After deployment, add the environment variables in the Cloud Run service: **Edit & deploy new revision > Variables & Secrets**. Prefer Secret Manager for `OPENROUTER_API_KEY` and `GMAIL_APP_PASSWORD`.

`.gcloudignore` keeps `.venv`, caches and editor files out of the upload. If a deployment fails, open the build logs link shown in the **Output** tab.

### Troubleshooting

* **`EISDIR` while deploying:** the `.venv` folder was included in the upload; check `.gcloudignore`.
* **Container failed to start on port 8080:** a dependency is missing from `requirements.txt`, so the import fails. Check the revision logs.
* **uv "unsatisfiable requirements":** a package is pinned twice or two pins conflict. Keep one pin per package.
* **429 from OpenRouter:** set `OPENROUTER_FALLBACK_MODELS` or use another model.
