import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

# Legacy aedleader defaults — kept so existing imports/CLI behave unchanged.
WOOCOMMERCE_URL = os.getenv("WOOCOMMERCE_URL")
CONSUMER_KEY = os.getenv("WC_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("WC_CONSUMER_SECRET")

API_BASE = f"{WOOCOMMERCE_URL}/wp-json/wc/v3"

# Use a browser-compatible user agent for store WAF compatibility.
WC_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MediyaChatbot/1.0; +https://mediya.nl)"}


def _api(tenant=None) -> tuple[str, tuple[str, str]]:
    """(api_base, auth) for a tenant; default = aedleader legacy env."""
    if tenant is None:
        return API_BASE, (CONSUMER_KEY, CONSUMER_SECRET)
    return f"{tenant.store_url.rstrip('/')}/wp-json/wc/v3", (tenant.wc_key, tenant.wc_secret)


def fetch_all_products(tenant=None):
    """Fetch all published products from WooCommerce, handling pagination."""
    api_base, auth = _api(tenant)
    all_products = []
    page = 1

    while True:
        print(f"Fetching page {page}...")
        response = requests.get(
            f"{api_base}/products",
            auth=auth,
            headers=WC_HTTP_HEADERS,
            params={"per_page": 100, "page": page, "status": "publish"},
            timeout=60,
        )
        response.raise_for_status()

        products = response.json()
        if not products:
            break

        all_products.extend(products)

        total_pages = int(response.headers.get("X-WP-TotalPages", 1))
        total_items = int(response.headers.get("X-WP-Total", 0))

        if page == 1:
            print(f"Total products: {total_items} across {total_pages} pages")

        if page >= total_pages:
            break

        page += 1

    return all_products


def fetch_product_categories(tenant=None):
    """Fetch all product categories."""
    api_base, auth = _api(tenant)
    all_categories = []
    page = 1

    while True:
        response = requests.get(
            f"{api_base}/products/categories",
            auth=auth,
            headers=WC_HTTP_HEADERS,
            params={"per_page": 100, "page": page},
            timeout=60,
        )
        response.raise_for_status()

        categories = response.json()
        if not categories:
            break

        all_categories.extend(categories)

        total_pages = int(response.headers.get("X-WP-TotalPages", 1))
        if page >= total_pages:
            break

        page += 1

    return all_categories


def save_data(products, categories, tenant_slug=None):
    """Save fetched data to JSON files. aedleader keeps legacy filenames."""
    os.makedirs("data", exist_ok=True)
    suffix = "" if not tenant_slug or tenant_slug == "aedleader" else f"_{tenant_slug}"

    products_path = f"data/products{suffix}.json"
    categories_path = f"data/categories{suffix}.json"

    with open(products_path, "w", encoding="utf-8") as f:
        json.dump(products, f, indent=2, ensure_ascii=False)

    with open(categories_path, "w", encoding="utf-8") as f:
        json.dump(categories, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(products)} products to {products_path}")
    print(f"Saved {len(categories)} categories to {categories_path}")


def print_summary(products, categories):
    """Print a quick summary of the fetched data."""
    print("\n" + "=" * 60)
    print("PRODUCT CATALOG SUMMARY")
    print("=" * 60)

    print(f"\nTotal Products: {len(products)}")
    print(f"Total Categories: {len(categories)}")

    # Price range
    prices = [float(p["price"]) for p in products if p.get("price")]
    if prices:
        print(f"Price Range: ${min(prices):.2f} - ${max(prices):.2f}")

    # Products by type
    types = {}
    for p in products:
        t = p.get("type", "unknown")
        types[t] = types.get(t, 0) + 1
    print(f"\nProduct Types: {types}")

    # Stock status
    stock = {}
    for p in products:
        s = p.get("stock_status", "unknown")
        stock[s] = stock.get(s, 0) + 1
    print(f"Stock Status: {stock}")

    # Categories breakdown
    print("\nCategories:")
    for cat in sorted(categories, key=lambda c: c.get("count", 0), reverse=True):
        if cat.get("count", 0) > 0:
            print(f"  - {cat['name']}: {cat['count']} products")

    # Sample products
    print("\nSample Products (first 5):")
    for p in products[:5]:
        sale = f" (sale: ${p['sale_price']})" if p.get("sale_price") else ""
        print(f"  - {p['name']} | ${p.get('price', 'N/A')}{sale} | {p.get('stock_status', '?')}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fetch a tenant's WooCommerce catalog")
    parser.add_argument("--site", default=None, help="tenant slug (default: aedleader)")
    args = parser.parse_args()

    tenant = None
    slug = args.site or "aedleader"
    if args.site:
        from tenants import TENANTS
        tenant = TENANTS.get(args.site)
        if tenant is None:
            raise SystemExit(f"Unknown site '{args.site}'. Valid: {', '.join(TENANTS)}")

    store = tenant.store_url if tenant else WOOCOMMERCE_URL
    print(f"Fetching products from {store} ({slug})...\n")

    products = fetch_all_products(tenant)
    categories = fetch_product_categories(tenant)

    save_data(products, categories, slug)
    print_summary(products, categories)
