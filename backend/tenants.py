"""Configuration registry for the five WooCommerce stores.

Each immutable tenant defines its identity, origins, Qdrant collection,
WooCommerce credentials, contact details, and behavior flags. Credentials come
from environment variables. Request resolution prefers a recognized Origin,
then an explicit site slug, then the AED Leader default."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Tenant:
    slug: str                       # stable id: used in collection names, data paths, events
    display_name: str               # brand name as shown to humans/admin
    store_url: str                  # WooCommerce base URL (wc/v3 REST root lives under it)
    wc_key: str
    wc_secret: str
    collection: str                 # Tenant-specific Qdrant product collection.
    origins: tuple[str, ...]
    phone: str
    email: str
    hours: str | None               # None = not published (MDSI) — bot must not invent
    # AEDTS behavior: "suppress" = aedleader rule (strip AEDTS next to AED machines),
    # "promote" = flagship (aedtotalsolution.com), "neutral" = ordinary product, no special code.
    aedts_mode: str
    aedts_product_ids: tuple[int, ...]
    sync_offset_min: int            # Per-tenant scheduler offset in minutes.


    handoff_reply: str = ""
    # Request email once after helping; off disables proactive requests while accepting supplied contact details.


    lead_ask_mode: str = "on"
    # Course retrieval (cpr1): top_category holding sellable training COURSES
    # (real WC products). When set, a course-shaped faq question also retrieves
    # these products so the bot answers WITH course cards instead of text-only.
    # None = feature off (every other tenant; group courses are cpr1's business).
    course_category: str | None = None


def _env(*names: str) -> str:
    """First non-empty env var among names — lets aedleader keep its legacy keys."""
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""


TENANTS: dict[str, Tenant] = {
    "aedleader": Tenant(
        slug="aedleader",
        display_name="AED Leader",
        store_url=_env("WOOCOMMERCE_URL") or "https://www.aedleader.com",
        wc_key=_env("WC_CONSUMER_KEY"),
        wc_secret=_env("WC_CONSUMER_SECRET"),
        collection="aed_products",  # unchanged — tenant zero, no reindex
        origins=("https://www.aedleader.com", "https://aedleader.com"),
        phone="855-888-2771",
        email="info@aedleader.com",
        hours="Mon–Fri, 8 AM–4:30 PM PST",
        aedts_mode="suppress",  # Suppress program promotion alongside AED machines for this store.
        aedts_product_ids=(981,),
        sync_offset_min=0,
        handoff_reply=(
            "Of course — you can reach our (human) team directly at **855-888-2771** "
            "(Mon–Fri, 8 AM–4:30 PM PST) or by email at **info@aedleader.com**. They can help with quotes, orders, "
            "technical questions, and anything else I can't. Or just reply here with "
            "your phone number and our team will call you back."
        ),
    ),
    "responseready": Tenant(
        slug="responseready",
        display_name="Response Ready",
        store_url=_env("WC_URL_RESPONSEREADY") or "https://responseready.com",
        wc_key=_env("WC_CONSUMER_KEY_RESPONSEREADY"),
        wc_secret=_env("WC_CONSUMER_SECRET_RESPONSEREADY"),
        collection="aed_products_responseready",
        origins=("https://responseready.com", "https://www.responseready.com"),
        phone="858-665-2025",
        email="info@responseready.com",
        hours="Mon–Fri, 8 AM–4:30 PM PST",
        aedts_mode="neutral",  # Treat the program as an ordinary product for this store.
        aedts_product_ids=(8499,),
        sync_offset_min=3,
        handoff_reply=(
            "Of course — you can reach our (human) team directly at **858-665-2025** "
            "(Mon–Fri, 8 AM–4:30 PM PST) or by email at **info@responseready.com**. They can help with quotes, orders, "
            "technical questions, and anything else I can't. Or just reply here with "
            "your phone number and our team will call you back."
        ),
    ),
    "cpr1": Tenant(
        slug="cpr1",
        display_name="CPR1",
        store_url=_env("WC_URL_CPR1") or "https://cpr1.com",
        wc_key=_env("WC_CONSUMER_KEY_CPR1"),
        wc_secret=_env("WC_CONSUMER_SECRET_CPR1"),
        collection="aed_products_cpr1",
        origins=("https://cpr1.com", "https://www.cpr1.com"),
        phone="858-665-8444",
        email="info@cpr1.com",
        hours="Mon–Fri, 8 AM–4:30 PM PST",
        aedts_mode="neutral",
        aedts_product_ids=(),  # No program product IDs are configured for this store.
        sync_offset_min=6,
        handoff_reply=(
            "Of course — you can reach our (human) team directly at **858-665-8444** "
            "(Mon–Fri, 8 AM–4:30 PM PST) or by email at **info@cpr1.com**. They can help with training bookings, quotes, "
            "orders, and anything else I can't. Or just reply here with your phone "
            "number and our team will call you back."
        ),
        # Retrieve group courses from WooCommerce; individual-course information stays in tenant prompt content.


        course_category="AED Training Products",
        lead_ask_mode="on",
    ),
    "mdsimedical": Tenant(
        slug="mdsimedical",
        display_name="MDSI",
        store_url=_env("WC_URL_MDSIMEDICAL") or "https://mdsimedical.com",
        wc_key=_env("WC_CONSUMER_KEY_MDSIMEDICAL"),
        wc_secret=_env("WC_CONSUMER_SECRET_MDSIMEDICAL"),
        collection="aed_products_mdsimedical",
        origins=("https://mdsimedical.com", "https://www.mdsimedical.com"),
        phone="888-683-7988",
        email="customerservice@mdsimedical.com",
        hours=None,  # No published hours are configured for this store.
        aedts_mode="neutral",
        aedts_product_ids=(5528,),
        sync_offset_min=9,
        handoff_reply=(
            "Of course — you can reach our (human) team directly at **888-683-7988** "
            "or by email at **customerservice@mdsimedical.com**. They can help with quotes, orders, "
            "program services, and anything else I can't. Or just reply here with "
            "your phone number and our team will call you back."
        ),
    ),
    "aedtotalsolution": Tenant(
        slug="aedtotalsolution",
        display_name="AED Total Solution",
        store_url=_env("WC_URL_AEDTOTALSOLUTION") or "https://www.aedtotalsolution.com",
        wc_key=_env("WC_CONSUMER_KEY_AEDTOTALSOLUTION"),
        wc_secret=_env("WC_CONSUMER_SECRET_AEDTOTALSOLUTION"),
        collection="aed_products_aedtotalsolution",
        origins=("https://www.aedtotalsolution.com", "https://aedtotalsolution.com"),
        phone="855-263-7772",
        email="info@aedtotalsolution.com",
        hours="Mon–Fri, 8 AM–4:30 PM PST",
        aedts_mode="promote",  # Promote the program on this store.
        aedts_product_ids=(5972, 6261, 9224),
        sync_offset_min=12,
        handoff_reply=(
            "Of course — you can reach our (human) team directly at **855-263-7772** "
            "(Mon–Fri, 8 AM–4:30 PM PST) or by email at **info@aedtotalsolution.com**. They can help with program enrollment, quotes, "
            "orders, and anything else I can't. Or just reply here with your phone "
            "number and our team will call you back."
        ),
    ),
}

DEFAULT_TENANT = TENANTS["aedleader"]

# Origin → tenant lookup, normalized (no trailing slash, lowercase host part is
# guaranteed by construction above).
_ORIGIN_MAP: dict[str, Tenant] = {
    origin: t for t in TENANTS.values() for origin in t.origins
}


def resolve_tenant(origin: str | None, site: str | None = None) -> Tenant:
    """Resolve a recognized Origin, then an explicit site slug, then the default tenant."""
    if origin:
        t = _ORIGIN_MAP.get(origin.strip().rstrip("/"))
        if t:
            return t
    if site:
        t = TENANTS.get(site.strip().lower())
        if t:
            return t
    return DEFAULT_TENANT
