#!/bin/sh
# A private PostgreSQL for trying udbi, run as your own user (no sudo) on port 5433.
# Its data lives in ~/.local/share/udbi-dev/postgres.
#   dev/postgres.sh start | stop | status
set -eu

dir="${UDBI_DEV_DIR:-$HOME/.local/share/udbi-dev}/postgres"
port="${UDBI_DEV_PG_PORT:-5433}"
bin="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)"
[ -n "$bin" ] || { echo "PostgreSQL's server programs aren't installed (/usr/lib/postgresql/*/bin)." >&2; exit 1; }

case "${1:-status}" in
  start)
    if [ ! -f "$dir/PG_VERSION" ]; then
      mkdir -p "$dir"
      "$bin/initdb" -D "$dir" --username="$(id -un)" --auth=trust --encoding=UTF8 >/dev/null
      echo "Created a cluster in $dir"
    fi
    "$bin/pg_ctl" -D "$dir" -l "$dir/server.log" -w \
      -o "-p $port -k $dir -c listen_addresses=localhost" status >/dev/null 2>&1 ||
      "$bin/pg_ctl" -D "$dir" -l "$dir/server.log" -w -o "-p $port -k $dir -c listen_addresses=localhost" start >/dev/null
    psql -h localhost -p "$port" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'udbi_dev'" | grep -q 1 ||
      psql -h localhost -p "$port" -d postgres -qc "CREATE DATABASE udbi_dev"
    echo "PostgreSQL is running: postgresql://$(id -un)@localhost:$port/udbi_dev"
    ;;
  stop) "$bin/pg_ctl" -D "$dir" stop ;;
  status) "$bin/pg_ctl" -D "$dir" status ;;
  *) echo "usage: $0 start | stop | status" >&2; exit 2 ;;
esac
