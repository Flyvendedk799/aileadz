#!/usr/bin/env bash
# Nightly live AI eval with a regression gate (docs/runbooks/AI_EVAL_NIGHTLY.md).
#
#   OPENAI_API_KEY=... CATALOG_SOURCE_FILE=/abs/catalog.json \
#     scripts/nightly_ai_eval.sh <work dir> <result dir>
#
# Clones main fresh into <work dir>, starts a throwaway MySQL 8 container, seeds the
# sandbox, runs the golden set for each provider that has a key, gates it against the
# committed ai_eval/baselines/<provider>.json and writes <result dir>/latest-<provider>.json
# (plus a dated copy). Admin > Systemstatus reads that directory (AI_EVAL_DIR).
# Exits 1 if any provider's gate failed, 2 on a setup error. Needs docker and python3.12.
set -uo pipefail
WORK="${1:?usage: nightly_ai_eval.sh <work dir> <result dir>}"
OUT="${2:?usage: nightly_ai_eval.sh <work dir> <result dir>}"
: "${OPENAI_API_KEY:?OPENAI_API_KEY is required (embeddings and every provider)}"
: "${CATALOG_SOURCE_FILE:?CATALOG_SOURCE_FILE must point at the catalog export}"
REPO="${EVAL_REPO:-https://github.com/Flyvendedk799/aileadz.git}"
BRANCH="${EVAL_BRANCH:-main}"
DB=fm_ai_eval_db
PORT="${EVAL_MYSQL_PORT:-3317}"
THRESHOLD="${EVAL_THRESHOLD:-10}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$WORK" "$OUT"

cleanup() { docker rm -f "$DB" >/dev/null 2>&1 || true; }
trap cleanup EXIT

rm -rf "$WORK/app"
git clone -q --depth 1 -b "$BRANCH" "$REPO" "$WORK/app" || exit 2
cd "$WORK/app" || exit 2
# Reuse the virtualenv while requirements.txt is unchanged.
REQ_HASH="$(sha256sum requirements.txt | cut -c1-16)"
VENV="$WORK/venv-$REQ_HASH"
if [ ! -x "$VENV/bin/python" ]; then
  rm -rf "$WORK"/venv-*
  python3.12 -m venv "$VENV" && "$VENV/bin/pip" install -q -r requirements.txt || exit 2
fi

cleanup
docker run -d --name "$DB" -e MYSQL_ROOT_PASSWORD=rootpw -e MYSQL_DATABASE=futurematch_sandbox \
  -e MYSQL_USER=fm -e MYSQL_PASSWORD=fm -p "127.0.0.1:$PORT:3306" \
  -v "$PWD/sandbox/init.sql:/docker-entrypoint-initdb.d/01-init.sql:ro" mysql:8.0 \
  --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci >/dev/null || exit 2
for _ in $(seq 1 90); do
  docker exec "$DB" mysql -ufm -pfm -h127.0.0.1 futurematch_sandbox -e "SELECT 1 FROM users LIMIT 1" >/dev/null 2>&1 && break
  sleep 2
done

export SANDBOX=1 MYSQL_HOST=127.0.0.1 MYSQL_PORT="$PORT" MYSQL_USER=fm MYSQL_PASSWORD=fm \
  MYSQL_DB=futurematch_sandbox ENTERPRISE_TABLE_SYNC_FORCE=1 CATALOG_SOURCE_FILE
"$VENV/bin/python" sandbox/run_sandbox.py init > "$OUT/seed-$STAMP.log" 2>&1 || exit 2

status=0
for provider in openai anthropic; do
  if [ "$provider" = anthropic ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then continue; fi
  "$VENV/bin/python" -m ai_eval.run_eval --provider "$provider" --gate --threshold "$THRESHOLD" \
    > "$OUT/run-$provider-$STAMP.log" 2>&1
  rc=$?
  if [ -f ai_eval/last_run.json ]; then
    python3 - "$provider" "$rc" "$(git rev-parse HEAD)" ai_eval/last_run.json "$OUT/$provider-$STAMP.json" <<'PY'
import json, sys
provider, rc, commit, src, dst = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
d = json.load(open(src))
d.update(provider=provider, exit_code=rc, commit=commit)
json.dump(d, open(dst, "w"), ensure_ascii=False, indent=1)
PY
    cp "$OUT/$provider-$STAMP.json" "$OUT/latest-$provider.json"
  fi
  [ "$rc" -eq 0 ] || status=1
  rm -f ai_eval/last_run.json
done
find "$OUT" -type f -mtime +30 -delete
exit $status
