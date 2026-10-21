"""Disk persistence for conversation records and daily event logs.

Sessions use atomic JSON replacement and configurable age/count retention.
Only token hashes are stored; the application can restore an eligible session
after eviction or restart when the visitor proves token and tenant ownership.
POSIX files are restricted to owner read/write. Events use daily JSONL files
and separate retention settings."""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any

SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "data", "sessions")
EVENTS_DIR = os.path.join(os.path.dirname(__file__), "data", "events")
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(EVENTS_DIR, exist_ok=True)

# Restrict session IDs to a bounded safe character set before filesystem or HTML use.


_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_safe_session_id(sid) -> bool:
    """True iff sid is a syntactically-safe session identifier.

    Also exported via app.py for the chat endpoint and admin endpoints
    so all three boundaries enforce the same rule.
    """
    if not isinstance(sid, str):
        return False
    return bool(_SAFE_SESSION_ID_RE.match(sid))

# Two-axis cap: hard count limit + age-based purge.
# Count cap keeps disk bounded under bursty load; age cap prevents stale
# conversations sitting around indefinitely under low traffic.
MAX_SESSION_FILES = int(os.getenv("MAX_SESSION_FILES", "500"))
SESSION_RETENTION_DAYS = int(os.getenv("SESSION_RETENTION_DAYS", "90"))

_IS_POSIX = os.name == "posix"


def _chmod_owner_only(path: str) -> None:
    """Restrict file to owner read/write. POSIX only — no-op on Windows."""
    if not _IS_POSIX:
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def token_hash(token) -> str | None:
    """Hash a session token for storage and later ownership verification.
    The raw token is never written to a session file."""
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _serialize_session(session_id: str, session: dict) -> dict:
    """Serialize conversation state, resume credentials as a hash, and flags
    needed to preserve contact and recommendation continuity across restarts."""
    return {
        "session_id": session_id,
        "history": session.get("history", []),
        "debug_log": session.get("debug_log", []),
        "last_products": session.get("last_products", []),
        "shown_aedts": session.get("shown_aedts", False),
        "created_at": session.get("created_at"),
        "last_activity": session.get("last_activity"),
        "message_count": len(session.get("history", [])),
        "turn_count": len(session.get("debug_log", [])),
        "max_products_shown": session.get("max_products_shown", 0),
        "tenant": session.get("tenant", "aedleader"),
        "lead": session.get("lead", {}),
        "lead_email_asked": session.get("lead_email_asked", False),
        "lead_email_followup_asked": session.get("lead_email_followup_asked", False),
        "lead_name_asked": session.get("lead_name_asked", False),
        "lead_exit_asked": session.get("lead_exit_asked", False),
        "shown_upsell_ids": sorted(session.get("shown_upsell_ids") or []),
        "token_hash": token_hash(session.get("token")),
    }


def save_session_to_disk(session_id: str, session: dict) -> None:
    """Atomically write a session to disk. Called per-turn from app.py.

    Atomic via tmp-file + os.replace — a crash mid-write leaves either the
    old file intact or the new one complete, never a half-written file.

    Rejects malformed session_ids silently (path traversal / XSS vectors).
    Defense-in-depth — the chat endpoint's get_session() also enforces this.
    """
    if not is_safe_session_id(session_id) or not isinstance(session, dict):
        return
    data = _serialize_session(session_id, session)
    filepath = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp_path, filepath)
        _chmod_owner_only(filepath)
    except OSError as e:
        # Persistence failure must NOT break the chat path. Log and move on.
        print(f"[persistence] save_session_to_disk failed for {session_id[:8]}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return
    _cleanup_old_sessions()


def _cleanup_old_sessions() -> None:
    """Purge session files older than the retention window, then enforce the count limit."""
    if not os.path.isdir(SESSIONS_DIR):
        return
    cutoff = time.time() - SESSION_RETENTION_DAYS * 86400
    survivors: list[tuple[float, str]] = []
    for fn in os.listdir(SESSIONS_DIR):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(SESSIONS_DIR, fn)
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                os.remove(fp)
                continue
            except OSError:
                pass
        survivors.append((mtime, fp))
    if len(survivors) > MAX_SESSION_FILES:
        survivors.sort()  # oldest first
        for _, fp in survivors[: len(survivors) - MAX_SESSION_FILES]:
            try:
                os.remove(fp)
            except OSError:
                pass


def list_persisted_sessions() -> list[dict[str, Any]]:
    """Return summary rows for every session on disk.

    Cheap fields only (no full history) — admin list view. Sorted by
    last_activity desc so most-recent appears first. Caller is responsible
    for merging this with live in-memory sessions.
    """
    if not os.path.isdir(SESSIONS_DIR):
        return []
    rows: list[dict[str, Any]] = []
    for fn in os.listdir(SESSIONS_DIR):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(SESSIONS_DIR, fn)
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        history = data.get("history", [])
        user_msgs = [m["content"] for m in history if m.get("role") == "user"]
        debug_log = data.get("debug_log", [])
        # Surface a few fields useful for filtering in the admin panel
        last_intent = None
        last_journey = None
        if debug_log:
            last_entry = debug_log[-1]
            analysis = last_entry.get("analysis") or {}
            last_intent = analysis.get("intent")
            last_journey = analysis.get("journey_stage")
        rows.append({
            "session_id": data.get("session_id") or fn.replace(".json", ""),
            "created_at": data.get("created_at"),
            "last_activity": data.get("last_activity"),
            "turn_count": data.get("turn_count", len(debug_log)),
            "message_count": data.get("message_count", len(history)),
            "first_message": (user_msgs[0] if user_msgs else "")[:200],
            "last_message": (user_msgs[-1] if user_msgs else "")[:200],
            "last_intent": last_intent,
            "last_journey": last_journey,
            "tenant": data.get("tenant", "aedleader"),
            "lead": data.get("lead", {}),
            "is_live": False,
        })
    rows.sort(key=lambda r: r.get("last_activity") or 0, reverse=True)
    return rows


def get_persisted_session(session_id: str) -> dict | None:
    """Read one session file from disk. Returns None if missing or corrupt."""
    if not is_safe_session_id(session_id):
        return None
    fp = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["is_live"] = False
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"[persistence] get_persisted_session({session_id[:8]}) failed: {e}")
        return None


def mark_session_converted(
    session_id: str,
    order_id,
    value: float,
    currency: str | None = None,
    ts: float | None = None,
) -> bool:
    """Best-effort: stamp conversion outcome onto a persisted session file.

    Called by the conversions poll when a WooCommerce order is matched back to
    a chat session. Returns True iff a session file existed and was updated.

    This is a CONVENIENCE for the admin session view — data/conversions.json is
    the durable source of truth. If the session file was already purged (90-day
    cap) or the session is still live in memory (a later per-turn _persist could
    clobber these fields), the conversion record still stands in conversions.json.
    """
    if not is_safe_session_id(session_id):
        return False
    fp = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(fp):
        return False
    try:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    data["converted"] = True
    data["matched_order_id"] = order_id
    data["outcome_value"] = value
    if currency:
        data["outcome_currency"] = currency
    data["conversion_ts"] = ts if ts is not None else time.time()

    tmp = fp + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, fp)
        _chmod_owner_only(fp)
    except OSError as e:
        print(f"[persistence] mark_session_converted({session_id[:8]}) failed: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False
    return True


def delete_persisted_session(session_id: str) -> bool:
    """Remove a session file. Returns True if a file was deleted."""
    if not is_safe_session_id(session_id):
        return False
    fp = os.path.join(SESSIONS_DIR, f"{session_id}.json")
    if not os.path.exists(fp):
        return False
    try:
        os.remove(fp)
        return True
    except OSError:
        return False


# Store pre-chat and conversation events in daily JSONL files, joined by tracking ID.


EVENT_RETENTION_DAYS = int(os.getenv("EVENT_RETENTION_DAYS", "30"))
# Limit serialized event size independently of endpoint validation.


MAX_EVENT_LINE_BYTES = 4096

# Chip IDs are user-supplied via the chip config — we don't whitelist values
# (chips can be added/renamed via WP plugin) but we DO enforce shape so an
# attacker can't smuggle path traversal or HTML into log lines that the admin
# UI later renders. Same alphanumeric-plus-underscore rule as session IDs.
_SAFE_EVENT_FIELD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _is_safe_event_field(value: Any) -> bool:
    """True iff value is a safe event_name / chip_id."""
    return isinstance(value, str) and bool(_SAFE_EVENT_FIELD_RE.match(value))


def _events_file_for_date(date_str: str) -> str:
    return os.path.join(EVENTS_DIR, f"events-{date_str}.jsonl")


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def append_event(
    session_id: str,
    event_name: str,
    chip_id: str | None = None,
    metadata: dict | None = None,
) -> bool:
    """Append one event to the daily JSONL file. Returns True on success.

    Validates session_id and event_name shape — rejects junk silently
    (returns False) so the /track endpoint can map to a clean 400 without
    leaking which field failed.
    """
    if not is_safe_session_id(session_id):
        return False
    if not _is_safe_event_field(event_name):
        return False
    if chip_id is not None and not _is_safe_event_field(chip_id):
        return False
    if metadata is not None and not isinstance(metadata, dict):
        return False

    record = {
        "ts": time.time(),
        "session_id": session_id,
        "event_name": event_name,
        "chip_id": chip_id,
        "metadata": metadata or {},
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    if len(line.encode("utf-8")) > MAX_EVENT_LINE_BYTES:
        return False

    fp = _events_file_for_date(_today_str())
    try:
        # Append a complete serialized event and report write failures to the caller.


        with open(fp, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _chmod_owner_only(fp)
    except OSError as e:
        print(f"[persistence] append_event failed: {e}")
        return False

    _cleanup_old_event_files()
    return True


def _cleanup_old_event_files() -> None:
    """Drop event files older than EVENT_RETENTION_DAYS."""
    if not os.path.isdir(EVENTS_DIR):
        return
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=EVENT_RETENTION_DAYS)
    for fn in os.listdir(EVENTS_DIR):
        if not (fn.startswith("events-") and fn.endswith(".jsonl")):
            continue
        date_part = fn[len("events-"):-len(".jsonl")]
        try:
            file_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                os.remove(os.path.join(EVENTS_DIR, fn))
            except OSError:
                pass


def read_events(since_ts: float | None = None, tenant: str | None = None) -> list[dict]:
    """Stream all events from disk, optionally filtering by min timestamp.

    Walks daily files in chronological order. Skips corrupt lines silently —
    one bad line cannot poison aggregation. Caller can pivot the result
    however they want (counts per chip_id, funnel per session, etc.).
    """
    if not os.path.isdir(EVENTS_DIR):
        return []
    out: list[dict] = []
    files = sorted(
        fn for fn in os.listdir(EVENTS_DIR)
        if fn.startswith("events-") and fn.endswith(".jsonl")
    )
    for fn in files:
        fp = os.path.join(EVENTS_DIR, fn)
        try:
            with open(fp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since_ts is not None and rec.get("ts", 0) < since_ts:
                        continue
                    # Legacy events without tenant metadata belong to AED Leader.

                    if tenant is not None:
                        rec_tenant = (rec.get("metadata") or {}).get("tenant", "aedleader")
                        if rec_tenant != tenant:
                            continue
                    out.append(rec)
        except OSError:
            continue
    return out


def aggregate_events(since_ts: float | None = None, tenant: str | None = None) -> dict:
    """Produce counts + funnel for the admin Engagement tab.

    Returns:
      total_events:  int
      chip_clicks:   { chip_id: count }   — total chip_click events
      form_clicks:   { chip_id: count }   — form_button_click within an info card
      link_clicks:   { product_id: count }— product "View Product" link clicks
      sessions_with_chip:    { chip_id: count }   — distinct sessions that clicked
      sessions_kept_chatting: { chip_id: count }  — sessions with >=2 events OR
                                                    that also have history on disk
      sessions_with_link:    int          — distinct sessions with a product-link click
      feedback_up/down:      int          — 👍/👎 votes (deduped by session+message)
      csat:                  float|None   — 👍 / (👍 + 👎), None if no votes
      negative_feedback:     [ {session_id, turn, preview, ts} ]  — recent 👎 to review
      widgets_loaded/opened/chats_started: int  — engagement funnel (distinct ids)
      engagement_rate/open_rate:           float|None  — chats/loads, opens/loads
      opened_but_silent(_rate):            int/float|None  — opened but never chatted
    """
    events = read_events(since_ts=since_ts, tenant=tenant)
    chip_clicks: dict[str, int] = {}
    form_clicks: dict[str, int] = {}
    link_clicks: dict[str, int] = {}
    sessions_per_chip: dict[str, set] = {}
    sessions_with_link: set = set()
    # 👍/👎 deduped by (session_id, message index): key -> (ts, rating, preview)
    feedback_latest: dict[tuple, tuple] = {}
    # Engagement funnel — distinct tracking ids per stage. Within a page visit
    # all three fire with the SAME id (chat_started fires before the pre_→real
    # promotion), so opened−chatted etc. are clean per-id joins.
    loaded_ids: set = set()
    opened_ids: set = set()
    chatted_ids: set = set()

    for ev in events:
        name = ev.get("event_name")
        chip = ev.get("chip_id") or "(unknown)"
        sid = ev.get("session_id")
        if name == "chip_click":
            chip_clicks[chip] = chip_clicks.get(chip, 0) + 1
            sessions_per_chip.setdefault(chip, set()).add(sid)
        elif name == "form_button_click":
            form_clicks[chip] = form_clicks.get(chip, 0) + 1
        elif name == "link_click":
            # chip_id carries the product_id for product-link clicks.
            link_clicks[chip] = link_clicks.get(chip, 0) + 1
            if sid:
                sessions_with_link.add(sid)
        elif name in ("feedback_up", "feedback_down"):
            # chip_id carries the message index. Dedupe by (session, message)
            # keeping the LATEST vote so a re-cast vote isn't counted twice.
            key = (sid, chip)
            ts = ev.get("ts", 0) or 0
            prev = feedback_latest.get(key)
            if prev is None or ts >= prev[0]:
                rating = "up" if name == "feedback_up" else "down"
                preview = (ev.get("metadata") or {}).get("message_preview", "")
                feedback_latest[key] = (ts, rating, preview)
        elif name == "widget_loaded":
            if sid:
                loaded_ids.add(sid)
        elif name == "widget_opened":
            if sid:
                opened_ids.add(sid)
        elif name == "chat_started":
            if sid:
                chatted_ids.add(sid)

    sessions_kept_chatting: dict[str, int] = {}
    for chip, sids in sessions_per_chip.items():
        kept = 0
        for sid in sids:
            # "Kept chatting" = the session has a chat history persisted on
            # disk. Pre-session synthetic IDs (no chat ever) won't have a file.
            if sid and isinstance(sid, str) and is_safe_session_id(sid):
                fp = os.path.join(SESSIONS_DIR, f"{sid}.json")
                if os.path.exists(fp):
                    kept += 1
        sessions_kept_chatting[chip] = kept

    feedback_up = sum(1 for v in feedback_latest.values() if v[1] == "up")
    feedback_down = sum(1 for v in feedback_latest.values() if v[1] == "down")
    total_votes = feedback_up + feedback_down
    csat = round(feedback_up / total_votes, 4) if total_votes else None
    negative_feedback = [
        {"session_id": k[0], "turn": k[1], "preview": v[2], "ts": v[0]}
        for k, v in feedback_latest.items() if v[1] == "down"
    ]
    negative_feedback.sort(key=lambda x: x["ts"] or 0, reverse=True)
    negative_feedback = negative_feedback[:50]

    # Engagement + abandonment (see funnel note above).
    widgets_loaded = len(loaded_ids)
    widgets_opened = len(opened_ids)
    chats_started = len(chatted_ids)
    engagement_rate = round(chats_started / widgets_loaded, 4) if widgets_loaded else None
    open_rate = round(widgets_opened / widgets_loaded, 4) if widgets_loaded else None
    # Abandonment: opened the chat but never sent a message.
    opened_but_silent = len(opened_ids - chatted_ids)
    opened_but_silent_rate = round(opened_but_silent / widgets_opened, 4) if widgets_opened else None

    return {
        "total_events": len(events),
        "chip_clicks": chip_clicks,
        "form_clicks": form_clicks,
        "link_clicks": link_clicks,
        "sessions_with_chip": {k: len(v) for k, v in sessions_per_chip.items()},
        "sessions_kept_chatting": sessions_kept_chatting,
        "sessions_with_link": len(sessions_with_link),
        "feedback_up": feedback_up,
        "feedback_down": feedback_down,
        "feedback_responses": total_votes,
        "csat": csat,
        "negative_feedback": negative_feedback,
        "widgets_loaded": widgets_loaded,
        "widgets_opened": widgets_opened,
        "chats_started": chats_started,
        "engagement_rate": engagement_rate,
        "open_rate": open_rate,
        "opened_but_silent": opened_but_silent,
        "opened_but_silent_rate": opened_but_silent_rate,
        "since_ts": since_ts,
    }
