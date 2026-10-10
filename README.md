[![License: MIT](https://shields.io)](https://opensource.org)

# Aero India Registration Tracker

An HTTP-triggered Google Cloud Function that watches the Aero India visitor registration page (`https://aeroindia.gov.in/visit`). It asks an LLM whether general-visitor registration has started and emails you the result. Call it on a schedule (for example with Cloud Scheduler). The LLM is only called again when the page text has changed.

## Table of Contents

* [Architecture](#architecture)
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

After the graph runs, `parse_verdict` extracts Yes, No or Unknown (ignoring `<think>` blocks). `send_result_email` sends a Gmail SMTP message with the subject "Started Now" or "Not Started". The function returns JSON with `status`, `run_time` (IST), `verdict`, `content_changed` and `answer`. Any exception returns HTTP 500 with an error message.

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

If the Gmail variables are missing, the function still runs but sends no email.

Example for Cloud Run (the `^@^` prefix changes the separator so commas in the value are kept):

```
gcloud run services update aero-india-tracker \
  --region asia-southeast2 \
  --update-env-vars "^@^LLM_PROVIDER=openrouter@OPENROUTER_MODEL=<model>@OPENROUTER_FALLBACK_MODELS=<model1>,<model2>"
```

## Directory contents

* `main.py` - the function code and LangGraph workflow
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
