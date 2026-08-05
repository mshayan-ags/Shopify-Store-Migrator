import logging
import os
from typing import Any, Dict

logger = logging.getLogger("postgresql_export")

RESOURCE_NAMES = ("products", "collections", "customers", "orders")


def _get_yaml():
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "pyyaml is required for the PostgreSQL connector's mapping file -- install it with: pip install pyyaml"
        ) from exc
    return yaml


def load_mapping(path: str) -> Dict[str, Any]:
    yaml = _get_yaml()

    if not os.path.isfile(path):
        raise ValueError(f"Mapping file not found: '{path}'")

    with open(path, "r", encoding="utf-8") as f:
        try:
            mapping = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ValueError(f"Mapping file '{path}' is not valid YAML: {exc}") from exc

    if mapping is None:
        raise ValueError(f"Mapping file '{path}' is empty")
    if not isinstance(mapping, dict):
        raise ValueError(
            f"Mapping file '{path}' must have a top-level mapping (dict) of resource names "
            f"({', '.join(RESOURCE_NAMES)}) to their config -- got a {type(mapping).__name__} instead"
        )

    unknown_keys = [name for name in mapping if name not in RESOURCE_NAMES]
    if unknown_keys:
        logger.warning(
            "Mapping file '%s' has top-level key(s) not recognized as canonical resources: %s "
            "(expected one or more of %s) -- they will be ignored",
            path, unknown_keys, RESOURCE_NAMES,
        )

    validate_mapping(mapping, source=path)
    return mapping


def validate_mapping(mapping: Dict[str, Any], source: str = "<mapping>") -> None:
    if not any(name in mapping for name in RESOURCE_NAMES):
        raise ValueError(
            f"Mapping file '{source}' has none of the recognized top-level resources "
            f"({', '.join(RESOURCE_NAMES)}) -- nothing to export"
        )

    if "products" in mapping:
        _validate_products(mapping["products"])
    if "collections" in mapping:
        _validate_collections(mapping["collections"])
    if "customers" in mapping:
        _validate_customers(mapping["customers"])
    if "orders" in mapping:
        _validate_orders(mapping["orders"])


def _require_dict(section: Any, where: str) -> Dict[str, Any]:
    if not isinstance(section, dict):
        raise ValueError(f"'{where}' must be a mapping (dict), got {type(section).__name__}")
    return section


def _require_table_or_query(section: Dict[str, Any], where: str) -> None:
    has_table = bool(section.get("table"))
    has_query = bool(section.get("query"))
    if not has_table and not has_query:
        raise ValueError(
            f"'{where}' is missing a 'table' key (a plain table name) or a 'query' key "
            f"(a full SELECT statement, for joins) -- exactly one is required"
        )
    if has_table and has_query:
        raise ValueError(
            f"'{where}' has both 'table' and 'query' set -- specify only one of them"
        )


def _require_columns(section: Dict[str, Any], where: str) -> None:
    if "columns" not in section:
        raise ValueError(
            f"'{where}' is missing a 'columns' key -- add a mapping of canonical field name -> source "
            f"column name (see sources/postgresql/mapping.example.yaml for the expected format)"
        )
    columns = section["columns"]
    if not isinstance(columns, dict):
        raise ValueError(
            f"'{where}.columns' must be a mapping (dict) of canonical field -> source column name, "
            f"got {type(columns).__name__}"
        )


def _require_key(section: Dict[str, Any], key: str, where: str) -> None:
    if not section.get(key):
        raise ValueError(f"'{where}' is missing a '{key}' key")


def _validate_products(section: Any) -> None:
    section = _require_dict(section, "products")
    _require_table_or_query(section, "products")
    _require_columns(section, "products")

    variants = section.get("variants")
    if variants is not None:
        variants = _require_dict(variants, "products.variants")
        _require_table_or_query(variants, "products.variants")
        _require_columns(variants, "products.variants")
        _require_key(
            variants, "foreign_key",
            "products.variants",
        )


def _validate_collections(section: Any) -> None:
    section = _require_dict(section, "collections")
    _require_table_or_query(section, "collections")
    _require_columns(section, "collections")

    membership = section.get("product_membership")
    if membership is not None:
        membership = _require_dict(membership, "collections.product_membership")
        _require_table_or_query(membership, "collections.product_membership")
        _require_key(membership, "product_id_column", "collections.product_membership")
        _require_key(membership, "collection_id_column", "collections.product_membership")


def _validate_customers(section: Any) -> None:
    section = _require_dict(section, "customers")
    _require_table_or_query(section, "customers")
    _require_columns(section, "customers")


def _validate_orders(section: Any) -> None:
    section = _require_dict(section, "orders")
    _require_table_or_query(section, "orders")
    _require_columns(section, "orders")

    line_items = section.get("line_items")
    if line_items is not None:
        line_items = _require_dict(line_items, "orders.line_items")
        _require_table_or_query(line_items, "orders.line_items")
        _require_columns(line_items, "orders.line_items")
        _require_key(line_items, "foreign_key", "orders.line_items")
