"""Transfer discounts from Src to dest.

Covers basic percentage/fixed-amount off (code and automatic), free shipping
(code and automatic), and Buy X Get Y / Bxgy (code and automatic). NOT
covered:
- App-owned discounts (DiscountCodeApp/DiscountAutomaticApp) -- tied to
  whatever app created them; only meaningful if that same app is installed
  on the destination, since it owns the discount's actual logic.

By default only ACTIVE/SCHEDULED discounts are transferred. Src has
~10,000 discount nodes, the overwhelming majority of which are expired,
auto-generated single-use affiliate codes (e.g. "KEVIN240") from a referral
app -- migrating those would be pure noise. Pass --include-expired to widen
that if you actually want the full historical set.

Requires read_discounts/write_discounts scope on both stores.

Usage:
    python transfer_discounts.py                      # dry-run export, active/scheduled only
    python transfer_discounts.py --execute             # create on dest
    python transfer_discounts.py --include-expired --execute
"""
import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from Transfer.transfer_product import make_client, fetch_all_product_handles
from Transfer.transfer_collections import fetch_all_collections
from Transfer.transfer_store_metafields import retry_with_backoff, gql_quote, set_metafields
from utils.shopify_graphql_utils import paginate_connection, mutation_errors, export_metafields

load_dotenv()

logger = logging.getLogger("transfer_discounts")
logging.basicConfig(level=logging.INFO)

DISCOUNT_FRAGMENTS = """
  __typename
  ... on DiscountCodeBasic {
    title status startsAt endsAt appliesOncePerCustomer usageLimit recurringCycleLimit
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
    codes(first: 5) { nodes { code } }
    customerGets {
      appliesOnOneTimePurchase appliesOnSubscription
      value {
        __typename
        ... on DiscountPercentage { percentage }
        ... on DiscountAmount { amount { amount currencyCode } appliesOnEachItem }
      }
      items {
        __typename
        ... on AllDiscountItems { allItems }
        ... on DiscountProducts { products(first: 100) { nodes { handle } } }
        ... on DiscountCollections { collections(first: 100) { nodes { handle } } }
      }
    }
    minimumRequirement {
      __typename
      ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity }
      ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount currencyCode } }
    }
  }
  ... on DiscountAutomaticBasic {
    title status startsAt endsAt recurringCycleLimit
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
    customerGets {
      appliesOnOneTimePurchase appliesOnSubscription
      value {
        __typename
        ... on DiscountPercentage { percentage }
        ... on DiscountAmount { amount { amount currencyCode } appliesOnEachItem }
      }
      items {
        __typename
        ... on AllDiscountItems { allItems }
        ... on DiscountProducts { products(first: 100) { nodes { handle } } }
        ... on DiscountCollections { collections(first: 100) { nodes { handle } } }
      }
    }
    minimumRequirement {
      __typename
      ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity }
      ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount currencyCode } }
    }
  }
  ... on DiscountCodeFreeShipping {
    title status startsAt endsAt appliesOncePerCustomer usageLimit recurringCycleLimit
    appliesOnOneTimePurchase appliesOnSubscription maximumShippingPrice { amount currencyCode }
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
    codes(first: 5) { nodes { code } }
    minimumRequirement {
      __typename
      ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity }
      ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount currencyCode } }
    }
  }
  ... on DiscountAutomaticFreeShipping {
    title status startsAt endsAt recurringCycleLimit
    appliesOnOneTimePurchase appliesOnSubscription maximumShippingPrice { amount currencyCode }
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
    minimumRequirement {
      __typename
      ... on DiscountMinimumQuantity { greaterThanOrEqualToQuantity }
      ... on DiscountMinimumSubtotal { greaterThanOrEqualToSubtotal { amount currencyCode } }
    }
  }
  ... on DiscountCodeBxgy {
    title status startsAt endsAt appliesOncePerCustomer usageLimit usesPerOrderLimit recurringCycleLimit
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
    codes(first: 5) { nodes { code } }
    customerBuys {
      value {
        __typename
        ... on DiscountQuantity { quantity }
        ... on DiscountPurchaseAmount { amount }
      }
      items {
        __typename
        ... on AllDiscountItems { allItems }
        ... on DiscountProducts { products(first: 100) { nodes { handle } } }
        ... on DiscountCollections { collections(first: 100) { nodes { handle } } }
      }
    }
    customerGets {
      value {
        __typename
        ... on DiscountOnQuantity {
          quantity
          effect {
            __typename
            ... on DiscountPercentage { percentage }
            ... on DiscountAmount { amount { amount currencyCode } }
          }
        }
      }
      items {
        __typename
        ... on AllDiscountItems { allItems }
        ... on DiscountProducts { products(first: 100) { nodes { handle } } }
        ... on DiscountCollections { collections(first: 100) { nodes { handle } } }
      }
    }
  }
  ... on DiscountAutomaticBxgy {
    title status startsAt endsAt usesPerOrderLimit recurringCycleLimit
    combinesWith { orderDiscounts productDiscounts shippingDiscounts }
    customerBuys {
      value {
        __typename
        ... on DiscountQuantity { quantity }
        ... on DiscountPurchaseAmount { amount }
      }
      items {
        __typename
        ... on AllDiscountItems { allItems }
        ... on DiscountProducts { products(first: 100) { nodes { handle } } }
        ... on DiscountCollections { collections(first: 100) { nodes { handle } } }
      }
    }
    customerGets {
      value {
        __typename
        ... on DiscountOnQuantity {
          quantity
          effect {
            __typename
            ... on DiscountPercentage { percentage }
            ... on DiscountAmount { amount { amount currencyCode } }
          }
        }
      }
      items {
        __typename
        ... on AllDiscountItems { allItems }
        ... on DiscountProducts { products(first: 100) { nodes { handle } } }
        ... on DiscountCollections { collections(first: 100) { nodes { handle } } }
      }
    }
  }
"""

SUPPORTED_TYPENAMES = {
    "DiscountCodeBasic",
    "DiscountAutomaticBasic",
    "DiscountCodeFreeShipping",
    "DiscountAutomaticFreeShipping",
    "DiscountCodeBxgy",
    "DiscountAutomaticBxgy",
}


def fetch_discount_nodes(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          discountNodes(first: 100{after_clause}) {{
            edges {{
              node {{
                id
                metafields(first: 50) {{ edges {{ node {{ namespace key value type }} }} }}
                discount {{ {DISCOUNT_FRAGMENTS} }}
              }}
            }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("discountNodes",))


def normalize_minimum_requirement(mr: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not mr:
        return None
    if mr["__typename"] == "DiscountMinimumQuantity":
        return {"kind": "quantity", "value": mr["greaterThanOrEqualToQuantity"]}
    if mr["__typename"] == "DiscountMinimumSubtotal":
        return {
            "kind": "subtotal",
            "amount": mr["greaterThanOrEqualToSubtotal"]["amount"],
            "currency": mr["greaterThanOrEqualToSubtotal"]["currencyCode"],
        }
    return None


def normalize_discount_items(items: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if items and items.get("__typename") == "DiscountProducts":
        return {"kind": "products", "handles": [n["handle"] for n in items["products"]["nodes"]]}
    if items and items.get("__typename") == "DiscountCollections":
        return {"kind": "collections", "handles": [n["handle"] for n in items["collections"]["nodes"]]}
    return {"kind": "all"}


def normalize_customer_buys(cb: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not cb:
        return None
    value = cb.get("value") or {}
    if value.get("__typename") == "DiscountQuantity":
        value_out = {"kind": "quantity", "quantity": value["quantity"]}
    elif value.get("__typename") == "DiscountPurchaseAmount":
        value_out = {"kind": "amount", "amount": value["amount"]}
    else:
        value_out = None

    return {"value": value_out, "items": normalize_discount_items(cb.get("items"))}


def normalize_customer_gets_bxgy(cg: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Bxgy's customerGets.value is DiscountOnQuantity (quantity + a nested
    percentage/amount effect) -- a different shape from the basic discount's
    customerGets.value (a bare DiscountPercentage/DiscountAmount)."""
    if not cg:
        return None
    value = cg.get("value") or {}
    if value.get("__typename") != "DiscountOnQuantity":
        return None

    effect = value.get("effect") or {}
    if effect.get("__typename") == "DiscountPercentage":
        effect_out = {"kind": "percentage", "percentage": effect["percentage"]}
    elif effect.get("__typename") == "DiscountAmount":
        effect_out = {"kind": "amount", "amount": effect["amount"]["amount"], "currency": effect["amount"]["currencyCode"]}
    else:
        effect_out = None

    return {
        "quantity": value.get("quantity"),
        "effect": effect_out,
        "items": normalize_discount_items(cg.get("items")),
    }


def normalize_customer_gets(cg: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not cg:
        return None
    value = cg.get("value") or {}
    items = cg.get("items") or {}

    if value.get("__typename") == "DiscountPercentage":
        value_out = {"kind": "percentage", "percentage": value["percentage"]}
    elif value.get("__typename") == "DiscountAmount":
        value_out = {
            "kind": "amount",
            "amount": value["amount"]["amount"],
            "currency": value["amount"]["currencyCode"],
            "applies_on_each_item": value.get("appliesOnEachItem", False),
        }
    else:
        value_out = None

    if items.get("__typename") == "AllDiscountItems":
        items_out = {"kind": "all"}
    elif items.get("__typename") == "DiscountProducts":
        items_out = {"kind": "products", "handles": [n["handle"] for n in items["products"]["nodes"]]}
    elif items.get("__typename") == "DiscountCollections":
        items_out = {"kind": "collections", "handles": [n["handle"] for n in items["collections"]["nodes"]]}
    else:
        items_out = {"kind": "all"}

    return {
        "value": value_out,
        "items": items_out,
        "applies_on_one_time_purchase": cg.get("appliesOnOneTimePurchase", True),
        "applies_on_subscription": cg.get("appliesOnSubscription", False),
    }


def export_discounts(client, include_expired: bool = False) -> Dict[str, Any]:
    nodes = fetch_discount_nodes(client)

    exported: List[Dict[str, Any]] = []
    skipped_unsupported = 0
    skipped_expired = 0

    for node in nodes:
        d = node["discount"]
        typename = d.get("__typename")

        if typename not in SUPPORTED_TYPENAMES:
            skipped_unsupported += 1
            continue

        if not include_expired and d.get("status") not in ("ACTIVE", "SCHEDULED"):
            skipped_expired += 1
            continue

        codes = [c["code"] for c in d.get("codes", {}).get("nodes", [])] if "codes" in d else []
        metafields = export_metafields(node.get("metafields"))

        exported.append(
            {
                "typename": typename,
                "metafields": metafields,
                "title": d.get("title"),
                "status": d.get("status"),
                "starts_at": d.get("startsAt"),
                "ends_at": d.get("endsAt"),
                "applies_once_per_customer": d.get("appliesOncePerCustomer"),
                "usage_limit": d.get("usageLimit"),
                "recurring_cycle_limit": d.get("recurringCycleLimit"),
                "combines_with": d.get("combinesWith"),
                "code": codes[0] if codes else None,
                "customer_gets": normalize_customer_gets(d.get("customerGets")) if typename in ("DiscountCodeBasic", "DiscountAutomaticBasic") else None,
                "minimum_requirement": normalize_minimum_requirement(d.get("minimumRequirement")),
                "applies_on_one_time_purchase": d.get("appliesOnOneTimePurchase"),
                "applies_on_subscription": d.get("appliesOnSubscription"),
                "maximum_shipping_price": (d.get("maximumShippingPrice") or {}).get("amount"),
                "uses_per_order_limit": d.get("usesPerOrderLimit"),
                "customer_buys": normalize_customer_buys(d.get("customerBuys")) if typename in ("DiscountCodeBxgy", "DiscountAutomaticBxgy") else None,
                "customer_gets_bxgy": normalize_customer_gets_bxgy(d.get("customerGets")) if typename in ("DiscountCodeBxgy", "DiscountAutomaticBxgy") else None,
            }
        )

    logger.info(
        "Exported %s discount(s) (%s skipped as unsupported type, %s skipped as expired/inactive)",
        len(exported),
        skipped_unsupported,
        skipped_expired,
    )
    return {"discounts": exported}


def combines_with_literal(cw: Optional[Dict[str, Any]]) -> str:
    cw = cw or {}
    return (
        "{"
        f"orderDiscounts: {'true' if cw.get('orderDiscounts') else 'false'}, "
        f"productDiscounts: {'true' if cw.get('productDiscounts') else 'false'}, "
        f"shippingDiscounts: {'true' if cw.get('shippingDiscounts') else 'false'}"
        "}"
    )


def minimum_requirement_literal(mr: Optional[Dict[str, Any]]) -> Optional[str]:
    if not mr:
        return None
    if mr["kind"] == "quantity":
        return f"{{ quantity: {{ greaterThanOrEqualToQuantity: {mr['value']} }} }}"
    if mr["kind"] == "subtotal":
        return (
            "{ subtotal: { greaterThanOrEqualToSubtotal: "
            f"{gql_quote(mr['amount'])} }} }}"
        )
    return None


def resolve_items_literal(
    items: Dict[str, Any],
    product_handle_to_gid: Dict[str, str],
    collection_handle_to_gid: Dict[str, str],
    products_field: str = "products",
    products_key: str = "productsToAdd",
    collections_key: str = "add",
) -> str:
    """Build the DiscountItemsInput literal (all/products/collections) shared by
    basic customerGets and Bxgy's customerBuys/customerGets. `products_field`
    lets Bxgy's customerBuys use a differently-named wrapper if needed."""
    if items["kind"] == "products":
        gids = [product_handle_to_gid[h] for h in items["handles"] if h in product_handle_to_gid]
        if not gids:
            logger.warning("No matching destination products found for targeted discount -- applying to all items instead")
            return "{ all: true }"
        gid_list = ", ".join(gql_quote(g) for g in gids)
        return f"{{ {products_field}: {{ {products_key}: [{gid_list}] }} }}"
    if items["kind"] == "collections":
        gids = [collection_handle_to_gid[h] for h in items["handles"] if h in collection_handle_to_gid]
        if not gids:
            logger.warning("No matching destination collections found for targeted discount -- applying to all items instead")
            return "{ all: true }"
        gid_list = ", ".join(gql_quote(g) for g in gids)
        return f"{{ collections: {{ {collections_key}: [{gid_list}] }} }}"
    return "{ all: true }"


def customer_buys_literal(
    cb: Optional[Dict[str, Any]],
    product_handle_to_gid: Dict[str, str],
    collection_handle_to_gid: Dict[str, str],
) -> Optional[str]:
    if not cb or not cb.get("value"):
        return None

    value = cb["value"]
    if value["kind"] == "quantity":
        value_literal = f"{{ quantity: {gql_quote(str(value['quantity']))} }}"
    else:
        value_literal = f"{{ amount: {gql_quote(value['amount'])} }}"

    items_literal = resolve_items_literal(cb["items"], product_handle_to_gid, collection_handle_to_gid)
    return f"{{ value: {value_literal}, items: {items_literal} }}"


def customer_gets_bxgy_literal(
    cg: Optional[Dict[str, Any]],
    product_handle_to_gid: Dict[str, str],
    collection_handle_to_gid: Dict[str, str],
) -> Optional[str]:
    if not cg or not cg.get("effect") or cg.get("quantity") is None:
        return None

    effect = cg["effect"]
    if effect["kind"] == "percentage":
        effect_literal = f"{{ percentage: {effect['percentage']} }}"
    else:
        effect_literal = f"{{ discountAmount: {{ amount: {gql_quote(effect['amount'])} }} }}"

    items_literal = resolve_items_literal(cg["items"], product_handle_to_gid, collection_handle_to_gid)
    value_literal = f"{{ discountOnQuantity: {{ quantity: {gql_quote(str(cg['quantity']))}, effect: {effect_literal} }} }}"
    return f"{{ value: {value_literal}, items: {items_literal} }}"


def customer_gets_literal(
    cg: Optional[Dict[str, Any]],
    product_handle_to_gid: Dict[str, str],
    collection_handle_to_gid: Dict[str, str],
) -> Optional[str]:
    if not cg or not cg.get("value"):
        return None

    value = cg["value"]
    if value["kind"] == "percentage":
        value_literal = f"{{ percentage: {value['percentage']} }}"
    else:
        value_literal = (
            "{ discountAmount: { amount: "
            f"{gql_quote(value['amount'])}, appliesOnEachItem: "
            f"{'true' if value.get('applies_on_each_item') else 'false'} }} }}"
        )

    items_literal = resolve_items_literal(cg["items"], product_handle_to_gid, collection_handle_to_gid)

    # appliesOnOneTimePurchase/appliesOnSubscription must be omitted entirely (not
    # even set to false) on a shop that doesn't have subscriptions enabled --
    # including them at all causes Shopify to reject the whole mutation. Only
    # emit them when the source discount was actually subscription-aware.
    subscription_fields = ""
    if cg.get("applies_on_subscription"):
        subscription_fields = (
            f", appliesOnOneTimePurchase: {'true' if cg.get('applies_on_one_time_purchase', True) else 'false'}"
            f", appliesOnSubscription: true"
        )

    return "{" f" value: {value_literal}, items: {items_literal}{subscription_fields}" "}"


def common_fields_literal(d: Dict[str, Any], include_code_fields: bool) -> List[str]:
    # context is required on every discount input type; DiscountBuyerSelection has
    # exactly one enum value (ALL), so this is the only valid non-empty context.
    fields = [
        f"title: {gql_quote(d.get('title'))}",
        f"combinesWith: {combines_with_literal(d.get('combines_with'))}",
        "context: { all: ALL }",
    ]
    if d.get("starts_at"):
        fields.append(f"startsAt: {gql_quote(d['starts_at'])}")
    if d.get("ends_at"):
        fields.append(f"endsAt: {gql_quote(d['ends_at'])}")
    if include_code_fields:
        if d.get("code"):
            fields.append(f"code: {gql_quote(d['code'])}")
        if d.get("applies_once_per_customer") is not None:
            fields.append(f"appliesOncePerCustomer: {'true' if d['applies_once_per_customer'] else 'false'}")
        if d.get("usage_limit") is not None:
            fields.append(f"usageLimit: {d['usage_limit']}")
    return fields


def import_discounts(dest_client, exported: Dict[str, Any]) -> None:
    product_handle_to_gid = {
        (p.get("handle") or "").strip().lower(): f"gid://shopify/Product/{p['id']}"
        for p in fetch_all_product_handles(dest_client)
        if p.get("handle")
    }
    collection_handle_to_gid = {
        (c.get("handle") or "").strip().lower(): f"gid://shopify/Collection/{c['id']}"
        for c in fetch_all_collections(dest_client, "custom_collections") + fetch_all_collections(dest_client, "smart_collections")
        if c.get("handle")
    }

    existing_titles = set()
    existing_codes = set()
    for node in fetch_discount_nodes(dest_client):
        d = node["discount"]
        if d.get("title"):
            existing_titles.add(d["title"].strip().lower())
        for c in d.get("codes", {}).get("nodes", []) if "codes" in d else []:
            existing_codes.add(c["code"].strip().lower())

    created = 0
    skipped_existing = 0
    failed = 0

    for d in exported.get("discounts", []):
        dedupe_key = (d.get("code") or d.get("title") or "").strip().lower()
        is_code_discount = d["typename"] in ("DiscountCodeBasic", "DiscountCodeFreeShipping")
        already_exists = (d.get("code") or "").strip().lower() in existing_codes if is_code_discount else dedupe_key in existing_titles
        if already_exists:
            skipped_existing += 1
            continue

        if d["typename"] == "DiscountCodeBasic":
            customer_gets = customer_gets_literal(d.get("customer_gets"), product_handle_to_gid, collection_handle_to_gid)
            if not customer_gets:
                logger.warning("Skipping code discount '%s': no usable customerGets value", d.get("title") or d.get("code"))
                failed += 1
                continue
            fields = common_fields_literal(d, include_code_fields=True)
            fields.append(f"customerGets: {customer_gets}")
            min_req = minimum_requirement_literal(d.get("minimum_requirement"))
            if min_req:
                fields.append(f"minimumRequirement: {min_req}")
            mutation = f"""
            mutation {{
              discountCodeBasicCreate(basicCodeDiscount: {{ {", ".join(fields)} }}) {{
                codeDiscountNode {{ id }}
                userErrors {{ field message }}
              }}
            }}
            """
            mutation_name = "discountCodeBasicCreate"

        elif d["typename"] == "DiscountAutomaticBasic":
            customer_gets = customer_gets_literal(d.get("customer_gets"), product_handle_to_gid, collection_handle_to_gid)
            if not customer_gets:
                logger.warning("Skipping automatic discount '%s': no usable customerGets value", d.get("title"))
                failed += 1
                continue
            fields = common_fields_literal(d, include_code_fields=False)
            fields.append(f"customerGets: {customer_gets}")
            min_req = minimum_requirement_literal(d.get("minimum_requirement"))
            if min_req:
                fields.append(f"minimumRequirement: {min_req}")
            mutation = f"""
            mutation {{
              discountAutomaticBasicCreate(automaticBasicDiscount: {{ {", ".join(fields)} }}) {{
                automaticDiscountNode {{ id }}
                userErrors {{ field message }}
              }}
            }}
            """
            mutation_name = "discountAutomaticBasicCreate"

        elif d["typename"] == "DiscountCodeFreeShipping":
            fields = common_fields_literal(d, include_code_fields=True)
            fields.append("destination: { all: true }")
            if d.get("maximum_shipping_price") is not None:
                fields.append(f"maximumShippingPrice: {gql_quote(d['maximum_shipping_price'])}")
            min_req = minimum_requirement_literal(d.get("minimum_requirement"))
            if min_req:
                fields.append(f"minimumRequirement: {min_req}")
            mutation = f"""
            mutation {{
              discountCodeFreeShippingCreate(freeShippingCodeDiscount: {{ {", ".join(fields)} }}) {{
                codeDiscountNode {{ id }}
                userErrors {{ field message }}
              }}
            }}
            """
            mutation_name = "discountCodeFreeShippingCreate"

        elif d["typename"] == "DiscountAutomaticFreeShipping":
            fields = common_fields_literal(d, include_code_fields=False)
            fields.append("destination: { all: true }")
            if d.get("maximum_shipping_price") is not None:
                fields.append(f"maximumShippingPrice: {gql_quote(d['maximum_shipping_price'])}")
            min_req = minimum_requirement_literal(d.get("minimum_requirement"))
            if min_req:
                fields.append(f"minimumRequirement: {min_req}")
            mutation = f"""
            mutation {{
              discountAutomaticFreeShippingCreate(freeShippingAutomaticDiscount: {{ {", ".join(fields)} }}) {{
                automaticDiscountNode {{ id }}
                userErrors {{ field message }}
              }}
            }}
            """
            mutation_name = "discountAutomaticFreeShippingCreate"

        elif d["typename"] == "DiscountCodeBxgy":
            customer_buys = customer_buys_literal(d.get("customer_buys"), product_handle_to_gid, collection_handle_to_gid)
            customer_gets = customer_gets_bxgy_literal(d.get("customer_gets_bxgy"), product_handle_to_gid, collection_handle_to_gid)
            if not customer_buys or not customer_gets:
                logger.warning("Skipping Bxgy code discount '%s': missing usable customerBuys/customerGets", d.get("title") or d.get("code"))
                failed += 1
                continue
            fields = common_fields_literal(d, include_code_fields=True)
            fields.append(f"customerBuys: {customer_buys}")
            fields.append(f"customerGets: {customer_gets}")
            if d.get("uses_per_order_limit") is not None:
                fields.append(f"usesPerOrderLimit: {d['uses_per_order_limit']}")
            mutation = f"""
            mutation {{
              discountCodeBxgyCreate(codeDiscount: {{ {", ".join(fields)} }}) {{
                codeDiscountNode {{ id }}
                userErrors {{ field message }}
              }}
            }}
            """
            mutation_name = "discountCodeBxgyCreate"

        else:  # DiscountAutomaticBxgy
            customer_buys = customer_buys_literal(d.get("customer_buys"), product_handle_to_gid, collection_handle_to_gid)
            customer_gets = customer_gets_bxgy_literal(d.get("customer_gets_bxgy"), product_handle_to_gid, collection_handle_to_gid)
            if not customer_buys or not customer_gets:
                logger.warning("Skipping Bxgy automatic discount '%s': missing usable customerBuys/customerGets", d.get("title"))
                failed += 1
                continue
            fields = common_fields_literal(d, include_code_fields=False)
            fields.append(f"customerBuys: {customer_buys}")
            fields.append(f"customerGets: {customer_gets}")
            if d.get("uses_per_order_limit") is not None:
                fields.append(f"usesPerOrderLimit: {d['uses_per_order_limit']}")
            mutation = f"""
            mutation {{
              discountAutomaticBxgyCreate(automaticBxgyDiscount: {{ {", ".join(fields)} }}) {{
                automaticDiscountNode {{ id }}
                userErrors {{ field message }}
              }}
            }}
            """
            mutation_name = "discountAutomaticBxgyCreate"

        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        except Exception as e:
            logger.warning("Failed to create discount '%s': %s", d.get("title") or d.get("code"), e)
            failed += 1
            continue

        errors = mutation_errors(result, mutation_name)
        if errors:
            logger.warning("Failed to create discount '%s': %s", d.get("title") or d.get("code"), errors)
            failed += 1
            continue

        logger.info("Created discount '%s'", d.get("title") or d.get("code"))
        created += 1

        if d.get("metafields"):
            payload = result.get(mutation_name) or {}
            new_gid = (payload.get("codeDiscountNode") or payload.get("automaticDiscountNode") or {}).get("id")
            if new_gid:
                set_metafields(dest_client, new_gid, d["metafields"])

    logger.info(
        "Discounts import complete: %s created, %s already existed, %s failed",
        created,
        skipped_existing,
        failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer discounts from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create discounts on the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument(
        "--include-expired",
        action="store_true",
        help="Also transfer expired/disabled discounts (Src has ~10,000 mostly-expired affiliate codes; off by default)",
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

    logger.info("Exporting discounts from %s", src_shop)
    exported = export_discounts(src_client, include_expired=args.include_expired)

    ts = int(time.time())
    out_file = out_dir / f"discounts_export_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(exported, f, indent=2, ensure_ascii=False)
    logger.info("Export complete: %s (%s discounts)", out_file, len(exported["discounts"]))

    if args.execute:
        logger.info("Importing discounts into %s", dest_shop)
        import_discounts(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to create discounts on the destination store")


if __name__ == "__main__":
    main()
