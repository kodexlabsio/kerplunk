#!/usr/bin/env bash
# Start (or stop) the Postgres and Iceberg REST catalog containers the integration tests use,
# and print the environment variables that point the tests at them.
#
#   eval "$(.kodex/scripts/services.sh up)"   then   uv run pytest
#   .kodex/scripts/services.sh down
set -euo pipefail

PG_PORT="${KONSTRUCT_PG_PORT:-55432}"
BIGQUERY_PORT="${KONSTRUCT_BIGQUERY_PORT:-59050}"
ICEBERG_PORT="${KONSTRUCT_ICEBERG_PORT:-58181}"

case "${1:-up}" in
  up)
    docker rm -f konstruct-pg konstruct-iceberg konstruct-bigquery >/dev/null 2>&1 || true
    docker run -d --name konstruct-pg -p "${PG_PORT}:5432" \
      -e POSTGRES_USER=konstruct -e POSTGRES_PASSWORD=konstruct -e POSTGRES_DB=konstruct \
      postgres:18@sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42 >/dev/null
    docker run -d --name konstruct-iceberg -p "${ICEBERG_PORT}:8181" apache/iceberg-rest-fixture:latest@sha256:db8de90b5b7693d4ac334c336f91d9bbe320d7b19f4f514d26de84cdfbcbfe8d >/dev/null
    docker run -d --name konstruct-bigquery -p "${BIGQUERY_PORT}:9050" ghcr.io/goccy/bigquery-emulator:latest@sha256:f4e428d265a93dc5ce36c294e1c584c7c9b384117d47ab8ddbb63d8d50b7f393 --project=test >/dev/null
    for _ in $(seq 1 30); do
      if docker exec konstruct-pg pg_isready -U konstruct >/dev/null 2>&1 \
        && curl -fs "http://localhost:${ICEBERG_PORT}/v1/config" >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    echo "export KONSTRUCT_TEST_PG_DSN=postgresql://konstruct:konstruct@localhost:${PG_PORT}/konstruct"
    echo "export KONSTRUCT_TEST_ICEBERG_URI=http://localhost:${ICEBERG_PORT}"
    echo "export KONSTRUCT_TEST_BIGQUERY_ENDPOINT=http://localhost:${BIGQUERY_PORT}"
    ;;
  down)
    docker rm -f konstruct-pg konstruct-iceberg konstruct-bigquery >/dev/null
    ;;
  *)
    echo "usage: $0 [up|down]" >&2
    exit 2
    ;;
esac
