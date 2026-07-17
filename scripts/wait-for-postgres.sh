#!/usr/bin/env bash
# Block until Postgres is accepting connections, then exec the given command.
set -euo pipefail

host="${POSTGRES_HOST:-db}"
port="${POSTGRES_PORT:-5432}"
user="${POSTGRES_USER:-jobborg}"

until pg_isready -h "$host" -p "$port" -U "$user" >/dev/null 2>&1; do
  echo "Waiting for Postgres at ${host}:${port}..."
  sleep 1
done

echo "Postgres is up."
exec "$@"
