import argparse
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

from transfer.transfer_product import make_client
from utils.shopify_graphql_utils import paginate_connection, mutation_errors
from utils.concurrency_utils import retry_with_backoff, gql_quote
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_files")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

MAX_FILES_PER_CREATE = 20

HASH_SUFFIX_RE = re.compile(r"^(.+)_[0-9a-fA-F]{8,}$")


def chunk(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def filename_key_from_path(path_or_filename: str) -> str:
    last = path_or_filename.rsplit("/", 1)[-1]
    stem, dot, ext = last.rpartition(".")
    if dot:
        match = HASH_SUFFIX_RE.match(stem)
        stem = match.group(1) if match else stem
        key = f"{stem}.{ext}"
    else:
        match = HASH_SUFFIX_RE.match(last)
        key = match.group(1) if match else last
    return key.lower()


def filename_key_from_url(url: str) -> str:
    return filename_key_from_path(urlparse(url).path)


def build_files_query(after_clause: str) -> str:
    return f"""
    query {{
      files(first: 50{after_clause}) {{
        edges {{
          node {{
            id
            alt
            createdAt
            fileStatus
            __typename
            ... on MediaImage {{ image {{ url width height }} }}
            ... on GenericFile {{ url originalFileSize mimeType }}
            ... on Video {{ filename sources {{ url }} }}
          }}
        }}
        pageInfo {{ hasNextPage endCursor }}
      }}
    }}
    """


def normalize_file_node(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    typename = node.get("__typename")

    if typename == "MediaImage":
        image = node.get("image") or {}
        url = image.get("url")
        if not url:
            return None
        return {
            "id": node["id"],
            "content_type": "IMAGE",
            "source_url": url,
            "alt": node.get("alt"),
            "filename_key": filename_key_from_url(url),
            "meta": {"width": image.get("width"), "height": image.get("height")},
        }

    if typename == "GenericFile":
        url = node.get("url")
        if not url:
            return None
        return {
            "id": node["id"],
            "content_type": "FILE",
            "source_url": url,
            "alt": node.get("alt"),
            "filename_key": filename_key_from_url(url),
            "meta": {"mime_type": node.get("mimeType"), "size": node.get("originalFileSize")},
        }

    if typename == "Video":
        sources = node.get("sources") or []
        url = sources[0].get("url") if sources else None
        if not url:
            return None
        filename = node.get("filename")
        key = filename_key_from_path(filename) if filename else filename_key_from_url(url)
        return {
            "id": node["id"],
            "content_type": "VIDEO",
            "source_url": url,
            "alt": node.get("alt"),
            "filename_key": key,
            "meta": {"filename": filename},
        }

    return None


def index_files(client) -> Dict[str, Dict[str, Any]]:
    nodes = paginate_connection(client, build_files_query, ("files",))

    index: Dict[str, Dict[str, Any]] = {}
    skipped_failed = 0
    skipped_unrecognized = 0

    for node in nodes:
        try:
            if node.get("fileStatus") == "FAILED":
                skipped_failed += 1
                continue
            entry = normalize_file_node(node)
            if entry is None:
                skipped_unrecognized += 1
                continue
            index[entry["filename_key"]] = entry
        except Exception:
            logger.exception("Failed to process a Files node, skipping: %s", node.get("id"))
            skipped_unrecognized += 1

    if skipped_failed:
        logger.info("Skipped %s file(s) with fileStatus=FAILED", skipped_failed)
    if skipped_unrecognized:
        logger.info("Skipped %s file(s) with an unrecognized type or missing URL (e.g. Model3d)", skipped_unrecognized)

    return index


def export_files(src_client) -> List[Dict[str, Any]]:
    src_index = index_files(src_client)
    exported = list(src_index.values())
    logger.info("Exported %s file(s) from the source Files library", len(exported))
    return exported


def import_files(dest_client, exported: List[Dict[str, Any]], dest_index: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    if dest_index is None:
        logger.info("Indexing destination Files library for de-dup")
        dest_index = index_files(dest_client)

    existing_keys = set(dest_index.keys())
    to_create = [f for f in exported if f["filename_key"] not in existing_keys]
    already_existing = len(exported) - len(to_create)
    logger.info(
        "%s file(s) already exist on destination (by filename match), creating %s new file(s)",
        already_existing,
        len(to_create),
    )

    created = 0
    failed = 0

    for batch in chunk(to_create, MAX_FILES_PER_CREATE):
        file_inputs = ", ".join(
            "{ originalSource: %s, alt: %s, contentType: %s }"
            % (gql_quote(f["source_url"]), gql_quote(f.get("alt") or ""), f["content_type"])
            for f in batch
        )
        mutation = f"""
        mutation {{
          fileCreate(files: [{file_inputs}]) {{
            files {{ id alt fileStatus }}
            userErrors {{ field message }}
          }}
        }}
        """
        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        except Exception as e:
            logger.warning("fileCreate batch of %s file(s) failed, skipping: %s", len(batch), e)
            failed += len(batch)
            continue

        errors = mutation_errors(result, "fileCreate")
        if errors:
            logger.warning("fileCreate batch of %s file(s) had userErrors, skipping: %s", len(batch), errors)
            failed += len(batch)
            continue

        created += len(result.get("fileCreate", {}).get("files", []))
        logger.info("Created %s/%s file(s) so far", created, len(to_create))

    logger.info(
        "Files import complete: %s created, %s already existed, %s failed",
        created,
        already_existing,
        failed,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer the standalone Content > Files library from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create missing files on the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    parser.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import this previously-saved canonical JSON file "
            "instead (see docs/CANONICAL_SCHEMA.md). Lets you import from a non-Shopify source "
            "connector or replay a prior dry-run export. No SRC_SHOPIFY_* credentials needed in "
            "this mode."
        ),
    )
    args = parser.parse_args()

    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")
    require_env(DEST_SHOPIFY_SHOP=dest_shop, DEST_SHOPIFY_ACCESS_TOKEN=dest_token)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    dest_client = make_client(dest_shop, dest_token)

    if args.import_from:
        logger.info("Loading export from %s (skipping source fetch)", args.import_from)
        if args.import_from.lower().endswith(".xlsx"):
            from utils.tabular_io import import_from_xlsx
            loaded = import_from_xlsx(args.import_from)
        else:
            with open(args.import_from, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        exported = loaded if isinstance(loaded, list) else [loaded]
    else:
        src_shop = os.getenv("SRC_SHOPIFY_SHOP")
        src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
        require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

        src_client = make_client(src_shop, src_token)

        logger.info("Indexing Content > Files library on %s", src_shop)
        exported = export_files(src_client)

        ts = int(time.time())
        out_file = out_dir / f"files_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)
        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"files_export_{ts}.xlsx")
        logger.info("Export complete: %s", out_file)

    if args.execute:
        logger.info("Importing files into %s", dest_shop)
        import_files(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to create missing files on the destination store")


if __name__ == "__main__":
    main()
