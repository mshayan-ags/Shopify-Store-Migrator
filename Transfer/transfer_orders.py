"""Transfer historical orders from Src to dest.

Requires the `read_orders`/`write_orders` scope (already granted). Reading
orders beyond the default 60-day window, or any customer PII embedded on them
(email/phone/addresses), may also require Shopify's "Protected customer data"
access approval for the app — if requests fail with a protected-data error,
that has to be requested from Shopify first.

IMPORTANT: this creates real Order records on the destination store via the
`orderCreate` mutation. No payment is actually processed — historical orders
are recorded with a manual "already paid" transaction reflecting what was
collected on Src, exactly as Shopify's own migration tooling does. This
is financial/accounting data: dry-run the export and review
Results/orders_export_*.json BEFORE running with --execute.

Run transfer_products.py (--all) and transfer_customers.py first, since line
items are matched to destination variants by SKU and orders are associated to
destination customers by email.

Usage:
    python transfer_orders.py                # dry-run export
    python transfer_orders.py --execute       # create orders on dest
    python transfer_orders.py --execute --limit 5   # test with a handful first
"""
import argparse
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from Transfer.transfer_product import make_client
from Transfer.transfer_store_metafields import retry_with_backoff, set_metafields, gql_quote
from utils.shopify_graphql_utils import paginate_connection, export_metafields, mutation_errors, run_concurrently, DEFAULT_WORKERS

load_dotenv()

logger = logging.getLogger("transfer_orders")
logging.basicConfig(level=logging.INFO)


ADDRESS_FIELDS = "address1 address2 city company countryCodeV2 firstName lastName phone provinceCode zip"

ORDER_NODE_FIELDS = f"""
    id
    name
    email
    phone
    note
    tags
    test
    processedAt
    closedAt
    cancelledAt
    cancelReason
    poNumber
    sourceName
    currencyCode
    displayFinancialStatus
    displayFulfillmentStatus
    discountCodes
    customAttributes {{ key value }}
    shippingAddress {{ {ADDRESS_FIELDS} }}
    billingAddress {{ {ADDRESS_FIELDS} }}
    shippingLine {{
      title
      originalPriceSet {{ shopMoney {{ amount currencyCode }} }}
    }}
    taxLines {{
      title
      rate
      channelLiable
      priceSet {{ shopMoney {{ amount currencyCode }} }}
    }}
    lineItems(first: 250) {{
      edges {{
        node {{
          sku
          title
          variantTitle
          vendor
          quantity
          taxable
          requiresShipping
          originalUnitPriceSet {{ shopMoney {{ amount currencyCode }} }}
        }}
      }}
    }}
    transactions {{
      kind
      status
      gateway
      test
      processedAt
      amountSet {{ shopMoney {{ amount currencyCode }} }}
    }}
    metafields(first: 100) {{
      edges {{ node {{ namespace key value type }} }}
    }}
"""

FULFILLMENT_STATUS_MAP = {
    "PARTIALLY_FULFILLED": "PARTIAL",
    "FULFILLED": "FULFILLED",
    "RESTOCKED": "RESTOCKED",
}


def fetch_all_orders(client, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          orders(first: 100{after_clause}, sortKey: PROCESSED_AT) {{
            edges {{ node {{ {ORDER_NODE_FIELDS} }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    orders = paginate_connection(client, build_query, ("orders",))
    if limit:
        orders = orders[:limit]
    return orders


def export_orders(src_client, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    orders = fetch_all_orders(src_client, limit=limit)
    exported = []
    for o in orders:
        line_items = [
            {
                "sku": e["node"].get("sku"),
                "title": e["node"]["title"],
                "variant_title": e["node"].get("variantTitle"),
                "vendor": e["node"].get("vendor"),
                "quantity": e["node"]["quantity"],
                "taxable": e["node"].get("taxable"),
                "requires_shipping": e["node"].get("requiresShipping"),
                "price": (e["node"].get("originalUnitPriceSet") or {}).get("shopMoney", {}).get("amount"),
                "currency": (e["node"].get("originalUnitPriceSet") or {}).get("shopMoney", {}).get("currencyCode"),
            }
            for e in o["lineItems"]["edges"]
        ]

        transactions = [
            {
                "kind": t["kind"],
                "status": t["status"],
                "gateway": t.get("gateway"),
                "test": t.get("test", False),
                "processed_at": t.get("processedAt"),
                "amount": (t.get("amountSet") or {}).get("shopMoney", {}).get("amount"),
                "currency": (t.get("amountSet") or {}).get("shopMoney", {}).get("currencyCode"),
            }
            for t in o.get("transactions", [])
        ]

        shipping_line = o.get("shippingLine")
        tax_lines = [
            {
                "title": t.get("title"),
                "rate": t.get("rate"),
                "channel_liable": t.get("channelLiable"),
                "amount": (t.get("priceSet") or {}).get("shopMoney", {}).get("amount"),
                "currency": (t.get("priceSet") or {}).get("shopMoney", {}).get("currencyCode"),
            }
            for t in (o.get("taxLines") or [])
        ]

        exported.append(
            {
                "id": o["id"],
                "name": o["name"],
                "email": o.get("email"),
                "phone": o.get("phone"),
                "note": o.get("note"),
                "tags": o.get("tags") or [],
                "test": o.get("test", False),
                "processed_at": o.get("processedAt"),
                "closed_at": o.get("closedAt"),
                "cancelled_at": o.get("cancelledAt"),
                "cancel_reason": o.get("cancelReason"),
                "po_number": o.get("poNumber"),
                "source_name": o.get("sourceName"),
                "discount_codes": o.get("discountCodes") or [],
                "custom_attributes": [
                    {"key": a["key"], "value": a["value"]} for a in (o.get("customAttributes") or [])
                ],
                "currency": o.get("currencyCode"),
                "financial_status": o.get("displayFinancialStatus"),
                "fulfillment_status": o.get("displayFulfillmentStatus"),
                "shipping_address": o.get("shippingAddress"),
                "billing_address": o.get("billingAddress"),
                "shipping_line": {
                    "title": shipping_line.get("title"),
                    "amount": (shipping_line.get("originalPriceSet") or {}).get("shopMoney", {}).get("amount"),
                    "currency": (shipping_line.get("originalPriceSet") or {}).get("shopMoney", {}).get("currencyCode"),
                }
                if shipping_line
                else None,
                "tax_lines": tax_lines,
                "line_items": line_items,
                "transactions": transactions,
                "metafields": export_metafields(o.get("metafields")),
            }
        )

    logger.info("Exported %s order(s)", len(exported))
    return exported


def build_dest_variant_sku_index(dest_client) -> Dict[str, str]:
    """SKU -> destination ProductVariant GID, across the whole store."""

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


def address_input_literal(address: Optional[Dict[str, Any]]) -> Optional[str]:
    if not address:
        return None
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
    fields = []
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
    if not fields:
        return None
    return "{" + ", ".join(fields) + "}"


def build_line_item_literal(item: Dict[str, Any], variant_index: Dict[str, str]) -> str:
    fields = [
        f"title: {gql_quote(item['title'])}",
        f"quantity: {item['quantity']}",
    ]
    if item.get("price") is not None:
        fields.append(
            f'priceSet: {{ shopMoney: {{ amount: {gql_quote(item["price"])}, currencyCode: {item.get("currency") or "USD"} }} }}'
        )
    if item.get("sku"):
        fields.append(f"sku: {gql_quote(item['sku'])}")
        variant_gid = variant_index.get(item["sku"].strip().lower())
        if variant_gid:
            fields.append(f"variantId: {gql_quote(variant_gid)}")
    if item.get("variant_title"):
        fields.append(f"variantTitle: {gql_quote(item['variant_title'])}")
    if item.get("vendor"):
        fields.append(f"vendor: {gql_quote(item['vendor'])}")
    if item.get("taxable") is not None:
        fields.append(f"taxable: {'true' if item['taxable'] else 'false'}")
    if item.get("requires_shipping") is not None:
        fields.append(f"requiresShipping: {'true' if item['requires_shipping'] else 'false'}")
    return "{" + ", ".join(fields) + "}"


def build_shipping_lines_literal(shipping_line: Optional[Dict[str, Any]]) -> Optional[str]:
    if not shipping_line or not shipping_line.get("title"):
        return None
    fields = [f"title: {gql_quote(shipping_line['title'])}"]
    if shipping_line.get("amount") is not None:
        fields.append(
            f'priceSet: {{ shopMoney: {{ amount: {gql_quote(shipping_line["amount"])}, '
            f'currencyCode: {shipping_line.get("currency") or "USD"} }} }}'
        )
    return "[{" + ", ".join(fields) + "}]"


def build_tax_lines_literal(tax_lines: List[Dict[str, Any]]) -> Optional[str]:
    entries = []
    for t in tax_lines:
        if t.get("amount") is None:
            continue
        fields = [
            f"title: {gql_quote(t.get('title'))}",
            f'priceSet: {{ shopMoney: {{ amount: {gql_quote(t["amount"])}, currencyCode: {t.get("currency") or "USD"} }} }}',
        ]
        if t.get("rate") is not None:
            fields.append(f"rate: {t['rate']}")
        if t.get("channel_liable") is not None:
            fields.append(f"channelLiable: {'true' if t['channel_liable'] else 'false'}")
        entries.append("{" + ", ".join(fields) + "}")
    if not entries:
        return None
    return "[" + ", ".join(entries) + "]"


def build_custom_attributes_literal(order: Dict[str, Any]) -> Optional[str]:
    attrs = list(order.get("custom_attributes") or [])
    if order.get("discount_codes"):
        # Order.discountCodes only exposes the code text, not its type/value, so it
        # can't be reconstructed as a real OrderCreateDiscountCodeInput. Preserve it
        # as an attribute instead of guessing at (and misrepresenting) the discount.
        attrs.append({"key": "original_discount_codes", "value": ", ".join(order["discount_codes"])})
    if not attrs:
        return None
    return "[" + ", ".join(f'{{ key: {gql_quote(a["key"])}, value: {gql_quote(a["value"])} }}' for a in attrs) + "]"


def build_transaction_literal(txn: Dict[str, Any]) -> Optional[str]:
    if txn.get("kind") not in ("SALE", "CAPTURE") or txn.get("status") != "SUCCESS":
        return None
    if txn.get("amount") is None:
        return None
    fields = [
        f"kind: {txn['kind']}",
        "status: SUCCESS",
        f'gateway: {gql_quote("manual")}',
        f'amountSet: {{ shopMoney: {{ amount: {gql_quote(txn["amount"])}, currencyCode: {txn.get("currency") or "USD"} }} }}',
        f"test: {'true' if txn.get('test') else 'false'}",
    ]
    if txn.get("processed_at"):
        fields.append(f"processedAt: {gql_quote(txn['processed_at'])}")
    return "{" + ", ".join(fields) + "}"


def import_orders(
    dest_client, exported: List[Dict[str, Any]], replace_existing: bool = False, max_workers: int = DEFAULT_WORKERS
) -> None:
    """Create each exported order on the destination, up to max_workers concurrently.

    By default, an order whose name already exists on the destination is left
    alone and skipped. With replace_existing=True, it's deleted and recreated
    instead -- use this after enhancing what fields get captured/set, so
    orders created by an earlier, thinner version of this script get the full
    detail set too.

    existing_by_name is built once up front and only ever read (never mutated)
    inside process_order, so concurrent workers can't race on it -- each order
    is independent, matched/created by its own unique name.
    """
    variant_index = build_dest_variant_sku_index(dest_client)
    existing_by_name = {o["name"]: o["id"] for o in fetch_all_orders(dest_client)}

    counts = {"created": 0, "skipped": 0, "failed": 0, "deleted": 0}
    counts_lock = threading.Lock()

    def record(key: str) -> None:
        with counts_lock:
            counts[key] += 1

    def process_order(order: Dict[str, Any]) -> None:
        existing_id = existing_by_name.get(order["name"])
        if existing_id and not replace_existing:
            logger.info("Order %s already exists on destination; skipping", order["name"])
            record("skipped")
            return

        if existing_id and replace_existing:
            try:
                retry_with_backoff(lambda: dest_client.mutation(f'mutation {{ orderDelete(orderId: {gql_quote(existing_id)}) {{ deletedId userErrors {{ field message }} }} }}'))
                record("deleted")
            except Exception as e:
                logger.warning("Failed to delete existing order %s before replacing it: %s -- skipping", order["name"], e)
                record("failed")
                return

        line_items_literal = "[" + ", ".join(build_line_item_literal(li, variant_index) for li in order["line_items"]) + "]"
        transactions = [t for t in (build_transaction_literal(t) for t in order["transactions"]) if t]
        tags_literal = "[" + ", ".join(gql_quote(t) for t in order.get("tags", [])) + "]"

        fields = [
            # Without an explicit name, Shopify assigns the destination's own next
            # sequential order number instead of preserving the source's -- which
            # also breaks the "does this order already exist" dedup check above,
            # since it can never match a destination-assigned name to a source one.
            f"name: {gql_quote(order['name'])}",
            f"email: {gql_quote(order.get('email'))}",
            f"currency: {order.get('currency') or 'USD'}",
            f"lineItems: {line_items_literal}",
            f"tags: {tags_literal}",
            f"test: {'true' if order.get('test') else 'false'}",
        ]
        if order.get("processed_at"):
            fields.append(f"processedAt: {gql_quote(order['processed_at'])}")
        if order.get("note"):
            fields.append(f"note: {gql_quote(order['note'])}")
        if order.get("financial_status"):
            fields.append(f"financialStatus: {order['financial_status']}")
        mapped_fulfillment = FULFILLMENT_STATUS_MAP.get(order.get("fulfillment_status"))
        if mapped_fulfillment:
            fields.append(f"fulfillmentStatus: {mapped_fulfillment}")
        if order.get("email"):
            fields.append(f"customer: {{ toAssociate: {{ email: {gql_quote(order['email'])} }} }}")
        billing = address_input_literal(order.get("billing_address"))
        if billing:
            fields.append(f"billingAddress: {billing}")
        shipping = address_input_literal(order.get("shipping_address"))
        if shipping:
            fields.append(f"shippingAddress: {shipping}")
        if transactions:
            fields.append("transactions: [" + ", ".join(transactions) + "]")
        if order.get("po_number"):
            fields.append(f"poNumber: {gql_quote(order['po_number'])}")
        # sourceName is intentionally not set here: Shopify rejects most real
        # values (e.g. "web") as "protected values" that untrusted/custom API
        # clients can't assign -- the source_name is still preserved in the
        # export JSON, just not replayed on creation.
        if order.get("closed_at"):
            fields.append(f"closedAt: {gql_quote(order['closed_at'])}")
        shipping_lines = build_shipping_lines_literal(order.get("shipping_line"))
        if shipping_lines:
            fields.append(f"shippingLines: {shipping_lines}")
        tax_lines = build_tax_lines_literal(order.get("tax_lines") or [])
        if tax_lines:
            fields.append(f"taxLines: {tax_lines}")
        custom_attributes = build_custom_attributes_literal(order)
        if custom_attributes:
            fields.append(f"customAttributes: {custom_attributes}")

        mutation = f"""
        mutation {{
          orderCreate(order: {{ {", ".join(fields)} }}, options: {{ inventoryBehaviour: BYPASS, sendReceipt: false, sendFulfillmentReceipt: false }}) {{
            order {{ id name }}
            userErrors {{ field message }}
          }}
        }}
        """

        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        except Exception as e:
            # A hard GraphQL error (bad enum literal, malformed input, etc.) raises
            # here instead of populating userErrors -- skip just this order rather
            # than losing the rest of the run.
            logger.warning("Failed to create order %s: %s", order["name"], e)
            record("failed")
            return

        errors = mutation_errors(result, "orderCreate")
        if errors:
            logger.warning("Failed to create order %s: %s", order["name"], errors)
            record("failed")
            return

        order_gid = result["orderCreate"]["order"]["id"]
        logger.info("Created order %s", order["name"])
        record("created")

        if order.get("cancelled_at"):
            cancel_mutation = f"""
            mutation {{
              orderCancel(
                orderId: {gql_quote(order_gid)}
                reason: {order.get("cancel_reason") or "OTHER"}
                restock: false
                notifyCustomer: false
                staffNote: {gql_quote("Cancelled on source store (Src) at " + str(order.get("cancelled_at")))}
              ) {{
                job {{ id }}
                userErrors {{ field message }}
              }}
            }}
            """
            try:
                cancel_result = retry_with_backoff(lambda: dest_client.mutation(cancel_mutation))
                cancel_errors = mutation_errors(cancel_result, "orderCancel")
                if cancel_errors:
                    logger.warning("Order %s created but failed to mark as cancelled: %s", order["name"], cancel_errors)
            except Exception as e:
                logger.warning("Order %s created but failed to mark as cancelled: %s", order["name"], e)

        if order.get("metafields"):
            set_metafields(dest_client, order_gid, order["metafields"])

    logger.info("Using %s worker(s)", max_workers)
    run_concurrently(exported, process_order, max_workers=max_workers, label="order")

    logger.info(
        "Orders import complete: %s created, %s already existed, %s replaced (deleted+recreated), %s failed",
        counts["created"],
        counts["skipped"],
        counts["deleted"],
        counts["failed"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer historical orders from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create orders on the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument("--limit", type=int, default=None, help="Only transfer the first N orders (oldest first)")
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Delete and recreate orders that already exist on the destination (e.g. to backfill fields added since they were first created)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of orders to import concurrently (default {DEFAULT_WORKERS}; 1 = sequential)",
    )
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

    logger.info("Exporting orders from %s", src_shop)
    exported = export_orders(src_client, limit=args.limit)

    ts = int(time.time())
    out_file = out_dir / f"orders_export_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(exported, f, indent=2, ensure_ascii=False)
    logger.info("Export complete: %s -- REVIEW THIS FILE before running --execute", out_file)

    if args.execute:
        logger.info("Importing orders into %s", dest_shop)
        import_orders(dest_client, exported, replace_existing=args.replace_existing, max_workers=args.workers)
    else:
        logger.info("Dry-run finished. Re-run with --execute to create orders on the destination store")


if __name__ == "__main__":
    main()
