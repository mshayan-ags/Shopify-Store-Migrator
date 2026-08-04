import logging
from pathlib import Path
from typing import Any, Dict, Union

logger = logging.getLogger("mongodb_export")

KNOWN_RESOURCES = ("products", "collections", "customers", "orders")

NESTED_ITEMS_KEY = {
    "products": "variants",
    "orders": "line_items",
}


def _get_yaml():
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "pyyaml is required for the MongoDB connector -- install it with: pip install pyyaml"
        ) from exc
    return yaml


def load_mapping(path: Union[str, Path]) -> Dict[str, Any]:
    yaml = _get_yaml()

    path = Path(path)
    if not path.exists():
        raise ValueError(f"Mapping file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        try:
            config = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise ValueError(f"Mapping file '{path}' is not valid YAML: {exc}") from exc

    validate_mapping(config, source=str(path))
    return config


def validate_mapping(config: Any, source: str = "<mapping>") -> None:
    if not isinstance(config, dict) or not config:
        raise ValueError(f"Mapping file '{source}' is empty or is not a YAML mapping/object at the top level")

    present_resources = [name for name in KNOWN_RESOURCES if name in config]
    if not present_resources:
        raise ValueError(
            f"Mapping file '{source}' defines none of the known resources {KNOWN_RESOURCES} "
            f"(found top-level keys: {list(config.keys())})"
        )

    for resource_name in present_resources:
        section = config[resource_name]
        if not isinstance(section, dict):
            raise ValueError(f"Resource '{resource_name}' in mapping file '{source}' must be a mapping/object")

        if "collection" not in section:
            raise ValueError(f"Resource '{resource_name}' in mapping file '{source}' is missing required key 'collection'")
        if not section.get("collection"):
            raise ValueError(f"Resource '{resource_name}' in mapping file '{source}' has an empty 'collection' value")

        if "fields" not in section:
            raise ValueError(f"Resource '{resource_name}' in mapping file '{source}' is missing required key 'fields'")
        if not isinstance(section["fields"], dict) or not section["fields"]:
            raise ValueError(f"Resource '{resource_name}' in mapping file '{source}' has an empty or invalid 'fields' mapping")

        nested_key = NESTED_ITEMS_KEY.get(resource_name)
        if nested_key and nested_key in section:
            nested = section[nested_key]
            if not isinstance(nested, dict):
                raise ValueError(
                    f"Resource '{resource_name}' in mapping file '{source}': '{nested_key}' must be a mapping/object"
                )

            has_embedded = bool(nested.get("embedded_field"))
            has_referenced = bool(nested.get("collection")) and bool(nested.get("foreign_key"))
            if not has_embedded and not has_referenced:
                raise ValueError(
                    f"Resource '{resource_name}' in mapping file '{source}': '{nested_key}' must set either "
                    f"'embedded_field' (array field on the parent document), or both 'collection' and "
                    f"'foreign_key' (a separate collection referencing the parent by id)"
                )
            if "fields" not in nested or not isinstance(nested["fields"], dict) or not nested["fields"]:
                raise ValueError(
                    f"Resource '{resource_name}' in mapping file '{source}': '{nested_key}' is missing a non-empty 'fields' mapping"
                )

        if resource_name == "collections" and "product_membership" in section:
            membership = section["product_membership"]
            if not isinstance(membership, dict) or not membership.get("product_field_referencing_collection"):
                raise ValueError(
                    f"Resource 'collections' in mapping file '{source}': 'product_membership' must set "
                    f"'product_field_referencing_collection'"
                )

    logger.info("Mapping file '%s' validated OK (resources: %s)", source, present_resources)
