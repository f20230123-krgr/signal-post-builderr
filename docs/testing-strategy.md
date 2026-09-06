# Testing Strategy

## Guiding rule

No live network calls in the test suite. Every test runs against recorded
fixtures in `fixtures/`. This keeps CI fast, deterministic, and free — and it
means test runs never eat into the real batch's request/dollar budget.

## Layers of testing

### 1. Unit tests per stage (`tests/pipeline/`)

Each stage in `src/pipeline/` gets its own test module, using the specific
cases listed in `component-specs.md` for that file (e.g. `resolve.py`'s tests
cover exact-match, no-match, ambiguous-match, and timeout scenarios). These
should run in milliseconds — mock the network boundary, not the logic.

### 2. Storage/reliability tests (`tests/storage/`)

- **Idempotency test:** run `snapshots.py` twice against the same input and
  assert two snapshots exist, the first is untouched, and the second correctly
  references the first as `previous_run_timestamp`.
- **Cache correctness test:** assert a cache hit never decrements the request
  budget counter, and a cache miss does.

### 3. Orchestrator tests (`tests/orchestrator/`)

- **Budget exhaustion test:** simulate a batch where the request budget runs
  out at company 60 of 100; assert the batch still emits exactly 100 profiles,
  with companies 61-100 correctly marked with a `blocked`/`not_available` state
  rather than missing entirely.
- **Concurrency cap test:** assert the semaphore actually limits in-flight
  requests to the configured maximum under simulated load.

### 4. Integration test (small, end-to-end)

Run the full pipeline (resolve → crawl → extract → verify → assemble →
persist) against ~5 fixture companies with recorded HTML pages, and assert:
- Exactly 5 valid `CompanyProfile` records are produced.
- Every claim in every profile has a valid state.
- No profile exceeds the declared time/request budget for that mini-batch.

### 5. Self-scoring harness (`tests/scoring/`, backed by `src/scoring/self_check.py`)

This is the most important test from a "will we pass" standpoint. Maintain a
small hand-labeled set of companies (10-30) with manually verified correct
values. `self_check.py` runs the pipeline against them and reports:

- **Weighted company recall** — did we find and correctly attach the right
  company at all, weighted per the 70/30 split described in `problem-statement.md`
- **External precision** — of what we published, what fraction was actually
  the correct company (must stay ≥95% in every self-check run before submitting)
- **Coverage score** — approximate 0-35 scale per the rubric

Treat any self-check run below the hard-gate thresholds as a blocking failure —
do not submit, and do not mark a related task "done" in `CLAUDE.md`'s sense of
the word.

### 6. What counts as a "material change" for refresh diffing

Define explicitly (avoid noisy false positives): a change is material if it
affects `legal_name`, `leadership`, `official_site`, `annual_accounts.latest`,
or adds/removes a `hiring_signal`. Whitespace/formatting differences in scraped
text, or a re-fetch of an unchanged page with a new `retrieved_at` timestamp,
are not material changes on their own.

## Fixtures policy

`fixtures/` holds: 2-3 raw HTML snapshots per test company (official site +
one secondary source), the expected `ResolvedEntity` for each, and the
hand-labeled expected profile for the self-check set. Update fixtures
deliberately, not automatically from live crawls, so tests stay reproducible.
