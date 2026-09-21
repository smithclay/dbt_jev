#!/bin/sh
set -eu

case "$*" in
  "run -s classified_tool_calls")
    printf '\033[2mRunning with dbt=1.11.12\033[0m\n'
    printf '\033[2mRegistered adapter: duckdb=1.11.0\033[0m\n\n'
    sleep 0.4
    printf '1 of 1 START  classified_tool_calls  \033[36m[RUN]\033[0m\n'
    sleep 0.7
    printf '1 of 1 OK     classified_tool_calls  \033[32m[4 rows]\033[0m\n\n'
    printf '\033[1;32mCompleted successfully\033[0m\n'
    printf '\033[2mPASS=1  WARN=0  ERROR=0\033[0m\n'
    ;;
  "--quiet show -s tool_failure_summary")
    printf '| tool_name    | failure_type | call_count |\n'
    printf '| ------------ | ------------ | ---------- |\n'
    printf '| http_client  | unexpected   |          1 |\n'
    printf '| file_lookup  | expected     |          1 |\n'
    printf '| external_api | unexpected   |          1 |\n'
    printf '| file_lookup  | unknown      |          1 |\n'
    ;;
  *)
    printf 'unsupported demo command: dbt %s\n' "$*" >&2
    exit 2
    ;;
esac
