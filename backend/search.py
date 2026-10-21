"""
Search & Retrieval module for AED Leader RAG chatbot.
Handles: hybrid search (dense+sparse), metadata filtering, score threshold,
graceful fallback, and upsell logic.
"""

import os
import re
import threading
from dotenv import load_dotenv
from openai import OpenAI
from fastembed import SparseTextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import (
    SparseVector,
    Prefetch,
    FusionQuery,
    Fusion,
    Filter,
    FieldCondition,
    MatchValue,
    Range,
)

load_dotenv()

QDRANT_URL = os.getenv("QDRANT_URL")
if not QDRANT_URL:
    raise RuntimeError("QDRANT_URL environment variable is required")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
COLLECTION_NAME = "aed_products"
EMBEDDING_MODEL = "text-embedding-3-large"
SCORE_THRESHOLD = 0.35  # Base threshold for RRF scores

# AED domain keywords — if query contains NONE of these, it's likely out of scope
DOMAIN_KEYWORDS = {
    "aed", "defibrillator", "defib", "cpr", "pads", "electrode", "battery",
    "cabinet", "sign", "case", "carry", "philips", "zoll", "defibtech",
    "cardiac", "heartsine", "lifepak", "heartstart", "powerheart",
    "samaritan", "lifeline", "first aid", "trauma", "bleeding", "medical",
    "emergency", "rescue", "training", "manikin", "sku", "replacement",
    "refurbished", "recertified", "used", "package", "bundle", "kit",
    "wall mount", "outdoor", "indoor", "school", "office", "gym",
    "church", "pediatric", "adult", "pad", "electrode", "accessory",
    "warranty", "shipping", "return", "price", "maintenance", "compliance",
}

openai_client = OpenAI(api_key=OPENAI_API_KEY)
qdrant_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None, timeout=10)
sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")

# Recognize numeric, alphanumeric, and dashed SKU shapes before semantic retrieval.


_SKU_TOKEN_RE = re.compile(
    r"\b(?:"
    r"\d{2,}[-/][A-Za-z0-9-]+"                      # Require digits in hyphenated part numbers so ordinary hyphenated words do not match.
    r"|[A-Za-z]{1,4}\d{3,}[A-Za-z0-9-]*"


    r"|[A-Za-z]{2,4}-(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{2,}"
    r"|\d{5,}"
    r")\b")


def _normalize_sku(text):
    """Case/dash/space/dot-insensitive form for part-number comparison."""
    return re.sub(r"[-/ ._]", "", text).upper()


def extract_sku_tokens(query_text):
    """Part-number-shaped tokens in a query (may be empty)."""
    return [m.group(0) for m in _SKU_TOKEN_RE.finditer(query_text)]


# Cache normalized SKU and name fields per collection for five minutes.


_SKU_CACHE: dict = {}
_SKU_CACHE_TTL = 300


def _sku_index(collection):
    import time as _time
    cached = _SKU_CACHE.get(collection)
    if cached and _time.time() - cached[0] < _SKU_CACHE_TTL:
        return cached[1]
    entries = []
    offset = None
    while True:
        points, offset = qdrant_client.scroll(
            collection_name=collection,
            limit=256,
            with_payload=["sku", "name", "product_id"],
            offset=offset,
        )
        for p in points:
            pl = p.payload or {}
            sku = (pl.get("sku") or "").strip()
            entries.append((p.id, _normalize_sku(sku) if sku else "", _normalize_sku(pl.get("name") or "")))
        if offset is None:
            break
    _SKU_CACHE[collection] = (_time.time(), entries)
    return entries


def lookup_by_sku(query_text, collection=None, limit=3):
    """Exact/normalized part-number lookup — runs BEFORE vector search.

    A token matches a product when it equals the normalized SKU, or (len>=6)
    appears inside the normalized SKU or product name — so "M5066A" finds the
    OnSite whose name carries the model even when the sku field differs.
    Returns full payloads (score 1.0) or []. No stock filter: a customer who
    asks for an exact part must see it even when out of stock (the card and
    the responder surface stock_status honestly).
    """
    tokens = [_normalize_sku(t) for t in extract_sku_tokens(query_text)]
    tokens = [t for t in tokens if len(t) >= 4]
    if not tokens:
        return []
    coll = collection or COLLECTION_NAME
    matched_ids = []
    for pid, norm_sku, norm_name in _sku_index(coll):
        for t in tokens:
            if (norm_sku and (t == norm_sku or (len(t) >= 6 and t in norm_sku))) or                (len(t) >= 6 and t in norm_name):
                matched_ids.append(pid)
                break
        if len(matched_ids) >= limit:
            break
    if not matched_ids:
        return []
    results = []
    for pid in matched_ids:
        points, _ = qdrant_client.scroll(
            collection_name=coll,
            scroll_filter=Filter(must=[FieldCondition(key="product_id", match=MatchValue(value=pid))]),
            limit=1, with_payload=True,
        )
        if points:
            payload = dict(points[0].payload)
            payload["_score"] = 1.0
            results.append(payload)
    return results


# Embedding helpers


def _embed_dense(text):
    """Get dense embedding for a query string."""
    response = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return response.data[0].embedding


# Serialize sparse embedding calls because wrapper thread safety is unspecified.


_sparse_lock = threading.Lock()


def _embed_sparse(text):
    """Get sparse BM25 embedding for a query string."""
    with _sparse_lock:
        raw = list(sparse_model.embed([text]))[0]
    return SparseVector(indices=raw.indices.tolist(), values=raw.values.tolist())


# Filter builder


def build_filters(
    brand=None,
    top_category=None,
    min_price=None,
    max_price=None,
    on_sale=None,
    stock_status="instock",
    product_type=None,
):
    """
    Build a Qdrant Filter from extracted query parameters.
    Returns None if no filters are needed.
    """
    conditions = []

    if brand:
        # Apply the same brand normalization during indexing and filtering.


        from brands import canonical_brand
        conditions.append(FieldCondition(key="brand", match=MatchValue(value=canonical_brand(brand))))

    if top_category:
        conditions.append(FieldCondition(key="top_category", match=MatchValue(value=top_category)))

    if min_price is not None or max_price is not None:
        range_params = {}
        if min_price is not None:
            range_params["gte"] = float(min_price)
        if max_price is not None:
            range_params["lte"] = float(max_price)
        conditions.append(FieldCondition(key="price", range=Range(**range_params)))

    if on_sale is not None:
        conditions.append(FieldCondition(key="on_sale", match=MatchValue(value=on_sale)))

    if stock_status:
        conditions.append(FieldCondition(key="stock_status", match=MatchValue(value=stock_status)))

    if product_type:
        conditions.append(FieldCondition(key="product_type", match=MatchValue(value=product_type)))

    if not conditions:
        return None

    return Filter(must=conditions)


# Graceful filter fallback


FALLBACK_ORDER = [
    # Relax filters in order: price, brand, category, product type, then remaining constraints.


    lambda f: {k: v for k, v in f.items() if k not in ("min_price", "max_price")},
    lambda f: {k: v for k, v in f.items() if k != "brand"},
    lambda f: {k: v for k, v in f.items() if k != "top_category"},
    lambda f: {k: v for k, v in f.items() if k != "product_type"},
    lambda f: {"stock_status": f.get("stock_status", "instock")},
    lambda f: {},
]


def _search_with_fallback(query_text, filter_params, limit=5, collection=None):
    """Run hybrid retrieval, progressively relaxing filters when no result qualifies.
    Return results, effective filters, relaxation status, and out-of-stock matches."""
    dense_vec = _embed_dense(query_text)
    sparse_vec = _embed_sparse(query_text)

    # Try with original filters first
    current_params = dict(filter_params)
    was_relaxed = False

    def _query(params):
        return qdrant_client.query_points(
            collection_name=collection or COLLECTION_NAME,
            prefetch=[
                Prefetch(query=dense_vec, using="dense", limit=20),
                Prefetch(query=sparse_vec, using="sparse", limit=20),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=build_filters(**params),
            limit=limit,
            with_payload=True,
        )

    def _oos_probe(found_ids):
        # Probe strong out-of-stock matches for honest availability messaging, without rendering them as cards.


        probe_params = {k: v for k, v in dict(filter_params).items() if k != "stock_status"}
        probe_params["stock_status"] = "outofstock"
        try:
            res = _query(probe_params)
        except Exception:
            return []
        return [dict(r.payload, _score=r.score) for r in res.points[:2]
                if r.score >= SCORE_THRESHOLD and r.payload.get("product_id") not in found_ids]

    for i, relax_fn in enumerate([lambda f: f] + FALLBACK_ORDER):
        current_params = relax_fn(current_params)
        results = _query(current_params)

        # Filter by score threshold
        good_results = [r for r in results.points if r.score >= SCORE_THRESHOLD]

        if good_results:
            if i > 0:
                was_relaxed = True
            oos = _oos_probe({r.payload.get("product_id") for r in good_results})
            return good_results, current_params, was_relaxed, oos

    # Nothing found even without filters - still check OOS (the product may
    # exist but be entirely hidden by the instock default)
    return [], {}, True, _oos_probe(set())


# Educational knowledge-chunk retrieval.


def search_knowledge(query_text, collection, limit=3):
    """Retrieve educational chunks from a tenant knowledge collection.
    Return an empty list when the collection is absent or retrieval fails."""
    try:
        dense_vec = _embed_dense(query_text)
        sparse_vec = _embed_sparse(query_text)
        results = qdrant_client.query_points(
            collection_name=collection,
            prefetch=[
                Prefetch(query=dense_vec, using="dense", limit=10),
                Prefetch(query=sparse_vec, using="sparse", limit=10),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
    except Exception:
        return []
    return [dict(r.payload, _score=r.score) for r in results.points
            if r.score >= SCORE_THRESHOLD]


# Upsell logic


def get_upsell_products(product_payload, limit=3, collection=None):
    """Fetch in-stock products in upsell, cross-sell, then related-ID priority order."""
    # Collect candidate IDs in priority order
    candidate_ids = []

    upsell_ids = product_payload.get("upsell_ids", [])
    if upsell_ids:
        candidate_ids.extend(upsell_ids)

    cross_sell_ids = product_payload.get("cross_sell_ids", [])
    if cross_sell_ids:
        candidate_ids.extend(cross_sell_ids)

    related_ids = product_payload.get("related_ids", [])
    if related_ids:
        candidate_ids.extend(related_ids)

    if not candidate_ids:
        return []

    # Deduplicate while preserving order
    seen = set()
    unique_ids = []
    for pid in candidate_ids:
        if pid not in seen:
            seen.add(pid)
            unique_ids.append(pid)

    # Fetch from Qdrant by product_id (filter-only, no vector query needed)
    upsell_products = []
    for pid in unique_ids[:limit * 2]:  # fetch extra, filter below
        points, _offset = qdrant_client.scroll(
            collection_name=collection or COLLECTION_NAME,
            scroll_filter=Filter(must=[
                FieldCondition(key="product_id", match=MatchValue(value=pid)),
                FieldCondition(key="stock_status", match=MatchValue(value="instock")),
            ]),
            limit=1,
            with_payload=True,
        )
        if points:
            upsell_products.append(points[0].payload)

        if len(upsell_products) >= limit:
            break

    return upsell_products


# Direct product lookup (for follow-up references like "the second one")


def lookup_product_by_id(product_id, include_upsells=True, collection=None):
    """
    Fetch a specific product by ID from Qdrant. Used when the user references
    a previously shown product ("the second one", "tell me more about it").
    Returns the same format as search_products() for pipeline compatibility.
    """
    points, _ = qdrant_client.scroll(
        collection_name=collection or COLLECTION_NAME,
        scroll_filter=Filter(must=[
            FieldCondition(key="product_id", match=MatchValue(value=product_id)),
        ]),
        limit=1,
        with_payload=True,
    )

    if not points:
        return None

    product = dict(points[0].payload)
    product["_score"] = 1.0  # Direct lookup, perfect match

    upsells = []
    if include_upsells:
        upsells = get_upsell_products(product, collection=collection)

    return {
        "products": [product],
        "upsells": upsells,
        "filters_used": {"product_id": product_id},
        "was_relaxed": False,
        "total_found": 1,
    }


# Main search function


def search_products(query_text, filters=None, limit=5, include_upsells=True, sort_by=None,
                    collection=None):
    """
    Main search entry point.

    Args:
        query_text: The search-optimized query (from query rewriting step)
        filters: Dict of filter params (brand, top_category, min_price, max_price, etc.)
        limit: Max products to return
        include_upsells: Whether to fetch upsell products for top result
        sort_by: Optional sorting — "price_asc" (cheapest first), "price_desc" (most expensive first)
        collection: Qdrant collection to search (default: aedleader's — tenant-zero unchanged)

    Returns:
        {
            "products": [...],        # List of product payloads with scores
            "upsells": [...],         # Upsell suggestions for top product
            "filters_used": {...},    # Actual filters applied (may differ if relaxed)
            "was_relaxed": bool,      # Whether filters were relaxed to find results
            "total_found": int,
        }
    """
    filter_params = filters or {}

    # Domain relevance check — reject clearly out-of-scope queries
    query_lower = query_text.lower()
    is_in_domain = any(kw in query_lower for kw in DOMAIN_KEYWORDS)
    # Also allow if filters are explicitly set (means query analysis identified it as relevant)
    has_explicit_filters = any(
        v is not None for k, v in filter_params.items() if k != "stock_status"
    )
    # Allow queries that look like SKUs/product codes (all catalog shapes —
    # dashed, alphanumeric, plain-numeric; see _SKU_TOKEN_RE)
    looks_like_sku = bool(extract_sku_tokens(query_text))

    if not is_in_domain and not has_explicit_filters and not looks_like_sku:
        return {
            "products": [],
            "upsells": [],
            "filters_used": {},
            "was_relaxed": False,
            "total_found": 0,
            "out_of_scope": True,
        }

    # Exact normalized part-number matches take precedence over semantic neighbors.


    if looks_like_sku:
        sku_hits = lookup_by_sku(query_text, collection=collection)
        if sku_hits:
            upsells = get_upsell_products(sku_hits[0], collection=collection) if include_upsells else []
            return {
                "products": sku_hits,
                "upsells": upsells,
                "filters_used": {"sku": extract_sku_tokens(query_text)},
                "was_relaxed": False,
                "total_found": len(sku_hits),
                "sku_exact_match": True,
            }

    # Always filter out-of-stock unless explicitly asked
    if "stock_status" not in filter_params:
        filter_params["stock_status"] = "instock"

    # When sorting by price, fetch more results so we have good candidates after sorting
    fetch_limit = limit * 3 if sort_by else limit

    results, filters_used, was_relaxed, oos_matches = _search_with_fallback(
        query_text, filter_params, limit=fetch_limit, collection=collection
    )

    # Build product list with scores
    products = []
    for point in results:
        product = dict(point.payload)
        product["_score"] = point.score
        products.append(product)

    # Apply price sorting if requested
    if sort_by == "price_asc":
        products.sort(key=lambda p: p.get("price", 0))
    elif sort_by == "price_desc":
        products.sort(key=lambda p: p.get("price", 0), reverse=True)

    # Trim to requested limit after sorting
    products = products[:limit]

    # Get upsells for the top result
    upsells = []
    if include_upsells and products:
        upsells = get_upsell_products(products[0], collection=collection)

    return {
        "products": products,
        "upsells": upsells,
        "filters_used": filters_used,
        "was_relaxed": was_relaxed,
        "total_found": len(products),
        # Mention-only: never rendered as cards (step5 builds cards from
        # products/upsells exclusively) - the responder cites these honestly.
        "oos_matches": oos_matches,
    }
