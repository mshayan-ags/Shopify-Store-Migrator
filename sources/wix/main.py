import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from sources.wix.wix_client import WixClient
from sources.wix.export_products import export_products
from sources.wix.export_collections import export_collections
from sources.wix.export_customers import export_customers
from sources.wix.export_orders import export_orders
from sources.wix.export_discounts import export_discounts, export_discount_rules
from sources.wix.export_pages import export_pages

load_dotenv()

logger = logging.getLogger("wix_export")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

RESOURCE_CHOICES = ("products", "collections", "customers", "orders", "discounts", "pages", "all")
ALL_RESOURCES = ("products", "collections", "customers", "orders", "discounts", "pages")


def _record_count(resource: str, data: Any) -> int:
    if resource == "collections":
        return len(data.get("custom_collections", [])) + len(data.get("smart_collections", []))
    if resource == "discounts":
        return len(data.get("discounts", []))
    if isinstance(data, list):
        return len(data)
    return 1


def _write_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str, ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a Wix store into Shopify canonical-schema JSON (see docs/CANONICAL_SCHEMA.md)"
    )
    parser.add_argument(
        "--resource",
        required=True,
        choices=RESOURCE_CHOICES,
        help="Which canonical resource to export, or 'all' for every resource this connector supports",
    )
    parser.add_argument("--out", default="Results", help="Output directory for the exported canonical JSON (default: 'Results')")
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    args = parser.parse_args()

    client = WixClient()
    out_dir = Path(args.out)
    ts = int(time.time())

    resources = list(ALL_RESOURCES) if args.resource == "all" else [args.resource]
    products_cache = None

    for resource in resources:
        logger.info("Exporting Wix resource '%s'", resource)

        if resource == "products":
            data = export_products(client, out_dir=out_dir)
            products_cache = data
        elif resource == "collections":
            data = export_collections(client, products=products_cache)
        elif resource == "customers":
            data = export_customers(client)
        elif resource == "orders":
            data = export_orders(client)
        elif resource == "discounts":
            data = export_discounts(client)
            try:
                rules = export_discount_rules(client)
                data["discounts"].extend(rules.get("discounts", []))
            except Exception:
                logger.exception(
                    "Failed to export Wix discount rules (automatic discounts) -- "
                    "continuing with coupon-based discounts only"
                )
        elif resource == "pages":
            data = export_pages(client)
        else:
            raise ValueError(f"Unknown resource '{resource}'")

        out_path = out_dir / f"wix_{resource}_export_{ts}.json"
        _write_json(data, out_path)
        logger.info("Wrote %s record(s) to %s", _record_count(resource, data), out_path)

        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(data, out_path.with_suffix(".xlsx"))

    logger.info("Done. Import into Shopify with e.g.: python transfer/transfer_product.py --import-from <file> --execute")


if __name__ == "__main__":
    main()
