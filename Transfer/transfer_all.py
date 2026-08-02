"""Run a complete Src -> dest store migration in the correct order.

Runs, in sequence:
  1. Pages
  2. Blogs + articles
  3. Products (all of them, with images/variants/metafields)
  4. Collections (product assignment needs products to already exist)
  5. Customers
  6. Orders (needs products + customers already transferred, for SKU/email matching)
  7. Navigation menus (needs products/collections/pages/blogs for resourceId remapping)
  8. Redirects
  9. Metaobjects (definitions + entries)
 10. Metafield definitions + store metafields (shop/blog/article/page/customer/
     location/order/draft_order -- the resource-specific steps above already cover
     product/variant/image/collection metafields, so this last pass mops up the
     remaining global owner types; metaobject-reference definitions are remapped
     using the GID map produced by the metaobjects step)
 11. Shop policies (refund/shipping/privacy/terms/etc.)
 12. Locations (name + address only; needs read_locations/write_locations, not
     currently granted on either store)
 13. Discounts (basic % / amount off + free shipping, code and automatic).
     Active/scheduled only by default -- Src has ~10,000 discount nodes,
     almost all expired single-use affiliate codes from a referral app, which
     would be pure noise to copy. Not covered by this or any script: Buy X Get Y
     discounts, and app-owned discounts (tied to whatever app created them).
 14. Theme code (see transfer_theme.py) -- copies every file from the source
     theme into a new UNPUBLISHED theme on the destination. Needs write_themes
     access PLUS a separate Shopify-granted exemption to modify themes via API;
     will fail until both are in place.
 15. Shipping rate configuration (see transfer_shipping.py) -- flat-rate zones
     for custom delivery profiles, and for the store's default profile. Needs
     the shipping scope/manage_delivery_settings permission, not currently
     granted on either store. Best run after locations, so zones can be
     matched to the right destination location by name.

Not covered by this migration at all, because the Admin API doesn't expose
them: installed apps/integrations, payment configuration, and staff accounts
(no mutation exists to install an app, configure a payment gateway, or invite
a staff member -- see audit_installed_apps.py / audit_payment_config.py /
audit_staff_accounts.py for read-only reports of what has to be recreated by
hand instead). Tax configuration is almost entirely in the same boat --
audit_tax_settings.py reports the two read-only fields the API does expose.
Checkout customization (checkoutBrandingUpsert) is Shopify Plus-only and
neither store is on Plus, so it isn't attempted at all. Gift cards are
deliberately excluded -- copying live cash-equivalent balances needs an
explicit decision, not a default.

Each step needs its own Admin API scopes granted on both stores' custom apps --
see the docstring of the corresponding transfer_*.py script for which ones.
A step whose required data can't be read (403) is skipped with a warning rather
than aborting the whole run.

Usage:
    python transfer_all.py                              # dry-run export of everything
    python transfer_all.py --execute                     # run the full migration for real
    python transfer_all.py --execute --skip orders,customers   # everything except orders/customers
    python transfer_all.py --execute --only products,collections
    python transfer_all.py --execute --skip pages,blogs,metaobjects,store_metafields
"""
import argparse
import json
import logging
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from Transfer.transfer_product import make_client, fetch_all_product_handles, transfer_one as transfer_one_product
from utils.concurrency_utils import run_concurrently, DEFAULT_WORKERS

load_dotenv()

logger = logging.getLogger("transfer_all")
logging.basicConfig(level=logging.INFO)


def save_export(name: str, data, out_dir: Path) -> None:
    out_file = out_dir / f"{name}_export_{int(time.time())}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Saved %s export: %s", name, out_file)

STEPS = [
    "pages",
    "blogs",
    "products",
    "collections",
    "customers",
    "orders",
    "navigation",
    "redirects",
    "metaobjects",
    "store_metafields",
    "policies",
    "locations",
    "discounts",
    "theme",
    "shipping",
]


def run_step(name: str, fn) -> None:
    logger.info("=" * 20 + f" STEP: {name} " + "=" * 20)
    try:
        fn()
    except Exception:
        logger.exception("Step '%s' failed; continuing with the remaining steps", name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full Src -> dest store migration")
    parser.add_argument("--execute", action="store_true", help="Actually write to the destination store (default is dry-run export only)")
    parser.add_argument("--out", default="Results", help="Output directory for export JSON files")
    parser.add_argument("--skip", default="", help="Comma-separated steps to skip: " + ", ".join(STEPS))
    parser.add_argument("--only", default="", help="Comma-separated steps to run exclusively: " + ", ".join(STEPS))
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

    if not all([src_shop, src_token, dest_shop, dest_token]):
        raise RuntimeError(
            "Missing .env values: SRC_SHOPIFY_SHOP, SRC_SHOPIFY_ACCESS_TOKEN, DEST_SHOPIFY_SHOP, DEST_SHOPIFY_ACCESS_TOKEN"
        )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    src_client = make_client(src_shop, src_token)
    dest_client = make_client(dest_shop, dest_token)

    if should_run("pages"):
        def step():
            import Transfer.transfer_pages as m

            exported = m.export_pages(src_client)
            save_export("pages", exported, out_dir)
            if args.execute:
                m.import_pages(dest_client, exported)

        run_step("pages", step)

    if should_run("blogs"):
        def step():
            import Transfer.transfer_blogs as m

            exported = m.export_blogs(src_client)
            save_export("blogs", exported, out_dir)
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
            import Transfer.transfer_collections as m

            if args.execute:
                m.transfer_collections_one_by_one(src_client, dest_client, out_dir, max_workers=args.workers)
            else:
                m.export_collections(src_client, out_dir)

        run_step("collections", step)

    if should_run("customers"):
        def step():
            import Transfer.transfer_customers as m

            exported = m.export_customers(src_client)
            save_export("customers", exported, out_dir)
            if args.execute:
                m.import_customers(dest_client, exported)

        run_step("customers", step)

    if should_run("orders"):
        def step():
            import Transfer.transfer_orders as m

            exported = m.export_orders(src_client, limit=args.order_limit)
            save_export("orders", exported, out_dir)
            if args.execute:
                m.import_orders(dest_client, exported, max_workers=args.workers)

        run_step("orders", step)

    if should_run("navigation"):
        def step():
            import Transfer.transfer_navigation as m

            exported = m.export_menus(src_client)
            save_export("navigation", exported, out_dir)
            if args.execute:
                m.import_menus(dest_client, exported)

        run_step("navigation", step)

    if should_run("redirects"):
        def step():
            import Transfer.transfer_redirects as m

            exported = m.export_redirects(src_client)
            save_export("redirects", exported, out_dir)
            if args.execute:
                m.import_redirects(dest_client, exported)

        run_step("redirects", step)

    metaobject_gid_map = {}
    if should_run("metaobjects"):
        def step():
            nonlocal metaobject_gid_map
            import Transfer.transfer_metaobjects as m

            exported = m.export_metaobjects(src_client)
            save_export("metaobjects", exported, out_dir)
            if args.execute:
                metaobject_gid_map = m.import_metaobjects(dest_client, exported)

        run_step("metaobjects", step)

    if should_run("store_metafields"):
        def step():
            import Transfer.transfer_store_metafields as m

            exported_definitions = m.export_metafield_definitions(src_client)
            save_export("metafield_definitions", exported_definitions, out_dir)

            exported = m.export_store_metafields(src_client)
            save_export("store_metafields", exported, out_dir)

            if args.execute:
                m.import_metafield_definitions(dest_client, exported_definitions, metaobject_gid_map)
                m.import_store_metafields(dest_client, exported)

        run_step("store_metafields", step)

    if should_run("policies"):
        def step():
            import Transfer.transfer_policies as m

            exported = m.export_policies(src_client)
            save_export("policies", exported, out_dir)
            if args.execute:
                m.import_policies(dest_client, exported)

        run_step("policies", step)

    if should_run("locations"):
        def step():
            import Transfer.transfer_locations as m

            exported = m.export_locations(src_client)
            save_export("locations", exported, out_dir)
            if args.execute:
                m.import_locations(dest_client, exported)

        run_step("locations", step)

    if should_run("discounts"):
        def step():
            import Transfer.transfer_discounts as m

            exported = m.export_discounts(src_client, include_expired=args.include_expired_discounts)
            save_export("discounts", exported, out_dir)
            if args.execute:
                m.import_discounts(dest_client, exported)

        run_step("discounts", step)

    if should_run("theme"):
        def step():
            import Transfer.transfer_theme as m

            exported = m.export_theme(src_client, None, "MAIN", out_dir)
            save_export("theme", exported, out_dir)
            if args.execute:
                name = f"{exported.get('theme_name') or 'Source theme'} (migrated)"
                m.import_theme(dest_client, exported, name, None, publish=False, src_client=src_client)

        run_step("theme", step)

    if should_run("shipping"):
        def step():
            import Transfer.transfer_shipping as m

            exported = m.export_shipping(src_client)
            save_export("shipping", exported, out_dir)
            if args.execute:
                m.import_shipping(dest_client, exported)

        run_step("shipping", step)

    logger.info("Migration run complete (%s)", "executed" if args.execute else "dry-run")


if __name__ == "__main__":
    main()
