"""Catalog-derived price facts for response generation.

Read program, rental, recertified AED, and course prices from each tenant's
synced Qdrant collection. Cache results per tenant; omit facts on read failures."""

import os
import time

from search import qdrant_client

FACTS_TTL = int(os.getenv("CATALOG_FACTS_TTL", "600"))
_FAILURE_TTL = 60
_FIELDS = ["product_id", "name", "price", "stock_status", "top_category", "product_type"]
_cache: dict[str, tuple[float, dict]] = {}


def _money(value) -> str:
    v = float(value)
    return f"${v:,.0f}" if v.is_integer() else f"${v:,.2f}"


def _compute(tenant) -> dict:
    """{"aedts": line | None, "other": [lines]} from the tenant's collection."""
    points, _ = qdrant_client.scroll(tenant.collection, limit=2000, with_payload=_FIELDS)
    products = [p.payload for p in points if p.payload and p.payload.get("price") is not None]
    instock = [p for p in products if p.get("stock_status") == "instock"]

    aedts_line = None
    aedts = sorted((p for p in products if p.get("product_id") in tenant.aedts_product_ids),
                   key=lambda p: p["price"])
    if aedts:
        seen, parts = set(), []
        for p in aedts:
            name = " ".join(p["name"].split())
            if name.lower() not in seen:
                seen.add(name.lower())
                parts.append(f"{name} {_money(p['price'])}")
        aedts_line = "- AED Total Solution program: " + "; ".join(parts)

    other = []
    recert = [p["price"] for p in instock if "recertified" in p.get("name", "").lower()]
    if recert:
        other.append(f"- Recertified AEDs: {_money(min(recert))}–{_money(max(recert))} "
                     f"({len(recert)} models in stock)")
    rentals = [p["price"] for p in instock if "subscription" in str(p.get("product_type"))]
    if rentals:
        other.append(f"- AED rentals: from {_money(min(rentals))} "
                     f"(billing term is on each rental's product page — do not state one)")
    if tenant.course_category:
        courses = sorted((p for p in instock if tenant.course_category in (p.get("top_category") or [])),
                         key=lambda p: -p["price"])
        if courses:
            other.append("- Group training courses: " + "; ".join(
                f"{' '.join(p['name'].split())} {_money(p['price'])}" for p in courses))
    return {"aedts": aedts_line, "other": other}


def _facts(tenant) -> dict:
    now = time.time()
    hit = _cache.get(tenant.slug)
    if hit and hit[0] > now:
        return hit[1]
    try:
        facts, ttl = _compute(tenant), FACTS_TTL
    except Exception as e:
        print(f"  [CatalogFacts] {tenant.slug}: catalog read failed ({type(e).__name__}) — no facts block")
        facts, ttl = {"aedts": None, "other": []}, _FAILURE_TTL
    _cache[tenant.slug] = (now + ttl, facts)
    return facts


def facts_block(tenant, include_aedts: bool = True) -> str:
    """The CATALOG FACTS block for step4, or "" when there is nothing to say.
    include_aedts=False keeps the AEDTS price out (aedleader's suppress mode:
    a price line on every turn would invite mentions next to AED machines)."""
    facts = _facts(tenant)
    lines = ([facts["aedts"]] if include_aedts and facts["aedts"] else []) + facts["other"]
    if not lines:
        return ""
    return ("## CATALOG FACTS (live from this store's catalog — quote these prices only "
            "from here or from SEARCH RESULTS)\n" + "\n".join(lines))
