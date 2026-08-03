import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from transfer.transfer_product import make_client
from utils.shopify_graphql_utils import paginate_connection, mutation_errors
from utils.concurrency_utils import retry_with_backoff, gql_quote
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_markets")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def fetch_all_markets(client) -> List[Dict[str, Any]]:
    """Return every market on the store with its regions, web presence, and
    currency settings, walked via cursor pagination."""

    def build_query(after_clause: str) -> str:
        return f"""
        query {{
          markets(first: 50{after_clause}) {{
            edges {{ node {{
              id name handle enabled type
              regions(first: 50) {{ edges {{ node {{
                __typename
                ... on MarketRegionCountry {{ code }}
              }} }} }}
              webPresence {{
                id
                subfolderSuffix
                defaultLocale {{ locale name }}
                alternateLocales {{ locale name }}
                domain {{ id host }}
              }}
              currencySettings {{
                baseCurrency {{ currencyCode }}
                localCurrencies
              }}
            }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("markets",))


def export_markets(client) -> Dict[str, Any]:
    markets = fetch_all_markets(client)
    exported = []

    for m in markets:
        regions = []
        for edge in (m.get("regions") or {}).get("edges", []):
            node = edge["node"]
            code = node.get("code")
            if code:
                regions.append({"country_code": code})

        web_presence = None
        wp = m.get("webPresence")
        if wp:
            domain = wp.get("domain")
            alternate_locales = [
                loc.get("locale") for loc in (wp.get("alternateLocales") or []) if loc.get("locale")
            ]
            web_presence = {
                "has_domain": bool(domain),
                "domain_host": (domain or {}).get("host"),
                "subfolder_suffix": wp.get("subfolderSuffix"),
                "default_locale": (wp.get("defaultLocale") or {}).get("locale"),
                "alternate_locales": alternate_locales,
            }

        currency_settings = None
        cs = m.get("currencySettings")
        if cs:
            base_currency = (cs.get("baseCurrency") or {}).get("currencyCode")
            currency_settings = {
                "base_currency": base_currency,
                "local_currencies": bool(cs.get("localCurrencies")),
            }

        exported.append(
            {
                "handle": m.get("handle"),
                "name": m.get("name"),
                "enabled": m.get("enabled", True),
                "type": m.get("type"),
                "regions": regions,
                "web_presence": web_presence,
                "currency_settings": currency_settings,
            }
        )

    logger.info("Exported %s market(s)", len(exported))
    return {"markets": exported}


def create_market(dest_client, market: Dict[str, Any]) -> Optional[str]:
    """Create the market itself (name + regions). Returns the new market's gid,
    or None if it couldn't be created (logged, not raised)."""
    regions_input = [
        f"{{ countryCode: {r['country_code']} }}" for r in market.get("regions", []) if r.get("country_code")
    ]
    if not regions_input:
        logger.warning(
            "Market '%s' has no country region with a resolvable code -- skipping "
            "(marketCreate requires at least one region)",
            market.get("name"),
        )
        return None

    mutation = f"""
    mutation {{
      marketCreate(input: {{
        name: {gql_quote(market.get("name"))}
        regions: [{", ".join(regions_input)}]
      }}) {{
        market {{ id }}
        userErrors {{ field message }}
      }}
    }}
    """
    try:
        result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    except Exception as e:
        logger.warning("Failed to create market '%s': %s", market.get("name"), e)
        return None

    errors = mutation_errors(result, "marketCreate")
    if errors:
        logger.warning(
            "Failed to create market '%s': %s (having more than one market may require a higher Shopify "
            "plan tier -- this can present as a permissions-looking error on Basic-tier destination stores)",
            market.get("name"),
            errors,
        )
        return None

    return (result.get("marketCreate") or {}).get("market", {}).get("id")


def create_web_presence(dest_client, market_id: str, market: Dict[str, Any]) -> None:
    """Best-effort marketWebPresenceCreate for the subfolder+locales approach only.
    A source web presence tied to a custom domain is skipped outright -- domains
    are store-specific and there's no destination equivalent to point it at."""
    wp = market.get("web_presence")
    if not wp:
        return

    if wp.get("has_domain"):
        logger.warning(
            "Market '%s' web presence used a custom domain (%s) on the source store -- domains are "
            "store-specific and can't be migrated, so web presence was skipped for this market. Set up a "
            "subfolder or the destination's own domain manually if this market needs one.",
            market.get("name"),
            wp.get("domain_host"),
        )
        return

    if not wp.get("subfolder_suffix") and not wp.get("alternate_locales"):
        return

    fields = []
    if wp.get("subfolder_suffix"):
        fields.append(f"subfolderSuffix: {gql_quote(wp['subfolder_suffix'])}")
    if wp.get("alternate_locales"):
        locales_clause = ", ".join(gql_quote(loc) for loc in wp["alternate_locales"])
        fields.append(f"alternateLocales: [{locales_clause}]")
    if wp.get("default_locale"):
        fields.append(f"defaultLocale: {gql_quote(wp['default_locale'])}")

    mutation = f"""
    mutation {{
      marketWebPresenceCreate(marketId: {gql_quote(market_id)}, webPresence: {{ {", ".join(fields)} }}) {{
        marketWebPresence {{ id }}
        userErrors {{ field message }}
      }}
    }}
    """
    try:
        result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    except Exception as e:
        logger.warning(
            "Failed to create web presence for market '%s': %s (market web presence customization may be "
            "gated to certain Shopify plans -- this can present as a permissions-looking error on "
            "Basic-tier destination stores)",
            market.get("name"),
            e,
        )
        return

    errors = mutation_errors(result, "marketWebPresenceCreate")
    if errors:
        logger.warning("Failed to create web presence for market '%s': %s", market.get("name"), errors)
        return

    logger.info("Created web presence for market '%s'", market.get("name"))


def update_currency_settings(dest_client, market_id: str, market: Dict[str, Any]) -> None:
    """Best-effort marketCurrencySettingsUpdate. Only attempted when the source
    market had a base currency recorded at all."""
    cs = market.get("currency_settings")
    if not cs or not cs.get("base_currency"):
        return

    fields = [
        f"baseCurrency: {cs['base_currency']}",
        f"localCurrencies: {'true' if cs.get('local_currencies') else 'false'}",
    ]

    mutation = f"""
    mutation {{
      marketCurrencySettingsUpdate(marketId: {gql_quote(market_id)}, input: {{ {", ".join(fields)} }}) {{
        userErrors {{ field message }}
      }}
    }}
    """
    try:
        result = retry_with_backoff(lambda: dest_client.mutation(mutation))
    except Exception as e:
        logger.warning(
            "Failed to update currency settings for market '%s': %s (per-market currency settings may be "
            "gated to certain Shopify plans -- this can present as a permissions-looking error on "
            "Basic-tier destination stores)",
            market.get("name"),
            e,
        )
        return

    errors = mutation_errors(result, "marketCurrencySettingsUpdate")
    if errors:
        logger.warning("Failed to update currency settings for market '%s': %s", market.get("name"), errors)
        return

    logger.info(
        "Set currency settings for market '%s' (base currency=%s, local currencies=%s)",
        market.get("name"),
        cs.get("base_currency"),
        cs.get("local_currencies"),
    )


def import_markets(dest_client, exported: Dict[str, Any]) -> None:
    existing_names = {(m.get("name") or "").strip().lower() for m in fetch_all_markets(dest_client)}

    created = 0
    skipped = 0
    failed = 0

    for market in exported.get("markets", []):
        name_key = (market.get("name") or "").strip().lower()
        if name_key in existing_names:
            logger.info("Market '%s' already exists on destination, skipping", market.get("name"))
            skipped += 1
            continue

        try:
            new_market_id = create_market(dest_client, market)
            if not new_market_id:
                failed += 1
                continue

            logger.info("Created market '%s' (id %s)", market.get("name"), new_market_id)
            created += 1

            create_web_presence(dest_client, new_market_id, market)
            update_currency_settings(dest_client, new_market_id, market)
        except Exception:
            logger.exception("Unexpected error transferring market '%s'", market.get("name"))
            failed += 1

    logger.info("Markets import complete: %s created, %s already existed, %s failed", created, skipped, failed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer Shopify Markets configuration from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create missing markets on the destination store")
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
    parser.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import this previously-saved canonical JSON file "
            "instead (see docs/CANONICAL_SCHEMA.md). Lets you import from a non-Shopify source "
            "connector or replay a prior dry-run export. No SRC_SHOPIFY_* credentials needed in "
            "this mode."
        ),
    )
    args = parser.parse_args()

    dest_shop = os.getenv("DEST_SHOPIFY_SHOP")
    dest_token = os.getenv("DEST_SHOPIFY_ACCESS_TOKEN")
    require_env(DEST_SHOPIFY_SHOP=dest_shop, DEST_SHOPIFY_ACCESS_TOKEN=dest_token)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    dest_client = make_client(dest_shop, dest_token)

    if args.import_from:
        logger.info("Loading export from %s (skipping source fetch)", args.import_from)
        if args.import_from.lower().endswith(".xlsx"):
            from utils.tabular_io import import_from_xlsx
            exported = import_from_xlsx(args.import_from)
        else:
            with open(args.import_from, "r", encoding="utf-8") as f:
                exported = json.load(f)
    else:
        src_shop = os.getenv("SRC_SHOPIFY_SHOP")
        src_token = os.getenv("SRC_SHOPIFY_ACCESS_TOKEN")
        require_env(SRC_SHOPIFY_SHOP=src_shop, SRC_SHOPIFY_ACCESS_TOKEN=src_token)

        src_client = make_client(src_shop, src_token)

        logger.info("Exporting markets from %s", src_shop)
        exported = export_markets(src_client)

        ts = int(time.time())
        out_file = out_dir / f"markets_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)
        logger.info("Export complete: %s", out_file)

        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"markets_export_{ts}.xlsx")

    if args.execute:
        logger.info("Importing markets into %s", dest_shop)
        import_markets(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to create markets on the destination store")


if __name__ == "__main__":
    main()
