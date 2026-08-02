"""Transfer customer accounts from Src to dest.

Requires the `read_customers` / `write_customers` Admin API scope on both
stores' custom apps. Grant it under Shopify Admin > Apps > [app name] >
Configuration, then reinstall to refresh the .env token.

Note: accessing customer PII (email, phone, address) via the Admin API may
also require Shopify's "Protected customer data" access approval for the app,
even for custom apps installed on a single store. If requests fail with a
protected-data error, that approval needs to be requested from Shopify first
(Partner Dashboard > App setup > API access, or Shopify Admin > Settings >
Users and permissions > for custom apps built via the CLI).

Customers are matched/deduped by email. Multipass identifiers and saved
payment methods are not portable across shops and are intentionally not
copied. Email/SMS marketing consent state IS copied.

Usage:
    python transfer_customers.py                # dry-run export
    python transfer_customers.py --execute       # create/update customers on dest
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
from Transfer.transfer_store_metafields import retry_with_backoff, set_metafields, gql_quote
from utils.shopify_graphql_utils import paginate_connection, export_metafields, mutation_errors

load_dotenv()

logger = logging.getLogger("transfer_customers")
logging.basicConfig(level=logging.INFO)


ADDRESS_FIELDS = """
    address1
    address2
    city
    company
    countryCodeV2
    firstName
    lastName
    phone
    provinceCode
    zip
"""

CUSTOMER_NODE_FIELDS = f"""
    id
    email
    firstName
    lastName
    phone
    note
    tags
    taxExempt
    taxExemptions
    locale
    emailMarketingConsent {{ marketingState marketingOptInLevel consentUpdatedAt }}
    smsMarketingConsent {{ marketingState marketingOptInLevel consentUpdatedAt }}
    defaultAddress {{ {ADDRESS_FIELDS} }}
    addressesV2(first: 20) {{ edges {{ node {{ {ADDRESS_FIELDS} }} }} }}
    metafields(first: 100) {{
      edges {{ node {{ namespace key value type }} }}
    }}
"""


def fetch_all_customers(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          customers(first: 100{after_clause}) {{
            edges {{ node {{ {CUSTOMER_NODE_FIELDS} }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("customers",))


def export_customers(src_client) -> List[Dict[str, Any]]:
    customers = fetch_all_customers(src_client)
    exported = []
    for c in customers:
        addresses = [edge["node"] for edge in (c.get("addressesV2") or {}).get("edges", [])]
        exported.append(
            {
                "id": c["id"],
                "email": c.get("email"),
                "first_name": c.get("firstName"),
                "last_name": c.get("lastName"),
                "phone": c.get("phone"),
                "note": c.get("note"),
                "tags": c.get("tags") or [],
                "tax_exempt": c.get("taxExempt"),
                "tax_exemptions": c.get("taxExemptions") or [],
                "locale": c.get("locale"),
                "email_marketing_consent": c.get("emailMarketingConsent"),
                "sms_marketing_consent": c.get("smsMarketingConsent"),
                "default_address": c.get("defaultAddress"),
                "addresses": addresses,
                "metafields": export_metafields(c.get("metafields")),
            }
        )
    logger.info("Exported %s customer(s)", len(exported))
    return exported


def address_input_literal(address: Dict[str, Any]) -> str:
    fields = []
    mapping = {
        "address1": "address1",
        "address2": "address2",
        "city": "city",
        "company": "company",
        "countryCodeV2": "countryCode",
        "firstName": "firstName",
        "lastName": "lastName",
        "phone": "phone",
        "provinceCode": "provinceCode",
        "zip": "zip",
    }
    for src_key, input_key in mapping.items():
        value = address.get(src_key)
        if not value:
            continue
        if input_key == "countryCode":
            # CountryCode is a GraphQL enum on MailingAddressInput -- must be a bare
            # literal (US), not a quoted string ("US"), or the mutation is rejected.
            fields.append(f"{input_key}: {value}")
        else:
            fields.append(f"{input_key}: {gql_quote(value)}")
    return "{" + ", ".join(fields) + "}"


def build_marketing_consent_literal(consent: Any) -> Optional[str]:
    if not consent or not consent.get("marketingState"):
        return None
    fields = [f"marketingState: {consent['marketingState']}"]
    if consent.get("marketingOptInLevel"):
        fields.append(f"marketingOptInLevel: {consent['marketingOptInLevel']}")
    if consent.get("consentUpdatedAt"):
        fields.append(f"consentUpdatedAt: {gql_quote(consent['consentUpdatedAt'])}")
    return "{ " + ", ".join(fields) + " }"


def build_customer_common_fields(customer: Dict[str, Any], include_email: bool) -> List[str]:
    fields = []
    if include_email and customer.get("email"):
        fields.append(f"email: {gql_quote(customer['email'])}")
    if customer.get("first_name"):
        fields.append(f"firstName: {gql_quote(customer['first_name'])}")
    if customer.get("last_name"):
        fields.append(f"lastName: {gql_quote(customer['last_name'])}")
    if customer.get("phone"):
        fields.append(f"phone: {gql_quote(customer['phone'])}")
    if customer.get("note"):
        fields.append(f"note: {gql_quote(customer['note'])}")
    tags_literal = "[" + ", ".join(gql_quote(t) for t in customer.get("tags", [])) + "]"
    fields.append(f"tags: {tags_literal}")
    if customer.get("tax_exempt"):
        fields.append("taxExempt: true")
    if customer.get("tax_exemptions"):
        fields.append("taxExemptions: [" + ", ".join(customer["tax_exemptions"]) + "]")
    if customer.get("locale"):
        fields.append(f"locale: {gql_quote(customer['locale'])}")
    email_consent = build_marketing_consent_literal(customer.get("email_marketing_consent"))
    if email_consent:
        fields.append(f"emailMarketingConsent: {email_consent}")
    sms_consent = build_marketing_consent_literal(customer.get("sms_marketing_consent"))
    if sms_consent:
        fields.append(f"smsMarketingConsent: {sms_consent}")
    return fields


def import_customers(dest_client, exported: List[Dict[str, Any]]) -> None:
    dest_customers = fetch_all_customers(dest_client)
    by_email = {(c.get("email") or "").strip().lower(): c for c in dest_customers if c.get("email")}

    created = updated = failed = 0

    for customer in exported:
        email_key = (customer.get("email") or "").strip().lower()
        if not email_key:
            logger.warning("Skipping customer with no email (id %s)", customer.get("id"))
            continue

        existing = by_email.get(email_key)

        if existing:
            customer_gid = existing["id"]
            update_fields = build_customer_common_fields(customer, include_email=False)
            mutation = f"""
            mutation {{
              customerUpdate(input: {{ id: {gql_quote(customer_gid)}, {", ".join(update_fields)} }}) {{
                customer {{ id email }}
                userErrors {{ field message }}
              }}
            }}
            """
            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
                errors = mutation_errors(result, "customerUpdate")
                if errors:
                    logger.warning("Failed to update customer '%s': %s", customer.get("email"), errors)
                    failed += 1
                else:
                    updated += 1
            except Exception as e:
                logger.warning("Failed to update customer '%s': %s", customer.get("email"), e)
                failed += 1
        else:
            input_fields = build_customer_common_fields(customer, include_email=True)

            mutation = f"""
            mutation {{
              customerCreate(input: {{ {", ".join(input_fields)} }}) {{
                customer {{ id email }}
                userErrors {{ field message }}
              }}
            }}
            """
            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            except Exception as e:
                logger.warning("Failed to create customer '%s': %s", customer.get("email"), e)
                failed += 1
                continue

            errors = mutation_errors(result, "customerCreate")
            if errors:
                logger.warning("Failed to create customer '%s': %s", customer.get("email"), errors)
                failed += 1
                continue
            customer_gid = result["customerCreate"]["customer"]["id"]
            logger.info("Created customer '%s'", customer.get("email"))
            created += 1

            addresses = customer.get("addresses") or ([customer["default_address"]] if customer.get("default_address") else [])
            for address in addresses:
                if not address:
                    continue
                mutation = f"""
                mutation {{
                  customerAddressCreate(customerId: {gql_quote(customer_gid)}, address: {address_input_literal(address)}) {{
                    address {{ id }}
                    userErrors {{ field message }}
                  }}
                }}
                """
                try:
                    addr_result = retry_with_backoff(lambda: dest_client.mutation(mutation))
                except Exception as e:
                    logger.warning("Failed to add address for '%s': %s", customer.get("email"), e)
                    continue

                addr_errors = mutation_errors(addr_result, "customerAddressCreate")
                if addr_errors:
                    logger.warning("Failed to add address for '%s': %s", customer.get("email"), addr_errors)

        if customer.get("metafields"):
            result = set_metafields(dest_client, customer_gid, customer["metafields"])
            logger.info(
                "Customer '%s' metafields: %s updated, %s skipped",
                customer.get("email"),
                result["updated_count"],
                result["skipped_count"],
            )

    logger.info(
        "Customers import complete: %s created, %s updated, %s failed", created, updated, failed
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer customer accounts from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create/update customers on the destination store")
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

    logger.info("Exporting customers from %s", src_shop)
    exported = export_customers(src_client)

    ts = int(time.time())
    out_file = out_dir / f"customers_export_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(exported, f, indent=2, ensure_ascii=False)
    logger.info("Export complete: %s", out_file)

    if args.execute:
        logger.info("Importing customers into %s", dest_shop)
        import_customers(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to write customers to the destination store")


if __name__ == "__main__":
    main()
