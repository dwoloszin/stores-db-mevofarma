"""
scraper_mevofarma.py — Scraper for Mevo Farma (https://www.mevofarma.com.br)

Platform  : VTEX (deco.cx / Next.js storefront; account "mevofarma")
API host  : https://mevofarma.vtexcommercestable.com.br
            (the public www domain 404s the catalog API; the account host serves it)
API       : /api/catalog_system/pub/products/search/
            /api/catalog_system/pub/category/tree/50
Auth      : none (public VTEX catalog API, Googlebot UA for safety)
Pagination: _from / _to, 50 items per page, VTEX hard cap _to <= 2549 (2550 max per fq)
EAN       : available inline at items[0].ean — ~100% coverage

Category note:
    This VTEX instance requires the FULL hierarchical fq path (C:/1/32/...),
    unlike the short-form C:/{id}/ used by some stores. Categories are planned
    adaptively: any category whose subtree total is <= the 2550 cap is scraped
    directly (a parent path covers its whole subtree); only over-cap categories
    (e.g. Medicamentos, ~5.6k) are descended into their children. Products are
    deduplicated globally by productId.

Usage:
    python -m markets.mevofarma.scraper_mevofarma              # scrape -> DB
    python -m markets.mevofarma.scraper_mevofarma --limit 500  # test run -> DB
    python -m markets.mevofarma.scraper_mevofarma --csv        # scrape -> DB + CSV
"""

import csv
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

sys.stdout.reconfigure(line_buffering=True)

SITE_BASE  = "https://www.mevofarma.com.br"                    # user-facing product URLs
API_BASE   = "https://mevofarma.vtexcommercestable.com.br"     # catalog API host
STORE_ID   = "mevofarma"
PAGE_SIZE  = 50     # items per VTEX page (_to - _from + 1)
PRICE_SENTINEL = 9_999_000  # VTEX "price not available" placeholder (real price is in Price)
VTEX_CAP   = 2550   # hard VTEX limit: _to cannot exceed 2549
DELAY      = 0.20   # seconds between requests

GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


# ──────────────────────────────────────────────────────────────────────────────
# Session
# ──────────────────────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":      GOOGLEBOT_UA,
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    })
    return s


# ──────────────────────────────────────────────────────────────────────────────
# Category planning (adaptive, full hierarchical fq paths)
# ──────────────────────────────────────────────────────────────────────────────

def _category_total(session: requests.Session, fq: str, attempt: int = 0) -> Tuple[int, int]:
    """Return (http_status, subtree_total) for a category fq path."""
    r = session.get(
        f"{API_BASE}/api/catalog_system/pub/products/search/",
        params={"fq": fq, "_from": 0, "_to": 1},
        timeout=25,
    )
    if r.status_code == 429:
        if attempt >= 5:
            return 429, 0
        time.sleep(15)
        return _category_total(session, fq, attempt + 1)
    total = 0
    resources = r.headers.get("resources", "")
    if "/" in resources:
        try:
            total = int(resources.split("/")[-1])
        except ValueError:
            pass
    return r.status_code, total


def plan_categories(session: requests.Session) -> List[Dict]:
    """
    Walk the VTEX category tree and return a minimal set of fq targets that
    cover the whole catalogue, each (where possible) under the 2550 cap.
    """
    r = session.get(f"{API_BASE}/api/catalog_system/pub/category/tree/50", timeout=25)
    r.raise_for_status()
    tree = r.json()

    targets: List[Dict] = []

    def visit(node: Dict, path_ids: List[str], path_names: List[str]) -> None:
        ids   = path_ids + [str(node["id"])]
        names = path_names + [str(node.get("name") or "")]
        fq    = "C:/" + "/".join(ids) + "/"
        children = node.get("children") or []

        status, total = _category_total(session, fq)

        if status not in (200, 206):
            # VTEX occasionally 500s a category — descend into children to recover
            for ch in children:
                visit(ch, ids, names)
            return
        if total <= 0:
            return
        if total <= VTEX_CAP or not children:
            targets.append({"fq": fq, "label": "/".join(n for n in names if n), "total": total})
            return
        # over cap with children → descend (a child path covers its own subtree)
        for ch in children:
            visit(ch, ids, names)

    for top in tree:
        visit(top, [], [])
    return targets


# ──────────────────────────────────────────────────────────────────────────────
# Page fetcher
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_page(session: requests.Session, fq: str, from_: int, attempt: int = 0) -> List[Dict]:
    to_ = min(from_ + PAGE_SIZE - 1, VTEX_CAP - 1)
    r = session.get(
        f"{API_BASE}/api/catalog_system/pub/products/search/",
        params={"fq": fq, "_from": from_, "_to": to_},
        timeout=25,
    )
    if r.status_code == 429:
        if attempt >= 5:
            print("    Rate limited 5x — giving up on this page")
            return []
        print("    Rate limited — sleeping 15s")
        time.sleep(15)
        return _fetch_page(session, fq, from_, attempt + 1)
    if r.status_code not in (200, 206):
        return []
    try:
        return r.json()
    except ValueError:
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Standardize
# ──────────────────────────────────────────────────────────────────────────────

def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _standardize(raw: Dict, cat_label: str) -> Optional[Dict]:
    name = str(raw.get("productName") or "").strip()
    if not name:
        return None

    items   = raw.get("items") or []
    item0   = items[0] if items else {}
    sellers = item0.get("sellers") or []
    offer   = (sellers[0].get("commertialOffer") or {}) if sellers else {}

    regular = _to_float(offer.get("ListPrice"))
    promo   = _to_float(offer.get("Price"))
    # Sentinel ListPrice (e.g. 9999876) with the real price in Price → treat
    # the sentinel as "no regular price" so no phantom discount is recorded.
    if regular and regular >= PRICE_SENTINEL:
        regular = promo
        promo = None
    if not regular or regular <= 0:
        return None
    if promo and promo >= regular:
        promo = None

    discount_pct = (
        round((1 - promo / regular) * 100, 1)
        if promo and regular and regular > 0 else None
    )

    images    = item0.get("images") or []
    image_url = images[0].get("imageUrl", "") if images else ""

    cats = raw.get("categories") or []
    cat_path = cats[0].strip("/") if cats else cat_label

    teasers   = offer.get("Teasers") or []
    offer_tag = teasers[0].get("Name", "") if teasers else ""

    return {
        "product_id":    str(raw.get("productId", "")).strip(),
        "store_id":      STORE_ID,
        "product_name":  name,
        "brand":         str(raw.get("brand") or "").strip(),
        "category_path": cat_path,
        "ean":           str(item0.get("ean") or "").strip(),
        "regular_price": regular,
        "promo_price":   promo,
        "discount_pct":  discount_pct,
        "unit":          str(item0.get("measurementUnit") or "").strip(),
        "is_available":  bool(offer.get("IsAvailable", False)),
        "stock":         offer.get("AvailableQuantity"),
        "offer_tag":     offer_tag,
        "product_url":   f"{SITE_BASE}/{raw.get('linkText', '')}/p",
        "image_url":     image_url,
        "scraped_at":    datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main scrape
# ──────────────────────────────────────────────────────────────────────────────

def scrape(db, limit: Optional[int] = None) -> Dict:
    """Scrape all planned category paths and save to DB per category."""
    import gc

    session = _make_session()
    seen_pids: set = set()
    total_saved = total_upserted = total_history = total_skipped = 0

    print("Planning category tree (full hierarchical fq paths) ...")
    categories = plan_categories(session)
    grand_total = sum(c["total"] for c in categories)
    print(f"Category targets: {len(categories)}  (sum of subtree totals: {grand_total:,})")

    for cat in categories:
        fq        = cat["fq"]
        cat_label = cat["label"]
        cat_total = cat["total"]
        from_     = 0
        cat_offers: List[Dict] = []

        while from_ < VTEX_CAP:
            page = _fetch_page(session, fq, from_)
            if not page:
                break

            new_this_page = 0
            for raw in page:
                pid = str(raw.get("productId", "")).strip()
                if not pid or pid in seen_pids:
                    continue
                seen_pids.add(pid)
                offer = _standardize(raw, cat_label)
                if offer:
                    cat_offers.append(offer)
                    new_this_page += 1

            if from_ == 0 or new_this_page:
                print(
                    f"  {cat_label[:50]:<50}  from={from_:>5}  "
                    f"got={len(page)}  new={new_this_page}  "
                    f"total={cat_total:>6}  saved={total_saved}"
                )

            from_ += len(page)
            if len(page) < PAGE_SIZE:
                break
            if from_ >= VTEX_CAP and cat_total > VTEX_CAP:
                print(
                    f"  WARNING: {cat_label[:50]} has {cat_total} products "
                    f"but VTEX caps at {VTEX_CAP} — {cat_total - VTEX_CAP} unreachable"
                )
                break
            time.sleep(DELAY)
            if limit and total_saved + len(cat_offers) >= limit:
                break

        if cat_offers:
            stats = db.save(cat_offers, verbose=False)
            total_saved    += stats["upserted"]
            total_upserted += stats["upserted"]
            total_history  += stats["history_inserted"]
            total_skipped  += stats["skipped_zero"]
            print(f"    -> saved {stats['upserted']} | price changes {stats['history_inserted']} | cumul {total_saved}")
            cat_offers.clear()
            gc.collect()

        time.sleep(DELAY)
        if limit and total_saved >= limit:
            print(f"Limit {limit} reached — stopping.")
            break

    print(f"\nFinished: {len(seen_pids):,} unique products seen.")
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
        description="Scrape Mevo Farma -> PostgreSQL (DB is always written; CSV is optional)"
    )
    parser.add_argument("--limit",  type=int, default=None, help="Stop after N products (test)")
    parser.add_argument("--csv",    action="store_true",    help="Also save a local CSV file")
    parser.add_argument("--output", type=str, default=None, help="CSV path (implies --csv)")
    parser.add_argument("--env",    type=str, default=".env", help=".env file path")
    args = parser.parse_args()

    from db.db_manager import MevofarmaDB, load_env
    load_env(args.env)

    db    = MevofarmaDB()
    stats = scrape(db, limit=args.limit)
    db.close()

    print(f"\nDone.")
    print(f"  Upserted: {stats['upserted']:,}  "
          f"history: {stats['history_inserted']:,}  "
          f"skipped (zero): {stats['skipped_zero']:,}")

    if args.csv or args.output:
        output_dir = args.output or "."
        db2 = MevofarmaDB()
        db2.export(output_dir, tables=["offers"])
        db2.close()
