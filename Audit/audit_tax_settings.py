import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

from transfer.transfer_product import make_client
from utils.concurrency_utils import retry_with_backoff
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("audit_tax_settings")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

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

    require_env(
        SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token,
        DEST_SHOPIFY_SHOP=dest_shop, DEST_SHOPIFY_ACCESS_TOKEN=dest_token,
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
