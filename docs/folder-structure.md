# Folder Structure

```
signalpost-agent/
  CLAUDE.md                  # persistent memory, auto-loaded — read first, always
  docs/
    problem-statement.md     # verbatim frozen requirements — source of truth
    approach.md               # why the pipeline is shaped this way
    architecture.md            # tech stack + concurrency + reliability reasoning
    folder-structure.md        # this file
    component-specs.md         # per-module contract: inputs, outputs, must-not-do
    data-schema.md             # the profile schema + evidence state enum
    testing-strategy.md        # what gets tested, how, and with what fixtures
    success-criteria.md        # hard gates + rubric as a literal checklist
  .claude/
    commands/
      self-score.md           # /self-score — run local scoring harness
      add-stage.md             # /add-stage <name> — scaffold a new stage + test
      guard-check.md           # /guard-check — review a diff against the gates
    settings.json              # starter permission scaffold (adjust as needed)
  src/
    pipeline/
      resolve.py               # org number -> verified entity
      universe.py               # loads Builderr's frozen company-universe manifest
                                  # for free, byte-identical identity anchoring
      selection.py               # deterministic N-company sample from the universe
      crawl.py                 # entity -> raw pages (httpx + optional Playwright fallback)
      extract.py                # raw pages -> raw facts (extruct + Trafilatura)
      verify.py                 # raw facts -> confirmed facts (RapidFuzz gate)
      registry_extras.py         # entity -> confirmed facts, straight from Brreg's
                                  # roller/regnskapsregisteret/underenheter endpoints
      assemble.py                # confirmed facts -> validated profile (Pydantic)
      envelope.py                 # CompanyProfile -> submission envelope
                                  # (claims/evidence/run/changes/errors/operations)
    storage/
      cache.py                  # durable HTTP cache (cache hits are free per budget)
      snapshots.py               # append-only profile store + diff-based refresh
    orchestrator/
      budget.py                  # request/cost/time governor, checked by every stage
      runner.py                   # async task pool wiring stages together end to end
    models/
      profile.py                  # Pydantic schema: 7 sections + evidence state enum
    scoring/
      self_check.py                # local recall/precision/coverage estimator
    run_batch.py                    # one-command CLI entrypoint
    select_entry_batch.py            # picks the >=1,000-company submission corpus
  tests/
    pipeline/                       # one test module per pipeline stage
    storage/                         # cache + snapshot/idempotency tests
    orchestrator/                    # budget governor + runner concurrency tests
    scoring/                          # self-check harness tests
  fixtures/                            # recorded/labeled sample data — no live network in tests
```

## One-line purpose per top-level item

- `docs/` is the source of truth. If code and docs disagree, reconcile the docs
  first, then change code — never let them silently drift apart.
- `src/pipeline/` is the assembly line. Each file is one station; none of them
  import each other's internals, only their declared input/output types.
- `src/storage/` is where reliability lives — cache to respect the request
  budget, snapshots to make refresh idempotent by construction.
- `src/orchestrator/` is where the resource constraints (time/requests/$) become
  enforced code rather than a doc everyone has to remember.
- `src/models/` is the contract enforcement layer — nothing malformed leaves the
  pipeline.
- `src/scoring/` is how we grade ourselves before Builderr grades us.
- `fixtures/` exists so tests never depend on live network calls, which would
  make CI flaky and burn real request/dollar budget for no reason.
