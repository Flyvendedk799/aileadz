# Runbook: RAG Catalog Rebuild

The chat assistant's product search uses a hybrid RAG index (BM25 + vector
embeddings + AI summaries) over the Shopify course catalog. The index lives in a
single augmented artifact, `app1/shopify_products_augmented.json`. **That file is
currently absent**, so RAG is **degraded** — search falls back to the raw,
un-augmented catalog without AI summaries or embeddings. This runbook regenerates
it.

## What it is

- **Builder:** `app1/build_index.py`.
- **Input:** `app1/shopify_products_all_pages.json` (raw Shopify export) —
  `app1/build_index.py:17`.
- **Output:** `app1/shopify_products_augmented.json` —
  `app1/build_index.py:18`.
- **Consumer:** `app1/rag.py` loads the augmented file at `app1/rag.py:17`. When
  it's missing, RAG runs degraded.
- **Readiness signal:** `/readyz` reports `"catalog": true` only if at least one
  RAG index file exists on disk (`health.py:82-87`, surfaced at
  `health.py:102,107`).

## ⚠️ This is a slow, paid, offline job

`build_index.py` calls OpenAI **per product**:

- One **`gpt-4o-mini` chat completion per product** to generate a Danish AI
  summary (`generate_summary()`, `app1/build_index.py:134-165`, model at
  `:155`). There's a deliberate `time.sleep(0.3)` between products
  (`:334`).
- **Embeddings** for every product, batched 100 at a time
  (`BATCH_SIZE = 100`, `:235-256`), with `time.sleep(0.5)` between batches
  (`:331`).

So the full run is a **one-time, billable, minutes-long offline job** — not
something to run on every deploy. It resumes from any existing augmented file
(skips products that already have embeddings — `:287`), so re-runs are cheaper
than the first run.

## Run it

1. Ensure `OPENAI_API_KEY` is set in the shell (the script exits if not —
   `app1/build_index.py:13-15`; it also reads `.env` via `load_dotenv`).
2. **Back up** any existing artifact first (so you can roll back a bad rebuild):
   ```
   cp app1/shopify_products_augmented.json app1/shopify_products_augmented.json.bak  # if it exists
   ```
3. Run from the repo root:
   ```
   python3 app1/build_index.py
   ```
4. Watch the progress output (per-product summary + batched embedding logs).
5. When it finishes, confirm `app1/shopify_products_augmented.json` exists and is
   non-trivial in size, and keep the `.bak` until you've verified the rebuild.

## Verify

1. `app1/shopify_products_augmented.json` exists and contains `ai_summary` +
   embedding fields per product.
2. Reload the web app, hit `GET /readyz` → `"catalog": true` (`health.py:102`).
3. Ask the chat assistant a product query and confirm relevant, summarized hits
   (full hybrid RAG restored).

## Storing the artifact (owner decision)

The augmented JSON is **large**, and the repo's `.gitignore` currently ignores
`*.json` broadly, so the artifact is **not** tracked. Whether to **un-gitignore**
and commit it (so deploys ship with a prebuilt index instead of re-running the
paid job) is the **repository owner's decision** — it trades repo size for not
having to rebuild on a fresh deploy. Options:

- **Keep it gitignored (default):** regenerate via this runbook on each fresh
  environment. Cheapest repo, but every clean deploy needs the paid job (or a
  manual copy of the artifact).
- **Track it:** add an explicit `!app1/shopify_products_augmented.json` negation
  in `.gitignore` and commit it. Deploys then ship with the index. Repo grows.

Until the owner decides, treat the file as a build artifact: regenerate or
hand-copy it onto each host, and keep a backup.

## Done criteria

- `app1/shopify_products_augmented.json` regenerated and backed up.
- `/readyz` shows `"catalog": true`.
- Chat product search returns summarized, ranked results.
