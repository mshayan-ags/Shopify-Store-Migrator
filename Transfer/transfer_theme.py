import argparse
import base64
import io
import json
import logging
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from transfer.transfer_product import make_client
from transfer.transfer_collections import download_image
from utils.shopify_graphql_utils import paginate_connection, mutation_errors
from utils.concurrency_utils import retry_with_backoff, gql_quote
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_theme")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

MAX_FILES_PER_UPSERT = 50
MAX_FILES_PER_CREATE = 20
MAX_NODES_PER_QUERY = 250
FILE_POLL_ATTEMPTS = 20
FILE_POLL_DELAY = 2.0

FILENAME_CHARS = r"[A-Za-z0-9_.\-%]+"
SHOP_IMAGES_RE = re.compile(r"shopify://shop_images/(" + FILENAME_CHARS + ")")
CDN_ABSOLUTE_RE = re.compile(
    r"(?:https?:)?//[a-zA-Z0-9.\-]+/(?:s/files/[0-9/]+/files|cdn/shop/files)/("
    + FILENAME_CHARS
    + r")(?:\?[A-Za-z0-9_=&.\-%]*)?"
)


def chunk(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def find_referenced_filenames(files: List[Dict[str, Any]]) -> Set[str]:
    names: Set[str] = set()
    for f in files:
        if f.get("kind") != "text":
            continue
        content = f.get("content") or ""
        names.update(SHOP_IMAGES_RE.findall(content))
        names.update(m.group(1) for m in CDN_ABSOLUTE_RE.finditer(content))
    return names


def fetch_shop_files_index(client) -> Dict[str, Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        query {{
          files(first: 100{after_clause}) {{
            edges {{
              node {{
                __typename
                ... on MediaImage {{ id alt image {{ url }} }}
                ... on GenericFile {{ id alt url }}
              }}
            }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    nodes = paginate_connection(client, build_query, ("files",))
    index: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        typename = node.get("__typename")
        if typename == "MediaImage":
            url = (node.get("image") or {}).get("url")
            content_type = "IMAGE"
        elif typename == "GenericFile":
            url = node.get("url")
            content_type = "FILE"
        else:
            continue
        if not url:
            continue
        filename = urlparse(url).path.rsplit("/", 1)[-1]
        index[filename] = {"url": url, "alt": node.get("alt"), "content_type": content_type}
    return index


def sync_referenced_files(
    src_client,
    dest_client,
    filenames: Set[str],
    src_index: Optional[Dict[str, Dict[str, Any]]] = None,
    dest_index: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, str]:
    if not filenames:
        return {}

    if src_index is None:
        logger.info("Theme references %s file(s) from Content > Files -- indexing source library", len(filenames))
        src_index = fetch_shop_files_index(src_client)
    if dest_index is None:
        logger.info("Indexing destination Files library for de-dup")
        dest_index = fetch_shop_files_index(dest_client)

    filename_to_url: Dict[str, str] = {name: info["url"] for name, info in dest_index.items() if name in filenames}

    missing = sorted(name for name in filenames if name not in src_index)
    if missing:
        logger.warning(
            "%s referenced filename(s) not found in source Files library, skipped: %s",
            len(missing),
            missing[:10],
        )

    to_create = [
        {"filename": name, **src_index[name]}
        for name in sorted(filenames)
        if name in src_index and name not in dest_index
    ]
    logger.info("%s file(s) already exist on destination, creating %s new file(s)", len(filename_to_url), len(to_create))

    created: List[tuple] = []
    for batch in chunk(to_create, MAX_FILES_PER_CREATE):
        file_inputs = ", ".join(
            "{ originalSource: %s, filename: %s, contentType: %s, alt: %s, duplicateResolutionMode: APPEND_UUID }"
            % (gql_quote(f["url"]), gql_quote(f["filename"]), f["content_type"], gql_quote(f.get("alt") or ""))
            for f in batch
        )
        mutation = f"""
        mutation {{
          fileCreate(files: [{file_inputs}]) {{
            files {{ id }}
            userErrors {{ field message }}
          }}
        }}
        """
        try:
            result = retry_with_backoff(lambda: dest_client.mutation(mutation))
        except Exception as e:
            logger.warning("fileCreate batch failed: %s", e)
            continue

        errors = mutation_errors(result, "fileCreate")
        if errors:
            logger.warning("fileCreate batch had userErrors: %s", errors)

        for node, f in zip(result.get("fileCreate", {}).get("files", []), batch):
            created.append((node["id"], f["filename"]))

    pending = created
    for _ in range(FILE_POLL_ATTEMPTS):
        if not pending:
            break
        still_pending = []
        for id_batch in chunk(pending, MAX_NODES_PER_QUERY):
            id_to_filename = dict(id_batch)
            ids_clause = ", ".join(gql_quote(i) for i, _ in id_batch)
            query = f"""
            query {{
              nodes(ids: [{ids_clause}]) {{
                id
                ... on MediaImage {{ fileStatus image {{ url }} }}
                ... on GenericFile {{ fileStatus url }}
              }}
            }}
            """
            result = retry_with_backoff(lambda: dest_client.query(query))
            for node in result.get("nodes", []):
                if not node:
                    continue
                filename = id_to_filename.get(node["id"])
                status = node.get("fileStatus")
                url = (node.get("image") or {}).get("url") or node.get("url")
                if status == "READY" and url:
                    filename_to_url[filename] = url
                elif status == "FAILED":
                    logger.warning("File '%s' failed processing on destination, skipping", filename)
                else:
                    still_pending.append((node["id"], filename))
        pending = still_pending
        if pending:
            time.sleep(FILE_POLL_DELAY)

    if pending:
        logger.warning("%s file(s) never reached READY status in time: %s", len(pending), [fn for _, fn in pending])

    for filename, url in filename_to_url.items():
        dest_index.setdefault(filename, {"url": url, "alt": None, "content_type": "IMAGE"})

    logger.info("Files sync complete: %s created, %s resolved to a usable URL", len(created), len(filename_to_url))
    return filename_to_url


def rewrite_cdn_references(files: List[Dict[str, Any]], filename_to_new_url: Dict[str, str]) -> int:
    if not filename_to_new_url:
        return 0

    pattern = re.compile(
        r"(?:https?:)?//[a-zA-Z0-9.\-]+/(?:s/files/[0-9/]+/files|cdn/shop/files)/("
        + "|".join(re.escape(name) for name in filename_to_new_url)
        + r")(?:\?[A-Za-z0-9_=&.\-%]*)?"
    )

    def replace(match: "re.Match") -> str:
        return filename_to_new_url[match.group(1)]

    rewritten = 0
    for f in files:
        if f.get("kind") != "text":
            continue
        content = f.get("content") or ""
        new_content, count = pattern.subn(replace, content)
        if count:
            f["content"] = new_content
            rewritten += count
    return rewritten


def fetch_all_themes(client) -> List[Dict[str, str]]:
    query = """
    query {
      themes(first: 50) {
        edges { node { id name role } }
      }
    }
    """
    data = retry_with_backoff(lambda: client.query(query))
    return [edge["node"] for edge in data["themes"]["edges"]]


def find_theme_id(client, role: str = "MAIN") -> Dict[str, str]:
    themes = fetch_all_themes(client)
    for t in themes:
        if t["role"] == role:
            return t
    raise RuntimeError(f"No theme with role '{role}' found. Available themes: {themes}")


def fetch_theme_filenames_only(client, theme_id: str) -> Set[str]:
    def build_query(after_clause: str) -> str:
        return f"""
        query {{
          theme(id: {gql_quote(theme_id)}) {{
            files(first: 100{after_clause}) {{
              edges {{ node {{ filename }} }}
              pageInfo {{ hasNextPage endCursor }}
            }}
          }}
        }}
        """

    nodes = paginate_connection(client, build_query, ("theme", "files"))
    return {n["filename"] for n in nodes}


def fetch_one_theme_file_with_body(client, theme_id: str, filename: str) -> Optional[Dict[str, Any]]:
    query = f"""
    query {{
      theme(id: {gql_quote(theme_id)}) {{
        files(first: 1, filenames: [{gql_quote(filename)}]) {{
          edges {{
            node {{
              filename
              contentType
              body {{
                __typename
                ... on OnlineStoreThemeFileBodyText {{ content }}
                ... on OnlineStoreThemeFileBodyBase64 {{ contentBase64 }}
                ... on OnlineStoreThemeFileBodyUrl {{ url }}
              }}
            }}
          }}
        }}
      }}
    }}
    """
    data = retry_with_backoff(lambda: client.query(query))
    edges = ((data.get("theme") or {}).get("files") or {}).get("edges") or []
    return edges[0]["node"] if edges else None


def recover_silently_dropped_files(client, theme_id: str, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    all_filenames = fetch_theme_filenames_only(client, theme_id)
    fetched_filenames = {n["filename"] for n in nodes}
    missing = sorted(all_filenames - fetched_filenames)
    if not missing:
        return []

    logger.warning(
        "%s file(s) present in the theme but silently dropped by the content fetch -- retrying individually: %s",
        len(missing),
        missing,
    )

    recovered = []
    unrecoverable = []
    for filename in missing:
        node = fetch_one_theme_file_with_body(client, theme_id, filename)
        if node is not None:
            recovered.append(node)
        else:
            unrecoverable.append(filename)

    if recovered:
        logger.info("Recovered %s of %s previously-dropped file(s) on retry", len(recovered), len(missing))
    if unrecoverable:
        logger.error(
            "%s file(s) cannot be read via the Admin API at all right now (Shopify-side, not fixable from here) "
            "-- these will be MISSING from the destination theme, recreate them manually: %s",
            len(unrecoverable),
            unrecoverable,
        )
    return recovered


def export_theme(client, theme_id: Optional[str], role: str, out_dir: Path) -> Dict[str, Any]:
    if theme_id:
        match = next((t for t in fetch_all_themes(client) if t["id"] == theme_id), None)
        theme = match or {"id": theme_id, "name": None, "role": role}
    else:
        theme = find_theme_id(client, role=role)

    logger.info("Exporting theme '%s' (%s, role=%s)", theme.get("name"), theme["id"], theme.get("role"))

    def build_query(after_clause: str) -> str:
        return f"""
        query {{
          theme(id: {gql_quote(theme["id"])}) {{
            files(first: 50{after_clause}) {{
              edges {{
                node {{
                  filename
                  contentType
                  body {{
                    __typename
                    ... on OnlineStoreThemeFileBodyText {{ content }}
                    ... on OnlineStoreThemeFileBodyBase64 {{ contentBase64 }}
                    ... on OnlineStoreThemeFileBodyUrl {{ url }}
                  }}
                }}
              }}
              pageInfo {{ hasNextPage endCursor }}
            }}
          }}
        }}
        """

    nodes = paginate_connection(client, build_query, ("theme", "files"))
    logger.info("Fetched %s theme file(s)", len(nodes))

    recovered = recover_silently_dropped_files(client, theme["id"], nodes)
    nodes.extend(recovered)

    assets_dir = out_dir / "theme_assets"
    files: List[Dict[str, Any]] = []

    for node in nodes:
        filename = node["filename"]
        body = node.get("body") or {}
        entry: Dict[str, Any] = {"filename": filename, "content_type": node.get("contentType")}

        if "content" in body:
            entry["kind"] = "text"
            entry["content"] = body["content"]
        elif "contentBase64" in body:
            entry["kind"] = "binary"
            local_path = assets_dir / filename
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(base64.b64decode(body["contentBase64"]))
            entry["local_path"] = str(local_path)
        elif "url" in body:
            entry["kind"] = "binary"
            local_path = assets_dir / filename
            local_path.parent.mkdir(parents=True, exist_ok=True)
            download_image(body["url"], local_path)
            entry["local_path"] = str(local_path)
        else:
            logger.warning("Skipping file with unrecognized body type: %s", filename)
            continue

        files.append(entry)

    return {"theme_name": theme.get("name"), "theme_role": theme.get("role"), "files": files}


def build_placeholder_theme_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("layout/theme.liquid", "<html><body>{{ content_for_layout }}</body></html>")
        zf.writestr("config/settings_schema.json", "[]")
    return buf.getvalue()


def stage_upload(dest_client, filename: str, mime_type: str, content: bytes) -> str:
    mutation = f"""
    mutation {{
      stagedUploadsCreate(input: [{{
        filename: {gql_quote(filename)}
        mimeType: {gql_quote(mime_type)}
        resource: FILE
        httpMethod: POST
      }}]) {{
        stagedTargets {{ url resourceUrl parameters {{ name value }} }}
        userErrors {{ field message }}
      }}
    }}
    """
    result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    errors = mutation_errors(result, "stagedUploadsCreate")
    if errors:
        raise RuntimeError(f"stagedUploadsCreate failed: {errors}")

    target = result["stagedUploadsCreate"]["stagedTargets"][0]
    params = {p["name"]: p["value"] for p in target["parameters"]}
    resp = requests.post(target["url"], data=params, files={"file": (filename, content, mime_type)}, timeout=60)
    resp.raise_for_status()
    return target["resourceUrl"]


def create_seed_theme(dest_client, name: str, seed_zip_url: Optional[str]) -> str:
    if seed_zip_url:
        source = seed_zip_url
    else:
        source = stage_upload(dest_client, "seed-theme.zip", "application/zip", build_placeholder_theme_zip())

    mutation = f"""
    mutation {{
      themeCreate(source: {gql_quote(source)}, name: {gql_quote(name)}, role: UNPUBLISHED) {{
        theme {{ id name role }}
        userErrors {{ field message }}
      }}
    }}
    """
    result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    errors = mutation_errors(result, "themeCreate")
    if errors:
        raise RuntimeError(f"themeCreate failed: {errors}")
    return result["themeCreate"]["theme"]["id"]


def build_file_input(f: Dict[str, Any]) -> Optional[str]:
    if f.get("kind") == "text":
        value = f.get("content") or ""
        body_type = "TEXT"
    else:
        local_path = f.get("local_path")
        if not local_path or not Path(local_path).exists():
            logger.warning("Missing local asset for %s, skipping", f["filename"])
            return None
        value = base64.b64encode(Path(local_path).read_bytes()).decode("ascii")
        body_type = "BASE64"

    return "{ filename: %s, body: { type: %s, value: %s } }" % (gql_quote(f["filename"]), body_type, gql_quote(value))


def try_upsert_batch(dest_client, theme_id: str, batch: List[Dict[str, Any]]) -> Optional[Any]:
    file_inputs = [inp for inp in (build_file_input(f) for f in batch) if inp]
    if not file_inputs:
        return []

    mutation = f"""
    mutation {{
      themeFilesUpsert(themeId: {gql_quote(theme_id)}, files: [{", ".join(file_inputs)}]) {{
        job {{ id }}
        userErrors {{ field message }}
      }}
    }}
    """
    try:
        result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    except Exception as e:
        logger.warning("themeFilesUpsert request failed for a batch of %s file(s): %s", len(batch), e)
        return None

    return mutation_errors(result, "themeFilesUpsert")


def upsert_files_pass(dest_client, theme_id: str, files: List[Dict[str, Any]], uploaded: Set[str]) -> None:
    def process(batch: List[Dict[str, Any]]) -> None:
        if not batch:
            return
        errors = try_upsert_batch(dest_client, theme_id, batch)
        if errors == []:
            uploaded.update(f["filename"] for f in batch)
            return
        if len(batch) == 1:
            logger.warning("File '%s' failed permanently: %s", batch[0]["filename"], errors)
            return
        mid = len(batch) // 2
        process(batch[:mid])
        process(batch[mid:])

    for batch in chunk(files, MAX_FILES_PER_UPSERT):
        process(batch)
        logger.info("Upserted %s/%s file(s) so far", len(uploaded), len(files))


def upsert_files(dest_client, theme_id: str, files: List[Dict[str, Any]]) -> None:
    uploaded: Set[str] = set()
    upsert_files_pass(dest_client, theme_id, files, uploaded)

    missing = [f for f in files if f["filename"] not in uploaded]
    if missing:
        logger.info(
            "First pass: %s/%s uploaded. Retrying %s file(s) that failed -- likely uploaded out of order "
            "relative to something they reference (e.g. a section not yet present); every file now exists "
            "so this pass should resolve pure ordering failures.",
            len(uploaded),
            len(files),
            len(missing),
        )
        upsert_files_pass(dest_client, theme_id, missing, uploaded)

    failed = len(files) - len(uploaded)
    logger.info("Theme file upload complete: %s uploaded, %s failed", len(uploaded), failed)


def publish_theme(dest_client, theme_id: str) -> None:
    mutation = f"""
    mutation {{
      themePublish(id: {gql_quote(theme_id)}) {{
        theme {{ id name role }}
        userErrors {{ field message }}
      }}
    }}
    """
    result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    errors = mutation_errors(result, "themePublish")
    if errors:
        raise RuntimeError(f"themePublish failed: {errors}")
    logger.info("Theme published live on destination store")


def import_theme(
    dest_client,
    exported: Dict[str, Any],
    name: str,
    seed_zip_url: Optional[str],
    publish: bool,
    src_client=None,
    src_files_index: Optional[Dict[str, Dict[str, Any]]] = None,
    dest_files_index: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    files = exported.get("files", [])

    if src_client is not None:
        referenced = find_referenced_filenames(files)
        filename_to_new_url = sync_referenced_files(
            src_client, dest_client, referenced, src_index=src_files_index, dest_index=dest_files_index
        )
        rewritten = rewrite_cdn_references(files, filename_to_new_url)
        logger.info("Rewrote %s hardcoded CDN URL reference(s) in theme content to point at the destination store", rewritten)
    else:
        logger.warning(
            "No src_client passed to import_theme -- skipping Content > Files sync. "
            "Images referenced via shopify://shop_images/ or a hardcoded source CDN URL will be broken."
        )

    logger.info(
        "Creating seed theme '%s' on destination from %s",
        name,
        seed_zip_url or "a locally-built placeholder zip (staged upload)",
    )
    theme_id = create_seed_theme(dest_client, name, seed_zip_url)
    logger.info("Created theme %s, uploading %s file(s)", theme_id, len(files))
    upsert_files(dest_client, theme_id, files)
    if publish:
        publish_theme(dest_client, theme_id)
    else:
        logger.info("Theme left UNPUBLISHED. Re-run with --publish (or publish manually) to make it live.")


STANDARD_PLAN_THEME_LIMIT = 20


def transfer_all_themes(src_client, dest_client, out_dir: Path, publish_main: bool = False, theme_limit: int = STANDARD_PLAN_THEME_LIMIT) -> None:
    themes = fetch_all_themes(src_client)
    logger.info("Found %s theme(s) on the source store", len(themes))

    existing_dest_count = len(fetch_all_themes(dest_client))
    remaining_capacity = max(0, theme_limit - existing_dest_count)
    logger.info(
        "Destination already has %s/%s theme(s) -- room for %s more this run",
        existing_dest_count,
        theme_limit,
        remaining_capacity,
    )

    themes_sorted = sorted(themes, key=lambda t: 0 if t.get("role") == "MAIN" else 1)
    to_transfer = themes_sorted[:remaining_capacity]
    skipped_for_capacity = themes_sorted[remaining_capacity:]
    if skipped_for_capacity:
        logger.warning(
            "%s theme(s) skipped -- no room on the destination (delete unused destination themes and re-run "
            "to pick up the rest): %s",
            len(skipped_for_capacity),
            [t.get("name") for t in skipped_for_capacity],
        )

    shared_src_index: Optional[Dict[str, Dict[str, Any]]] = None
    shared_dest_index: Optional[Dict[str, Dict[str, Any]]] = None
    succeeded = 0
    failed = 0

    for i, theme in enumerate(to_transfer, 1):
        logger.info("=== Theme %s/%s: '%s' (role=%s) ===", i, len(to_transfer), theme.get("name"), theme.get("role"))
        try:
            exported = export_theme(src_client, theme["id"], theme.get("role"), out_dir)

            ts = int(time.time())
            theme_numeric_id = theme["id"].rsplit("/", 1)[-1]
            out_file = out_dir / f"theme_export_{theme_numeric_id}_{ts}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(exported, f, indent=2, ensure_ascii=False)

            if shared_src_index is None:
                logger.info("Indexing source store's Files library (shared across every theme in this run)")
                shared_src_index = fetch_shop_files_index(src_client)
            if shared_dest_index is None:
                logger.info("Indexing destination store's Files library (shared across every theme in this run)")
                shared_dest_index = fetch_shop_files_index(dest_client)

            name = f"{theme.get('name') or theme_numeric_id} (migrated)"
            should_publish = publish_main and theme.get("role") == "MAIN"
            import_theme(
                dest_client,
                exported,
                name,
                None,
                should_publish,
                src_client=src_client,
                src_files_index=shared_src_index,
                dest_files_index=shared_dest_index,
            )
            succeeded += 1
        except Exception:
            logger.exception(
                "Failed to transfer theme '%s' (%s) -- continuing with the rest", theme.get("name"), theme.get("id")
            )
            failed += 1

    logger.info(
        "All-themes transfer complete: %s succeeded, %s failed, %s skipped for capacity",
        succeeded,
        failed,
        len(skipped_for_capacity),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer theme code from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Actually create the theme(s) on the destination store")
    parser.add_argument("--all", action="store_true", help="Transfer every theme on the source store, not just one (ignores --theme-id/--role/--name)")
    parser.add_argument("--theme-id", default=None, help="Source theme GID to export (default: the MAIN/published theme)")
    parser.add_argument("--role", default="MAIN", help="Source theme role to look up if --theme-id isn't given (default: MAIN)")
    parser.add_argument("--name", default=None, help="Name for the new destination theme (default: '<source name> (migrated)')")
    parser.add_argument("--seed-zip", default=None, help="Override: a theme zip URL to seed the destination theme from, instead of the built-in placeholder")
    parser.add_argument("--publish", action="store_true", help="Publish live after import -- with --all, only the source's MAIN theme is published; other roles never are")
    parser.add_argument("--out", default="Results", help="Output directory for the export")
    parser.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import a single previously-saved theme export JSON file "
            "instead (the shape produced by this script's own dry-run export -- see "
            "docs/CANONICAL_SCHEMA.md). Lets you replay a prior dry-run export, or import a theme bundle "
            "produced by some other means, without a live source Shopify store -- no SRC_SHOPIFY_* "
            "credentials needed in this mode. LIMITATION: Content > Files image/reference syncing "
            "(shopify://shop_images/ and hardcoded source-CDN URLs) is skipped entirely, since resolving "
            "those requires a live query against the source store's Files library -- such references will "
            "be broken on the destination until fixed up manually. Binary theme files are read from the "
            "'local_path' the original export wrote to disk, so that local theme_assets folder must still "
            "be present alongside the JSON file. Not compatible with --all."
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
        if args.all:
            raise RuntimeError("--import-from is not compatible with --all -- pass a single theme export file instead")

        logger.info("Loading theme export from %s (skipping source fetch)", args.import_from)
        with open(args.import_from, "r", encoding="utf-8") as f:
            exported = json.load(f)

        if args.execute:
            name = args.name or f"{exported.get('theme_name') or 'Source theme'} (migrated)"
            import_theme(dest_client, exported, name, args.seed_zip, args.publish, src_client=None)
        else:
            logger.info("Dry-run finished. Re-run with --execute to create the theme on the destination store")
        return

    src_shop = os.getenv("SRC_SHOPIFY_SHOP")
    src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
    require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

    src_client = make_client(src_shop, src_token)

    if args.all:
        if not args.execute:
            themes = fetch_all_themes(src_client)
            logger.info("Found %s theme(s) on the source store (dry-run -- re-run with --execute to transfer them):", len(themes))
            for t in themes:
                logger.info("  %s  role=%s  %s", t["id"], t.get("role"), t.get("name"))
            return
        transfer_all_themes(src_client, dest_client, out_dir, publish_main=args.publish)
        return

    exported = export_theme(src_client, args.theme_id, args.role, out_dir)

    ts = int(time.time())
    out_file = out_dir / f"theme_export_{ts}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(exported, f, indent=2, ensure_ascii=False)
    logger.info("Export complete: %s (binary assets under %s)", out_file, out_dir / "theme_assets")

    if args.execute:
        name = args.name or f"{exported.get('theme_name') or 'Source theme'} (migrated)"
        import_theme(dest_client, exported, name, args.seed_zip, args.publish, src_client=src_client)
    else:
        logger.info("Dry-run finished. Re-run with --execute to create the theme on the destination store")


if __name__ == "__main__":
    main()
