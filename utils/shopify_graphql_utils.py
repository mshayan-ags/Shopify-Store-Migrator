from typing import Any, Callable, Dict, List, Tuple

from utils.shopify_client import ShopifyClient
from transfer.transfer_store_metafields import retry_with_backoff, gql_quote

from utils.concurrency_utils import run_concurrently, DEFAULT_WORKERS


def paginate_connection(
    client: ShopifyClient,
    build_query: Callable[[str], str],
    connection_path: Tuple[str, ...],
) -> List[Dict[str, Any]]:
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
