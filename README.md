# dbt_jev

`dbt_jev` evaluates SQL values with Jev through either
[TypeSafe AI's hosted API](https://docs.typesafe.ai/api) or
[OpenRouter](https://openrouter.ai/~typesafe/jev-latest/). The same public macros
work on DuckDB and ClickHouse. They expose Jev Choice as a nullable
label, Noul as a nullable match probability, and Score as a nullable numeric
rating.

<p align="center">
  <a href="demos/dbt_jev_x_demo.mp4">
    <img src="demos/dbt_jev_x_demo.gif" alt="Terminal demo showing dbt_jev classifying messy tool-call results as expected, unexpected, or unknown" width="800">
  </a>
</p>

_A real OpenRouter-backed result, replayed deterministically with
[VHS](https://github.com/charmbracelet/vhs). Click the demo for the MP4._

```sql
{{ config(materialized='table') }}

select
    span_id,
    {{ dbt_jev.classify(
        'tool_context',
        choices={
            'expected': 'An expected miss during exploration',
            'unexpected': 'An actual malfunction',
            'unknown': 'Insufficient evidence'
        }
    ) }} as failure_type
from {{ ref('agent_tool_calls') }}
```

Score a candidate pair after ordinary SQL has generated it:

```sql
select
    lead_id,
    customer_id,
    {{ dbt_jev.match_probability(
        'lead_record',
        'customer_record',
        instructions='Do these records represent the same customer?',
        criteria={
            'true': 'Both records identify the same customer',
            'false': 'The records identify different customers'
        }
    ) }} as match_probability
from {{ ref('int_leads_customers_candidates') }}
```

Apply an ordered rubric to one value:

```sql
select
    ticket_id,
    {{ dbt_jev.score(
        'ticket_context',
        levels=['No urgency', 'Needs attention', 'Urgent'],
        instructions='Rate the urgency of this support request'
    ) }} as urgency_score
from {{ ref('stg_support__tickets') }}
```

`match_probability` sends the two text-cast expressions as structured `left`
and `right` state and returns a number from 0 to 1. `score` returns the expected
score from 0 through `levels | length - 1`, so a three-level rubric can produce
fractional values such as `1.75`.

Batch several typed questions about the same input into one request with
`decisions`, then read each answer back with the accessors:

```sql
with judged as (
    select
        ticket_id,
        {{ dbt_jev.decisions(
            'ticket_context',
            questions={
                'failure_type': {
                    'type': 'choice',
                    'choices': {
                        'expected': 'An expected miss during exploration',
                        'unexpected': 'An actual malfunction',
                        'unknown': 'Insufficient evidence'
                    }
                },
                'urgency': {
                    'type': 'score',
                    'levels': ['No urgency', 'Needs attention', 'Urgent'],
                    'instructions': 'Rate the urgency of this support request'
                }
            }
        ) }} as decisions
    from {{ ref('stg_support__tickets') }}
)

select
    ticket_id,
    {{ dbt_jev.decision_label('decisions', 'failure_type') }} as failure_type,
    {{ dbt_jev.decision_value('decisions', 'urgency') }} as urgency_score
from judged
```

`decisions` returns a JSON object mapping each question id to its scalar answer
in a single Jev request. `decision_label` extracts a Choice label as text;
`decision_value` extracts a Noul probability or Score as double.

The macros only generate SQL. Inference starts when the database executes the
SQL, never while dbt parses or compiles it. Materialise decisions as tables so
downstream reads use stored answers.

## Tested support

| Component | Tested version |
| --- | --- |
| Python on the dbt runner | 3.12.12 |
| dbt Core | 1.11.12 |
| dbt-duckdb | 1.11.0 |
| DuckDB | 1.5.5 |
| dbt-clickhouse | 1.10.2 |
| ClickHouse Server | 26.8.9.10 |
| Python on the ClickHouse server | 3.10.12 |
| TypeSafe Python SDK | 0.7.0 |

This MVP deliberately targets the Python-based dbt Core v1 runtime. dbt v2 does
not load `dbt-duckdb` Python plugins, and dbt's current `functions/` resource does
not support DuckDB or ClickHouse.

## Install for DuckDB

These steps run on the dbt runner.

1. Install the runtime and DuckDB adapter in the same Python environment as dbt:

   ```bash
   python -m pip install -e '/path/to/dbt_jev[duckdb]'
   ```

2. Add this standalone package to the consuming project's `packages.yml`:

   ```yaml
   packages:
     - local: ../dbt_jev
   ```

3. Register the plugin in the DuckDB output in `profiles.yml`:

   ```yaml
   plugins:
     - module: dbt_jev.duckdb_plugin
   ```

4. Select a provider and set its credential in the runner environment. TypeSafe
   is the default:

   ```bash
   export TYPESAFE_API_KEY='...'
   dbt deps
   ```

   For OpenRouter:

   ```bash
   export DBT_JEV_PROVIDER=openrouter
   export OPENROUTER_API_KEY='...'
   dbt deps
   ```

`dbt deps` installs only the macro package. It does not install this Python
runtime. Credentials are read lazily when SQL first calls the function, so `dbt
parse` and `dbt compile` need no credential and make no API request.

## Install for self-hosted ClickHouse

ClickHouse separates dbt-runner installation from database-server installation.
The supported route is a self-hosted server whose administrator can install an
executable UDF. Network-access executable UDFs on ClickHouse Cloud are currently
private beta, so managed ClickHouse Cloud is outside this MVP.

On the dbt runner:

1. Install dbt Core and the adapter:

   ```bash
   python -m pip install 'dbt-core==1.11.12' 'dbt-clickhouse==1.10.2'
   ```

2. Add the local package entry shown above and run `dbt deps`.

On every ClickHouse server that can execute the function:

1. Install Python 3.10 or newer and install the runtime into that interpreter:

   ```bash
   sudo python3 -m pip install /path/to/dbt_jev
   ```

2. Install the executable and function configuration using the server's
   configured `user_scripts_path` and
   `user_defined_executable_functions_config` locations. The standard package
   defaults are:

   ```bash
   sudo install -m 0755 \
     /path/to/dbt_jev/install/clickhouse/dbt_jev_clickhouse_udf \
     /var/lib/clickhouse/user_scripts/dbt_jev_clickhouse_udf
   sudo install -m 0644 \
     /path/to/dbt_jev/install/clickhouse/dbt_jev_function.xml \
     /etc/clickhouse-server/dbt_jev_function.xml
   ```

3. Put `TYPESAFE_API_KEY` in the ClickHouse server service environment. For
   OpenRouter, put `DBT_JEV_PROVIDER=openrouter` and `OPENROUTER_API_KEY` there
   instead. Do not put credentials in SQL, dbt variables, or `profiles.yml`.
   Restart ClickHouse so it loads both the environment and UDF configuration.

4. Confirm that ClickHouse loaded the function:

   ```sql
   select name, origin
   from system.functions
   where name in ('jev_classify', 'jev_match_probability', 'jev_score');
   ```

The database administrator needs filesystem access to the server configuration,
permission to install the Python package, and permission to restart ClickHouse.
The dbt role needs its ordinary database creation/read permissions and must be
allowed to call the configured function. `dbt deps` does not install any of these
server-side prerequisites.

The supplied ClickHouse definitions use non-deterministic `executable_pool`
functions, `JSONEachRow`, nullable input/output, a pool of long-lived workers per
function (`pool_size`, default 4, which bounds server-side request concurrency),
and a 35-second block timeout. Adjust the pools and timeouts only after
considering API rate limits and the configured retry budget.

## Run the integration-test project with fixtures

The `integration_tests/` dbt project contains four synthetic tool-call records
plus pair-matching and scoring fixtures. It prepares `tool_context`, materialises
all three decision primitives, and aggregates classifications by tool and label.
Mock responses are protocol fixtures, not evidence of decision accuracy.

Prepare the environment once:

```bash
uv sync --all-extras
source .venv/bin/activate
```

For DuckDB, start the mock in one terminal:

```bash
python tests/mock_service.py --port 8000
```

Then run dbt in another terminal:

```bash
export TYPESAFE_API_KEY=mock-key
export TYPESAFE_BASE_URL=http://127.0.0.1:8000
cd integration_tests
DBT_PROFILES_DIR=. dbt deps
DBT_PROFILES_DIR=. dbt build --target duckdb
DBT_PROFILES_DIR=. dbt show --target duckdb \
  --inline "select * from {{ ref('tool_failure_summary') }} order by 1, 2"
```

For ClickHouse, the reproducible container installs the shared runtime and
executable UDF on the database server:

```bash
docker compose up --build --wait
export DBT_ENV_SECRET_CLICKHOUSE_PASSWORD=dbt
cd integration_tests
DBT_PROFILES_DIR=. dbt deps
DBT_PROFILES_DIR=. dbt build --target clickhouse
DBT_PROFILES_DIR=. dbt show --target clickhouse \
  --inline "select * from {{ ref('tool_failure_summary') }} order by 1, 2"
cd ..
docker compose down --volumes
```

The composed server calls `http://mock:8000`; the API credential and endpoint
exist only in its environment. The dbt runner receives only the ClickHouse
password. To test OpenRouter instead, start the stack with
`DBT_JEV_PROVIDER=openrouter docker compose up --build --wait`.

## Runtime configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `DBT_JEV_PROVIDER` | `typesafe` | `typesafe` or `openrouter` |
| `TYPESAFE_API_KEY` | required for `typesafe` | Direct TypeSafe Bearer credential |
| `TYPESAFE_BASE_URL` | `https://api.typesafe.ai` | Direct API base URL |
| `TYPESAFE_DEFAULT_MODEL` | `jev-latest` | Direct Jev model or alias |
| `OPENROUTER_API_KEY` | required for `openrouter` | OpenRouter Bearer credential |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai` | OpenRouter origin, not `/api/v1` |
| `OPENROUTER_MODEL` | `typesafe/jev-1.13` | Decisions API model |
| `DBT_JEV_BASE_URL` | unset | Provider-independent base URL override |
| `DBT_JEV_MODEL` | unset | Provider-independent model override |
| `DBT_JEV_REQUEST_TIMEOUT` | `10` | Per-attempt timeout in seconds |
| `DBT_JEV_MAX_RETRIES` | `2` | Retries after the first attempt |
| `DBT_JEV_BACKOFF_INITIAL` | `0.5` | Initial exponential backoff in seconds |
| `DBT_JEV_BACKOFF_MAX` | `5` | Maximum backoff in seconds |
| `DBT_JEV_BACKOFF_JITTER` | `0.5` | Backoff jitter fraction (0 = fixed, 1 = full jitter) |
| `DBT_JEV_RETRY_BUDGET` | `30` | Total retry budget in seconds |
| `DBT_JEV_MAX_CONCURRENCY` | `8` | Max in-flight requests per DuckDB connection |

DuckDB may receive the non-secret settings in the plugin's `config` mapping.
API keys are intentionally rejected there. ClickHouse reads all settings from
the database-server environment; restart its UDF worker after changing them.

The TypeSafe route uses the official SDK's bounded retry policy. The OpenRouter
route retries documented transient statuses (`429`, `500`, `502`, `503`, `524`,
and `529`), connection failures, and timeouts within the configured budgets.
Authentication failure, malformed responses, exhausted retries, and out-of-set
labels raise sanitised query errors. They never become the semantic label
`unknown`.

## Operational limits

- Execution is row-level but throughput-aware. The DuckDB plugin registers
  vectorized (Arrow) UDFs, so each data chunk is evaluated with bounded
  concurrency (`DBT_JEV_MAX_CONCURRENCY`, default 8) and identical inputs are
  de-duplicated to a single request within the run. On ClickHouse, concurrency is
  the executable-UDF `pool_size` (default 4). There is still no exactly-once
  guarantee or durable cross-run cache: retries can repeat calls, and re-running a
  model re-issues requests unless you materialise results as a table.
- Prefer `dbt_jev.decisions` when a model derives several Jev columns from the
  same input. Jev scores every question in a request in parallel for roughly the
  cost of one, so batching questions is far cheaper and faster than emitting a
  separate primitive (and its own request) per column.
- DuckDB registers the function with `side_effects=True`. ClickHouse declares
  the executable UDF non-deterministic. Optimisers must not assume a pure result.
- Jev Choice accepts at most 255 labels. This package additionally requires at
  least two non-empty text labels with non-empty text descriptions.
- `match_probability` returns SQL NULL without a request when either input is
  NULL. `score` and `classify` do the same when their input is NULL.
- Row content leaves the database and is sent to the selected provider. Review
  data-handling requirements before using production data.
- The ClickHouse pool runs on the database server and duplicates its Python
  runtime once per configured worker. A larger pool also increases outbound API
  concurrency.

See [the runtime reference](docs/reference.md) and
[the design explanation](docs/design.md) for the SQL contract, protocol sources,
and relationship to `dbt_context_engineering`.

## Test and build

```bash
uv sync --all-extras
.venv/bin/pytest -m "not clickhouse"
./scripts/test_clickhouse.sh
uvx ruff check .
uvx ruff format --check .
uv build
```

The ClickHouse script runs both TypeSafe-shaped and OpenRouter-shaped fixture
routes against actual DuckDB and ClickHouse databases. The hosted smoke test is
opt-in and uses synthetic input only:

```bash
TYPESAFE_API_KEY='...' uv run python scripts/live_smoke.py
DBT_JEV_PROVIDER=openrouter OPENROUTER_API_KEY='...' \
  uv run python scripts/live_smoke.py
```
