"""
scraper_mundial.py — Scraper for Mundial Farma (https://mundialfarma.com.br)

Platform  : Magento 2 Open Source (theme "Mageshop"). MIGRATED off WooCommerce
            ~2026-11 — the old `/wp-json/wc/store/v1/products` Store API 404s
            now. Confirmed by homepage markup ("Magento", requirejs-config.js)
            and the dead WooCommerce endpoint.
API       : public Magento GraphQL, POST /graphql, no auth needed.
            products(filter: {category_id: {eq: "2"}}, pageSize, currentPage)
            — category id 2 is the storefront ROOT ("Menu Loja Virtual") and
            returns the WHOLE catalogue (111 products) in one filter — no need
            to walk the category tree (small, specialty/oncology catalogue).
Pagination: pageSize<=100, page_info.total_pages gives the loop bound.
EAN       : `sku` — most are 13-digit EANs; a few high-value biologics carry a
            longer internal/ANVISA code instead (not a real EAN) — same EAN
            regex validation as the rest of the fleet filters those out.
Price     : price_range.minimum_price.{regular_price,final_price}.value (BRL,
            already decimal, NOT minor units like the old WooCommerce API).
URL       : product_url = {BASE_URL}/{url_key}  (clean URLs, no .html suffix —
            confirmed against the live site, e.g. .../keytruda-100mg-4ml).

Usage:
    python -m markets.mundial.scraper_mundial              # scrape -> DB
    python -m markets.mundial.scraper_mundial --limit 100  # test run -> DB
    python -m markets.mundial.scraper_mundial --csv        # scrape -> DB + CSV
"""

import csv
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

sys.stdout.reconfigure(line_buffering=True)

BASE_URL    = "https://mundialfarma.com.br"
STORE_ID    = "mundial"
GRAPHQL_URL = f"{BASE_URL}/graphql"
ROOT_CAT_ID = "2"    # storefront root ("Menu Loja Virtual") -> whole catalogue
PAGE_SIZE   = 100    # Magento GraphQL default pageSize cap
DELAY       = 0.25   # seconds between page requests
MAX_TRIES   = 6

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_QUERY = """
query ($catId: String!, $pageSize: Int!, $currentPage: Int!) {
  products(filter: {category_id: {eq: $catId}}, pageSize: $pageSize, currentPage: $currentPage) {
    total_count
    page_info { current_page total_pages }
    items {
      id
      sku
      name
      url_key
      stock_status
      categories { name }
      small_image { url }
      price_range {
        minimum_price {
          regular_price { value }
          final_price { value }
          discount { percent_off }
        }
      }
    }
  }
}
"""


# ──────────────────────────────────────────────────────────────────────────────
# Session / fetch
# ──────────────────────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":      BROWSER_UA,
        "Accept":          "application/json",
        "Content-Type":    "application/json",
        "Accept-Language": "pt-BR,pt;q=0.9",
    })
    return s


def _fetch_page(session: requests.Session, page: int) -> Optional[Dict]:
    """POST one GraphQL page with back-off on 429/5xx. Returns the `products` dict or None."""
    body = {"query": _QUERY, "variables": {"catId": ROOT_CAT_ID, "pageSize": PAGE_SIZE, "currentPage": page}}
    for attempt in range(MAX_TRIES):
        try:
            r = session.post(GRAPHQL_URL, json=body, timeout=30)
        except requests.RequestException:
            time.sleep(min(3 * (attempt + 1), 20))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(5 * (attempt + 1), 30))
            continue
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        if data.get("errors"):
            return None
        return (data.get("data") or {}).get("products")
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Standardize
# ──────────────────────────────────────────────────────────────────────────────

_EAN_RE = re.compile(r"^\d{8,14}$")


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _standardize(p: Dict) -> Optional[Dict]:
    name = str(p.get("name") or "").strip()
    pid  = str(p.get("id") or "").strip()
    if not name or not pid:
        return None

    price_range = p.get("price_range") or {}
    min_price   = price_range.get("minimum_price") or {}
    regular = _to_float((min_price.get("regular_price") or {}).get("value"))
    final   = _to_float((min_price.get("final_price") or {}).get("value"))

    if regular is None or regular <= 0:
        if final and final > 0:
            regular = final
        else:
            return None

    promo_price = final if (final is not None and 0 < final < regular) else None
    discount_pct = (
        round((1 - promo_price / regular) * 100, 1)
        if promo_price and regular > 0 else None
    )

    sku = str(p.get("sku") or "").strip()
    ean = sku if _EAN_RE.match(sku) else ""

    cats = p.get("categories") or []
    category_path = " > ".join(str(c.get("name") or "").strip() for c in cats if c.get("name"))

    image_url = str((p.get("small_image") or {}).get("url") or "").strip()

    url_key = str(p.get("url_key") or "").strip()
    product_url = f"{BASE_URL}/{url_key}" if url_key else BASE_URL

    return {
        "product_id":    pid,
        "store_id":      STORE_ID,
        "product_name":  name,
        "brand":         "",
        "category_path": category_path,
        "ean":           ean,
        "regular_price": regular,
        "promo_price":   promo_price,
        "discount_pct":  discount_pct,
        "unit":          "",
        "is_available":  str(p.get("stock_status") or "").upper() == "IN_STOCK",
        "stock":         None,
        "offer_tag":     sku if not ean else "",
        "is_discounted": promo_price is not None,
        "product_url":   product_url,
        "image_url":     image_url,
        "scraped_at":    datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main scrape
# ──────────────────────────────────────────────────────────────────────────────

def scrape(db, limit: Optional[int] = None) -> Dict:
    session = _make_session()

    total_upserted = total_history = total_skipped = total_saved = 0
    seen: set = set()

    first = _fetch_page(session, 1)
    if first is None:
        print("ERROR: could not fetch products page 1 (GraphQL) — aborting.")
        return {"upserted": 0, "history_inserted": 0, "skipped_zero": 0, "total_unique": 0}

    total_products = int(first.get("total_count") or 0)
    total_pages    = int((first.get("page_info") or {}).get("total_pages") or 1)
    print(f"Magento GraphQL: {total_products:,} products across {total_pages} page(s)")

    def _flush(offers: List[Dict]) -> None:
        nonlocal total_saved, total_upserted, total_history, total_skipped
        if not offers:
            return
        stats = db.save(offers, verbose=False)
        total_saved    += stats["upserted"]
        total_upserted += stats["upserted"]
        total_history  += stats["history_inserted"]
        total_skipped  += stats["skipped_zero"]
        print(f"    -> saved {stats['upserted']} | price changes {stats['history_inserted']} | cumul {total_saved}")

    page = 1
    products = first
    while True:
        items = products.get("items") or []
        offers: List[Dict] = []
        for p in items:
            pid = str(p.get("id") or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            offer = _standardize(p)
            if offer:
                offers.append(offer)
        _flush(offers)
        print(f"  page {page}/{total_pages}: {len(offers)} products  (cumul {total_saved})")

        if limit and total_saved >= limit:
            print(f"Limit {limit} reached — stopping.")
            break
        page += 1
        if page > total_pages:
            break
        time.sleep(DELAY)
        products = _fetch_page(session, page)
        if products is None:
            print(f"  WARNING: page {page} failed after retries — catalogue may be incomplete")
            break

    print(f"\nFinished: {len(seen):,} unique products seen.")
    return {"upserted": total_upserted, "history_inserted": total_history,
            "skipped_zero": total_skipped, "total_unique": total_saved}


# ──────────────────────────────────────────────────────────────────────────────
# CSV export (optional)
# ──────────────────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "product_id", "store_id", "product_name", "brand", "category_path",
    "ean", "regular_price", "promo_price", "discount_pct",
    "unit", "is_available", "stock", "offer_tag",
    "product_url", "image_url", "scraped_at",
]


def save_csv(offers: List[Dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(offers)
    print(f"Saved {len(offers):,} rows -> {path}")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scrape Mundial Farma (Magento GraphQL) -> PostgreSQL (DB always written; CSV optional)"
    )
    parser.add_argument("--limit",  type=int, default=None, help="Stop after N products (test)")
    parser.add_argument("--csv",    action="store_true",    help="Also save a local CSV file")
    parser.add_argument("--output", type=str, default=None, help="CSV path (implies --csv)")
    parser.add_argument("--env",    type=str, default=".env", help=".env file path")
    args = parser.parse_args()

    from db.db_manager import MundialDB, load_env
    load_env(args.env)

    db    = MundialDB()
    stats = scrape(db, limit=args.limit)
    db.close()

    print(f"\nDone.")
    print(f"  Upserted: {stats['upserted']:,}  "
          f"history: {stats['history_inserted']:,}  "
          f"skipped (zero): {stats['skipped_zero']:,}")

    if stats["upserted"] == 0:
        print("ERROR: 0 products scraped — treating as failure.")
        sys.exit(1)

    if args.csv or args.output:
        output_dir = args.output or "."
        db2 = MundialDB()
        db2.export(output_dir, tables=["offers"])
        db2.close()
