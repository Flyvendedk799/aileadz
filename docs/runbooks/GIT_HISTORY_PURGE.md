# Runbook: Purge leaked secrets from git history

> **Requires explicit owner go-ahead.** This rewrites public history and force-pushes:
> every commit SHA changes, existing clones/forks/open PRs break.

## Status

**Not performed** (the purge has never been run; `git log --all --oneline -- run_old.py`
still returns commits). The leaked material is only reachable through history; the working
tree is clean:

- `run.py` fallbacks in old commits: a placeholder `SECRET_KEY`, a MySQL password and an
  SSH password. The old PythonAnywhere database is gone, so these are largely moot, but they
  are still in history.
- Deleted files `run_old.py`, `run_b4570bd.py`, `checkout_run.py` (duplicate entrypoints
  with the same literals), `app1/count.py` and `app1/Pypy` (a Shopify Admin token
  `shpat_...`), and `run_history.txt` (a run log with credentials; now git-ignored).

Consequences while unpurged: the weekly full-history gitleaks job (`.github/workflows/security.yml`)
reports any of them that match its rules; `.gitleaks.toml` deliberately has no allowlist, and
`.gitleaksignore` only lists the unrelated `order_service.py` false positive.

## Rotation comes first and is independent

Purging does not make a leaked secret safe: assume it is compromised. Complete
`SECRET_ROTATION.md` (in particular the Shopify token) regardless. Sequence: rotate,
confirm the new secrets work, then optionally purge.

## Procedure (git-filter-repo)

1. Announce a freeze; everyone pushes or stashes first.
2. Work in a fresh mirror clone:
   ```
   git clone --mirror https://github.com/Flyvendedk799/aileadz aileadz-purge.git
   cd aileadz-purge.git
   ```
3. Remove the leaked files from all history:
   ```
   git filter-repo --invert-paths --path run_old.py --path run_b4570bd.py \
     --path checkout_run.py --path app1/count.py --path app1/Pypy --path run_history.txt
   ```
4. Redact the literals still present in other files (`run.py` history): create
   `replacements.txt` (never commit it), one rule per old literal, filled in locally:
   ```
   literal:<OLD_SECRET_KEY>==>REMOVED_SECRET_KEY
   literal:<OLD_MYSQL_PASSWORD>==>REMOVED_MYSQL_PASSWORD
   literal:<OLD_SSH_PASSWORD>==>REMOVED_SSH_PASSWORD
   ```
   ```
   git filter-repo --replace-text replacements.txt
   ```
5. filter-repo drops `origin`; re-add it and force-push everything:
   ```
   git remote add origin https://github.com/Flyvendedk799/aileadz
   git push --force --all origin
   git push --force --tags origin
   ```
   (BFG Repo-Cleaner with `--replace-text secrets.txt`, then
   `git reflog expire --expire=now --all && git gc --prune=now --aggressive`, is the alternative.)

## Afterwards

- Everyone re-clones (do not pull into old clones; that can reintroduce the old commits),
  including the ServerHoster checkouts of the web and worker services.
- Fork owners delete/re-fork; open PRs are re-created against the new history.
- GitHub may keep cached commit/PR views: ask support to purge them if needed.

## Done criteria

- `gitleaks detect --config .gitleaks.toml` (full history) is clean, apart from the
  `.gitleaksignore` entries; the weekly Security workflow passes.
- Rotation is complete (`SECRET_ROTATION.md`) and `/readyz` is healthy.
