# Signalpost Agent

Autonomous research agent for the Builderr "Signalpost" challenge: turns a
bare Norwegian organization number into a sourced, verifiable company profile.

**Start here if you're a person:** `CLAUDE.md` at the repo root is the real
project brief -- it's written for Claude Code but is the clearest single
summary of what this project is and how it's organized. `docs/` has the full
detail behind it. `starter-briefs/` holds the real Builderr documents
(challenge brief, source policy, agent playbook, learning harness, evaluation
contract) fetched from builderr.ai/challenges/signalpost.

**Start here if you're Claude Code:** `CLAUDE.md` is auto-loaded already --
read it before making any change.

## Quick start

```bash
pip install -r requirements.txt
python -m pytest tests/                                # 82 tests, all green, no live network
curl -LO https://builderr.ai/signalpost-company-universe-2025.jsonl.gz  # optional but recommended
python -m src.run_batch --input batch.jsonl --out results/
python -m src.scoring.self_check --fixtures fixtures/   # coverage/recall/precision vs hard gates
```

`batch.jsonl` is one Norwegian organization number per line, e.g.:

```
923609016
997770234
```

`run_batch` writes into `--out`:
- `envelopes.jsonl` -- one submission envelope per input, matching the
  reference agent's `OUTPUT_CONTRACT.md` shape (`claims`/`evidence`/`run`/
  `changes`/`errors`/`operations`) -- this is the file to submit
- `manifest.txt` -- the exact organisation-number manifest processed
- `run-report.json` -- machine-readable requests/spend/runtime totals
- `snapshots/` -- append-only `CompanyProfile` history (idempotent refresh)

If `signalpost-company-universe-2025.jsonl.gz` is present in the working
directory (or passed via `--universe`), identity resolution for any covered
org number is free and network-free (Builderr's own frozen, hash-verified
manifest) -- see `src/pipeline/universe.py`.

## Status

All pipeline stages (resolve/universe/crawl/extract/verify/registry_extras/
assemble/envelope), both storage components (cache, snapshots), the
orchestrator (budget governor + async runner), and the self-check scoring
harness are implemented and covered by unit + integration tests (built
test-first: every module has a red-then-green history in the tests it ships
with).

This was built in two passes. The first implemented every stage against this
repo's own `docs/` interpretation of the public problem statement. The second
followed up by fetching and reading every document actually linked from
builderr.ai/challenges/signalpost (the real source policy, agent playbook,
learning harness, evaluation contract, the runnable reference agent's code,
and the 411,160-company universe file -- both published SHA-256 hashes
verified against the downloaded bytes) and reconciled several real gaps found
there:

- **Output format**: the reference agent's `OUTPUT_CONTRACT.md` defines a flat
  `claims`+`evidence` envelope, not this repo's nested `CompanyProfile` --
  `src/pipeline/envelope.py` is the export layer that converts one to the
  other, since that's almost certainly what an automated evaluator parses.
- **Free, reproducible identity**: the frozen universe file lets `resolve()`
  skip the live registry call entirely for any covered org number --
  zero request-budget cost, byte-identical for every entrant.
- **Evidence completeness**: `Claim` gained `content_hash`, `source_class`,
  `extraction_method`, and `match_confidence`, per the real source policy's
  "every claim records ... content hash and extraction method."
- **More official-registry coverage**: `src/pipeline/registry_extras.py`
  pulls leadership roles, filed annual accounts, and registered workplaces
  from Brreg's own `roller`/`regnskapsregisteret`/`underenheter` endpoints --
  same trust tier as `resolve.py`, no source-policy ambiguity.
- **Same-domain company pages**: `crawl()` can now also fetch `/about`,
  `/careers`, `/contact`, `/news`, `/investor`, etc. on the entity's own
  domain (opt-in via `company_owned_paths`) -- explicitly prioritised by both
  the agent playbook and the learning harness.
- **Honest HTTP status handling**: a 404/403/5xx response is no longer
  silently treated as a successful fetch.
- **Pinned dependencies**, per the "reproducible setup" hard gate.
- Deliberately **not** pursued: `signalpost-sources.md`'s own restrictions
  rule out scraping LinkedIn/proff.no/purehelp.no directly (checked their
  actual `robots.txt`/access behavior -- purehelp.no explicitly disallows the
  exact pages needed, proff.no returns 403 even on `robots.txt`). NAV's job
  board (`arbeidsplassen.nav.no`) has an open `robots.txt` and would be a
  reasonable next connector, but its search API is client-side only and
  couldn't be reverse-engineered from this environment without a browser.

On the self-check harness's small hand-labeled fixture sample (4 real
companies -- see `fixtures/expected/README.md` for why it's smaller than the
recommended 10-30), all three hard gates pass: coverage 34.45/35, weighted
company recall 100%, external precision 100%.

Use `/self-score` to re-run the harness and quote fresh numbers, and
`/guard-check` before any PR to confirm no hard gate has regressed.

## Submitting

Per `starter-briefs/signalpost.md`: email `submit@builderr.ai` with the
**repository URL and exact commit hash** (not a zip -- up to 4 revisions are
allowed before the deadline, each a new commit hash), completed-profile
count (>=1,000), the organisation-number manifest (`manifest.txt`), the
one-command run instruction above, models/APIs/licences used, and expected
cost per 100-company batch.
