# Approach

## Mental model

Treat this as an assembly line, not a monolith: each company is a "part" moving
through fixed stations (Resolve → Crawl → Extract → Verify → Assemble → Persist).
Every station has one job, a clear input, and a clear output, so any one station
can be tested and improved without touching the others.

One sentence to hold onto for every decision: **"Find the right company, say
only what you can cite, flag what you can't find, and never let a re-run lose
history."**

## Why this order, specifically

1. **Resolve first, always.** A wrong resolve poisons every downstream claim —
   this is the single biggest lever on the 95%-precision hard gate. No crawling
   starts until the entity is confirmed against the official registry.
2. **Crawl within budget, not exhaustively.** With ~20 requests and ~$0.10 per
   company (from the 2,000-request / $10 batch budget), the crawler must be
   selective: official site and a small number of permitted secondary sources,
   not "download everything that mentions this name."
3. **Extract cheaply before extracting expensively.** Prefer structured data
   already on the page (schema.org/JSON-LD tags) over full-page rendering with a
   headless browser — the latter costs far more CPU/RAM and should be a fallback,
   not the default path.
4. **Verify before trusting.** Every extracted fact is fuzzy-matched back against
   the resolved legal name before being accepted as a claim. This is the
   mechanism that actually enforces the precision gate in code, not just in principle.
5. **Assemble with validation, not hope.** The output schema (see
   `data-schema.md`) is enforced at write time — a malformed or under-evidenced
   profile cannot reach the output file.
6. **Persist as history, not state.** Every run writes a new snapshot; nothing is
   overwritten. This makes "idempotent refresh" a property of the storage layer,
   not something each stage has to remember to respect.

## Two failure modes we are explicitly designing against

- **Silent overreach:** a stage that keeps retrying, keeps crawling, or keeps
  spending after the budget is gone. Countered by a budget governor every stage
  checks in with (see `architecture.md`).
- **Silent gap-filling:** a stage that guesses or fabricates a value instead of
  reporting `not_available` / `blocked` / `ambiguous`. Countered by making the
  state enum a required field, not an optional annotation — there is no way to
  emit a claim without picking a state.

## What "clean" means for this project specifically

Clean is not "elegant abstractions." Clean here means: every hard gate has a
traceable, testable piece of code responsible for it, and the fastest possible
answer to "did we just break the 95% precision gate?" is always "run
`/self-score` and look." Optimize for that feedback loop above almost anything else.
