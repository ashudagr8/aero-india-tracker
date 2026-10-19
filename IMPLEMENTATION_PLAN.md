# Implementation plan: producer/consumer insights, multi-recipient, feedback

## Current state
Single HTTP function `aero_india_tracker`: one request scrapes, drafts up to 3 articles, sends review emails, picks the oldest approved article, sends one notification to one recipient (`NOTIFY_EMAIL_TO`), marks the article `sent`. State in `articles.json` + `topics.json` (GCS bucket or `ARTICLES_DIR`). Emails are HTML with an aircraft header image.

## Target
- Producer (`/produce`, own Scheduler job) drafts/revises articles and sends review emails. Consumer (`/`) scrapes, sends notifications, never drafts.
- Several recipients, each in a separate message (nobody sees other addresses). Each recipient works through approved articles from the start; late joiners get the backlog.
- Recipients can send text feedback via a signed link; stored per recipient with the article id.
- Aircraft header image removed.

## Rules for every phase
Change -> run tests (`/tmp/venv/bin/python -m pytest -q tests`) -> you deploy -> verify live -> only then the next phase. Max instances = 1. Existing `articles.json` stays readable. Each phase can be rolled back by redeploying the previous revision.

## Phase 0 - Remove aircraft header image
- Delete `assets/`; remove `EMAIL_HEADER_IMAGE_URL`, `insights.header_image_url()`, `image_url` params (main.py, email_templates.py), CSS/`background` attr/Outlook VML. Keep solid navy header.
- README: drop hosting section, env var row, directory entry. `.gcloudignore` and tests: drop image references.
- Verify: tests pass; no image/VML markup in rendered emails; deployed email shows navy header.

## Phase 1 - Producer/consumer split
- insights.py: `prepare_insight` -> `produce()` (revisions, drafts, reviews, backlog cap `MAX_UNSENT_BACKLOG`) and `next_insight()`.
- main.py: `/produce` route guarded by `PRODUCER_TOKEN` (401 otherwise); `/` consumer only. Both under `STATE_LOCK`. Approval links unchanged.
- Tests: producer first run = 3 reviews; cap respected; bad token rejected; consumer sends oldest approved or no insight.
- Deploy: set `PRODUCER_TOKEN`, add second Scheduler job (`/produce` with token), keep tracker job on `/`.
- Verify: curl `/produce` -> review emails; curl `/` -> notification; wrong token rejected.

## Phase 2 - Multiple recipients, private delivery
- `NOTIFY_EMAIL_TO` comma-separated; `send_email(to, ...)` sends one message per recipient (only that address in To, no Cc/Bcc). Review emails go only to `GMAIL_ADDRESS`.
- Logs/responses show counts, not addresses. One failed recipient does not block others. All still get the same next insight in this phase.
- Tests: capture messages; no message contains another recipient's address; partial failure.
- Deploy + verify: two addresses, each inbox shows only its own.

## Phase 3 - Per-recipient article tracking
- Article gets `sent_to: {email: sent_at}`; `next_insight(recipient)` = oldest approved not yet sent to that recipient; `mark_sent(id, recipient)` only on success. Legacy `status: sent` migrates to "sent to current recipients". Backlog cap counts articles not yet sent to everyone.
- Tests: late-added recipient gets backlog from the start while others continue; failed send retries; legacy migration.
- Deploy + verify: add a third address; it receives article 1 while others get their next.

## Phase 4 - Recipient feedback with article id
- Each notification email has a "Send feedback" link `?action=feedback&r=<signed recipient token>&a=<article id|none>&sig=...`; signature covers recipient token + article id; address not in URL.
- GET shows form (max ~2000 chars), POST saves. 403 bad sig, 413 too long, HTML-escaped.
- Storage: plain text, one file per recipient `feedback/<sha256(email)[:16]>.txt` in the private store; entries appended under `STATE_LOCK`: `[timestamp] recipient=<email> article=<id|none> topic=<topic>` + text. `append_text()` added to GcsStore and LocalStore.
- Tests: link in each email; article id captured (`none` without insight); separate per-recipient files; tampered id/sig rejected; escaping.
- Deploy + verify: feedback from two recipients lands in separate files with the right article id.

## Phase 5 - Docs and cleanup
README: producer/consumer diagram, env vars (`PRODUCER_TOKEN`, `MAX_UNSENT_BACKLOG`, multi-address `NOTIFY_EMAIL_TO`), scheduler setup, feedback layout, privacy notes. Full test run.
