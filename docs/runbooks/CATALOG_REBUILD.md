# Runbook: Catalog, search index and embeddings

The chat assistant's course search is hybrid RAG (BM25 + vector embeddings + reranker)
over ONE catalog assembled by `catalog_service.py`; `app1/rag.load_augmented_products()`
is a view over it. The index (BM25 + vectors) is rebuilt in memory whenever the catalog
signature changes (mtime of the five files below).

## Catalog inputs (all git-ignored; none ships with a fresh clone)

| Input | Path | Written by |
|---|---|---|
| Raw Shopify export | `catalog_service.source_file_path()`: `$CATALOG_SOURCE_FILE` (absolute, or relative to the app root), default `app1/shopify_products_all_pages.json` | `shopify_sync` job / `POST /admin/catalog/sync` |
| Augmented products (AI summary, structured metadata, embedding) | `app1/shopify_products_augmented.json` | offline `app1/build_index.py` only |
| CSV imports | `instance/catalog_import_products.json` | admin import flow |
| Admin edits / hide / archive overlay | `instance/catalog_overlay.json` | admin product browser |
| Category overrides | `instance/catalog_category_overrides.json` | admin category flow |
| Embeddings for products without one in the augmented file | `instance/catalog_embeddings.json` (sidecar, by handle) | `rag.embed_missing()` |

Vendor profiles (`app1/vendor_profiles.json`) are separate and ARE tracked in git.
`/readyz` reports `"catalog": true` when the source file or either `app1/shopify_products_*.json`
file exists on disk (`health.py`).

## Day-to-day (no manual rebuild needed)

- `shopify_sync` (daily, worker): pulls active products from the Shopify Admin API into
  the source file, replacing it atomically and refusing a response with under 50 % of the
  current product count. Needs `SHOPIFY_STORE` + `SHOPIFY_ADMIN_TOKEN` (`SHOPIFY_API_VERSION`
  optional); otherwise it skips cleanly.
- `catalog_embed` (every 6 h, worker): `rag.embed_missing()` embeds up to 300 products
  that have no vector into the sidecar (batches of 100, `AI_EMBEDDING_MODEL`,
  `AI_EMBEDDING_DIMENSIONS`). A catalog change also triggers it in the background unless
  `CATALOG_AUTO_EMBED=0`. Skipped without `OPENAI_API_KEY` (search stays keyword-only).
- Admin -> Katalogadmin: "Synkronisér fra Shopify" = `POST /admin/catalog/sync`; "Genopbyg indeks" =
  `POST /admin/catalog/reindex` (rebuild the in-memory index, then `embed_missing`);
  `GET /admin/catalog/index-status` returns products / with_embeddings / missing_embeddings /
  bm25_terms / last_error. All platform-admin only (`catalog_admin_routes.py`).

## Full offline augmentation: `app1/build_index.py`

Only needed to regenerate AI summaries/metadata for the whole catalog, or after changing
the embedding model or dimensions (existing vectors are then the wrong size or space).
It is a slow, paid job: per product two `gpt-4o-mini` calls (structured metadata + Danish
summary), run with `BUILD_INDEX_WORKERS` (default 16) concurrent threads, then embeddings in
batches of 100 with a checkpoint write after every batch.

1. Put the raw export in place (run the sync, or set `CATALOG_SOURCE_FILE`). The script
   exits if `OPENAI_API_KEY` is unset (it also loads `.env`) or the input is missing.
2. Back up any existing `app1/shopify_products_augmented.json` (the script overwrites it).
3. From the repo root: `python app1/build_index.py`, or `python app1/build_index.py --skip-existing`
   to keep products that already have an embedding and only process new ones.
4. Copy the result to the host if you built it elsewhere, then use "Genopbyg indeks".
5. Verify: `/readyz` shows `catalog: true`; `index-status` shows `missing_embeddings` near 0;
   ask the chat a course query and check summarized, ranked hits.

## Notes

- Changing `AI_EMBEDDING_MODEL` / `AI_EMBEDDING_DIMENSIONS`: the sidecar and augmented vectors
  must be regenerated; delete `instance/catalog_embeddings.json`, rerun `build_index.py`
  (without `--skip-existing`), then reindex.
- Because `*.json` is git-ignored, these files exist only on the host. Back them up; whether
  to un-ignore and commit the (large) augmented file is the owner's call.
