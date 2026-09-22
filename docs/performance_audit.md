# Jev API efficiency & performance audit

_Scope: how `dbt_jev` calls the Jev decision API, and where it leaves latency,
throughput, and cost on the table. Findings are grounded in the current runtime
(`src/dbt_jev/`), the ClickHouse UDF config (`install/clickhouse/`), and the
public behaviour of the Jev API and comparable per-row LLM-in-SQL integrations
(sources at the end)._

## TL;DR

The package is correct and safe, but it calls Jev in the **slowest and least
economical shape the API allows**:

1. **Every call is globally serialized** behind one process lock
   (`runtime.py:360`). No matter how many cores the database has or how high
   Jev's rate limits are, requests go out strictly one at a time. This is the
   single biggest problem and it is a throughput problem, not a correctness one.
2. **Every macro call is its own HTTP request.** Jev's headline optimization is
   *many typed questions against one state, scored in parallel* — the vendor
   cookbook reports **~12x cheaper and ~10x faster** for a 13-question batch vs.
   one-at-a-time. When a model derives several Jev columns from the same input
   row, we send N separate requests and re-pay the state tokens N times instead
   of batching N questions into one.
3. **No de-duplication or cache.** Jev bills input tokens only, so re-sending an
   identical `(state, question)` is pure waste. dbt workloads repeat short
   categorical text constantly.

Because Jev is priced at **$0.042 / M input tokens with output free**, raw cost
is usually *not* the pain — **latency/throughput is**. A 100k-row classify at
~200 ms/call runs **~5.6 hours fully serial**; modest bounded concurrency brings
that under the Jev **1,200 req/min** ceiling to **~1.4 hours (~4x)**, and
question-batching + de-dup cut both time and spend further on top.

Priority order for the work: **concurrency (P0) → question-batching (P0/P1) →
de-dup/cache (P1) → connection reuse + retry hygiene (P1) → token & call-volume
reduction guidance (P2).**

## Implementation status

All **P0 and P1** findings below are now implemented on the runtime and both
adapters:

| Finding | Status | What shipped |
| --- | --- | --- |
| P0-1 concurrency | Done | Global lock removed; per-thread clients/connections; bounded `ThreadPoolExecutor` sized by `DBT_JEV_MAX_CONCURRENCY` (default 8); DuckDB converted to vectorized Arrow UDFs so a chunk fans out. |
| P0-2 question-batching | Done | New `dbt_jev.decisions(input, questions)` macro + `jev_decisions` UDF send many typed questions against one state in one request; `decision_label` / `decision_value` accessors read answers back. |
| P1-1 de-dup | Done (in-run) | Identical `(state, question)` inputs collapse to one request within a batch. Durable cross-run cache remains out of scope (materialise as a table). |
| P1-2 connection reuse | Done | OpenRouter route uses a per-thread keep-alive `http.client` connection instead of a new socket per row. |
| P1-3 retry hygiene | Done | Jittered backoff (`DBT_JEV_BACKOFF_JITTER`), `Retry-After` honored on 429/503, and sleeps no longer run under a shared lock. |
| P1-4 ClickHouse concurrency | Done | `pool_size` raised to a configurable default of 4 (bounds server-side request concurrency) + `jev_decisions` added. Intra-worker block fan-out (needs `send_chunk_header`) is left as a follow-up. |

P2 items (Arrow UDFs — delivered early as the vehicle for P0-1; SQL pre-filter /
narrow-projection guidance; durable cache) are documented but only partially
implemented. The findings below are retained as written for the rationale and
evidence.

---

## 1. What Jev charges for (the economics that drive this audit)

These facts decide which optimizations matter. All are from Jev's public docs and
independent write-ups (see Sources); they are "as of early access, subject to
change," so the package should treat them as tunable, not hard-coded.

| Property | Value | Consequence for `dbt_jev` |
| --- | --- | --- |
| Price | **$0.042 / M input tokens, output free** | Cost is driven entirely by **input tokens sent**. Re-sending the same state/question is the only real "waste." |
| Latency | **~70–500 ms per request** (one parallel pass over all questions) | Per-row round-trip dominates wall-clock. Serial execution multiplies it by row count. |
| Batching model | **One `state`, a *map* of `questions`, all scored in parallel** | Adding questions "barely changes response time" and only costs the extra question tokens. This is the native lever. |
| Vendor benchmark | 13-question briefing batched = **12.2x cheaper, 10.0x faster** vs one-by-one; 21 questions answered inside ~0.7 s | Quantifies the upside of question-batching. |
| Multi-state batching | **Not supported** — one state per `POST /v1/systemone` | You cannot pack different rows into one request. Cross-row scale = **concurrency**, not a batch endpoint. |
| Rate limits | **~1,200 req/min and ~250,000 tokens/sec**, 32k tokens/request, `429` on exceed | Sets the concurrency ceiling. At 200 ms/call, ~4–10 in-flight requests already saturate the 1,200/min cap. |
| Context | 64k/request; 32k for state + longest question | Comfortable headroom; not a current constraint. |
| Prompt/state caching | Not documented for Jev | Don't design around it. The equivalent win here is **de-dup + question-batching**, which we control. |

**Two workload archetypes** (the right fix depends on which you have):

- **Tall** — many rows, one primitive (e.g. classify 100k tickets). Bottleneck:
  serial round-trips. Levers: **concurrency, de-dup, SQL pre-filtering.**
- **Wide** — one row, several primitives (e.g. classify a ticket *and* score its
  urgency *and* match it to an account). Bottleneck: N requests re-sending the
  same state. Lever: **question-batching** (Jev's native 10–12x win).

Real pipelines are usually **tall × wide**, so both levers compound.

---

## 2. What the code does today

- **DuckDB** (`duckdb_plugin.py:67-87`): registers `jev_classify` /
  `jev_match_probability` / `jev_score` as **native, per-tuple** scalar UDFs
  (`connection.create_function(...)`, no `type='arrow'`). DuckDB invokes the
  Python callback **once per row**; each callback makes **one** Jev request.
- **ClickHouse** (`install/clickhouse/dbt_jev_function.xml`): `executable_pool`
  with **`pool_size=1`**; `clickhouse_udf.py:78-91` `serve()` reads **one
  JSONEachRow line, makes one request, writes one line, flushes** — strictly
  sequential, even though ClickHouse streams whole blocks.
- **Shared runtime** (`runtime.py`): every request goes through
  `JevClassifier._ask` (`runtime.py:351-374`), which wraps the **entire** call —
  including the OpenRouter retry loop and its `time.sleep` backoff
  (`runtime.py:391-440`) — in **`with self._lock:`**. One `threading.Lock` per
  classifier, held for the full network round-trip.
- **OpenRouter transport** (`runtime.py:395-408`): a fresh
  `urllib.request.urlopen` per call — **new TCP + TLS handshake every row**, no
  keep-alive or connection pool. (The TypeSafe SDK path does own connection
  reuse.)
- **No cache, no de-dup, no request coalescing** anywhere. `README.md` and
  `docs/design.md` state this explicitly as a deliberate MVP exclusion
  ("no automatic batching, durable cache").

---

## 3. Findings (prioritized)

### P0-1 — Global lock serializes all inference

**Evidence:** `runtime.py:360` — `with self._lock:` around the whole of `_ask`,
covering the SDK call / the OpenRouter HTTP request *and* its backoff sleeps.

**Impact:** Throughput is capped at **one in-flight request, period.** For an
I/O-bound workload where each call waits 70–500 ms on the network, this is the
dominant cost. Worse, on the OpenRouter path a single row's backoff `sleep`
(`runtime.py:440`) blocks *every other row* because the lock is held during the
sleep. Concurrency the database could otherwise provide (DuckDB's execution
threads; more ClickHouse workers) is thrown away.

**Fix:** Make concurrency safe rather than forbidden.
- Don't just delete the lock (the comment correctly warns the sync SDK client may
  not be thread-safe). Instead, give each worker its own client, or use a small
  pool of clients, or the SDK's async client if `typesafe-sdk` 0.7 exposes one.
- Narrow the critical section to only what must be serialized (client
  init/state), never the network wait or the backoff sleep.
- Drive concurrency from a **bounded** `ThreadPoolExecutor` + `Semaphore` sized
  to stay under Jev's ~1,200 req/min ceiling (configurable, e.g.
  `DBT_JEV_MAX_CONCURRENCY`, default ~8).

**Expected result:** ~5–10x wall-clock reduction on tall workloads before the RPM
cap binds. Effort: **medium.**

### P0-2 — One request per macro call wastes Jev's native question-batching

**Evidence:** Each of `classify` / `match_probability` / `score`
(`runtime.py:267-349`) issues exactly one `_ask`, carrying exactly one question.
The DuckDB (`duckdb_plugin.py`) and ClickHouse (`clickhouse_udf.py`) layers each
map one SQL function call to one request. The mock even hard-codes
"expected one question" (`tests/mock_service.py:95`).

**Impact:** For **wide** models — multiple Jev columns computed from the same
input expression on the same row — we make one request *per column* and re-pay
the (often large) **state tokens** each time, at N× the latency. Jev is built to
answer all of them in one parallel pass for roughly the cost of one. This is the
documented 10–12x opportunity, unused.

**Fix (design-level, biggest single win where it applies):** coalesce questions
that share a state into one `systemone` request.
- Simplest, ship-now: a `dbt_jev.decisions(input_expr, {...questions...})` macro
  that takes several primitives against one input and emits **one** UDF call
  returning a struct, so the runtime sends one request with a `questions` map.
- More ambitious: a request-coalescing layer that batches questions sharing a
  `(state)` within a vector/block automatically. Note the scalar-UDF model makes
  *automatic* coalescing across separate SQL function calls hard (each call is
  independent to the engine) — hence the explicit multi-question macro is the
  pragmatic path.

**Expected result:** up to N× fewer requests and up to N× less state-token spend
for wide models, at ~unchanged latency per row. Effort: **medium–large.**

### P1-1 — No de-duplication or cache of identical inputs

**Evidence:** No memoization in `runtime.py`; `README.md` "Operational limits"
confirms "no durable cache," and retries "can repeat calls."

**Impact:** Every non-NULL row calls Jev even when an identical `(state,
question)` was just answered. Because input tokens are the only billable unit,
duplicate inputs are 100% wasted spend *and* latency. Categorical/short-text
columns in dbt repeat heavily; re-running `dbt build` re-pays everything.

**Fix:**
- **In-run memoization** keyed by a hash of `(provider, model, question
  signature, state)` — cheap, safe, big win when duplicates exist.
- Optional **durable cache** (e.g. a dbt-side lookup table or a keyed store) so
  re-runs and incremental models reuse prior answers; document that Jev is
  non-deterministic so cache semantics are "reuse a prior sample," not "pure."
- Reinforce the existing, correct guidance to **materialize as a table** so
  downstream reads never re-invoke Jev.

**Expected result:** savings proportional to duplicate rate (often 30–80% on
categorical columns); faster re-runs. Effort: **small (in-run) / medium
(durable).**

### P1-2 — OpenRouter transport opens a new connection every row

**Evidence:** `runtime.py:395-408` builds a fresh `urllib.request.Request` and
`urlopen` per call; no session/pool is retained.

**Impact:** Adds a TCP + TLS handshake to every OpenRouter row on top of Jev's
compute time — pure per-row overhead, and it compounds under the serial lock.

**Fix:** Keep a persistent pooled connection (e.g. `http.client`/`urllib3`
`PoolManager`, or `requests.Session`) on the classifier, reused across calls and
safe for the concurrency model chosen in P0-1.

**Expected result:** meaningful per-row latency cut on OpenRouter, especially for
short states where handshakes are a large fraction of the round-trip. Effort:
**small.**

### P1-3 — Retry backoff lacks jitter and blocks under the lock

**Evidence:** TypeSafe `RetryPolicy(..., backoff_jitter=0.0)`
(`runtime.py:242`); OpenRouter backoff is deterministic
(`runtime.py:432-440`) and executed **inside** the lock.

**Impact:** Once concurrency is added, deterministic backoff makes many workers
retry in lockstep after a `429`, amplifying rate-limit storms. Today the
in-lock sleep also stalls unrelated rows.

**Fix:** Add jitter to both retry paths; move sleeps out of any shared critical
section; on `429`, honor `Retry-After` / rate-limit response headers rather than
fixed backoff. Pair with the concurrency limiter from P0-1.

**Effort: small.**

### P1-4 — ClickHouse path is single-worker and row-at-a-time

**Evidence:** `pool_size=1` for all three functions
(`install/clickhouse/dbt_jev_function.xml`); `serve()` processes one line per
loop with a `flush` each row (`clickhouse_udf.py:81-91`).

**Impact:** On ClickHouse, inference is serial per function regardless of server
size, mirroring P0-1 on the database-server side.

**Fix:** Two complementary levers — (a) raise `pool_size` so ClickHouse runs
several worker processes concurrently (README already flags this trades against
API rate limits — tie it to the same concurrency budget), and (b) let each
worker read a block and fan its rows out concurrently (bounded) before writing
results, instead of strict line-by-line. Keep `max_command_execution_time` (35 s)
in mind when sizing.

**Effort: medium.**

### P2-1 — Adopt DuckDB vectorized (Arrow) UDFs to enable batched fan-out

**Evidence:** `duckdb_plugin.py:67-87` registers native per-tuple UDFs. DuckDB
supports `type='arrow'` scalar UDFs that receive a whole chunk
(~`STANDARD_VECTOR_SIZE` rows) and return a chunk, often zero-copy.

**Impact/opportunity:** A vectorized UDF hands the runtime a *vector of rows per
call*, which is the natural place to (a) issue concurrent requests for the chunk
(I/O releases the GIL) and (b) de-dup identical states within the chunk before
calling Jev. This is the cleanest DuckDB mechanism to realize P0-1 and P1-1
together.

**Effort: medium** (it changes the registration + runtime entry shape).

### P2-2 — Give users call-volume and token-reduction levers (docs + macro)

Comparable LLM-in-SQL work (see Sources) consistently shows the cheapest call is
the one you don't make:

- **SQL pre-filtering / cascades:** resolve easy rows with plain SQL and only
  send ambiguous rows to Jev, e.g.
  `case when <cheap rule> then '<label>' else jev_classify(<input>, <choices>) end`.
- **Narrow projection:** send only the text needed as `state`, not a whole
  concatenated row — fewer input tokens = lower cost and latency.
- **Concise criteria/instructions:** these are re-sent per request; trimming them
  reduces the per-row token floor (and, with question-batching, is amortized).
- **Incremental models:** only classify new/changed rows.

Document these as first-class patterns; consider a helper macro for the
pre-filter cascade. **Effort: small (docs) + optional small macro.**

---

## 4. What comparable integrations do (benchmark)

- **Jev's own guidance / cookbook:** batch many typed questions per state; adding
  questions is nearly free in both time and money (12.2x/10.0x figures).
- **Anthropic Message Batches / OpenAI Batch API:** async, up to 50% token
  discount, ~24 h SLA, and it **stacks with prompt caching**. Directly analogous
  to dbt's offline nightly builds — *if* Jev adds an async batch tier this becomes
  the cost play; today Jev is already cheap/fast enough that **concurrency +
  question-batching** is the better fit.
- **Snowflake Cortex AISQL / `AI_CLASSIFY`, BigQuery `ML.GENERATE_TEXT`:** push
  batch inference into the warehouse and lean on **massive parallel scale-out**
  (Snowflake cites ~134k input tok/s on small models) rather than per-row serial
  calls — the throughput model `dbt_jev` should imitate via concurrency.
- **OpenRouter production guidance:** bound in-flight requests with an
  `asyncio.Semaphore` (3–5 per key), read live limits from `GET
  /api/v1/auth/key` and the rate-limit response headers, retry `429` with
  backoff. Confirms the "bounded concurrency + header-aware retry" design.
- **Academic (VLDB/arXiv "Optimizing LLM Queries in Relational Analytics"):**
  parallelize but expect **rate-limit ceilings**; row-marshaling helps only with
  **diminishing returns** and added latency; **pre-filter with SQL heuristics**
  and **narrow projection** to cut calls and tokens. Reinforces P0-1, P2-1, P2-2
  and cautions against over-batching rows into one prompt (which would also break
  Jev's typed contract).

---

## 5. Recommended roadmap

**Phase 1 — throughput (P0/P1, mostly runtime-local, no API contract change)**
1. Bounded concurrency with per-worker/pooled clients; narrow the lock (P0-1).
2. In-run de-dup/memoization of identical `(state, question)` (P1-1).
3. Pooled OpenRouter connections; jittered, header-aware retries out of the lock
   (P1-2, P1-3).
4. ClickHouse: expose `pool_size`/concurrency budget; optional block fan-out
   (P1-4).

**Phase 2 — question-batching (P0-2, the native 10–12x lever)**
5. A multi-question macro (`dbt_jev.decisions(...)`) that sends several primitives
   against one state in a single request and returns a struct.

**Phase 3 — batched fan-out & call-volume reduction (P2)**
6. DuckDB Arrow/vectorized UDFs as the fan-out + in-chunk de-dup point (P2-1).
7. Docs + optional macros for SQL pre-filter cascades, narrow projection,
   incremental models; optional durable cache (P2-2, P1-1 durable).

Every new lever should be **config-gated and default-safe** (concurrency,
cache on/off, batch size), tied to `DBT_JEV_*` env vars so behaviour stays
predictable and rate-limit-respecting.

## 6. A note on framing

At **$0.042 / M input tokens with output free**, most teams will find Jev spend
small; the headline win from this audit is **wall-clock throughput** (serial →
bounded-concurrent + question-batched), with **cost savings as a bonus** from
de-dup and amortized state tokens. Prioritize accordingly.

---

## Sources

- [Introducing System One Models & Jev — TypeSafe AI](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
- [Models — TypeSafe AI docs](https://docs.typesafe.ai/models)
- [Jev rate limits, context window and pricing — OpenTweet](https://opentweet.io/jev/limits)
- [Jev API — TypeSafe System One Model API access & docs](https://jevapi.org/)
- [TypeSafe Jev explained: how it works, LLM differences and API pricing — Requesty](https://www.requesty.ai/blog/typesafe-jev-explained)
- [What Is Jev? A Guide to TypeSafe AI's System One Model — LangChain](https://www.langchain.com/blog/building-a-harness-with-jev)
- [How to Use Jev: a practical guide — DEV Community](https://dev.to/valyuai/how-to-use-jev-a-practical-guide-to-typesafes-system-one-model-g5e)
- [TypeSafe Jev now available in AI Gateway — Netlify](https://www.netlify.com/changelog/typesafe-jev-ai-gateway/)
- [Batch processing — Claude Platform docs](https://platform.claude.com/docs/en/build-with-claude/batch-processing)
- [Anthropic Message Batches API: 50% off async jobs — Respan](https://www.respan.ai/articles/anthropic-message-batches-api)
- [Scale Unstructured Text Analytics with Batch LLM Inference — Snowflake](https://www.snowflake.com/en/blog/batch-llm-inference-text-analytics-cortex/)
- [Snowflake Cortex AISQL functions — Snowflake docs](https://docs.snowflake.com/en/user-guide/snowflake-cortex/aisql)
- [OpenRouter API rate limits](https://openrouter.ai/docs/api_reference/limits)
- [Optimizing LLM Queries in Relational Data Analytics Workloads — arXiv 2403.05821](https://arxiv.org/pdf/2403.05821)
- [Quickly Expanding DuckDB's Functionality with Scalar Python UDFs — DuckDB](https://duckdb.org/2023/07/07/python-udf)
- [DuckDB Python Function API (native vs. arrow UDFs)](https://duckdb.org/docs/current/clients/python/function)
