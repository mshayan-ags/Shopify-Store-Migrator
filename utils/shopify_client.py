import time
import logging
from typing import Dict, Any, Optional
import requests
from utils.config import (
    SRC_SHOPIFY_ACCESS_TOKEN,
    SHOPIFY_OAUTH_URL,
    SHOPIFY_ADMIN_API_URL,
    SRC_SHOPIFY_CLIENT_ID,
    SRC_SHOPIFY_CLIENT_SECRET,
    API_TIMEOUT,
)
from utils.errors import ShopifyAPIError

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _raise_for_request_error(e: requests.RequestException, context: str) -> None:
    status_code = e.response.status_code if isinstance(e, requests.HTTPError) and e.response is not None else None
    if status_code is not None:
        retryable = status_code in RETRYABLE_STATUS_CODES
    else:
        retryable = isinstance(e, (requests.ConnectionError, requests.Timeout))
    logger.error("%s: %s", context, e)
    raise ShopifyAPIError(f"{context}: {e}", retryable=retryable, status_code=status_code) from e


class ShopifyClient:
    def __init__(self, access_token: str = SRC_SHOPIFY_ACCESS_TOKEN):
        self.access_token = access_token
        self.token_expires_at = time.time() + 3600 * 24 * 365 if access_token else 0.0

        self.base_url = SHOPIFY_ADMIN_API_URL
        self.oauth_url = SHOPIFY_OAUTH_URL

        self.headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.access_token,
        }

    def set_shop(self, shop_base_url: str, oauth_url: Optional[str] = None) -> None:
        self.base_url = shop_base_url.rstrip("/")
        if oauth_url:
            self.oauth_url = oauth_url

    def _refresh_token(self) -> str:
        logger.info("Refreshing Shopify access token")

        try:
            response = requests.post(
                getattr(self, "oauth_url", SHOPIFY_OAUTH_URL),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": SRC_SHOPIFY_CLIENT_ID,
                    "client_secret": SRC_SHOPIFY_CLIENT_SECRET,
                },
                timeout=API_TIMEOUT,
            )
            response.raise_for_status()

            data = response.json()
            self.access_token = data["access_token"]
            self.token_expires_at = time.time() + data.get("expires_in", 3600)

            self.headers["X-Shopify-Access-Token"] = self.access_token

            logger.info("Access token refreshed successfully")
            return self.access_token

        except requests.RequestException as e:
            _raise_for_request_error(e, "Token refresh failed")

    def _ensure_valid_token(self) -> None:
        if time.time() >= self.token_expires_at - 60:
            self._refresh_token()

    def query(self, query: str) -> Dict[str, Any]:
        self._ensure_valid_token()

        try:
            response = requests.post(
                f"{self.base_url}/graphql.json",
                headers=self.headers,
                json={"query": query},
                timeout=API_TIMEOUT,
            )
            response.raise_for_status()

            payload = response.json()

            if payload.get("errors"):
                error_msg = str(payload["errors"])
                logger.error("GraphQL error: %s", error_msg)
                raise ShopifyAPIError(f"GraphQL error: {error_msg}", retryable=False)

            return payload.get("data", {})

        except requests.RequestException as e:
            _raise_for_request_error(e, "GraphQL request failed")

    def mutation(self, mutation: str) -> Dict[str, Any]:
        return self.query(mutation)

    def rest_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/{path}.json"
        try:
            resp = requests.get(url, headers=self.headers, params=params, timeout=API_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            _raise_for_request_error(e, f"REST GET failed ({url})")

    def rest_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/{path}.json"
        try:
            resp = requests.post(url, headers=self.headers, json=payload, timeout=API_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            _raise_for_request_error(e, f"REST POST failed ({url})")

    def rest_put(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/{path}.json"
        try:
            resp = requests.put(url, headers=self.headers, json=payload, timeout=API_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            _raise_for_request_error(e, f"REST PUT failed ({url})")

    def rest_delete(self, path: str) -> None:
        url = f"{self.base_url}/{path}.json"
        try:
            resp = requests.delete(url, headers=self.headers, timeout=API_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            _raise_for_request_error(e, f"REST DELETE failed ({url})")
