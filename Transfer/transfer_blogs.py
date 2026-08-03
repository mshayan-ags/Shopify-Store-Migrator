import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from transfer.transfer_product import make_client
from transfer.transfer_store_metafields import retry_with_backoff, set_metafields, gql_quote
from utils.shopify_graphql_utils import paginate_connection, export_metafields, mutation_errors
from utils.config import require_env

load_dotenv()

logger = logging.getLogger("transfer_blogs")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


BLOG_NODE_FIELDS = """
    id
    handle
    title
    commentPolicy
    templateSuffix
    metafields(first: 100) {
      edges { node { namespace key value type } }
    }
"""

ARTICLE_NODE_FIELDS = """
    id
    handle
    title
    author { name }
    body
    summary
    tags
    isPublished
    publishedAt
    templateSuffix
    image { url altText }
    metafields(first: 100) {
      edges { node { namespace key value type } }
    }
"""


def fetch_all_blogs(client) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          blogs(first: 100{after_clause}) {{
            edges {{ node {{ {BLOG_NODE_FIELDS} }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("blogs",))


def fetch_blog_articles(client, blog_gid: str) -> List[Dict[str, Any]]:
    def build_query(after_clause: str) -> str:
        return f"""
        {{
          blog(id: {gql_quote(blog_gid)}) {{
            articles(first: 100{after_clause}) {{
              edges {{ node {{ {ARTICLE_NODE_FIELDS} }} }}
              pageInfo {{ hasNextPage endCursor }}
            }}
          }}
        }}
        """

    return paginate_connection(client, build_query, ("blog", "articles"))


def export_blogs(src_client) -> List[Dict[str, Any]]:
    blogs = fetch_all_blogs(src_client)
    exported = []

    for blog in blogs:
        articles = fetch_blog_articles(src_client, blog["id"])
        exported_articles = [
            {
                "id": a["id"],
                "handle": a["handle"],
                "title": a["title"],
                "author": (a.get("author") or {}).get("name") or "Staff",
                "body": a["body"],
                "summary": a.get("summary"),
                "tags": a.get("tags") or [],
                "is_published": a["isPublished"],
                "published_at": a.get("publishedAt"),
                "template_suffix": a.get("templateSuffix"),
                "image_url": (a.get("image") or {}).get("url"),
                "image_alt": (a.get("image") or {}).get("altText"),
                "metafields": export_metafields(a.get("metafields")),
            }
            for a in articles
        ]

        exported.append(
            {
                "id": blog["id"],
                "handle": blog["handle"],
                "title": blog["title"],
                "comment_policy": blog.get("commentPolicy"),
                "template_suffix": blog.get("templateSuffix"),
                "metafields": export_metafields(blog.get("metafields")),
                "articles": exported_articles,
            }
        )
        logger.info("Exported blog '%s' with %s article(s)", blog["title"], len(exported_articles))

    return exported


def import_blogs(dest_client, exported: List[Dict[str, Any]]) -> None:
    dest_blogs = fetch_all_blogs(dest_client)
    blogs_by_handle = {(b.get("handle") or "").strip().lower(): b for b in dest_blogs}

    blogs_created = blogs_updated = 0
    articles_created = articles_updated = 0

    for blog in exported:
        handle_key = (blog.get("handle") or "").strip().lower()
        existing_blog = blogs_by_handle.get(handle_key)

        blog_extra_fields = ""
        if blog.get("template_suffix"):
            blog_extra_fields += f"\n                    templateSuffix: {gql_quote(blog['template_suffix'])}"
        if blog.get("comment_policy"):
            blog_extra_fields += f"\n                    commentPolicy: {blog['comment_policy']}"

        if existing_blog:
            needs_blog_update = (
                existing_blog.get("title") != blog.get("title")
                or existing_blog.get("templateSuffix") != blog.get("template_suffix")
                or existing_blog.get("commentPolicy") != blog.get("comment_policy")
            )
            if needs_blog_update:
                mutation = f"""
                mutation {{
                  blogUpdate(id: {gql_quote(existing_blog["id"])}, blog: {{
                    title: {gql_quote(blog.get("title"))}{blog_extra_fields}
                  }}) {{
                    blog {{ id handle }}
                    userErrors {{ field message }}
                  }}
                }}
                """
                try:
                    result = retry_with_backoff(lambda: dest_client.mutation(mutation))
                    errors = mutation_errors(result, "blogUpdate")
                except Exception as e:
                    errors = str(e)
                if errors:
                    logger.warning("Failed to update blog '%s': %s", blog.get("title"), errors)
                else:
                    blogs_updated += 1
            blog_gid = existing_blog["id"]
        else:
            mutation = f"""
            mutation {{
              blogCreate(blog: {{
                title: {gql_quote(blog.get("title"))}
                handle: {gql_quote(blog.get("handle"))}{blog_extra_fields}
              }}) {{
                blog {{ id handle }}
                userErrors {{ field message }}
              }}
            }}
            """
            try:
                result = retry_with_backoff(lambda: dest_client.mutation(mutation))
            except Exception as e:
                logger.warning("Failed to create blog '%s': %s", blog.get("title"), e)
                continue

            errors = mutation_errors(result, "blogCreate")
            if errors:
                logger.warning("Failed to create blog '%s': %s", blog.get("title"), errors)
                continue
            blog_gid = result["blogCreate"]["blog"]["id"]
            logger.info("Created blog '%s'", blog.get("title"))
            blogs_created += 1

        if blog.get("metafields"):
            set_metafields(dest_client, blog_gid, blog["metafields"])

        dest_articles = fetch_blog_articles(dest_client, blog_gid)
        articles_by_handle = {(a.get("handle") or "").strip().lower(): a for a in dest_articles}

        for article in blog.get("articles", []):
            article_handle_key = (article.get("handle") or "").strip().lower()
            existing_article = articles_by_handle.get(article_handle_key)

            image_block = ""
            if article.get("image_url"):
                image_block = f"""
                image: {{
                  url: {gql_quote(article["image_url"])}
                  altText: {gql_quote(article.get("image_alt"))}
                }}
                """

            tags_literal = "[" + ", ".join(gql_quote(t) for t in article.get("tags", [])) + "]"

            article_extra_fields = ""
            if article.get("template_suffix"):
                article_extra_fields += f"\n                        templateSuffix: {gql_quote(article['template_suffix'])}"
            if article.get("is_published") and article.get("published_at"):
                article_extra_fields += f"\n                        publishDate: {gql_quote(article['published_at'])}"

            if existing_article:
                needs_update = (
                    existing_article.get("title") != article.get("title")
                    or existing_article.get("body") != article.get("body")
                    or existing_article.get("templateSuffix") != article.get("template_suffix")
                )
                if needs_update:
                    mutation = f"""
                    mutation {{
                      articleUpdate(id: {gql_quote(existing_article["id"])}, article: {{
                        title: {gql_quote(article.get("title"))}
                        body: {gql_quote(article.get("body"))}
                        summary: {gql_quote(article.get("summary"))}
                        tags: {tags_literal}
                        isPublished: {"true" if article.get("is_published") else "false"}{article_extra_fields}
                      }}) {{
                        article {{ id handle }}
                        userErrors {{ field message }}
                      }}
                    }}
                    """
                    try:
                        result = retry_with_backoff(lambda: dest_client.mutation(mutation))
                    except Exception as e:
                        logger.warning("Failed to update article '%s': %s", article.get("title"), e)
                        continue

                    errors = mutation_errors(result, "articleUpdate")
                    if errors:
                        logger.warning("Failed to update article '%s': %s", article.get("title"), errors)
                        continue
                    articles_updated += 1
                article_gid = existing_article["id"]
            else:
                mutation = f"""
                mutation {{
                  articleCreate(article: {{
                    blogId: {gql_quote(blog_gid)}
                    title: {gql_quote(article.get("title"))}
                    handle: {gql_quote(article.get("handle"))}
                    author: {{ name: {gql_quote(article.get("author") or "Staff")} }}
                    body: {gql_quote(article.get("body"))}
                    summary: {gql_quote(article.get("summary"))}
                    tags: {tags_literal}
                    isPublished: {"true" if article.get("is_published") else "false"}{article_extra_fields}
                    {image_block}
                  }}) {{
                    article {{ id handle }}
                    userErrors {{ field message }}
                  }}
                }}
                """
                try:
                    result = retry_with_backoff(lambda: dest_client.mutation(mutation))
                except Exception as e:
                    logger.warning("Failed to create article '%s': %s", article.get("title"), e)
                    continue

                errors = mutation_errors(result, "articleCreate")
                if errors:
                    logger.warning("Failed to create article '%s': %s", article.get("title"), errors)
                    continue
                article_gid = result["articleCreate"]["article"]["id"]
                logger.info("Created article '%s' in blog '%s'", article.get("title"), blog.get("title"))
                articles_created += 1

            if article.get("metafields"):
                set_metafields(dest_client, article_gid, article["metafields"])

    logger.info(
        "Blogs import complete: %s blog(s) created, %s updated; %s article(s) created, %s updated",
        blogs_created,
        blogs_updated,
        articles_created,
        articles_updated,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Transfer blogs and articles from Src to dest")
    parser.add_argument("--execute", action="store_true", help="Create/update blogs and articles on the destination store")
    parser.add_argument(
        "--import-from",
        help=(
            "Skip the source export step and import this previously-saved canonical JSON file "
            "instead (see docs/CANONICAL_SCHEMA.md). Lets you import from a non-Shopify source "
            "connector or replay a prior dry-run export. No SRC_SHOPIFY_* credentials needed "
            "in this mode."
        ),
    )
    parser.add_argument("--out", default="Results", help="Output directory for the export JSON")
    parser.add_argument("--xlsx", action="store_true", help="Also write an .xlsx workbook alongside the .json export")
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

        logger.info("Exporting blogs from %s", src_shop)
        exported = export_blogs(src_client)

        ts = int(time.time())
        out_file = out_dir / f"blogs_export_{ts}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(exported, f, indent=2, ensure_ascii=False)
        if args.xlsx:
            from utils.tabular_io import export_to_xlsx
            export_to_xlsx(exported, out_dir / f"blogs_export_{ts}.xlsx")
        logger.info("Export complete: %s", out_file)

    if args.execute:
        logger.info("Importing blogs into %s", dest_shop)
        import_blogs(dest_client, exported)
    else:
        logger.info("Dry-run finished. Re-run with --execute to write blogs/articles to the destination store")


if __name__ == "__main__":
    main()
