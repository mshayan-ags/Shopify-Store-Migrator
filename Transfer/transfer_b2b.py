import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from transfer.transfer_product import make_client
from transfer.transfer_store_metafields import retry_with_backoff, gql_quote
from utils.shopify_graphql_utils import paginate_connection, mutation_errors
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_b2b")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


COMPANY_NODE_FIELDS = """
    id
    name
    note
    externalId
    locations(first: 50) {
      edges { node {
        id
        name
        billingAddress { address1 address2 city zoneCode zip countryCode phone recipient }
        shippingAddress { address1 address2 city zoneCode zip countryCode phone recipient }
      } }
    }
    contacts(first: 50) {
      edges { node {
        id
        customer { email firstName lastName phone }
      } }
    }
"""


def _nodes(connection: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [edge["node"] for edge in (connection or {}).get("edges", [])]


def fetch_all_companies(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          companies(first: 50{after_clause}) {{
            edges {{ node {{ {COMPANY_NODE_FIELDS} }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("companies",))


def export_companies(src_client) -> List[Dict[str, Any]]:
    companies = fetch_all_companies(src_client)
    exported = []

    for c in companies:
        locations = [
            {
                "name": loc.get("name"),
                "billing_address": loc.get("billingAddress"),
                "shipping_address": loc.get("shippingAddress"),
            }
            for loc in _nodes(c.get("locations"))
        ]

        contacts = []
        for contact in _nodes(c.get("contacts")):
            customer = contact.get("customer") or {}
            if not customer.get("email"):
                continue
            contacts.append(
                {
                    "email": customer.get("email"),
                    "first_name": customer.get("firstName"),
                    "last_name": customer.get("lastName"),
                    "phone": customer.get("phone"),
                }
            )

        exported.append(
            {
                "id": c["id"],
                "name": c.get("name"),
                "note": c.get("note"),
                "external_id": c.get("externalId"),
                "locations": locations,
                "contacts": contacts,
            }
        )
        logger.info(
            "Exported company '%s' with %s location(s) and %s contact(s)",
            c.get("name"), len(locations), len(contacts),
        )

    return exported


def build_dest_customer_email_index(dest_client) -> Dict[str, str]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          customers(first: 100{after_clause}) {{
            edges {{ node {{ id email }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    customers = paginate_connection(dest_client, build_query, ("customers",))
    return {c["email"].strip().lower(): c["id"] for c in customers if c.get("email")}


def company_address_input_literal(address: Optional[Dict[str, Any]]) -> Optional[str]:
    if not address:
        return None

    fields = []
    mapping = [
        ("address1", "address1"),
        ("address2", "address2"),
        ("city", "city"),
        ("zoneCode", "zoneCode"),
        ("zip", "zip"),
        ("phone", "phone"),
        ("recipient", "recipient"),
    ]
    for src_key, input_key in mapping:
        value = address.get(src_key)
        if value:
            fields.append(f"{input_key}: {gql_quote(value)}")

    if address.get("countryCode"):
        fields.append(f"countryCode: {address['countryCode']}")

    if not fields:
        return None
    return "{ " + ", ".join(fields) + " }"


def build_company_location_input_literal(location: Dict[str, Any]) -> str:
    fields = []
    if location.get("name"):
        fields.append(f"name: {gql_quote(location['name'])}")

    billing = company_address_input_literal(location.get("billing_address"))
    if billing:
        fields.append(f"billingAddress: {billing}")

    shipping = company_address_input_literal(location.get("shipping_address"))
    if shipping:
        fields.append(f"shippingAddress: {shipping}")

    return "{ " + ", ".join(fields) + " }"


def import_companies(dest_client, exported_companies: List[Dict[str, Any]]) -> None:
    dest_companies = fetch_all_companies(dest_client)
    dest_by_name = {(c.get("name") or "").strip().lower(): c for c in dest_companies}
    dest_customer_by_email = build_dest_customer_email_index(dest_client)

    companies_created = companies_reused = 0
    locations_created = 0
    contacts_assigned = contacts_skipped_no_customer = 0
    failed = 0

    for company in exported_companies:
        try:
            name_key = (company.get("name") or "").strip().lower()
            existing_company = dest_by_name.get(name_key)
            locations = company.get("locations") or []

            if existing_company:
                company_gid = existing_company["id"]
                companies_reused += 1
                existing_location_names = {
                    (loc.get("name") or "").strip().lower()
                    for loc in _nodes(existing_company.get("locations"))
                }
                locations_to_create = [
                    loc for loc in locations
                    if (loc.get("name") or "").strip().lower() not in existing_location_names
                ]
            else:
                company_fields = []
                if company.get("name"):
                    company_fields.append(f"name: {gql_quote(company['name'])}")
                if company.get("note"):
                    company_fields.append(f"note: {gql_quote(company['note'])}")
                if company.get("external_id"):
                    company_fields.append(f"externalId: {gql_quote(company['external_id'])}")

                first_location = locations[0] if locations else {
                    "name": f"{company.get('name') or 'Company'} location"
                }
                location_literal = build_company_location_input_literal(first_location)

                mutation = f"""
                mutation {{
                  companyCreate(input: {{
                    company: {{ {", ".join(company_fields)} }}
                    companyLocation: {location_literal}
                  }}) {{
                    company {{ id name }}
                    companyLocation {{ id name }}
                    userErrors {{ field message }}
                  }}
                }}
                """
                try:
                    result = retry_with_backoff(lambda: dest_client.mutation(mutation))
                except Exception as e:
                    logger.error(
                        "companyCreate failed for company '%s' -- full Shopify error: %s",
                        company.get("name"), e,
                    )
                    failed += 1
                    continue

                errors = mutation_errors(result, "companyCreate")
                if errors:
                    logger.error(
                        "companyCreate returned userErrors for company '%s': %s",
                        company.get("name"), errors,
                    )
                    failed += 1
                    continue

                company_gid = result["companyCreate"]["company"]["id"]
                logger.info("Created company '%s'", company.get("name"))
                companies_created += 1

                locations_to_create = locations[1:]

            for loc in locations_to_create:
                loc_literal = build_company_location_input_literal(loc)
                mutation = f"""
                mutation {{
                  companyLocationCreate(companyId: {gql_quote(company_gid)}, input: {loc_literal}) {{
                    companyLocation {{ id name }}
                    userErrors {{ field message }}
                  }}
                }}
                """
                try:
                    result = retry_with_backoff(lambda: dest_client.mutation(mutation))
                except Exception as e:
                    logger.error(
                        "companyLocationCreate failed for location '%s' (company '%s') -- "
                        "full Shopify error: %s",
                        loc.get("name"), company.get("name"), e,
                    )
                    failed += 1
                    continue

                errors = mutation_errors(result, "companyLocationCreate")
                if errors:
                    logger.error(
                        "companyLocationCreate returned userErrors for location '%s' "
                        "(company '%s'): %s",
                        loc.get("name"), company.get("name"), errors,
                    )
                    failed += 1
                    continue

                logger.info(
                    "Created location '%s' under company '%s'", loc.get("name"), company.get("name")
                )
                locations_created += 1

            existing_contact_emails = set()
            if existing_company:
                for contact in _nodes(existing_company.get("contacts")):
                    email = (contact.get("customer") or {}).get("email")
                    if email:
                        existing_contact_emails.add(email.strip().lower())

            for contact in company.get("contacts", []):
                email_key = (contact.get("email") or "").strip().lower()
                if not email_key or email_key in existing_contact_emails:
                    continue

                customer_gid = dest_customer_by_email.get(email_key)
                if not customer_gid:
                    logger.warning(
                        "No destination customer found for contact email '%s' (company '%s') "
                        "-- run transfer_customers.py --execute first so this contact has a "
                        "destination customer to be assigned to",
                        contact.get("email"), company.get("name"),
                    )
                    contacts_skipped_no_customer += 1
                    continue

                mutation = f"""
                mutation {{
                  companyAssignCustomerAsContact(
                    companyId: {gql_quote(company_gid)}
                    customerId: {gql_quote(customer_gid)}
                  ) {{
                    companyContact {{ id }}
                    userErrors {{ field message }}
                  }}
                }}
                """
                try:
                    result = retry_with_backoff(lambda: dest_client.mutation(mutation))
                except Exception as e:
                    logger.error(
                        "companyAssignCustomerAsContact failed for '%s' (company '%s') -- "
                        "full Shopify error: %s",
                        contact.get("email"), company.get("name"), e,
                    )
                    failed += 1
                    continue

                errors = mutation_errors(result, "companyAssignCustomerAsContact")
                if errors:
                    logger.error(
                        "companyAssignCustomerAsContact returned userErrors for '%s' "
                        "(company '%s'): %s",
                        contact.get("email"), company.get("name"), errors,
                    )
                    failed += 1
                    continue

                logger.info(
                    "Assigned contact '%s' to company '%s'", contact.get("email"), company.get("name")
                )
                contacts_assigned += 1

        except Exception:
            logger.exception("Unhandled error migrating company '%s'", company.get("name"))
            failed += 1

    logger.info(
        "Companies import complete: %s created, %s reused, %s location(s) created, "
        "%s contact(s) assigned, %s contact(s) skipped (no matching destination customer), "
        "%s failure(s)",
        companies_created, companies_reused, locations_created,
        contacts_assigned, contacts_skipped_no_customer, failed,
    )


def fetch_all_catalogs(client) -> List[Dict[str, Any]]:
    query = """
    {
      catalogs(first: 50) {
        edges { node {
          id
          title
          status
          ... on CompanyLocationCatalog {
            priceList {
              id
              name
              currencyCode
              prices(first: 250) {
                edges { node {
                  variant { id sku }
                  price { amount currencyCode }
                } }
              }
            }
          }
        } }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    data = retry_with_backoff(lambda: client.query(query))
    connection = data["catalogs"]

    if connection.get("pageInfo", {}).get("hasNextPage"):
        logger.warning(
            "Store has more than 50 catalogs; only the first 50 were exported "
            "(catalogs/price lists are a best-effort, non-paginated section here)"
        )

    return _nodes(connection)


def export_catalogs(src_client) -> List[Dict[str, Any]]:
    catalogs = fetch_all_catalogs(src_client)
    exported = []

    for cat in catalogs:
        price_list = cat.get("priceList")
        if not price_list:
            continue

        prices = []
        for edge in (price_list.get("prices") or {}).get("edges", []):
            node = edge["node"]
            variant = node.get("variant") or {}
            price = node.get("price") or {}
            if not variant.get("sku") or not price.get("amount"):
                continue
            prices.append(
                {
                    "sku": variant["sku"],
                    "amount": price.get("amount"),
                    "currency_code": price.get("currencyCode"),
                }
            )

        exported.append(
            {
                "catalog_id": cat["id"],
                "catalog_title": cat.get("title"),
                "price_list_name": price_list.get("name"),
                "currency_code": price_list.get("currencyCode"),
                "prices": prices,
            }
        )

    logger.info("Exported %s catalog price list(s)", len(exported))
    return exported


def build_dest_variant_sku_index(dest_client) -> Dict[str, str]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          productVariants(first: 250{after_clause}) {{
            edges {{ node {{ id sku }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    variants = paginate_connection(dest_client, build_query, ("productVariants",))
    return {v["sku"].strip().lower(): v["id"] for v in variants if v.get("sku")}


def fetch_all_dest_price_lists(dest_client) -> List[Dict[str, Any]]:
    query = """
    {
      priceLists(first: 50) {
        edges { node { id name currencyCode } }
      }
    }
    """
    data = retry_with_backoff(lambda: dest_client.query(query))
    return _nodes(data["priceLists"])


def import_catalogs(
    dest_client,
    exported_catalogs: List[Dict[str, Any]],
    variant_sku_index: Dict[str, str],
) -> None:
    dest_price_lists = fetch_all_dest_price_lists(dest_client)
    by_name = {(pl.get("name") or "").strip().lower(): pl for pl in dest_price_lists}

    created = prices_set = failed = 0

    for cat in exported_catalogs:
        name_key = (cat.get("price_list_name") or "").strip().lower()
        existing = by_name.get(name_key)

        if existing:
            price_list_gid = existing["id"]
            logger.info(
                "Price list '%s' already exists on destination; reusing it",
                cat.get("price_list_name"),
            )
        else:
            currency = cat.get("currency_code") or "USD"
            mutation = f"""
            mutation {{
              priceListCreate(input: {{
                name: {gql_quote(cat.get("price_list_name") or "Migrated price list")}
                currency: {currency}
              }}) {{
                priceList {{ id }}
                userErrors {{ field message }}
              }}
            }}
            """
            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            except Exception as e:
                logger.error(
                    "priceListCreate failed for '%s' -- full Shopify error: %s",
                    cat.get("price_list_name"), e,
                )
                failed += 1
                continue

            errors = mutation_errors(result, "priceListCreate")
            if errors:
                logger.error(
                    "priceListCreate returned userErrors for '%s': %s",
                    cat.get("price_list_name"), errors,
                )
                failed += 1
                continue

            price_list_gid = result["priceListCreate"]["priceList"]["id"]
            logger.info("Created price list '%s'", cat.get("price_list_name"))
            created += 1

        matched_prices = []
        skipped_no_match = 0
        for price in cat.get("prices", []):
            variant_gid = variant_sku_index.get((price.get("sku") or "").strip().lower())
            if not variant_gid:
                skipped_no_match += 1
                continue
            matched_prices.append(
                {
                    "variant_id": variant_gid,
                    "amount": price["amount"],
                    "currency_code": price.get("currency_code") or cat.get("currency_code") or "USD",
                }
            )

        if skipped_no_match:
            logger.warning(
                "Price list '%s': %s price(s) skipped (no matching destination variant SKU)",
                cat.get("price_list_name"), skipped_no_match,
            )

        for i in range(0, len(matched_prices), 250):
            batch = matched_prices[i:i + 250]
            prices_literal = "[" + ", ".join(
                f'{{ variantId: {gql_quote(p["variant_id"])}, '
                f'price: {{ amount: {gql_quote(p["amount"])}, currencyCode: {p["currency_code"]} }} }}'
                for p in batch
            ) + "]"

            mutation = f"""
            mutation {{
              priceListFixedPricesAdd(priceListId: {gql_quote(price_list_gid)}, prices: {prices_literal}) {{
                prices {{ variant {{ id }} }}
                userErrors {{ field message }}
              }}
            }}
            """
            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            except Exception as e:
                logger.error(
                    "priceListFixedPricesAdd failed for '%s' -- full Shopify error: %s",
                    cat.get("price_list_name"), e,
                )
                failed += 1
                continue

            errors = mutation_errors(result, "priceListFixedPricesAdd")
            if errors:
                logger.error(
                    "priceListFixedPricesAdd returned userErrors for '%s': %s",
                    cat.get("price_list_name"), errors,
                )
                failed += 1
                continue

            prices_set += len(batch)

    logger.info(
        "Catalogs/price lists import complete: %s price list(s) created, %s price(s) set, "
        "%s failure(s)",
        created, prices_set, failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transfer B2B/wholesale data (companies, locations, contacts, "
        "catalogs, price lists) from Src to dest"
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Create/update companies, locations, contacts, and price lists on the destination store",
    )
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    parser.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import this previously-saved canonical JSON file "
            "instead (see docs/CANONICAL_SCHEMA.md). Lets you import from a non-Shopify source "
            "connector or replay a prior dry-run export. No SRC_SHOPIFY_* credentials needed in "
            "this mode."
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
            loaded = import_from_xlsx(args.import_from)
        else:
            with open(args.import_from, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        exported_companies = loaded.get("companies", [])
        exported_catalogs = loaded.get("catalogs", [])
    else:
        src_shop = os.getenv("SRC_SHOPIFY_SHOP")
        src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
        require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

        src_client = make_client(src_shop, src_token)

        logger.info("Exporting companies from %s", src_shop)
        exported_companies = export_companies(src_client)

        logger.info("Exporting catalogs/price lists from %s (best-effort)", src_shop)
        try:
            exported_catalogs = export_catalogs(src_client)
        except Exception as e:
            logger.warning(
                "Could not export catalogs/price lists from %s -- this section is best-effort "
                "since Shopify's B2B catalog/price-list schema is one of the least stable parts "
                "of the Admin API (possibly plan-gated, or the field/mutation names above don't "
                "match your store's API version -- see the module docstring). Companies, "
                "locations, and contacts are unaffected. Full error: %s",
                src_shop, e,
            )
            exported_catalogs = []

        exported = {"companies": exported_companies, "catalogs": exported_catalogs}

        ts = int(time.time())
        out_file = out_dir / f"b2b_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)
        logger.info("Export complete: %s", out_file)

        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"b2b_export_{ts}.xlsx")

    if args.execute:
        logger.info("Importing companies into %s", dest_shop)
        import_companies(dest_client, exported["companies"])

        if exported_catalogs:
            logger.info("Importing catalogs/price lists into %s (best-effort)", dest_shop)
            try:
                variant_sku_index = build_dest_variant_sku_index(dest_client)
                import_catalogs(dest_client, exported_catalogs, variant_sku_index)
            except Exception as e:
                logger.warning(
                    "Could not migrate catalogs/price lists into %s -- this section is "
                    "best-effort (see module docstring); companies/locations/contacts were "
                    "still migrated above. Full error: %s",
                    dest_shop, e,
                )
        else:
            logger.info("No catalogs/price lists were exported; skipping that section")
    else:
        logger.info("Dry-run finished. Re-run with --execute to write B2B data to the destination store")


if __name__ == "__main__":
    main()
