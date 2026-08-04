import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from sources.bigcommerce.bigcommerce_client import BigCommerceClient
from sources.bigcommerce.export_products import export_products
from sources.bigcommerce.export_collections import export_collections
from sources.bigcommerce.export_customers import export_customers
from sources.bigcommerce.export_orders import export_orders
from sources.bigcommerce.export_discounts import export_discounts
from sources.bigcommerce.export_pages import export_pages

load_dotenv()

logger = logging.getLogger("bigcommerce_export")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

RESOURCE_CHOICES = ("products", "collections", "customers", "orders", "discounts", "pages", "all")


def _write_json(data: Any, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    logger.info("Wrote %s to %s", out_path.name, out_path)


def _write_xlsx_if_requested(data: Any, out_path: Path, xlsx: bool) -> None:
    if not xlsx:
        return
    from utils.tabular_io import export_to_xlsx
    export_to_xlsx(data, out_path.with_suffix(".xlsx"))


def _run_products(client: BigCommerceClient, out_dir: Path, ts: int, xlsx: bool = False):
    products = export_products(client)
    out_path = out_dir / f"bigcommerce_products_export_{ts}.json"
    _write_json(products, out_path)
    _write_xlsx_if_requested(products, out_path, xlsx)
    return products


def _run_collections(client: BigCommerceClient, out_dir: Path, ts: int, products=None, xlsx: bool = False):
    collections = export_collections(client, products=products)
    out_path = out_dir / f"bigcommerce_collections_export_{ts}.json"
    _write_json(collections, out_path)
    _write_xlsx_if_requested(collections, out_path, xlsx)
    return collections


def _run_customers(client: BigCommerceClient, out_dir: Path, ts: int, xlsx: bool = False):
    customers = export_customers(client)
    out_path = out_dir / f"bigcommerce_customers_export_{ts}.json"
    _write_json(customers, out_path)
    _write_xlsx_if_requested(customers, out_path, xlsx)
    return customers


def _run_orders(client: BigCommerceClient, out_dir: Path, ts: int, limit=None, xlsx: bool = False):
    orders = export_orders(client, limit=limit)
    out_path = out_dir / f"bigcommerce_orders_export_{ts}.json"
    _write_json(orders, out_path)
    _write_xlsx_if_requested(orders, out_path, xlsx)
    return orders


def _run_discounts(client: BigCommerceClient, out_dir: Path, ts: int, xlsx: bool = False):
    discounts = export_discounts(client)
    out_path = out_dir / f"bigcommerce_discounts_export_{ts}.json"
    _write_json(discounts, out_path)
    _write_xlsx_if_requested(discounts, out_path, xlsx)
    return discounts


def _run_pages(client: BigCommerceClient, out_dir: Path, ts: int, xlsx: bool = False):
    pages = export_pages(client)
    out_path = out_dir / f"bigcommerce_pages_export_{ts}.json"
    _write_json(pages, out_path)
    _write_xlsx_if_requested(pages, out_path, xlsx)
    return pages


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a BigCommerce store into Shopify canonical-schema JSON files."
    )
    parser.add_argument(
        "--resource",
        required=True,
        choices=RESOURCE_CHOICES,
        help="Which resource to export, or 'all' for every resource this connector supports",
    )
    parser.add_argument(
        "--out",
        default="Results",
        help="Output directory for the exported canonical JSON file(s) (default: 'Results')",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only used for --resource orders/all: only export the first N orders (oldest first)",
    )
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    args = parser.parse_args()

    client = BigCommerceClient()
    out_dir = Path(args.out)
    ts = int(time.time())

    if args.resource == "all":
        products = None
        try:
            products = _run_products(client, out_dir, ts, xlsx=args.xlsx)
        except Exception:
            logger.warning("Products export failed -- continuing with the remaining resources", exc_info=True)

        try:
            _run_collections(client, out_dir, ts, products=products, xlsx=args.xlsx)
        except Exception:
            logger.warning("Collections export failed -- continuing with the remaining resources", exc_info=True)

        try:
            _run_customers(client, out_dir, ts, xlsx=args.xlsx)
        except Exception:
            logger.warning("Customers export failed -- continuing with the remaining resources", exc_info=True)

        try:
            _run_orders(client, out_dir, ts, limit=args.limit, xlsx=args.xlsx)
        except Exception:
            logger.warning("Orders export failed -- continuing with the remaining resources", exc_info=True)

        try:
            _run_discounts(client, out_dir, ts, xlsx=args.xlsx)
        except Exception:
            logger.warning("Discounts export failed -- continuing with the remaining resources", exc_info=True)

        try:
            _run_pages(client, out_dir, ts, xlsx=args.xlsx)
        except Exception:
            logger.warning("Pages export failed -- continuing with the remaining resources", exc_info=True)

        logger.info("Done exporting all BigCommerce resources to %s", out_dir)
        return

    if args.resource == "products":
        _run_products(client, out_dir, ts, xlsx=args.xlsx)
    elif args.resource == "collections":
        _run_collections(client, out_dir, ts, xlsx=args.xlsx)
    elif args.resource == "customers":
        _run_customers(client, out_dir, ts, xlsx=args.xlsx)
    elif args.resource == "orders":
        _run_orders(client, out_dir, ts, limit=args.limit, xlsx=args.xlsx)
    elif args.resource == "discounts":
        _run_discounts(client, out_dir, ts, xlsx=args.xlsx)
    elif args.resource == "pages":
        _run_pages(client, out_dir, ts, xlsx=args.xlsx)

    logger.info(
        "Import this into a destination Shopify store with, e.g.: "
        "python transfer/transfer_product.py --import-from <file> --execute"
    )


if __name__ == "__main__":
    main()
