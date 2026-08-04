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

logger = logging.getLogger("audit_payment_config")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

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

    require_env(
        SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token,
        DEST_SHOPIFY_SHOP=dest_shop, DEST_SHOPIFY_ACCESS_TOKEN=dest_token,
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
