# Runbook: Backup, restore and rollback

Production is one server. The database and a handful of runtime files exist only there.
This runbook says what to back up, how to restore it, and how to roll a deploy back.

The procedure below was rehearsed once against the sandbox demo tenant on MySQL 8.0:
dump, restore into an empty database, identical row counts in all tables, and the app
booted and logged in on the restored copy. It has **not** been rehearsed on the
production server; do that once (section 4) before relying on it.

## 1. What has to be backed up

| What | Where | Why it is not reproducible |
|---|---|---|
| MySQL database | `MYSQL_DB` on `MYSQL_HOST` | all customer data |
| `instance/` | app root | catalogue admin overlay (hidden/archived products), CSV imports, category overrides, import drafts, `catalog_embeddings.json` |
| `static/uploads/` | app root | uploaded images (brands, notification images) |
| `static/brand_assets/` | app root | each company's white-label logo and assets |
| catalogue source | `CATALOG_SOURCE_FILE`, or `app1/shopify_products_all_pages.json` | recreated by the Shopify sync only if the Shopify variables are set |
| `app1/shopify_products_augmented.json` | app root | search index sidecar; can be rebuilt (CATALOG_REBUILD.md) but takes time and an OpenAI key |
| environment variables | ServerHoster, both services | `SECRET_KEY` and `AI_SECRET_KEY` decrypt stored AI keys, SSO secrets and TOTP secrets: **a database restored without the same keys cannot read them** |

Keep a copy of the environment variables in the password manager, not in the backup
directory and never in git.

## 2. Take a backup

```bash
MYSQL_HOST=... MYSQL_PORT=3306 MYSQL_USER=... MYSQL_PASSWORD=... MYSQL_DB=... \
  scripts/backup.sh /var/backups/futurematch
```

It writes `<target>/<UTC timestamp>/` with `db.sql.gz` (one consistent snapshot, taken
without locking the app out), `files.tar.gz` (the paths above that exist) and `MANIFEST`
(commit, sizes, which paths were missing), and prints the directory. It exits non-zero
if the dump fails.

- Run it daily from the server's scheduler and **before every deploy**.
- Copy each backup off the server. A backup on the same disk covers mistakes, not a
  lost disk.
- Keep at least 14 daily and 8 weekly copies. Delete older ones; the retention the
  company promises customers (`retention_service.POLICIES`) applies to backups too, so
  do not keep them for years.
- A backup holds personal data. Store it encrypted and restrict who can read it.

## 3. Restore

Restore into an **empty** database. Never restore over a live one: take a backup of the
current state first, even if it is broken.

```bash
# 1. stop the web and worker services in ServerHoster
# 2. empty target database
mysql -h "$MYSQL_HOST" -u root -p -e "CREATE DATABASE futurematch_restored CHARACTER SET utf8mb4"
# 3. load the dump
gunzip -c /var/backups/futurematch/<stamp>/db.sql.gz | mysql -h "$MYSQL_HOST" -u "$MYSQL_USER" -p futurematch_restored
# 4. runtime files, from the app root (overwrites the listed paths)
tar -xzf /var/backups/futurematch/<stamp>/files.tar.gz -C /path/to/app
# 5. point MYSQL_DB at futurematch_restored on BOTH services, deploy the commit in MANIFEST, start web then worker
```

Check before letting anyone in:

1. `GET /readyz` is 200.
2. Admin, Systemstatus (`/admin/system-health`): no missing tables or columns, "Worker kører".
3. Log in as a known user; open an order, the catalogue and Virksomhedsindstillinger.
4. Admin, AI-indstillinger shows the stored provider key as present (proves `SECRET_KEY` /
   `AI_SECRET_KEY` match the restored data).
5. "Mail og leveringer": mails that were `pending` at backup time will be sent now. If the
   backup is old, set those rows aside before starting the worker, or people get stale mail.

What a restore loses: everything written after the backup was taken. Tell the customer
which period that is.

## 4. Rehearse it (do this once, then every quarter)

On the server, restore the latest backup into a scratch database
(`futurematch_restore_check`) and compare row counts with production table by table; then
drop the scratch database. A backup that has never been restored is a hope, not a backup.
Write the date and the result in the handover notes.

## 5. Roll a deploy back

The schema only ever grows (tables and columns are added, never dropped or renamed, see
"Schema lifecycle" in DEPLOY.md), so the previous commit runs against the newer schema.

1. In ServerHoster, redeploy the previous commit on web **and** worker.
2. `GET /readyz`, then Systemstatus.
3. Do **not** restore the database for a code problem. Restore only when data is wrong,
   and then follow section 3.

One-time data migrations (`schema_registry.run_data_migrations`, flagged in `schema_meta`)
are not reversed by a rollback. The release that adds one says so in its pull request;
take the pre-deploy backup seriously for those.

## 6. Not in place yet

- No staging environment: a release is first run for real in production. The sandbox
  (`sandbox/README.md`) with the demo tenant and `tests/e2e/` is the closest thing.
- No automatic off-site copy and no monitoring that a backup ran. Both are owner tasks
  (ROADMAP T0-7).
