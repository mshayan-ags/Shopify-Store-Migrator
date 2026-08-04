import argparse
import json
import logging
from pathlib import Path

from sources.postgresql.db_export import export_resource
from sources.postgresql.mapping_loader import load_mapping

logger = logging.getLogger("postgresql_export")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

RESOURCE_CHOICES = ("products", "collections", "customers", "orders", "all")


def _resolve_out_path(out_arg: str, resource_name: str, multi: bool) -> Path:
    path = Path(out_arg)

    if multi:
        directory = path.parent if path.suffix.lower() == ".json" else path
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"postgres_{resource_name}.json"

    if path.suffix.lower() == ".json":
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    path.mkdir(parents=True, exist_ok=True)
    return path / f"postgres_{resource_name}.json"


def _write_json(data, out_path: Path) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export data from a PostgreSQL database into Shopify's canonical JSON schema, "
            "driven entirely by a mapping YAML file (see sources/postgresql/mapping.example.yaml)."
        )
    )
    parser.add_argument(
        "--mapping",
        required=True,
        help="Path to a mapping YAML file describing which table(s)/column(s) map to which canonical resource/field",
    )
    parser.add_argument(
        "--resource",
        required=True,
        choices=RESOURCE_CHOICES,
        help="Which canonical resource to export (or 'all' for every resource configured in the mapping file)",
    )
    parser.add_argument(
        "--out",
        default="Results",
        help="Output directory (default 'Results') or a specific .json file path for the exported canonical JSON",
    )
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    args = parser.parse_args()

    mapping = load_mapping(args.mapping)

    if args.resource == "all":
        resources_to_run = [name for name in ("products", "collections", "customers", "orders") if name in mapping]
        if not resources_to_run:
            raise ValueError(
                f"Mapping file '{args.mapping}' has none of products/collections/customers/orders "
                f"configured -- nothing to export"
            )
        for resource_name in resources_to_run:
            data = export_resource(mapping, resource_name)
            out_path = _resolve_out_path(args.out, resource_name, multi=True)
            _write_json(data, out_path)
            logger.info("Wrote %s %s record(s) to %s", len(data), resource_name, out_path)
            if args.xlsx:
                from utils.tabular_io import export_to_xlsx
                export_to_xlsx(data, out_path.with_suffix(".xlsx"))
        return

    if args.resource not in mapping:
        raise ValueError(f"Mapping file '{args.mapping}' has no '{args.resource}' section configured")

    data = export_resource(mapping, args.resource)
    out_path = _resolve_out_path(args.out, args.resource, multi=False)
    _write_json(data, out_path)
    logger.info("Wrote %s %s record(s) to %s", len(data), args.resource, out_path)
    if args.xlsx:
        from utils.tabular_io import export_to_xlsx
        export_to_xlsx(data, out_path.with_suffix(".xlsx"))
    logger.info(
        "Import this into a destination Shopify store with, e.g.: "
        "python transfer/transfer_product.py --import-from %s --execute",
        out_path,
    )


if __name__ == "__main__":
    main()
