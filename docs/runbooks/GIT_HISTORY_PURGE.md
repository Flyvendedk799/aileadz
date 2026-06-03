# Runbook: Purge Leaked Secrets from Git History

> **⚠️ REQUIRES EXPLICIT OWNER GO-AHEAD.** This procedure **rewrites public git
> history** and force-pushes. It breaks every existing clone, fork, and open PR.
> Do **not** run any step below without the repository owner's explicit, recorded
> approval and a chosen coordination window.

## Why this is needed

Old credentials were committed as hardcoded fallbacks in `run.py` and in the
(now-deleted) duplicate entrypoints `run_old.py`, `run_b4570bd.py`, and
`checkout_run.py`. Deleting those files removes them from the **working tree and
future commits only** — the secret values still sit in historical commits and
remain retrievable by anyone with the repo.

The leaked values include (at minimum):

- The Flask `SECRET_KEY` literal (`run.py:134` fallback, and the deleted dupes).
- The MySQL password literal (`run.py:158` fallback).
- The SSH password literal (`run.py:305`).

## CRITICAL: rotation happens regardless

**Purging history does NOT make the leaked secrets safe again.** They are
already public and must be assumed compromised. You **must** complete
`SECRET_ROTATION.md` whether or not you ever rewrite history. History rewriting
only reduces *future* exposure of the *old* values; it does nothing for the fact
that they were already exposed. Rotate first or in parallel; never treat the
purge as a substitute for rotation.

Recommended sequence: **rotate → confirm new secrets live → then (optionally)
purge history.**

---

## Option A — git-filter-repo (recommended)

`git-filter-repo` is the modern, maintained tool (BFG is the alternative below).

1. **Coordinate.** Announce a freeze: no pushes/merges from anyone during the
   rewrite. Have everyone push or stash outstanding work first.

2. **Fresh mirror clone** (filter-repo wants a clean clone):
   ```
   git clone --mirror <repo-url> aileadz-purge.git
   cd aileadz-purge.git
   ```

3. **Build a replacements file** listing every leaked literal → placeholder.
   Create `replacements.txt` (keep it out of git) with one rule per secret:
   ```
   literal:<OLD_SECRET_KEY>==>REMOVED_SECRET_KEY
   literal:<OLD_MYSQL_PASSWORD>==>REMOVED_MYSQL_PASSWORD
   literal:<OLD_SSH_PASSWORD>==>REMOVED_SSH_PASSWORD
   ```
   (Fill in the real old literals locally — they are NOT written in this
   runbook on purpose.)

4. **Rewrite:**
   ```
   git filter-repo --replace-text replacements.txt
   ```
   To remove a whole leaked file from all history instead of redacting strings:
   ```
   git filter-repo --invert-paths --path run_old.py --path run_b4570bd.py --path checkout_run.py
   ```

5. **Re-add the remote** (filter-repo drops `origin` by design) and force-push:
   ```
   git remote add origin <repo-url>
   git push --force --all origin
   git push --force --tags origin
   ```

## Option B — BFG Repo-Cleaner

1. Mirror clone as in A.2.
2. Put the leaked literals in `secrets.txt` (one per line, kept out of git).
3. Run:
   ```
   java -jar bfg.jar --replace-text secrets.txt aileadz-purge.git
   ```
4. Clean reflogs and gc:
   ```
   git reflog expire --expire=now --all && git gc --prune=now --aggressive
   ```
5. Force-push as in A.5.

---

## The force-push step (what it does)

`git push --force --all` and `--force --tags` overwrite the remote's branches and
tags with the rewritten history. **Every commit SHA changes.** Consequences:

- All existing clones now diverge and cannot fast-forward.
- Open pull requests built on the old history break.
- Forks keep the old (leaked) commits until each fork owner re-syncs.
- CI caches keyed on old SHAs are invalidated.

## After the rewrite — coordination checklist

- [ ] Owner approval recorded before starting.
- [ ] Freeze announced; window agreed.
- [ ] Rotation (`SECRET_ROTATION.md`) completed or in flight.
- [ ] History rewritten and force-pushed (`--all` + `--tags`).
- [ ] Every collaborator re-clones fresh (old local clones must be discarded, not
      pulled — pulling can reintroduce the old commits).
- [ ] Fork owners notified to delete/re-fork.
- [ ] Open PRs re-based or re-created against rewritten history.
- [ ] If the repo is on a host that caches refs/PRs (GitHub etc.), confirm the
      old commits are no longer reachable; ask the host to purge cached views if
      necessary.

## Done criteria

- Leaked literals no longer appear anywhere in `git log -p` / `git grep` across
  all branches and tags of the rewritten repo.
- Rotation complete and verified via `/readyz` (see `SECRET_ROTATION.md`).
- All collaborators on fresh clones.
