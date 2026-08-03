import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from transfer.transfer_product import make_client
from utils.concurrency_utils import retry_with_backoff, gql_quote
from utils.shopify_graphql_utils import paginate_connection, mutation_errors
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_gift_cards")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


GIFT_CARD_NODE_FIELDS = """
    id
    maskedCode
    note
    enabled
    expiresOn
    createdAt
    initialValue { amount currencyCode }
    balance { amount currencyCode }
    customer { email }
"""


def fetch_all_gift_cards(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          giftCards(first: 50{after_clause}) {{
            edges {{ node {{ {GIFT_CARD_NODE_FIELDS} }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("giftCards",))


def fetch_dest_customers_by_email(client) -> Dict[str, str]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          customers(first: 50{after_clause}) {{
            edges {{ node {{ id email }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    customers = paginate_connection(client, build_query, ("customers",))
    return {
        c["email"].strip().lower(): c["id"]
        for c in customers
        if c.get("email")
    }


def export_gift_cards(src_client) -> List[Dict[str, Any]]:
    raw = fetch_all_gift_cards(src_client)

    exported = []
    skipped_disabled = 0
    skipped_zero_balance = 0

    for gc in raw:
        if not gc.get("enabled"):
            skipped_disabled += 1
            continue

        balance = gc.get("balance") or {}
        balance_amount = balance.get("amount")
        try:
            if balance_amount is None or float(balance_amount) <= 0:
                skipped_zero_balance += 1
                continue
        except (TypeError, ValueError):
            skipped_zero_balance += 1
            continue

        customer = gc.get("customer") or {}
        exported.append(
            {
                "id": gc["id"],
                "masked_code": gc.get("maskedCode"),
                "note": gc.get("note"),
                "enabled": gc.get("enabled"),
                "expires_on": gc.get("expiresOn"),
                "created_at": gc.get("createdAt"),
                "initial_value": gc.get("initialValue"),
                "balance": balance,
                "customer_email": customer.get("email"),
            }
        )

    logger.info(
        "Exported %s gift card(s) with remaining balance to migrate (%s skipped: disabled, %s skipped: zero balance)",
        len(exported), skipped_disabled, skipped_zero_balance,
    )
    return exported


def load_migration_marker(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            records = json.load(f)
        return records if isinstance(records, list) else []
    except Exception:
        logger.exception("Failed to read migration marker %s -- treating as empty", path)
        return []


def save_migration_marker(path: Path, records: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def build_migration_note(masked_code: Optional[str], original_note: Optional[str]) -> str:
    prefix = f"Migrated from source store (was {masked_code})."
    return f"{prefix} {original_note or ''}".strip()


def build_gift_card_create_input(gift_card: Dict[str, Any], customer_gid: Optional[str]) -> str:
    fields = [f"initialValue: {gql_quote(gift_card['balance']['amount'])}"]

    note = build_migration_note(gift_card.get("masked_code"), gift_card.get("note"))
    fields.append(f"note: {gql_quote(note)}")

    if gift_card.get("expires_on"):
        fields.append(f"expiresOn: {gql_quote(gift_card['expires_on'])}")

    if customer_gid:
        fields.append(f"customerId: {gql_quote(customer_gid)}")

    return "{ " + ", ".join(fields) + " }"


def import_gift_cards(
    dest_client,
    to_create: List[Dict[str, Any]],
    marker_path: Path,
    existing_marker_records: List[Dict[str, Any]],
) -> None:
    customer_by_email: Dict[str, str] = {}
    if any(gc.get("customer_email") for gc in to_create):
        try:
            customer_by_email = fetch_dest_customers_by_email(dest_client)
        except Exception:
            logger.exception("Failed to fetch destination customers for email matching -- proceeding without customer assignment")

    marker_records = list(existing_marker_records)
    created = 0
    failed = 0
    total_balance_created = 0.0

    for gift_card in to_create:
        try:
            customer_gid = None
            email_key = (gift_card.get("customer_email") or "").strip().lower()
            if email_key:
                customer_gid = customer_by_email.get(email_key)
                if not customer_gid:
                    logger.warning(
                        "No destination customer found for '%s' -- gift card for source %s will be created unassigned",
                        gift_card.get("customer_email"), gift_card.get("id"),
                    )

            input_literal = build_gift_card_create_input(gift_card, customer_gid)
            mutation = f"""
            mutation {{
              giftCardCreate(input: {input_literal}) {{
                giftCard {{ id maskedCode }}
                userErrors {{ field message }}
              }}
            }}
            """

            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            except Exception as e:
                logger.warning("Failed to create gift card for source %s (was %s): %s", gift_card["id"], gift_card.get("masked_code"), e)
                failed += 1
                continue

            errors = mutation_errors(result, "giftCardCreate")
            if errors:
                logger.warning("Failed to create gift card for source %s (was %s): %s", gift_card["id"], gift_card.get("masked_code"), errors)
                failed += 1
                continue

            new_gift_card = (result.get("giftCardCreate") or {}).get("giftCard") or {}
            amount = gift_card["balance"]["amount"]
            logger.info(
                "Created gift card %s (was %s, balance %s) on destination%s",
                new_gift_card.get("maskedCode"), gift_card.get("masked_code"), amount,
                f" assigned to {gift_card.get('customer_email')}" if customer_gid else "",
            )
            created += 1
            total_balance_created += float(amount)

            marker_records.append(
                {
                    "source_id": gift_card["id"],
                    "dest_id": new_gift_card.get("id"),
                    "source_masked_code": gift_card.get("masked_code"),
                    "dest_masked_code": new_gift_card.get("maskedCode"),
                    "amount": amount,
                    "currency": gift_card["balance"].get("currencyCode"),
                    "customer_email": gift_card.get("customer_email"),
                    "migrated_at": int(time.time()),
                }
            )
            save_migration_marker(marker_path, marker_records)
        except Exception:
            logger.exception("Unexpected error migrating gift card %s", gift_card.get("id"))
            failed += 1

    logger.info(
        "Gift cards import complete: %s created (total balance created: %.2f), %s failed",
        created, total_balance_created, failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer gift cards from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create missing gift cards on the destination store")
    parser.add_argument(
        "--i-understand-this-creates-real-balance",
        action="store_true",
        help=(
            "Required alongside --execute. Confirms you understand this creates brand-new, "
            "real, redeemable gift cards funded with the source cards' remaining balance -- "
            "it does NOT copy the source cards' actual codes (Shopify's API never exposes them)."
        ),
    )
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

    marker_path = out_dir / "gift_cards_migrated.json"
    marker_records = load_migration_marker(marker_path)
    migrated_ids = {r["source_id"] for r in marker_records if r.get("source_id")}
    if migrated_ids:
        logger.info("%s gift card(s) already migrated in a prior run (per %s) -- these will be skipped", len(migrated_ids), marker_path)

    if args.import_from:
        logger.info("Loading export from %s (skipping source fetch)", args.import_from)
        if args.import_from.lower().endswith(".xlsx"):
            from utils.tabular_io import import_from_xlsx
            loaded = import_from_xlsx(args.import_from)
        else:
            with open(args.import_from, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        exported = loaded.get("gift_cards", []) if isinstance(loaded, dict) else loaded
    else:
        src_shop = os.getenv("SRC_SHOPIFY_SHOP")
        src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
        require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

        src_client = make_client(src_shop, src_token)

        logger.info("Exporting gift cards from %s", src_shop)
        exported = export_gift_cards(src_client)

    to_create = [gc for gc in exported if gc["id"] not in migrated_ids]
    already_migrated_count = len(exported) - len(to_create)
    total_remaining_balance = sum(float(gc["balance"]["amount"]) for gc in to_create)

    summary = {
        "total_eligible_gift_cards": len(exported),
        "already_migrated_previously": already_migrated_count,
        "would_create": len(to_create),
        "total_remaining_balance_to_create": f"{total_remaining_balance:.2f}",
    }

    if not args.import_from:
        ts = int(time.time())
        out_file = out_dir / f"gift_cards_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "gift_cards": exported}, f, indent=2, ensure_ascii=False)
        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx({"summary": summary, "gift_cards": exported}, out_dir / f"gift_cards_export_{ts}.xlsx")
        logger.info(
            "Export complete: %s. Would create %s new gift card(s) totalling %s remaining balance "
            "(%s already migrated in a prior run).",
            out_file, summary["would_create"], summary["total_remaining_balance_to_create"], already_migrated_count,
        )

    if args.execute:
        if not args.i_understand_this_creates_real_balance:
            logger.error(
                "Refusing to create gift cards: --execute was passed without "
                "--i-understand-this-creates-real-balance. Creating a gift card on the destination "
                "mints a brand-new, real, redeemable balance (it cannot copy the source card's actual "
                "code -- Shopify's API never exposes it). Re-run with both flags to proceed: "
                "python transfer_gift_cards.py --execute --i-understand-this-creates-real-balance"
            )
            return

        logger.info(
            "Importing %s gift card(s) into %s (total balance to be created: %s)",
            len(to_create), dest_shop, summary["total_remaining_balance_to_create"],
        )
        import_gift_cards(dest_client, to_create, marker_path, marker_records)
    else:
        logger.info(
            "Dry-run finished. Re-run with --execute --i-understand-this-creates-real-balance to "
            "create %s gift card(s) (totalling %s remaining balance) on the destination store.",
            summary["would_create"], summary["total_remaining_balance_to_create"],
        )


if __name__ == "__main__":
    main()
