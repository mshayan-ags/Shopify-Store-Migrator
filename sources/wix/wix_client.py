import logging
import os
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

from utils.concurrency_utils import retry_with_backoff

load_dotenv()

logger = logging.getLogger("wix_client")

WIX_API_KEY = os.getenv("WIX_API_KEY", "")
WIX_SITE_ID = os.getenv("WIX_SITE_ID", "")

WIX_BASE_URL = "https://www.wixapis.com"
API_TIMEOUT = 30


class WixClient:
    def __init__(
        self,
        api_key: str = WIX_API_KEY,
        site_id: str = WIX_SITE_ID,
        base_url: str = WIX_BASE_URL,
        api_timeout: int = API_TIMEOUT,
    ):
        if not api_key:
            raise RuntimeError("WIX_API_KEY is not set (env var or constructor arg required)")
        if not site_id:
            raise RuntimeError("WIX_SITE_ID is not set (env var or constructor arg required)")

        self.api_key = api_key
        self.site_id = site_id
        self.base_url = base_url.rstrip("/")
        self.api_timeout = api_timeout

        self.headers = {
            "Authorization": self.api_key,
            "wix-site-id": self.site_id,
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self._url(path)

        def _do_request() -> Any:
            try:
                response = requests.get(
                    url, headers=self.headers, params=params or {}, timeout=self.api_timeout
                )
            except requests.exceptions.RequestException as e:
                raise RuntimeError(f"Wix API connection error: GET {url} -> {e}")

            if response.status_code == 429:
                raise RuntimeError(f"Wix API 429 rate limited: GET {url}")
            if response.status_code >= 500:
                raise RuntimeError(f"Wix API {response.status_code} server error: GET {url}")
            if not response.ok:
                raise RuntimeError(
                    f"Wix API {response.status_code} error: GET {url} -> {response.text[:500]}"
                )
            if not response.content:
                return None
            return response.json()

        return retry_with_backoff(_do_request)

    def post(self, path: str, json_body: Optional[Dict[str, Any]] = None) -> Any:
        url = self._url(path)

        def _do_request() -> Any:
            try:
                response = requests.post(
                    url, headers=self.headers, json=json_body or {}, timeout=self.api_timeout
                )
            except requests.exceptions.RequestException as e:
                raise RuntimeError(f"Wix API connection error: POST {url} -> {e}")

            if response.status_code == 429:
                raise RuntimeError(f"Wix API 429 rate limited: POST {url}")
            if response.status_code >= 500:
                raise RuntimeError(f"Wix API {response.status_code} server error: POST {url}")
            if not response.ok:
                raise RuntimeError(
                    f"Wix API {response.status_code} error: POST {url} -> {response.text[:500]}"
                )
            if not response.content:
                return None
            return response.json()

        return retry_with_backoff(_do_request)
