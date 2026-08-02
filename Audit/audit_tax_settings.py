"""Audit-only report of shop-level tax settings -- NOT a transfer script.

This is mostly NOT automatable. `Shop.taxesIncluded` and `Shop.taxShipping`
are the only general tax fields the Admin API exposes, and both are
read-only -- there is no documented mutation that writes them. Region-by-
region tax rates/registrations (Shopify Tax, or a third-party tax app) live
outside the Admin API entirely for a regular merchant; the only tax
mutations that exist (`taxAppConfigure`, `companyLocationTaxSettingsUpdate`)
configure a tax PARTNER app's own behavior or B2B company-location
overrides, not general shop tax rates, so they don't apply here.

What this script actually does: reads the two booleans that ARE exposed,
reports them side by side for both stores, and reminds you to match the
"Charge tax on shipping" / "All prices include tax" toggles by hand under
Settings > Taxes and duties on dest -- those toggles, plus any
country/region tax registrations, must be set manually.

Usage:
    python audit_tax_settings.py
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

from Transfer.transfer_product import make_client
from utils.concurrency_utils import retry_with_backoff

load_dotenv()

logger = logging.getLogger("audit_tax_settings")
logging.basicConfig(level=logging.INFO)

QUERY = """
query {
  shop {
    taxesIncluded
    taxShipping
    currencyCode
    shopAddress {
      countryCodeV2
    }
  }
}
"""


def audit_shop_tax_fields(client) -> Dict[str, Any]:
    data = retry_with_backoff(lambda: client.query(QUERY))
    shop = data.get("shop") or {}
    return {
        "taxes_included": shop.get("taxesIncluded"),
        "tax_shipping": shop.get("taxShipping"),
        "shop_country_code": (shop.get("shopAddress") or {}).get("countryCodeV2"),
        "currency_code": shop.get("currencyCode"),
    }


def main() -> None:
    src_shop = os.getenv("SRC_SHOPIFY_SHOP")
    src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")

    if not all([src_shop, src_token, dest_shop, dest_token]):
        raise RuntimeError(
            "Missing .env values: SRC_SHOPIFY_SHOP, SRC_SHOPIFY_ACCESS_TOKEN, DEST_SHOPIFY_SHOP, DEST_SHOPIFY_ACCESS_TOKEN"
        )

    out_dir = Path("Results")
    out_dir.mkdir(parents=True, exist_ok=True)

    src_client = make_client(src_shop, src_token)
    dest_client = make_client(dest_shop, dest_token)

    src_tax = audit_shop_tax_fields(src_client)
    le_tax = audit_shop_tax_fields(dest_client)

    mismatches = [
        field
        for field in ("taxes_included", "tax_shipping")
        if src_tax.get(field) != le_tax.get(field)
    ]

    report = {
        "note": (
            "taxesIncluded/taxShipping are read-only via the Admin API -- there is no mutation "
            "to write them. Region-level tax rates/registrations aren't exposed to a regular "
            "merchant app at all. Everything below is informational; apply changes manually "
            "under Settings > Taxes and duties on dest."
        ),
        "Src": src_tax,
        "dest": le_tax,
        "mismatched_fields": mismatches,
        "manual_checklist": [
            "Match 'All prices include tax' (taxesIncluded) manually if mismatched",
            "Match 'Charge tax on shipping rates' (taxShipping) manually if mismatched",
            "Review and recreate country/region tax registrations under Settings > Taxes and duties",
            "Recreate any manual tax overrides on specific products/collections",
        ],
    }

    ts = int(time.time())
    out_file = out_dir / f"tax_settings_audit_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logger.info("Tax settings audit written to %s", out_file)
    if mismatches:
        logger.warning("Mismatched tax fields between stores: %s", mismatches)
    logger.info("This is a MANUAL checklist item -- see the report's 'manual_checklist' field")


if __name__ == "__main__":
    main()
