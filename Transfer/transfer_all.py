import argparse
import json
import logging
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from transfer.transfer_product import make_client, fetch_all_product_handles, transfer_one as transfer_one_product
from utils.concurrency_utils import run_concurrently, DEFAULT_WORKERS
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_all")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def save_export(name: str, data, out_dir: Path, write_xlsx: bool = False) -> None:
    ts = int(time.time())
    out_file = out_dir / f"{name}_export_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Saved %s export: %s", name, out_file)

    if write_xlsx:
        from utils.tabular_io import export_to_xlsx
        try:
            export_to_xlsx(data, out_dir / f"{name}_export_{ts}.xlsx")
        except Exception:
            logger.exception("Failed to write .xlsx export for %s", name)

STEPS = [
    "pages",
    "blogs",
    "products",
    "collections",
    "customers",
    "customer_segments",
    "orders",
    "draft_orders",
    "navigation",
    "redirects",
    "metaobjects",
    "store_metafields",
    "policies",
    "locations",
    "markets",
    "discounts",
    "selling_plans",
    "b2b",
    "files",
    "theme",
    "shipping",
    "translations",
]

OPT_IN_STEPS = ["gift_cards"]


def run_step(name: str, fn) -> None:
    logger.info("Starting step: %s", name)
    try:
        fn()
        logger.info("Completed step: %s", name)
    except Exception:
        logger.exception("Step '%s' failed; continuing with the remaining steps", name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full Src -> dest store migration")
    parser.add_argument("--execute", action="store_true", help="Actually write to the destination store (default is dry-run export only)")
    parser.add_argument("--out", default="Results", help="Output directory for export JSON files")
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside each step's .json export")
    parser.add_argument("--skip", default="", help="Comma-separated steps to skip: " + ", ".join(STEPS))
    parser.add_argument(
        "--only",
        default="",
        help="Comma-separated steps to run exclusively: " + ", ".join(STEPS) + " (also accepts opt-in steps: " + ", ".join(OPT_IN_STEPS) + ")",
    )
    parser.add_argument("--product-limit", type=int, default=None, help="Only transfer the first N products")
    parser.add_argument("--order-limit", type=int, default=None, help="Only transfer the first N orders")
    parser.add_argument(
        "--include-expired-discounts",
        action="store_true",
        help="Also transfer expired/disabled discounts (off by default -- see transfer_discounts.py)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Number of items to transfer concurrently within each of the products/orders/collections steps (default: %(default)s)",
    )
    parser.add_argument(
        "--include-gift-cards",
        action="store_true",
        help=(
            "Also run the gift_cards step (off by default -- creates real monetary balance on the "
            "destination store, see transfer_gift_cards.py). Requires --gift-cards-i-understand-this-creates-real-balance too."
        ),
    )
    parser.add_argument(
        "--gift-cards-i-understand-this-creates-real-balance",
        action="store_true",
        help="Confirms you understand --include-gift-cards creates new gift cards with real redeemable balance on the destination store",
    )
    parser.add_argument(
        "--translations-locale",
        default=None,
        help="Comma-separated locale codes for the translations step (default: every published, non-primary destination locale)",
    )
    args = parser.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    def should_run(step: str) -> bool:
        if only:
            return step in only
        return step not in skip

    src_shop = os.getenv("SRC_SHOPIFY_SHOP")
    src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")

    require_env(
        SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token,
        DEST_SHOPIFY_SHOP=dest_shop, DEST_SHOPIFY_ACCESS_TOKEN=dest_token,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    src_client = make_client(src_shop, src_token)
    dest_client = make_client(dest_shop, dest_token)

    if should_run("pages"):
        def step():
            import transfer.transfer_pages as m

            exported = m.export_pages(src_client)
            save_export("pages", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_pages(dest_client, exported)

        run_step("pages", step)

    if should_run("blogs"):
        def step():
            import transfer.transfer_blogs as m

            exported = m.export_blogs(src_client)
            save_export("blogs", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_blogs(dest_client, exported)

        run_step("blogs", step)

    if should_run("products"):
        def step():
            entries = fetch_all_product_handles(src_client)
            if args.product_limit:
                entries = entries[: args.product_limit]
            logger.info("Transferring %s product(s) using %s worker(s)", len(entries), args.workers)

            progress_lock = threading.Lock()
            completed = 0

            def process(entry) -> None:
                nonlocal completed
                identifier = entry.get("handle") or str(entry.get("id"))
                with progress_lock:
                    completed += 1
                    position = completed
                logger.info("[product %s/%s] %s", position, len(entries), identifier)
                try:
                    transfer_one_product(src_client, dest_client, out_dir, identifier, args.execute)
                except Exception:
                    logger.exception("Failed to transfer product %s", identifier)

            run_concurrently(entries, process, max_workers=args.workers, label="product")

        run_step("products", step)

    if should_run("collections"):
        def step():
            import transfer.transfer_collections as m

            if args.execute:
                m.transfer_collections_one_by_one(src_client, dest_client, out_dir, max_workers=args.workers)
            else:
                m.export_collections(src_client, out_dir)

        run_step("collections", step)

    if should_run("customers"):
        def step():
            import transfer.transfer_customers as m

            exported = m.export_customers(src_client)
            save_export("customers", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_customers(dest_client, exported)

        run_step("customers", step)

    if should_run("customer_segments"):
        def step():
            import transfer.transfer_customer_segments as m

            exported = m.export_segments(src_client)
            save_export("customer_segments", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_segments(dest_client, exported)

        run_step("customer_segments", step)

    if should_run("orders"):
        def step():
            import transfer.transfer_orders as m

            exported = m.export_orders(src_client, limit=args.order_limit)
            save_export("orders", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_orders(dest_client, exported, max_workers=args.workers)

        run_step("orders", step)

    if should_run("draft_orders"):
        def step():
            import transfer.transfer_draft_orders as m

            exported = m.export_draft_orders(src_client, limit=args.order_limit)
            save_export("draft_orders", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_draft_orders(dest_client, exported, max_workers=args.workers)

        run_step("draft_orders", step)

    if should_run("navigation"):
        def step():
            import transfer.transfer_navigation as m

            exported = m.export_menus(src_client)
            save_export("navigation", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_menus(dest_client, exported)

        run_step("navigation", step)

    if should_run("redirects"):
        def step():
            import transfer.transfer_redirects as m

            exported = m.export_redirects(src_client)
            save_export("redirects", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_redirects(dest_client, exported)

        run_step("redirects", step)

    metaobject_gid_map = {}
    if should_run("metaobjects"):
        def step():
            nonlocal metaobject_gid_map
            import transfer.transfer_metaobjects as m

            exported = m.export_metaobjects(src_client)
            save_export("metaobjects", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                metaobject_gid_map = m.import_metaobjects(dest_client, exported)

        run_step("metaobjects", step)

    if should_run("store_metafields"):
        def step():
            import transfer.transfer_store_metafields as m

            exported_definitions = m.export_metafield_definitions(src_client)
            save_export("metafield_definitions", exported_definitions, out_dir, write_xlsx=args.xlsx)

            exported = m.export_store_metafields(src_client)
            save_export("store_metafields", exported, out_dir, write_xlsx=args.xlsx)

            if args.execute:
                m.import_metafield_definitions(dest_client, exported_definitions, metaobject_gid_map)
                m.import_store_metafields(dest_client, exported)

        run_step("store_metafields", step)

    if should_run("policies"):
        def step():
            import transfer.transfer_policies as m

            exported = m.export_policies(src_client)
            save_export("policies", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_policies(dest_client, exported)

        run_step("policies", step)

    if should_run("locations"):
        def step():
            import transfer.transfer_locations as m

            exported = m.export_locations(src_client)
            save_export("locations", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_locations(dest_client, exported)

        run_step("locations", step)

    if should_run("markets"):
        def step():
            import transfer.transfer_markets as m

            exported = m.export_markets(src_client)
            save_export("markets", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_markets(dest_client, exported)

        run_step("markets", step)

    if should_run("discounts"):
        def step():
            import transfer.transfer_discounts as m

            exported = m.export_discounts(src_client, include_expired=args.include_expired_discounts)
            save_export("discounts", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_discounts(dest_client, exported)

        run_step("discounts", step)

    if should_run("selling_plans"):
        def step():
            import transfer.transfer_selling_plans as m

            exported = m.export_selling_plan_groups(src_client)
            save_export("selling_plans", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_selling_plan_groups(dest_client, exported)

        run_step("selling_plans", step)

    if should_run("b2b"):
        def step():
            import transfer.transfer_b2b as m

            exported_companies = m.export_companies(src_client)
            save_export("b2b_companies", exported_companies, out_dir, write_xlsx=args.xlsx)
            try:
                exported_catalogs = m.export_catalogs(src_client)
                save_export("b2b_catalogs", exported_catalogs, out_dir, write_xlsx=args.xlsx)
            except Exception:
                logger.exception("Failed to export B2B catalogs/price lists -- continuing without them")
                exported_catalogs = []

            if args.execute:
                m.import_companies(dest_client, exported_companies)
                if exported_catalogs:
                    variant_sku_index = m.build_dest_variant_sku_index(dest_client)
                    m.import_catalogs(dest_client, exported_catalogs, variant_sku_index)

        run_step("b2b", step)

    if should_run("files"):
        def step():
            import transfer.transfer_files as m

            exported = m.export_files(src_client)
            save_export("files", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_files(dest_client, exported)

        run_step("files", step)

    if should_run("theme"):
        def step():
            import transfer.transfer_theme as m

            exported = m.export_theme(src_client, None, "MAIN", out_dir)
            save_export("theme", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                name = f"{exported.get('theme_name') or 'Source theme'} (migrated)"
                m.import_theme(dest_client, exported, name, None, publish=False, src_client=src_client)

        run_step("theme", step)

    if should_run("shipping"):
        def step():
            import transfer.transfer_shipping as m

            exported = m.export_shipping(src_client)
            save_export("shipping", exported, out_dir, write_xlsx=args.xlsx)
            if args.execute:
                m.import_shipping(dest_client, exported)

        run_step("shipping", step)

    gift_cards_selected = "gift_cards" in only or (not only and args.include_gift_cards)
    if gift_cards_selected and "gift_cards" not in skip:
        if args.execute and not args.gift_cards_i_understand_this_creates_real_balance:
            logger.warning(
                "Skipping gift_cards step: --execute was passed without "
                "--gift-cards-i-understand-this-creates-real-balance, so no gift cards will be created."
            )
        else:
            def step():
                import transfer.transfer_gift_cards as m

                exported = m.export_gift_cards(src_client)
                save_export("gift_cards", exported, out_dir, write_xlsx=args.xlsx)
                if args.execute:
                    marker_path = out_dir / "gift_cards_migrated.json"
                    m.import_gift_cards(dest_client, exported, marker_path)

            run_step("gift_cards", step)

    if should_run("translations"):
        def step():
            import transfer.transfer_translations as m

            locale_filter = [c.strip() for c in args.translations_locale.split(",") if c.strip()] if args.translations_locale else None
            target_locales = m.resolve_target_locales(src_client, dest_client, locale_filter)
            if not target_locales:
                logger.warning("No destination locale(s) to translate into -- skipping translations step")
                return

            indices = m.build_destination_indices(dest_client)
            stats = {}
            export_records = []
            for resource_type in m.GENERIC_RESOURCE_TYPES:
                resource_stats = {}
                m.transfer_resource_type_translations(
                    src_client, dest_client, resource_type, target_locales, indices, resource_stats, args.execute, export_records
                )
                stats[m.RESOURCE_TYPE_LABELS[resource_type]] = resource_stats

            policy_stats = {}
            m.transfer_policy_translations(src_client, dest_client, target_locales, policy_stats, args.execute, export_records)
            stats["policy"] = policy_stats

            save_export("translations", {"target_locales": target_locales, "stats": stats, "records": export_records}, out_dir, write_xlsx=args.xlsx)

        run_step("translations", step)

    logger.info("Migration run complete (%s)", "executed" if args.execute else "dry-run")


if __name__ == "__main__":
    main()
