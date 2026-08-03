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

logger = logging.getLogger("audit_installed_apps")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

QUERY = """
query {
  currentAppInstallation {
    id
    launchUrl
    accessScopes { handle }
    app { title handle }
  }
}
"""


def audit_current_app_installation(client) -> Dict[str, Any]:
    data = retry_with_backoff(lambda: client.query(QUERY))
    installation = data.get("currentAppInstallation") or {}
    return {
        "app_title": (installation.get("app") or {}).get("title"),
        "app_handle": (installation.get("app") or {}).get("handle"),
        "granted_scopes": sorted(s["handle"] for s in installation.get("accessScopes", [])),
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
            "The Admin API cannot list a store's other installed apps -- this only shows "
            "this custom app's own installation. Check Settings > Apps and sales channels "
            "in the Shopify admin on Src for the real list, and reinstall/reconfigure "
            "each equivalent app manually on dest."
        ),
        "Src_app_installation": audit_current_app_installation(src_client),
        "dest_app_installation": audit_current_app_installation(dest_client),
        "manual_checklist": [
            "List every app under Settings > Apps and sales channels on Src",
            "For each: note the app name, plan/tier, and what it's configured to do",
            "Install the equivalent app on dest (or the same app, if available)",
            "Reconfigure each app's settings to match -- configuration is app-specific and not copyable via Shopify's API",
        ],
    }

    ts = int(time.time())
    out_file = out_dir / f"installed_apps_audit_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logger.info("Installed-apps audit written to %s", out_file)
    logger.info("This is a MANUAL checklist item -- see the report's 'manual_checklist' field")


if __name__ == "__main__":
    main()
