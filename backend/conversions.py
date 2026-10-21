"""Attribute WooCommerce orders to chatbot conversations.

Product links carry a disposable attribution ID in utm_content. Click events
map that ID to a conversation; WooCommerce order metadata completes the join.
Polling is opt-in and currently uses the AED Leader store credentials.
Only paid orders count toward reported sales. Order IDs provide idempotency,
and status updates account for cancellations and refunds. Billing and shipping
customer fields are excluded from fetched order data."""

import os
import json
import logging
import time
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

import persistence
from persistence import is_safe_session_id

load_dotenv()

# Config (same env vars as sync.py / fetch_products.py)
WOOCOMMERCE_URL = os.getenv("WOOCOMMERCE_URL")
WC_KEY = os.getenv("WC_CONSUMER_KEY")
WC_SECRET = os.getenv("WC_CONSUMER_SECRET")

# Off by default — see module docstring. Set CONVERSIONS_POLL_ENABLED=true once
# Order Attribution is on and the API key can read orders.
POLL_ENABLED = os.getenv("CONVERSIONS_POLL_ENABLED", "false").strip().lower() in ("1", "true", "yes")
# Polling interval in minutes; first-run lookback controls backfill.
ORDERS_POLL_INTERVAL = int(os.getenv("ORDERS_POLL_INTERVAL", "1440"))

FIRST_RUN_LOOKBACK_DAYS = int(os.getenv("CONVERSIONS_FIRST_RUN_LOOKBACK_DAYS", "30"))
# Re-fetch a small overlap each run so a boundary order can't slip between polls
# (dedupe on order_id makes the overlap free).
POLL_OVERLAP_MINUTES = 10

# WooCommerce order-attribution meta keys (written by native Order Attribution).
ATTR_SOURCE_META = "_wc_order_attribution_utm_source"
ATTR_CONTENT_META = "_wc_order_attribution_utm_content"
# The widget's attribution source value.
CHATBOT_SOURCE = "chatbot"

# Only these count as a completed sale. "pending"/"on-hold" are unpaid;
# "refunded"/"cancelled"/"failed" are excluded, so a refunded order — caught on
# its status transition by modified_after — drops out of the reported total.
PAID_STATUSES = frozenset({"processing", "completed"})

CONVERSIONS_FILE = os.path.join(os.path.dirname(__file__), "data", "conversions.json")

log = logging.getLogger("conversions")


#   PURE LOGIC (no network / no disk — unit-testable)


def _extract_meta(order: dict, key: str):
    """Read a single meta_data value by key from a WooCommerce order dict."""
    for m in order.get("meta_data") or []:
        if isinstance(m, dict) and m.get("key") == key:
            return m.get("value")
    return None


CID_PREFIX = "cid_"


def build_cid_map() -> dict:
    """Map disposable attribution IDs to session IDs from retained link-click
    events. Session credentials are never included in outbound product URLs."""
    mapping: dict = {}
    for ev in persistence.read_events():
        if ev.get("event_name") != "link_click":
            continue
        cid = (ev.get("metadata") or {}).get("cid")
        sid = ev.get("session_id")
        if isinstance(cid, str) and cid.startswith(CID_PREFIX) and sid:
            mapping[cid] = sid
    return mapping


def order_to_conversion(order: dict, cid_map: dict | None = None) -> dict | None:
    """Map chatbot-attributed order metadata to a pseudonymous conversion record.
    Resolve disposable attribution IDs through click events and support legacy
    session IDs. Unresolved attribution remains a channel-level conversion."""
    source = _extract_meta(order, ATTR_SOURCE_META)
    if not source or str(source).strip().lower() != CHATBOT_SOURCE:
        return None

    raw_sid = _extract_meta(order, ATTR_CONTENT_META)
    sid = None
    if isinstance(raw_sid, str):
        if raw_sid.startswith(CID_PREFIX):
            sid = (cid_map or {}).get(raw_sid)
        elif is_safe_session_id(raw_sid):
            sid = raw_sid  # legacy: pre-cid orders carried the session id

    status = str(order.get("status") or "")
    try:
        total = float(order.get("total") or 0)
    except (TypeError, ValueError):
        total = 0.0

    return {
        "order_id": order.get("id"),
        "status": status,
        "paid": status in PAID_STATUSES,
        "total": total,
        "currency": order.get("currency") or "",
        "attribution_source": CHATBOT_SOURCE,
        "session_id": sid,
        "matched": sid is not None,
        "date_created_gmt": order.get("date_created_gmt"),
        "date_modified_gmt": order.get("date_modified_gmt"),
    }


def _gmt_to_epoch(value) -> float | None:
    """Parse a WooCommerce *_gmt datetime (no tz suffix) as UTC → epoch."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


#   STORE (data/conversions.json — atomic, idempotent)


def _load_store() -> dict:
    if os.path.exists(CONVERSIONS_FILE):
        try:
            with open(CONVERSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("last_polled", None)
                data.setdefault("conversions", {})
                return data
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"conversions store unreadable, starting fresh: {e}")
    return {"last_polled": None, "conversions": {}}


def _save_store(store: dict) -> None:
    """Atomic write (tmp + os.replace) — a crash mid-write can't corrupt."""
    os.makedirs(os.path.dirname(CONVERSIONS_FILE), exist_ok=True)
    tmp = CONVERSIONS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, CONVERSIONS_FILE)
    except OSError as e:
        log.error(f"failed to save conversions store: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def record_orders(orders: list[dict], now_iso: str | None = None,
                  set_cursor: str | None = None) -> dict:
    """Fold a batch of WooCommerce orders into the conversion store.

    Idempotent: keyed by order_id, so re-processing (overlap windows, manual
    re-runs) updates in place and never double-counts. Best-effort write-back
    stamps the matched session's on-disk file with its outcome (for the admin
    session view) — the store here remains the source of truth if that file was
    already purged.

    set_cursor: if given, persist it as last_polled. Kept separate so tests can
    fold orders without touching the cursor.
    """
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    store = _load_store()
    stats = {"scanned": len(orders), "chatbot": 0, "new": 0, "updated": 0,
             "matched": 0, "unmatched": 0, "session_writebacks": 0}
    cid_map = build_cid_map()  # once per batch — resolves utm_content cids

    for order in orders:
        conv = order_to_conversion(order, cid_map)
        if conv is None:
            continue
        stats["chatbot"] += 1
        oid = str(conv.get("order_id"))
        conv["recorded_at"] = now_iso
        if oid in store["conversions"]:
            stats["updated"] += 1
        else:
            stats["new"] += 1
        store["conversions"][oid] = conv

        if conv["matched"] and conv["session_id"]:
            stats["matched"] += 1
            if conv["paid"]:
                wrote = persistence.mark_session_converted(
                    conv["session_id"], conv["order_id"], conv["total"],
                    currency=conv["currency"],
                )
                if wrote:
                    stats["session_writebacks"] += 1
        else:
            stats["unmatched"] += 1

    if set_cursor is not None:
        store["last_polled"] = set_cursor
    _save_store(store)
    return stats


#   WOOCOMMERCE ORDERS API


def fetch_orders_modified_since(since_iso: str) -> list[dict]:
    """Fetch orders modified after `since_iso` (UTC ISO), handling pagination.

    dates_are_gmt=true makes modified_after interpreted as UTC (matches our
    cursor). _fields keeps the payload minimal — no customer PII.
    """
    all_orders: list[dict] = []
    page = 1
    while True:
        try:
            resp = requests.get(
                f"{WOOCOMMERCE_URL}/wp-json/wc/v3/orders",
                auth=(WC_KEY, WC_SECRET),
                params={
                    "per_page": 100,
                    "page": page,
                    "modified_after": since_iso,
                    "dates_are_gmt": "true",
                    "orderby": "modified",
                    "order": "asc",
                    "_fields": "id,status,total,currency,date_created_gmt,date_modified_gmt,meta_data",
                },
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error(f"WooCommerce orders API error (page {page}): {e}")
            break

        orders = resp.json()
        if not orders:
            break
        all_orders.extend(orders)

        total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break
        page += 1

    return all_orders


#   RUNNER (called by the scheduler)


def run_orders_poll() -> dict:
    """Poll WooCommerce for recently-modified orders and fold chatbot-attributed
    ones into the conversion store. Safe to call anytime — no-ops with a clear
    reason if disabled or misconfigured."""
    start = time.time()
    if not WOOCOMMERCE_URL or not WC_KEY or not WC_SECRET:
        log.info("orders poll skipped — WooCommerce credentials not configured")
        return {"skipped": "no_credentials"}

    store = _load_store()
    last = store.get("last_polled")
    if last:
        try:
            since_dt = datetime.fromisoformat(last) - timedelta(minutes=POLL_OVERLAP_MINUTES)
        except ValueError:
            since_dt = datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
    else:
        since_dt = datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)

    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    since_iso = since_dt.astimezone(timezone.utc).isoformat()

    now_iso = datetime.now(timezone.utc).isoformat()
    log.info(f"orders poll: fetching orders modified since {since_iso}")
    orders = fetch_orders_modified_since(since_iso)

    stats = record_orders(orders, now_iso=now_iso, set_cursor=now_iso)
    duration = round(time.time() - start, 2)
    result = {
        "timestamp": now_iso,
        "since": since_iso,
        "duration_seconds": duration,
        **stats,
    }
    log.info(
        f"orders poll done in {duration}s: scanned {stats['scanned']}, "
        f"chatbot {stats['chatbot']} ({stats['new']} new, {stats['updated']} updated), "
        f"matched {stats['matched']}, unmatched {stats['unmatched']}"
    )
    return result


#   REPORTING


def get_conversions_summary(since_ts: float | None = None) -> dict:
    """Headline "sales attributed to chat" + attribution-quality breakdown.

    Counts only PAID orders (created after since_ts, if given). Reports matched
    (tied to a specific conversation) vs channel_only (chatbot sale we couldn't
    tie to a session — native attribution dropped utm_content) so the number's
    trustworthiness is visible.
    """
    store = _load_store()
    convs = list(store.get("conversions", {}).values())

    count = 0
    total_value = 0.0
    matched = 0
    channel_only = 0
    currencies: set[str] = set()

    for c in convs:
        if not c.get("paid"):
            continue
        if since_ts is not None:
            created = _gmt_to_epoch(c.get("date_created_gmt"))
            if created is not None and created < since_ts:
                continue
        count += 1
        total_value += float(c.get("total") or 0)
        if c.get("matched"):
            matched += 1
        else:
            channel_only += 1
        if c.get("currency"):
            currencies.add(c["currency"])

    return {
        "attributed_sales": count,
        "attributed_value": round(total_value, 2),
        "matched_to_session": matched,
        "channel_only": channel_only,
        "currencies": sorted(currencies),
        "last_polled": store.get("last_polled"),
        "since_ts": since_ts,
    }


def get_click_to_conversion_funnel(since_ts: float | None = None) -> dict:
    """Join retained product-click events with matched conversions.
    Older conversions may outlive their click events, so long-window joins can
    undercount the number of converted clicking sessions."""
    events = persistence.read_events(since_ts=since_ts)
    click_events = 0
    click_sessions: set[str] = set()
    for ev in events:
        if ev.get("event_name") == "link_click":
            click_events += 1
            sid = ev.get("session_id")
            if sid:
                click_sessions.add(sid)

    store = _load_store()
    converted_sessions: set[str] = set()
    for c in store.get("conversions", {}).values():
        if not (c.get("paid") and c.get("matched") and c.get("session_id")):
            continue
        if since_ts is not None:
            created = _gmt_to_epoch(c.get("date_created_gmt"))
            if created is not None and created < since_ts:
                continue
        converted_sessions.add(c["session_id"])

    clicked_and_converted = click_sessions & converted_sessions
    rate = round(len(clicked_and_converted) / len(click_sessions), 4) if click_sessions else 0.0
    return {
        "link_click_events": click_events,
        "click_sessions": len(click_sessions),
        "converted_sessions": len(converted_sessions),
        "clicked_and_converted": len(clicked_and_converted),
        "click_to_sale_rate": rate,
    }


#   CLI — manual run


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
    print("Running orders poll...")
    print(json.dumps(run_orders_poll(), indent=2))
    print("\nSummary:")
    print(json.dumps(get_conversions_summary(), indent=2))
