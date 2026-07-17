#!/usr/bin/env bash
# Container entrypoint: wait for Postgres, then run the requested command.
set -euo pipefail

scripts/wait-for-postgres.sh true

exec "$@"
