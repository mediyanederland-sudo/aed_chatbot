"""
AED Leader RAG Chatbot — FastAPI Backend.
Pipeline: Query Analysis → Hybrid Search → Product Assembly → LLM Response + Guardrails
"""

import hmac
import os
import re
import json
import time
import uuid
import asyncio
from collections import defaultdict, deque, OrderedDict
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, HTTPException, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from llm import call_llm, call_llm_with_history
from search import search_products, lookup_product_by_id, get_upsell_products, search_knowledge
from prompts.query_analysis import QUERY_ANALYSIS_PROMPT
from prompts.chat_response import CHAT_RESPONSE_PROMPT, build_chat_prompt
from tenants import TENANTS, DEFAULT_TENANT, resolve_tenant
from sync import (
    run_incremental_sync,
    run_full_sync,
    run_deletion_check,
    get_sync_status,
    INCREMENTAL_INTERVAL,
    FULL_SYNC_INTERVAL,
)
import persistence
from persistence import is_safe_session_id
from handoff import wants_human, is_closer, HANDOFF_REPLY
from leads import (extract_lead, is_contact_only, validate_llm_name, capture_ask_reply,
                   detect_forget, invalid_attempt, normalize_contact_text, contactable, _CONFIRM_RE,
                   build_lead_state, contact_sentence, strip_contact_claims,
                   lead_history_append, lead_add_note, flag_suspicious, OFFER_PHONE_RE)
import lead_extractor
import catalog_facts
import conversions
from admin_auth import (
    login as admin_login,
    logout as admin_logout,
    validate_token as admin_validate_token,
    require_admin as require_admin_dashboard,
)

load_dotenv()

# Config
WC_URL = os.getenv("WOOCOMMERCE_URL")
WC_KEY = os.getenv("WC_CONSUMER_KEY")
WC_SECRET = os.getenv("WC_CONSUMER_SECRET")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
# Multisite: every tenant's origins are allowed alongside the env list. The
# env list stays the master switch — when it's empty (dev/tests) the origin
# check remains fully permissive, exactly as before.
_TENANT_ORIGINS = sorted({o for t in TENANTS.values() for o in t.origins})
EFFECTIVE_ORIGINS = sorted(set(ALLOWED_ORIGINS) | set(_TENANT_ORIGINS)) if ALLOWED_ORIGINS else []
MAX_HISTORY_TURNS = 15
MAX_DEBUG_ENTRIES = 50  # Bound debug records; eligible idle sessions can resume from disk.
SESSION_TTL = 30 * 60


RESUME_WINDOW_DAYS = int(os.getenv("RESUME_WINDOW_DAYS", "7"))
LIVE_THRESHOLD = 10 * 60  # Admin "Live" badge cutoff.

# Rate limiting: sliding window per IP address
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "20"))  # max requests
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))      # per N seconds
_rate_store: dict[str, deque] = defaultdict(deque)
_rate_store_lock = asyncio.Lock()

# Stricter rate limit dedicated to admin login. bcrypt cost=12 already
# imposes ~250ms latency per attempt; this caps absolute volume so a
# parallelized attacker still can't exhaust the keyspace.
LOGIN_RATE_LIMIT = int(os.getenv("LOGIN_RATE_LIMIT", "5"))           # attempts
LOGIN_RATE_WINDOW = int(os.getenv("LOGIN_RATE_WINDOW", "900"))       # per N seconds (15 min)
_login_attempts: dict[str, deque] = defaultdict(deque)
_login_attempts_lock = asyncio.Lock()

# Per-session user-message cap. Stops a single session being driven through
# thousands of LLM calls by an attacker who's already inside the IP rate
# limit. Forces them to rotate session_id, which leaves a clearer trail.
SESSION_MSG_CAP = int(os.getenv("SESSION_MSG_CAP", "100"))


async def _check_rate_limit(ip: str) -> bool:
    """Sliding window rate limiter. Returns True if allowed, False if over limit."""
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    async with _rate_store_lock:
        dq = _rate_store[ip]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_REQUESTS:
            return False
        dq.append(now)
        return True


async def _check_login_rate_limit(ip: str) -> bool:
    """Per-IP login attempt rate limit. Returns True if allowed."""
    now = time.time()
    cutoff = now - LOGIN_RATE_WINDOW
    async with _login_attempts_lock:
        dq = _login_attempts[ip]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= LOGIN_RATE_LIMIT:
            return False
        dq.append(now)
        return True


def _check_origin(http_request: Request) -> bool:
    """Check allowed Origins when public-endpoint filtering is enabled.
    Empty configuration permits requests; otherwise accept listed or same-host
    Origins. This header check does not authenticate a client."""
    if not ALLOWED_ORIGINS:
        return True
    origin = http_request.headers.get("origin", "").strip().rstrip("/")
    if not origin:
        return False
    if origin in EFFECTIVE_ORIGINS:
        return True
    host = http_request.headers.get("host", "").strip()
    if host:
        try:
            from urllib.parse import urlparse
            if urlparse(origin).netloc == host:
                return True
        except Exception:
            pass
    return False


def require_admin(request: Request):
    """Admin auth — accepts dashboard cookie/Bearer OR legacy ADMIN_TOKEN bearer.
    Delegates to admin_auth.require_admin which handles both paths.
    """
    return require_admin_dashboard(request)


# Bounded in-memory session cache; persisted records support restoration after eviction.


class _BoundedSessionDict(OrderedDict):
    def __init__(self, maxsize: int):
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        super().move_to_end(key)
        if len(self) > self._maxsize:
            super().popitem(last=False)

    def __getitem__(self, key):
        v = super().__getitem__(key)
        super().move_to_end(key)
        return v


MAX_LIVE_SESSIONS = int(os.getenv("MAX_LIVE_SESSIONS", "500"))
sessions: "_BoundedSessionDict[str, dict]" = _BoundedSessionDict(MAX_LIVE_SESSIONS)
_session_locks: dict[str, asyncio.Lock] = {}
_request_count = 0


def _resume_check(session_id: str, token: str | None, tenant_slug: str) -> dict | None:
    """Return a disk session only when its token hash, tenant, and idle window
    permit restoration. Invalid resume attempts leave the saved record intact."""
    data = persistence.get_persisted_session(session_id)
    if not isinstance(data, dict):
        return None
    stored_hash = data.get("token_hash")
    presented_hash = persistence.token_hash(token)
    if not stored_hash or not presented_hash or not hmac.compare_digest(
            stored_hash, presented_hash):
        return None
    if data.get("tenant", DEFAULT_TENANT.slug) != tenant_slug:
        return None
    last = data.get("last_activity") or 0
    try:
        last = float(last)
    except (TypeError, ValueError):
        return None
    if time.time() - last > RESUME_WINDOW_DAYS * 86400:
        return None
    return data


def _session_from_disk(data: dict, token: str) -> dict:
    """Rebuild the in-memory session from a resume-checked file dict.

    The presented (verified) token stays the live token — the widget keeps
    echoing it, continuity holds. shown_upsell_ids returns to a set; fields
    absent from older files default like a fresh session."""
    return {
        "history": data.get("history") or [],
        "last_products": data.get("last_products") or [],
        "max_products_shown": data.get("max_products_shown", 0),
        "debug_log": data.get("debug_log") or [],
        "shown_upsell_ids": set(data.get("shown_upsell_ids") or []),
        "shown_aedts": bool(data.get("shown_aedts", False)),
        "created_at": data.get("created_at"),
        "last_activity": time.time(),
        "token": token,
        "tenant": data.get("tenant", DEFAULT_TENANT.slug),
        "lead": data.get("lead") or {},
        "lead_email_asked": bool(data.get("lead_email_asked", False)),
        "lead_email_followup_asked": bool(data.get("lead_email_followup_asked", False)),
        "lead_name_asked": bool(data.get("lead_name_asked", False)),
        "lead_exit_asked": bool(data.get("lead_exit_asked", False)),
    }


# Prune orphaned locks separately from the bounded session cache.


MAX_SESSION_LOCKS = 5000


def get_session(session_id: str, token: str | None = None,
                tenant_slug: str | None = None) -> dict:
    """Get, restore, or create a session after validating its identifier.
    Existing sessions require token ownership; disk restoration also verifies
    tenant and idle-window eligibility. The caller holds the session lock."""
    if not is_safe_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format.")
    if session_id not in sessions:
        restored = _resume_check(session_id, token,
                                 tenant_slug or DEFAULT_TENANT.slug)
        if restored is not None:
            sessions[session_id] = _session_from_disk(restored, token)
            print(f"[Session] rehydrated {session_id[:8]} from disk "
                  f"({len(sessions[session_id]['history'])} msgs, "
                  f"lead={sorted((sessions[session_id]['lead'] or {}))})")
        else:
            new_token = str(uuid.uuid4())
            sessions[session_id] = {
                "history": [],
                "last_products": [],
                "max_products_shown": 0,
                "debug_log": [],
                "shown_upsell_ids": set(),
                "shown_aedts": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_activity": time.time(),
                "token": new_token,
                "tenant": DEFAULT_TENANT.slug,  # caller overrides right after creation
                "lead": {},  # captured contact details (leads.py; last-wins per field)
            }
    else:
        stored_token = sessions[session_id].get("token")
        if stored_token and token != stored_token:
            raise HTTPException(status_code=403, detail="Invalid session token.")
        sessions[session_id]["last_activity"] = time.time()
    return sessions[session_id]


def _cleanup_expired_sessions():
    """Evict idle, unlocked sessions from memory while retaining persisted records."""
    now = time.time()
    expired = [sid for sid, s in sessions.items()
               if now - s.get("last_activity", 0) > SESSION_TTL
               and not (_session_locks.get(sid) is not None
                        and _session_locks[sid].locked())]
    for sid in expired:
        del sessions[sid]
        _session_locks.pop(sid, None)
    if expired:
        print(f"[Session] Cleaned up {len(expired)} expired sessions. Active: {len(sessions)}")
    # Orphan-lock prune: ids that never became sessions (probes) leave locks
    # behind that the loop above can't reach.
    if len(_session_locks) > MAX_SESSION_LOCKS:
        for sid in [s for s, lock in list(_session_locks.items())
                    if not lock.locked() and s not in sessions]:
            _session_locks.pop(sid, None)


def _persist(session_id: str) -> None:
    """Write the session to disk for admin viewing. Best-effort —
    persistence failures must NOT break the chat path (already swallowed
    inside persistence.save_session_to_disk)."""
    s = sessions.get(session_id)
    if s is not None:
        persistence.save_session_to_disk(session_id, s)


def _append_debug(session_id, session, entry):
    """Append debug entry, cap at MAX_DEBUG_ENTRIES, persist to disk."""
    session["debug_log"].append(entry)
    if len(session["debug_log"]) > MAX_DEBUG_ENTRIES:
        session["debug_log"] = session["debug_log"][-MAX_DEBUG_ENTRIES:]
    _persist(session_id)


def add_to_history(session_id, role, content):
    if session_id not in sessions:
        return
    session = sessions[session_id]
    session["history"].append({"role": role, "content": content})
    # Keep the full transcript on disk; apply the context window only when building prompts.


    _persist(session_id)


def _build_product_summary(search_results):
    """Build a short product summary to store in history so follow-ups work.
    e.g. user says 'tell me more about the second one' or 'cheaper than that'
    """
    if not search_results or not search_results.get("products"):
        return ""
    lines = []
    for i, p in enumerate(search_results["products"], 1):
        lines.append(f"{i}. {p['name']} (ID:{p['product_id']}) — ${p['price']} — {p['brand']}")
    return "\n[Products in context: " + "; ".join(lines) + "]"


# Request/Response models
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    session_token: str | None = None  # Ownership proof — must match server-issued token
    # Multisite: optional tenant slug. The Origin header outranks this;
    # it exists for the same-origin test page and non-browser clients.
    site: str | None = Field(default=None, max_length=32)


class ProductCard(BaseModel):
    product_id: int
    name: str
    brand: str
    price: float
    regular_price: float
    sale_price: float | None
    on_sale: bool
    image_url: str
    thumbnail_url: str
    permalink: str
    stock_status: str
    recommendation_reason: str


class ChatResponse(BaseModel):
    message: str
    product_cards: list[ProductCard]
    upsell_message: str | None
    upsell_cards: list[ProductCard]
    follow_up_question: str | None
    session_id: str
    session_token: str  # Client must echo this back in subsequent requests
    # Per-turn pipeline trace is intentionally NOT included in the public
    # response — it leaks intent/filters/query/result-count to anyone
    # opening DevTools. The data is still recorded on the server in
    # session["debug_log"] and reachable via the admin-gated
    # /debug/{session_id}/turn/{n} endpoint.


# /track endpoint payload — chip clicks and info-card CTA clicks.
# Field caps are tighter than ChatRequest because there's no LLM token budget
# to soak up large payloads here — it's pure write throughput.
class TrackEventRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    event_name: str = Field(min_length=1, max_length=64)
    chip_id: str | None = Field(default=None, max_length=64)
    metadata: dict | None = None


# Whitelist of event names. Anything else → 400. Defense against junk-key
# DoS (attacker spamming arbitrary event names to bloat the log).
#   chip_click        — a suggestion chip was tapped
#   form_button_click — CTA inside an info card (e.g. "Open inquiry form")
#   link_click        — a product "View Product" link was clicked (chip_id
#                       carries the product_id; metadata has target_type/url)
#   feedback_up       — 👍 on a bot answer (chip_id = message index)
#   feedback_down     — 👎 on a bot answer (metadata has message_preview)
#   widget_loaded     — the widget rendered on a page ("saw it")
#   widget_opened     — the user opened the chat panel (excludes auto-open)
#   chat_started      — the user sent their first message (engagement)
_ALLOWED_EVENT_NAMES = frozenset({
    "chip_click", "form_button_click", "link_click", "feedback_up", "feedback_down",
    "widget_loaded", "widget_opened", "chat_started",
})


def _salvage_truncated_response(raw):
    """Recover message text and selected product IDs from malformed model JSON.
    Return None if no card IDs can be recovered."""
    # Salvage the message field if the LLM wrote a structured "message" key.
    # If we can't find it, fall back to using the raw prose preceding the
    # ```json fence (or the whole raw text) — better than blank.
    message = ""
    msg_match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
    if msg_match:
        try:
            message = json.loads('"' + msg_match.group(1) + '"')
        except ValueError:  # ValueError covers JSONDecodeError (subclass)
            message = msg_match.group(1)
    else:
        # No structured "message" found — use the prose that came before any
        # ```json fence, stripped of any trailing partial JSON markers.
        prose_end = raw.find("```json")
        if prose_end > 0:
            message = raw[:prose_end].strip()
        else:
            message = raw.strip()

    # Position-based bucketing: product_ids appearing AFTER the
    # `"upsell_cards"` marker go to upsell_cards; ids after `"product_cards"`
    # but before `"upsell_cards"` go to product_cards. Ids before the first
    # `"product_cards"` marker (e.g. inside the message field) are ignored.
    pc_start = raw.find('"product_cards"')
    uc_start = raw.find('"upsell_cards"')
    product_ids, upsell_ids = [], []
    if pc_start >= 0 or uc_start >= 0:
        for m in re.finditer(r'"product_id"\s*:\s*(\d+)', raw):
            pos = m.start()
            pid = int(m.group(1))
            if uc_start > 0 and pos > uc_start:
                upsell_ids.append(pid)
            elif pc_start > 0 and pos > pc_start:
                product_ids.append(pid)

    if not product_ids and not upsell_ids:
        return None

    return {
        "message": message,
        "product_cards": [{"product_id": pid, "recommendation_reason": ""} for pid in product_ids],
        "upsell_cards": [{"product_id": pid, "recommendation_reason": ""} for pid in upsell_ids],
        "upsell_message": "",
        "follow_up_question": "",
    }


def _extract_json(raw):
    """Extract JSON from LLM response, handling code fences and surrounding text.

    Falls back to regex-based salvage if all standard parsers fail (e.g. the
    LLM ran out of tokens mid-JSON). See _salvage_truncated_response.
    """
    cleaned = raw.strip()
    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    if "```" in cleaned:
        # Find the JSON block between code fences
        parts = cleaned.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                try:
                    return json.loads(part, strict=False)
                except json.JSONDecodeError:
                    continue
    # Try parsing the whole thing
    if cleaned.startswith("{"):
        try:
            return json.loads(cleaned, strict=False)
        except json.JSONDecodeError:
            pass
    # Try first { to last }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1:
        try:
            return json.loads(cleaned[start:end + 1], strict=False)
        except json.JSONDecodeError:
            pass
    # Last resort: salvage product_ids from a truncated/malformed response
    # so we don't drop the cards the LLM actually picked.
    salvaged = _salvage_truncated_response(cleaned)
    if salvaged is not None:
        return salvaged
    raise json.JSONDecodeError("No JSON found", cleaned, 0)


# Normalize model JSON field types before search, string operations, and aggregation.


# Top-level analysis fields that must be scalar strings.
_ANALYSIS_SCALAR_STR_FIELDS = ("search_query", "intent", "journey_stage", "sort_by",
                               "customer_name")
# Reject model-supplied product_type; it is an internal catalog field.


_FILTER_SCALAR_STR_FIELDS = ("brand", "top_category", "stock_status")
_FILTER_DROP_FIELDS = ("product_type",)
# Filter fields that must be numeric (used as float() bounds).
_FILTER_NUMERIC_FIELDS = ("min_price", "max_price")
# Filter fields that must be bool.
_FILTER_BOOL_FIELDS = ("on_sale",)
# Configurator fields that must be scalar strings.
_CONFIG_SCALAR_STR_FIELDS = ("environment", "budget", "audience", "package_preference")
# Configurator fields that must be bool.
_CONFIG_BOOL_FIELDS = ("pediatric", "ready_to_search", "gathered_from_history")


def _coerce_scalar_str(value, *, field: str) -> str | None:
    """Normalize a scalar string, selecting the first usable list item and rejecting mappings."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                print(f"[normalize] {field} was list — coerced to first str: {item!r}")
                return item
            if isinstance(item, (int, float)):
                print(f"[normalize] {field} was list — coerced to first numeric: {item!r}")
                return str(item)
        print(f"[normalize] {field} was list with no usable items — dropped: {value!r}")
        return None
    if isinstance(value, dict):
        print(f"[normalize] {field} was dict — dropped: {value!r}")
        return None
    return None


def _coerce_numeric(value, *, field: str) -> float | None:
    """Force value into float. List/dict/garbage → drop with warning."""
    if value is None:
        return None
    if isinstance(value, bool):  # bool is int — exclude before numeric check
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            print(f"[normalize] {field} string not numeric — dropped: {value!r}")
            return None
    if isinstance(value, list):
        for item in value:
            coerced = _coerce_numeric(item, field=field)
            if coerced is not None:
                print(f"[normalize] {field} was list — coerced to first numeric: {coerced}")
                return coerced
        print(f"[normalize] {field} was list with no numeric — dropped: {value!r}")
        return None
    print(f"[normalize] {field} unexpected type {type(value).__name__} — dropped")
    return None


def _coerce_bool(value, *, field: str) -> bool | None:
    """Force value into bool. Strings 'true'/'false'/'yes'/'no' accepted."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "1"):
            return True
        if v in ("false", "no", "0"):
            return False
        print(f"[normalize] {field} string not bool-like — dropped: {value!r}")
        return None
    if isinstance(value, list):
        for item in value:
            coerced = _coerce_bool(item, field=field)
            if coerced is not None:
                print(f"[normalize] {field} was list — coerced to first bool: {coerced}")
                return coerced
        return None
    print(f"[normalize] {field} unexpected type {type(value).__name__} — dropped")
    return None


# Detect AED Total Solution mentions by name and distinctive features to avoid repeated pitches.


_AEDTS_DISTINCTIVE_PHRASES = (
    "total solution",
    "physician oversight",
    "compliance documentation",
    "compliance reminder",
    "expiration reminder",
    "post-event service",
    "post event service",
    "$99 add-on",
    "$99 add on",
    "$99 program",
    "$99 service",
    "handles prescription",
    "handles the prescription",
)

# Phrases the USER might say that mean "tell me about AEDTS" — when present,
# the bot SHOULD pitch AEDTS even on a turn where it was already shown.
_USER_ASKED_AEDTS_PHRASES = (
    "total solution",
    "$99",
    "add-on",
    "addon",
    "complete program",
    "completeness",
    "what comes with",
    "what else do i need",
    "what else is needed",
    "what do i need",
    "physician oversight",
    "prescription",
    "compliance",
)


# Categories treated as AED machines for tenant-specific program suppression.


AED_MACHINE_CATEGORIES = frozenset({
    "AED Machine",
    "AED Brands",
    "AED Packages",
    "Recertified AEDs",
    "Rental AEDs",
})


def _is_aed_machine(payload: dict) -> bool:
    """True if a product payload belongs to an AED-machine top category.

    `top_category` can be a list (genuine multi-category products) or a
    single string in some legacy paths — handle both.
    """
    cats = payload.get("top_category") or []
    if isinstance(cats, str):
        cats = [cats]
    return any(c in AED_MACHINE_CATEGORIES for c in cats)


def _mentions_aedts(text: str) -> bool:
    """True if `text` mentions AEDTS by name OR by distinctive feature."""
    if not text or not isinstance(text, str):
        return False
    lower = text.lower()
    return any(phrase in lower for phrase in _AEDTS_DISTINCTIVE_PHRASES)


def _user_asked_about_aedts(text: str) -> bool:
    """True if the user message looks like an explicit AEDTS / completeness
    inquiry — the strip filter should NOT fire if the user asked."""
    if not text or not isinstance(text, str):
        return False
    lower = text.lower()
    return any(phrase in lower for phrase in _USER_ASKED_AEDTS_PHRASES)


def _strip_aedts_sentences(text: str) -> str:
    """Drop every sentence in `text` that mentions AEDTS by name OR feature.

    Sentence splitter handles . ! ? as terminators. Newline-separated bullets
    are treated as separate sentences too — important because the LLM often
    formats AEDTS as a bulleted post-script.
    """
    if not text:
        return text
    # Split on sentence terminators AND newlines (preserve list items)
    parts = re.split(r'(?<=[.!?])\s+|\n+', text)
    keep = [p for p in parts if not _mentions_aedts(p)]
    cleaned = " ".join(p.strip() for p in keep if p.strip())
    # Collapse runs of whitespace
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    return cleaned


# Course-shaped questions (cpr1 course path): people-training words trigger,
# training HARDWARE words veto ("training pads for my FRx" is an accessory ask).
_COURSE_WORDS_RE = re.compile(
    r"\b(courses?|classes?|certifications?|certified|recertif\w+"
    r"|cpr training|first.?aid training"
    r"|training (?:sessions?|options?|programs?))\b", re.I)
_COURSE_HARDWARE_RE = re.compile(
    r"\b(trainers?|manikins?|training (?:pads?|devices?|units?|kits?|aeds?))\b", re.I)


def _wants_courses(text: str) -> bool:
    """True for questions about training COURSES for people (classes,
    certification), never for training hardware (trainer devices, manikins,
    training pads) — those must stay on the normal accessory path."""
    if not _COURSE_WORDS_RE.search(text or ""):
        return False
    return not _COURSE_HARDWARE_RE.search(text)


# Bare "training" (matches "training", never "untrained") — the shape of an
# answer to cpr1's training-vs-equipment qualifier question.
_COURSE_BARE_RE = re.compile(r"\btraining\b", re.I)

_CONFIGURATOR_FIELDS = ("environment", "budget", "audience", "package_preference")


def _configurator_fields_known(config_info) -> bool:
    """Any configurator field extracted? (pediatric False counts as answered)"""
    ci = config_info or {}
    return any(ci.get(k) for k in _CONFIGURATOR_FIELDS) or ci.get("pediatric") is not None


def _course_flip_applies(tenant, intent, needs_retrieval, config_info, filters,
                         user_message) -> bool:
    """Decide whether to retrieve configured course products.
    Match explicit course topics and training-only qualifier replies while
    protecting equipment qualification and training-hardware queries."""
    if not tenant.course_category:
        return False
    text = user_message or ""
    course_like = _wants_courses(text) or (
        _COURSE_BARE_RE.search(text) and not _COURSE_HARDWARE_RE.search(text))
    if not course_like:
        return False
    if intent in ("faq", "out_of_scope"):
        return True
    if intent != "configurator":
        return False
    if (filters or {}).get("top_category") == tenant.course_category:
        return True
    # A short pure-training message ("training please") is the qualifier
    # answer even when the classifier filled a field from it — but any AED
    # mention means a mixed ask: leave those to the classifier's own path.
    stripped = text.strip()
    if len(stripped) <= 40 and not re.search(r"\baed|defib", stripped, re.I):
        return True
    return not _configurator_fields_known(config_info)


def _normalize_analysis(analysis: dict) -> dict:
    """Normalize model fields required by downstream consumers.
    Preserve list-valued and unknown fields; discard incompatible filter shapes."""
    if not isinstance(analysis, dict):
        return {}

    for f in _ANALYSIS_SCALAR_STR_FIELDS:
        if f in analysis:
            analysis[f] = _coerce_scalar_str(analysis[f], field=f)

    filters = analysis.get("filters")
    if isinstance(filters, dict):
        for f in _FILTER_SCALAR_STR_FIELDS:
            if f in filters:
                filters[f] = _coerce_scalar_str(filters[f], field=f"filters.{f}")
        for f in _FILTER_NUMERIC_FIELDS:
            if f in filters:
                filters[f] = _coerce_numeric(filters[f], field=f"filters.{f}")
        for f in _FILTER_BOOL_FIELDS:
            if f in filters:
                filters[f] = _coerce_bool(filters[f], field=f"filters.{f}")
        for f in _FILTER_DROP_FIELDS:
            if f in filters and filters[f] is not None:
                print(f"[normalize] filters.{f} is not user-controllable, dropping: {filters[f]!r}")
            filters.pop(f, None)
    elif filters is not None:
        # filters came back as a list/string — drop it, downstream uses .get()
        print(f"[normalize] filters not a dict — dropped: {type(filters).__name__}")
        analysis["filters"] = {}

    config = analysis.get("configurator_info")
    if isinstance(config, dict):
        for f in _CONFIG_SCALAR_STR_FIELDS:
            if f in config:
                config[f] = _coerce_scalar_str(config[f], field=f"configurator_info.{f}")
        for f in _CONFIG_BOOL_FIELDS:
            if f in config:
                config[f] = _coerce_bool(config[f], field=f"configurator_info.{f}")
    elif config is not None:
        print(f"[normalize] configurator_info not a dict — dropped: {type(config).__name__}")
        analysis["configurator_info"] = {}

    return analysis


# Pipeline steps

def step1_analyze_query(user_message, history):
    """Query analysis + rewriting. Returns parsed intent and filters."""
    # Build history context for the LLM
    history_text = ""
    if history:
        history_lines = []
        for msg in history[-(MAX_HISTORY_TURNS * 2):]:
            role = "Customer" if msg["role"] == "user" else "Assistant"
            history_lines.append(f"{role}: {msg['content'][:300]}")
        history_text = "\n".join(history_lines)

    # Preserve a pending contact request when history previews are truncated.


    ask_note = ""
    if (history and history[-1]["role"] == "assistant"
            and "may I have your name and email" in history[-1]["content"]):
        ask_note = ("\n\nNOTE: The assistant's LAST message asked the customer for "
                    "their name and email.")

    prompt_input = f"""## CONVERSATION HISTORY
{history_text if history_text else "(No prior conversation)"}{ask_note}

## CURRENT MESSAGE
{user_message}"""

    raw = call_llm(
        system_prompt=QUERY_ANALYSIS_PROMPT,
        user_message=prompt_input,
        model_type="query",
        temperature=0.1,
        max_tokens=512,
        json_mode=True,
    )

    # Parse JSON response
    try:
        analysis = _extract_json(raw)
    except json.JSONDecodeError:
        analysis = {
            "search_query": user_message,
            "intent": "browse",
            "filters": {},
            "needs_retrieval": True,
        }

    # Coerce LLM-returned lists-where-scalars-expected before anything
    # downstream touches it (search filters, configurator string ops,
    # debug log persistence, admin stats counter).
    return _normalize_analysis(analysis)


def _compare_search(compare_brands, base_filters, collection=None):
    """
    Run one brand-targeted search per brand and merge results.
    Used when intent=compare and two brands are identified.
    A single vector search can't reliably surface both brands — this guarantees it.
    """
    merged = {}
    was_relaxed = False

    for brand in compare_brands[:2]:
        query_text = f"{brand} AED defibrillator"
        result = search_products(
            query_text=query_text,
            filters=base_filters,
            limit=3,
            include_upsells=False,
            collection=collection,
        )
        was_relaxed = was_relaxed or result.get("was_relaxed", False)
        for p in result.get("products", []):
            pid = p.get("product_id")
            if pid and pid not in merged:
                merged[pid] = p

    products = list(merged.values())
    upsells = get_upsell_products(products[0], collection=collection) if products else []

    return {
        "products": products,
        "upsells": upsells,
        "filters_used": base_filters,
        "was_relaxed": was_relaxed,
        "total_found": len(products),
    }


def _budget_search(max_price, extra_filters=None, brand=None, collection=None):
    """Merge budget-filtered new and recertified AED searches, optionally by brand."""
    merged = {}
    was_relaxed = False
    base = extra_filters or {}
    brand_filter = {"brand": brand} if brand else {}

    query_new = f"{brand} AED defibrillator" if brand else "AED defibrillator"
    query_recert = f"{brand} AED defibrillator recertified" if brand else "AED defibrillator recertified"

    # Search 1: new AED machines within budget
    result1 = search_products(
        query_text=query_new,
        filters={**base, **brand_filter, "top_category": "AED Machine", "max_price": max_price},
        limit=4,
        include_upsells=False,
        sort_by="price_asc",
        collection=collection,
    )
    was_relaxed = was_relaxed or result1.get("was_relaxed", False)
    for p in result1.get("products", []):
        pid = p.get("product_id")
        if pid:
            merged[pid] = p

    # Search 2: recertified AEDs within budget
    result2 = search_products(
        query_text=query_recert,
        filters={**base, **brand_filter, "top_category": "Recertified AEDs", "max_price": max_price},
        limit=3,
        include_upsells=False,
        sort_by="price_asc",
        collection=collection,
    )
    was_relaxed = was_relaxed or result2.get("was_relaxed", False)
    for p in result2.get("products", []):
        pid = p.get("product_id")
        if pid and pid not in merged:
            merged[pid] = p

    products = list(merged.values())
    # Drop anything that exceeds max_price — fallback can bleed over-budget products in
    if max_price is not None:
        products = [p for p in products if p.get("price", 0) <= max_price]
    products.sort(key=lambda p: p.get("price", 0))
    products = products[:6]

    upsells = get_upsell_products(products[0], collection=collection) if products else []

    return {
        "products": products,
        "upsells": upsells,
        "filters_used": {"max_price": max_price, "brand": brand, **(extra_filters or {})},
        "was_relaxed": was_relaxed,
        "total_found": len(products),
    }


def step2_search(analysis, tenant=None):
    """Hybrid search with filters, fallback, and optional price sorting."""
    collection = tenant.collection if tenant else None
    if not analysis.get("needs_retrieval", True):
        return None

    filters = analysis.get("filters", {})
    # Remove null values
    clean_filters = {k: v for k, v in filters.items() if v is not None}

    # Dual-search for comparison queries: one search per brand, results merged
    compare_brands = analysis.get("compare_brands")
    if analysis.get("intent") == "compare" and compare_brands and len(compare_brands) == 2:
        base_filters = {k: v for k, v in clean_filters.items() if k != "brand"}
        return _compare_search(compare_brands, base_filters, collection=collection)

    query_text = analysis.get("search_query", "").strip()
    if not query_text:
        return None

    sort_by = analysis.get("sort_by")  # "price_asc", "price_desc", or None

    # Budget dual-search: price-browse or price-capped AED queries with no brand/category.
    # Triggered when: (a) max_price is set, OR (b) sort_by=price_asc (cheapest/best deal).
    # Single vector search can't show both new AED machines AND recertified AEDs together —
    # category filter hides one, no filter lets accessories sneak in.
    # Dual search guarantees both options appear so customer sees full price range.
    max_price = clean_filters.get("max_price")
    brand = clean_filters.get("brand")
    no_category = clean_filters.get("top_category") is None
    not_compare = analysis.get("intent") != "compare"
    is_price_browse = (max_price is not None) or (sort_by == "price_asc")
    if is_price_browse and no_category and not_compare:
        extra = {k: v for k, v in clean_filters.items()
                 if k not in ("max_price", "top_category", "brand", "stock_status")}
        return _budget_search(max_price, extra_filters=extra if extra else None, brand=brand, collection=collection)

    return search_products(
        query_text=query_text,
        filters=clean_filters,
        limit=5,
        include_upsells=True,
        sort_by=sort_by,
        collection=collection,
    )


PRICE_VERIFY_CAP = 8  # Max IDs verified per response — bounds latency under abuse


def _wc_fetch_product(pid, tenant=None):
    """Sync WooCommerce GET. Wrapped in asyncio.to_thread by callers for parallelism."""
    url = tenant.store_url.rstrip("/") if tenant else WC_URL
    auth = (tenant.wc_key, tenant.wc_secret) if tenant else (WC_KEY, WC_SECRET)
    try:
        resp = requests.get(
            f"{url}/wp-json/wc/v3/products/{pid}",
            auth=auth,
            timeout=5,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MediyaChatbot/1.0; +https://mediya.nl)"},
        )
        if resp.status_code == 200:
            return resp.json()
        # Log live-verification failures and retain indexed values when WooCommerce is unavailable.


        print(f"  [LiveVerify] WC returned {resp.status_code} for product {pid} "
              f"({url}) — serving indexed price instead")
    except Exception as e:
        print(f"  [LiveVerify] WC fetch failed for product {pid}: {type(e).__name__}")
    return None


def _apply_live_data(product, live):
    """Mutate `product` dict with fresh price/stock/sale fields from `live` JSON."""
    try:
        product["price"] = float(live.get("price", 0) or 0)
        product["stock_status"] = live.get("stock_status", product.get("stock_status"))
        product["on_sale"] = live.get("on_sale", product.get("on_sale", False))
        sp = live.get("sale_price", "")
        product["sale_price"] = float(sp) if sp else None
    except Exception:
        pass


async def fetch_live_products(ids, tenant=None) -> dict:
    """Live WooCommerce payloads for `ids` in parallel ({pid: json | None}).
    Read-only — applying them is verify_picked_prices' job."""
    if not (tenant.store_url if tenant else WC_URL) or not ids:
        return {}
    results = await asyncio.gather(
        *[asyncio.to_thread(_wc_fetch_product, pid, tenant) for pid in ids]
    )
    return dict(zip(ids, results))


async def verify_picked_prices(products_by_id, ids_to_verify, tenant=None, prefetched=None):
    """Apply live WooCommerce data to selected cards, capped by PRICE_VERIFY_CAP.
    Reuse prefetched results and fetch only selected IDs not already attempted."""
    if not (tenant.store_url if tenant else WC_URL) or not ids_to_verify:
        return

    seen = set()
    ids = []
    for pid in ids_to_verify:
        if pid in seen or pid not in products_by_id:
            continue
        ids.append(pid)
        seen.add(pid)
        if len(ids) >= PRICE_VERIFY_CAP:
            break

    if not ids:
        return

    live_by_id = dict(prefetched or {})
    live_by_id.update(await fetch_live_products(
        [pid for pid in ids if pid not in live_by_id], tenant))
    for pid in ids:
        if live_by_id.get(pid) is not None:
            _apply_live_data(products_by_id[pid], live_by_id[pid])


def _build_configurator_context(config_info, tenant=None):
    """Build extra context string for the LLM when in configurator gathering mode."""
    lines = ["## CONFIGURATOR MODE — GATHERING INFORMATION"]

    # Ask training-first tenants to choose training or equipment before AED qualification.


    if (tenant is not None and tenant.course_category
            and not _configurator_fields_known(config_info)):
        lines.append(
            "The customer has NOT yet said whether they want TRAINING or AED EQUIPMENT. "
            "This store leads with CPR/First-Aid training. Your first question this turn "
            "must be whether they're interested in CPR/First-Aid training for their team, "
            "or AED equipment — do NOT ask AED-specific questions (environment, budget) yet.")
        lines.append("\n### Guidelines:")
        lines.append("- If the user asked a question, ANSWER it first, then ask the training-vs-equipment question")
        lines.append("- Acknowledge what the user said, then ask naturally — keep it conversational (2-4 sentences)")
        lines.append("- Don't recommend specific products yet")
        lines.append("- Return product_cards as an empty array []")
        return "\n".join(lines)

    lines.append("You are helping the customer find the right AED. You don't have enough info to recommend products yet.")
    lines.append("Ask a natural follow-up question to understand their needs better.\n")

    env = config_info.get("environment")
    budget = config_info.get("budget")
    audience = config_info.get("audience")
    pediatric = config_info.get("pediatric")
    package = config_info.get("package_preference")

    known = []
    if env:
        known.append(f"Environment: {env}")
    if budget:
        known.append(f"Budget: {budget}")
    if audience:
        known.append(f"Audience: {audience}")
    if pediatric is not None:
        known.append(f"Pediatric: {'Yes' if pediatric else 'No'}")
    if package:
        known.append(f"Package: {package}")

    if known:
        lines.append("### What we know: " + " | ".join(known))

    missing = config_info.get("missing_fields", [])
    if missing:
        # Priority: ask about needs first, then budget before showing products
        # Budget comes BEFORE package_preference so we ask it mid-flow, not last
        priority_order = ["audience", "pediatric", "budget", "package_preference"]
        ordered_missing = sorted(missing, key=lambda f: priority_order.index(f) if f in priority_order else 0)
        lines.append(f"### Could ask about (in priority order): {', '.join(ordered_missing)}")
        lines.append("Ask about the FIRST 1-2 items in this list. Don't interrogate — keep it conversational.")
        if "budget" in missing:
            lines.append("IMPORTANT: Ask about budget before showing products. Without budget, you'll recommend products that may be way outside their price range. Frame it naturally: 'Do you have a budget range in mind?' If they say no budget or seem impatient, that's fine — proceed without it.")
        # Environment-specific hints so the LLM gives relevant advice
        env_lower = (env or "").lower()
        if env_lower in ("school", "elementary school"):
            lines.append("For a school: ask how many people are there. Mention pediatric pads are important for younger students. Easy-to-use AEDs with voice prompts are best since staff are typically untrained.")
        elif env_lower == "office":
            lines.append("For an office: ask how many people work there. Compact, low-maintenance AEDs work well. Consider whether they want a wall cabinet for visibility.")
        elif env_lower == "church":
            lines.append("For a church: ask about congregation size and events. Older congregation members may be at higher cardiac risk. Easy-to-use models with clear voice guidance are ideal for untrained volunteers.")
        elif env_lower == "gym":
            lines.append("For a gym: ask about member/staff count during peak hours. High-exertion environments have elevated cardiac risk. CPR feedback features are valuable. Consider whether they need one near the entrance and one near the workout floor.")
        elif env_lower in ("construction site", "construction"):
            lines.append("For a construction site: durability is critical — mention IP55/IP56 dust and water resistance ratings, shock resistance, wide operating temperature range (-4F to 122F), and rugged carrying cases. Workers are often spread across a large area, so portability matters. Ask about site size and whether multiple AEDs are needed.")
        elif env_lower in ("outdoor", "park", "sports field", "pool"):
            lines.append("For outdoor use: weather resistance is key — mention IP ratings for dust/water, UV-resistant cabinets, wide temperature tolerance, and alarmed outdoor cabinets to prevent tampering. Ask about the specific outdoor setting.")
        else:
            lines.append(
                "Use your judgment for this environment. "
                "If it is an institutional, commercial, or public setting (warehouse, restaurant, marina, hotel, factory, sports venue, community center, etc.) "
                "— ask how many people are typically there, since that determines whether they need one AED or multiple. "
                "If it is a personal or residential setting (home, house, apartment, boat or RV for personal use, etc.) "
                "— do NOT ask how many people live or are there. That question is irrelevant for a personal purchase. "
                "Instead focus on: budget (personal buyers have tighter budgets), "
                "whether they have young children at home (determines pediatric need), "
                "and ease of use (home users are untrained bystanders). "
                "Recertified AEDs are a good option to mention for home buyers on a budget."
            )

    lines.append("\n### Guidelines:")
    lines.append("- If the user asked a question ('what should I consider?', 'what factors matter?', 'what do I need?'), ANSWER it first with helpful information, then ask your follow-up question")
    lines.append("- If the environment has special requirements (construction, outdoor, etc.), share 2-3 relevant considerations BEFORE asking your follow-up. Show expertise — don't just ask questions.")
    lines.append("- Acknowledge what the user said, then ask your question naturally")
    lines.append("- Keep it conversational (3-5 sentences)")
    lines.append("- Don't recommend specific products yet — just gather info and answer questions")
    lines.append("- Return product_cards as an empty array []")
    lines.append("- If user seems eager to see products, you can mention you'll show options once you understand their needs")

    return "\n".join(lines)


# host -> owning tenant slug, for every family store (with and without www).
# Used by the prose link guard below; subdomains like classes.cpr1.com are
# deliberately NOT family hosts (legitimately linked cross-site).
def _family_host_map():
    from urllib.parse import urlparse
    mapping = {}
    for t in TENANTS.values():
        for u in (t.store_url, *t.origins):
            host = urlparse(u).netloc.lower()
            bare = host.removeprefix("www.")
            mapping[bare] = t.slug
            mapping["www." + bare] = t.slug
    return mapping


_FAMILY_HOSTS = _family_host_map()
_URL_RE = re.compile(r"""https?://([A-Za-z0-9.-]+)(/[^\s)\]"'<>]*)?""")


def _rewrite_foreign_family_links(text, tenant):
    """Prose companion of _enforce_tenant_permalinks: if the LLM writes a link
    to a SISTER store into the reply text, rewrite the host to the serving
    tenant's (the family sites are mirrored WP installs sharing page slugs, so
    the deep link usually survives). Non-family URLs are untouched."""
    if not text or "http" not in text:
        return text
    own_host = tenant.store_url.split("//", 1)[1].rstrip("/")

    def _swap(m):
        host = m.group(1).lower()
        owner = _FAMILY_HOSTS.get(host)
        if owner is None or owner == tenant.slug:
            return m.group(0)
        print(f"  [LINK-GUARD] rewrote {host} -> {own_host} in reply text ({tenant.slug})")
        return f"https://{own_host}{m.group(2) or ''}"

    return _URL_RE.sub(_swap, text)


def step4_generate_response(user_message, search_results, history, extra_context=None, tenant=None):
    """LLM generates conversational response with product recommendations."""
    # Build product context for the LLM
    if search_results and search_results["products"]:
        products_text = "## SEARCH RESULTS\n"
        for i, p in enumerate(search_results["products"], 1):
            products_text += f"\n### Product {i} (ID: {p['product_id']})\n"
            products_text += f"- Name: {p['name']}\n"
            products_text += f"- Brand: {p['brand']}\n"
            products_text += f"- Price: ${p['price']}\n"
            if p.get("sale_price"):
                products_text += f"- Sale Price: ${p['sale_price']} (regular: ${p['regular_price']})\n"
            cat_display = ", ".join(p['top_category']) if isinstance(p['top_category'], list) else p['top_category']
            products_text += f"- Category: {cat_display}\n"
            products_text += f"- Stock: {p['stock_status']}\n"
            if p.get("short_description"):
                products_text += f"- Summary: {p['short_description'][:200]}\n"
            if p.get("description"):
                products_text += f"- Description: {p['description'][:400]}\n"
            if p.get("attributes"):
                attrs = [f"{k}: {', '.join(v)}" for k, v in p["attributes"].items()]
                products_text += f"- Attributes: {'; '.join(attrs)}\n"
            if p.get("sku"):
                products_text += f"- SKU: {p['sku']}\n"
            products_text += f"- Score: {p.get('_score', 'N/A')}\n"

        if search_results.get("upsells"):
            products_text += "\n## UPSELL SUGGESTIONS\n"
            for p in search_results["upsells"]:
                upsell_cat = ", ".join(p['top_category']) if isinstance(p['top_category'], list) else p['top_category']
                products_text += f"\n- {p['name']} (ID: {p['product_id']}) — ${p['price']} — {upsell_cat}\n"

        if search_results.get("was_relaxed"):
            products_text += "\n## NOTE: Filters were relaxed to find results. Mention this naturally.\n"
    else:
        products_text = _search_status_text(search_results)

    # Build messages with history
    messages = []
    if history:
        for msg in history[-(MAX_HISTORY_TURNS * 2):]:
            messages.append({"role": msg["role"], "content": msg["content"]})

    context_parts = [products_text]
    if tenant is not None:
        # Supplement search results with prices from the synced catalog rather than prompt constants.


        facts = catalog_facts.facts_block(
            tenant, include_aedts=(tenant.aedts_mode != "suppress"
                                   or _user_asked_about_aedts(user_message)))
        if facts:
            context_parts.append(facts)
    if extra_context:
        context_parts.append(extra_context)
    context_parts.append(f"## CUSTOMER MESSAGE\n{user_message}")

    messages.append({
        "role": "user",
        "content": "\n\n".join(context_parts),
    })

    raw = call_llm_with_history(
        system_prompt=build_chat_prompt(tenant.slug) if tenant else CHAT_RESPONSE_PROMPT,
        messages=messages,
        model_type="chat",
        temperature=0.4,
        max_tokens=4096,
    )

    # Parse JSON response
    try:
        response = _extract_json(raw)
    except json.JSONDecodeError:
        response = {
            "message": raw,
            "product_cards": [],
            "upsell_message": None,
            "upsell_cards": [],
            "follow_up_question": None,
        }

    # Strip LLM artifacts from message text
    msg = response.get("message", "")
    if msg:
        msg = re.sub(r'\[(?:Product|Upsell)\s*Card:.*?\]', '', msg)  # [Product Card: 1440, ...]
        msg = re.sub(r'\(ID:\s*\d+\)', '', msg)                      # (ID: 514) or (ID:514)
        msg = re.sub(r'\n{3,}', '\n\n', msg).strip()
        response["message"] = msg

    # Prose link guard: sister-store URLs in reply text get re-hosted to the
    # serving tenant (covers every step4 call site — main, FAQ, configurator,
    # direct-reference).
    if tenant is not None:
        for key in ("message", "upsell_message"):
            val = response.get(key)
            if val:
                response[key] = _rewrite_foreign_family_links(val, tenant)

    return response


def step5_guardrails(llm_response, search_results):
    """Verify LLM didn't hallucinate products. Build cards from DB payload."""
    # Get valid product IDs from search results
    valid_ids = set()
    product_lookup = {}

    if search_results and search_results.get("products"):
        for p in search_results["products"]:
            valid_ids.add(p["product_id"])
            product_lookup[p["product_id"]] = p

    if search_results and search_results.get("upsells"):
        for p in search_results["upsells"]:
            valid_ids.add(p["product_id"])
            product_lookup[p["product_id"]] = p

    # Build product cards from DB payload (NOT from LLM text)
    product_cards = []
    for card in llm_response.get("product_cards", []):
        pid = card.get("product_id")
        if pid in product_lookup:
            p = product_lookup[pid]
            product_cards.append(ProductCard(
                product_id=pid,
                name=p["name"],
                brand=p.get("brand", ""),
                price=p["price"],
                regular_price=p.get("regular_price", p["price"]),
                sale_price=p.get("sale_price"),
                on_sale=p.get("on_sale", False),
                image_url=p.get("image_url", ""),
                thumbnail_url=p.get("thumbnail_url", ""),
                permalink=p.get("permalink", ""),
                stock_status=p.get("stock_status", "instock"),
                recommendation_reason=card.get("recommendation_reason", ""),
            ))
        # Skip any product_id not in valid_ids (hallucinated)

    # Build upsell cards from DB payload
    upsell_cards = []
    for card in llm_response.get("upsell_cards", []):
        pid = card.get("product_id")
        if pid in product_lookup:
            p = product_lookup[pid]
            upsell_cards.append(ProductCard(
                product_id=pid,
                name=p["name"],
                brand=p.get("brand", ""),
                price=p["price"],
                regular_price=p.get("regular_price", p["price"]),
                sale_price=p.get("sale_price"),
                on_sale=p.get("on_sale", False),
                image_url=p.get("image_url", ""),
                thumbnail_url=p.get("thumbnail_url", ""),
                permalink=p.get("permalink", ""),
                stock_status=p.get("stock_status", "instock"),
                recommendation_reason=card.get("recommendation_reason", ""),
            ))

    return product_cards, upsell_cards


# FastAPI app

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("AED Leader Chatbot starting...")

    # Provide threadpool capacity for concurrent model, database, and file operations.


    import anyio.to_thread
    anyio.to_thread.current_default_thread_limiter().total_tokens = 100

    # Start sync scheduler.
    # job_defaults per APScheduler guidance for long-running jobs: a slow full
    # sync must never stack concurrent executions (max_instances=1), queued
    # misses collapse to one run (coalesce), and a run up to 5 min late still
    # fires (misfire_grace_time) instead of being dropped.
    from apscheduler.schedulers.background import BackgroundScheduler
    scheduler = BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )

    # Schedule the default tenant and sister tenants whose product collections exist.


    syncable = [DEFAULT_TENANT]
    try:
        from search import qdrant_client as _qc
        _existing_collections = {c.name for c in _qc.get_collections().collections}
        syncable += [t for t in TENANTS.values()
                     if t.slug != DEFAULT_TENANT.slug and t.collection in _existing_collections]
    except Exception as e:
        print(f"  [Scheduler] Qdrant collection discovery failed ({e}) — scheduling aedleader only")

    for t in syncable:
        # Stagger first runs per tenant (sync_offset_min) so WC fetches and
        # embedding calls never fire simultaneously across stores.
        scheduler.add_job(
            run_incremental_sync,
            "interval",
            minutes=INCREMENTAL_INTERVAL,
            args=[t.slug],
            id=f"incremental_sync:{t.slug}",
            name=f"Incremental product sync ({t.slug})",
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2 + t.sync_offset_min),
        )
        scheduler.add_job(
            run_full_sync,
            "interval",
            minutes=FULL_SYNC_INTERVAL,
            args=[t.slug],
            id=f"full_sync:{t.slug}",
            name=f"Full catalog sync ({t.slug})",
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5 + t.sync_offset_min * 4),
        )
    print(f"  [Scheduler] Sync tenants: {', '.join(t.slug for t in syncable)}")

    # Conversion tracking: nightly WooCommerce orders poll (gated behind
    # CONVERSIONS_POLL_ENABLED — turn on once Order Attribution is enabled and
    # the API key can read orders). Reuses the product-sync WC credentials.
    if conversions.POLL_ENABLED:
        scheduler.add_job(
            conversions.run_orders_poll,
            "interval",
            minutes=conversions.ORDERS_POLL_INTERVAL,
            id="orders_poll",
            name="Conversion orders poll",
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3),  # First run 3 min after startup
        )

    scheduler.start()
    print(f"  Sync scheduler started: incremental every {INCREMENTAL_INTERVAL}min, full every {FULL_SYNC_INTERVAL}min")
    if conversions.POLL_ENABLED:
        print(f"  Conversion orders poll enabled: every {conversions.ORDERS_POLL_INTERVAL}min")
    else:
        print("  Conversion orders poll DISABLED (set CONVERSIONS_POLL_ENABLED=true to enable)")

    yield

    scheduler.shutdown()
    print("AED Leader Chatbot shutting down...")


app = FastAPI(
    title="AED Leader RAG Chatbot",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=EFFECTIVE_ORIGINS if ALLOWED_ORIGINS else ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

# Serve frontend static files for testing
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'frontend')
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="frontend")

    @app.get("/")
    async def serve_frontend():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, http_request: Request):
    """Main chat endpoint — full RAG pipeline."""
    start_time = time.time()

    # Server-side Origin check (curl/script bypass of CORS)
    if not _check_origin(http_request):
        raise HTTPException(status_code=403, detail="Forbidden.")

    # Rate limiting — per client IP
    client_ip = http_request.client.host if http_request.client else "unknown"
    if not await _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"Too many requests. Max {RATE_LIMIT_REQUESTS} per {RATE_LIMIT_WINDOW}s.",
        )

    # Periodic session cleanup (every 50 requests)
    global _request_count
    _request_count += 1
    if _request_count % 50 == 0:
        _cleanup_expired_sessions()

    # Resolve the tenant from Origin, then site, then the default.

    tenant = resolve_tenant(http_request.headers.get("origin"), request.site)

    # Session management
    session_id = request.session_id or str(uuid.uuid4())
    session_token = request.session_token  # None on first message (new session)

    # A session belongs to exactly one tenant. Presenting another tenant's
    # session_id gets a FRESH session, never cross-site history/products.
    # (Legacy sessions without a tenant key are aedleader's.)
    existing = sessions.get(session_id)
    if existing is not None and existing.get("tenant", DEFAULT_TENANT.slug) != tenant.slug:
        session_id = str(uuid.uuid4())
        session_token = None
    elif (request.session_id and existing is None
          and is_safe_session_id(session_id)
          and _resume_check(session_id, session_token, tenant.slug) is None):
        # Mint a new ID when a supplied session cannot resume; preserve any existing disk record.


        session_id = str(uuid.uuid4())
        session_token = None

    # Per-session lock: prevent race conditions from rapid messages / double-clicks
    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        return await _process_chat(request.message, session_id, session_token, start_time, tenant)


def _enforce_tenant_permalinks(cards, tenant):
    """Drop cards whose permalink host belongs to a different store and log the mismatch."""
    from urllib.parse import urlparse
    allowed = set()
    for u in (tenant.store_url, *tenant.origins):
        host = urlparse(u).netloc.lower()
        allowed.add(host)
        allowed.add(host.removeprefix("www."))
        allowed.add("www." + host.removeprefix("www."))
    kept = []
    for c in cards:
        host = urlparse(getattr(c, "permalink", "") or "").netloc.lower()
        if host and host not in allowed:
            print(f"  [LEAK-GUARD] DROPPED card {c.product_id} — permalink host "
                  f"{host!r} is not {tenant.slug}'s store! Investigate immediately.")
            continue
        kept.append(c)
    return kept


def _merge_lead_fields(session, session_id, tenant, fields, source: str = "regex") -> dict:
    """Merge validated lead fields, preserving contact ownership and change history.
    Record changed field names in events without exposing their values."""
    lead_new = {}
    lead = session.setdefault("lead", {})
    turn = len(session.get("debug_log", [])) + 1
    owner = lead.get("contact_owner")
    for k in ("email", "phone", "name", "company"):
        v = fields.get(k)
        if k == "name" and owner and v and v.split()[0].lower() != owner.split()[0].lower():
            # one choke point for every layer: the name stays the reachable
            # person; whoever is chatting becomes a note (any layer, any path)
            if lead_add_note(lead, f"the person chatting is {v} (contact on file is {owner})", turn):
                ctx = session.setdefault("_turn_lead", {})
                ctx["captured"] = sorted(set(ctx.get("captured") or []) | {"notes"})
                _persist(session_id)
            continue
        if v and lead.get(k) != v:
            lead_history_append(lead, turn, k, lead.get(k), v, fields.get("source", source))
            lead[k] = v  # last-wins: corrections overwrite
            lead_new[k] = v
            hint = flag_suspicious(k, v) if k in ("email", "phone") else None
            flags = lead.setdefault("flags", {})
            if hint:
                flags[f"{k}_unverified"] = hint
            else:
                flags.pop(f"{k}_unverified", None)
    if lead_new:
        ctx = session.setdefault("_turn_lead", {})
        ctx["captured"] = sorted(set(ctx.get("captured") or []) | set(lead_new))
        persistence.append_event(session_id, "lead_captured",
                                 metadata={"tenant": tenant.slug,
                                           "fields": sorted(lead_new)})
        print(f"  [Lead] captured {sorted(lead_new)} (session {session_id[:8]}, {tenant.slug})")
        _persist(session_id)
    return lead_new


def _forget_lead_fields(session, session_id, tenant, fields) -> list:
    """Remove requested contact fields from the stored lead and persist the change."""
    lead = session.get("lead") or {}
    turn = len(session.get("debug_log", [])) + 1
    gone = []
    for k in fields:
        if lead.get(k):
            lead_history_append(lead, turn, k, lead.get(k), None, "delete")
            lead.pop(k, None)
            (lead.get("flags") or {}).pop(f"{k}_unverified", None)
            gone.append(k)
    if gone:
        ctx = session.setdefault("_turn_lead", {})
        ctx["deleted"] = gone
        persistence.append_event(session_id, "lead_deleted",
                                 metadata={"tenant": tenant.slug, "fields": gone})
        print(f"  [Lead] deleted {gone} on request (session {session_id[:8]})")
        _persist(session_id)
    return gone


def _lead_ack_response(session, session_id, session_token, tenant,
                       user_message, start_time) -> "ChatResponse":
    """Acknowledge stored contact changes without model generation or product retrieval."""
    lead = session.get("lead", {})
    ctx = session.get("_turn_lead") or {}
    reach_bits = [b for b in (lead.get("phone"), lead.get("email")) if b]
    first = f", {lead['name'].split()[0]}" if lead.get("name") else ""
    if ctx.get("deleted") or ctx.get("invalid"):
        # Use deterministic acknowledgements for deletions and invalid contact attempts.
        msg = (contact_sentence(lead, ctx.get("captured"), ctx.get("invalid"),
                                ctx.get("deleted"), tenant.phone)
               + f" You can also call us any time at **{tenant.phone}**.")
        session["lead_email_asked"] = True if ctx.get("invalid") == "email" else session.get("lead_email_asked")
    elif reach_bits:
        msg = (f"Thank you{first}! I've noted your details - our team can reach you at "
               f"{' or '.join(reach_bits)}. You can also call us any time at "
               f"**{tenant.phone}**. Is there anything else I can help you find today?")
    elif not session.get("lead_email_followup_asked"):
        # A name alone is not a contact method; request a reachable address before claiming follow-up.


        session["lead_email_followup_asked"] = True
        session["lead_email_asked"] = True  # This response consumes the once-per-conversation contact request.
        _persist(session_id)
        msg = (f"Thank you{first}! And what's the best email to reach you at, "
               "in case we get disconnected? You can also call us any time at "
               f"**{tenant.phone}**.")
    else:
        msg = (f"Thank you{first}! I've noted that. You can reach us any time at "
               f"**{tenant.phone}**. Is there anything else I can help you find today?")
    add_to_history(session_id, "user", user_message)
    add_to_history(session_id, "assistant", msg)
    elapsed = round(time.time() - start_time, 2)
    bar = "=" * 60
    print(f"\n{bar}")
    print(f"[Chat] Turn for session {session_id[:8]}...")
    print(f"  User: {user_message}")
    print(f"  [Lead] contact-only message - deterministic ack ({elapsed}s)")
    print(f"{bar}\n")
    _append_debug(session_id, session, {
        "turn": len(session["debug_log"]) + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_message": user_message,
        "analysis": {"intent": "lead_contact"},
        "search": None,
        "response_preview": msg[:200],
        "products_shown": [],
        "guardrails": {"cards_requested": 0, "cards_passed": 0, "hallucinated": 0},
        "time_seconds": elapsed,
    })
    return ChatResponse(
        message=msg, product_cards=[], upsell_message=None, upsell_cards=[],
        follow_up_question=None, session_id=session_id, session_token=session_token,
    )


def _retrieval_backstop_applies(intent, needs_retrieval, search_query) -> bool:
    """Require retrieval for FAQ turns that include a usable product search query.
    Pure policy questions with no search query remain retrieval-free."""
    return intent == "faq" and not needs_retrieval and bool((search_query or "").strip())


# Distinguish retrieval not attempted from retrieval attempted with no matches.


_NO_SEARCH_STATUS = (
    "## SEARCH STATUS: No product search was run this turn. You have NO "
    "knowledge of what the catalog does or does not contain. NEVER state or "
    "imply that we do not sell/offer/carry/rent something. If the customer "
    "asks about an offering, say you can check — ask one clarifying question "
    "— or point them to our phone number.")
_EMPTY_SEARCH_STATUS = (
    "## SEARCH STATUS: A full catalog search ran (filters relaxed) and found "
    "no in-stock match. You may say you couldn't find it; offer the closest "
    "alternative or our phone number. Never phrase it as 'we don't offer' — "
    "a search miss is not proof of absence.")


def _search_status_text(search_results) -> str:
    """No-products context for step4: None = no search ran; dict = real miss."""
    return _NO_SEARCH_STATUS if search_results is None else _EMPTY_SEARCH_STATUS


def _build_knowledge_context(k_chunks) -> str | None:
    """Build a knowledge-context block for FAQ and product-retrieval paths.
    Curated FAQ content takes precedence over retrieved chunks."""
    if not k_chunks:
        return None
    parts = [
        f"### {c.get('page_title', '')} — {c.get('section', '')}\n"
        f"Source link: {c.get('source_url', '')}\n{(c.get('text') or '')[:1400]}"
        for c in k_chunks
    ]
    return (
        "## SITE KNOWLEDGE (retrieved from our own website)\n"
        "Use this to answer the question, and include the most relevant Source link "
        "verbatim in your reply so the customer can read more. RULES: if anything here "
        "conflicts with the FAQ KNOWLEDGE section of your instructions, FAQ KNOWLEDGE "
        "wins. Do NOT state shipping, pricing, return, or payment policies from this "
        "material — those are answered only from FAQ KNOWLEDGE.\n\n"
        + "\n\n---\n\n".join(parts)
    )


def _lead_context_note(session, tenant):
    """Build the authoritative lead state for response generation: stored fields
    and this turn's captures, invalid attempts, and deletions."""
    ctx = session.get("_turn_lead") or {}
    return build_lead_state(session.get("lead") or {}, captured=ctx.get("captured"),
                            invalid=ctx.get("invalid"), deleted=ctx.get("deleted"))


_LEAD_ASK_LINE = "What's the best email to reach you at, in case we get disconnected?"
_LEAD_EXIT_LINE = ("If you'd like our team to follow up, just leave your email here — no pressure.")
_TRAILING_Q_RE = re.compile(r"(?:^|(?<=[.!]\s)|(?<=\n))([^.!?\n]{8,}\?)\s*$")


def _lead_ask_allowed(session, tenant) -> bool:
    return (tenant.lead_ask_mode in ("on", "first_answer")
            and not contactable(session.get("lead") or {})
            and not session.get("lead_email_asked")
            and not (session.get("_turn_lead") or {}).get("void"))  # not on a hypothetical/refusal turn


def _apply_lead_ask(msg: str, session, tenant, *, helped: bool, product_turn: bool) -> str:
    """Ask once for email after a substantive answer, without gating assistance.
    Preserve questions needed by the purchase flow; a generic trailing product
    question may be replaced when configured."""
    if not helped or not _lead_ask_allowed(session, tenant):
        return msg
    if _ASK_RE_MODEL.search(msg or ""):
        session["lead_email_asked"] = True  # the model asked — that was the ask
        return msg
    questions = re.findall(r"[^.!?\n]*\?", msg or "")
    if any(_GATHER_Q_RE.search(q) for q in questions):
        return msg  # a flow question anywhere in the reply: defer, never stack
    trailing_q = _TRAILING_Q_RE.search(msg or "")
    if trailing_q:
        q = trailing_q.group(0)
        if _GATHER_Q_RE.search(q) or not product_turn or not LEAD_ASK_REPLACES_QUESTION:
            # Preserve questions needed for compatibility or qualification and defer the contact request.


            return msg
        stripped = _TRAILING_Q_RE.sub("", msg or "").rstrip()
        if len(stripped) >= 20:  # never leave an empty reply behind the ask
            session["lead_email_asked"] = True
            return stripped + "\n\n" + _LEAD_ASK_LINE
        return msg
    session["lead_email_asked"] = True
    return (msg or "").rstrip() + "\n\n" + _LEAD_ASK_LINE


# a trailing question that gathers data the flow depends on (vs a generic
# "Would you like the package?" closer that the cards already answer)
_GATHER_Q_RE = re.compile(
    r"\b(?:which|what|how many|how much|how large|how big|do you (?:have|currently|already|know)|"
    r"are you|will (?:it|the|this)|is (?:it|this|the)|where|when)\b", re.IGNORECASE)


LEAD_ASK_REPLACES_QUESTION = os.getenv("LEAD_ASK_REPLACES_QUESTION", "1") == "1"
_ASK_RE_MODEL = re.compile(
    r"\b(?:may I have|could I (?:get|have)|what'?s (?:your|the best)|can I get|share your)\b"
    r"[^.!?\n]*\b(?:email|e-mail|phone|number)\b", re.IGNORECASE)


def _finalize_reply(msg: str, session, session_id, tenant, *, helped: bool,
                    product_turn: bool, model_generated: bool = True) -> str:
    """Assemble contact acknowledgements, remove unsupported model claims, and
    apply the once-per-conversation follow-up request when eligible."""
    ctx = session.get("_turn_lead") or {}
    lead = session.get("lead") or {}
    body = msg or ""
    if model_generated:
        # Replace unsupported outreach claims with a request for contact information.


        body, removed, replaced = strip_contact_claims(
            body, lead, store_values=(tenant.phone, tenant.email),
            strip_asks=bool(session.get("lead_email_asked")) or contactable(lead),
            replacement=None if contactable(lead) else _NOT_REACHABLE_LINE)
        if removed:
            print(f"  [Lead-guard] stripped {len(removed)} sentence(s) from the model reply: "
                  f"{[r[:60] for r in removed]}")
        if replaced:
            session["lead_email_asked"] = True
    lead_line = contact_sentence(lead, ctx.get("captured"), ctx.get("invalid"),
                                 ctx.get("deleted"), tenant.phone)
    if lead_line:
        # Avoid duplicate model acknowledgements after the system confirms a contact change.


        body = _CONFIRM_RE.sub("", body)
        for _ in range(2):
            body = _MODEL_ACK_RE.sub("", body, count=1)
        body = re.sub(r"[ \t]{2,}", " ", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        body = (lead_line + "\n\n" + body).strip() if body.strip() else lead_line
    return _apply_lead_ask(body, session, tenant, helped=helped, product_turn=product_turn)


_NOT_REACHABLE_LINE = ("I don't have a way to reach you yet — leave your email or phone number "
                       "here if you'd like our team to follow up.")
# a short leading acknowledgement sentence (≤ 80 chars, ends in . or !)
_MODEL_ACK_RE = re.compile(
    r"^\s*(?:thanks?|thank you|got it|great|perfect|noted|understood|no problem|sure)"
    r"[^.!?\n]{0,80}[.!]\s*", re.IGNORECASE)


def _resolve_product_reference(user_message, last_products):
    """
    Deterministic resolver: if the user references a previously shown product
    by position ("the second one", "#2") or implicitly ("tell me more", "that one"),
    return the product dict. No LLM involved.

    Returns: (product_dict, position) or (None, None)
    """
    if not last_products:
        return None, None

    msg = user_message.lower().strip()

    # Ordinal patterns: "the first one", "the second one", "the 3rd one", "#2", "number 2"
    ordinal_map = {
        "first": 0, "1st": 0, "#1": 0, "number 1": 0,
        "second": 1, "2nd": 1, "#2": 1, "number 2": 1,
        "third": 2, "3rd": 2, "#3": 2, "number 3": 2,
        "fourth": 3, "4th": 3, "#4": 3, "number 4": 3,
        "fifth": 4, "5th": 4, "#5": 4, "number 5": 4,
    }

    for pattern, idx in ordinal_map.items():
        if pattern in msg and idx < len(last_products):
            return last_products[idx], idx + 1

    # Implicit references: "that one", "this one", "tell me more" → first product (most prominent)
    implicit_patterns = ["tell me more", "that one", "this one", "more about it", "more details", "more info"]
    for pattern in implicit_patterns:
        if pattern in msg:
            return last_products[0], 1

    return None, None


async def _process_chat(user_message, session_id, session_token, start_time, tenant=None):
    """Process chat message (called under per-session lock)."""
    tenant = tenant or DEFAULT_TENANT
    session = get_session(session_id, token=session_token, tenant_slug=tenant.slug)
    session["tenant"] = tenant.slug  # stamp on creation; chat() guards mismatches
    session_token = session["token"]  # authoritative token for responses
    history = session["history"]

    # Enforce the message limit against the complete session transcript.


    user_msg_count = sum(1 for m in history if m.get("role") == "user")
    if user_msg_count >= SESSION_MSG_CAP:
        raise HTTPException(
            status_code=429,
            detail=(
                f"This conversation has reached its message limit ({SESSION_MSG_CAP}). "
                "Please start a new chat to continue."
            ),
        )

    # Parse contact details before retrieval; semantic extraction refines their meaning.


    session["_turn_lead"] = {}
    last_bot = next((m["content"] for m in reversed(session.get("history") or [])
                     if m.get("role") == "assistant"), None)
    normalized_message = normalize_contact_text(user_message)
    pipeline_message = normalized_message
    lead_on_file = session.get("lead") or {}
    lead_found = extract_lead(user_message, lead_on_file)
    # A bare reply to OUR OWN ask/offer is the answer (any language). Single-token
    # names wait for the analysis intent (post-step1) — everything else lands now.
    ask_reply = capture_ask_reply(user_message, last_bot, lead_on_file)
    if ask_reply and (len(user_message.split()) > 1 or "name" not in ask_reply):
        for k in ("email", "phone", "name"):
            if ask_reply.get(k) and not lead_found.get(k):
                lead_found[k] = ask_reply[k]
        lead_found["confident"] = True
        lead_found["source"] = "ask_reply"
    forget = detect_forget(user_message)

    # Use validated semantic extraction for ownership, corrections, refusals, and sales notes.


    understood = await run_in_threadpool(
        lead_extractor.extract, user_message, last_bot, lead_on_file, lead_found or None)
    session["_turn_lead"]["extractor_ran"] = understood is not None
    if understood is not None:
        if understood.get("hypothetical") or understood.get("refused"):
            # nothing in this message is an offered contact — regex shapes are void
            for k in ("email", "phone", "name"):
                lead_found.pop(k, None)
            if understood.get("refused"):
                session["lead_email_asked"] = True  # never re-ask after a refusal
            # a hypothetical ("if I gave you test@example.com…") is neither an
            # attempt to re-type nor the moment to ask for their email
            session["_turn_lead"]["void"] = True
        else:
            for k in ("email", "phone"):
                if understood.get(k):
                    lead_found[k] = understood[k]
                    lead_found["confident"] = True
            # When semantic extraction ran, its validated name result takes precedence over regex guesses.


            if understood.get("name"):
                lead_found["name"] = understood["name"]
            else:
                lead_found.pop("name", None)
            if understood.get("company"):
                lead_found["company"] = understood["company"]
            owner = understood.get("contact_belongs_to")
            if owner and (understood.get("email") or understood.get("phone")):
                # name and email must be the SAME person: the reachable one
                self_name = understood.get("name") or lead_found.get("name")
                lead_found["name"] = owner
                session.setdefault("lead", {})["contact_owner"] = owner
                if not self_name or self_name.split()[0].lower() != owner.split()[0].lower():
                    lead_add_note(session.setdefault("lead", {}),
                                  (f"chatted with {self_name}; contact details belong to {owner}"
                                   if self_name else
                                   f"contact details belong to {owner} (third party) — customer's own name not given"),
                                  len(session.get("debug_log", [])) + 1)
            for n in understood.get("notes") or []:
                if lead_add_note(session.setdefault("lead", {}), n["text"],
                                 len(session.get("debug_log", [])) + 1, kind=n.get("kind")):
                    session["_turn_lead"].setdefault("captured", [])
                    if "notes" not in session["_turn_lead"]["captured"]:
                        session["_turn_lead"]["captured"].append("notes")
                    _persist(session_id)
        forget = sorted(set(forget) | set(understood.get("forget") or []))
    # Treat a removed field with a valid replacement in this message as a correction.


    forget = [f for f in forget if not lead_found.get(f)]
    if forget:
        _forget_lead_fields(session, session_id, tenant, forget)
    # Keep contact ownership consistent; a different speaker name becomes a note.


    if lead_found.get("email") or lead_found.get("phone") or lead_found.get("name") or lead_found.get("company"):
        _merge_lead_fields(session, session_id, tenant, lead_found)
        if lead_found.get("confident"):
            # Phone/email must not reach the SKU/search layer as query tokens
            # ("619-555-0142" is also a valid SKU shape in this catalog).
            from leads import _PHONE_RE, _EMAIL_RE
            pipeline_message = _EMAIL_RE.sub(" ", pipeline_message)
            if lead_found.get("phone"):
                pipeline_message = _PHONE_RE.sub(" ", pipeline_message)
    bad = None if session["_turn_lead"].get("void") else invalid_attempt(user_message, lead_found)
    if bad:
        # The customer TRIED to share contact and it did not validate: the
        # reply layer asks them to re-type it and forbids confirming it.
        session["_turn_lead"]["invalid"] = bad
        print(f"  [Lead] invalid {bad} attempt — asking to re-type")

    # Use contact-only acknowledgements only when there is no pending question or product thread.


    _open_thread = bool(last_bot and "?" in last_bot) or bool(session.get("last_products"))
    if (lead_found or forget or bad) and is_contact_only(user_message, lead_found or {"name": "x"}) \
            and not _open_thread:
        return _lead_ack_response(session, session_id, session_token, tenant,
                                  user_message, start_time)

    # Step 0: Resolve product references deterministically (before LLM)
    referenced_product, ref_position = _resolve_product_reference(
        pipeline_message, session.get("last_products", [])
    )

    if referenced_product:
        print(f"\n{'='*60}")
        print(f"[Chat] Turn for session {session_id[:8]}...")
        print(f"  User: {user_message}")
        print(f"  [Step 0] Product reference resolved: #{ref_position} → {referenced_product['name']} (ID:{referenced_product['product_id']})")

        # Direct lookup — skip query analysis and search entirely
        t_lookup = time.time()
        # Blocking Qdrant call → threadpool so the event loop keeps serving
        # other requests (admin, health, other tenants) during this turn.
        search_results = await run_in_threadpool(
            lookup_product_by_id, referenced_product["product_id"],
            collection=tenant.collection)
        t_lookup = round(time.time() - t_lookup, 2)
        print(f"  [Step 2] Direct lookup ({t_lookup}s)")

        if search_results and search_results["products"]:
            # Price verification — direct lookup is always one product
            ref_lookup = {p["product_id"]: p for p in search_results["products"]}
            await verify_picked_prices(ref_lookup, list(ref_lookup.keys()), tenant=tenant)

            # Generate response — tell LLM exactly which product the customer asked about
            ref_name = search_results["products"][0]["name"]
            ref_context = f"## CUSTOMER ASKED ABOUT: {ref_name}\nThe customer selected this specific product from the previously shown list. Describe THIS product. Do NOT describe any other product."
            llm_response = await run_in_threadpool(
                step4_generate_response, user_message, search_results, history,
                extra_context=ref_context, tenant=tenant)
            product_cards, upsell_cards = step5_guardrails(llm_response, search_results)
            product_cards = _enforce_tenant_permalinks(product_cards, tenant)
            upsell_cards = _enforce_tenant_permalinks(upsell_cards, tenant)

            # Update history and session
            add_to_history(session_id, "user", user_message)
            msg = _finalize_reply(llm_response.get("message", ""), session, session_id,
                                  tenant, helped=True, product_turn=True)
            product_summary = _build_product_summary(search_results)
            add_to_history(session_id, "assistant", msg + product_summary)

            products_shown = [
                {"product_id": p["product_id"], "name": p["name"], "price": p["price"], "brand": p["brand"], "score": 1.0}
                for p in search_results["products"]
            ]
            session["last_products"] = products_shown
            # Persist the maximum number of cards displayed, independently of debug-log retention.

            session["max_products_shown"] = max(
                session.get("max_products_shown", 0), len(product_cards) + len(upsell_cards)
            )

            elapsed = round(time.time() - start_time, 2)
            print(f"  [Done] {elapsed}s — Direct product reference")
            print(f"    Response: {msg[:120]}...")
            print(f"{'='*60}\n")

            _append_debug(session_id, session, {
                "turn": len(session["debug_log"]) + 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "user_message": user_message,
                "analysis": {"intent": "reference_resolver",
                             "resolved_product_id": referenced_product["product_id"]},
                "search": {"products_found": len(search_results["products"]),
                           "direct_lookup": True},
                "products_shown": products_shown,
                "response_preview": msg[:200],
                "guardrails": {"cards_requested": len(product_cards) + len(upsell_cards),
                               "cards_passed": len(product_cards) + len(upsell_cards),
                               "hallucinated": 0},
                "time_seconds": elapsed,
            })

            return ChatResponse(
                message=msg,
                product_cards=product_cards,
                upsell_message=llm_response.get("upsell_message"),
                upsell_cards=upsell_cards,
                follow_up_question=llm_response.get("follow_up_question"),
                session_id=session_id,
                session_token=session_token,
            )

    # Run blocking query analysis in the threadpool.


    t_analysis = time.time()
    analysis = await run_in_threadpool(step1_analyze_query, pipeline_message, history)
    t_analysis = round(time.time() - t_analysis, 2)

    # Validate analysis-proposed names against the message when the dedicated extractor did not run.


    _extractor_ran = bool((session.get("_turn_lead") or {}).get("extractor_ran"))
    # The dedicated extractor takes precedence over the query analyzer for contact ownership.


    llm_name = None if _extractor_ran else validate_llm_name(analysis.get("customer_name"), user_message)
    if not llm_name and not (session.get("lead") or {}).get("name") and not _extractor_ran:
        # Use a bare-name fallback only when semantic extraction is unavailable and intent permits it.

        single = capture_ask_reply(user_message, last_bot, session.get("lead") or {},
                                   intent=analysis.get("intent"))
        llm_name = single.get("name")
    if llm_name:
        _merge_lead_fields(session, session_id, tenant, {"name": llm_name}, source="llm")
        if is_contact_only(user_message, {**(lead_found or {}), "name": llm_name}) and not _open_thread:
            return _lead_ack_response(session, session_id, session_token, tenant,
                                      user_message, start_time)

    intent = analysis.get("intent", "unknown")
    search_query = analysis.get("search_query", "")
    filters = analysis.get("filters", {})
    needs_retrieval = analysis.get("needs_retrieval", True)
    journey_stage = analysis.get("journey_stage", "unknown")

    print(f"\n{'='*60}")
    print(f"[Chat] Turn for session {session_id[:8]}...")
    print(f"  User: {user_message}")
    print(f"  [Step 1] Query Analysis ({t_analysis}s)")
    print(f"    Intent:     {intent}")
    print(f"    Journey:    {journey_stage}")
    print(f"    Search:     {search_query or '(none)'}")
    print(f"    Filters:    {json.dumps(filters) if filters else '(none)'}")
    # Extract configurator info
    config_info = analysis.get("configurator_info") or {}
    missing_fields = config_info.get("missing_fields", [])
    ready_to_search = config_info.get("ready_to_search", True) if intent == "configurator" else None

    print(f"    Retrieval:  {needs_retrieval}")
    if intent == "configurator":
        print("    [Configurator]")
        print(f"      Environment:  {config_info.get('environment', '?')}")
        print(f"      Budget:       {config_info.get('budget', '?')}")
        print(f"      Audience:     {config_info.get('audience', '?')}")
        print(f"      Pediatric:    {config_info.get('pediatric', '?')}")
        print(f"      Package:      {config_info.get('package_preference', '?')}")
        print(f"      Ready:        {ready_to_search}")
        print(f"      Missing:      {missing_fields or '(none)'}")

    # Search FAQ turns with a usable product query even when the retrieval flag was omitted.


    if _retrieval_backstop_applies(intent, needs_retrieval, search_query):
        needs_retrieval = True
        analysis["needs_retrieval"] = True
        print("  [Retrieval-backstop] faq turn carries a search_query — forcing search")

    # Route course questions to the configured course category without intercepting hardware questions.


    if _course_flip_applies(tenant, intent, needs_retrieval, config_info, filters,
                            user_message):
        needs_retrieval = True
        analysis["needs_retrieval"] = True  # step2_search re-reads the dict
        filters = dict(filters or {})
        filters["top_category"] = tenant.course_category
        analysis["filters"] = filters
        if not search_query:
            search_query = "CPR AED first aid training course certification"
            analysis["search_query"] = search_query
        print(f"  [Course-path] {intent} flipped to product search "
              f"(top_category={tenant.course_category!r})")

    # Handle non-retrieval intents (greetings, out-of-scope, configurator gathering)
    if not needs_retrieval:
        print(f"  [Skip] No retrieval needed — intent: {intent}")

        if intent == "human_handoff" or wants_human(user_message):
            # Combine semantic intent and deterministic human-request detection; reply with tenant contact details.


            msg = tenant.handoff_reply or HANDOFF_REPLY
            _known_phone = session.get("lead", {}).get("phone")
            _ctx = session.get("_turn_lead") or {}
            if _known_phone and ("phone" in (_ctx.get("captured") or [])
                                 or (last_bot and OFFER_PHONE_RE.search(last_bot))):
                # Acknowledge a supplied callback number rather than repeating the contact offer.


                msg = (f"Thanks — I've noted {_known_phone}. Our team will call you back at that "
                       f"number; you can also reach them directly at **{tenant.phone}** "
                       f"(Mon–Fri, 8 AM–4:30 PM PST). Anything I can help with in the meantime?")
            elif _known_phone:
                # Replace the phone request when a callback number is already stored.

                msg = re.sub(r"\s*Or just reply here with your phone number[^.]*\.", "", msg)
                msg += f" We have your number as {_known_phone} — our team will call you back."
            elif last_bot and OFFER_PHONE_RE.search(last_bot):
                # Avoid repeating the same contact paragraph on consecutive turns.


                if _ctx.get("invalid") == "phone":
                    msg = ("I couldn't read that number — could you type it as digits, "
                           f"like 619-555-0142? You can also call us at **{tenant.phone}**.")
                else:
                    msg = (f"Sure — just reply here with your phone number (digits, like "
                           f"619-555-0142) and our team will call you back, or call us "
                           f"directly at **{tenant.phone}**.")
            session["lead_email_asked"] = True  # the phone offer IS the one ask
            follow_up = None
        elif intent == "out_of_scope":
            if tenant.course_category:
                msg = "I can help with CPR/First-Aid training courses and AED equipment. Which are you interested in?"
                follow_up = "Are you looking into training for your team, or an AED for your workplace?"
            else:
                msg = "I specialize in AED equipment and first aid products. How can I help you find the right AED or accessories?"
                follow_up = "Are you looking for an AED for your workplace, school, or organization?"
        elif intent == "greeting" and is_closer(user_message):
            # Use deterministic acknowledgements for closers; true greetings continue through generation.


            turn_n = len(session.get("debug_log", []))
            _CLOSER_REPLIES = (
                "You're welcome! If you have any more questions about AEDs or accessories, just let me know.",
                "Glad I could help! I'm here whenever you need more info on AEDs, packages, or accessories.",
                "Happy to help. Reach out any time if you'd like more recommendations or product details.",
                "Anytime! Let me know if there's anything else I can help you find.",
            )
            _CLOSER_REPLIES_COURSE = (
                "You're welcome! If you have any more questions about our courses, AEDs, or accessories, just let me know.",
                "Glad I could help! I'm here whenever you need info on training courses, AEDs, or accessories.",
                "Happy to help. Reach out any time about courses, products, or recommendations.",
                "Anytime! Let me know if there's anything else I can help you find.",
            )
            _replies = _CLOSER_REPLIES_COURSE if tenant.course_category else _CLOSER_REPLIES
            msg = _replies[turn_n % len(_replies)]
            # Offer one optional follow-up request on exit if no contact method is stored.


            if _lead_ask_allowed(session, tenant) and not session.get("lead_exit_asked"):
                session["lead_exit_asked"] = True
                session["lead_email_asked"] = True
                msg += " " + _LEAD_EXIT_LINE
            follow_up = None
        elif intent == "faq":
            # Include available educational chunks; curated FAQ content takes precedence.


            k_chunks = await run_in_threadpool(
                search_knowledge, user_message, f"aed_knowledge_{tenant.slug}")
            k_ctx = _build_knowledge_context(k_chunks)
            if k_ctx:
                print(f"  [Knowledge] {len(k_chunks)} chunks (aed_knowledge_{tenant.slug}): "
                      f"{[c.get('page_slug') for c in k_chunks]}")
            _ln = _lead_context_note(session, tenant)
            _ctx = "\n\n".join(x for x in (k_ctx, _ln) if x) or None
            llm_response = await run_in_threadpool(
                step4_generate_response, user_message, None, history,
                extra_context=_ctx, tenant=tenant)
            msg = llm_response.get("message", "Great question! Let me help with that.")
            msg = _finalize_reply(msg, session, session_id, tenant, helped=True, product_turn=False)
            follow_up = llm_response.get("follow_up_question")
        elif intent == "configurator" and not ready_to_search:
            # Configurator: gathering info — pass context to LLM so it asks the right questions
            configurator_context = _build_configurator_context(config_info, tenant=tenant)
            _ln = _lead_context_note(session, tenant)
            if _ln:
                configurator_context = configurator_context + "\n\n" + _ln
            llm_response = await run_in_threadpool(
                step4_generate_response, user_message, None, history,
                extra_context=configurator_context, tenant=tenant,
            )
            msg = llm_response.get("message",
                "I'd love to help! Are you interested in training for your team, or AED equipment?"
                if tenant.course_category
                else "I'd love to help you find the right AED! Let me ask a few questions.")
            # gathering turn: NOT "helped" — the ask never stacks on a qualifier
            msg = _finalize_reply(msg, session, session_id, tenant, helped=False, product_turn=False)
            follow_up = llm_response.get("follow_up_question")
        else:
            llm_response = await run_in_threadpool(
                step4_generate_response, user_message, None, history,
                extra_context=_lead_context_note(session, tenant), tenant=tenant)
            msg = llm_response.get("message",
                "Hello! How can I help — training for your team, or AED equipment?"
                if tenant.course_category
                else "Hello! How can I help you find the right AED equipment today?")
            # greetings / bare openers: not a substantive answer yet
            msg = _finalize_reply(msg, session, session_id, tenant, helped=False, product_turn=False)
            follow_up = llm_response.get("follow_up_question")

        add_to_history(session_id, "user", user_message)
        add_to_history(session_id, "assistant", msg)

        elapsed = round(time.time() - start_time, 2)
        print(f"  [Done] {elapsed}s — No products (non-retrieval)")
        print(f"    Response: {msg[:120]}...")
        if follow_up:
            print(f"    Follow-up: {follow_up}")
        print(f"{'='*60}\n")

        debug_entry = {
            "turn": len(session["debug_log"]) + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_message": user_message,
            "analysis": analysis,
            "search": None,
            "response_preview": msg[:200],
            "products_shown": [],
            "guardrails": {"cards_requested": 0, "cards_passed": 0, "hallucinated": 0},
            "time_seconds": elapsed,
        }
        _append_debug(session_id, session, debug_entry)

        return ChatResponse(
            message=msg,
            product_cards=[],
            upsell_message=None,
            upsell_cards=[],
            follow_up_question=follow_up,
            session_id=session_id,
            session_token=session_token,
        )

    # Step 2: Hybrid Search
    t_search = time.time()
    try:
        search_results = await run_in_threadpool(step2_search, analysis, tenant)
    except Exception as e:
        print(f"  [Step 2] Search FAILED: {e}")
        search_results = None
    t_search = round(time.time() - t_search, 2)

    products_found = len(search_results.get("products", [])) if search_results else 0
    filters_used = search_results.get("filters_used") if search_results else None
    was_relaxed = search_results.get("was_relaxed", False) if search_results else False
    out_of_scope = search_results.get("out_of_scope", False) if search_results else False

    # Record in-domain zero-result searches for operational monitoring.


    if search_results is not None and products_found == 0 and not out_of_scope:
        persistence.append_event(
            session_id,
            "zero_results",
            metadata={
                "query": str(analysis.get("search_query") or "")[:200],
                "filters": filters_used or {},
                "tenant": tenant.slug,
            },
        )

    print(f"  [Step 2] Hybrid Search ({t_search}s)")
    print(f"    Found:      {products_found} products")
    print(f"    Filters:    {json.dumps(filters_used) if filters_used else '(none)'}")
    if was_relaxed:
        print("    Relaxed:    YES — filters were loosened to find results")
    if search_results and search_results.get("oos_matches"):
        print(f"    OOS-match:  {[p['name'][:40] for p in search_results['oos_matches']]}")
    if out_of_scope:
        print("    Out-of-scope: YES — domain gate rejected query")
    if search_results and search_results.get("products"):
        for i, p in enumerate(search_results["products"][:5], 1):
            print(f"    #{i} {p['name'][:50]} — ${p['price']} ({p['brand']}) score={round(p.get('_score', 0), 4)}")

    # Verify selected product prices after response generation.


    # Filter upsells already shown in this session to avoid repetition
    shown_upsells = session.get("shown_upsell_ids", set())
    if shown_upsells and search_results and search_results.get("upsells"):
        before = len(search_results["upsells"])
        search_results["upsells"] = [u for u in search_results["upsells"] if u.get("product_id") not in shown_upsells]
        filtered = before - len(search_results["upsells"])
        if filtered:
            print(f"  [Filter] {filtered} upsell(s) suppressed (already shown this session)")

    # Step 4: LLM Response Generation
    # Tell the LLM about filtered upsells AND AEDTS state so it doesn't pitch
    # the same thing twice in one conversation.
    extras = []
    if shown_upsells:
        extras.append(
            "## NOTE: The following product IDs were already recommended as upsells earlier in this conversation. Do NOT mention them again: "
            + ", ".join(str(pid) for pid in shown_upsells) + "."
        )
    if session.get("shown_aedts") and not _user_asked_about_aedts(user_message):
        extras.append(
            "## NOTE: The AED Total Solution add-on has already been mentioned earlier in this conversation. "
            "Do NOT bring it up again. Do NOT describe its features (physician oversight, prescription handling, "
            "compliance documentation, pad/battery expiration reminders, post-event services) — even paraphrased. "
            "The customer has already heard the pitch. ONLY describe AEDTS if the user explicitly asks about it."
        )
    if (tenant.course_category and search_results and search_results.get("products")
            and (analysis.get("filters") or {}).get("top_category") == tenant.course_category):
        # Require course cards when sellable course results are available.


        extras.append(
            "## COURSE RESULTS: The SEARCH RESULTS above are this store's sellable group "
            "training courses. The customer is asking about training — you MUST include the "
            "relevant courses as product_cards (use their IDs). Never answer a course question "
            "text-only while course results are present; the cards are how the customer books."
        )
    if search_results and search_results.get("oos_matches"):
        oos_lines = "; ".join(
            f"{p['name']} (SKU {p.get('sku') or 'n/a'})" for p in search_results["oos_matches"]
        )
        extras.append(
            "## OUT-OF-STOCK MATCHES: These closely-matching products exist in our catalog "
            f"but are currently OUT OF STOCK: {oos_lines}. If one of these is what the customer "
            "is actually asking for, say honestly that we carry it but it is currently out of "
            f"stock and they can call {tenant.phone} for lead time — do NOT silently substitute "
            "a different model as if it were the same product. Do not include these in product_cards."
        )
    # Retain FAQ knowledge citations when a turn also performs product retrieval.


    if intent == "faq":
        _k_chunks = await run_in_threadpool(
            search_knowledge, user_message, f"aed_knowledge_{tenant.slug}")
        _k_ctx = _build_knowledge_context(_k_chunks)
        if _k_ctx:
            extras.append(_k_ctx)
            print(f"  [Knowledge] {len(_k_chunks)} chunks alongside product search "
                  f"(aed_knowledge_{tenant.slug})")
    _ln = _lead_context_note(session, tenant)
    if _ln:
        extras.append(_ln)
    upsell_context = "\n\n".join(extras) if extras else None

    # Fetch live prices concurrently with response generation; the model receives indexed prices.


    products_by_id = {}
    if search_results:
        for p in (search_results.get("products") or []):
            products_by_id[p["product_id"]] = p
        for p in (search_results.get("upsells") or []):
            products_by_id[p["product_id"]] = p
    prefetch = asyncio.create_task(fetch_live_products(
        list(products_by_id)[:PRICE_VERIFY_CAP], tenant)) if products_by_id else None

    t_llm = time.time()
    try:
        llm_response = await run_in_threadpool(
            step4_generate_response, user_message, search_results, history,
            extra_context=upsell_context, tenant=tenant)
    except Exception as e:
        print(f"  [Step 4] LLM Response FAILED: {e}")
        llm_response = {
            "message": "I'm having trouble generating a response right now. Please try again in a moment.",
            "product_cards": [],
            "upsell_message": None,
            "upsell_cards": [],
            "follow_up_question": "Could you repeat your question?",
        }
    t_llm = round(time.time() - t_llm, 2)
    print(f"  [Step 4] LLM Response ({t_llm}s)")

    # Apply prefetched live data only to selected cards.


    picked_ids = []
    for c in llm_response.get("product_cards") or []:
        pid = c.get("product_id")
        if pid is not None:
            picked_ids.append(pid)
    for c in llm_response.get("upsell_cards") or []:
        pid = c.get("product_id")
        if pid is not None:
            picked_ids.append(pid)
    t_verify = time.time()
    prefetched = await prefetch if prefetch else None  # never leave threads behind the response
    if picked_ids and products_by_id:
        await verify_picked_prices(products_by_id, picked_ids, tenant=tenant,
                                   prefetched=prefetched)
    t_verify = round(time.time() - t_verify, 2)
    print(f"  [Step 3*] Live Price Verify (post-LLM, parallel, cap={PRICE_VERIFY_CAP}) ({t_verify}s) — {min(len(set(picked_ids)), PRICE_VERIFY_CAP)} IDs")

    # Step 5: Guardrails — build cards from DB, not LLM
    product_cards, upsell_cards = step5_guardrails(llm_response, search_results)
    product_cards = _enforce_tenant_permalinks(product_cards, tenant)
    upsell_cards = _enforce_tenant_permalinks(upsell_cards, tenant)

    # Suppress program upsells alongside AED machines only for tenants configured in suppress mode.


    aedts_ids = set(tenant.aedts_product_ids)
    if tenant.aedts_mode == "suppress" and any(
            _is_aed_machine(products_by_id.get(c.product_id, {}))
            for c in (product_cards + upsell_cards)):
        before_upsells = len(upsell_cards)
        upsell_cards = [c for c in upsell_cards if c.product_id not in aedts_ids]
        if before_upsells != len(upsell_cards):
            print("  [AEDTS-with-AED] Stripped AEDTS card from upsells")
        msg = llm_response.get("message", "")
        if _mentions_aedts(msg):
            llm_response["message"] = _strip_aedts_sentences(msg)
            print("  [AEDTS-with-AED] Stripped AEDTS sentences from message")
        up = llm_response.get("upsell_message") or ""
        if _mentions_aedts(up):
            llm_response["upsell_message"] = _strip_aedts_sentences(up)
            print("  [AEDTS-with-AED] Stripped AEDTS sentences from upsell_message")

    # Guardrail stats
    cards_requested = len(llm_response.get("product_cards", []))
    cards_passed = len(product_cards)
    hallucinated = cards_requested - cards_passed

    # Track shown upsell IDs so they don't repeat in future turns
    for c in upsell_cards:
        session.setdefault("shown_upsell_ids", set()).add(c.product_id)
        if c.product_id in aedts_ids:
            session["shown_aedts"] = True

    # Strip AEDTS sentences from text if already shown earlier this session
    # (suppress-mode tenants only — the repeat NUDGE via extra_context above is
    # universal, but sentence-stripping the reply is aedleader's hard rule).
    # Skipped when: (a) the user explicitly asked about AEDTS this turn, or
    # (b) AEDTS is being shown as a card right now (the text reinforces the card).
    resp_msg = llm_response.get("message", "")
    if (tenant.aedts_mode == "suppress"
            and session.get("shown_aedts")
            and not _user_asked_about_aedts(user_message)
            and not any(c.product_id in aedts_ids for c in upsell_cards)
            and _mentions_aedts(resp_msg)):
        stripped = _strip_aedts_sentences(resp_msg)
        if stripped != resp_msg:
            print("  [Filter] Stripped AEDTS sentences (already pitched this session)")
            llm_response["message"] = stripped

    # Mark AEDTS as shown if it survived this turn — by name OR by feature
    # description OR by upsell message OR by appearing as an upsell card.
    if not session.get("shown_aedts") and (
        _mentions_aedts(llm_response.get("message", ""))
        or _mentions_aedts(llm_response.get("upsell_message") or "")
        or any(c.product_id in aedts_ids for c in upsell_cards)
    ):
        session["shown_aedts"] = True

    # Save to history — include product summary so follow-ups can reference them
    llm_response["message"] = _finalize_reply(
        llm_response.get("message", ""), session, session_id, tenant,
        helped=True, product_turn=bool(product_cards))
    add_to_history(session_id, "user", user_message)
    assistant_msg = llm_response.get("message", "")
    product_summary = _build_product_summary(search_results)
    add_to_history(session_id, "assistant", assistant_msg + product_summary)

    # Track last products shown in DISPLAY ORDER (what the user sees)
    # Use product_cards order (LLM-decided), not search score order
    products_shown = []
    if product_cards:
        products_shown = [
            {"product_id": c.product_id, "name": c.name, "price": c.price, "brand": c.brand, "score": 1.0}
            for c in product_cards
        ]
        session["last_products"] = products_shown
    # Universal render signal: truncation-proof session max of cards shown to the
    # user (product + upsell), immune to the 50-turn debug_log roll-off.
    session["max_products_shown"] = max(
        session.get("max_products_shown", 0), len(product_cards) + len(upsell_cards)
    )

    elapsed = round(time.time() - start_time, 2)

    # Build full debug entry for this turn
    debug_entry = {
        "turn": len(session["debug_log"]) + 1,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_message": user_message,
        "analysis": analysis,
        "search": {
            "products_found": search_results.get("total_found", 0) if search_results else 0,
            "filters_used": search_results.get("filters_used") if search_results else None,
            "was_relaxed": search_results.get("was_relaxed", False) if search_results else False,
            "upsells_found": len(search_results.get("upsells", [])) if search_results else 0,
            "out_of_scope": search_results.get("out_of_scope", False) if search_results else False,
            "oos_matches": [p.get("name") for p in search_results.get("oos_matches", [])]
                           if search_results else [],
        },
        "products_shown": products_shown,
        "response_preview": assistant_msg[:200],
        "guardrails": {
            "cards_requested": cards_requested,
            "cards_passed": cards_passed,
            "hallucinated": hallucinated,
        },
        "timing": {
            "search": t_search,
            "price_verify": t_verify,
            "llm_response": t_llm,
            "total": elapsed,
        },
    }
    _append_debug(session_id, session, debug_entry)

    print("  [Step 5] Guardrails")
    print(f"    Cards:      {cards_passed}/{cards_requested} passed ({hallucinated} hallucinated)")
    print(f"    Upsells:    {len(upsell_cards)}")
    follow_up = llm_response.get("follow_up_question")
    if follow_up:
        print(f"    Follow-up:  {follow_up}")
    print(f"  [Done] {elapsed}s total")
    print(f"    Response:   {assistant_msg[:120]}...")
    print(f"{'='*60}\n")

    if hallucinated > 0:
        print(f"[Guardrail] Turn {debug_entry['turn']}: blocked {hallucinated} hallucinated product(s)")

    return ChatResponse(
        message=llm_response.get("message", ""),
        product_cards=product_cards,
        upsell_message=llm_response.get("upsell_message"),
        upsell_cards=upsell_cards,
        follow_up_question=llm_response.get("follow_up_question"),
        session_id=session_id,
        session_token=session_token,
    )


@app.get("/debug/{session_id}", dependencies=[Depends(require_admin)])
async def debug_session(session_id: str):
    """Full debug view of a session — history, products shown, pipeline trace per turn."""
    if not is_safe_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format.")
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    session = sessions[session_id]
    return {
        "session_id": session_id,
        "created_at": session["created_at"],
        "total_turns": len(session["debug_log"]),
        "history_length": len(session["history"]),
        "last_products": session.get("last_products", []),
        "debug_log": session["debug_log"],
    }


@app.get("/debug/{session_id}/turn/{turn_num}", dependencies=[Depends(require_admin)])
async def debug_turn(session_id: str, turn_num: int):
    """Debug a specific turn in a session."""
    if not is_safe_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format.")
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    log = sessions[session_id]["debug_log"]
    if turn_num < 1 or turn_num > len(log):
        raise HTTPException(status_code=404, detail=f"Turn {turn_num} not found. Session has {len(log)} turns.")
    return log[turn_num - 1]


@app.get("/sessions", dependencies=[Depends(require_admin)])
async def list_sessions():
    """List all active sessions with summary info."""
    result = []
    for sid, s in sessions.items():
        result.append({
            "session_id": sid,
            "created_at": s["created_at"],
            "turns": len(s["debug_log"]),
            "history_messages": len(s["history"]),
            "last_products_count": len(s.get("last_products", [])),
            "idle_seconds": round(time.time() - s.get("last_activity", 0)),
        })
    return {"active_sessions": len(result), "sessions": result}


@app.delete("/session/{session_id}", dependencies=[Depends(require_admin)])
async def clear_session(session_id: str):
    """Clear conversation history for a session."""
    if not is_safe_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format.")
    if session_id in sessions:
        del sessions[session_id]
        _session_locks.pop(session_id, None)
    return {"status": "cleared"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "aed-leader-chatbot"}


@app.post("/track", status_code=204)
async def track_event(req: TrackEventRequest, http_request: Request):
    """Record a public-widget event after Origin, rate, and payload validation.
    Pre-chat tracking IDs correlate events before a server session exists.
    Return an empty 204 response when the event is accepted."""
    if not _check_origin(http_request):
        raise HTTPException(status_code=403, detail="Forbidden.")

    client_ip = http_request.client.host if http_request.client else "unknown"
    if not await _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests.")

    if req.event_name not in _ALLOWED_EVENT_NAMES:
        raise HTTPException(status_code=400, detail="Unknown event_name.")

    if not is_safe_session_id(req.session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id.")

    # Multisite: stamp the resolving tenant on the event (Origin outranks any
    # client-supplied value; a spoofed slug only mislabels the caller's own event).
    _track_tenant = resolve_tenant(http_request.headers.get("origin"))
    _meta = dict(req.metadata or {})
    _meta["tenant"] = _track_tenant.slug

    # Disk append → threadpool (append-mode single-line write is safe for
    # concurrent callers — see append_event's atomicity note).
    ok = await run_in_threadpool(
        persistence.append_event,
        session_id=req.session_id,
        event_name=req.event_name,
        chip_id=req.chip_id,
        metadata=_meta,
    )
    if not ok:
        # Likely chip_id failed shape check, or oversized metadata. Don't
        # specify which — same opaque 400 for all field-level rejections.
        raise HTTPException(status_code=400, detail="Invalid event payload.")
    # 204 — no body, nothing to leak.
    return Response(status_code=204)


# Admin Dashboard endpoints
# Cookie-auth login flow for the HTML dashboard. The legacy ADMIN_TOKEN
# Bearer above (used by curl scripts) keeps working — both paths land in
# the same require_admin dependency.

class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


@app.post("/api/admin/login")
async def api_admin_login(
    request: AdminLoginRequest,
    http_request: Request,
    response: Response,
):
    """Validate rate-limited admin credentials and set an HTTP-only session cookie."""
    client_ip = http_request.client.host if http_request.client else "unknown"
    if not await _check_login_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"Too many login attempts. Try again in {LOGIN_RATE_WINDOW // 60} minutes.",
        )
    token = admin_login(request.username, request.password)
    if not token:
        # Delay failed login responses in addition to enforcing the request limit.
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    is_https = http_request.url.scheme == "https"
    response.set_cookie(
        "aed_admin_token",
        token,
        max_age=8 * 3600,
        httponly=True,
        samesite="strict",
        path="/",
        secure=is_https,
    )
    return {"status": "ok"}


@app.post("/api/admin/logout")
async def api_admin_logout(http_request: Request, response: Response):
    """Invalidate the admin token and clear the cookie."""
    token = http_request.cookies.get("aed_admin_token")
    if token:
        admin_logout(token)
    response.delete_cookie("aed_admin_token", path="/")
    return {"status": "ok"}


def _build_session_summary(sid: str, s: dict, *, live: bool) -> dict:
    """Build the summary row used by /api/admin/sessions list view."""
    history = s.get("history", [])
    user_msgs = [m["content"] for m in history if m.get("role") == "user"]
    debug_log = s.get("debug_log", [])
    last_intent = None
    last_journey = None
    hallucinations = 0
    if debug_log:
        for entry in debug_log:
            g = entry.get("guardrails") or {}
            hallucinations += int(g.get("hallucinated") or 0)
        last = debug_log[-1]
        analysis = last.get("analysis") or {}
        last_intent = analysis.get("intent")
        last_journey = analysis.get("journey_stage")
    return {
        "session_id": sid,
        "created_at": s.get("created_at"),
        "last_activity": s.get("last_activity"),
        "turn_count": len(debug_log),
        "message_count": len(history),
        "first_message": (user_msgs[0] if user_msgs else "")[:200],
        "last_message": (user_msgs[-1] if user_msgs else "")[:200],
        "last_intent": last_intent,
        "last_journey": last_journey,
        "hallucinations": hallucinations,
        "tenant": s.get("tenant", "aedleader"),
        "lead": s.get("lead", {}),
        "is_live": live,
    }


@app.get("/api/admin/sessions", dependencies=[Depends(require_admin)])
async def api_admin_sessions(site: str | None = None):
    """Merged list: live in-memory sessions + historical from disk.

    Live takes precedence — if a session is in memory, the in-memory
    snapshot wins (it has the most recent state). Sorted by last_activity
    desc so most-recent appears first.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    now = time.time()
    # Live dict is walked ON the event loop (it mutates between awaits);
    # only the disk scan (one file read per persisted session) is offloaded.
    for sid, s in sessions.items():
        is_live = (now - (s.get("last_activity") or 0)) < LIVE_THRESHOLD
        rows.append(_build_session_summary(sid, s, live=is_live))
        seen.add(sid)
    for row in await run_in_threadpool(persistence.list_persisted_sessions):
        if row["session_id"] in seen:
            continue
        rows.append(row)
    if site:
        rows = [r for r in rows if r.get("tenant", "aedleader") == site]
    rows.sort(key=lambda r: r.get("last_activity") or 0, reverse=True)
    return {"count": len(rows), "sessions": rows}


@app.get("/api/admin/leads", dependencies=[Depends(require_admin)])
async def api_admin_leads(site: str | None = None):
    """Captured leads (name/email/phone) across live + persisted sessions.

    Rides the same merged summary rows as /api/admin/sessions - no extra disk
    pass. The universal admin site switcher scopes it via ?site=.
    """
    _validate_site(site)
    rows: list[dict] = []
    seen: set[str] = set()
    now = time.time()
    for sid, sess in sessions.items():
        seen.add(sid)
        lead = sess.get("lead") or {}
        if lead:
            rows.append({"session_id": sid, "tenant": sess.get("tenant", "aedleader"),
                         "lead": lead, "last_activity": sess.get("last_activity"),
                         "is_live": (now - (sess.get("last_activity") or 0)) < LIVE_THRESHOLD})
    for row in persistence.list_persisted_sessions():
        if row["session_id"] in seen:
            continue
        if row.get("lead"):
            rows.append({"session_id": row["session_id"], "tenant": row.get("tenant", "aedleader"),
                         "lead": row["lead"], "last_activity": row.get("last_activity"),
                         "is_live": False})
    if site:
        rows = [r for r in rows if r["tenant"] == site]
    for r in rows:  # A reachable lead requires email or phone; a name alone is not contactable.
        r["contactable"] = contactable(r["lead"])
    rows.sort(key=lambda r: r.get("last_activity") or 0, reverse=True)
    return {"count": len(rows), "leads": rows}


@app.get("/api/admin/sessions/{session_id}", dependencies=[Depends(require_admin)])
async def api_admin_session_detail(session_id: str):
    """Full session view — conversation history + per-turn debug entries.

    Live in-memory wins; falls back to disk for ended/evicted sessions.
    """
    if not is_safe_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format.")
    if session_id in sessions:
        s = sessions[session_id]
        return {
            "session_id": session_id,
            "created_at": s.get("created_at"),
            "last_activity": s.get("last_activity"),
            "history": s.get("history", []),
            "debug_log": s.get("debug_log", []),
            "last_products": s.get("last_products", []),
            "is_live": True,
        }
    disk = await run_in_threadpool(persistence.get_persisted_session, session_id)
    if disk is None:
        raise HTTPException(status_code=404, detail="Session not found")
    # token_hash is a resume credential derivative — the admin UI has no use
    # for it and it must not ride along into browsers/exports.
    disk.pop("token_hash", None)
    return disk


@app.delete("/api/admin/sessions/{session_id}", dependencies=[Depends(require_admin)])
async def api_admin_session_delete(session_id: str):
    """Wipe a session from both memory and disk."""
    if not is_safe_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session_id format.")
    in_mem = session_id in sessions
    if in_mem:
        del sessions[session_id]
        _session_locks.pop(session_id, None)
    on_disk = persistence.delete_persisted_session(session_id)
    return {"status": "deleted", "removed_from_memory": in_mem, "removed_from_disk": on_disk}


@app.get("/api/admin/stats", dependencies=[Depends(require_admin)])
async def api_admin_stats(site: str | None = None):
    _validate_site(site)
    """Aggregate stats across live + historical sessions.

    Walks the JSON files on disk (cheap at our scale — capped at
    MAX_SESSION_FILES). If this ever gets slow, the answer is to add a
    SQLite sidecar, NOT to cache here.
    """
    now = time.time()
    day_ago = now - 86400
    week_ago = now - 7 * 86400

    sessions_24h = 0
    sessions_7d = 0
    total_turns = 0
    total_hallucinations = 0
    intent_counts: dict[str, int] = defaultdict(int)
    journey_counts: dict[str, int] = defaultdict(int)
    llm_times: list[float] = []
    # Compute end-to-end reply latency over the last seven days.


    reply_times_7d: list[float] = []
    total_sessions = 0

    def _count_into(counter: dict, value):
        """Increment counters for scalar strings or historical list-valued fields."""
        if value is None:
            return
        if isinstance(value, list):
            for v in value:
                if isinstance(v, str) and v:
                    counter[v] += 1
            return
        if isinstance(value, str) and value:
            counter[value] += 1

    def _walk(s: dict):
        nonlocal sessions_24h, sessions_7d, total_turns, total_hallucinations
        last_activity = s.get("last_activity") or 0
        if last_activity > day_ago:
            sessions_24h += 1
        if last_activity > week_ago:
            sessions_7d += 1
        for entry in s.get("debug_log", []):
            total_turns += 1
            g = entry.get("guardrails") or {}
            try:
                total_hallucinations += int(g.get("hallucinated") or 0)
            except (TypeError, ValueError):
                pass
            analysis = entry.get("analysis") or {}
            _count_into(intent_counts, analysis.get("intent"))
            _count_into(journey_counts, analysis.get("journey_stage"))
            timing = entry.get("timing") or {}
            t = timing.get("llm_response")
            if isinstance(t, (int, float)):
                llm_times.append(float(t))
            # product turns record timing.total; every other path time_seconds
            total = timing.get("total", entry.get("time_seconds"))
            try:
                recent = datetime.fromisoformat(entry["timestamp"]).timestamp() > week_ago
            except (KeyError, TypeError, ValueError):
                recent = False
            if recent and isinstance(total, (int, float)):
                reply_times_7d.append(float(total))

    seen: set[str] = set()
    live_count = 0
    # Live dict is walked ON the event loop (it mutates between awaits).
    for sid, s in sessions.items():
        seen.add(sid)
        if site and s.get("tenant", "aedleader") != site:
            continue
        live_count += 1
        total_sessions += 1
        _walk(s)

    # Read historical sessions in the threadpool to keep disk scans off the event loop.


    def _walk_disk():
        nonlocal total_sessions
        for row in persistence.list_persisted_sessions():
            if row["session_id"] in seen:
                continue
            if site and row.get("tenant", "aedleader") != site:
                continue
            full = persistence.get_persisted_session(row["session_id"])
            if full is None:
                continue
            total_sessions += 1
            _walk(full)

    await run_in_threadpool(_walk_disk)

    avg_llm_time = round(sum(llm_times) / len(llm_times), 3) if llm_times else 0.0
    hallucination_rate = round(total_hallucinations / total_turns, 4) if total_turns else 0.0

    def _pct(xs: list[float], p: float) -> float:
        xs = sorted(xs)
        k = (len(xs) - 1) * p
        lo = int(k)
        hi = min(lo + 1, len(xs) - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)

    reply_7d = {"n": len(reply_times_7d), "mean": 0.0, "p50": 0.0, "p90": 0.0}
    if reply_times_7d:
        reply_7d.update(mean=round(sum(reply_times_7d) / len(reply_times_7d), 2),
                        p50=round(_pct(reply_times_7d, 0.5), 2),
                        p90=round(_pct(reply_times_7d, 0.9), 2))

    return {
        "total_sessions": total_sessions,
        "live_sessions": live_count if site else len(sessions),
        "sessions_24h": sessions_24h,
        "sessions_7d": sessions_7d,
        "total_turns": total_turns,
        "total_hallucinations": total_hallucinations,
        "hallucination_rate": hallucination_rate,
        "avg_llm_response_seconds": avg_llm_time,
        "reply_seconds_7d": reply_7d,
        "intent_breakdown": dict(intent_counts),
        "journey_breakdown": dict(journey_counts),
        "site": site or "all",
        "sync_status": get_sync_status(site),
    }


@app.get("/api/admin/engagement", dependencies=[Depends(require_admin)])
async def api_admin_engagement(window: str = "7d", site: str | None = None):
    _validate_site(site)
    """Chip-click + info-card-CTA aggregation for the Engagement tab.

    window: "24h" | "7d" | "30d" | "all" — filters by event timestamp.
    Defaults to 7d to match the dashboard mental model.
    """
    now = time.time()
    if window == "24h":
        since = now - 86400
    elif window == "30d":
        since = now - 30 * 86400
    elif window == "all":
        since = None
    else:  # default + "7d"
        since = now - 7 * 86400
        window = "7d"
    agg = await run_in_threadpool(persistence.aggregate_events, since_ts=since, tenant=site)
    agg["window"] = window
    agg["site"] = site or "all"
    return agg


@app.get("/api/admin/conversions", dependencies=[Depends(require_admin)])
async def api_admin_conversions(window: str = "7d", site: str | None = None):
    _validate_site(site)
    """"Sales attributed to chat" for the dashboard.

    window: "24h" | "7d" | "30d" | "all" — filters by order creation time.
    Counts only PAID orders; reports matched-to-session vs channel-only so the
    number's attribution quality is visible.
    """
    now = time.time()
    if window == "24h":
        since = now - 86400
    elif window == "30d":
        since = now - 30 * 86400
    elif window == "all":
        since = None
    else:  # default + "7d"
        since = now - 7 * 86400
        window = "7d"
    if site and site != "aedleader":
        # Return an empty summary for stores without order polling; preserve the API response shape.


        faraway = now + 10**9
        summary = await run_in_threadpool(conversions.get_conversions_summary, since_ts=faraway)
        summary["funnel"] = await run_in_threadpool(
            conversions.get_click_to_conversion_funnel, since_ts=faraway)
        summary["window"] = window
        summary["poll_enabled"] = False
        summary["site"] = site
        summary["note"] = "Conversion tracking is not yet enabled for this site."
        return summary
    summary = await run_in_threadpool(conversions.get_conversions_summary, since_ts=since)
    summary["funnel"] = await run_in_threadpool(
        conversions.get_click_to_conversion_funnel, since_ts=since)
    summary["window"] = window
    summary["poll_enabled"] = conversions.POLL_ENABLED
    summary["site"] = site or "aedleader"
    return summary


# Admin static pages
# Routes that serve the dashboard HTML. The files live in backend/admin_pages/
# (NOT under frontend/) so they can't be reached via the /static mount that
# serves the chatbot widget — that would bypass the auth check on /admin.

ADMIN_DIR = os.path.join(os.path.dirname(__file__), "admin_pages")


@app.get("/admin/login")
async def admin_login_page():
    fp = os.path.join(ADMIN_DIR, "admin_login.html")
    if not os.path.exists(fp):
        raise HTTPException(status_code=404, detail="Admin login page not built yet")
    return FileResponse(fp)


@app.get("/admin")
async def admin_page(request: Request):
    """Serve the admin panel. Redirect to login if no valid cookie."""
    token = request.cookies.get("aed_admin_token")
    if not token or not admin_validate_token(token):
        return RedirectResponse(url="/admin/login")
    fp = os.path.join(ADMIN_DIR, "admin.html")
    if not os.path.exists(fp):
        raise HTTPException(status_code=404, detail="Admin page not built yet")
    return FileResponse(fp)


# Sync endpoints

def _validate_site(site: str | None) -> str | None:
    if site is not None and site not in TENANTS:
        raise HTTPException(status_code=400, detail=f"Unknown site. Valid: {', '.join(TENANTS)}")
    return site


@app.get("/sync/status", dependencies=[Depends(require_admin)])
async def sync_status(site: str | None = None):
    _validate_site(site)
    """Current sync engine status — last sync times, recent results.

    Admin-only — leaks operational telemetry (timing, recent run history)
    that an attacker could use to time their attacks against quiet windows.
    """
    return get_sync_status(site)


@app.post("/sync/trigger/incremental", dependencies=[Depends(require_admin)])
async def trigger_incremental_sync(site: str | None = None):
    _validate_site(site)
    """Manually trigger an incremental sync (modified products only)."""
    result = run_incremental_sync(site)
    return result


@app.post("/sync/trigger/full", dependencies=[Depends(require_admin)])
async def trigger_full_sync(site: str | None = None):
    _validate_site(site)
    """Manually trigger a full catalog sync (all products + deletion check)."""
    result = run_full_sync(site)
    return result


@app.post("/sync/trigger/delete-check", dependencies=[Depends(require_admin)])
async def trigger_deletion_check(site: str | None = None):
    _validate_site(site)
    """Manually trigger a deletion check (lightweight, IDs only)."""
    result = run_deletion_check(site)
    return result


@app.post("/sync/trigger/orders", dependencies=[Depends(require_admin)])
async def trigger_orders_poll():
    """Manually trigger the conversion orders poll (WooCommerce orders →
    conversion store). Runs regardless of the CONVERSIONS_POLL_ENABLED flag so
    an admin can test it on demand; no-ops with a reason if WC creds are unset.
    """
    result = conversions.run_orders_poll()
    return result


@app.get("/sync/audit", dependencies=[Depends(require_admin)])
async def get_audit_log(date: str | None = None):
    """Return product-change audit entries for a date, defaulting to today."""
    from sync import _load_audit_log, AUDIT_DIR
    import os as _os

    if date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
        path = _os.path.join(AUDIT_DIR, f"{date}.json")

        def _read_audit_file():
            if not _os.path.exists(path):
                return []
            import json as _json
            with open(path, "r", encoding="utf-8") as f:
                return _json.load(f)

        entries = await run_in_threadpool(_read_audit_file)
    else:
        entries = await run_in_threadpool(_load_audit_log)

    return {
        "date": date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_entries": len(entries),
        "entries": entries,
    }


@app.get("/sync/audit/dates", dependencies=[Depends(require_admin)])
async def list_audit_dates():
    """List all available audit log dates."""
    from sync import AUDIT_DIR
    import os as _os

    if not _os.path.exists(AUDIT_DIR):
        return {"dates": []}

    dates = sorted([f.replace(".json", "") for f in _os.listdir(AUDIT_DIR) if f.endswith(".json")], reverse=True)
    return {"dates": dates}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8003, reload=True)
