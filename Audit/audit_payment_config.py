"""Audit-only report of payment configuration -- NOT a transfer script.

This is NOT automatable. There is no documented Admin API query or mutation
that lists or configures a store's payment gateways/providers (Shopify
Payments setup, third-party gateways, manual payment methods) -- merchants
have consistently asked for this and Shopify has never exposed it, almost
certainly because it touches live financial/banking configuration. Nothing
here can be written to the destination store.

What this script actually reads (the only payment-adjacent fields the Admin
API exposes): the shop's currency, enabled presentment currencies, and
`paymentSettings.supportedDigitalWallets` (Apple/Google Pay etc.) --
informational only, not something payments can be "created" from.

The actual payment gateway setup (which processor is connected, bank/payout
details, manual payment method text) has to be recreated by hand: go to
Settings > Payments on dest and configure each provider used on
Src, then re-enter any real credentials directly (never paste them
into this tool or its output).

Usage:
    python audit_payment_config.py
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

logger = logging.getLogger("audit_payment_config")
logging.basicConfig(level=logging.INFO)

QUERY = """
query {
  shop {
    currencyCode
    enabledPresentmentCurrencies
    paymentSettings {
      supportedDigitalWallets
    }
  }
}
"""


def audit_shop_payment_fields(client) -> Dict[str, Any]:
    data = retry_with_backoff(lambda: client.query(QUERY))
    shop = data.get("shop") or {}
    payment_settings = shop.get("paymentSettings") or {}
    return {
        "currency_code": shop.get("currencyCode"),
        "enabled_presentment_currencies": shop.get("enabledPresentmentCurrencies"),
        "supported_digital_wallets": payment_settings.get("supportedDigitalWallets"),
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

    report = {
        "note": (
            "Shopify's Admin API does not expose configured payment gateways/providers at all. "
            "The fields below are the only payment-adjacent data it will return -- everything else "
            "must be set up by hand under Settings > Payments on the destination store."
        ),
        "Src": audit_shop_payment_fields(src_client),
        "dest": audit_shop_payment_fields(dest_client),
        "manual_checklist": [
            "Review Settings > Payments on Src for every active provider (Shopify Payments, PayPal, manual methods, etc.)",
            "Set up the equivalent provider(s) on dest with real credentials entered directly in Shopify admin",
            "Match presentment currencies and digital wallet support to what's reported below, if relevant",
            "Never store real payment credentials in this repo or its output files",
        ],
    }

    ts = int(time.time())
    out_file = out_dir / f"payment_config_audit_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logger.info("Payment config audit written to %s", out_file)
    logger.info("This is a MANUAL checklist item -- see the report's 'manual_checklist' field")


if __name__ == "__main__":
    main()
