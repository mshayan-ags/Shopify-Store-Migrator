import logging
import os
import time
from typing import Any, Dict, Generator, List, Optional

import requests
from dotenv import load_dotenv

from utils.concurrency_utils import retry_with_backoff

load_dotenv()

logger = logging.getLogger("bigcommerce_client")

BIGCOMMERCE_STORE_HASH = os.getenv("BIGCOMMERCE_STORE_HASH", "")
BIGCOMMERCE_ACCESS_TOKEN = os.getenv("BIGCOMMERCE_ACCESS_TOKEN", "")
BIGCOMMERCE_CLIENT_ID = os.getenv("BIGCOMMERCE_CLIENT_ID", "")

API_TIMEOUT = 30
DEFAULT_PAGE_LIMIT = 250


class BigCommerceClient:
    def __init__(
        self,
        store_hash: str = BIGCOMMERCE_STORE_HASH,
        access_token: str = BIGCOMMERCE_ACCESS_TOKEN,
        client_id: str = BIGCOMMERCE_CLIENT_ID,
        api_timeout: int = API_TIMEOUT,
    ):
        if not store_hash:
            raise RuntimeError(
                "BIGCOMMERCE_STORE_HASH is not set (env var or constructor arg required)"
            )
        if not access_token:
            raise RuntimeError(
                "BIGCOMMERCE_ACCESS_TOKEN is not set (env var or constructor arg required)"
            )

        self.store_hash = store_hash
        self.access_token = access_token
        self.client_id = client_id
        self.api_timeout = api_timeout

        self.base_url_v3 = f"https://api.bigcommerce.com/stores/{store_hash}/v3"
        self.base_url_v2 = f"https://api.bigcommerce.com/stores/{store_hash}/v2"

        self.headers = {
            "X-Auth-Token": self.access_token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.client_id:
            self.headers["X-Auth-Client"] = self.client_id

    def _base_url(self, api_version: str) -> str:
        if api_version == "v2":
            return self.base_url_v2
        if api_version == "v3":
            return self.base_url_v3
        raise ValueError(f"Unsupported BigCommerce api_version: {api_version!r}")

    def get(
        self, path: str, params: Optional[Dict[str, Any]] = None, api_version: str = "v3"
    ) -> Any:
        url = f"{self._base_url(api_version)}/{path.lstrip('/')}"

        def _do_request() -> Any:
            response = requests.get(
                url, headers=self.headers, params=params or {}, timeout=self.api_timeout
            )
            if response.status_code == 429:
                raise RuntimeError(f"BigCommerce API 429 rate limited: GET {url}")
            if response.status_code >= 500:
                raise RuntimeError(
                    f"BigCommerce API {response.status_code} server error: GET {url}"
                )
            if not response.ok:
                raise RuntimeError(
                    f"BigCommerce API {response.status_code} error: GET {url} -> {response.text[:500]}"
                )
            if not response.content:
                return None
            return response.json()

        return retry_with_backoff(_do_request)

    def get_paginated_v3(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> Generator[Dict[str, Any], None, None]:
        page = 1
        params = dict(params or {})
        while True:
            page_params = dict(params)
            page_params["page"] = page
            page_params["limit"] = limit

            body = self.get(path, params=page_params, api_version="v3") or {}
            items = body.get("data") or []
            for item in items:
                yield item

            pagination = (body.get("meta") or {}).get("pagination") or {}
            total_pages = pagination.get("total_pages")
            current_page = pagination.get("current_page", page)

            if not items:
                break
            if total_pages is not None:
                if current_page >= total_pages:
                    break
            elif len(items) < limit:
                break

            page += 1

    def get_paginated_v2(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        limit: int = DEFAULT_PAGE_LIMIT,
    ) -> Generator[Dict[str, Any], None, None]:
        page = 1
        params = dict(params or {})
        while True:
            page_params = dict(params)
            page_params["page"] = page
            page_params["limit"] = limit

            body = self.get(path, params=page_params, api_version="v2")
            items = body or []
            if not isinstance(items, list):
                break
            for item in items:
                yield item

            if len(items) < limit:
                break
            page += 1
