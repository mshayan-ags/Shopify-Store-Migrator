import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from transfer.transfer_product import make_client
from transfer.transfer_locations import fetch_all_locations
from utils.shopify_graphql_utils import paginate_connection, mutation_errors
from utils.concurrency_utils import retry_with_backoff, gql_quote
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_shipping")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def location_gid(location_id: int) -> str:
    return f"gid://shopify/Location/{location_id}"


def fetch_merchant_delivery_profiles(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str, include_location_names: bool) -> str:
        location_group_fields = "id locations(first: 10) { nodes { id name } }" if include_location_names else "id"
        return f"""
        query {{
          deliveryProfiles(first: 20, merchantOwnedOnly: true{after_clause}) {{
            edges {{
              node {{
                id
                name
                default
                profileLocationGroups {{
                  locationGroup {{
                    {location_group_fields}
                  }}
                  locationGroupZones(first: 20) {{
                    edges {{
                      node {{
                        zone {{
                          id
                          name
                          countries {{
                            id
                            name
                            code {{ countryCode restOfWorld }}
                            provinces {{ id name code }}
                          }}
                        }}
                        methodDefinitions(first: 20) {{
                          edges {{
                            node {{
                              id
                              name
                              active
                              rateProvider {{
                                __typename
                                ... on DeliveryRateDefinition {{
                                  price {{ amount currencyCode }}
                                }}
                              }}
                            }}
                          }}
                        }}
                      }}
                    }}
                  }}
                }}
              }}
            }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    try:
        return paginate_connection(client, lambda after: build_query(after, True), ("deliveryProfiles",))
    except RuntimeError as e:
        if "read_locations" not in str(e) and "access denied" not in str(e).lower():
            raise
        logger.warning(
            "Store doesn't have read_locations granted yet -- exporting zones/rates without location "
            "group membership. Every profile will fall back to the destination's first location on import "
            "(re-run once read_locations is granted for accurate per-location assignment): %s",
            e,
        )
        return paginate_connection(client, lambda after: build_query(after, False), ("deliveryProfiles",))


def export_shipping(client) -> Dict[str, Any]:
    profiles = fetch_merchant_delivery_profiles(client)
    exported = []

    for profile in profiles:
        zones_out = []
        skipped_non_flat = 0

        for group in profile.get("profileLocationGroups") or []:
            location_names = [n["name"] for n in ((group.get("locationGroup") or {}).get("locations") or {}).get("nodes", [])]

            for zone_edge in ((group.get("locationGroupZones") or {}).get("edges") or []):
                zone = zone_edge["node"]["zone"]
                methods_out = []

                for method_edge in (zone_edge["node"].get("methodDefinitions") or {}).get("edges", []):
                    method = method_edge["node"]
                    rate_provider = method.get("rateProvider") or {}
                    if rate_provider.get("__typename") != "DeliveryRateDefinition":
                        skipped_non_flat += 1
                        continue
                    price = rate_provider.get("price") or {}
                    methods_out.append(
                        {
                            "name": method.get("name"),
                            "active": method.get("active", True),
                            "amount": price.get("amount"),
                            "currency_code": price.get("currencyCode"),
                        }
                    )

                countries_out = []
                for c in zone.get("countries") or []:
                    code = c.get("code") or {}
                    countries_out.append(
                        {
                            "name": c.get("name"),
                            "country_code": code.get("countryCode"),
                            "rest_of_world": code.get("restOfWorld", False),
                            "provinces": [p.get("code") for p in (c.get("provinces") or []) if p.get("code")],
                        }
                    )

                zones_out.append(
                    {
                        "name": zone.get("name"),
                        "countries": countries_out,
                        "methods": methods_out,
                        "location_names": location_names,
                    }
                )

        if skipped_non_flat:
            logger.info(
                "Profile '%s': skipped %s non-flat-rate method(s) (carrier-calculated/app-owned, not portable)",
                profile.get("name"),
                skipped_non_flat,
            )

        exported.append({"name": profile.get("name"), "default": profile.get("default", False), "zones": zones_out})

    logger.info("Exported %s delivery profile(s)", len(exported))
    return {"profiles": exported}


def build_destination_location_lookup(dest_client) -> Dict[str, str]:
    locations = fetch_all_locations(dest_client)
    return {(loc.get("name") or "").strip().lower(): location_gid(loc["id"]) for loc in locations if loc.get("id")}


def fetch_destination_default_profile_id(dest_client) -> Optional[str]:
    query = """
    query {
      deliveryProfiles(first: 20) {
        edges { node { id default } }
      }
    }
    """
    data = retry_with_backoff(lambda: dest_client.query(query))
    for edge in data.get("deliveryProfiles", {}).get("edges", []):
        if edge["node"].get("default"):
            return edge["node"]["id"]
    return None


def build_zones_input(profile: Dict[str, Any]) -> List[str]:
    zones_input = []
    for zone in profile.get("zones", []):
        countries_input = []
        for c in zone.get("countries", []):
            if c.get("rest_of_world"):
                countries_input.append("{ restOfWorld: true }")
                continue
            provinces = c.get("provinces") or []
            if provinces:
                province_list = ", ".join(f"{{ code: {gql_quote(p)} }}" for p in provinces)
                province_clause = f", provinces: [{province_list}]"
            else:
                province_clause = ", includeAllProvinces: true"
            countries_input.append(f"{{ code: {c['country_code']}{province_clause} }}")

        methods_input = []
        for m in zone.get("methods", []):
            if m.get("amount") is None:
                continue
            methods_input.append(
                "{ name: %s, rateDefinition: { price: { amount: %s, currencyCode: %s } } }"
                % (gql_quote(m.get("name")), m["amount"], m.get("currency_code") or "USD")
            )

        if not methods_input:
            continue

        zones_input.append(
            "{ name: %s, countries: [%s], methodDefinitionsToCreate: [%s] }"
            % (gql_quote(zone.get("name")), ", ".join(countries_input), ", ".join(methods_input))
        )

    return zones_input


def resolve_location_gids(profile: Dict[str, Any], location_lookup: Dict[str, str], fallback_gid: str) -> List[str]:
    location_names = {n for z in profile.get("zones", []) for n in z.get("location_names", [])}
    matched_gids = [location_lookup[n.strip().lower()] for n in location_names if n.strip().lower() in location_lookup]
    if not matched_gids:
        if location_names:
            logger.warning(
                "Profile '%s': no destination location matched %s, falling back to first destination location",
                profile.get("name"),
                sorted(location_names),
            )
        matched_gids = [fallback_gid]
    return matched_gids


def import_shipping(dest_client, exported: Dict[str, Any]) -> None:
    location_lookup = build_destination_location_lookup(dest_client)
    if not location_lookup:
        raise RuntimeError("Destination store has no locations -- run transfer_locations.py --execute first")
    fallback_location_gid = next(iter(location_lookup.values()))

    created = 0
    updated = 0
    failed = 0

    for profile in exported.get("profiles", []):
        zones_input = build_zones_input(profile)
        if not zones_input:
            logger.warning("Profile '%s' has no flat-rate zones to create, skipping", profile.get("name"))
            continue

        matched_gids = resolve_location_gids(profile, location_lookup, fallback_location_gid)
        locations_clause = ", ".join(gql_quote(g) for g in matched_gids)
        location_groups_clause = f"""{{
              locations: [{locations_clause}]
              zonesToCreate: [{", ".join(zones_input)}]
            }}"""

        if profile.get("default"):
            dest_default_id = fetch_destination_default_profile_id(dest_client)
            if not dest_default_id:
                logger.warning("Could not find a default delivery profile on the destination store, skipping '%s'", profile.get("name"))
                failed += 1
                continue

            mutation = f"""
            mutation {{
              deliveryProfileUpdate(id: {gql_quote(dest_default_id)}, profile: {{
                locationGroupsToCreate: {location_groups_clause}
              }}) {{
                profile {{ id name }}
                userErrors {{ field message }}
              }}
            }}
            """
            mutation_name = "deliveryProfileUpdate"
        else:
            mutation = f"""
            mutation {{
              deliveryProfileCreate(profile: {{
                name: {gql_quote(profile.get("name"))}
                locationGroupsToCreate: {location_groups_clause}
              }}) {{
                profile {{ id name }}
                userErrors {{ field message }}
              }}
            }}
            """
            mutation_name = "deliveryProfileCreate"

        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        except Exception as e:
            logger.warning("Failed to apply delivery profile '%s': %s", profile.get("name"), e)
            failed += 1
            continue

        errors = mutation_errors(result, mutation_name)
        if errors:
            logger.warning("Failed to apply delivery profile '%s': %s", profile.get("name"), errors)
            failed += 1
            continue

        if profile.get("default"):
            logger.info("Added zones to destination's default profile from '%s'", profile.get("name"))
            updated += 1
        else:
            logger.info("Created delivery profile '%s'", profile.get("name"))
            created += 1

    logger.info("Shipping import complete: %s created, %s updated (default profile), %s failed", created, updated, failed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer shipping rate configuration from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create matching delivery profiles on the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    parser.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import this previously-saved canonical JSON file "
            "instead (see docs/CANONICAL_SCHEMA.md). Lets you import from a non-Shopify source "
            "connector or replay a prior dry-run export. No SRC_SHOPIFY_* credentials needed "
            "in this mode."
        ),
    )
    args = parser.parse_args()

    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")
    require_env(DEST_SHOPIFY_SHOP=dest_shop, DEST_SHOPIFY_ACCESS_TOKEN=dest_token)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    dest_client = make_client(dest_shop, dest_token)

    if args.import_from:
        logger.info("Loading export from %s (skipping source fetch)", args.import_from)
        if args.import_from.lower().endswith(".xlsx"):
            from utils.tabular_io import import_from_xlsx
            exported = import_from_xlsx(args.import_from)
        else:
            with open(args.import_from, "r", encoding="utf-8") as f:
                exported = json.load(f)
    else:
        src_shop = os.getenv("SRC_SHOPIFY_SHOP")
        src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
        require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

        src_client = make_client(src_shop, src_token)

        exported = export_shipping(src_client)

        ts = int(time.time())
        out_file = out_dir / f"shipping_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)
        logger.info("Export complete: %s", out_file)

        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"shipping_export_{ts}.xlsx")

    if args.execute:
        import_shipping(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to create delivery profiles on the destination store")


if __name__ == "__main__":
    main()
