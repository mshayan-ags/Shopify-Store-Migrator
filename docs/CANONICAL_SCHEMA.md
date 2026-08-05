# Canonical intermediate schema

Every `transfer/transfer_*.py` script is really two independent halves that happen
to live in the same file:

1. **export** -- read a source platform's data and produce a plain-dict/list JSON
   shape (no source-platform-specific types, no GraphQL/REST response envelopes).
2. **import** -- take that same JSON shape and push it into a destination Shopify
   store.

The JSON shape produced by step 1 is the **canonical schema** documented here. It
is the contract between the two halves -- and, critically, the contract any new
*source* connector (Wix, BigCommerce, a raw Postgres/Mongo database, or anything
else) must produce so it can be fed straight into the *existing* Shopify
`import_*` functions via `--import-from <file>` (see
`docs/IMPORT_FROM.md`) without writing a single new line of Shopify-side code.

If a source platform has no concept of a field (e.g. a raw database row has no
"SEO description"), a connector should simply omit that key or set it to `None`
-- every `import_*` function already treats missing/`None` fields as "leave
unset," since that's the same shape it already gets from a Src Shopify store that
also might not have every optional field populated.

Field names below are the exact dict keys used in the exported JSON (snake_case
throughout, regardless of the source platform's own naming convention -- a
connector is responsible for the translation).

---

## product

Produced by `transfer/transfer_product.py:export_product`. One dict per product.

```
{
  "id": <source-platform product id, any type -- used only for logging>,
  "handle": str,                     # url slug, must be unique per store
  "title": str,
  "body_html": str | None,           # description, HTML allowed
  "vendor": str | None,
  "product_type": str | None,
  "tags": str | None,                # comma-separated, e.g. "red, sale, new"
  "status": "active" | "draft" | "archived",
  "template_suffix": str | None,
  "seo_title": str | None,
  "seo_description": str | None,
  "category_id": str | None,         # Shopify standardized taxonomy GID -- leave None if source has no equivalent
  "requires_selling_plan": bool,
  "options": [{"name": str, "position": int, "values": [str, ...]}, ...],
  "images": [ImageExport, ...],
  "variants": [VariantExport, ...],
  "metafields": [MetafieldExport, ...]
}
```

**ImageExport**
```
{
  "id": <source id>,
  "position": int,
  "alt": str | None,
  "src": str,                # source URL -- import downloads+reuploads
  "local_path": str | None,  # populated by export step, connectors may leave None and only set "src"
  "variant_ids": [<source variant id>, ...],  # which variants this image applies to
  "metafields": [MetafieldExport, ...]
}
```

**VariantExport**
```
{
  "id": <source id>,
  "title": str,
  "sku": str | None,                 # used everywhere downstream (orders/draft orders/discounts/price lists) to match a variant across stores -- populate this if at all possible
  "barcode": str | None,
  "price": str,                      # decimal string, e.g. "19.99"
  "compare_at_price": str | None,
  "position": int,
  "option1": str | None, "option2": str | None, "option3": str | None,
  "taxable": bool,
  "weight": float | None, "weight_unit": "kg"|"g"|"lb"|"oz" | None,
  "requires_shipping": bool,
  "inventory_policy": "deny" | "continue",
  "inventory_management": "shopify" | None,   # None = don't track inventory
  "inventory_quantity": int | None,           # total across all locations -- fallback if inventory_by_location is empty
  "inventory_by_location": [{"location_name": str, "available": int}, ...],
  "unit_cost": str | None,
  "country_code_of_origin": str | None,       # ISO 3166-1 alpha-2
  "harmonized_system_code": str | None,
  "province_code_of_origin": str | None,
  "image_position": int | None,               # matches an ImageExport's "position"
  "metafields": [MetafieldExport, ...]
}
```

**MetafieldExport** (shared by every resource that has metafields)
```
{"namespace": str, "key": str, "value": str, "type": str}
```
`type` must be a valid Shopify metafield type string (e.g. `single_line_text_field`,
`number_integer`, `json`) -- a connector pulling from a platform with untyped
custom fields should default to `single_line_text_field` or `json`.

---

## collection

Produced by `transfer/transfer_collections.py`. One dict per collection.
```
{
  "type": "custom" | "smart",
  "id": <source id>,
  "title": str,
  "handle": str,
  "body_html": str | None,
  "image": <source image URL> | None,
  "image_local": str | None,
  "products": [{"id": <source id>, "handle": <product handle>}, ...],  # only for type="custom"
  "rules": [...] | None,   # only for type="smart" -- Shopify's rule-array shape; a connector with no rule-engine equivalent should export as type="custom" with an explicit product list instead
  "published": bool,
  "metafields": [MetafieldExport, ...]
}
```

---

## customer

Produced by `transfer/transfer_customers.py:export_customers`.
```
{
  "id": <source id>,
  "email": str,                      # REQUIRED -- the only cross-store matching key used everywhere (orders, draft orders, gift cards, B2B contacts)
  "first_name": str | None, "last_name": str | None,
  "phone": str | None,
  "note": str | None,
  "tags": [str, ...],
  "tax_exempt": bool | None,
  "tax_exemptions": [str, ...],
  "locale": str | None,
  "email_marketing_consent": {"marketingState": str, "marketingOptInLevel": str | None} | None,
  "sms_marketing_consent": {"marketingState": str, ...} | None,
  "default_address": AddressExport | None,
  "addresses": [AddressExport, ...],
  "metafields": [MetafieldExport, ...]
}
```

**AddressExport**
```
{"address1": str|None, "address2": str|None, "city": str|None, "company": str|None,
 "countryCodeV2": str|None,  # ISO 3166-1 alpha-2, bare code (e.g. "US")
 "firstName": str|None, "lastName": str|None, "phone": str|None,
 "provinceCode": str|None, "zip": str|None}
```
(Note: address sub-dict keys are camelCase, matching Shopify's own
`MailingAddressInput` -- kept as-is rather than renamed to snake_case, since
`import_customers` passes this dict's keys almost directly into the GraphQL
input literal. A new connector should match this camelCase shape for addresses
specifically.)

---

## order / draft_order

`order` produced by `transfer/transfer_orders.py:export_orders`;
`draft_order` (unpaid quotes) by `transfer/transfer_draft_orders.py:export_draft_orders`.
Orders are immutable financial records; draft orders are still-editable quotes.
Line items in both are matched to a destination product variant **by SKU**, so
`sku` must be populated for a connector's order data to import as real line
items (unmatched SKUs fall back to a custom/manual line item with just a title
and price).

```
{
  "id": <source id>,
  "name": str,               # e.g. "#1001" -- used as the cross-store dedup key
  "email": str | None,
  "phone": str | None,
  "note": str | None,
  "tags": [str, ...],
  "test": bool,
  "processed_at": ISO8601 str | None,
  "closed_at": ISO8601 str | None,
  "cancelled_at": ISO8601 str | None,
  "cancel_reason": str | None,
  "po_number": str | None,
  "source_name": str | None,          # NOT replayed on import (Shopify rejects most values) -- kept for reference only
  "discount_codes": [str, ...],       # preserved as a note only, not reconstructed as a real discount
  "custom_attributes": [{"key": str, "value": str}, ...],
  "currency": str,                    # ISO 4217, e.g. "USD"
  "financial_status": str | None,
  "fulfillment_status": str | None,
  "shipping_address": AddressExport | None,
  "billing_address": AddressExport | None,
  "shipping_line": {"title": str, "amount": str, "currency": str} | None,
  "tax_lines": [{"title": str, "rate": float, "channel_liable": bool, "amount": str, "currency": str}, ...],
  "line_items": [{
      "sku": str | None, "title": str, "variant_title": str | None, "vendor": str | None,
      "quantity": int, "taxable": bool, "requires_shipping": bool,
      "price": str, "currency": str
  }, ...],
  "transactions": [{             # order-only -- omit entirely for draft_order
      "kind": "SALE"|"CAPTURE", "status": "SUCCESS", "gateway": str|None,
      "test": bool, "processed_at": ISO8601 str|None, "amount": str, "currency": str
  }, ...],
  "metafields": [MetafieldExport, ...]
}
```

---

## discount

Produced by `transfer/transfer_discounts.py:export_discounts`. Supports basic
percentage/fixed-amount, free shipping, and Buy-X-Get-Y (Bxgy), each code-based
or automatic. A source platform's promotion/coupon concept rarely maps cleanly
onto all of these -- connectors should map to whichever `typename` is the
closest fit and omit fields the source has no equivalent for.
```
{
  "typename": "DiscountCodeBasic" | "DiscountAutomaticBasic" | "DiscountCodeFreeShipping"
             | "DiscountAutomaticFreeShipping" | "DiscountCodeBxgy" | "DiscountAutomaticBxgy",
  "title": str, "status": "ACTIVE"|"EXPIRED"|"SCHEDULED", "starts_at": ISO8601|None, "ends_at": ISO8601|None,
  "code": str | None,                 # required for Code* typenames, None for Automatic*
  "combines_with": {"orderDiscounts": bool, "productDiscounts": bool, "shippingDiscounts": bool},
  "applies_once_per_customer": bool | None, "usage_limit": int | None,
  "customer_gets": {"value": {...}, "items": {...}, ...} | None,          # Basic types only
  "customer_buys": {"value": {...}, "items": {...}} | None,               # Bxgy only
  "customer_gets_bxgy": {"quantity": int, "effect": {...}, "items": {...}} | None,  # Bxgy only
  "minimum_requirement": {"kind": "quantity", "value": int} | {"kind": "subtotal", "amount": str, "currency": str} | None,
  "maximum_shipping_price": str | None,       # FreeShipping types only
  "uses_per_order_limit": int | None,         # Bxgy only
  "metafields": [MetafieldExport, ...]
}
```
See `transfer/transfer_discounts.py`'s docstring for the exact nested shapes of
`customer_gets`/`customer_buys`/`customer_gets_bxgy`'s `value`/`items` -- these
are intricate enough that a connector should read that file directly rather than
rely on this summary alone.

---

## gift_card

Produced by `transfer/transfer_gift_cards.py:export_gift_cards`. **A gift
card's redeemable code can never be read back from Shopify's API and so can
never be migrated as-is** -- the import side always mints a fresh code. A
connector only needs to supply the remaining balance, not an original code.
```
{
  "id": <source id>, "masked_code": str | None, "note": str | None,
  "initial_value": {"amount": str, "currency": str},
  "balance": {"amount": str, "currency": str},     # THIS is what gets migrated, not initial_value
  "customer_email": str | None, "expires_on": ISO8601 date | None, "enabled": bool
}
```

---

## b2b company

Produced by `transfer/transfer_b2b.py:export_companies`.
```
{
  "name": str, "note": str | None, "external_id": str | None,
  "locations": [{"name": str, "billing_address": AddressExport|None, "shipping_address": AddressExport|None}, ...],
  "contacts": [{"email": str}, ...]   # matched to an ALREADY-migrated destination customer by email -- not created fresh
}
```

---

## market

Produced by `transfer/transfer_markets.py:export_markets`.
```
{
  "name": str, "enabled": bool,
  "regions": [{"country_code": str}, ...],           # ISO 3166-1 alpha-2
  "web_presence": {"subfolder_suffix": str|None, "alternate_locales": [str,...], "default_locale": str|None} | None,
  "base_currency": str | None                        # ISO 4217
}
```

---

## file (standalone Content > Files entry)

Produced by `transfer/transfer_files.py:export_files`. Not product images or
theme assets (those are covered by the `product`/theme pipelines already).
```
{
  "filename_key": str,     # dedup key -- see transfer_files.py's filename_key_from_url/_from_path
  "url": str,              # source URL Shopify's fileCreate will fetch directly
  "alt": str | None,
  "content_type": "IMAGE" | "VIDEO" | "FILE"
}
```

---

## selling_plan_group

Produced by `transfer/transfer_selling_plans.py:export_selling_plan_groups`.
Templates only -- **live customer subscription contracts can never be
migrated** (tied to the source's payment processor).
```
{
  "name": str, "merchant_code": str, "description": str | None, "options": [str, ...],
  "product_handles": [str, ...],
  "selling_plans": [{
    "name": str, "description": str | None,
    "billing_policy": {"interval": "DAY"|"WEEK"|"MONTH"|"YEAR", "interval_count": int},
    "delivery_policy": {"interval": ..., "interval_count": int},
    "pricing_policies": [{"adjustment_type": "PERCENTAGE"|"FIXED_AMOUNT"|"PRICE", "value": str}]
  }, ...]
}
```

---

## customer_segment

Produced by `transfer/transfer_customer_segments.py:export_segments`.
```
{"name": str, "query": str}
```
`query` is Shopify's saved-search predicate language -- a source platform with a
fundamentally different query language (or none at all) should skip this
resource entirely rather than attempt a lossy translation.

---

## page / blog+article / redirect / policy

Simple enough to show inline rather than as separate sections:

```
page:     {"id", "handle", "title", "body", "is_published", "published_at", "template_suffix", "metafields"}
blog:     {"id", "handle", "title", "comment_policy", "template_suffix", "metafields", "articles": [article, ...]}
article:  {"id", "handle", "title", "author", "body", "summary", "tags", "is_published",
           "published_at", "template_suffix", "image_url", "image_alt", "metafields"}
redirect: {"path", "target"}
policy:   {"type": "REFUND_POLICY"|"PRIVACY_POLICY"|"TERMS_OF_SERVICE"|"SHIPPING_POLICY"|..., "body"}
```

---

## Where each canonical shape is consumed

| Resource | Import function | File |
|---|---|---|
| product | `import_products` (per-product create/update path in `main()`) | `transfer/transfer_product.py` |
| collection | `transfer_collections_one_by_one` | `transfer/transfer_collections.py` |
| customer | `import_customers` | `transfer/transfer_customers.py` |
| order | `import_orders` | `transfer/transfer_orders.py` |
| draft_order | `import_draft_orders` | `transfer/transfer_draft_orders.py` |
| discount | `import_discounts` | `transfer/transfer_discounts.py` |
| gift_card | `import_gift_cards` | `transfer/transfer_gift_cards.py` |
| b2b company | `import_companies` | `transfer/transfer_b2b.py` |
| market | `import_markets` | `transfer/transfer_markets.py` |
| file | `import_files` | `transfer/transfer_files.py` |
| selling_plan_group | `import_selling_plan_groups` | `transfer/transfer_selling_plans.py` |
| customer_segment | `import_segments` | `transfer/transfer_customer_segments.py` |
| page | `import_pages` | `transfer/transfer_pages.py` |
| blog | `import_blogs` | `transfer/transfer_blogs.py` |
| redirect | `import_redirects` | `transfer/transfer_redirects.py` |
| policy | `import_policies` / `sync_policies` | `transfer/transfer_policies.py` |

A new source connector's job stops at producing the canonical JSON on disk (or
in memory) -- it should never need to know anything about Shopify's GraphQL
schema, mutation names, or auth flow. That's entirely the existing
`import_*` functions' responsibility, reused unchanged.
