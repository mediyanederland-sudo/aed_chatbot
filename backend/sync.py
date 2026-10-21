"""Synchronize tenant WooCommerce catalogs with Qdrant.

Incremental polling updates modified products; full sync also detects deletions.
Separate content and payload hashes avoid embedding unchanged product text."""
import os
import json
import hashlib
import logging
import time
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.models import PointIdsList

from index_to_qdrant import (
    strip_html,
    extract_brand,
    extract_top_category,
    build_embedding_text,
    build_payload,
    embed_dense,
    embed_sparse,
    category_map_for,
    EXCLUDED_TYPES_SISTERS,
    COLLECTION_NAME,
    BATCH_SIZE,
)

load_dotenv()

# Config
QDRANT_URL = os.getenv("QDRANT_URL")
if not QDRANT_URL:
    raise RuntimeError("QDRANT_URL environment variable is required")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
WOOCOMMERCE_URL = os.getenv("WOOCOMMERCE_URL")
WC_KEY = os.getenv("WC_CONSUMER_KEY")
WC_SECRET = os.getenv("WC_CONSUMER_SECRET")

# Sync intervals (in minutes)
INCREMENTAL_INTERVAL = 15  # Quick poll every 15 min
FULL_SYNC_INTERVAL = 1440  # Full catalog comparison once per day (24h)

qdrant_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)

log = logging.getLogger("sync")
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")

# Audit Log
AUDIT_DIR = os.path.join(os.path.dirname(__file__), "data", "sync_audit")
SYNC_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "data", "sync_history.json")


def _audit_dir(ctx=None) -> str:
    """Per-tenant audit dir. aedleader keeps the legacy flat AUDIT_DIR."""
    if ctx is None or ctx.slug == "aedleader":
        return AUDIT_DIR
    return os.path.join(AUDIT_DIR, ctx.slug)


def _ensure_audit_dir(ctx=None):
    """Create audit directory if it doesn't exist."""
    os.makedirs(_audit_dir(ctx), exist_ok=True)


def _audit_log_path(ctx=None) -> str:
    """Return the UTC daily audit-file path for the selected tenant."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(_audit_dir(ctx), f"{today}.json")


def _load_audit_log(ctx=None) -> list[dict]:
    """Load today's audit entries."""
    path = _audit_log_path(ctx)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_audit_entry(entry: dict, ctx=None):
    """Append an audit entry to today's log file."""
    _ensure_audit_dir(ctx)
    entries = _load_audit_log(ctx)
    entries.append(entry)
    with open(_audit_log_path(ctx), "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def audit_update(product_id: int, product_name: str, action: str, reason: str,
                 old_values: dict | None = None, new_values: dict | None = None, ctx=None):
    """Log a product update with old vs new values."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,  # "re_embed", "update_payload", "new", "deleted"
        "product_id": product_id,
        "product_name": product_name,
        "reason": reason,
        "old": old_values,
        "new": new_values,
    }
    _save_audit_entry(entry, ctx)
    log.info(f"  AUDIT [{action}] ID:{product_id} '{product_name}' — {reason}")


def audit_deletion(product_id: int, old_payload: dict, ctx=None):
    """Log a product deletion with full old payload preserved."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": "deleted",
        "product_id": product_id,
        "product_name": old_payload.get("name", "UNKNOWN"),
        "reason": "product removed from WooCommerce or unpublished",
        "preserved_payload": {
            "name": old_payload.get("name"),
            "brand": old_payload.get("brand"),
            "price": old_payload.get("price"),
            "regular_price": old_payload.get("regular_price"),
            "sale_price": old_payload.get("sale_price"),
            "stock_status": old_payload.get("stock_status"),
            "on_sale": old_payload.get("on_sale"),
            "top_category": old_payload.get("top_category"),
            "permalink": old_payload.get("permalink"),
            "sku": old_payload.get("sku"),
            "image_url": old_payload.get("image_url"),
        },
    }
    _save_audit_entry(entry, ctx)
    log.info(f"  AUDIT [deleted] ID:{product_id} '{old_payload.get('name', 'UNKNOWN')}' — preserved payload saved")


def _extract_tracked_fields(payload: dict) -> dict:
    """Extract the fields we track for change comparison."""
    return {
        "price": payload.get("price"),
        "regular_price": payload.get("regular_price"),
        "sale_price": payload.get("sale_price"),
        "on_sale": payload.get("on_sale"),
        "stock_status": payload.get("stock_status"),
        "image_url": payload.get("image_url"),
        "name": payload.get("name"),
        "brand": payload.get("brand"),
        "top_category": payload.get("top_category"),
    }


def _diff_fields(old: dict, new: dict) -> dict:
    """Return only the fields that changed, with old and new values."""
    changes = {}
    all_keys = set(old.keys()) | set(new.keys())
    for key in all_keys:
        old_val = old.get(key)
        new_val = new.get(key)
        if old_val != new_val:
            changes[key] = {"old": old_val, "new": new_val}
    return changes


def load_sync_history(history_file: str = SYNC_HISTORY_FILE) -> list[dict]:
    """Load persisted sync history from disk."""
    if os.path.exists(history_file):
        with open(history_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_sync_history(history: list[dict], history_file: str = SYNC_HISTORY_FILE):
    """Persist sync history to disk."""
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


#   HASH FUNCTIONS


def compute_embedding_hash(product, top_category):
    """Hash normalized text and taxonomy fields used to decide whether to re-embed."""
    brand = extract_brand(product)
    short_desc = strip_html(product.get("short_description", ""))
    full_desc = strip_html(product.get("description", ""))
    if len(full_desc) > 500:
        full_desc = full_desc[:500]

    fields = {
        "name": product.get("name", ""),
        "brand": brand,
        "top_category": top_category,
        "short_description": short_desc,
        "description": full_desc,
        "categories": sorted([c["name"] for c in product.get("categories", [])]),
        "tags": sorted([t["name"] for t in product.get("tags", [])]),
        "sku": product.get("sku", ""),
        "attributes": sorted([
            f"{a['name']}:{','.join(sorted(a.get('options', [])))}"
            for a in product.get("attributes", [])
        ]),
    }
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def compute_payload_hash(product):
    """Hash normalized dynamic fields to detect payload-only updates."""
    sale_price = product.get("sale_price", "")
    if not sale_price or sale_price == "":
        sale_price = None

    fields = {
        "price": str(product.get("price", "") or ""),
        "regular_price": str(product.get("regular_price", "") or ""),
        "sale_price": str(sale_price) if sale_price else "",
        "on_sale": product.get("on_sale", False),
        "stock_status": product.get("stock_status", ""),
        "image_url": (product.get("images", [{}])[0].get("src", "")
                      if product.get("images") else ""),
    }
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


#   WOOCOMMERCE API


def _wc_products_get(params: dict, ctx=None):
    """Fetch WooCommerce products with bounded retries and exponential backoff.
    Honor Retry-After on throttled responses."""
    url = f"{(ctx.wc_url if ctx else WOOCOMMERCE_URL)}/wp-json/wc/v3/products"
    auth = ctx.wc_auth if ctx else (WC_KEY, WC_SECRET)
    delay = 2.0
    last_exc = None
    for attempt in range(4):
        try:
            response = requests.get(url, auth=auth, params=params, timeout=30,
                                    headers={"User-Agent": "Mozilla/5.0 (compatible; MediyaChatbot/1.0; +https://mediya.nl)"})
            if response.status_code in (429, 503):
                retry_after = response.headers.get("Retry-After")
                wait = min(float(retry_after) if retry_after else delay, 60.0)
                log.warning(f"WC {response.status_code}, backing off {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
                delay = min(delay * 2, 60.0)
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            last_exc = e
            if attempt == 3:
                break
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
    raise last_exc if last_exc else RuntimeError("WC fetch failed")


def fetch_modified_products(since: datetime, ctx=None) -> list[dict]:
    """Fetch products modified after a given timestamp from WooCommerce API."""
    all_products = []
    page = 1

    while True:
        try:
            response = _wc_products_get(
                {
                    "per_page": 100,
                    "page": page,
                    "modified_after": since.isoformat(),
                    "status": "publish",
                },
                ctx,
            )
        except requests.RequestException as e:
            log.error(f"WooCommerce API error (page {page}): {e}")
            break

        products = response.json()
        if not products:
            break

        all_products.extend(products)
        total_pages = int(response.headers.get("X-WP-TotalPages", 1))

        if page >= total_pages:
            break
        page += 1

    return all_products


def fetch_all_product_ids(ctx=None) -> set[int]:
    """Fetch all published product IDs from WooCommerce (lightweight, for deletion detection)."""
    all_ids = set()
    page = 1

    while True:
        try:
            response = _wc_products_get(
                {
                    "per_page": 100,
                    "page": page,
                    "status": "publish",
                    "_fields": "id,type",
                },
                ctx,
            )
        except requests.RequestException as e:
            log.error(f"WooCommerce API error fetching IDs (page {page}): {e}")
            break

        products = response.json()
        if not products:
            break

        for p in products:
            # Excluded types are "not in catalog" for this tenant — leaving them
            # out of the ID set means any that slipped into Qdrant get cleaned.
            if ctx and p.get("type") in ctx.exclude_types:
                continue
            all_ids.add(p["id"])

        total_pages = int(response.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break
        page += 1

    return all_ids


#   QDRANT STATE


def get_qdrant_state(ctx=None) -> dict[int, dict]:
    """Scroll all points from Qdrant — returns {product_id: payload}."""
    existing = {}
    offset = None

    while True:
        points, offset = qdrant_client.scroll(
            collection_name=ctx.collection if ctx else COLLECTION_NAME,
            limit=100,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        for p in points:
            existing[p.id] = p.payload
        if offset is None:
            break

    return existing


#   SYNC LOGIC


def classify_product(product, qdrant_payload, top_category):
    """Determine what action is needed for a single product.

    Returns: (action, reason)
        action: "re_embed" | "update_payload" | "skip"
        reason: human-readable explanation
    """
    new_emb_hash = compute_embedding_hash(product, top_category)
    new_pay_hash = compute_payload_hash(product)

    stored_emb_hash = qdrant_payload.get("embedding_hash", "")
    stored_pay_hash = qdrant_payload.get("payload_hash", "")

    # Migration: if point doesn't have new hashes yet, always update
    if not stored_emb_hash or not stored_pay_hash:
        # Check if text actually changed using old content_hash
        old_content_hash = qdrant_payload.get("content_hash", "")
        # Compute content_hash the OLD way (raw HTML, no truncation) for comparison
        content_for_hash = f"{product.get('name', '')}{product.get('description', '')}{product.get('short_description', '')}"
        new_old_style_hash = hashlib.md5(content_for_hash.encode()).hexdigest()

        if old_content_hash and old_content_hash != new_old_style_hash:
            return "re_embed", "text changed (migration + content_hash mismatch)"

        # Text same but need to store new hashes — do payload update
        return "update_payload", "migration: storing embedding_hash + payload_hash"

    # Normal two-hash comparison
    if stored_emb_hash != new_emb_hash:
        return "re_embed", "embedding_hash changed (text fields modified)"

    if stored_pay_hash != new_pay_hash:
        return "update_payload", "payload_hash changed (price/stock/image modified)"

    return "skip", "no changes"


def sync_product_payload(product_id, product, top_category, ctx=None):
    """Update only dynamic payload fields via set_payload (no re-embedding)."""
    payload = build_payload(product, top_category)
    new_emb_hash = compute_embedding_hash(product, top_category)
    new_pay_hash = compute_payload_hash(product)

    # Normalize empty sale prices.
    sale_price = payload.get("sale_price")
    if sale_price is None or sale_price == "":
        sale_price = None

    qdrant_client.set_payload(
        collection_name=ctx.collection if ctx else COLLECTION_NAME,
        payload={
            "price": payload["price"],
            "regular_price": payload["regular_price"],
            "sale_price": sale_price,
            "on_sale": payload["on_sale"],
            "stock_status": payload["stock_status"],
            "image_url": payload["image_url"],
            "thumbnail_url": payload["thumbnail_url"],
            "permalink": payload["permalink"],
            "related_ids": payload["related_ids"],
            "upsell_ids": payload["upsell_ids"],
            "cross_sell_ids": payload["cross_sell_ids"],
            "featured": payload["featured"],
            # Store new hashes
            "embedding_hash": new_emb_hash,
            "payload_hash": new_pay_hash,
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
        },
        points=[product_id],
    )


def sync_product_full(product, top_category, ctx=None):
    """Full re-index: generate new embeddings + upsert point."""
    from qdrant_client.models import PointStruct

    embedding_text = build_embedding_text(product, top_category)
    payload = build_payload(product, top_category)

    # Normalize sale_price
    if payload.get("sale_price") is None or payload.get("sale_price") == "":
        payload["sale_price"] = None

    # Add new hashes
    payload["embedding_hash"] = compute_embedding_hash(product, top_category)
    payload["payload_hash"] = compute_payload_hash(product)

    # Generate embeddings
    dense_vecs = embed_dense([embedding_text])
    sparse_vecs = embed_sparse([embedding_text])

    point = PointStruct(
        id=product["id"],
        vector={
            "dense": dense_vecs[0],
            "sparse": sparse_vecs[0],
        },
        payload=payload,
    )

    qdrant_client.upsert(
        collection_name=ctx.collection if ctx else COLLECTION_NAME,
        points=[point],
    )


def delete_stale_products(stale_ids: set[int], ctx=None):
    """Remove products from Qdrant that no longer exist in WooCommerce."""
    if not stale_ids:
        return

    qdrant_client.delete(
        collection_name=ctx.collection if ctx else COLLECTION_NAME,
        points_selector=PointIdsList(points=list(stale_ids)),
    )
    log.info(f"Deleted {len(stale_ids)} stale products: {stale_ids}")


#   SYNC RUNNERS


class SyncState:
    """Tracks sync state across runs. Persists to disk (per-tenant history file)."""

    def __init__(self, history_file: str = SYNC_HISTORY_FILE):
        self.history_file = history_file
        self.last_incremental_sync: datetime | None = None
        self.last_full_sync: datetime | None = None
        self.sync_history: list[dict] = load_sync_history(history_file)

    def add_result(self, result: dict):
        self.sync_history.append(result)
        # Keep last 100 results
        if len(self.sync_history) > 100:
            self.sync_history = self.sync_history[-100:]
        save_sync_history(self.sync_history, self.history_file)


class TenantSyncContext:
    """Everything a sync run needs for one tenant: creds, collection, paths,
    hygiene flags, per-tenant category map, and persisted state."""

    def __init__(self, tenant):
        base = os.path.dirname(__file__)
        self.tenant = tenant
        self.slug = tenant.slug
        self.collection = tenant.collection
        self.wc_url = tenant.store_url.rstrip("/")
        self.wc_auth = (tenant.wc_key, tenant.wc_secret)
        suffix = "" if tenant.slug == "aedleader" else f"_{tenant.slug}"
        self.history_file = os.path.join(base, "data", f"sync_history{suffix}.json")
        self.categories_file = os.path.join(base, "data", f"categories{suffix}.json")
        # Retain AED Leader rentals; exclude sister-store subscriptions.

        self.exclude_types = () if tenant.slug == "aedleader" else EXCLUDED_TYPES_SISTERS
        self.category_map = category_map_for(tenant.slug)
        self.state = SyncState(self.history_file)


_contexts: dict[str, TenantSyncContext] = {}


def get_ctx(site: str | None = None) -> TenantSyncContext:
    """Lazily-built per-tenant sync context. Default: aedleader (tenant zero)."""
    slug = site or "aedleader"
    if slug not in _contexts:
        from tenants import TENANTS
        tenant = TENANTS.get(slug)
        if tenant is None:
            raise ValueError(f"Unknown tenant '{slug}'. Valid: {', '.join(TENANTS)}")
        _contexts[slug] = TenantSyncContext(tenant)
    return _contexts[slug]


# Retain the default tenant state alias for compatibility.
sync_state = get_ctx().state


def load_categories_lookup(ctx=None) -> dict:
    """Load categories from the tenant's cached JSON file."""
    cats_path = ctx.categories_file if ctx else os.path.join(
        os.path.dirname(__file__), "data", "categories.json")
    try:
        with open(cats_path, "r", encoding="utf-8") as f:
            categories = json.load(f)
        return {cat["id"]: cat for cat in categories}
    except FileNotFoundError:
        log.warning(f"{os.path.basename(cats_path)} not found, fetching from API...")
        from fetch_products import fetch_product_categories
        tenant = ctx.tenant if ctx and ctx.slug != "aedleader" else None
        categories = fetch_product_categories(tenant)
        return {cat["id"]: cat for cat in categories}


def run_incremental_sync(site: str | None = None) -> dict:
    """Incremental sync: fetch recently modified products, compare hashes, update Qdrant.
    This is the fast path — runs every 15 minutes.
    Does NOT detect deleted products (use full sync for that).
    site: tenant slug (default aedleader — legacy behavior unchanged).
    """
    ctx = get_ctx(site)
    state = ctx.state
    start_time = time.time()
    log.info(f"[{ctx.slug}] Starting incremental sync...")

    # Determine time window
    if state.last_incremental_sync:
        since = state.last_incremental_sync - timedelta(minutes=5)  # 5-min overlap for safety
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=24)  # First run: last 24h

    # Fetch modified products from WooCommerce
    modified_products = fetch_modified_products(since, ctx)
    log.info(f"[{ctx.slug}] WooCommerce returned {len(modified_products)} modified products since {since.isoformat()}")

    # Sister hygiene: subscription-type products are never indexed
    if ctx.exclude_types:
        modified_products = [p for p in modified_products if p.get("type") not in ctx.exclude_types]

    if not modified_products:
        result = {
            "type": "incremental",
            "site": ctx.slug,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "products_checked": 0,
            "re_embedded": 0,
            "payload_updated": 0,
            "skipped": 0,
            "new_indexed": 0,
            "errors": 0,
            "duration_seconds": round(time.time() - start_time, 2),
        }
        state.last_incremental_sync = datetime.now(timezone.utc)
        state.add_result(result)
        return result

    # Get current Qdrant state
    qdrant_state = get_qdrant_state(ctx)
    categories_lookup = load_categories_lookup(ctx)

    # Classify and process each product
    stats = {"re_embedded": 0, "payload_updated": 0, "skipped": 0, "new_indexed": 0, "errors": 0}

    for product in modified_products:
        pid = product["id"]
        try:
            top_cat = extract_top_category(product, categories_lookup, ctx.category_map)

            if pid not in qdrant_state:
                # New product — full index
                sync_product_full(product, top_cat, ctx)
                stats["new_indexed"] += 1
                new_payload = build_payload(product, top_cat)
                audit_update(pid, product["name"], "new", "new product added to catalog",
                             old_values=None, new_values=_extract_tracked_fields(new_payload), ctx=ctx)
            else:
                action, reason = classify_product(product, qdrant_state[pid], top_cat)

                if action == "re_embed":
                    old_fields = _extract_tracked_fields(qdrant_state[pid])
                    sync_product_full(product, top_cat, ctx)
                    new_payload = build_payload(product, top_cat)
                    new_fields = _extract_tracked_fields(new_payload)
                    _diff_fields(old_fields, new_fields)
                    audit_update(pid, product["name"], "re_embed", reason,
                                 old_values=old_fields, new_values=new_fields, ctx=ctx)
                    stats["re_embedded"] += 1
                elif action == "update_payload":
                    old_fields = _extract_tracked_fields(qdrant_state[pid])
                    sync_product_payload(pid, product, top_cat, ctx)
                    new_payload = build_payload(product, top_cat)
                    new_fields = _extract_tracked_fields(new_payload)
                    _diff_fields(old_fields, new_fields)
                    audit_update(pid, product["name"], "update_payload", reason,
                                 old_values=old_fields, new_values=new_fields, ctx=ctx)
                    stats["payload_updated"] += 1
                else:
                    stats["skipped"] += 1

        except Exception as e:
            stats["errors"] += 1
            log.error(f"  ERROR syncing ID:{pid}: {e}")

    duration = round(time.time() - start_time, 2)
    result = {
        "type": "incremental",
        "site": ctx.slug,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "products_checked": len(modified_products),
        **stats,
        "duration_seconds": duration,
    }

    state.last_incremental_sync = datetime.now(timezone.utc)
    state.add_result(result)

    log.info(f"Incremental sync done in {duration}s: "
             f"{stats['new_indexed']} new, {stats['re_embedded']} re-embedded, "
             f"{stats['payload_updated']} payload-updated, {stats['skipped']} skipped, "
             f"{stats['errors']} errors")

    return result


def run_full_sync(site: str | None = None) -> dict:
    """Full catalog sync: compare ALL products, catch deletions.
    This is the slow path — runs once per day as a safety net.
    site: tenant slug (default aedleader — legacy behavior unchanged).
    """
    ctx = get_ctx(site)
    state = ctx.state
    start_time = time.time()
    log.info(f"[{ctx.slug}] Starting full catalog sync...")

    # Fetch all products from WooCommerce
    from fetch_products import fetch_all_products
    wc_products = fetch_all_products(ctx.tenant if ctx.slug != "aedleader" else None)
    log.info(f"[{ctx.slug}] Fetched {len(wc_products)} products from WooCommerce")

    # Sister hygiene: subscription-type products are treated as not-in-catalog —
    # they're skipped for indexing AND absent from wc_product_ids, so any that
    # ever slipped into Qdrant get deleted as stale below.
    if ctx.exclude_types:
        wc_products = [p for p in wc_products if p.get("type") not in ctx.exclude_types]

    # Get current Qdrant state
    qdrant_state = get_qdrant_state(ctx)
    categories_lookup = load_categories_lookup(ctx)

    wc_product_ids = set()
    stats = {"re_embedded": 0, "payload_updated": 0, "skipped": 0, "new_indexed": 0, "deleted": 0, "errors": 0}

    # Process in batches for re-embeds (to batch OpenAI API calls)
    to_reembed = []  # (product, top_category)

    for product in wc_products:
        pid = product["id"]
        wc_product_ids.add(pid)

        try:
            top_cat = extract_top_category(product, categories_lookup, ctx.category_map)

            if pid not in qdrant_state:
                to_reembed.append((product, top_cat))
                stats["new_indexed"] += 1
                new_payload = build_payload(product, top_cat)
                audit_update(pid, product["name"], "new", "new product added to catalog",
                             old_values=None, new_values=_extract_tracked_fields(new_payload), ctx=ctx)
            else:
                action, reason = classify_product(product, qdrant_state[pid], top_cat)

                if action == "re_embed":
                    old_fields = _extract_tracked_fields(qdrant_state[pid])
                    to_reembed.append((product, top_cat))
                    new_payload = build_payload(product, top_cat)
                    new_fields = _extract_tracked_fields(new_payload)
                    audit_update(pid, product["name"], "re_embed", reason,
                                 old_values=old_fields, new_values=new_fields, ctx=ctx)
                    stats["re_embedded"] += 1
                elif action == "update_payload":
                    old_fields = _extract_tracked_fields(qdrant_state[pid])
                    sync_product_payload(pid, product, top_cat, ctx)
                    new_payload = build_payload(product, top_cat)
                    new_fields = _extract_tracked_fields(new_payload)
                    audit_update(pid, product["name"], "update_payload", reason,
                                 old_values=old_fields, new_values=new_fields, ctx=ctx)
                    stats["payload_updated"] += 1
                else:
                    stats["skipped"] += 1

        except Exception as e:
            stats["errors"] += 1
            log.error(f"  ERROR processing ID:{pid}: {e}")

    # Batch re-embed (more efficient than one-by-one for OpenAI API)
    if to_reembed:
        log.info(f"Batch re-embedding {len(to_reembed)} products...")
        _batch_reembed(to_reembed, ctx)

    # Detect and delete stale products — audit each deletion
    qdrant_ids = set(qdrant_state.keys())
    stale_ids = qdrant_ids - wc_product_ids
    if stale_ids:
        for stale_id in stale_ids:
            audit_deletion(stale_id, qdrant_state.get(stale_id, {}), ctx=ctx)
        delete_stale_products(stale_ids, ctx)
        stats["deleted"] = len(stale_ids)

    duration = round(time.time() - start_time, 2)
    result = {
        "type": "full",
        "site": ctx.slug,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "wc_total": len(wc_products),
        "qdrant_total": len(qdrant_state),
        **stats,
        "duration_seconds": duration,
    }

    state.last_full_sync = datetime.now(timezone.utc)
    state.add_result(result)

    log.info(f"Full sync done in {duration}s: "
             f"{stats['new_indexed']} new, {stats['re_embedded']} re-embedded, "
             f"{stats['payload_updated']} payload-updated, {stats['skipped']} skipped, "
             f"{stats['deleted']} deleted, {stats['errors']} errors")

    return result


def _batch_reembed(products_with_categories: list[tuple], ctx=None):
    """Batch re-embed multiple products efficiently (fewer OpenAI API calls)."""
    from qdrant_client.models import PointStruct

    for i in range(0, len(products_with_categories), BATCH_SIZE):
        batch = products_with_categories[i:i + BATCH_SIZE]

        texts = []
        payloads = []
        for product, top_cat in batch:
            texts.append(build_embedding_text(product, top_cat))
            payload = build_payload(product, top_cat)

            # Normalize sale_price
            if payload.get("sale_price") is None or payload.get("sale_price") == "":
                payload["sale_price"] = None

            # Add new hashes
            payload["embedding_hash"] = compute_embedding_hash(product, top_cat)
            payload["payload_hash"] = compute_payload_hash(product)
            payloads.append(payload)

        # Batch embed
        log.info(f"  Embedding batch of {len(texts)} products...")
        dense_vecs = embed_dense(texts)
        sparse_vecs = embed_sparse(texts)

        points = [
            PointStruct(
                id=product["id"],
                vector={
                    "dense": dense_vec,
                    "sparse": sparse_vec,
                },
                payload=payload,
            )
            for (product, _), dense_vec, sparse_vec, payload
            in zip(batch, dense_vecs, sparse_vecs, payloads)
        ]

        qdrant_client.upsert(
            collection_name=ctx.collection if ctx else COLLECTION_NAME,
            points=points,
        )
        log.info(f"  Upserted {len(points)} points")


def run_deletion_check(site: str | None = None) -> dict:
    """Lightweight deletion check: compare IDs only (no full product fetch).
    Can run more frequently than full sync since it only fetches IDs.
    """
    ctx = get_ctx(site)
    start_time = time.time()
    log.info(f"[{ctx.slug}] Running deletion check...")

    wc_ids = fetch_all_product_ids(ctx)
    qdrant_state = get_qdrant_state(ctx)
    qdrant_ids = set(qdrant_state.keys())

    stale_ids = qdrant_ids - wc_ids

    if stale_ids:
        for stale_id in stale_ids:
            audit_deletion(stale_id, qdrant_state.get(stale_id, {}), ctx=ctx)
        delete_stale_products(stale_ids, ctx)

    duration = round(time.time() - start_time, 2)
    result = {
        "type": "deletion_check",
        "site": ctx.slug,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "wc_count": len(wc_ids),
        "qdrant_count": len(qdrant_ids),
        "deleted": len(stale_ids),
        "duration_seconds": duration,
    }
    ctx.state.add_result(result)
    return result


def get_sync_status(site: str | None = None) -> dict:
    """Return current sync state for monitoring."""
    state = get_ctx(site).state
    return {
        "site": site or "aedleader",
        "last_incremental_sync": state.last_incremental_sync.isoformat() if state.last_incremental_sync else None,
        "last_full_sync": state.last_full_sync.isoformat() if state.last_full_sync else None,
        "total_syncs": len(state.sync_history),
        "recent_syncs": state.sync_history[-5:] if state.sync_history else [],
    }


#   CLI — run directly for manual sync


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run a sync for one tenant")
    parser.add_argument("mode", nargs="?", default="incremental",
                        choices=["incremental", "full", "delete-check"])
    parser.add_argument("--site", default=None, help="tenant slug (default: aedleader)")
    args = parser.parse_args()

    if args.mode == "full":
        print(f"Running FULL catalog sync ({args.site or 'aedleader'})...")
        result = run_full_sync(args.site)
    elif args.mode == "delete-check":
        print(f"Running deletion check ({args.site or 'aedleader'})...")
        result = run_deletion_check(args.site)
    else:
        print(f"Running INCREMENTAL sync ({args.site or 'aedleader'})...")
        result = run_incremental_sync(args.site)

    print(json.dumps(result, indent=2))
