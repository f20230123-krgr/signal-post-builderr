# Architecture & Tech Stack

## Language

Python. The provided starter kit and recommended libraries (Scrapy, extruct,
Trafilatura, Playwright, Pydantic, RapidFuzz) are all Python-native — matching
the ecosystem saves engineering time against the fixed October deadline.

## Stage-by-stage tool choices, and why

| Stage | Tool | Why, given the constraints |
|---|---|---|
| Resolve | `httpx.Client` + Pydantic model against Brønnøysundregisteret's public Enhetsregisteret API | Cheap, authoritative, structured, free, no auth. Spend request budget here first — a bad resolve poisons everything downstream. |
| Crawl | `httpx.Client` (primary), Playwright (optional, lazy fallback) | **Reconciled during implementation:** Scrapy was dropped in favor of using `httpx` directly for the static fetch — it's what the Concurrency model section below already specified, and running two separate HTTP stacks (Scrapy's own reactor + httpx) added complexity with no real benefit at this scale. Playwright is invoked only via an injected `render_js` callable when a page looks like a JS-shell (see `crawl.py`); it is not a hard dependency and its absence degrades gracefully (the static/shell content is kept as-is) rather than failing the company. |
| Extract | extruct (schema.org/JSON-LD/microdata) then Trafilatura (article text) | extruct captures data companies already publish in structured form — fastest and cheapest. Trafilatura is the fallback for unstructured prose. |
| Verify | RapidFuzz (fuzzy string matching) | Directly enforces the 95%-precision gate: a claim is only accepted if (a) it came from the entity's own verified official-site domain (provenance) and (b) for prose fields, the source page's company name matches the resolved legal name above a threshold. |
| Assemble | Pydantic models | Schema + state-enum enforcement at write time — a malformed profile cannot leave the pipeline. |
| Persist | Append-only snapshot store (SQLite for the HTTP cache, JSONL for profile snapshots, keyed by org_number + run timestamp) | Never overwrite — always add. Makes "idempotent refresh" structural rather than a rule to remember. |

## Concurrency model

Two different kinds of work need two different concurrency mechanisms:

- **Crawling is I/O-bound** (waiting on the network). Use `asyncio` + `httpx`
  (async HTTP client) so many requests can be in flight at once — like a waiter
  taking many tables' orders before any food comes back, instead of waiting at
  one table until they finish.
- **Extraction/parsing is CPU-bound** (actual computation). Use a
  `ProcessPoolExecutor` (a pool of worker processes) to actually use all 8 vCPUs,
  instead of one core doing everything while seven sit idle.

Both are wrapped in a **semaphore** (a counter capping how many operations run
at once) sized to respect the 2,000-request batch ceiling and to avoid hammering
any single target site.

**Implementation note:** `src/orchestrator/runner.py` bounds concurrency with
an `asyncio.Semaphore` as above, but each company's Resolve→Assemble chain is
itself synchronous (`httpx.Client`, not `AsyncClient`) so it can be tested with
`httpx.MockTransport` without an event loop. The runner dispatches each
company via `asyncio.to_thread`, which still gets the I/O-bound concurrency
benefit under the same semaphore cap — see the docstring in `runner.py`.

## The budget governor

A small, cross-cutting component (`src/orchestrator/budget.py`) that every stage
checks before doing expensive work: remaining requests, remaining dollars,
remaining wall-clock time. When a budget is nearly exhausted, the pipeline stops
pulling new data and moves straight to writing out whatever it has — correctly
flagged `not_available`/`blocked` — rather than crashing mid-batch. This is what
protects the "exactly 100 terminal results" hard gate: a graceful degrade that
emits 100 thinner profiles passes; a crash that emits 87 profiles fails outright.

## Error handling philosophy

Every network call: retry with backoff, capped, then classified into the state
enum (`blocked` / `failed` / `not_available`) rather than silently dropped. This
turns error handling into scoring evidence, not just log noise.

## Reliability / idempotency

Snapshots are append-only, keyed by `(org_number, run_timestamp)`. Refresh reads
the latest prior snapshot, diffs it against the freshly built profile, and
records a `material_changes` list — never mutates or deletes the prior row/file.
Mental model: git commits, not a file getting silently replaced.

## Observability

Structured, per-company logs at each stage boundary, tagged with timestamps.
This does double duty: it's both debugging output and the raw material for the
evidence log's `retrieved_at` fields, so instrument once, use twice.

## Deployment / reproducibility

One-command CLI entrypoint (`run_batch.py`), since the submission requires a
"one-command run instruction." Containerizing with Docker is recommended so the
declared 8 vCPU / 16 GB / 10 GB environment is reproducible rather than assumed.
