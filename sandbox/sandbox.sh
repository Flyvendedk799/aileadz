#!/usr/bin/env bash
# Convenience wrapper for the Futurematch local sandbox.
#   ./sandbox.sh up      start the MySQL container (waits until ready)
#   ./sandbox.sh init    create all tables + seed a test login/company
#   ./sandbox.sh run     run the app at http://127.0.0.1:5001
#   ./sandbox.sh smoke   non-interactive end-to-end check (login + profile + chat)
#   ./sandbox.sh mysql   open a mysql shell on the sandbox DB
#   ./sandbox.sh logs    tail the DB logs
#   ./sandbox.sh down    stop + remove the container (keeps data volume)
#   ./sandbox.sh reset   destroy the data volume + recreate the DB
#
# Uses plain `docker run` (no docker-compose required).
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
NAME=fm_sandbox_db
VOL=fm_sandbox_data
IMAGE=mysql:8.0
PORT=3307

start_db() {
  if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    docker start "$NAME" >/dev/null
  else
    docker run -d --name "$NAME" \
      -e MYSQL_ROOT_PASSWORD=rootpw \
      -e MYSQL_DATABASE=futurematch_sandbox \
      -e MYSQL_USER=fm -e MYSQL_PASSWORD=fm \
      -p "${PORT}:3306" \
      -v "${VOL}:/var/lib/mysql" \
      -v "$PWD/init.sql:/docker-entrypoint-initdb.d/01-init.sql:ro" \
      "$IMAGE" \
      --default-authentication-plugin=mysql_native_password \
      --character-set-server=utf8mb4 \
      --collation-server=utf8mb4_unicode_ci >/dev/null
  fi
}

wait_db() {
  echo "Waiting for MySQL to be ready…"
  for _ in $(seq 1 60); do
    if docker exec "$NAME" mysqladmin ping -h127.0.0.1 -uroot -prootpw --silent >/dev/null 2>&1; then
      echo "✓ MySQL ready on 127.0.0.1:${PORT}"; return 0
    fi
    sleep 1
  done
  echo "✗ MySQL did not become ready in time"; return 1
}

case "${1:-help}" in
  up)     start_db; wait_db ;;
  down)   docker rm -f "$NAME" >/dev/null 2>&1 || true; echo "stopped (data volume kept)" ;;
  reset)  docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm "$VOL" >/dev/null 2>&1 || true; start_db; wait_db ;;
  init)   $PY run_sandbox.py init ;;
  run)    $PY run_sandbox.py run ;;
  smoke)  $PY run_sandbox.py smoke ;;
  mysql)  docker exec -it "$NAME" mysql -ufm -pfm futurematch_sandbox ;;
  logs)   docker logs -f "$NAME" ;;
  *) echo "usage: ./sandbox.sh {up|init|run|smoke|mysql|logs|down|reset}" ;;
esac
