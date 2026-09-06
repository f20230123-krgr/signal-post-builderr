# Signalpost Agent — Project Memory

This file is auto-loaded at the start of every Claude Code session in this repo.
Read it fully before touching any code. It exists so that no session — yours or a
future one — drifts away from what this project is actually for.

## Prime directive

We are building an autonomous agent that turns a bare Norwegian organization number
into a **sourced, verifiable company profile**, at scale, inside hard resource limits,
for the Builderr "Signalpost" challenge. Every design and code decision must trace
back to one of these four phases. If a change doesn't serve one of them, it doesn't
belong in this repo:

1. **Resolve** — org number → verified legal name + official site
2. **Crawl** — permitted public sources only
3. **Prove** — every claim carries source, retrieval time, reporting period, state
4. **Refresh** — re-runs update profiles without ever losing prior history

Full verbatim problem statement: @docs/problem-statement.md

## Non-negotiable hard gates (read before every PR / before saying "done")

These are pass/fail, not partial credit. A change that improves one metric while
silently breaking another is a regression, not progress.

- Coverage score ≥ 21/35
- Weighted external company recall ≥ 60%
- External precision ≥ 95% (never publish a claim on the wrong company)
- Exactly 100 terminal results per daily 100-company batch — not 99, not 101
- Zero fabricated financial values, ever
- Refresh is idempotent: prior snapshots are never overwritten or deleted
- Batch completes within 45 min wall-clock, ≤2,000 outbound requests, ≤$10 spend

Full scoring rubric and definition of done: @docs/success-criteria.md

## How this repo is organized

- `docs/` — the persistent memory: problem statement, approach, architecture,
  component contracts, data schema, test strategy, success criteria. This is the
  source of truth. Code should conform to docs, not the other way around — if code
  and docs disagree, stop and reconcile before continuing.
- `src/pipeline/` — the five per-company stages: resolve, crawl, extract, verify, assemble
- `src/storage/` — durable cache + append-only snapshot store
- `src/orchestrator/` — budget governor + async runner that wires stages together
- `src/models/` — Pydantic schema for the profile output + evidence state enum
- `src/scoring/` — local self-check harness (estimate recall/precision before submitting)
- `tests/` — mirrors `src/`, one test module per component
- `fixtures/` — recorded/labeled sample data for offline tests (no live network in tests)

Full layout with one-line purpose per file: @docs/folder-structure.md

## Working agreement for coding sessions

- Before implementing a component, read its contract in `docs/component-specs.md`.
  Do not invent a different input/output shape — if the contract seems wrong, flag
  it and propose an update to the doc first, then implement.
- Every stage function must be testable in isolation with mocked inputs. No stage
  should reach across to another stage's internals.
- Never let a stage crash the batch. Unrecoverable errors become a state value
  (`blocked` / `failed` / `not_available`) attached to that one company, and the
  batch continues. A batch that produces fewer than 100 terminal results is a
  bug, not an acceptable outcome.
- Any new external request path must be checked against the budget governor
  (`src/orchestrator/budget.py`) before it ships — request count and spend are
  gated resources, not just nice-to-track metrics.
- Snapshots are append-only. Never write code that mutates or deletes a prior
  snapshot file/row. "Refresh" means "write a new version and diff against the
  last one," never "overwrite."
- Before marking any task done, run (or ask the user to run) `/self-score` and
  quote the resulting recall/precision/coverage numbers. "I implemented it" is
  not done; "it clears the gates on the fixture sample" is done.
- If you notice the implementation solving a more interesting/general problem
  than the one in `docs/problem-statement.md`, stop. Flag the scope creep instead
  of continuing — this repo has one job.

## Useful custom commands

- `/self-score` — run the local scoring harness against `fixtures/` and report
  recall, precision, and coverage against the thresholds above.
- `/add-stage <name>` — scaffold a new pipeline stage + matching test file that
  follows the contract shape in `docs/component-specs.md`.
- `/guard-check` — re-read the hard gates and problem statement, then review the
  current diff for anything that violates a gate or drifts from the four phases.

## Source of truth for everything else

Approach and rationale: @docs/approach.md
Architecture and tech stack reasoning: @docs/architecture.md
Data/evidence schema: @docs/data-schema.md
Test strategy: @docs/testing-strategy.md
