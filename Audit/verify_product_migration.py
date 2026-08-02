"""Verify that products transferred from Src to dest are an exact match.

Read-only comparison tool -- writes nothing to either store. For each product it
re-fetches fresh data from BOTH stores (not the local Results/ export snapshots,
which can go stale) and diffs every field transfer_product.py is supposed to have
copied: core fields, options, SEO, category, requiresSellingPlan, every variant
(price/sku/barcode/weight/inventory policy/cost & customs fields), images
(count/position/alt, optionally byte-identical content), and metafields at the
product, variant, and image level.

Usage:
    # Spot-check one product by source handle or numeric ID
    python verify_product_migration.py --product bmw-e46-side-skirts

    # Verify every product that exists on the source store
    python verify_product_migration.py --all

    # Resume a large run, and byte-compare images too (slower -- downloads
    # every image from both stores)
    python verify_product_migration.py --all --start-at 500 --deep-images

Outputs (under --out, default "Results"):
    product_verification_report.json  -- full machine-readable diff, one entry per product
    product_verification_report.xlsx -- Summary + Details sheets for human review
"""
import argparse
import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import xlsxwriter
from dotenv import load_dotenv

from utils.shopify_client import ShopifyClient
from Transfer.transfer_product import (
    make_client,
    fetch_all_product_handles,
    find_source_product,
    find_existing_destination_product,
    fetch_owner_metafields,
    fetch_variant_inventory_details,
    normalize_tags,
    normalize_options,
)
from utils.concurrency_utils import retry_with_backoff, run_concurrently, DEFAULT_WORKERS

load_dotenv()

logger = logging.getLogger("verify_product_migration")
logging.basicConfig(level=logging.INFO)

CORE_FIELDS = [
    "title", "body_html", "vendor", "product_type", "template_suffix",
    "seo_title", "seo_description", "category_id", "requires_selling_plan",
]

VARIANT_FIELDS = [
    "price", "compare_at_price", "barcode", "weight", "weight_unit", "taxable",
    "requires_shipping", "inventory_policy", "inventory_management",
    "option1", "option2", "option3", "unit_cost",
    "country_code_of_origin", "harmonized_system_code", "province_code_of_origin",
]

# The full set of product-level metafields product_uploader.py._prepare_metafields()
# actually writes (grounded in that file, not guessed). Each is conditional on the
# AI-optimized JSON having that section, so absence on a given source product isn't
# necessarily a bug -- this is coverage visibility, not a pass/fail check. See
# Docs/PRODUCT_VERIFICATION_CHECKLIST.md for the source JSON path each key maps from.
CANONICAL_PRODUCT_METAFIELD_KEYS = [
    ("custom", "installation_video"),
    ("custom", "description"),
    ("custom", "features"),
    ("custom", "feature_1"),
    ("custom", "feature_2"),
    ("custom", "feature_3"),
    ("custom", "feature_4"),
    ("custom", "material"),
    ("custom", "quality_json"),
    ("custom", "fitment_summary"),
    ("custom", "fitment"),
    ("custom", "fitment_json"),
    ("custom", "installation_difficulty"),
    ("custom", "installation_time"),
    ("custom", "installation_reversible"),
    ("custom", "instruction_json"),
    ("custom", "product_differences"),
    ("custom", "faq"),
    ("custom", "includes"),
    ("custom", "ships_business_days"),
    ("custom", "return_policy"),
    ("custom", "customer_support"),
    ("custom", "search_product_boosts"),
    ("custom", "icon_text_heading"),
    ("custom", "icon_with_text1"),
    ("custom", "icon_with_text2"),
    ("custom", "icon_with_text3"),
    ("custom", "icon_with_text4"),
    ("custom", "icon_with_text5"),
    ("custom", "icon_with_text6"),
    ("custom", "manufacturer_warranty_icon_text"),
    ("shopify--discovery--product_recommendation", "related_products_display"),
    ("shopify--discovery--product_recommendation", "related_products"),
]


def fetch_product_snapshot(client: ShopifyClient, product: Dict[str, Any]) -> Dict[str, Any]:
    """Collect the same fields transfer_product.py's export_product does, minus
    downloading images to disk (this tool only needs to diff them, not re-upload)."""
    pid = product["id"]

    images = []
    for img in product.get("images", []):
        metafields = []
        try:
            metafields = fetch_owner_metafields(client, "product_image", img.get("id"))
        except Exception:
            logger.exception("Failed to fetch metafields for image %s", img.get("id"))
        images.append({
            "position": img.get("position"),
            "alt": img.get("alt") or "",
            "src": img.get("src"),
            "metafields": metafields,
        })

    inventory_details = fetch_variant_inventory_details(client, pid)

    variants = []
    for v in product.get("variants", []):
        metafields = []
        try:
            metafields = fetch_owner_metafields(client, "variant", v.get("id"))
        except Exception:
            logger.exception("Failed to fetch metafields for variant %s", v.get("id"))
        details = inventory_details.get(v.get("id"), {})
        variants.append({
            "sku": (v.get("sku") or "").strip(),
            "title": v.get("title"),
            "barcode": v.get("barcode"),
            "price": v.get("price"),
            "compare_at_price": v.get("compare_at_price"),
            "option1": v.get("option1"),
            "option2": v.get("option2"),
            "option3": v.get("option3"),
            "taxable": v.get("taxable"),
            "weight": v.get("weight"),
            "weight_unit": v.get("weight_unit"),
            "requires_shipping": v.get("requires_shipping"),
            "inventory_policy": v.get("inventory_policy"),
            "inventory_management": v.get("inventory_management"),
            "inventory_quantity": v.get("inventory_quantity"),
            "unit_cost": details.get("unit_cost"),
            "country_code_of_origin": details.get("country_code_of_origin"),
            "harmonized_system_code": details.get("harmonized_system_code"),
            "province_code_of_origin": details.get("province_code_of_origin"),
            "metafields": metafields,
        })

    product_metafields = []
    try:
        product_metafields = fetch_owner_metafields(client, "product", pid)
    except Exception:
        logger.exception("Failed to fetch metafields for product %s", pid)

    seo, category_id, requires_selling_plan = None, None, None
    try:
        gql_data = retry_with_backoff(
            lambda: client.query(
                f"""
                {{ product(id: "gid://shopify/Product/{pid}") {{
                  seo {{ title description }}
                  category {{ id }}
                  requiresSellingPlan
                }} }}
                """
            )
        )
        gql_product = gql_data.get("product") or {}
        seo = gql_product.get("seo")
        category_id = (gql_product.get("category") or {}).get("id")
        requires_selling_plan = gql_product.get("requiresSellingPlan")
    except Exception:
        logger.exception("Failed to fetch GraphQL-only fields for product %s", pid)

    return {
        "id": pid,
        "handle": product.get("handle"),
        "title": product.get("title"),
        "body_html": product.get("body_html"),
        "vendor": product.get("vendor"),
        "product_type": product.get("product_type"),
        "tags": product.get("tags"),
        "status": product.get("status"),
        "template_suffix": product.get("template_suffix"),
        "seo_title": (seo or {}).get("title"),
        "seo_description": (seo or {}).get("description"),
        "category_id": category_id,
        "requires_selling_plan": requires_selling_plan,
        "options": [{"name": o.get("name"), "values": o.get("values")} for o in product.get("options", [])],
        "images": images,
        "variants": variants,
        "metafields": product_metafields,
    }


def metafield_key(mf: Dict[str, Any]) -> Tuple[str, str]:
    return (mf.get("namespace"), mf.get("key"))


# *_reference metafield values are Shopify GIDs, which are store-specific by
# construction -- a correctly-migrated reference metafield MUST have a different raw
# value on the destination (it points at the destination's copy of that resource).
# Comparing raw GID strings would flag every one of these as a false-positive mismatch,
# so instead they're resolved to a cross-store-comparable identifier (handle/sku/url)
# and compared on that.
REFERENCE_METAFIELD_TYPES = {
    "product_reference", "list.product_reference",
    "collection_reference", "list.collection_reference",
    "variant_reference", "list.variant_reference",
    "page_reference", "list.page_reference",
    "metaobject_reference", "list.metaobject_reference",
    "file_reference", "list.file_reference",
}


def parse_gid_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    value = value.strip()
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            return [v for v in parsed if isinstance(v, str) and v.startswith("gid://")]
        except Exception:
            return []
    if value.startswith("gid://"):
        return [value]
    return []


def normalize_file_identifier(url: str) -> str:
    """A re-uploaded file's URL will never match the source's byte-for-byte -- it
    lives under a different shop's CDN path, and Shopify appends a fresh `?v=`
    cache-busting param on every upload. The filename itself is what's actually
    comparable across stores."""
    return url.split("?", 1)[0].rsplit("/", 1)[-1]


def resolve_gid_identifiers(client: ShopifyClient, gids: List[str]) -> Dict[str, str]:
    """Resolve a batch of GIDs (any of Product/Variant/Collection/Page/Metaobject/File)
    to a stable identifier that's meaningful across different stores."""
    if not gids:
        return {}
    ids_str = ", ".join(f'"{g}"' for g in gids)
    query = f"""
    {{ nodes(ids: [{ids_str}]) {{
        id
        ... on Product {{ handle }}
        ... on ProductVariant {{ sku }}
        ... on Collection {{ handle }}
        ... on Page {{ handle }}
        ... on Metaobject {{ handle }}
        ... on MediaImage {{ image {{ url }} }}
        ... on GenericFile {{ url }}
    }} }}
    """
    try:
        data = retry_with_backoff(lambda: client.query(query))
    except Exception:
        logger.exception("Failed to resolve reference metafield GIDs")
        return {}

    resolved: Dict[str, str] = {}
    for node in data.get("nodes") or []:
        if not node:
            continue
        url = node.get("url") or (node.get("image") or {}).get("url")
        ident = node.get("handle") or node.get("sku") or (normalize_file_identifier(url) if url else None)
        resolved[node.get("id")] = ident or node.get("id")
    return resolved


def resolve_reference_changes(
    candidates: List[Dict[str, Any]], src_client: ShopifyClient, dest_client: ShopifyClient
) -> List[Dict[str, Any]]:
    """Re-check raw-value mismatches on reference-type metafields by resolved identifier
    instead of raw GID. Returns only the ones that are still genuinely different."""
    still_changed = []
    for entry in candidates:
        src_gids = parse_gid_list(entry["source_value"])
        dest_gids = parse_gid_list(entry["dest_value"])
        src_idents = set(resolve_gid_identifiers(src_client, src_gids).values())
        dest_idents = set(resolve_gid_identifiers(dest_client, dest_gids).values())
        if not src_idents or src_idents != dest_idents:
            entry["note"] = "reference metafield -- raw GIDs differ across stores by design; compared resolved handle/sku/url instead"
            entry["source_resolved"] = sorted(src_idents)
            entry["dest_resolved"] = sorted(dest_idents)
            still_changed.append(entry)
    return still_changed


def diff_metafields(
    src_mfs: List[Dict[str, Any]],
    dest_mfs: List[Dict[str, Any]],
    src_client: Optional[ShopifyClient] = None,
    dest_client: Optional[ShopifyClient] = None,
) -> Dict[str, Any]:
    """namespace+key identifies a metafield across stores (ids never match across shops)."""
    src_by_key = {metafield_key(m): m for m in src_mfs}
    dest_by_key = {metafield_key(m): m for m in dest_mfs}

    missing, changed, reference_candidates = [], [], []
    for key, src_mf in src_by_key.items():
        dest_mf = dest_by_key.get(key)
        if dest_mf is None:
            missing.append({"namespace": key[0], "key": key[1], "type": src_mf.get("type"), "value": src_mf.get("value")})
        elif (src_mf.get("value") or "") != (dest_mf.get("value") or "") or src_mf.get("type") != dest_mf.get("type"):
            entry = {
                "namespace": key[0], "key": key[1],
                "source_type": src_mf.get("type"), "dest_type": dest_mf.get("type"),
                "source_value": src_mf.get("value"), "dest_value": dest_mf.get("value"),
            }
            if src_mf.get("type") in REFERENCE_METAFIELD_TYPES and src_client is not None and dest_client is not None:
                reference_candidates.append(entry)
            else:
                changed.append(entry)

    if reference_candidates:
        changed.extend(resolve_reference_changes(reference_candidates, src_client, dest_client))

    extra = [
        {"namespace": key[0], "key": key[1], "type": dest_mf.get("type"), "value": dest_mf.get("value")}
        for key, dest_mf in dest_by_key.items() if key not in src_by_key
    ]

    return {"missing": missing, "extra": extra, "changed": changed}


def metafield_diff_is_clean(diff: Dict[str, Any]) -> bool:
    return not diff["missing"] and not diff["extra"] and not diff["changed"]


def diff_core_fields(src: Dict[str, Any], dest: Dict[str, Any]) -> Dict[str, Tuple[Any, Any]]:
    mismatches: Dict[str, Tuple[Any, Any]] = {}
    for field in CORE_FIELDS:
        s, d = src.get(field), dest.get(field)
        if (s or None) != (d or None):
            mismatches[field] = (s, d)

    if normalize_tags(src.get("tags")) != normalize_tags(dest.get("tags")):
        mismatches["tags"] = (src.get("tags"), dest.get("tags"))

    src_status = src.get("status") or "draft"
    dest_status = dest.get("status") or "draft"
    if src_status != dest_status:
        mismatches["status"] = (src_status, dest_status)

    src_options = normalize_options(src.get("options"))
    dest_options = normalize_options(dest.get("options"))
    if src_options != dest_options:
        mismatches["options"] = (src_options, dest_options)

    return mismatches


def hash_image(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return hashlib.sha256(resp.content).hexdigest()
    except Exception:
        logger.exception("Failed to download image for hashing: %s", url)
        return None


def diff_images(
    src_images: List[Dict[str, Any]], dest_images: List[Dict[str, Any]], deep: bool,
    src_client: ShopifyClient, dest_client: ShopifyClient,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"count_mismatch": None, "position_mismatches": []}
    if len(src_images) != len(dest_images):
        result["count_mismatch"] = {"source": len(src_images), "dest": len(dest_images)}

    src_sorted = sorted(src_images, key=lambda i: i.get("position") or 0)
    dest_sorted = sorted(dest_images, key=lambda i: i.get("position") or 0)

    for idx, (si, di) in enumerate(zip(src_sorted, dest_sorted)):
        issues: Dict[str, Any] = {}
        if si.get("position") != di.get("position"):
            issues["position"] = (si.get("position"), di.get("position"))
        if (si.get("alt") or "") != (di.get("alt") or ""):
            issues["alt"] = (si.get("alt"), di.get("alt"))
        if deep:
            src_hash, dest_hash = hash_image(si.get("src")), hash_image(di.get("src"))
            if src_hash and dest_hash and src_hash != dest_hash:
                issues["content_hash"] = (src_hash, dest_hash)
        mf_diff = diff_metafields(si.get("metafields", []), di.get("metafields", []), src_client, dest_client)
        if not metafield_diff_is_clean(mf_diff):
            issues["metafields"] = mf_diff
        if issues:
            result["position_mismatches"].append({"index": idx, **issues})

    return result


def match_variants(
    src_variants: List[Dict[str, Any]], dest_variants: List[Dict[str, Any]]
) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    dest_by_sku = {(v.get("sku") or "").strip().lower(): v for v in dest_variants if v.get("sku")}
    dest_by_opts = {(v.get("option1"), v.get("option2"), v.get("option3")): v for v in dest_variants}

    pairs, unmatched_src = [], []
    matched_dest_ids = set()
    for sv in src_variants:
        sku_key = (sv.get("sku") or "").strip().lower()
        dv = dest_by_sku.get(sku_key) if sku_key else None
        if dv is None:
            dv = dest_by_opts.get((sv.get("option1"), sv.get("option2"), sv.get("option3")))
        if dv is None:
            unmatched_src.append(sv)
        else:
            pairs.append((sv, dv))
            matched_dest_ids.add(id(dv))

    unmatched_dest = [v for v in dest_variants if id(v) not in matched_dest_ids]
    return pairs, unmatched_src, unmatched_dest


def verify_one(src_client: ShopifyClient, dest_client: ShopifyClient, identifier: str, deep_images: bool) -> Dict[str, Any]:
    src_product = find_source_product(src_client, identifier)
    handle = src_product.get("handle")
    src_snapshot = fetch_product_snapshot(src_client, src_product)

    src_keys = [metafield_key(m) for m in src_snapshot["metafields"]]

    dest_product = find_existing_destination_product(dest_client, handle)
    if not dest_product:
        return {
            "handle": handle,
            "source_id": src_product.get("id"),
            "dest_id": None,
            "status": "MISSING_IN_DESTINATION",
            "core_field_mismatches": {},
            "product_metafield_diff": diff_metafields(src_snapshot["metafields"], []),
            "product_metafield_keys_source": src_keys,
            "product_metafield_keys_dest": [],
            "images": {"count_mismatch": {"source": len(src_snapshot["images"]), "dest": 0}, "position_mismatches": []},
            "variants": {
                "mismatched": [],
                "unmatched_source": [v.get("sku") or v.get("title") for v in src_snapshot["variants"]],
                "unmatched_dest": [],
            },
        }

    dest_snapshot = fetch_product_snapshot(dest_client, dest_product)

    core_mismatches = diff_core_fields(src_snapshot, dest_snapshot)
    product_mf_diff = diff_metafields(src_snapshot["metafields"], dest_snapshot["metafields"], src_client, dest_client)
    images_diff = diff_images(src_snapshot["images"], dest_snapshot["images"], deep_images, src_client, dest_client)

    pairs, unmatched_src, unmatched_dest = match_variants(src_snapshot["variants"], dest_snapshot["variants"])
    variant_reports = []
    for sv, dv in pairs:
        field_diff = {}
        for f in VARIANT_FIELDS:
            sval, dval = sv.get(f), dv.get(f)
            if (sval if sval not in (None, "") else None) != (dval if dval not in (None, "") else None):
                field_diff[f] = (sval, dval)
        inv_diff = None
        if sv.get("inventory_quantity") != dv.get("inventory_quantity"):
            inv_diff = (sv.get("inventory_quantity"), dv.get("inventory_quantity"))
        mf_diff = diff_metafields(sv.get("metafields", []), dv.get("metafields", []), src_client, dest_client)
        if field_diff or inv_diff or not metafield_diff_is_clean(mf_diff):
            variant_reports.append({
                "sku": sv.get("sku") or sv.get("title"),
                "field_mismatches": field_diff,
                # Known pipeline limitation: inventory only syncs to destination's first
                # location, so this is reported but doesn't count toward pass/fail.
                "inventory_quantity_note": inv_diff,
                "metafield_diff": mf_diff,
            })

    is_clean = (
        not core_mismatches
        and metafield_diff_is_clean(product_mf_diff)
        and not images_diff["count_mismatch"] and not images_diff["position_mismatches"]
        and not any(vr["field_mismatches"] or not metafield_diff_is_clean(vr["metafield_diff"]) for vr in variant_reports)
        and not unmatched_src and not unmatched_dest
    )

    return {
        "handle": handle,
        "source_id": src_product.get("id"),
        "dest_id": dest_product.get("id"),
        "status": "MATCH" if is_clean else "MISMATCH",
        "core_field_mismatches": {k: list(v) for k, v in core_mismatches.items()},
        "product_metafield_diff": product_mf_diff,
        "product_metafield_keys_source": src_keys,
        "product_metafield_keys_dest": [metafield_key(m) for m in dest_snapshot["metafields"]],
        "images": images_diff,
        "variants": {
            "mismatched": variant_reports,
            "unmatched_source": [v.get("sku") or v.get("title") for v in unmatched_src],
            "unmatched_dest": [v.get("sku") or v.get("title") for v in unmatched_dest],
        },
    }


def build_detail_rows(report: Dict[str, Any]) -> List[List[str]]:
    handle = report["handle"]
    rows: List[List[str]] = []

    for field, (s, d) in report.get("core_field_mismatches", {}).items():
        rows.append([handle, "core_field", field, str(s), str(d)])

    for mf in report.get("product_metafield_diff", {}).get("missing", []):
        rows.append([handle, "product_metafield_missing", f"{mf['namespace']}.{mf['key']}", str(mf.get("value")), ""])
    for mf in report.get("product_metafield_diff", {}).get("extra", []):
        rows.append([handle, "product_metafield_extra", f"{mf['namespace']}.{mf['key']}", "", str(mf.get("value"))])
    for mf in report.get("product_metafield_diff", {}).get("changed", []):
        rows.append([handle, "product_metafield_changed", f"{mf['namespace']}.{mf['key']}", str(mf.get("source_value")), str(mf.get("dest_value"))])

    images = report.get("images", {})
    if images.get("count_mismatch"):
        cm = images["count_mismatch"]
        rows.append([handle, "image_count", "count", str(cm["source"]), str(cm["dest"])])
    for pm in images.get("position_mismatches", []):
        idx = pm["index"]
        for k in ("position", "alt", "content_hash"):
            if k in pm:
                s, d = pm[k]
                rows.append([handle, f"image[{idx}]_{k}", k, str(s), str(d)])
        if "metafields" in pm:
            for mf in pm["metafields"].get("missing", []):
                rows.append([handle, f"image[{idx}]_metafield_missing", f"{mf['namespace']}.{mf['key']}", str(mf.get("value")), ""])
            for mf in pm["metafields"].get("changed", []):
                rows.append([handle, f"image[{idx}]_metafield_changed", f"{mf['namespace']}.{mf['key']}", str(mf.get("source_value")), str(mf.get("dest_value"))])

    variants = report.get("variants", {})
    for vr in variants.get("mismatched", []):
        sku = vr["sku"]
        for field, (s, d) in vr.get("field_mismatches", {}).items():
            rows.append([handle, f"variant[{sku}]_field", field, str(s), str(d)])
        mfd = vr.get("metafield_diff", {})
        for mf in mfd.get("missing", []):
            rows.append([handle, f"variant[{sku}]_metafield_missing", f"{mf['namespace']}.{mf['key']}", str(mf.get("value")), ""])
        for mf in mfd.get("changed", []):
            rows.append([handle, f"variant[{sku}]_metafield_changed", f"{mf['namespace']}.{mf['key']}", str(mf.get("source_value")), str(mf.get("dest_value"))])
    for sku in variants.get("unmatched_source", []):
        rows.append([handle, "variant_missing_in_dest", str(sku), "", ""])
    for sku in variants.get("unmatched_dest", []):
        rows.append([handle, "variant_extra_in_dest", str(sku), "", ""])

    return rows


def write_reports(reports: List[Dict[str, Any]], extra_in_dest: List[str], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "product_verification_report.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"products": reports, "extra_products_in_destination": extra_in_dest}, f, indent=2)
    logger.info("Wrote %s", json_path)

    xlsx_path = out_dir / "product_verification_report.xlsx"
    workbook = xlsxwriter.Workbook(str(xlsx_path))
    bold = workbook.add_format({"bold": True})
    red = workbook.add_format({"bg_color": "#FFC7CE"})
    green = workbook.add_format({"bg_color": "#C6EFCE"})

    summary = workbook.add_worksheet("Summary")
    headers = ["Handle", "Source ID", "Dest ID", "Status", "Core Mismatches", "Product Metafield Issues", "Image Issues", "Variant Issues"]
    for col, h in enumerate(headers):
        summary.write(0, col, h, bold)

    for row, report in enumerate(reports, 1):
        pmf = report.get("product_metafield_diff", {})
        pmf_count = len(pmf.get("missing", [])) + len(pmf.get("extra", [])) + len(pmf.get("changed", []))
        img = report.get("images", {})
        img_count = (1 if img.get("count_mismatch") else 0) + len(img.get("position_mismatches", []))
        variants = report.get("variants", {})
        variant_count = len(variants.get("mismatched", [])) + len(variants.get("unmatched_source", [])) + len(variants.get("unmatched_dest", []))
        status = report["status"]
        fmt = green if status == "MATCH" else red

        summary.write(row, 0, report["handle"])
        summary.write(row, 1, report.get("source_id") or "")
        summary.write(row, 2, report.get("dest_id") or "")
        summary.write(row, 3, status, fmt)
        summary.write(row, 4, len(report.get("core_field_mismatches", {})))
        summary.write(row, 5, pmf_count)
        summary.write(row, 6, img_count)
        summary.write(row, 7, variant_count)

    summary.set_column(0, 0, 40)
    summary.set_column(3, 3, 22)

    details = workbook.add_worksheet("Details")
    for col, h in enumerate(["Handle", "Category", "Field/Key", "Source Value", "Dest Value"]):
        details.write(0, col, h, bold)
    row = 1
    for report in reports:
        for detail_row in build_detail_rows(report):
            for col, val in enumerate(detail_row):
                details.write(row, col, val)
            row += 1
    details.set_column(0, 0, 40)
    details.set_column(2, 2, 30)
    details.set_column(3, 4, 40)

    if extra_in_dest:
        extras = workbook.add_worksheet("Extra In Destination")
        extras.write(0, 0, "Handle (exists in destination, not found in source)", bold)
        for row, handle in enumerate(extra_in_dest, 1):
            extras.write(row, 0, handle)

    coverage = workbook.add_worksheet("Canonical Metafield Coverage")
    for col, h in enumerate(["Namespace", "Key", "On Source", "On Destination", "Gap (Source has it, Dest doesn't)"]):
        coverage.write(0, col, h, bold)
    for row, (ns, key) in enumerate(CANONICAL_PRODUCT_METAFIELD_KEYS, 1):
        on_source = sum(1 for r in reports if [ns, key] in r.get("product_metafield_keys_source", []) or (ns, key) in r.get("product_metafield_keys_source", []))
        on_dest = sum(1 for r in reports if [ns, key] in r.get("product_metafield_keys_dest", []) or (ns, key) in r.get("product_metafield_keys_dest", []))
        gap = on_source - on_dest
        coverage.write(row, 0, ns)
        coverage.write(row, 1, key)
        coverage.write(row, 2, on_source)
        coverage.write(row, 3, on_dest)
        coverage.write(row, 4, gap, red if gap > 0 else green)
    coverage.set_column(0, 0, 45)
    coverage.set_column(1, 1, 32)
    coverage.write(len(CANONICAL_PRODUCT_METAFIELD_KEYS) + 2, 0, f"Products checked: {len(reports)}", bold)
    coverage.write(
        len(CANONICAL_PRODUCT_METAFIELD_KEYS) + 3, 0,
        "'On Source'/'On Destination' counts are informational -- most keys are conditional on the AI-optimized "
        "JSON having that section, so a low count isn't necessarily a bug. A non-zero Gap means the key IS on the "
        "source product but didn't make it to destination -- that's a real transfer miss.",
    )

    workbook.close()
    logger.info("Wrote %s", xlsx_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify products transferred from Src to dest match exactly")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--product", help="Source product ID or handle to verify")
    group.add_argument("--all", action="store_true", help="Verify every product in the source store")
    parser.add_argument("--out", default="Results", help="Output directory for the report files")
    parser.add_argument("--limit", type=int, default=None, help="With --all, only verify the first N products")
    parser.add_argument("--start-at", type=int, default=0, help="With --all, skip the first N products and resume from there (0-indexed)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="With --all, number of products to verify concurrently")
    parser.add_argument("--deep-images", action="store_true", help="Also byte-compare image content (downloads every image from both stores -- slow)")
    args = parser.parse_args()

    src_shop = os.getenv("SRC_SHOPIFY_SHOP")
    src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")

    if not all([src_shop, src_token, dest_shop, dest_token]):
        raise RuntimeError(
            "Missing .env values: SRC_SHOPIFY_SHOP, SRC_SHOPIFY_ACCESS_TOKEN, DEST_SHOPIFY_SHOP, DEST_SHOPIFY_ACCESS_TOKEN"
        )

    src_client = make_client(src_shop, src_token)
    dest_client = make_client(dest_shop, dest_token)
    out_dir = Path(args.out)

    if args.product:
        report = verify_one(src_client, dest_client, args.product, args.deep_images)
        write_reports([report], [], out_dir)
        print(json.dumps(report, indent=2))
        return

    entries = fetch_all_product_handles(src_client)
    total_found = len(entries)
    if args.start_at:
        entries = entries[args.start_at:]
    if args.limit:
        entries = entries[: args.limit]

    logger.info(
        "Verifying %s product(s) from %s against %s (starting at #%s of %s found)",
        len(entries), src_shop, dest_shop, args.start_at + 1, total_found,
    )

    reports: List[Dict[str, Any]] = []
    reports_lock = threading.Lock()
    progress_lock = threading.Lock()
    completed = 0

    def process(entry: Dict[str, Any]) -> None:
        nonlocal completed
        identifier = entry.get("handle") or str(entry.get("id"))
        with progress_lock:
            completed += 1
            position = completed
        logger.info("[%s/%s] %s", position, len(entries), identifier)
        try:
            report = verify_one(src_client, dest_client, identifier, args.deep_images)
            with reports_lock:
                reports.append(report)
        except Exception:
            logger.exception("Failed to verify product %s", identifier)
            with reports_lock:
                reports.append({
                    "handle": identifier,
                    "source_id": entry.get("id"),
                    "dest_id": None,
                    "status": "ERROR",
                    "core_field_mismatches": {},
                    "product_metafield_diff": {"missing": [], "extra": [], "changed": []},
                    "images": {"count_mismatch": None, "position_mismatches": []},
                    "variants": {"mismatched": [], "unmatched_source": [], "unmatched_dest": []},
                })

    run_concurrently(entries, process, max_workers=args.workers, label="product")

    extra_in_dest: List[str] = []
    if not args.limit and not args.start_at:
        src_handles = {(e.get("handle") or "").strip().lower() for e in entries}
        dest_entries = fetch_all_product_handles(dest_client)
        extra_in_dest = [
            e.get("handle") for e in dest_entries
            if (e.get("handle") or "").strip().lower() not in src_handles
        ]

    write_reports(reports, extra_in_dest, out_dir)

    matched = sum(1 for r in reports if r["status"] == "MATCH")
    mismatched = sum(1 for r in reports if r["status"] == "MISMATCH")
    missing = sum(1 for r in reports if r["status"] == "MISSING_IN_DESTINATION")
    errored = sum(1 for r in reports if r["status"] == "ERROR")

    print("\n" + "=" * 70)
    print("PRODUCT VERIFICATION SUMMARY")
    print("=" * 70)
    print(f"Total checked:            {len(reports)}")
    print(f"Exact match:               {matched}")
    print(f"Mismatch (see Details):    {mismatched}")
    print(f"Missing in destination:    {missing}")
    print(f"Errored during check:      {errored}")
    if extra_in_dest:
        print(f"Extra products in dest:    {len(extra_in_dest)} (see 'Extra In Destination' sheet)")
    print("=" * 70)


if __name__ == "__main__":
    main()
