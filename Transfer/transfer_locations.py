"""Transfer store locations (warehouses/pickup points) from Src to dest.

Requires read_locations/write_locations scope on both stores -- neither
currently has it (both apps were originally granted only products/orders
scopes), so this will 403 until that's added in Shopify Admin > Apps >
[app name] > Configuration on both stores.

Only the location's name and address are transferred. Inventory levels at
each location are handled separately by transfer_product.py's per-variant
inventory sync, which distributes stock across every destination location
whose name matches a source location -- run this script with --execute
*before* transferring products so those destination locations exist.

Usage:
    python transfer_locations.py                # dry-run export
    python transfer_locations.py --execute       # create missing locations on dest
"""
import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from Transfer.transfer_product import make_client
from Transfer.transfer_store_metafields import retry_with_backoff, gql_quote
from utils.shopify_graphql_utils import mutation_errors

load_dotenv()

logger = logging.getLogger("transfer_locations")
logging.basicConfig(level=logging.INFO)


def fetch_all_locations(client) -> List[Dict[str, Any]]:
    locations: List[Dict[str, Any]] = []
    since_id = 0

    while True:
        params = {"limit": 250}
        if since_id:
            params["since_id"] = since_id
        page = retry_with_backoff(lambda: client.rest_get("locations", params=params))
        page_locations = page.get("locations", [])
        if not page_locations:
            break
        locations.extend(page_locations)
        since_id = page_locations[-1].get("id") or 0
        if len(page_locations) < 250 or not since_id:
            break

    return locations


def export_locations(client) -> Dict[str, Any]:
    locations = fetch_all_locations(client)
    exported = [
        {
            "name": loc.get("name"),
            "address1": loc.get("address1"),
            "address2": loc.get("address2"),
            "city": loc.get("city"),
            "province_code": loc.get("province_code"),
            "country_code": loc.get("country_code"),
            "zip": loc.get("zip"),
            "phone": loc.get("phone"),
            "active": loc.get("active", True),
            "fulfills_online_orders": loc.get("legacy", False) or True,
        }
        for loc in locations
    ]
    logger.info("Exported %s location(s)", len(exported))
    return {"locations": exported}


def import_locations(dest_client, exported: Dict[str, Any]) -> None:
    existing_names = {(loc.get("name") or "").strip().lower() for loc in fetch_all_locations(dest_client)}

    created = 0
    skipped = 0
    failed = 0

    for loc in exported.get("locations", []):
        if (loc.get("name") or "").strip().lower() in existing_names:
            skipped += 1
            continue

        address_fields = []
        for src_key, input_key in [
            ("address1", "address1"),
            ("address2", "address2"),
            ("city", "city"),
            ("phone", "phone"),
            ("zip", "zip"),
            ("province_code", "provinceCode"),
        ]:
            if loc.get(src_key):
                address_fields.append(f"{input_key}: {gql_quote(loc[src_key])}")
        if loc.get("country_code"):
            # CountryCode is a GraphQL enum -- bare literal, not a quoted string.
            address_fields.append(f"countryCode: {loc['country_code']}")

        mutation = f"""
        mutation {{
          locationAdd(input: {{
            name: {gql_quote(loc.get("name"))}
            address: {{ {", ".join(address_fields)} }}
            fulfillsOnlineOrders: {"true" if loc.get("fulfills_online_orders") else "false"}
          }}) {{
            location {{ id name }}
            userErrors {{ field message }}
          }}
        }}
        """
        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        except Exception as e:
            logger.warning("Failed to create location '%s': %s", loc.get("name"), e)
            failed += 1
            continue

        errors = mutation_errors(result, "locationAdd")
        if errors:
            logger.warning("Failed to create location '%s': %s", loc.get("name"), errors)
            failed += 1
            continue

        logger.info("Created location '%s'", loc.get("name"))
        created += 1

    logger.info("Locations import complete: %s created, %s already existed, %s failed", created, skipped, failed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer store locations from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create missing locations on the destination store")
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

    logger.info("Exporting locations from %s", src_shop)
    exported = export_locations(src_client)

    ts = int(time.time())
    out_file = out_dir / f"locations_export_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(exported, f, indent=2, ensure_ascii=False)
    logger.info("Export complete: %s", out_file)

    if args.execute:
        logger.info("Importing locations into %s", dest_shop)
        import_locations(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to create locations on the destination store")


if __name__ == "__main__":
    main()
