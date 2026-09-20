#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

cleanup() {
  docker compose down --volumes
}
trap cleanup EXIT

for provider in typesafe openrouter; do
  DBT_JEV_PROVIDER="$provider" docker compose up --build --wait
  DBT_JEV_TEST_CLICKHOUSE=1 DBT_JEV_TEST_PROVIDER="$provider" \
    uv run pytest -m clickhouse tests/test_dbt_clickhouse.py
  docker compose down --volumes
done
