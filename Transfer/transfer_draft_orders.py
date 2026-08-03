import argparse
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from transfer.transfer_product import make_client
from transfer.transfer_store_metafields import retry_with_backoff, set_metafields, gql_quote
from utils.shopify_graphql_utils import (
    paginate_connection,
    export_metafields,
    mutation_errors,
    run_concurrently,
    DEFAULT_WORKERS,
)
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_draft_orders")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


MIGRATABLE_STATUSES = {"OPEN", "INVOICE_SENT"}

ADDRESS_QUERY_FIELDS = "firstName lastName address1 address2 city provinceCode zip countryCode phone company"

DRAFT_ORDER_NODE_FIELDS = f"""
    id
    name
    email
    note2
    tags
    currencyCode
    taxExempt
    taxesIncluded
    status
    customAttributes {{ key value }}
    appliedDiscount {{ title description valueType value }}
    shippingLine {{
      title
      originalPriceSet {{ shopMoney {{ amount currencyCode }} }}
    }}
    shippingAddress {{ {ADDRESS_QUERY_FIELDS} }}
    billingAddress {{ {ADDRESS_QUERY_FIELDS} }}
    customer {{ email }}
    lineItems(first: 100) {{
      edges {{
        node {{
          title
          sku
          quantity
          custom
          originalUnitPriceSet {{ shopMoney {{ amount currencyCode }} }}
          taxable
          requiresShipping
          customAttributes {{ key value }}
        }}
      }}
    }}
    metafields(first: 50) {{
      edges {{ node {{ namespace key value type }} }}
    }}
"""


def fetch_all_draft_orders(client, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          draftOrders(first: 50{after_clause}) {{
            edges {{ node {{ {DRAFT_ORDER_NODE_FIELDS} }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    draft_orders = paginate_connection(client, build_query, ("draftOrders",))
    if limit:
        draft_orders = draft_orders[:limit]
    return draft_orders


def fetch_all_draft_order_names(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          draftOrders(first: 100{after_clause}) {{
            edges {{ node {{ name }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("draftOrders",))


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


def build_dest_customer_email_index(dest_client) -> Dict[str, str]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          customers(first: 250{after_clause}) {{
            edges {{ node {{ id email }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    customers = paginate_connection(dest_client, build_query, ("customers",))
    return {c["email"].strip().lower(): c["id"] for c in customers if c.get("email")}


def export_draft_orders(src_client, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    draft_orders = fetch_all_draft_orders(src_client, limit=limit)

    exported = []
    skipped_completed = 0
    for d in draft_orders:
        status = d.get("status")
        if status not in MIGRATABLE_STATUSES:
            skipped_completed += 1
            continue

        line_items = []
        for edge in d["lineItems"]["edges"]:
            n = edge["node"]
            shop_money = (n.get("originalUnitPriceSet") or {}).get("shopMoney") or {}
            line_items.append(
                {
                    "title": n.get("title"),
                    "sku": n.get("sku"),
                    "quantity": n.get("quantity"),
                    "custom": n.get("custom", False),
                    "price": shop_money.get("amount"),
                    "currency": shop_money.get("currencyCode"),
                    "taxable": n.get("taxable"),
                    "requires_shipping": n.get("requiresShipping"),
                    "custom_attributes": [
                        {"key": a["key"], "value": a["value"]} for a in (n.get("customAttributes") or [])
                    ],
                }
            )

        applied_discount = d.get("appliedDiscount")
        shipping_line = d.get("shippingLine")

        exported.append(
            {
                "id": d["id"],
                "name": d["name"],
                "email": d.get("email"),
                "customer_email": (d.get("customer") or {}).get("email"),
                "note2": d.get("note2"),
                "tags": d.get("tags") or [],
                "currency": d.get("currencyCode"),
                "tax_exempt": d.get("taxExempt"),
                "taxes_included": d.get("taxesIncluded"),
                "status": status,
                "custom_attributes": [
                    {"key": a["key"], "value": a["value"]} for a in (d.get("customAttributes") or [])
                ],
                "applied_discount": (
                    {
                        "title": applied_discount.get("title"),
                        "description": applied_discount.get("description"),
                        "value_type": applied_discount.get("valueType"),
                        "value": applied_discount.get("value"),
                    }
                    if applied_discount
                    else None
                ),
                "shipping_line": (
                    {
                        "title": shipping_line.get("title"),
                        "amount": (shipping_line.get("originalPriceSet") or {}).get("shopMoney", {}).get("amount"),
                        "currency": (shipping_line.get("originalPriceSet") or {})
                        .get("shopMoney", {})
                        .get("currencyCode"),
                    }
                    if shipping_line
                    else None
                ),
                "shipping_address": d.get("shippingAddress"),
                "billing_address": d.get("billingAddress"),
                "line_items": line_items,
                "metafields": export_metafields(d.get("metafields")),
            }
        )

    logger.info(
        "Exported %s draft order(s) (%s skipped as COMPLETED/CANCELLED)",
        len(exported),
        skipped_completed,
    )
    return exported


def address_input_literal(address: Optional[Dict[str, Any]]) -> Optional[str]:
    if not address:
        return None
    fields = []
    for key in ("firstName", "lastName", "address1", "address2", "city", "provinceCode", "zip", "phone", "company"):
        value = address.get(key)
        if not value:
            continue
        fields.append(f"{key}: {gql_quote(value)}")
    if address.get("countryCode"):
        fields.append(f"countryCode: {address['countryCode']}")
    if not fields:
        return None
    return "{" + ", ".join(fields) + "}"


def build_line_item_literal(item: Dict[str, Any], variant_index: Dict[str, str]) -> str:
    quantity = item.get("quantity") or 1
    sku = (item.get("sku") or "").strip()

    fields = [f"quantity: {quantity}"]
    variant_gid = None
    if not item.get("custom") and sku:
        variant_gid = variant_index.get(sku.lower())

    if variant_gid:
        fields.append(f"variantId: {gql_quote(variant_gid)}")
    else:
        fields.append(f"title: {gql_quote(item.get('title') or sku or 'Item')}")
        price = item.get("price") if item.get("price") is not None else "0.00"
        fields.append(f"originalUnitPrice: {gql_quote(price)}")
        if item.get("requires_shipping") is not None:
            fields.append(f"requiresShipping: {'true' if item['requires_shipping'] else 'false'}")
        if item.get("taxable") is not None:
            fields.append(f"taxable: {'true' if item['taxable'] else 'false'}")

    custom_attrs = item.get("custom_attributes") or []
    if custom_attrs:
        attrs_literal = (
            "[" + ", ".join(f'{{ key: {gql_quote(a["key"])}, value: {gql_quote(a["value"])} }}' for a in custom_attrs) + "]"
        )
        fields.append(f"customAttributes: {attrs_literal}")

    return "{" + ", ".join(fields) + "}"


def build_custom_attributes_literal(attrs: List[Dict[str, Any]]) -> Optional[str]:
    if not attrs:
        return None
    return "[" + ", ".join(f'{{ key: {gql_quote(a["key"])}, value: {gql_quote(a["value"])} }}' for a in attrs) + "]"


def build_applied_discount_literal(discount: Optional[Dict[str, Any]]) -> Optional[str]:
    if not discount or discount.get("value") is None:
        return None
    value_type = discount.get("value_type")
    if value_type not in ("PERCENTAGE", "FIXED_AMOUNT"):
        return None

    fields = [f"value: {discount['value']}", f"valueType: {value_type}"]
    if discount.get("title"):
        fields.append(f"title: {gql_quote(discount['title'])}")
    if discount.get("description"):
        fields.append(f"description: {gql_quote(discount['description'])}")
    return "{" + ", ".join(fields) + "}"


def build_shipping_line_literal(shipping_line: Optional[Dict[str, Any]]) -> Optional[str]:
    if not shipping_line or not shipping_line.get("title"):
        return None
    fields = [f"title: {gql_quote(shipping_line['title'])}"]
    if shipping_line.get("amount") is not None:
        fields.append(f"price: {gql_quote(shipping_line['amount'])}")
    return "{" + ", ".join(fields) + "}"


def import_draft_orders(
    dest_client, exported: List[Dict[str, Any]], max_workers: int = DEFAULT_WORKERS
) -> None:
    variant_index = build_dest_variant_sku_index(dest_client)
    customer_index = build_dest_customer_email_index(dest_client)
    existing_names = {d["name"] for d in fetch_all_draft_order_names(dest_client)}

    counts = {"created": 0, "skipped": 0, "failed": 0}
    counts_lock = threading.Lock()

    def record(key: str) -> None:
        with counts_lock:
            counts[key] += 1

    def process_draft_order(order: Dict[str, Any]) -> None:
        if order["name"] in existing_names:
            logger.info("Draft order %s already exists on destination; skipping", order["name"])
            record("skipped")
            return

        try:
            line_items_literal = (
                "[" + ", ".join(build_line_item_literal(li, variant_index) for li in order["line_items"]) + "]"
            )
            tags_literal = "[" + ", ".join(gql_quote(t) for t in order.get("tags", [])) + "]"

            fields = [f"lineItems: {line_items_literal}", f"tags: {tags_literal}"]
            if order.get("email"):
                fields.append(f"email: {gql_quote(order['email'])}")
            if order.get("note2"):
                fields.append(f"note2: {gql_quote(order['note2'])}")
            if order.get("tax_exempt") is not None:
                fields.append(f"taxExempt: {'true' if order['tax_exempt'] else 'false'}")

            custom_attributes = build_custom_attributes_literal(order.get("custom_attributes") or [])
            if custom_attributes:
                fields.append(f"customAttributes: {custom_attributes}")

            shipping_address = address_input_literal(order.get("shipping_address"))
            if shipping_address:
                fields.append(f"shippingAddress: {shipping_address}")
            billing_address = address_input_literal(order.get("billing_address"))
            if billing_address:
                fields.append(f"billingAddress: {billing_address}")

            shipping_line = build_shipping_line_literal(order.get("shipping_line"))
            if shipping_line:
                fields.append(f"shippingLine: {shipping_line}")

            applied_discount = build_applied_discount_literal(order.get("applied_discount"))
            if applied_discount:
                fields.append(f"appliedDiscount: {applied_discount}")

            match_email = (order.get("customer_email") or order.get("email") or "").strip().lower()
            customer_gid = customer_index.get(match_email) if match_email else None
            if customer_gid:
                fields.append(f"purchasingEntity: {{ customerId: {gql_quote(customer_gid)} }}")

            mutation = f"""
            mutation {{
              draftOrderCreate(input: {{ {", ".join(fields)} }}) {{
                draftOrder {{ id name }}
                userErrors {{ field message }}
              }}
            }}
            """

            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            except Exception as e:
                logger.warning("Failed to create draft order %s: %s", order["name"], e)
                record("failed")
                return

            errors = mutation_errors(result, "draftOrderCreate")
            if errors:
                logger.warning("Failed to create draft order %s: %s", order["name"], errors)
                record("failed")
                return

            draft_order_gid = result["draftOrderCreate"]["draftOrder"]["id"]
            logger.info("Created draft order %s", order["name"])
            record("created")

            if order.get("metafields"):
                set_metafields(dest_client, draft_order_gid, order["metafields"])
        except Exception:
            logger.exception("Unhandled error creating draft order %s", order.get("name"))
            record("failed")

    logger.info("Using %s worker(s)", max_workers)
    run_concurrently(exported, process_draft_order, max_workers=max_workers, label="draft order")

    logger.info(
        "Draft orders import complete: %s created, %s already existed, %s failed",
        counts["created"],
        counts["skipped"],
        counts["failed"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer draft orders from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create draft orders on the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument("--limit", type=int, default=None, help="Only transfer the first N draft orders")
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of draft orders to import concurrently (default {DEFAULT_WORKERS}; 1 = sequential)",
    )
    parser.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import draft order(s) from this previously-saved canonical "
            "JSON file instead (a list of draft_order dicts -- see docs/CANONICAL_SCHEMA.md). Lets you "
            "import from a non-Shopify source connector, or replay a prior dry-run export, without a live "
            "source Shopify store. No SRC_SHOPIFY_* credentials needed in this mode."
        ),
    )
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
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

        logger.info("Exporting draft orders from %s", src_shop)
        exported = export_draft_orders(src_client, limit=args.limit)

        ts = int(time.time())
        out_file = out_dir / f"draft_orders_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)
        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"draft_orders_export_{ts}.xlsx")
        logger.info("Export complete: %s -- REVIEW THIS FILE before running --execute", out_file)

    if args.execute:
        logger.info("Importing draft orders into %s", dest_shop)
        import_draft_orders(dest_client, exported, max_workers=args.workers)
    else:
        logger.info("Dry-run finished. Re-run with --execute to create draft orders on the destination store")


if __name__ == "__main__":
    main()
