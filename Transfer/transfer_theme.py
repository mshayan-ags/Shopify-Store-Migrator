"""Transfer online store theme code from Src to dest.

Copies every file (Liquid templates/sections/snippets, JSON templates/config,
assets, locales) from a source theme into a brand-new theme on the destination
store, using the Admin GraphQL theme file APIs (`theme`, `themeFilesUpsert`)
rather than the Shopify CLI.

IMPORTANT -- this needs more than a scope toggle:
    `write_themes` must be granted on both stores' custom apps AND, per
    Shopify's own docs, theme file read/write additionally requires "an
    exemption from Shopify to modify themes" -- a separate approval Shopify
    grants to apps, on top of the access scope. Until that exemption is
    granted for this custom app on both stores, themeCreate/themeFilesUpsert
    calls will fail even with the scope present. If this script errors with
    an access/permission message, that -- not a missing scope -- is almost
    certainly why; contact Shopify support/Partner Dashboard to request it.

How theme creation actually works here:
    The Admin API has no "create an empty theme" mutation -- `themeCreate`
    requires a `source` (a URL to a theme .zip). By default this script builds
    a tiny placeholder theme zip in memory and uploads it through Shopify's own
    staged-upload pipeline (`stagedUploadsCreate` -> POST the bytes -> pass the
    resulting resourceUrl as `source`), then immediately overwrites every file
    with the source theme's real files via `themeFilesUpsert`. The placeholder
    content never actually ends up live -- it only exists so there's a theme ID
    to upsert real files into.

    Earlier versions of this script pointed `source` at a public GitHub zip
    URL instead -- don't go back to that. Verified live: both GitHub's
    `codeload.github.com` archive endpoint (no Content-Length header, chunked
    encoding) and its release-asset download (redirects to a short-lived
    signed URL) get rejected by themeCreate with "Src is empty", even though
    both were genuinely fetchable with curl -- Shopify's fetcher doesn't
    tolerate whatever it is about those responses. The staged-upload path
    sidesteps this entirely since Shopify is serving its own upload back to
    itself. Pass --seed-zip only if you have a URL you've already confirmed
    themeCreate accepts.

    Non-text files (images, fonts, video) are downloaded locally and
    re-uploaded as base64; text files (Liquid/JSON/CSS/JS/locale files) are
    copied inline. Batches are capped at 50 files per themeFilesUpsert call,
    matching Shopify's documented per-request limit.

Images referenced by the theme, not just theme package files:
    A theme's own asset folder isn't the only place images live. Confirmed
    against a live export of Src's theme: its settings_data.json and
    JSON templates reference 775 distinct images from Content > Files (the
    shop's general media library) -- 249 via `shopify://shop_images/<filename>`
    (banners/logos/section images picked in the theme customizer) and 552+ via
    a hardcoded absolute CDN URL (`cdn.shopify.com/s/files/.../files/<filename>`
    or a custom domain's `/cdn/shop/files/<filename>` proxy path). Neither is
    part of the theme's file bundle, so without handling them separately the
    imported theme would render with broken images everywhere despite every
    theme file copying successfully.

    This script handles both automatically on --execute:
      1. Scans every text theme file for both reference patterns.
      2. Indexes the source AND destination Content > Files libraries (only
         once, not per-reference) and creates on the destination whichever
         referenced files don't already exist there, via `fileCreate` --
         verified live: source CDN URLs are fetched directly by Shopify
         (`originalSource` accepts an external URL, no local download/staged
         upload needed), and polls each new file until `fileStatus: READY` to
         get its destination URL.
      3. `shopify://shop_images/<filename>` references need no rewriting --
         Shopify resolves those dynamically against the destination's own
         Files library by filename match at render time, once the same-named
         file exists there. Hardcoded absolute CDN URLs DO need rewriting
         (they point at the source store's specific shop path/domain, which
         won't resolve on the destination even with a same-named file
         present) -- rewritten in place before the theme files are uploaded.
    This only covers images actually referenced in the theme's own files --
    it does not sync the store's entire (possibly much larger) Files library,
    which would include lots of unused/orphaned uploads outside the theme's
    scope.

The new theme is created with role UNPUBLISHED by default -- it will NOT go
live automatically. Pass --publish to make it the live theme immediately;
that's a customer-facing, hard-to-silently-undo action, so it's opt-in only.

Not handled by this script:
    - Theme app-embed blocks/settings that reference a specific app's ID
      (those app IDs differ across stores; blocks tied to an app not
      installed on the destination will silently no-op there).
    - Theme editor customizations stored against a *different* theme than
      the one exported (e.g. draft/unpublished alternate themes) -- run this
      once per theme you want copied, passing --theme-id explicitly.
    - Metafields/settings_data.json values that reference product/collection/
      page/blog/metaobject IDs from the source store -- those IDs won't
      resolve on the destination and need manual remapping in the theme
      editor after import.
    - Video/3D model files referenced the same way as images (rare) -- only
      IMAGE and generic FILE content types are synced; extend
      fetch_shop_files_index's inline fragments to cover Video/Model3d if
      those turn out to matter for this theme.

Usage:
    python transfer_theme.py                      # dry-run export of the published theme
    python transfer_theme.py --execute             # create the theme on dest (unpublished)
    python transfer_theme.py --execute --publish   # create AND publish it live
    python transfer_theme.py --theme-id gid://shopify/OnlineStoreTheme/123 --execute
"""
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

from Transfer.transfer_product import make_client
from Transfer.transfer_collections import download_image
from utils.shopify_graphql_utils import paginate_connection, mutation_errors
from utils.concurrency_utils import retry_with_backoff, gql_quote

load_dotenv()

logger = logging.getLogger("transfer_theme")
logging.basicConfig(level=logging.INFO)

MAX_FILES_PER_UPSERT = 50
MAX_FILES_PER_CREATE = 20
MAX_NODES_PER_QUERY = 250
FILE_POLL_ATTEMPTS = 20
FILE_POLL_DELAY = 2.0

# Theme JSON/Liquid content references images picked in Content > Files two ways:
#   1. shopify://shop_images/<filename> -- resolved dynamically by filename against
#      the destination's own Files library, so it needs no rewriting as long as a
#      file with the same filename exists there.
#   2. An absolute CDN URL baked directly into the content (cdn.shopify.com/s/files/...
#      or a custom domain's /cdn/shop/files/... proxy path) -- this hardcodes the
#      SOURCE store's shop path/domain and will 404 on the destination even if a
#      same-named file exists there, so these need rewriting to the new absolute URL.
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
    """Scan every text theme file for Content > Files image/file references."""
    names: Set[str] = set()
    for f in files:
        if f.get("kind") != "text":
            continue
        content = f.get("content") or ""
        names.update(SHOP_IMAGES_RE.findall(content))
        names.update(m.group(1) for m in CDN_ABSOLUTE_RE.finditer(content))
    return names


def fetch_shop_files_index(client) -> Dict[str, Dict[str, Any]]:
    """Return {filename: {url, alt, content_type}} for every file in Content > Files."""

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
    """Ensure every Content > Files image/file the theme references also exists on
    the destination, and return {filename: destination_absolute_url} for rewriting.

    src_index/dest_index can be pre-fetched and passed in (and are mutated in
    place with newly-created entries) so a multi-theme run only indexes each
    store's Files library once instead of once per theme.
    """
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
    """Replace hardcoded source-store CDN URLs in theme text files with the
    matching destination URL. shopify://shop_images/ references need no rewrite --
    they resolve by filename against the destination's Files library at render time.
    """
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
    """Cheap filename-only pagination -- used as a completeness cross-check
    against the body-content fetch, which was confirmed live to silently drop
    files entirely (no error, node just doesn't appear in the connection) when
    a file's body can't be resolved for some Shopify-side reason.
    """

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
    """Cross-check the body-content fetch against a cheap filename-only fetch
    and try to individually recover anything missing. Confirmed live: this
    genuinely happens (2 files on a real Src theme, both present in the
    filename-only list, silently absent whenever body content was requested,
    even via a single-file filtered lookup) -- not a pagination fluke, and not
    fixable by changing page size (tested 10/20/50/100, same 2 files missing
    every time). When even the single-file retry comes back empty, the file
    truly can't be read via this API right now; it's logged clearly so it can
    be recreated by hand rather than silently missing from the destination.
    """
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
    """A minimal valid theme structure -- its content is irrelevant since every
    file gets overwritten by upsert_files() immediately after theme creation.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("layout/theme.liquid", "<html><body>{{ content_for_layout }}</body></html>")
        zf.writestr("config/settings_schema.json", "[]")
    return buf.getvalue()


def stage_upload(dest_client, filename: str, mime_type: str, content: bytes) -> str:
    """Upload bytes to Shopify's own staged-upload storage and return a resourceUrl
    themeCreate can use as `source`. Verified live: this is far more reliable than
    passing an external URL -- GitHub's codeload zip endpoint has no Content-Length
    header (chunked encoding) and its release-asset download redirects to a
    short-lived signed URL, and Shopify's themeCreate fetcher rejected both with
    "Src is empty" even though the URLs were genuinely fetchable by curl.
    """
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
    """Attempt one themeFilesUpsert call. Returns the raw userErrors list on a
    GraphQL-level failure (empty list means success), or None if the whole
    request failed to execute (network/transport error).
    """
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
    """Upload files in batches, adding each successfully-upserted filename to
    `uploaded`. A batch that fails (transport error or userErrors -- either can
    come from a single bad file poisoning the whole batch, e.g. a malformed
    unicode escape, or a file uploaded out of order relative to something it
    references, e.g. a section referenced before its file exists) is bisected
    and retried as smaller batches so one bad/out-of-order file can't also
    fail its ~49 good batch-mates. Isolates down to individual files only when
    truly necessary.
    """

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


STANDARD_PLAN_THEME_LIMIT = 20  # Shopify Plus allows 100; both stores here are confirmed non-Plus.


def transfer_all_themes(src_client, dest_client, out_dir: Path, publish_main: bool = False, theme_limit: int = STANDARD_PLAN_THEME_LIMIT) -> None:
    """Export and import every theme on the source store in one run.

    Each theme becomes its own new UNPUBLISHED theme on the destination (only
    the source's MAIN theme is optionally published, via publish_main -- every
    other role, e.g. UNPUBLISHED/DEVELOPMENT drafts, is never auto-published
    regardless of this flag). The Content > Files library is indexed once and
    shared across every theme's file sync instead of once per theme -- that
    indexing pass was the slow part in testing (hundreds of files), and a
    store's file library doesn't change mid-run, so re-indexing it per theme
    would be pure waste. A file referenced by more than one theme is created on
    the destination only once and reused for every later theme in the run.

    Standard (non-Plus) Shopify plans cap a store at 20 themes total; Plus
    allows 100 (theme_limit lets a Plus store override this). This checks the
    destination's current theme count up front and, if there isn't room for
    every source theme, transfers as many as fit -- MAIN first, since that's
    the one that actually matters most -- and logs exactly which ones were
    skipped for capacity rather than silently dropping them or failing
    partway through the run.

    A single theme's export or import failing is logged and does not stop the
    rest of the run, matching transfer_all.py's per-step failure handling.
    """
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
    args = parser.parse_args()

    src_shop = os.getenv("SRC_SHOPIFY_SHOP")
    src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")

    if not all([src_shop, src_token, dest_shop, dest_token]):
        raise RuntimeError(
            "Missing .env values: SRC_SHOPIFY_SHOP, SRC_SHOPIFY_ACCESS_TOKEN, DEST_SHOPIFY_SHOP, DEST_SHOPIFY_ACCESS_TOKEN"
        )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    src_client = make_client(src_shop, src_token)
    dest_client = make_client(dest_shop, dest_token)

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
