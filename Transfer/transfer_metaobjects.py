"""Transfer Metaobject definitions and entries from Src to dest.

Metaobjects are Shopify's custom structured-content records (used by apps like
Checkout Blocks, or merchant-defined content types) -- a different resource
from Metafields, with their own definitions and entries.

Requires the `read_metaobject_definitions`/`write_metaobject_definitions` and
`read_metaobjects`/`write_metaobjects` Admin API scopes on both stores' custom
apps. Grant them under Shopify Admin > Apps > [app name] > Configuration, then
reinstall to refresh the .env token.

Run this BEFORE transfer_store_metafields.py: some metafield definitions
reference a metaobject definition by GID in their validations (e.g. a
`metaobject_reference` field), and that GID has to be remapped to the matching
destination definition (matched by the definition's `type`, which is the
stable identifier shared across stores) before those metafield definitions
can be created correctly.

Entries are synced with metaobjectUpsert (matched by handle), so re-running
this script keeps the destination an exact copy of source entries.

Usage:
    python transfer_metaobjects.py                # dry-run export
    python transfer_metaobjects.py --execute       # create/update on dest
"""
import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from Transfer.transfer_product import make_client
from Transfer.transfer_store_metafields import retry_with_backoff, gql_quote
from utils.shopify_graphql_utils import paginate_connection, mutation_errors

load_dotenv()

logger = logging.getLogger("transfer_metaobjects")
logging.basicConfig(level=logging.INFO)


def fetch_metaobject_definitions(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          metaobjectDefinitions(first: 50{after_clause}) {{
            edges {{
              node {{
                id
                type
                name
                description
                displayNameKey
                fieldDefinitions {{
                  key
                  name
                  description
                  required
                  type {{ name }}
                  validations {{ name value }}
                }}
              }}
            }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("metaobjectDefinitions",))


def fetch_metaobjects(client, definition_type: str) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          metaobjects(type: {gql_quote(definition_type)}, first: 100{after_clause}) {{
            edges {{
              node {{
                id
                handle
                fields {{ key value type }}
              }}
            }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("metaobjects",))


def export_metaobjects(src_client) -> Dict[str, Any]:
    definitions = fetch_metaobject_definitions(src_client)
    exported_definitions = []
    exported_entries = []

    for d in definitions:
        exported_definitions.append(
            {
                "id": d["id"],
                "type": d["type"],
                "name": d["name"],
                "description": d.get("description"),
                "display_name_key": d.get("displayNameKey"),
                "field_definitions": [
                    {
                        "key": fd["key"],
                        "name": fd["name"],
                        "description": fd.get("description"),
                        "required": fd.get("required", False),
                        "type": fd["type"]["name"],
                        "validations": [{"name": v["name"], "value": v["value"]} for v in fd.get("validations", [])],
                    }
                    for fd in d.get("fieldDefinitions", [])
                ],
            }
        )

        entries = fetch_metaobjects(src_client, d["type"])
        for e in entries:
            exported_entries.append(
                {
                    "definition_type": d["type"],
                    "handle": e["handle"],
                    "fields": [{"key": f["key"], "value": f["value"]} for f in e.get("fields", [])],
                }
            )
        logger.info("Exported metaobject definition '%s' (%s) with %s entrie(s)", d["type"], d["name"], len(entries))

    return {"definitions": exported_definitions, "entries": exported_entries}


def import_metaobject_definitions(dest_client, exported_definitions: List[Dict[str, Any]]) -> Dict[str, str]:
    """Create any missing definitions on destination. Returns source-GID -> dest-GID map."""
    existing = {d["type"]: d["id"] for d in fetch_metaobject_definitions(dest_client)}
    gid_map: Dict[str, str] = {}

    created = 0
    already_existed = 0
    failed = 0

    for definition in exported_definitions:
        if definition["type"] in existing:
            gid_map[definition["id"]] = existing[definition["type"]]
            already_existed += 1
            continue

        # Types prefixed "shopify--" are Shopify's own standard metaobject
        # definitions (fixed schema, e.g. shopify--color-pattern). They can't be
        # created via metaobjectDefinitionCreate ("reserved for use by another
        # application") -- they're turned on per-store via a dedicated mutation
        # that provisions Shopify's predefined field set itself.
        if definition["type"].startswith("shopify--"):
            mutation = f"""
            mutation {{
              standardMetaobjectDefinitionEnable(type: {gql_quote(definition["type"])}) {{
                metaobjectDefinition {{ id type }}
                userErrors {{ field message }}
              }}
            }}
            """
            mutation_name = "standardMetaobjectDefinitionEnable"
        else:
            field_defs_literal = (
                "["
                + ", ".join(
                    "{"
                    + f'key: {gql_quote(fd["key"])}, '
                    + f'name: {gql_quote(fd["name"])}, '
                    + f'description: {gql_quote(fd.get("description"))}, '
                    + f'type: {gql_quote(fd["type"])}, '
                    + f'required: {"true" if fd.get("required") else "false"}'
                    + "}"
                    for fd in definition["field_definitions"]
                )
                + "]"
            )

            mutation = f"""
            mutation {{
              metaobjectDefinitionCreate(definition: {{
                type: {gql_quote(definition["type"])}
                name: {gql_quote(definition["name"])}
                description: {gql_quote(definition.get("description"))}
                fieldDefinitions: {field_defs_literal}
              }}) {{
                metaobjectDefinition {{ id type }}
                userErrors {{ field message }}
              }}
            }}
            """
            mutation_name = "metaobjectDefinitionCreate"

        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        except Exception as e:
            # A hard GraphQL error (e.g. a namespace/type reserved by another app)
            # raises here instead of populating userErrors -- skip just this
            # definition rather than losing every remaining one.
            logger.warning("Failed to create metaobject definition '%s': %s", definition["type"], e)
            failed += 1
            continue

        errors = mutation_errors(result, mutation_name)
        if errors:
            logger.warning("Failed to create metaobject definition '%s': %s", definition["type"], errors)
            failed += 1
            continue

        dest_id = result[mutation_name]["metaobjectDefinition"]["id"]
        gid_map[definition["id"]] = dest_id
        existing[definition["type"]] = dest_id
        logger.info("Created metaobject definition '%s'", definition["type"])
        created += 1

    logger.info(
        "Metaobject definitions import complete: %s created, %s already existed, %s failed",
        created,
        already_existed,
        failed,
    )
    return gid_map


def import_metaobject_entries(dest_client, exported_entries: List[Dict[str, Any]]) -> None:
    created = 0
    updated = 0
    failed = 0

    for entry in exported_entries:
        fields_literal = (
            "[" + ", ".join(f'{{ key: {gql_quote(f["key"])}, value: {gql_quote(f["value"])} }}' for f in entry["fields"]) + "]"
        )

        mutation = f"""
        mutation {{
          metaobjectUpsert(
            handle: {{ type: {gql_quote(entry["definition_type"])}, handle: {gql_quote(entry["handle"])} }}
            metaobject: {{ fields: {fields_literal} }}
          ) {{
            metaobject {{ id handle }}
            userErrors {{ field message }}
          }}
        }}
        """
        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        except Exception as e:
            logger.warning("Failed to upsert metaobject '%s/%s': %s", entry["definition_type"], entry["handle"], e)
            failed += 1
            continue

        errors = mutation_errors(result, "metaobjectUpsert")
        if errors:
            logger.warning(
                "Failed to upsert metaobject '%s/%s': %s", entry["definition_type"], entry["handle"], errors
            )
            failed += 1
            continue
        created += 1

    logger.info("Metaobject entries import complete: %s upserted, %s failed", created, failed)


def import_metaobjects(dest_client, exported: Dict[str, Any]) -> Dict[str, str]:
    gid_map = import_metaobject_definitions(dest_client, exported.get("definitions", []))
    import_metaobject_entries(dest_client, exported.get("entries", []))
    return gid_map


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer metaobject definitions and entries from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create/update metaobjects on the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    args = parser.parse_args()

    src_shop = os.getenv("SRC_SHOPIFY_SHOP")
    src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")

    if not all([src_shop, src_token, dest_shop, dest_token]):
        raise RuntimeError(
            "Missing .env values: SRC_SHOPIFY_SHOP, SRC_SHOPIFY_ACCESS_TOKEN, DEST_SHOPIFY_SHOP, DEST_SHOPIFY_ACCESS_TOKEN"
        )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    src_client = make_client(src_shop, src_token)
    dest_client = make_client(dest_shop, dest_token)

    logger.info("Exporting metaobjects from %s", src_shop)
    exported = export_metaobjects(src_client)

    ts = int(time.time())
    out_file = out_dir / f"metaobjects_export_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(exported, f, indent=2, ensure_ascii=False)
    logger.info(
        "Export complete: %s (%s definitions, %s entries)",
        out_file,
        len(exported["definitions"]),
        len(exported["entries"]),
    )

    if args.execute:
        logger.info("Importing metaobjects into %s", dest_shop)
        import_metaobjects(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to write metaobjects to the destination store")


if __name__ == "__main__":
    main()
