import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from transfer.transfer_product import make_client, fetch_all_product_handles
from transfer.transfer_store_metafields import gql_quote, retry_with_backoff
from utils.shopify_graphql_utils import paginate_connection, mutation_errors
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_selling_plans")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


_app_purchase_type_support: Dict[int, bool] = {}


def _supports_app_purchase_type(client) -> bool:
    key = id(client)
    if key in _app_purchase_type_support:
        return _app_purchase_type_support[key]

    probe = "{ sellingPlanGroups(first: 1) { edges { node { id appPurchaseType } } } }"
    try:
        retry_with_backoff(lambda: client.query(probe))
        supported = True
    except Exception as e:
        logger.warning(
            "sellingPlanGroups.appPurchaseType is not queryable on this store/API version (%s) -- "
            "app-owned selling plan groups can't be auto-detected/skipped for this run; "
            "dropping that field from the query.",
            e,
        )
        supported = False

    _app_purchase_type_support[key] = supported
    return supported


def _selling_plan_group_node_fields(include_app_purchase_type: bool) -> str:
    app_field = "appPurchaseType" if include_app_purchase_type else ""
    return f"""
        id
        name
        merchantCode
        description
        options
        {app_field}
        sellingPlans(first: 50) {{
          edges {{
            node {{
              id
              name
              description
              billingPolicy {{
                __typename
                ... on SellingPlanRecurringBillingPolicy {{ interval intervalCount }}
              }}
              deliveryPolicy {{
                __typename
                ... on SellingPlanRecurringDeliveryPolicy {{ interval intervalCount }}
              }}
              pricingPolicies {{
                __typename
                ... on SellingPlanFixedPricingPolicy {{
                  adjustmentType
                  adjustmentValue {{
                    __typename
                    ... on SellingPlanPricingPolicyPercentageValue {{ percentage }}
                    ... on MoneyV2 {{ amount currencyCode }}
                  }}
                }}
              }}
            }}
          }}
        }}
        products(first: 50) {{ edges {{ node {{ handle }} }} }}
    """


def fetch_selling_plan_groups(client) -> List[Dict[str, Any]]:
    include_app_purchase_type = _supports_app_purchase_type(client)
    node_fields = _selling_plan_group_node_fields(include_app_purchase_type)

    def build_query(after_clause: str) -> str:
        return f"""
        {{
          sellingPlanGroups(first: 50{after_clause}) {{
            edges {{ node {{ {node_fields} }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("sellingPlanGroups",))


def normalize_billing_policy(bp: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not bp or bp.get("__typename") != "SellingPlanRecurringBillingPolicy":
        return None
    return {"interval": bp.get("interval"), "interval_count": bp.get("intervalCount")}


def normalize_delivery_policy(dp: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not dp or dp.get("__typename") != "SellingPlanRecurringDeliveryPolicy":
        return None
    return {"interval": dp.get("interval"), "interval_count": dp.get("intervalCount")}


def normalize_pricing_policy(pp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if pp.get("__typename") != "SellingPlanFixedPricingPolicy":
        return None

    adjustment_type = pp.get("adjustmentType")
    value = pp.get("adjustmentValue") or {}
    value_typename = value.get("__typename")

    if value_typename == "SellingPlanPricingPolicyPercentageValue":
        return {"adjustment_type": adjustment_type, "percentage": value.get("percentage")}
    if value_typename == "MoneyV2":
        return {"adjustment_type": adjustment_type, "amount": value.get("amount")}
    return None


def normalize_selling_plan(sp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    billing_policy = normalize_billing_policy(sp.get("billingPolicy"))
    delivery_policy = normalize_delivery_policy(sp.get("deliveryPolicy"))
    if not billing_policy or not delivery_policy:
        logger.warning(
            "Selling plan '%s' uses a non-recurring billing/delivery policy shape -- skipping (only "
            "recurring policies are supported by this script)",
            sp.get("name"),
        )
        return None

    pricing_policies = []
    for pp in sp.get("pricingPolicies") or []:
        normalized = normalize_pricing_policy(pp)
        if normalized:
            pricing_policies.append(normalized)
        else:
            logger.warning(
                "Selling plan '%s' has a pricing policy type this script doesn't support recreating -- dropping "
                "that policy (only SellingPlanFixedPricingPolicy is supported)",
                sp.get("name"),
            )

    return {
        "name": sp.get("name"),
        "description": sp.get("description"),
        "billing_policy": billing_policy,
        "delivery_policy": delivery_policy,
        "pricing_policies": pricing_policies,
    }


def export_selling_plan_groups(src_client) -> List[Dict[str, Any]]:
    raw_groups = fetch_selling_plan_groups(src_client)

    exported: List[Dict[str, Any]] = []
    skipped_app_owned = 0
    skipped_no_plans = 0

    for g in raw_groups:
        if g.get("appPurchaseType"):
            skipped_app_owned += 1
            logger.info(
                "Skipping selling plan group '%s': owned by a third-party app (appPurchaseType=%s), only works "
                "if that same app is installed on the destination",
                g.get("name"),
                g.get("appPurchaseType"),
            )
            continue

        selling_plans = []
        for edge in (g.get("sellingPlans") or {}).get("edges", []):
            normalized = normalize_selling_plan(edge["node"])
            if normalized:
                selling_plans.append(normalized)

        if not selling_plans:
            skipped_no_plans += 1
            logger.warning(
                "Skipping selling plan group '%s': no selling plan with a supported policy shape", g.get("name")
            )
            continue

        product_handles = [
            edge["node"]["handle"] for edge in (g.get("products") or {}).get("edges", []) if edge["node"].get("handle")
        ]

        exported.append(
            {
                "name": g.get("name"),
                "merchant_code": g.get("merchantCode"),
                "description": g.get("description"),
                "options": g.get("options") or [],
                "selling_plans": selling_plans,
                "product_handles": product_handles,
            }
        )

    logger.info(
        "Exported %s selling plan group(s) (%s skipped as app-owned, %s skipped with no supported selling plan)",
        len(exported),
        skipped_app_owned,
        skipped_no_plans,
    )
    return exported


def _billing_policy_literal(bp: Dict[str, Any]) -> str:
    return f"{{ recurring: {{ interval: {bp['interval']}, intervalCount: {bp['interval_count']} }} }}"


def _delivery_policy_literal(dp: Dict[str, Any]) -> str:
    return f"{{ recurring: {{ interval: {dp['interval']}, intervalCount: {dp['interval_count']} }} }}"


def _pricing_policies_literal(policies: List[Dict[str, Any]]) -> str:
    parts = []
    for p in policies:
        adjustment_type = p["adjustment_type"]
        if adjustment_type == "PERCENTAGE":
            value_literal = f"{{ percentage: {p['percentage']} }}"
        else:
            value_literal = f"{{ fixedValue: {gql_quote(p.get('amount'))} }}"
        parts.append(f"{{ fixed: {{ adjustmentType: {adjustment_type}, adjustmentValue: {value_literal} }} }}")
    return "[" + ", ".join(parts) + "]"


def _selling_plans_to_create_literal(selling_plans: List[Dict[str, Any]]) -> str:
    parts = []
    for sp in selling_plans:
        fields = [f"name: {gql_quote(sp['name'])}"]
        if sp.get("description"):
            fields.append(f"description: {gql_quote(sp['description'])}")
        fields.append(f"billingPolicy: {_billing_policy_literal(sp['billing_policy'])}")
        fields.append(f"deliveryPolicy: {_delivery_policy_literal(sp['delivery_policy'])}")
        if sp.get("pricing_policies"):
            fields.append(f"pricingPolicies: {_pricing_policies_literal(sp['pricing_policies'])}")
        parts.append("{ " + ", ".join(fields) + " }")
    return "[" + ", ".join(parts) + "]"


def fetch_existing_destination_keys(dest_client) -> set:
    existing = fetch_selling_plan_groups(dest_client)
    return {
        ((g.get("name") or "").strip().lower(), (g.get("merchantCode") or "").strip().lower()) for g in existing
    }


def create_selling_plan_group(dest_client, group: Dict[str, Any]) -> Optional[str]:
    fields = [f"name: {gql_quote(group['name'])}"]
    if group.get("merchant_code"):
        fields.append(f"merchantCode: {gql_quote(group['merchant_code'])}")
    if group.get("description"):
        fields.append(f"description: {gql_quote(group['description'])}")
    if group.get("options"):
        options_literal = "[" + ", ".join(gql_quote(o) for o in group["options"]) + "]"
        fields.append(f"options: {options_literal}")
    fields.append(f"sellingPlansToCreate: {_selling_plans_to_create_literal(group['selling_plans'])}")

    mutation = f"""
    mutation {{
      sellingPlanGroupCreate(input: {{ {", ".join(fields)} }}) {{
        sellingPlanGroup {{ id }}
        userErrors {{ field message }}
      }}
    }}
    """

    try:
        result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    except Exception as e:
        logger.warning("Failed to create selling plan group '%s': %s", group.get("name"), e)
        return None

    errors = mutation_errors(result, "sellingPlanGroupCreate")
    if errors:
        logger.warning("Failed to create selling plan group '%s': %s", group.get("name"), errors)
        return None

    return (result.get("sellingPlanGroupCreate") or {}).get("sellingPlanGroup", {}).get("id")


def attach_products_to_group(dest_client, group_gid: str, product_gids: List[str]) -> bool:
    if not product_gids:
        return True

    gid_list = ", ".join(gql_quote(g) for g in product_gids)
    mutation = f"""
    mutation {{
      sellingPlanGroupAddProducts(id: {gql_quote(group_gid)}, productIds: [{gid_list}]) {{
        userErrors {{ field message }}
      }}
    }}
    """
    try:
        result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    except Exception as e:
        logger.warning("Failed to attach products to selling plan group %s: %s", group_gid, e)
        return False

    errors = mutation_errors(result, "sellingPlanGroupAddProducts")
    if errors:
        logger.warning("Failed to attach products to selling plan group %s: %s", group_gid, errors)
        return False

    return True


def import_selling_plan_groups(dest_client, exported: List[Dict[str, Any]]) -> None:
    existing_keys = fetch_existing_destination_keys(dest_client)

    dest_handle_to_gid = {
        (p.get("handle") or "").strip().lower(): f"gid://shopify/Product/{p['id']}"
        for p in fetch_all_product_handles(dest_client)
        if p.get("handle")
    }

    created = 0
    skipped_existing = 0
    failed = 0

    for group in exported:
        key = ((group.get("name") or "").strip().lower(), (group.get("merchant_code") or "").strip().lower())
        if key in existing_keys:
            skipped_existing += 1
            continue

        try:
            group_gid = create_selling_plan_group(dest_client, group)
        except Exception:
            logger.exception("Unexpected error creating selling plan group '%s'", group.get("name"))
            failed += 1
            continue

        if not group_gid:
            failed += 1
            continue

        logger.info("Created selling plan group '%s' (%s)", group.get("name"), group_gid)
        existing_keys.add(key)

        product_gids = []
        unmatched_handles = []
        for handle in group.get("product_handles", []):
            gid = dest_handle_to_gid.get((handle or "").strip().lower())
            if gid:
                product_gids.append(gid)
            else:
                unmatched_handles.append(handle)

        if unmatched_handles:
            logger.warning(
                "Selling plan group '%s': %s source product handle(s) have no destination match, skipping "
                "attachment for them: %s",
                group.get("name"),
                len(unmatched_handles),
                unmatched_handles,
            )

        try:
            attach_products_to_group(dest_client, group_gid, product_gids)
        except Exception:
            logger.exception("Unexpected error attaching products to selling plan group '%s'", group.get("name"))

        created += 1

    logger.info(
        "Selling plan groups import complete: %s created, %s already existed, %s failed",
        created,
        skipped_existing,
        failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transfer selling plan group templates (not live subscriber contracts) from Src to dest"
    )
    parser.add_argument("--execute", action="store_true", help="Create missing selling plan groups on the destination store")
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
        exported = loaded if isinstance(loaded, list) else [loaded]
    else:
        src_shop = os.getenv("SRC_SHOPIFY_SHOP")
        src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
        require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

        src_client = make_client(src_shop, src_token)

        logger.info("Exporting selling plan groups from %s", src_shop)
        exported = export_selling_plan_groups(src_client)

        ts = int(time.time())
        out_file = out_dir / f"selling_plans_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)
        logger.info("Export complete: %s (%s selling plan group(s))", out_file, len(exported))

        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"selling_plans_export_{ts}.xlsx")

    if args.execute:
        logger.info("Importing selling plan groups into %s", dest_shop)
        import_selling_plan_groups(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to create selling plan groups on the destination store")


if __name__ == "__main__":
    main()
