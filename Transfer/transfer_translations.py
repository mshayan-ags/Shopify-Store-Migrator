import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from transfer.transfer_product import make_client, fetch_all_product_handles
from transfer.transfer_collections import fetch_all_collections
from transfer.transfer_pages import fetch_all_pages
from transfer.transfer_blogs import fetch_all_blogs, fetch_blog_articles
from transfer.transfer_store_metafields import (
    retry_with_backoff,
    gql_quote,
    product_gid,
    collection_gid,
)
from utils.shopify_graphql_utils import paginate_connection, mutation_errors
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_translations")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


RESOURCE_TYPE_LABELS = {
    "PRODUCT": "product",
    "COLLECTION": "collection",
    "PAGE": "page",
    "BLOG": "blog",
    "ARTICLE": "article",
    "METAOBJECT": "metaobject",
}

GENERIC_RESOURCE_TYPES = ("PRODUCT", "COLLECTION", "PAGE", "BLOG", "ARTICLE", "METAOBJECT")

NODE_IDENTITY_FRAGMENT = """
    id
    __typename
    ... on Product { handle }
    ... on Collection { handle }
    ... on Page { handle }
    ... on Blog { handle }
    ... on Article { handle blog { handle } }
    ... on Metaobject { handle type }
"""


def fetch_shop_locales(client) -> List[Dict[str, Any]]:
    data = retry_with_backoff(lambda: client.query("{ shopLocales { locale name primary published } }"))
    return data.get("shopLocales") or []


def resolve_target_locales(src_client, dest_client, locale_filter: Optional[List[str]]) -> List[str]:
    src_locales = fetch_shop_locales(src_client)
    dest_locales = fetch_shop_locales(dest_client)

    dest_by_code = {l["locale"]: l for l in dest_locales}
    src_non_primary_codes = {l["locale"] for l in src_locales if not l.get("primary")}
    missing_on_dest = sorted(src_non_primary_codes - set(dest_by_code.keys()))
    if missing_on_dest:
        logger.warning(
            "Source has translated content in locale(s) %s that don't exist on the destination store at all -- "
            "add these languages under Settings > Languages on the destination, then re-run this script to "
            "migrate them.",
            missing_on_dest,
        )

    if locale_filter:
        targets = []
        for code in locale_filter:
            entry = dest_by_code.get(code)
            if not entry:
                logger.warning("--locale '%s' requested but that locale doesn't exist on the destination store -- skipping it", code)
                continue
            if entry.get("primary"):
                logger.warning("--locale '%s' is the destination's primary locale (already covered by the resource transfer scripts) -- skipping it", code)
                continue
            if not entry.get("published"):
                logger.warning("--locale '%s' exists on the destination but isn't published yet -- translating it anyway since it was explicitly requested", code)
            targets.append(code)
        return targets

    return [l["locale"] for l in dest_locales if l.get("published") and not l.get("primary")]


def build_destination_indices(dest_client) -> Dict[str, Any]:
    indices: Dict[str, Any] = {}

    products = fetch_all_product_handles(dest_client)
    indices["PRODUCT"] = {
        (p.get("handle") or "").strip().lower(): product_gid(p["id"]) for p in products if p.get("handle")
    }
    logger.info("Indexed %s destination product(s) by handle", len(indices["PRODUCT"]))

    collections: Dict[str, str] = {}
    for resource in ("custom_collections", "smart_collections"):
        for c in fetch_all_collections(dest_client, resource):
            handle = (c.get("handle") or "").strip().lower()
            if handle:
                collections[handle] = collection_gid(c["id"])
    indices["COLLECTION"] = collections
    logger.info("Indexed %s destination collection(s) by handle", len(collections))

    pages = fetch_all_pages(dest_client)
    indices["PAGE"] = {(p.get("handle") or "").strip().lower(): p["id"] for p in pages if p.get("handle")}
    logger.info("Indexed %s destination page(s) by handle", len(indices["PAGE"]))

    dest_blogs = fetch_all_blogs(dest_client)
    indices["BLOG"] = {(b.get("handle") or "").strip().lower(): b["id"] for b in dest_blogs if b.get("handle")}
    logger.info("Indexed %s destination blog(s) by handle", len(indices["BLOG"]))

    articles: Dict[Tuple[str, str], str] = {}
    for b in dest_blogs:
        blog_handle = (b.get("handle") or "").strip().lower()
        try:
            for a in fetch_blog_articles(dest_client, b["id"]):
                article_handle = (a.get("handle") or "").strip().lower()
                if article_handle:
                    articles[(blog_handle, article_handle)] = a["id"]
        except Exception:
            logger.exception("Failed to index articles for destination blog '%s'", b.get("handle"))
    indices["ARTICLE"] = articles
    logger.info("Indexed %s destination article(s) by (blog handle, article handle)", len(articles))

    return indices


_dest_metaobject_gid_cache: Dict[Tuple[str, str], Optional[str]] = {}


def find_destination_metaobject_gid(dest_client, metaobject_type: str, handle: str) -> Optional[str]:
    cache_key = (metaobject_type, handle)
    if cache_key in _dest_metaobject_gid_cache:
        return _dest_metaobject_gid_cache[cache_key]

    gid: Optional[str] = None
    try:
        data = retry_with_backoff(
            lambda: dest_client.query(
                f'{{ metaobjectByHandle(handle: {{ type: {gql_quote(metaobject_type)}, '
                f'handle: {gql_quote(handle)} }}) {{ id }} }}'
            )
        )
        gid = (data.get("metaobjectByHandle") or {}).get("id")
    except Exception:
        logger.exception("Failed to look up destination metaobject %s/%s", metaobject_type, handle)

    _dest_metaobject_gid_cache[cache_key] = gid
    return gid


def resolve_destination_gid(dest_client, indices: Dict[str, Any], resource_type: str, identity: Dict[str, Any]) -> Optional[str]:
    if resource_type in ("PRODUCT", "COLLECTION", "PAGE", "BLOG"):
        handle = (identity.get("handle") or "").strip().lower()
        return indices[resource_type].get(handle) if handle else None
    if resource_type == "ARTICLE":
        key = ((identity.get("blog_handle") or "").strip().lower(), (identity.get("handle") or "").strip().lower())
        return indices["ARTICLE"].get(key)
    if resource_type == "METAOBJECT":
        metaobject_type = identity.get("metaobject_type")
        handle = identity.get("handle")
        if not metaobject_type or not handle:
            return None
        return find_destination_metaobject_gid(dest_client, metaobject_type, handle)
    return None


def fetch_translatable_resource_ids(client, resource_type: str) -> List[str]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          translatableResources(resourceType: {resource_type}, first: 50{after_clause}) {{
            edges {{ node {{ resourceId }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    nodes = paginate_connection(client, build_query, ("translatableResources",))
    return [n["resourceId"] for n in nodes]


def resolve_source_node_identities(client, gids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not gids:
        return {}

    ids_str = ", ".join(gql_quote(g) for g in gids)
    query = f"{{ nodes(ids: [{ids_str}]) {{ {NODE_IDENTITY_FRAGMENT} }} }}"
    data = retry_with_backoff(lambda: client.query(query))

    identities: Dict[str, Dict[str, Any]] = {}
    for node in data.get("nodes") or []:
        if not node:
            continue
        info: Dict[str, Any] = {"typename": node.get("__typename")}
        if node.get("handle"):
            info["handle"] = node["handle"]
        if node.get("type"):
            info["metaobject_type"] = node["type"]
        blog = node.get("blog")
        if blog and blog.get("handle"):
            info["blog_handle"] = blog["handle"]
        identities[node["id"]] = info

    return identities


def fetch_destination_digests(client, resource_gid: str) -> Dict[str, str]:
    query = f"""
    {{ translatableResource(resourceId: {gql_quote(resource_gid)}) {{
        translatableContent {{ key digest }}
    }} }}
    """
    data = retry_with_backoff(lambda: client.query(query))
    content = ((data.get("translatableResource") or {}).get("translatableContent")) or []
    return {c["key"]: c["digest"] for c in content if c.get("key") and c.get("digest")}


def _locale_alias(locale: str) -> str:
    return "loc_" + "".join(ch if ch.isalnum() else "_" for ch in locale)


def fetch_source_translations(client, resource_gid: str, locales: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    if not locales:
        return {}

    alias_by_locale = {locale: _locale_alias(locale) for locale in locales}
    fields = "\n".join(
        f"{alias}: translations(locale: {gql_quote(locale)}) {{ key value locale outdated }}"
        for locale, alias in alias_by_locale.items()
    )
    query = f"""
    {{ translatableResource(resourceId: {gql_quote(resource_gid)}) {{
        {fields}
    }} }}
    """
    data = retry_with_backoff(lambda: client.query(query))
    node = data.get("translatableResource") or {}

    return {locale: (node.get(alias) or []) for locale, alias in alias_by_locale.items()}


def register_translations(dest_client, resource_gid: str, entries: List[Dict[str, Any]]) -> int:
    if not entries:
        return 0

    translations_literal = (
        "["
        + ", ".join(
            "{ "
            f"locale: {gql_quote(e['locale'])}, "
            f"key: {gql_quote(e['key'])}, "
            f"value: {gql_quote(e['value'])}, "
            f"translatableContentDigest: {gql_quote(e['digest'])}"
            " }"
            for e in entries
        )
        + "]"
    )
    mutation = f"""
    mutation {{
      translationsRegister(resourceId: {gql_quote(resource_gid)}, translations: {translations_literal}) {{
        translations {{ locale key value }}
        userErrors {{ field message }}
      }}
    }}
    """
    try:
        result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    except Exception as e:
        logger.warning("translationsRegister failed for %s: %s", resource_gid, e)
        return 0

    errors = mutation_errors(result, "translationsRegister")
    if errors:
        logger.warning("translationsRegister reported error(s) for %s: %s", resource_gid, errors)

    registered = (result.get("translationsRegister") or {}).get("translations") or []
    return len(registered)


def translate_one_resource(
    src_client,
    dest_client,
    src_gid: str,
    dest_gid: str,
    target_locales: List[str],
    stats: Dict[str, int],
    label: str,
    execute: bool,
    export_records: List[Dict[str, Any]],
) -> None:
    source_translations = fetch_source_translations(src_client, src_gid, target_locales)
    if not any(source_translations.values()):
        stats["no_source_content"] = stats.get("no_source_content", 0) + 1
        return

    dest_digests = fetch_destination_digests(dest_client, dest_gid)
    if not dest_digests:
        logger.warning("Destination %s %s has no translatable content -- skipping", label, dest_gid)
        stats["no_dest_content"] = stats.get("no_dest_content", 0) + 1
        return

    entries: List[Dict[str, Any]] = []
    skipped_keys = 0
    for locale, translations in source_translations.items():
        for t in translations:
            key = t.get("key")
            value = t.get("value")
            if not key or value is None:
                continue
            digest = dest_digests.get(key)
            if not digest:
                skipped_keys += 1
                continue
            entries.append({"locale": locale, "key": key, "value": value, "digest": digest})

    if skipped_keys:
        logger.info(
            "%s %s: %s translated key(s) skipped (not present on destination's current content)",
            label, dest_gid, skipped_keys,
        )

    if not entries:
        stats["no_matching_keys"] = stats.get("no_matching_keys", 0) + 1
        return

    export_records.append(
        {
            "resource_type": label,
            "source_resource_id": src_gid,
            "destination_resource_id": dest_gid,
            "translations": entries,
        }
    )

    if execute:
        registered = register_translations(dest_client, dest_gid, entries)
        stats["registered"] = stats.get("registered", 0) + registered
        if registered:
            stats["resources_updated"] = stats.get("resources_updated", 0) + 1
        logger.info("%s %s: registered %s/%s translation(s)", label, dest_gid, registered, len(entries))
    else:
        stats["planned"] = stats.get("planned", 0) + len(entries)
        stats["resources_planned"] = stats.get("resources_planned", 0) + 1


def transfer_resource_type_translations(
    src_client,
    dest_client,
    resource_type: str,
    target_locales: List[str],
    indices: Dict[str, Any],
    stats: Dict[str, int],
    execute: bool,
    export_records: List[Dict[str, Any]],
) -> None:
    label = RESOURCE_TYPE_LABELS[resource_type]
    resource_ids = fetch_translatable_resource_ids(src_client, resource_type)
    logger.info("Found %s source %s(s) with translatable content", len(resource_ids), label)

    batch_size = 50
    for i in range(0, len(resource_ids), batch_size):
        batch = resource_ids[i : i + batch_size]

        try:
            identities = resolve_source_node_identities(src_client, batch)
        except Exception:
            logger.exception("Failed to resolve source %s identities for a batch -- skipping %s item(s)", label, len(batch))
            stats["failed"] = stats.get("failed", 0) + len(batch)
            continue

        for resource_gid in batch:
            try:
                identity = identities.get(resource_gid)
                if not identity:
                    logger.warning("Could not resolve source %s %s to a handle -- skipping", label, resource_gid)
                    stats["unresolved"] = stats.get("unresolved", 0) + 1
                    continue

                dest_gid = resolve_destination_gid(dest_client, indices, resource_type, identity)
                if not dest_gid:
                    logger.warning(
                        "No destination %s found for source handle '%s' -- run the %s transfer script first",
                        label, identity.get("handle"), label,
                    )
                    stats["unmatched"] = stats.get("unmatched", 0) + 1
                    continue

                translate_one_resource(
                    src_client, dest_client, resource_gid, dest_gid, target_locales, stats, label, execute, export_records
                )
            except Exception:
                logger.exception("Failed to transfer translations for %s %s", label, resource_gid)
                stats["failed"] = stats.get("failed", 0) + 1


def fetch_shop_policies_with_id(client) -> List[Dict[str, Any]]:
    data = retry_with_backoff(lambda: client.query("{ shop { shopPolicies { id type body url } } }"))
    return data["shop"]["shopPolicies"]


def transfer_policy_translations(
    src_client,
    dest_client,
    target_locales: List[str],
    stats: Dict[str, int],
    execute: bool,
    export_records: List[Dict[str, Any]],
) -> None:
    src_policies = [p for p in fetch_shop_policies_with_id(src_client) if p.get("body")]
    dest_policies_by_type = {p["type"]: p for p in fetch_shop_policies_with_id(dest_client)}
    logger.info("Found %s source shop polic(y/ies) with content", len(src_policies))

    for policy in src_policies:
        dest_policy = dest_policies_by_type.get(policy["type"])
        if not dest_policy:
            logger.warning(
                "Destination has no '%s' policy -- run transfer_policies.py --execute first", policy["type"]
            )
            stats["unmatched"] = stats.get("unmatched", 0) + 1
            continue

        try:
            translate_one_resource(
                src_client, dest_client, policy["id"], dest_policy["id"], target_locales, stats, "policy", execute, export_records
            )
        except Exception:
            logger.exception("Failed to transfer translations for policy %s", policy["type"])
            stats["failed"] = stats.get("failed", 0) + 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transfer product/collection/page/blog/article/metaobject/policy translations from Src to dest"
    )
    parser.add_argument("--execute", action="store_true", help="Write translations to the destination store (dry-run otherwise)")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    parser.add_argument(
        "--locale",
        default=None,
        help=(
            "Comma-separated locale codes to limit the run to (e.g. --locale fr,de). "
            "Default: every published, non-primary locale found on the destination store."
        ),
    )
    args = parser.parse_args()

    src_shop = os.getenv("SRC_SHOPIFY_SHOP")
    src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")

    require_env(
        SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token,
        DEST_SHOPIFY_SHOP=dest_shop, DEST_SHOPIFY_ACCESS_TOKEN=dest_token,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    src_client = make_client(src_shop, src_token)
    dest_client = make_client(dest_shop, dest_token)

    locale_filter = [c.strip() for c in args.locale.split(",") if c.strip()] if args.locale else None

    logger.info("Resolving target locale(s) from %s (source) / %s (destination)", src_shop, dest_shop)
    target_locales = resolve_target_locales(src_client, dest_client, locale_filter)
    if not target_locales:
        logger.warning(
            "No destination locale(s) to translate into -- nothing to do. Add languages under "
            "Settings > Languages on the destination store, or check the --locale value."
        )
        return
    logger.info("Translating into locale(s): %s", ", ".join(target_locales))

    logger.info("Indexing destination products/collections/pages/blogs/articles by handle")
    indices = build_destination_indices(dest_client)

    stats: Dict[str, Dict[str, int]] = {}
    export_records: List[Dict[str, Any]] = []

    for resource_type in GENERIC_RESOURCE_TYPES:
        label = RESOURCE_TYPE_LABELS[resource_type]
        resource_stats: Dict[str, int] = {}
        try:
            transfer_resource_type_translations(
                src_client, dest_client, resource_type, target_locales, indices, resource_stats, args.execute, export_records
            )
        except Exception:
            logger.exception("Failed while processing %s translations", label)
        stats[label] = resource_stats
        logger.info("%s translations: %s", label, resource_stats)

    policy_stats: Dict[str, int] = {}
    try:
        transfer_policy_translations(src_client, dest_client, target_locales, policy_stats, args.execute, export_records)
    except Exception:
        logger.exception("Failed while processing shop policy translations")
    stats["policy"] = policy_stats
    logger.info("policy translations: %s", policy_stats)

    ts = int(time.time())
    export_data = {"target_locales": target_locales, "stats": stats, "records": export_records}
    out_file = out_dir / f"translations_export_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2, ensure_ascii=False)
    logger.info("Export complete: %s", out_file)

    if args.xlsx:
        from utils.tabular_io import export_to_xlsx
        export_to_xlsx(export_data, out_dir / f"translations_export_{ts}.xlsx")

    if args.execute:
        logger.info("Translations import complete")
    else:
        logger.info("Dry-run finished. Re-run with --execute to write translations to the destination store")


if __name__ == "__main__":
    main()
