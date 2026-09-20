# Integration tests

This directory is a self-contained dbt project that installs the package from
its parent directory. It uses only synthetic tool-call records and supports the
`duckdb` and `clickhouse` targets defined in `profiles.yml`.

The project follows the standard dbt package-test shape: a seed, a staging view,
materialized mart tables, YAML data tests, and one `dbt build` entry point. The
Python test suite additionally asserts request equivalence, error handling, and
that parsing, compilation, and reads do not trigger inference.

From this directory, after starting the fixture service described in the root
README, run:

```bash
DBT_PROFILES_DIR=. dbt deps
DBT_PROFILES_DIR=. dbt build --target duckdb
```

For the repository-managed ClickHouse service, use the root-level
`scripts/test_clickhouse.sh` command so the server-side UDF is installed before
dbt runs.
