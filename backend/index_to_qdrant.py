import os
import sys
import json
import hashlib
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from fastembed import SparseTextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    SparseVectorParams,
    PointStruct,
    SparseVector,
    PayloadSchemaType,
)

load_dotenv()

# Config
QDRANT_URL = os.getenv("QDRANT_URL")
if not QDRANT_URL:
    raise RuntimeError("QDRANT_URL environment variable is required")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
COLLECTION_NAME = "aed_products"
EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIM = 3072
BATCH_SIZE = 50  # products per embedding batch

openai_client = OpenAI(api_key=OPENAI_API_KEY)
qdrant_client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY or None)
sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")

# Top-level category mapping
# aedleader's map — the tenant-zero invariant. NEVER add sister-site slugs here:
# a new key can silently re-categorize aedleader products (top_category feeds the
# embedding hash → churn + filter behavior change). Sister slugs go in
# _SISTER_EXTRA_SLUGS below; each tenant gets its own map via category_map_for().
TOP_LEVEL_CATEGORIES = {
    "aed-accessories": "AED Accessories",
    "aed-training-products": "AED Training Products",
    "cabinets-signs": "Cabinets & Signs",
    "aed-machine": "AED Machine",
    "aed-brands": "AED Brands",
    "value-packages": "AED Packages",
    "first-aid": "First Aid",
    "mobile-trauma-kits": "Mobile Trauma Kits",
    "bleeding-control-kits": "Bleeding Control Kits",
    "recertified-used-aeds": "Recertified AEDs",
    "manikins": "Manikins",
    "clearance-product": "Clearance",
    "emergency-preparedness-kits": "Emergency Preparedness Kits",
    "aed-trainer-devices": "AED Trainer Devices",
    "rental-aeds": "Rental AEDs",
}

# Map sister-store category slugs to the shared search vocabulary; unmapped categories remain Other.


_SISTER_EXTRA_SLUGS = {
    "aed-packages": "AED Packages",          # sisters' slug for value-packages
    "first-aid-kits": "First Aid",           # sisters' slug for first-aid
    "cpr-and-aed-training": "AED Training Products",
    "training": "AED Training Products",     # cpr1 variant
}


def category_map_for(tenant_slug: str) -> dict:
    """Per-tenant top-level category map (aedleader = unchanged base map)."""
    if tenant_slug == "aedleader":
        return TOP_LEVEL_CATEGORIES
    return {**TOP_LEVEL_CATEGORIES, **_SISTER_EXTRA_SLUGS}


# Repair replacement characters heuristically when the original source bytes are unavailable.


_MOJIBAKE_BETWEEN_LETTERS = re.compile(r"(?<=\w)�(?=\w)")


def fix_mojibake(text):
    if not text or "�" not in text:
        return text
    text = _MOJIBAKE_BETWEEN_LETTERS.sub("'", text)
    text = text.replace("�", "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def strip_html(html_text):
    """Remove HTML tags and clean up whitespace."""
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()
    return fix_mojibake(text)


def extract_brand(product):
    """Extract and canonicalize a product brand for consistent indexing and filtering."""
    from brands import canonical_brand
    brands = product.get("brands", [])
    if brands:
        return canonical_brand(brands[0].get("name", ""))
    # Infer missing brands from category names, then from product names.


    brand_keywords = ["zoll", "philips", "cardiac science", "defibtech", "heartsine", "lifepak", "physio-control", "physio control"]
    for cat in product.get("categories", []):
        cat_name = cat.get("name", "").lower()
        for brand in brand_keywords:
            if brand in cat_name:
                return canonical_brand(brand)
    name = (product.get("name") or "").lower()
    for brand in brand_keywords:
        if brand in name:
            return canonical_brand(brand)
    return ""


def extract_top_category(product, categories_lookup, category_map=None):
    """Determine ALL top-level categories by walking up the full parent chain.
    Returns a sorted list of matching top-level categories (e.g. ["AED Machine", "AED Packages"]).
    Products can genuinely belong to multiple top-level categories.
    category_map defaults to aedleader's map (tenant-zero behavior unchanged).
    """
    cmap = category_map or TOP_LEVEL_CATEGORIES
    matches = set()
    for cat in product.get("categories", []):
        slug = cat.get("slug", "")
        # Direct match
        if slug in cmap:
            matches.add(cmap[slug])
        # Walk up parent chain (handles deeply nested categories)
        cat_id = cat.get("id")
        visited = set()
        while cat_id and cat_id in categories_lookup and cat_id not in visited:
            visited.add(cat_id)
            parent_id = categories_lookup[cat_id].get("parent", 0)
            if parent_id and parent_id in categories_lookup:
                parent_slug = categories_lookup[parent_id].get("slug", "")
                if parent_slug in cmap:
                    matches.add(cmap[parent_slug])
            cat_id = parent_id
    return sorted(matches) if matches else ["Other"]


def build_embedding_text(product, top_category):
    """Build text for embedding — STABLE fields only, no price/stock."""
    brand = extract_brand(product)
    name = fix_mojibake(product.get("name", ""))
    short_desc = strip_html(product.get("short_description", ""))
    full_desc = strip_html(product.get("description", ""))

    # Truncate full description to ~500 chars
    if len(full_desc) > 500:
        full_desc = full_desc[:500].rsplit(" ", 1)[0] + "..."

    # Sub-categories
    sub_cats = [cat["name"] for cat in product.get("categories", [])]

    # Tags
    tags = [tag["name"] for tag in product.get("tags", [])]

    # Attributes
    attrs = []
    for attr in product.get("attributes", []):
        attr_name = attr.get("name", "")
        attr_options = attr.get("options", [])
        if attr_options:
            attrs.append(f"{attr_name}: {', '.join(attr_options)}")

    # Build the text
    cat_str = ", ".join(top_category) if isinstance(top_category, list) else top_category
    parts = [f"{name} | {brand} | {cat_str}"]

    if short_desc:
        parts.append(short_desc)
    elif full_desc:
        parts.append(full_desc)

    if full_desc and short_desc:
        parts.append(full_desc)

    if sub_cats:
        parts.append(f"Categories: {', '.join(sub_cats)}")

    if tags:
        parts.append(f"Tags: {', '.join(tags)}")

    sku = product.get("sku", "")
    if sku:
        parts.append(f"SKU: {sku}")

    if attrs:
        parts.append(f"Attributes: {'; '.join(attrs)}")

    return "\n".join(parts)


def build_payload(product, top_category):
    """Build Qdrant payload — includes dynamic fields for filtering."""
    brand = extract_brand(product)
    sub_cats = [cat["name"] for cat in product.get("categories", [])]
    tags = [tag["name"] for tag in product.get("tags", [])]

    # Parse prices safely
    price = 0.0
    try:
        price = float(product.get("price", 0) or 0)
    except (ValueError, TypeError):
        pass

    regular_price = 0.0
    try:
        regular_price = float(product.get("regular_price", 0) or 0)
    except (ValueError, TypeError):
        pass

    sale_price = None
    try:
        sp = product.get("sale_price", "")
        if sp:
            sale_price = float(sp)
    except (ValueError, TypeError):
        pass

    # Image URL
    images = product.get("images", [])
    image_url = images[0]["src"] if images else ""
    thumbnail_url = images[0].get("thumbnail", "") if images else ""

    # Content hash for detecting text changes (re-embed trigger)
    content_for_hash = f"{product.get('name', '')}{product.get('description', '')}{product.get('short_description', '')}"
    content_hash = hashlib.md5(content_for_hash.encode()).hexdigest()

    # Clean descriptions for display
    short_desc = strip_html(product.get("short_description", ""))
    full_desc = strip_html(product.get("description", ""))
    # Truncate very long descriptions to save payload space
    if len(full_desc) > 1000:
        full_desc = full_desc[:1000].rsplit(" ", 1)[0] + "..."

    # Attributes (e.g., "Color: Red, Blue")
    attributes = {}
    for attr in product.get("attributes", []):
        attr_name = attr.get("name", "")
        attr_options = attr.get("options", [])
        if attr_name and attr_options:
            attributes[attr_name] = attr_options

    return {
        "product_id": product["id"],
        "name": fix_mojibake(product.get("name", "")),
        "brand": brand,
        "top_category": top_category,
        "sub_categories": sub_cats,
        "product_type": product.get("type", "simple"),
        "price": price,
        "regular_price": regular_price,
        "sale_price": sale_price,
        "on_sale": product.get("on_sale", False),
        "featured": product.get("featured", False),
        "stock_status": product.get("stock_status", "instock"),
        "sku": product.get("sku", ""),
        "short_description": short_desc,
        "description": full_desc,
        "attributes": attributes,
        "image_url": image_url,
        "thumbnail_url": thumbnail_url,
        "permalink": product.get("permalink", ""),
        "related_ids": product.get("related_ids", []),
        "upsell_ids": product.get("upsell_ids", []),
        "cross_sell_ids": product.get("cross_sell_ids", []),
        "tags": tags,
        "content_hash": content_hash,
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
    }


def create_collection(collection_name=None):
    """Create Qdrant collection with dense + sparse vectors and payload indexes."""
    collection_name = collection_name or COLLECTION_NAME
    collections = [c.name for c in qdrant_client.get_collections().collections]

    if collection_name in collections:
        print(f"Collection '{collection_name}' already exists. Deleting and recreating...")
        qdrant_client.delete_collection(collection_name)

    qdrant_client.create_collection(
        collection_name=collection_name,
        vectors_config={
            "dense": VectorParams(
                size=EMBEDDING_DIM,
                distance=Distance.COSINE,
            ),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(),
        },
    )
    print(f"Created collection '{collection_name}'")
    print(f"  Dense: {EMBEDDING_DIM} dims, cosine")
    print("  Sparse: BM25 (Fastembed)")

    # Create payload indexes for filtering
    indexes = {
        "brand": PayloadSchemaType.KEYWORD,
        "top_category": PayloadSchemaType.KEYWORD,
        "price": PayloadSchemaType.FLOAT,
        "stock_status": PayloadSchemaType.KEYWORD,
        "on_sale": PayloadSchemaType.BOOL,
        "product_type": PayloadSchemaType.KEYWORD,
        "product_id": PayloadSchemaType.INTEGER,
    }

    for field, schema_type in indexes.items():
        qdrant_client.create_payload_index(
            collection_name=collection_name,
            field_name=field,
            field_schema=schema_type,
        )
        print(f"  Created index: {field} ({schema_type})")


def embed_dense(texts):
    """Batch embed texts using OpenAI (dense vectors)."""
    response = openai_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=texts,
    )
    return [item.embedding for item in response.data]


def embed_sparse(texts):
    """Batch embed texts using Fastembed BM25 (sparse vectors)."""
    embeddings = list(sparse_model.embed(texts))
    return [
        SparseVector(
            indices=emb.indices.tolist(),
            values=emb.values.tolist(),
        )
        for emb in embeddings
    ]


# Exclude sister-store subscription products from one-time price comparisons; retain AED Leader rentals.


EXCLUDED_TYPES_SISTERS = ("subscription", "variable-subscription")


def _data_paths(tenant_slug: str) -> tuple[str, str]:
    """Cached catalog file paths. aedleader keeps its legacy filenames."""
    base = os.path.join(os.path.dirname(__file__), "data")
    suffix = "" if tenant_slug == "aedleader" else f"_{tenant_slug}"
    return (
        os.path.join(base, f"products{suffix}.json"),
        os.path.join(base, f"categories{suffix}.json"),
    )


def index_products(site=None):
    """Main pipeline: load products → process → embed (dense+sparse) → index into Qdrant.

    site: tenant slug (default aedleader — legacy behavior, unchanged paths/collection).
    """
    from tenants import TENANTS, DEFAULT_TENANT

    tenant = TENANTS.get(site) if site else DEFAULT_TENANT
    if tenant is None:
        raise SystemExit(f"Unknown site '{site}'. Valid: {', '.join(TENANTS)}")

    products_path, categories_path = _data_paths(tenant.slug)

    # Auto-fetch the catalog when the cache files are missing (sister sites).
    if not (os.path.exists(products_path) and os.path.exists(categories_path)):
        print(f"Catalog cache missing for '{tenant.slug}' — fetching from {tenant.store_url}...")
        from fetch_products import fetch_all_products, fetch_product_categories, save_data
        save_data(fetch_all_products(tenant), fetch_product_categories(tenant), tenant.slug)

    with open(products_path, "r", encoding="utf-8") as f:
        products = json.load(f)

    with open(categories_path, "r", encoding="utf-8") as f:
        categories = json.load(f)

    # Build category lookup by ID
    categories_lookup = {cat["id"]: cat for cat in categories}

    print(f"[{tenant.slug}] Loaded {len(products)} products, {len(categories)} categories")

    # Sister hygiene: exclude subscription-type products (see EXCLUDED_TYPES_SISTERS).
    if tenant.slug != "aedleader":
        excluded = [p for p in products if p.get("type") in EXCLUDED_TYPES_SISTERS]
        if excluded:
            print(f"[{tenant.slug}] EXCLUDED {len(excluded)} subscription-type products:")
            for p in excluded:
                print(f"  - id={p['id']} type={p['type']} | {p.get('name', '')[:60]}")
        products = [p for p in products if p.get("type") not in EXCLUDED_TYPES_SISTERS]

    category_map = category_map_for(tenant.slug)

    # Create collection
    create_collection(tenant.collection)

    # Import shared hash helpers lazily to avoid the import cycle and stamp hashes during indexing.


    from sync import compute_embedding_hash, compute_payload_hash

    # Process all products
    processed = []
    unmapped_top_slugs = set()
    for product in products:
        top_category = extract_top_category(product, categories_lookup, category_map)
        if top_category == ["Other"]:
            for cat in product.get("categories", []):
                if categories_lookup.get(cat.get("id"), {}).get("parent", 0) == 0:
                    unmapped_top_slugs.add(cat.get("slug", ""))
        embedding_text = build_embedding_text(product, top_category)
        payload = build_payload(product, top_category)
        payload["embedding_hash"] = compute_embedding_hash(product, top_category)
        payload["payload_hash"] = compute_payload_hash(product)
        if payload.get("sale_price") == "":
            payload["sale_price"] = None  # Match sync normalization.
        processed.append({
            "id": product["id"],
            "text": embedding_text,
            "payload": payload,
        })

    if unmapped_top_slugs:
        print(f"[{tenant.slug}] top-level slugs mapped to 'Other' (extend map deliberately "
              f"if these deserve a real top_category): {sorted(unmapped_top_slugs)}")

    print(f"\nProcessed {len(processed)} products")
    print(f"Sample embedding text:\n{'='*60}")
    print(processed[0]["text"][:500].encode("ascii", "replace").decode())
    print(f"{'='*60}\n")

    # Embed and upsert in batches
    total_batches = (len(processed) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_idx in range(total_batches):
        start = batch_idx * BATCH_SIZE
        end = min(start + BATCH_SIZE, len(processed))
        batch = processed[start:end]

        print(f"Batch {batch_idx + 1}/{total_batches} ({len(batch)} products)...")

        texts = [item["text"] for item in batch]

        # Generate both dense and sparse embeddings
        print("  Generating dense embeddings (OpenAI)...")
        dense_embeddings = embed_dense(texts)

        print("  Generating sparse embeddings (BM25)...")
        sparse_embeddings = embed_sparse(texts)

        points = [
            PointStruct(
                id=item["id"],
                vector={
                    "dense": dense_emb,
                    "sparse": sparse_emb,
                },
                payload=item["payload"],
            )
            for item, dense_emb, sparse_emb in zip(batch, dense_embeddings, sparse_embeddings)
        ]

        qdrant_client.upsert(
            collection_name=tenant.collection,
            points=points,
        )

        print(f"  Upserted {len(points)} points to Qdrant")

    # Final verification
    collection_info = qdrant_client.get_collection(tenant.collection)
    print(f"\nDone! Collection '{tenant.collection}' now has {collection_info.points_count} points")
    print(f"Vectors config: {list(collection_info.config.params.vectors.keys())}")
    print(f"Sparse config: {list(collection_info.config.params.sparse_vectors.keys())}")


if __name__ == "__main__":
    import argparse
    # Windows cp1252 console crashes on unicode in product names (U+FFFD, dashes)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Index a tenant's catalog into its Qdrant collection")
    parser.add_argument("--site", default=None, help="tenant slug (default: aedleader)")
    args = parser.parse_args()
    index_products(args.site)
