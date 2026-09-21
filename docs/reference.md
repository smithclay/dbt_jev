# Runtime reference

## Macros

### `classify`

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

### `match_probability`

```jinja
dbt_jev.match_probability(left, right, instructions, criteria=none)
```

`left` and `right` are SQL expressions. Each is cast to text, then sent as the
corresponding property of structured Jev state. `instructions` must be non-empty
compile-time text. Optional `criteria` is a compile-time mapping whose only
permitted keys are `true` and `false`; each present value must be a non-empty text
description.

The return type is nullable SQL double precision. A result is Jev's Noul
probability from 0 to 1 that the instructions are true for the pair. If either
SQL input is NULL, the result is NULL and no request is made.

| Adapter | SQL function |
| --- | --- |
| DuckDB | `jev_match_probability(left, right, instructions, criteria_json)` |
| ClickHouse | `jev_match_probability(left, right, instructions, criteria_json)` |

### `score`

```jinja
dbt_jev.score(input_expr, levels, instructions)
```

`input_expr` is inserted as a SQL expression and cast to text. `levels` must be
a non-empty, ordered compile-time sequence of non-empty text descriptions. Their
zero-based positions define the rubric values. `instructions` must be non-empty
compile-time text.

The return type is nullable SQL double precision. The result is Jev's
probability-weighted expected score and can be fractional, from 0 through
`levels | length - 1`. SQL NULL input returns NULL without a request.

| Adapter | SQL function |
| --- | --- |
| DuckDB | `jev_score(cast(input_expr as varchar), levels_json, instructions)` |
| ClickHouse | `jev_score(cast(input_expr as Nullable(String)), levels_json, instructions)` |

## Jev request

All SQL functions call the shared Python runtime. `classify` produces this
semantic request on either provider:

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

`match_probability` instead sends structured state shaped as
`{"left": "...", "right": "..."}` with a `match` question of type `noul`.
`score` sends text state with a `score` question of type `score` and the ordered
levels as its criteria.

The runtime reads `answers.classification.choice`, `answers.match.noul`, or
`answers.score.score` as appropriate. It rejects an out-of-set Choice label,
a Noul value outside 0–1, or a Score outside the supplied rubric. It intentionally
does not expose provider confidence or the full probability distribution.
Provider credentials are selected at execution and never appear in request
criteria or compiled SQL.

## Errors

Errors exposed through SQL omit API keys and response bodies:

- missing provider credential: configuration error naming the required environment
  variable;
- HTTP 401: authentication error without the credential value;
- retryable failure after the configured budget: bounded failure with status or
  timeout information;
- structurally invalid success response: malformed-response error;
- response label or numeric result outside the supplied criteria: out-of-range
  error.

No transport or protocol error is mapped to a classification label.

## Backend execution

DuckDB registers three scalar functions on every adapter connection through the
`dbt-duckdb` Python plugin:

- `jev_classify(VARCHAR, VARCHAR) -> VARCHAR`;
- `jev_match_probability(VARCHAR, VARCHAR, VARCHAR, VARCHAR) -> DOUBLE`;
- `jev_score(VARCHAR, VARCHAR, VARCHAR) -> DOUBLE`.

They use DuckDB's default NULL propagation and declare `side_effects=True`.

ClickHouse loads equivalent nullable functions from
`install/clickhouse/dbt_jev_function.xml`: `jev_classify` returns
`Nullable(String)`, while `jev_match_probability` and `jev_score` return
`Nullable(Float64)`. The non-deterministic `executable_pool` functions send
named arguments and results as `JSONEachRow` to long-lived Python workers. An
incomplete NULL input returns JSON `null` before runtime configuration or
credentials are loaded.

Both wrappers call the same `dbt_jev.runtime` implementation. Credentials and
provider selection are read in the process that executes that implementation:
the dbt runner for DuckDB and the database server for ClickHouse.
