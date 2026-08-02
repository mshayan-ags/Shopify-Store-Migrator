"""Thread-pool and retry helpers shared by every transfer_*.py script.

Deliberately has zero imports from any other project module: transfer_collections.py
is imported by transfer_store_metafields.py, which shopify_graphql_utils.py also
imports -- putting this here (rather than in shopify_graphql_utils.py or
transfer_store_metafields.py) avoids a transfer_collections -> shopify_graphql_utils
-> transfer_store_metafields -> transfer_collections import cycle. retry_with_backoff
and gql_quote live here (not just the thread pool) for the same reason: transfer_collections.py
needs to retry its own raw REST calls without importing transfer_store_metafields.py.
"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, List, TypeVar

logger = logging.getLogger("concurrency_utils")

T = TypeVar("T")


def retry_with_backoff(
    func: Callable[..., T],
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
) -> T:
    """Retry a function with exponential backoff for transient errors.

    Args:
        func: Callable to execute
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
        backoff_factor: Multiplier for delay after each retry

    Returns:
        Result of the function call

    Raises:
        RuntimeError: If all retries are exhausted
    """
    last_exception = None
    delay = base_delay

    for attempt in range(max_retries + 1):
        try:
            return func()
        except RuntimeError as e:
            last_exception = e
            # Check if it's a transient error: HTTP-level (503/429/500/502/504) or a
            # network-level hiccup (reset/aborted connections, timeouts, TLS hiccups)
            # that ShopifyClient wraps into a RuntimeError without an HTTP status code.
            error_msg = str(e)
            error_msg_lower = error_msg.lower()
            is_transient = any(
                code in error_msg for code in ["503", "429", "500", "502", "504"]
            ) or any(
                phrase in error_msg_lower
                for phrase in [
                    "connection aborted",
                    "connection reset",
                    "connectionreseterror",
                    "connectionerror",
                    "remote host",
                    "remotedisconnected",
                    "timed out",
                    "timeout",
                    "read timed out",
                    "handshake",
                    "protocolerror",
                    "decryption failed",
                    "bad record mac",
                ]
            )

            if not is_transient or attempt >= max_retries:
                raise

            logger.warning(
                f"Transient error on attempt {attempt + 1}/{max_retries + 1}: {error_msg}. "
                f"Retrying in {delay:.1f}s..."
            )
            time.sleep(delay)
            delay = min(delay * backoff_factor, max_delay)

    raise last_exception


def gql_quote(value: Any) -> str:
    """Render a Python value as a GraphQL string literal.

    ensure_ascii=False is required, not cosmetic: json.dumps's default
    ensure_ascii=True encodes any astral-plane character (nearly all emoji,
    e.g. the U+1F44D thumbs-up in a real Src theme template) as a
    UTF-16 surrogate-pair \\u escape (e.g. \\ud83d\\udc4d). That's valid JSON
    but not a valid GraphQL string literal -- GraphQL's grammar has no
    surrogate-pair escape mechanism, so Shopify's parser sees the lone
    high-surrogate escape and rejects the whole request with a hard
    "Parse error on bad Unicode escape sequence", confirmed live against a
    real theme file. With ensure_ascii=False the character is emitted as a
    raw UTF-8 source character instead (which GraphQL's grammar explicitly
    allows), and the request/response JSON transport layer (a separate,
    outer encoding step) still carries it losslessly either way.
    """
    return json.dumps("" if value is None else str(value), ensure_ascii=False)

DEFAULT_WORKERS = 500
# Shopify's Admin API rate-limits per store regardless of how many threads call
# it (REST: ~2 req/s leaky bucket; GraphQL: a shared cost budget). Past that
# ceiling, extra workers don't add throughput -- they just queue up in
# retry_with_backoff's 429 retries. At this worker count, expect a lot of
# rate-limit retries under the hood; each script exposes --workers so this can
# be tuned down if that becomes counterproductive.


def run_concurrently(
    items: List[T],
    worker_fn: Callable[[T], None],
    max_workers: int = DEFAULT_WORKERS,
    label: str = "item",
) -> None:
    """Run worker_fn(item) for every item on a thread pool.

    Threads (not processes) are correct here: this work is entirely network
    I/O-bound (HTTP calls to Shopify), and Python releases the GIL during
    socket I/O, so real concurrency is achieved despite the GIL.

    worker_fn is expected to handle its own per-item logging/counters (every
    caller here does, matching the pre-existing sequential-loop pattern) --
    this just catches and logs any exception that slips through uncaught, so
    one bad item can't crash the whole pool.
    """
    if max_workers <= 1:
        for item in items:
            try:
                worker_fn(item)
            except Exception:
                logger.exception("Unhandled error processing %s", label)
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(worker_fn, item): item for item in items}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                logger.exception("Unhandled error processing %s", label)
