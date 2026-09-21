# Success Criteria — Definition of Done

Use this as a literal checklist. "Implemented" is not "done." Done means every
box below is checked against real (or fixture) numbers, not assumed.

## Official-run checks and our standards (revised 2026-09)

Builderr no longer has numeric qualification gates: an entry qualifies with an
official run and 65/100, and coverage / recall / precision only contribute to
the score. What can stop a run from being official, or cost a whole batch, is
below; treat every box as must-pass.

- [ ] No material wrong-company publication (blocks an official run; first
      tie-breaker). We keep >= 95% external precision as our own self-check target.
- [ ] Exactly 100 terminal results produced for a 100-company batch, every time,
      including under simulated budget exhaustion and simulated network failures
      (a failed or missed batch scores zero)
- [ ] Zero fabricated financial values anywhere in the output
- [ ] Refresh is idempotent -- running the same batch twice never deletes,
      overwrites, or duplicates a prior snapshot
- [ ] A 100-company batch completes within 45 minutes wall-clock, <= 2,000
      outbound requests **including redirects and retries** (measured with the
      real wire counter, see `run-report.json` `outbound_requests_measured`),
      <= $10 declared external spend, on the declared 8 vCPU / 16GB / 10GB envelope
- [ ] Source rights, secrets and outbound URL policy are documented and safe
      (README "Requests, secrets, caches and outbound URLs")
- [ ] Self-check targets (not gates): coverage >= 21/35, weighted company
      recall >= 60%

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
