# Design and integration boundaries

## Protocol source

The implementation follows TypeSafe's current primary documentation:

- [HTTP API reference](https://docs.typesafe.ai/api): `POST /v1/systemone`, Bearer
  authentication, request and response shapes, and documented HTTP failures.
- [Choice primitive](https://docs.typesafe.ai/primitives/choice): criteria semantics
  and the maximum of 255 options.
- [Noul primitive](https://docs.typesafe.ai/primitives/noul): yes/no probability
  semantics used by `match_probability`.
- [Score primitive](https://docs.typesafe.ai/primitives/score): ordered rubric and
  expected-score semantics used by `score`.
- [Models](https://docs.typesafe.ai/models): `jev-latest`, text input, context and
  rate limits.
- [Python SDK](https://docs.typesafe.ai/sdk/python): official synchronous client,
  timeouts, and bounded retry policy.

The optional OpenRouter route follows its newly published primary sources checked
on 2026-09-20:

- [TypeSafe provider page](https://openrouter.ai/typesafe) and
  [Jev Latest page](https://openrouter.ai/~typesafe/jev-latest/) for availability,
  model identity, and context window.
- [OpenRouter's Jev lab](https://openrouter.ai/labs/jev/compile) for the actual
  `alpha.decisions.create` usage rather than assuming the chat-completions shape.
- [Official TypeScript SDK source](https://github.com/OpenRouterTeam/typescript-sdk/blob/main/src/funcs/alphaDecisionsCreate.ts)
  for `POST /api/alpha/decisions`, Bearer authentication, response types, error
  statuses, timeout, and retry boundaries.

The moving OpenRouter landing-page alias is `~typesafe/jev-latest`, but the current
official Decisions example uses `typesafe/jev-1.13`; the latter is therefore the
runtime default. `OPENROUTER_MODEL` or `DBT_JEV_MODEL` can override it. This MVP
does not silently fall back from Decisions to chat completions.

The [`duckdb-jev` source](https://github.com/colliber/duckdb-jev) independently
confirmed the endpoint path, Choice response field, per-row execution model, and
the need to treat rate limits, server failures, and dropped connections as
transient. This package does not use or build its native DuckDB extension.

At the time of implementation, Jev 1.13 accepts text or structured JSON state,
documents a 64k-token request budget, a 32k-token budget for `state` plus the
longest question, and at most 255 Choice options. The moving `jev-latest` alias
can change behaviour; set
`TYPESAFE_DEFAULT_MODEL=jev-1.13.0` when a deployment needs a pinned model.

## Why these backend mechanisms

`dbt-duckdb` documents a Python plugin hook that receives every DuckDB connection
and can register Python scalar functions. It keeps the official Jev client in the
dbt runner process and requires no compiled extension.

ClickHouse's documented executable-UDF mechanism streams blocks to an external
program over standard input/output. An `executable_pool` retains Python processes
instead of paying process-startup cost for each block. The wrapper uses named
`JSONEachRow` fields so quotes, Unicode, newlines, and SQL literal escaping remain
separate concerns. It imports the same runtime package, accepts nullable input,
and is explicitly non-deterministic. No compiled extension is required.

dbt's native `functions/` resource does not currently support DuckDB or
ClickHouse. dbt v2 also does not load `dbt-duckdb`'s Python plugins. The MVP
therefore targets dbt Core v1 and uses the backend mechanisms above.

Provider selection and credentials are runtime configuration. They are never
macro arguments, JSON criteria, or SQL literals, so they do not enter compiled
SQL, manifests, run results, or source control.

## Possible local-model provider

Laya's `choice` request and response can map to this package's public
`input_expr` and `choices` contract. It is not part of the MVP. A future
implementation could add it inside the shared Python runtime: DuckDB's plugin
would retain the model in the dbt runner and ClickHouse's executable pool would
retain it on the database server. The large model dependencies and checkpoints
would remain an optional installation extra, and provider-specific confidence or
probability outputs would not change the label-only `classify` contract.

## Relationship to dbt_context_engineering

[`dbt_context_engineering.classify`](https://github.com/dbt-labs/dbt-context-engineering/blob/main/macros/functions/classify.sql)
is also a dispatched row-level SQL expression, so the execution model is aligned.
Its public interface is not directly compatible, however: it accepts
`(input_column, prompt, output_schema, model)`, extracts enum labels from a JSON
Schema, and applies package-specific safety gates. `dbt_jev.classify` accepts
`(input_expr, choices)` where descriptions are first-class Jev criteria.

The smallest useful upstream extension point would be a classification-provider
dispatch beneath `dbt_context_engineering.classify`. A Jev provider could translate
the first enum-bearing schema property plus its per-enum descriptions into the
`choices` mapping, then call this package's runtime SQL function. Merely changing
dbt dispatch search order is insufficient because the macro signatures differ, and
replacing the existing adapter macro would bypass its safety gates. This package
does not add that integration.

## Deliberate exclusions

There is no provider framework, service, durable inference cache, evaluation
framework, or automatic batching. Scalar network calls are expensive and may be
repeated by SQL re-evaluation or retries. The example therefore uses ordinary table
materialisation so downstream queries do not invoke Jev again.
