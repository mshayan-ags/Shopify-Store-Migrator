import argparse
import json
import logging
from pathlib import Path
from typing import Any

from sources.mongodb.mapping_loader import load_mapping, KNOWN_RESOURCES
from sources.mongodb.db_export import export_resource

logger = logging.getLogger("mongodb_export")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

DEFAULT_OUTPUT_FILENAMES = {
    "products": "mongodb_products.json",
    "collections": "mongodb_collections.json",
    "customers": "mongodb_customers.json",
    "orders": "mongodb_orders.json",
}


def _write_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    logger.info("Wrote %s record(s) to %s", len(data) if isinstance(data, list) else 1, path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export MongoDB collections into Shopify canonical-schema JSON, driven entirely by a mapping YAML file."
    )
    parser.add_argument("--mapping", required=True, help="Path to a mapping YAML file (see mapping.example.yaml)")
    parser.add_argument(
        "--resource",
        required=True,
        choices=list(KNOWN_RESOURCES) + ["all"],
        help="Which canonical resource to export, or 'all' for every resource present in the mapping file",
    )
    parser.add_argument(
        "--out",
        default="Results",
        help=(
            "Output path. For a single --resource, this is used as-is if it looks like a file "
            "(has a .json suffix) or as a directory otherwise (default: 'Results')"
        ),
    )
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    args = parser.parse_args()

    config = load_mapping(args.mapping)

    resources = list(KNOWN_RESOURCES) if args.resource == "all" else [args.resource]
    out_arg = Path(args.out)

    for resource in resources:
        if resource not in config:
            if args.resource == "all":
                logger.info("Skipping resource '%s' -- not present in mapping file '%s'", resource, args.mapping)
                continue
            raise ValueError(f"Resource '{resource}' is not present in mapping file '{args.mapping}'")

        logger.info("Exporting resource '%s' from MongoDB using mapping '%s'", resource, args.mapping)
        data = export_resource(resource, config)

        if args.resource != "all" and out_arg.suffix.lower() == ".json":
            out_path = out_arg
        else:
            out_path = out_arg / DEFAULT_OUTPUT_FILENAMES[resource]

        _write_json(data, out_path)

        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(data, out_path.with_suffix(".xlsx"))

    logger.info("Done. Import into Shopify with e.g.: python transfer/transfer_product.py --import-from <file> --execute")


if __name__ == "__main__":
    main()
