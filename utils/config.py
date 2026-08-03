import os
from dotenv import load_dotenv

from utils.errors import ConfigurationError

load_dotenv()


def require_env(**named_values):
    missing = [name for name, value in named_values.items() if not value]
    if missing:
        raise ConfigurationError(f"Missing .env values: {', '.join(named_values.keys())}")

SRC_SHOPIFY_SHOP = os.getenv("SRC_SHOPIFY_SHOP", "")
SRC_SHOPIFY_CLIENT_ID = os.getenv("SRC_SHOPIFY_CLIENT_ID", "")
SRC_SHOPIFY_CLIENT_SECRET = os.getenv("SRC_SHOPIFY_CLIENT_SECRET", "")
SRC_SHOPIFY_ACCESS_TOKEN = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN", "")

DEST_SHOPIFY_SHOP = os.getenv("DEST_SHOPIFY_SHOP", "")
DEST_SHOPIFY_CLIENT_ID = os.getenv("DEST_SHOPIFY_CLIENT_ID", "")
DEST_SHOPIFY_CLIENT_SECRET = os.getenv("DEST_SHOPIFY_CLIENT_SECRET", "")
DEST_SHOPIFY_ACCESS_TOKEN = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN", "")

API_TIMEOUT = 30
SHOPIFY_API_VERSION = "2026-01"

SHOPIFY_BASE_URL = f"https://{SRC_SHOPIFY_SHOP}.myshopify.com"
SHOPIFY_ADMIN_API_URL = f"{SHOPIFY_BASE_URL}/admin/api/{SHOPIFY_API_VERSION}"
SHOPIFY_OAUTH_URL = f"{SHOPIFY_BASE_URL}/admin/oauth/access_token"

DEFAULT_PAGE_SIZE = 250
