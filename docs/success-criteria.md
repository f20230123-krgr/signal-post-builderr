# Success Criteria — Definition of Done

Use this as a literal checklist. "Implemented" is not "done." Done means every
box below is checked against real (or fixture) numbers, not assumed.

## Hard gates (must ALL pass — any single failure fails the whole submission)

- [ ] Coverage score ≥ 21/35 (measured via `/self-check` against fixtures, and
      trusted as a proxy for the real daily batch)
- [ ] Weighted external company recall ≥ 60%
- [ ] External precision ≥ 95% — zero material wrong-company publications
- [ ] Exactly 100 terminal results produced for a 100-company batch, every time,
      including under simulated budget exhaustion and simulated network failures
- [ ] Zero fabricated financial values anywhere in the output
- [ ] Refresh is idempotent — running the same batch twice never deletes,
      overwrites, or duplicates a prior snapshot
- [ ] A 100-company batch completes within 45 minutes wall-clock, ≤2,000
      outbound requests, ≤$10 external spend, on the declared 8 vCPU / 16GB /
      10GB resource envelope

## Scoring rubric self-estimate (100 pts total, 65 to qualify)

- [ ] Coverage & source discovery (35 pts) — estimated via self-check harness
- [ ] Accuracy, identity & evidence (30 pts) — every claim has source +
      timestamp + period + state; entity resolution spot-checked
- [ ] Refresh & extensibility (20 pts) — idempotency test passes; diffing
      correctly identifies material changes without noise
- [ ] Decision-useful synthesis (10 pts) — a human can read one profile and
      understand the company and its evidence gaps in under a minute
- [ ] UX & interaction (5 pts) — profiles are legible/reviewable on desktop and
      mobile if a viewer surface exists; otherwise output format is at least clean/consistent

## Definition of Ready (before starting work on any component)

- [ ] The component's contract in `docs/component-specs.md` has been read
- [ ] Its dependencies (upstream stage outputs) are understood and match the
      `data-schema.md` types it will consume

## Definition of Done (before marking any task complete)

- [ ] Unit tests for the component pass, per `docs/testing-strategy.md`
- [ ] No test in the suite makes a live network call
- [ ] `/self-score` has been run and its numbers are quoted in the PR/summary
- [ ] `/guard-check` has been run and reports no gate violations or scope drift
- [ ] Docs updated if the implementation revealed the spec was wrong (fix the
      doc in the same change, don't let it silently go stale)

## Submission-readiness checklist (before emailing submit@builderr.ai)

- [ ] ≥1,000 completed profiles generated
- [ ] Repository URL and exact commit hash recorded
- [ ] One-command run instruction verified on a clean checkout
- [ ] Models/APIs/licenses used are documented
- [ ] Expected cost per 100-company batch calculated and under $10
- [ ] Organisation-number manifest URL prepared
- [ ] `signalpost-sources.md` and `signalpost-agent-playbook.md` have been
      obtained and reconciled against `docs/problem-statement.md`'s open items
