# Runtime reference

## Macro

```jinja
dbt_jev.classify(input_expr, choices)
```

`input_expr` is inserted as a SQL expression and cast to text. `choices` must be a
compile-time Jinja mapping containing 2–255 non-empty text labels with non-empty
text descriptions. The macro validates the mapping, serialises it as JSON, and
escapes it as a SQL string literal. It does not evaluate `input_expr` or contact
Jev.

The return type is nullable SQL text. A non-NULL result is guaranteed to be one of
the supplied mapping keys. SQL NULL input returns SQL NULL without an HTTP request.

Adapter dispatch emits:

| Adapter | SQL function |
| --- | --- |
| DuckDB | `jev_classify(cast(input_expr as varchar), choices_json)` |
| ClickHouse | `jev_classify(cast(input_expr as Nullable(String)), cast(choices_json as String))` |

Unsupported adapters raise a compiler error.

## Jev request

Both SQL functions call the shared Python runtime. Each row produces this semantic
request on either provider:

```json
{
  "state": "<input text>",
  "model": "jev-latest",
  "questions": {
    "classification": {
      "type": "choice",
      "instructions": "Classify the state into exactly one of the supplied choices.",
      "criteria": {"label": "description"}
    }
  }
}
```

With `DBT_JEV_PROVIDER=typesafe` (the default), the official `typesafe-sdk` sends it
to `POST /v1/systemone`. With `DBT_JEV_PROVIDER=openrouter`, the runtime sends it to
OpenRouter's `POST /api/alpha/decisions`. OpenRouter's model default is
`typesafe/jev-1.13`; its base URL is the origin `https://openrouter.ai`, not the
chat-compatible `/api/v1` base.

The runtime reads `answers.classification.choice` and rejects a label absent from
`criteria`. It does not use probabilities or confidence in the MVP. Provider
credentials are selected at execution and never appear in this request body or in
compiled SQL.

## Errors

Errors exposed through SQL omit API keys and response bodies:

- missing provider credential: configuration error naming the required environment
  variable;
- HTTP 401: authentication error without the credential value;
- retryable failure after the configured budget: bounded failure with status or
  timeout information;
- structurally invalid success response: malformed-response error;
- response label outside the supplied criteria: out-of-set error.

No transport or protocol error is mapped to a classification label.

## Backend execution

DuckDB registers `jev_classify(VARCHAR, VARCHAR) -> VARCHAR` on every adapter
connection through the `dbt-duckdb` Python plugin. It uses DuckDB's default
NULL propagation and declares `side_effects=True`.

ClickHouse loads `jev_classify(Nullable(String), String) -> Nullable(String)`
from `install/clickhouse/dbt_jev_function.xml`. Its non-deterministic
`executable_pool` sends named arguments and results as `JSONEachRow` to a
long-lived Python worker. A JSON `null` input returns JSON `null` before runtime
configuration or credentials are loaded.

Both wrappers call the same `dbt_jev.runtime` implementation. Credentials and
provider selection are read in the process that executes that implementation:
the dbt runner for DuckDB and the database server for ClickHouse.
