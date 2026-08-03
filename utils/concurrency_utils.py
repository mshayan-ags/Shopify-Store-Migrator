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
    last_exception = None
    delay = base_delay

    for attempt in range(max_retries + 1):
        try:
            return func()
        except RuntimeError as e:
            last_exception = e
            error_msg = str(e)

            explicit_retryable = getattr(e, "retryable", None)
            if explicit_retryable is not None:
                is_transient = explicit_retryable
            else:
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

            if not is_transient:
                raise

            if attempt >= max_retries:
                logger.warning(
                    "Giving up after %d attempt(s), last error: %s", attempt + 1, error_msg
                )
                raise

            logger.warning(
                "Transient error on attempt %d/%d: %s. Retrying in %.1fs...",
                attempt + 1, max_retries + 1, error_msg, delay,
            )
            time.sleep(delay)
            delay = min(delay * backoff_factor, max_delay)

    raise last_exception


def gql_quote(value: Any) -> str:
    return json.dumps("" if value is None else str(value), ensure_ascii=False)

DEFAULT_WORKERS = 500


def run_concurrently(
    items: List[T],
    worker_fn: Callable[[T], None],
    max_workers: int = DEFAULT_WORKERS,
    label: str = "item",
) -> None:
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
