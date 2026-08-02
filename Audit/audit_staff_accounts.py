"""Audit-only report of staff accounts -- NOT a transfer script.

This is NOT automatable. There is no documented Admin API mutation to
create or invite a staff member -- invitations are email-based and only
issued through the Shopify admin UI, almost certainly a deliberate security
restriction (an API-created account with arbitrary permissions would be a
major attack surface). `StaffMember` also has no granular permissions field
exposed via the Admin API -- role/permission sets are admin-UI-only too.
Nothing here can be written to the destination store.

What this script actually does: reads the `staffMembers` list on the source
store (name, email, active flag, account type, whether they're the shop
owner) via the Admin API, and reports it as a checklist of who needs to be
manually re-invited on dest with equivalent permissions.

Requires `read_staff_members` (or the store-owner token) on the source
store's custom app.

Usage:
    python audit_staff_accounts.py
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from Transfer.transfer_product import make_client
from utils.shopify_graphql_utils import paginate_connection

load_dotenv()

logger = logging.getLogger("audit_staff_accounts")
logging.basicConfig(level=logging.INFO)


def fetch_staff_members(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        query {{
          staffMembers(first: 50{after_clause}) {{
            edges {{
              node {{
                id
                name
                email
                active
                isShopOwner
                accountType
                locale
              }}
            }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("staffMembers",))


def audit_staff(client) -> List[Dict[str, Any]]:
    members = fetch_staff_members(client)
    return [
        {
            "name": m.get("name"),
            "email": m.get("email"),
            "active": m.get("active"),
            "is_shop_owner": m.get("isShopOwner"),
            "account_type": m.get("accountType"),
            "locale": m.get("locale"),
        }
        for m in members
    ]


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

    src_staff = audit_staff(src_client)
    le_staff = audit_staff(dest_client)
    le_emails = {(m.get("email") or "").strip().lower() for m in le_staff}

    missing_on_dest = [m for m in src_staff if (m.get("email") or "").strip().lower() not in le_emails]

    report = {
        "note": (
            "Staff accounts cannot be created/invited via the Admin API, and granular permission "
            "sets aren't exposed by it either -- both must be recreated by hand under "
            "Settings > Users and permissions on dest."
        ),
        "Src_staff": src_staff,
        "dest_staff": le_staff,
        "missing_on_destination": missing_on_dest,
        "manual_checklist": [
            "For each entry in 'missing_on_destination', invite that person on dest with a matching role",
            "Set the specific permission toggles (locations, orders, etc.) per person -- not exposed via API",
            "Deactivate/remove access for anyone who shouldn't have it on the new store",
        ],
    }

    ts = int(time.time())
    out_file = out_dir / f"staff_accounts_audit_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logger.info("Staff accounts audit written to %s (%s missing on destination)", out_file, len(missing_on_dest))
    logger.info("This is a MANUAL checklist item -- see the report's 'manual_checklist' field")


if __name__ == "__main__":
    main()
