"""Shared GraphQL helpers for the Src -> dest content-transfer scripts."""
from typing import Any, Callable, Dict, List, Tuple

from utils.shopify_client import ShopifyClient
from Transfer.transfer_store_metafields import retry_with_backoff, gql_quote

# Re-exported for scripts that already import these from here; the canonical
# definitions live in concurrency_utils.py (which has zero project-internal
# imports) to avoid an import cycle -- transfer_collections.py is imported by
# transfer_store_metafields.py, which this module also imports, so
# transfer_collections.py can't import run_concurrently from here without
# forming a cycle back to itself.
from utils.concurrency_utils import run_concurrently, DEFAULT_WORKERS  # noqa: F401


def paginate_connection(
    client: ShopifyClient,
    build_query: Callable[[str], str],
    connection_path: Tuple[str, ...],
) -> List[Dict[str, Any]]:
    """Walk a cursor-paginated GraphQL connection, returning every node.

    `build_query(after_clause)` must return a full query string with `after_clause`
    spliced into the connection's argument list (e.g. `(first: 100{after_clause})`).
    `after_clause` is either "" for the first page or `, after: "<cursor>"` afterward.
    """
    nodes: List[Dict[str, Any]] = []
    after_clause = ""

    while True:
        query = build_query(after_clause)
        data = retry_with_backoff(lambda: client.query(query))

        connection = data
        for key in connection_path:
            connection = connection[key]

        nodes.extend(edge["node"] for edge in connection.get("edges", []))

        page_info = connection.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        after_clause = f", after: {gql_quote(page_info['endCursor'])}"

    return nodes


def export_metafields(node_metafields_connection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a `metafields(first: N) { edges { node { ... } } }` selection into plain dicts."""
    return [
        {
            "namespace": edge["node"]["namespace"],
            "key": edge["node"]["key"],
            "value": edge["node"]["value"],
            "type": edge["node"]["type"],
        }
        for edge in (node_metafields_connection or {}).get("edges", [])
    ]


def mutation_errors(result: Dict[str, Any], mutation_name: str) -> List[Dict[str, str]]:
    return (result.get(mutation_name) or {}).get("userErrors") or []
