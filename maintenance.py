"""Admin maintenance mode: pauses notification emails to self-service subscribers.

State lives in `maintenance.json` in the same store as the other tracker state
(`ARTICLES_BUCKET`, or `ARTICLES_DIR` locally), so it survives cold starts and is shared
across instances. Toggled via the token-guarded `/maintenance` HTTP route (see README);
while paused, runs on `/` skip subscriber recipients only.
"""
from datetime import datetime, timezone

import insights

STATE_BLOB = "maintenance.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state(store=None) -> dict:
    """Current maintenance state; a missing blob means not paused."""
    store = store or insights.default_store()
    data = store.load(STATE_BLOB, {})
    if not isinstance(data, dict):
        return {"paused": False}
    data.setdefault("paused", False)
    return data


def is_paused(store=None) -> bool:
    return bool(state(store).get("paused"))


def set_paused(paused: bool, store=None) -> dict:
    """Read-modify-write under STATE_LOCK; idempotent. Returns the new state."""
    store = store or insights.default_store()
    with insights.STATE_LOCK:
        data = state(store)
        data["paused"] = bool(paused)
        data["updated_at"] = _now()
        data["paused_at" if paused else "resumed_at"] = data["updated_at"]
        store.save(STATE_BLOB, data)
    return data
