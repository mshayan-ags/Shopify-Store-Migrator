"""
Configuration settings for Shopify API integration
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Source store
SRC_SHOPIFY_SHOP = os.getenv("SRC_SHOPIFY_SHOP", "")
SRC_SHOPIFY_CLIENT_ID = os.getenv("SRC_SHOPIFY_CLIENT_ID", "")
SRC_SHOPIFY_CLIENT_SECRET = os.getenv("SRC_SHOPIFY_CLIENT_SECRET", "")
SRC_SHOPIFY_ACCESS_TOKEN = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN", "")

# Destination store
DEST_SHOPIFY_SHOP = os.getenv("DEST_SHOPIFY_SHOP", "")
DEST_SHOPIFY_CLIENT_ID = os.getenv("DEST_SHOPIFY_CLIENT_ID", "")
DEST_SHOPIFY_CLIENT_SECRET = os.getenv("DEST_SHOPIFY_CLIENT_SECRET", "")
DEST_SHOPIFY_ACCESS_TOKEN = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN", "")

# API Configuration
API_TIMEOUT = 30
SHOPIFY_API_VERSION = "2026-01"

# ShopifyClient defaults to the source store; every Transfer/Audit script
# overrides both a source and destination client via ShopifyClient.set_shop().
SHOPIFY_BASE_URL = f"https://{SRC_SHOPIFY_SHOP}.myshopify.com"
SHOPIFY_ADMIN_API_URL = f"{SHOPIFY_BASE_URL}/admin/api/{SHOPIFY_API_VERSION}"
SHOPIFY_OAUTH_URL = f"{SHOPIFY_BASE_URL}/admin/oauth/access_token"

# Pagination
DEFAULT_PAGE_SIZE = 250
