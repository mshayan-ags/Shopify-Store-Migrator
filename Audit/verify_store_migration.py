import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import xlsxwriter
from dotenv import load_dotenv

from utils.shopify_client import ShopifyClient
from transfer.transfer_product import make_client
from utils.concurrency_utils import retry_with_backoff
from utils.shopify_graphql_utils import paginate_connection

from transfer.transfer_pages import fetch_all_pages
from transfer.transfer_blogs import fetch_all_blogs, fetch_blog_articles
from transfer.transfer_navigation import fetch_all_menus
from transfer.transfer_customers import fetch_all_customers
from transfer.transfer_redirects import fetch_all_redirects
from transfer.transfer_discounts import fetch_discount_nodes
from transfer.transfer_orders import fetch_all_orders
from transfer.transfer_policies import fetch_shop_policies
from transfer.transfer_draft_orders import fetch_all_draft_orders
from transfer.transfer_gift_cards import fetch_all_gift_cards
from transfer.transfer_b2b import fetch_all_companies
from transfer.transfer_markets import fetch_all_markets
from transfer.transfer_files import export_files
from transfer.transfer_selling_plans import export_selling_plan_groups
from transfer.transfer_customer_segments import export_segments
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("verify_store_migration")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

ALL_RESOURCES = [
    "collections", "pages", "blogs", "navigation", "customers",
    "redirects", "discounts", "orders", "policies", "locations",
    "draft_orders", "gift_cards", "b2b", "markets", "files",
    "selling_plans", "customer_segments",
]


def diff_value(s: Any, d: Any) -> bool:
    return (s or None) != (d or None)


def fetch_all_collections_reliable(client: ShopifyClient) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{ collections(first: 100{after_clause}) {{
            edges {{ node {{
                id handle title descriptionHtml sortOrder templateSuffix
                seo {{ title description }}
                image {{ url }}
                productsCount {{ count }}
            }} }}
            pageInfo {{ hasNextPage endCursor }}
        }} }}
        """
    return paginate_connection(client, build_query, ("collections",))


def compare_collections(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    src = fetch_all_collections_reliable(src_client)
    dest = fetch_all_collections_reliable(dest_client)

    src_by_handle = {c["handle"]: c for c in src}
    dest_by_handle = {c["handle"]: c for c in dest}

    missing = sorted(set(src_by_handle) - set(dest_by_handle))
    extra = sorted(set(dest_by_handle) - set(src_by_handle))

    mismatches = []
    for handle, s in src_by_handle.items():
        d = dest_by_handle.get(handle)
        if not d:
            continue

        diff: Dict[str, Any] = {}
        if diff_value(s.get("title"), d.get("title")):
            diff["title"] = (s.get("title"), d.get("title"))
        if diff_value(s.get("sortOrder"), d.get("sortOrder")):
            diff["sort_order"] = (s.get("sortOrder"), d.get("sortOrder"))
        if diff_value(s.get("templateSuffix"), d.get("templateSuffix")):
            diff["template_suffix"] = (s.get("templateSuffix"), d.get("templateSuffix"))

        s_seo, d_seo = s.get("seo") or {}, d.get("seo") or {}
        if diff_value(s_seo.get("title"), d_seo.get("title")):
            diff["seo_title"] = (s_seo.get("title"), d_seo.get("title"))
        if diff_value(s_seo.get("description"), d_seo.get("description")):
            diff["seo_description"] = (s_seo.get("description"), d_seo.get("description"))

        s_count = (s.get("productsCount") or {}).get("count")
        d_count = (d.get("productsCount") or {}).get("count")
        if s_count != d_count:
            diff["product_count"] = (s_count, d_count)

        s_desc, d_desc = s.get("descriptionHtml") or "", d.get("descriptionHtml") or ""
        if s_desc != d_desc:
            diff["description"] = (f"{len(s_desc)} chars", f"{len(d_desc)} chars")

        s_img = (s.get("image") or {}).get("url")
        d_img = (d.get("image") or {}).get("url")
        if bool(s_img) != bool(d_img):
            diff["image_presence"] = (bool(s_img), bool(d_img))

        if diff:
            mismatches.append({"handle": handle, "field_mismatches": diff})

    matched_count = len(set(src_by_handle) & set(dest_by_handle))

    return {
        "resource": "collections",
        "source_count": len(src),
        "dest_count": len(dest),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "matched_urls_identical": matched_count,
        "mismatches": mismatches,
    }


def compare_pages(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    src_by = {p["handle"]: p for p in fetch_all_pages(src_client)}
    dest_by = {p["handle"]: p for p in fetch_all_pages(dest_client)}

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    mismatches = []
    for handle, s in src_by.items():
        d = dest_by.get(handle)
        if not d:
            continue
        diff: Dict[str, Any] = {}
        if diff_value(s.get("title"), d.get("title")):
            diff["title"] = (s.get("title"), d.get("title"))
        if diff_value(s.get("isPublished"), d.get("isPublished")):
            diff["is_published"] = (s.get("isPublished"), d.get("isPublished"))
        if diff_value(s.get("templateSuffix"), d.get("templateSuffix")):
            diff["template_suffix"] = (s.get("templateSuffix"), d.get("templateSuffix"))
        s_body, d_body = s.get("body") or "", d.get("body") or ""
        if s_body != d_body:
            diff["body"] = (f"{len(s_body)} chars", f"{len(d_body)} chars")
        if diff:
            mismatches.append({"handle": handle, "field_mismatches": diff})

    return {
        "resource": "pages",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
    }


def compare_blogs(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    src_blogs = fetch_all_blogs(src_client)
    dest_blogs = fetch_all_blogs(dest_client)

    src_by = {b["handle"]: b for b in src_blogs}
    dest_by = {b["handle"]: b for b in dest_blogs}

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    blog_mismatches = []
    article_missing_by_blog = {}
    article_extra_by_blog = {}
    article_mismatches = []
    total_src_articles = 0
    total_dest_articles = 0

    for handle, s in src_by.items():
        d = dest_by.get(handle)
        if not d:
            continue
        diff: Dict[str, Any] = {}
        if diff_value(s.get("title"), d.get("title")):
            diff["title"] = (s.get("title"), d.get("title"))
        if diff_value(s.get("commentPolicy"), d.get("commentPolicy")):
            diff["comment_policy"] = (s.get("commentPolicy"), d.get("commentPolicy"))
        if diff:
            blog_mismatches.append({"handle": handle, "field_mismatches": diff})

        src_articles = fetch_blog_articles(src_client, s["id"])
        dest_articles = fetch_blog_articles(dest_client, d["id"])
        total_src_articles += len(src_articles)
        total_dest_articles += len(dest_articles)

        src_a_by = {a["handle"]: a for a in src_articles}
        dest_a_by = {a["handle"]: a for a in dest_articles}
        a_missing = sorted(set(src_a_by) - set(dest_a_by))
        a_extra = sorted(set(dest_a_by) - set(src_a_by))
        if a_missing:
            article_missing_by_blog[handle] = a_missing
        if a_extra:
            article_extra_by_blog[handle] = a_extra

        for a_handle, sa in src_a_by.items():
            da = dest_a_by.get(a_handle)
            if not da:
                continue
            adiff: Dict[str, Any] = {}
            if diff_value(sa.get("title"), da.get("title")):
                adiff["title"] = (sa.get("title"), da.get("title"))
            if diff_value(sa.get("isPublished"), da.get("isPublished")):
                adiff["is_published"] = (sa.get("isPublished"), da.get("isPublished"))
            sbody, dbody = sa.get("body") or "", da.get("body") or ""
            if sbody != dbody:
                adiff["body"] = (f"{len(sbody)} chars", f"{len(dbody)} chars")
            if adiff:
                article_mismatches.append({"blog_handle": handle, "article_handle": a_handle, "field_mismatches": adiff})

    return {
        "resource": "blogs",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": blog_mismatches,
        "articles": {
            "source_count": total_src_articles,
            "dest_count": total_dest_articles,
            "missing_by_blog": article_missing_by_blog,
            "extra_by_blog": article_extra_by_blog,
            "mismatches": article_mismatches,
        },
    }


def compare_navigation(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    src_by = {m["handle"]: m for m in fetch_all_menus(src_client)}
    dest_by = {m["handle"]: m for m in fetch_all_menus(dest_client)}

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    mismatches = []
    for handle, s in src_by.items():
        d = dest_by.get(handle)
        if not d:
            continue
        diff: Dict[str, Any] = {}
        if diff_value(s.get("title"), d.get("title")):
            diff["title"] = (s.get("title"), d.get("title"))
        s_items, d_items = s.get("items") or [], d.get("items") or []
        if len(s_items) != len(d_items):
            diff["item_count"] = (len(s_items), len(d_items))
        if diff:
            mismatches.append({"handle": handle, "field_mismatches": diff})

    return {
        "resource": "navigation",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
    }


def compare_customers(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    src_by = {(c.get("email") or "").strip().lower(): c for c in fetch_all_customers(src_client) if c.get("email")}
    dest_by = {(c.get("email") or "").strip().lower(): c for c in fetch_all_customers(dest_client) if c.get("email")}

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    mismatches = []
    for email, s in src_by.items():
        d = dest_by.get(email)
        if not d:
            continue
        diff: Dict[str, Any] = {}
        for field in ("firstName", "lastName", "phone", "taxExempt"):
            if diff_value(s.get(field), d.get(field)):
                diff[field] = (s.get(field), d.get(field))
        if diff:
            mismatches.append({"email": email, "field_mismatches": diff})

    return {
        "resource": "customers",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
    }


def compare_redirects(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    src_by = {r["path"]: r for r in fetch_all_redirects(src_client)}
    dest_by = {r["path"]: r for r in fetch_all_redirects(dest_client)}

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    mismatches = []
    for path, s in src_by.items():
        d = dest_by.get(path)
        if not d:
            continue
        if diff_value(s.get("target"), d.get("target")):
            mismatches.append({"path": path, "field_mismatches": {"target": (s.get("target"), d.get("target"))}})

    return {
        "resource": "redirects",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
    }


def discount_key(node: Dict[str, Any]) -> Optional[str]:
    d = node.get("discount") or {}
    codes = (d.get("codes") or {}).get("nodes") or []
    if codes:
        return f"code:{codes[0]['code']}"
    if d.get("title"):
        return f"title:{d['title']}"
    return None


def compare_discounts(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    src_nodes = [n for n in fetch_discount_nodes(src_client) if discount_key(n)]
    dest_nodes = [n for n in fetch_discount_nodes(dest_client) if discount_key(n)]

    src_by = {discount_key(n): n for n in src_nodes}
    dest_by = {discount_key(n): n for n in dest_nodes}

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    mismatches = []
    for key, s in src_by.items():
        d = dest_by.get(key)
        if not d:
            continue
        sd, dd = s.get("discount") or {}, d.get("discount") or {}
        diff: Dict[str, Any] = {}
        if diff_value(sd.get("status"), dd.get("status")):
            diff["status"] = (sd.get("status"), dd.get("status"))
        if diff_value(sd.get("title"), dd.get("title")):
            diff["title"] = (sd.get("title"), dd.get("title"))
        if diff:
            mismatches.append({"key": key, "field_mismatches": diff})

    return {
        "resource": "discounts",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
        "note": (
            "Source count reflects EVERY discount node on Src, including ~10k+ "
            "expired/disabled codes the pipeline deliberately excludes by default -- a "
            "large missing_on_dest count here is expected, not a bug. See "
            "transfer_discounts.py --include-expired to widen scope if needed."
        ),
    }


def compare_orders(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    src_by = {o["name"]: o for o in fetch_all_orders(src_client)}
    dest_by = {o["name"]: o for o in fetch_all_orders(dest_client)}

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    mismatches = []
    for name, s in src_by.items():
        d = dest_by.get(name)
        if not d:
            continue
        diff: Dict[str, Any] = {}
        if diff_value(s.get("email"), d.get("email")):
            diff["email"] = (s.get("email"), d.get("email"))
        if diff_value(s.get("test"), d.get("test")):
            diff["test"] = (s.get("test"), d.get("test"))
        if diff:
            mismatches.append({"name": name, "field_mismatches": diff})

    return {
        "resource": "orders",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
    }


def compare_policies(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    src_by = {p["type"]: p for p in fetch_shop_policies(src_client) if p.get("body")}
    dest_by = {p["type"]: p for p in fetch_shop_policies(dest_client)}

    missing = sorted(set(src_by) - set(dest_by.keys()) - {t for t in dest_by if not dest_by[t].get("body")})
    mismatches = []
    for ptype, s in src_by.items():
        d = dest_by.get(ptype)
        if not d:
            continue
        s_body, d_body = s.get("body") or "", d.get("body") or ""
        if s_body != d_body:
            mismatches.append({"type": ptype, "field_mismatches": {"body": (f"{len(s_body)} chars", f"{len(d_body)} chars")}})

    return {
        "resource": "policies",
        "source_count": len(src_by),
        "dest_count": len([p for p in dest_by.values() if p.get("body")]),
        "missing_on_dest": missing,
        "extra_on_dest": [],
        "mismatches": mismatches,
    }


def compare_locations(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    try:
        from transfer.transfer_locations import fetch_all_locations
        src_by = {l.get("name"): l for l in fetch_all_locations(src_client)}
        dest_by = {l.get("name"): l for l in fetch_all_locations(dest_client)}
    except Exception as e:
        return {
            "resource": "locations",
            "blocked": True,
            "reason": str(e),
            "note": "read_locations scope not granted on one or both stores as of this writing -- documented, known gap.",
        }

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))
    return {
        "resource": "locations",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": [],
    }


def compare_draft_orders(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    try:
        src_by = {o["name"]: o for o in fetch_all_draft_orders(src_client)}
        dest_by = {o["name"]: o for o in fetch_all_draft_orders(dest_client)}
    except Exception as e:
        return {
            "resource": "draft_orders",
            "blocked": True,
            "reason": str(e),
            "note": "read_draft_orders scope not granted on one or both stores (or another fetch error).",
        }

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    mismatches = []
    for name, s in src_by.items():
        d = dest_by.get(name)
        if not d:
            continue
        diff: Dict[str, Any] = {}
        if diff_value(s.get("email"), d.get("email")):
            diff["email"] = (s.get("email"), d.get("email"))
        if diff_value(s.get("status"), d.get("status")):
            diff["status"] = (s.get("status"), d.get("status"))
        if diff:
            mismatches.append({"name": name, "field_mismatches": diff})

    return {
        "resource": "draft_orders",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
        "note": (
            "transfer_draft_orders.py deliberately excludes COMPLETED (and CANCELLED) source draft "
            "orders from migration -- a COMPLETED draft order already became a real Order on Src, "
            "already verified separately by the 'orders' resource, so it is never re-created as a "
            "draft on dest. Draft orders appearing in missing_on_dest here are therefore expected for "
            "every COMPLETED/CANCELLED source draft order, not a migration bug."
        ),
    }


def compare_gift_cards(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    try:
        src_raw = fetch_all_gift_cards(src_client)
        dest_raw = fetch_all_gift_cards(dest_client)
    except Exception as e:
        return {
            "resource": "gift_cards",
            "blocked": True,
            "reason": str(e),
            "note": "read_gift_cards scope not granted on one or both stores.",
        }

    src_eligible = []
    for gc in src_raw:
        if not gc.get("enabled"):
            continue
        balance = gc.get("balance") or {}
        amount = balance.get("amount")
        try:
            if amount is None or float(amount) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        src_eligible.append(gc)

    def sum_by_currency(cards: List[Dict[str, Any]]) -> Dict[str, str]:
        totals: Dict[str, float] = {}
        for c in cards:
            balance = c.get("balance") or {}
            amount = balance.get("amount")
            currency = balance.get("currencyCode") or "UNKNOWN"
            try:
                totals[currency] = totals.get(currency, 0.0) + float(amount)
            except (TypeError, ValueError):
                continue
        return {cur: f"{amt:.2f}" for cur, amt in totals.items()}

    src_totals = sum_by_currency(src_eligible)
    dest_totals = sum_by_currency(dest_raw)

    marker_path = Path("Results") / "gift_cards_migrated.json"
    marker_count: Optional[int] = None
    if marker_path.exists():
        try:
            with open(marker_path, "r", encoding="utf-8") as f:
                marker_records = json.load(f)
            marker_count = len(marker_records) if isinstance(marker_records, list) else None
        except Exception:
            marker_count = None

    return {
        "resource": "gift_cards",
        "source_count": len(src_eligible),
        "dest_count": len(dest_raw),
        "source_total_balance": src_totals,
        "dest_total_balance": dest_totals,
        "missing_on_dest": [],
        "extra_on_dest": [],
        "mismatches": [],
        "marker_file_entries": marker_count,
        "note": (
            "Gift cards can NEVER be matched 1:1 by identity across stores: Shopify's Admin API never "
            "exposes a gift card's actual redeemable code (only a masked one), and a migrated gift card "
            "is always created with a brand-new, different code on dest. So this is a balance-total "
            "sanity check only, not a per-record diff. source_count/source_total_balance reflect only "
            "ENABLED, non-zero-balance gift cards on Src (the same eligibility filter "
            "transfer_gift_cards.py uses, grouped by currency); dest_count/dest_total_balance reflect ALL "
            "gift cards on dest, since a fresh destination store may already have unrelated ones. "
            "marker_file_entries is the count of entries in Results/gift_cards_migrated.json if that "
            "marker file exists -- it's the authoritative record of what this pipeline has actually "
            "created on dest."
        ),
    }


def compare_b2b(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    try:
        src_by = {c["name"]: c for c in fetch_all_companies(src_client) if c.get("name")}
        dest_by = {c["name"]: c for c in fetch_all_companies(dest_client) if c.get("name")}
    except Exception as e:
        return {
            "resource": "b2b",
            "blocked": True,
            "reason": str(e),
            "note": (
                "read_companies scope not granted on one or both stores, or the B2B schema doesn't "
                "match this store's API version -- see transfer_b2b.py's schema-stability warning."
            ),
        }

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    mismatches = []
    for name, s in src_by.items():
        d = dest_by.get(name)
        if not d:
            continue
        diff: Dict[str, Any] = {}
        if diff_value(s.get("note"), d.get("note")):
            diff["note"] = (s.get("note"), d.get("note"))
        if diff_value(s.get("externalId"), d.get("externalId")):
            diff["externalId"] = (s.get("externalId"), d.get("externalId"))

        s_locations = (s.get("locations") or {}).get("edges") or []
        d_locations = (d.get("locations") or {}).get("edges") or []
        if len(s_locations) != len(d_locations):
            diff["location_count"] = (len(s_locations), len(d_locations))

        s_contacts = (s.get("contacts") or {}).get("edges") or []
        d_contacts = (d.get("contacts") or {}).get("edges") or []
        if len(s_contacts) != len(d_contacts):
            diff["contact_count"] = (len(s_contacts), len(d_contacts))

        if diff:
            mismatches.append({"name": name, "field_mismatches": diff})

    return {
        "resource": "b2b",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
    }


def compare_markets(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    try:
        src_by = {m["name"]: m for m in fetch_all_markets(src_client) if m.get("name")}
        dest_by = {m["name"]: m for m in fetch_all_markets(dest_client) if m.get("name")}
    except Exception as e:
        return {
            "resource": "markets",
            "blocked": True,
            "reason": str(e),
            "note": "read_markets scope not granted on one or both stores.",
        }

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    def region_codes(market: Dict[str, Any]) -> set:
        codes = set()
        for edge in (market.get("regions") or {}).get("edges", []):
            node = edge.get("node") or {}
            code = node.get("code")
            if code:
                codes.add(code)
        return codes

    mismatches = []
    for name, s in src_by.items():
        d = dest_by.get(name)
        if not d:
            continue
        diff: Dict[str, Any] = {}
        if diff_value(s.get("enabled"), d.get("enabled")):
            diff["enabled"] = (s.get("enabled"), d.get("enabled"))

        s_regions = region_codes(s)
        d_regions = region_codes(d)
        if s_regions != d_regions:
            diff["region_country_codes"] = (sorted(s_regions), sorted(d_regions))

        if diff:
            mismatches.append({"name": name, "field_mismatches": diff})

    return {
        "resource": "markets",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
    }


def compare_files(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    try:
        src_by = {f["filename_key"]: f for f in export_files(src_client)}
        dest_by = {f["filename_key"]: f for f in export_files(dest_client)}
    except Exception as e:
        return {
            "resource": "files",
            "blocked": True,
            "reason": str(e),
            "note": "read_files scope not granted on one or both stores.",
        }

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    mismatches = []
    for key, s in src_by.items():
        d = dest_by.get(key)
        if not d:
            continue
        diff: Dict[str, Any] = {}
        if diff_value(s.get("content_type"), d.get("content_type")):
            diff["content_type"] = (s.get("content_type"), d.get("content_type"))
        if diff_value(s.get("alt"), d.get("alt")):
            diff["alt"] = (s.get("alt"), d.get("alt"))
        if diff:
            mismatches.append({"filename_key": key, "field_mismatches": diff})

    return {
        "resource": "files",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
        "note": (
            "Matched by filename_key, the same de-dup heuristic transfer_files.py itself uses: the "
            "last URL path segment (lowercased) with a trailing hash/UUID-like suffix stripped -- see "
            "transfer_files.py's filename_key_from_url/filename_key_from_path. This is a heuristic, not "
            "a true identity check: two genuinely different files that happen to share a filename would "
            "appear matched here rather than as missing/extra."
        ),
    }


def compare_selling_plans(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    try:
        src_groups = export_selling_plan_groups(src_client)
        dest_groups = export_selling_plan_groups(dest_client)
    except Exception as e:
        return {
            "resource": "selling_plans",
            "blocked": True,
            "reason": str(e),
            "note": "read_products/read_selling_plans scope not granted on one or both stores.",
        }

    def group_key(g: Dict[str, Any]):
        return ((g.get("name") or "").strip().lower(), (g.get("merchant_code") or "").strip().lower())

    src_by = {group_key(g): g for g in src_groups}
    dest_by = {group_key(g): g for g in dest_groups}

    missing = sorted(f"{n}|{m}" for n, m in (set(src_by) - set(dest_by)))
    extra = sorted(f"{n}|{m}" for n, m in (set(dest_by) - set(src_by)))

    mismatches = []
    for key, s in src_by.items():
        d = dest_by.get(key)
        if not d:
            continue
        diff: Dict[str, Any] = {}
        if diff_value(s.get("description"), d.get("description")):
            diff["description"] = (s.get("description"), d.get("description"))
        s_plans, d_plans = s.get("selling_plans") or [], d.get("selling_plans") or []
        if len(s_plans) != len(d_plans):
            diff["selling_plan_count"] = (len(s_plans), len(d_plans))
        s_handles, d_handles = set(s.get("product_handles") or []), set(d.get("product_handles") or [])
        if s_handles != d_handles:
            diff["product_handles"] = (sorted(s_handles), sorted(d_handles))
        if diff:
            mismatches.append({"name": key[0], "field_mismatches": diff})

    return {
        "resource": "selling_plans",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
        "note": (
            "Matched by (name, merchantCode) -- the same key transfer_selling_plans.py's own "
            "import_selling_plan_groups uses to dedupe against the destination. This resource covers "
            "selling plan GROUP TEMPLATES only, not individual customers' live subscription contracts "
            "(those aren't portable via the Admin API at all -- see transfer_selling_plans.py's module "
            "docstring)."
        ),
    }


def compare_customer_segments(src_client: ShopifyClient, dest_client: ShopifyClient) -> Dict[str, Any]:
    try:
        src_by = {(s.get("name") or "").strip().lower(): s for s in export_segments(src_client)}
        dest_by = {(s.get("name") or "").strip().lower(): s for s in export_segments(dest_client)}
    except Exception as e:
        return {
            "resource": "customer_segments",
            "blocked": True,
            "reason": str(e),
            "note": "read_customers scope not granted on one or both stores.",
        }

    missing = sorted(set(src_by) - set(dest_by))
    extra = sorted(set(dest_by) - set(src_by))

    mismatches = []
    for key, s in src_by.items():
        d = dest_by.get(key)
        if not d:
            continue
        if diff_value(s.get("query"), d.get("query")):
            mismatches.append({
                "name": s.get("name"),
                "field_mismatches": {"query": (s.get("query"), d.get("query"))},
            })

    return {
        "resource": "customer_segments",
        "source_count": len(src_by),
        "dest_count": len(dest_by),
        "missing_on_dest": missing,
        "extra_on_dest": extra,
        "mismatches": mismatches,
    }


COMPARE_FUNCS = {
    "collections": compare_collections,
    "pages": compare_pages,
    "blogs": compare_blogs,
    "navigation": compare_navigation,
    "customers": compare_customers,
    "redirects": compare_redirects,
    "discounts": compare_discounts,
    "orders": compare_orders,
    "policies": compare_policies,
    "locations": compare_locations,
    "draft_orders": compare_draft_orders,
    "gift_cards": compare_gift_cards,
    "b2b": compare_b2b,
    "markets": compare_markets,
    "files": compare_files,
    "selling_plans": compare_selling_plans,
    "customer_segments": compare_customer_segments,
}


def build_detail_rows(resource: str, result: Dict[str, Any]) -> List[List[str]]:
    rows: List[List[str]] = []
    for h in result.get("missing_on_dest", []):
        rows.append([resource, "missing_on_dest", str(h), "", ""])
    for h in result.get("extra_on_dest", []):
        rows.append([resource, "extra_on_dest", str(h), "", ""])
    for m in result.get("mismatches", []):
        key = m.get("handle") or m.get("email") or m.get("path") or m.get("key") or m.get("name") or m.get("type") or m.get("filename_key") or ""
        for field, (s, d) in m.get("field_mismatches", {}).items():
            rows.append([resource, f"mismatch:{field}", str(key), str(s), str(d)])

    if resource == "blogs":
        articles = result.get("articles", {})
        for blog_handle, handles in articles.get("missing_by_blog", {}).items():
            for h in handles:
                rows.append(["blogs.articles", "missing_on_dest", f"{blog_handle}/{h}", "", ""])
        for blog_handle, handles in articles.get("extra_by_blog", {}).items():
            for h in handles:
                rows.append(["blogs.articles", "extra_on_dest", f"{blog_handle}/{h}", "", ""])
        for m in articles.get("mismatches", []):
            key = f"{m['blog_handle']}/{m['article_handle']}"
            for field, (s, d) in m.get("field_mismatches", {}).items():
                rows.append(["blogs.articles", f"mismatch:{field}", key, str(s), str(d)])

    return rows


def write_reports(results: Dict[str, Dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "store_verification_report.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info("Wrote %s", json_path)

    xlsx_path = out_dir / "store_verification_report.xlsx"
    workbook = xlsxwriter.Workbook(str(xlsx_path))
    bold = workbook.add_format({"bold": True})
    red = workbook.add_format({"bg_color": "#FFC7CE"})
    green = workbook.add_format({"bg_color": "#C6EFCE"})

    overview = workbook.add_worksheet("Overview")
    for col, h in enumerate(["Resource", "Source Count", "Dest Count", "Missing on Dest", "Extra on Dest", "Field Mismatches", "Status"]):
        overview.write(0, col, h, bold)
    for row, (resource, result) in enumerate(results.items(), 1):
        if result.get("blocked"):
            overview.write(row, 0, resource)
            overview.write(row, 1, "N/A")
            overview.write(row, 6, f"BLOCKED: {result.get('reason', '')}", red)
            continue
        missing_ct = len(result.get("missing_on_dest", []))
        extra_ct = len(result.get("extra_on_dest", []))
        mismatch_ct = len(result.get("mismatches", []))
        clean = missing_ct == 0 and extra_ct == 0 and mismatch_ct == 0
        overview.write(row, 0, resource)
        overview.write(row, 1, result.get("source_count", ""))
        overview.write(row, 2, result.get("dest_count", ""))
        overview.write(row, 3, missing_ct)
        overview.write(row, 4, extra_ct)
        overview.write(row, 5, mismatch_ct)
        overview.write(row, 6, "CLEAN" if clean else "ISSUES", green if clean else red)
    overview.set_column(0, 0, 20)
    overview.set_column(6, 6, 40)

    details = workbook.add_worksheet("Details")
    for col, h in enumerate(["Resource", "Category", "Key", "Source Value", "Dest Value"]):
        details.write(0, col, h, bold)
    row = 1
    for resource, result in results.items():
        if result.get("blocked"):
            continue
        for detail_row in build_detail_rows(resource, result):
            for col, val in enumerate(detail_row):
                details.write(row, col, val)
            row += 1
    details.set_column(0, 0, 16)
    details.set_column(1, 1, 22)
    details.set_column(2, 2, 45)
    details.set_column(3, 4, 40)

    workbook.close()
    logger.info("Wrote %s", xlsx_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify non-product resources transferred from Src to dest match")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Verify every resource type")
    group.add_argument("--resource", choices=ALL_RESOURCES, help="Verify just one resource type")
    parser.add_argument("--out", default="Results", help="Output directory for the report files")
    args = parser.parse_args()

    src_shop = os.getenv("SRC_SHOPIFY_SHOP")
    src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")

    require_env(
        SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token,
        DEST_SHOPIFY_SHOP=dest_shop, DEST_SHOPIFY_ACCESS_TOKEN=dest_token,
    )

    src_client = make_client(src_shop, src_token)
    dest_client = make_client(dest_shop, dest_token)

    resources = ALL_RESOURCES if args.all else [args.resource]

    results: Dict[str, Dict[str, Any]] = {}
    for resource in resources:
        logger.info("Verifying %s...", resource)
        try:
            results[resource] = COMPARE_FUNCS[resource](src_client, dest_client)
        except Exception:
            logger.exception("Failed to verify %s", resource)
            results[resource] = {"resource": resource, "blocked": True, "reason": "unexpected error, see logs"}

    write_reports(results, Path(args.out))

    print("\n" + "=" * 70)
    print("STORE MIGRATION VERIFICATION SUMMARY")
    print("=" * 70)
    for resource, result in results.items():
        if result.get("blocked"):
            print(f"{resource:15s}  BLOCKED ({result.get('reason', '')})")
            continue
        missing_ct = len(result.get("missing_on_dest", []))
        extra_ct = len(result.get("extra_on_dest", []))
        mismatch_ct = len(result.get("mismatches", []))
        status = "CLEAN" if (missing_ct == 0 and extra_ct == 0 and mismatch_ct == 0) else "ISSUES"
        print(
            f"{resource:15s}  src={result.get('source_count', '?'):<6} dest={result.get('dest_count', '?'):<6} "
            f"missing={missing_ct:<5} extra={extra_ct:<5} mismatches={mismatch_ct:<5} [{status}]"
        )
        if resource == "collections":
            print(f"                 -> matched collection URLs identical: {result.get('matched_urls_identical')}")
    print("=" * 70)


if __name__ == "__main__":
    main()
