import logging

import requests
from flask import Blueprint, jsonify, render_template, request

logger = logging.getLogger("app.connections")

bp = Blueprint("connections", __name__)

KINDS = ["shopify", "wix", "bigcommerce", "postgresql", "mongodb"]

KIND_FIELDS = {
    "shopify": ["shop_domain", "access_token"],
    "wix": ["api_key", "site_id"],
    "bigcommerce": ["store_hash", "access_token", "client_id"],
    "postgresql": ["database_url", "host", "port", "database", "user", "password", "mapping_path"],
    "mongodb": ["uri", "database", "mapping_path"],
}


@bp.route("/")
def index():
    return render_template("connections/index.html", kinds=KINDS, kind_fields=KIND_FIELDS)


@bp.route("/test", methods=["POST"])
def test():
    connection = request.get_json(silent=True) or {}
    kind = connection.get("kind", "unknown")
    ok, message = _test_connection(connection)
    if ok:
        logger.info("Connection test succeeded for kind=%s", kind)
    else:
        logger.warning("Connection test failed for kind=%s: %s", kind, message)
    return jsonify({"ok": ok, "message": message})


def _test_connection(connection):
    kind = connection.get("kind")
    config = connection.get("config") or {}

    try:
        if kind == "shopify":
            shop = connection.get("shop_domain") or ""
            token = connection.get("access_token") or ""
            if not shop or not token:
                return False, "Missing shop domain or access token"
            host = shop if "." in shop else f"{shop}.myshopify.com"
            resp = requests.post(
                f"https://{host}/admin/api/2026-01/graphql.json",
                headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
                json={"query": "{ shop { name } }"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                return False, f"Shopify API error: {data['errors']}"
            return True, f"Connected to '{data['data']['shop']['name']}'"

        if kind == "wix":
            if not config.get("api_key") or not config.get("site_id"):
                return False, "Missing API key or site ID"
            return True, "Wix credentials look present (run a Sources job to fully verify)"

        if kind == "bigcommerce":
            if not config.get("store_hash") or not config.get("access_token"):
                return False, "Missing store hash or access token"
            resp = requests.get(
                f"https://api.bigcommerce.com/stores/{config['store_hash']}/v2/store",
                headers={"X-Auth-Token": config["access_token"], "Accept": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            return True, f"Connected to '{resp.json().get('name', 'store')}'"

        if kind == "postgresql":
            if not config.get("database_url") and not config.get("host"):
                return False, "Missing connection details"
            return True, "PostgreSQL config looks present (run a Sources job to fully verify)"

        if kind == "mongodb":
            if not config.get("uri"):
                return False, "Missing connection URI"
            return True, "MongoDB config looks present (run a Sources job to fully verify)"

        return False, f"Unknown connector kind '{kind}'"
    except Exception as e:
        return False, f"Connection test failed: {e}"
