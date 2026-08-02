"""
Shopify GraphQL API Client
Handles authentication and GraphQL queries/mutations
"""
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

logger = logging.getLogger(__name__)


class ShopifyClient:
    """Client for interacting with Shopify GraphQL Admin API"""

    def __init__(self, access_token: str = SRC_SHOPIFY_ACCESS_TOKEN):
        self.access_token = access_token
        # Admin API access tokens for custom/private apps don't expire on a timer.
        # Only treat the token as needing a proactive refresh if none was supplied;
        # otherwise _ensure_valid_token would force a client-credentials refresh on
        # the very first query() call, which fails for stores that only issued a
        # static access token (no client-credentials grant enabled).
        self.token_expires_at = time.time() + 3600 * 24 * 365 if access_token else 0.0

        # Allow passing alternate shop admin API url via environment/config
        # If SHOPIFY_ADMIN_API_URL is appropriate, it will be used by default.
        self.base_url = SHOPIFY_ADMIN_API_URL
        self.oauth_url = SHOPIFY_OAUTH_URL

        self.headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.access_token,
        }

    def set_shop(self, shop_base_url: str, oauth_url: Optional[str] = None) -> None:
        """Override the base admin API URL and optional oauth url for this client.

        Args:
            shop_base_url: Full admin API base URL (eg. https://example.com/admin/api/2026-01)
            oauth_url: Optional OAuth token URL for refreshing tokens
        """
        self.base_url = shop_base_url.rstrip("/")
        if oauth_url:
            self.oauth_url = oauth_url

    def _refresh_token(self) -> str:
        """
        Refresh the access token using OAuth credentials
        
        Returns:
            str: New access token
            
        Raises:
            RuntimeError: If token refresh fails
        """
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
            
            # Update headers with new token
            self.headers["X-Shopify-Access-Token"] = self.access_token
            
            logger.info("Access token refreshed successfully")
            return self.access_token
            
        except requests.RequestException as e:
            logger.error(f"Failed to refresh access token: {str(e)}")
            raise RuntimeError(f"Token refresh failed: {str(e)}")

    def _ensure_valid_token(self) -> None:
        """Ensure the access token is still valid, refresh if needed"""
        if time.time() >= self.token_expires_at - 60:
            self._refresh_token()

    def query(self, query: str) -> Dict[str, Any]:
        """
        Execute a GraphQL query
        
        Args:
            query (str): GraphQL query string
            
        Returns:
            Dict: Response data
            
        Raises:
            RuntimeError: If query execution fails
        """
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
                logger.error(f"GraphQL error: {error_msg}")
                raise RuntimeError(f"GraphQL error: {error_msg}")
            
            return payload.get("data", {})
            
        except requests.RequestException as e:
            logger.error(f"Request failed: {str(e)}")
            raise RuntimeError(f"API request failed: {str(e)}")

    def mutation(self, mutation: str) -> Dict[str, Any]:
        """
        Execute a GraphQL mutation
        
        Args:
            mutation (str): GraphQL mutation string
            
        Returns:
            Dict: Response data
            
        Raises:
            RuntimeError: If mutation execution fails
        """
        return self.query(mutation)

    # REST helpers for easier resource operations
    def rest_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Perform a GET request against the Admin REST API.

        `path` should be the resource path without the leading slash and without the `.json` suffix,
        e.g. `custom_collections` or `products/123`.
        """
        url = f"{self.base_url}/{path}.json"
        try:
            resp = requests.get(url, headers=self.headers, params=params, timeout=API_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"REST GET failed ({url}): {e}")
            raise RuntimeError(f"REST GET failed: {e}")

    def rest_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/{path}.json"
        try:
            resp = requests.post(url, headers=self.headers, json=payload, timeout=API_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"REST POST failed ({url}): {e}")
            raise RuntimeError(f"REST POST failed: {e}")

    def rest_put(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/{path}.json"
        try:
            resp = requests.put(url, headers=self.headers, json=payload, timeout=API_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error(f"REST PUT failed ({url}): {e}")
            raise RuntimeError(f"REST PUT failed: {e}")

    def rest_delete(self, path: str) -> None:
        url = f"{self.base_url}/{path}.json"
        try:
            resp = requests.delete(url, headers=self.headers, timeout=API_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"REST DELETE failed ({url}): {e}")
            raise RuntimeError(f"REST DELETE failed: {e}")
