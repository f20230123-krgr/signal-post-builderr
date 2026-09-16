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
python -m pytest tests/                                # all green, no live network
curl -LO https://builderr.ai/signalpost-company-universe-2025.jsonl.gz  # optional but recommended
export EXA_API_KEY=... PARALLEL_API_KEY=...            # your own keys -- see "API keys and cost"
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
  exact pages needed, proff.no returns 403 even on `robots.txt`).

A third pass (September 2026) focused on coverage and precision:

- **Hiring signals from NAV's official job-vacancy feed** (a public feed for
  outside developers, with a published access token). An ad is attributed only
  when its employer org number is the company's own or one of its registered
  sub-units; only the job title and dates are published, never contact details.
- **Free registry identity facts**: industry, legal form, employee count and
  operating status from the universe manifest (zero requests), plus founding
  date and former names from the live registry record.
- **Dated activity for every company**, including the majority with no
  website: registry change events, founding and renaming dates, JSON-LD events
  and declared RSS/Atom feeds.
- **Workplaces for every company** (its own registered location when it has no
  sub-units) and **company-owned social profiles** read from site markup.
- **Precision filters on website search**: aggregator and directory pages,
  directory-shaped URLs, foreign lookalike domains, shared venues, pages that
  publish a different company's org number, and short near-miss names are all
  rejected. A page that publishes the company's own org number is accepted
  even under an unrelated brand name.
- **A fourth search round** using the company's most recent former name, only
  when no other registered company holds that name.
- **Graceful key failure**: a dead or exhausted search key is detected at
  startup and mid-run, switched off for the rest of the run, and reported;
  search results are cached per day and search spend is recorded.

On the self-check harness's fixture sample (10 companies: 4 hand-verified
with websites, plus 6 with no website on file to represent the ~89% majority
case), all three hard gates pass: coverage 34.56/35, weighted company recall
100%, external precision 100%.

Use `/self-score` to re-run the harness and quote fresh numbers, and
`/guard-check` before any PR to confirm no hard gate has regressed.

## API keys and cost

The agent reads its API keys **only** from environment variables. No key is
stored in this repository. Supply your own:

| Variable | Used for | Needed? |
|---|---|---|
| `EXA_API_KEY` | Finding the official website of companies with none on file in the registry (paid, [$7 per 1,000 searches](https://exa.ai/pricing)) | Recommended: largest effect on website coverage |
| `PARALLEL_API_KEY` | The same website search, as a fallback (free tier) | Recommended |

Everything else is free and needs no key: the Brønnøysund registry, NAV's
public job-vacancy feed (its access token is published by NAV and fetched at
run time), and each company's own website.

**Without keys**, the run still completes and every profile is valid, but a
website is only found for companies that registered one (~11% of the
universe), and every field that depends on the website is thinner.

**Expected cost per 100-company batch (default command): up to about $2.40**
of Exa searches, well under the $10-per-batch limit. Each company without a
registered website gets up to four search rounds (company name, CEO name, org
number, former name), stopping as soon as a verified website is found, so the
real figure is usually lower. For reference, the submitted corpus below, with
Exa limited to the first round, measured **$0.56 per 100 companies**. The exact
figure for any run is written to `run-report.json` as `spend_used_usd`.
The 2,000-request, 45-minute and $10-per-batch limits are enforced in code on
every run.

**If a key runs out of credits mid-run**, the provider is switched off for the
rest of the run, the run continues on the remaining providers, and the
problem is printed at the start and end of the run and recorded in
`run-report.json` under `degraded_providers`.

### Limiting your own spend (optional, off by default)

- `--exa-first-round-only` uses Exa only for the company-name search; the
  fallback rounds use the free providers. Roughly one paid search per company.
- `--max-search-spend USD` puts a hard cap on paid search spend across the
  whole run. When reached, Exa is switched off and the run continues.

The default one-command run uses **neither**.

### How the submitted profile corpus was generated

To limit the author's own spend, the submitted 1,000-company corpus was
generated with:

```
python -m src.run_batch --input entry-companies.jsonl --out results/ --chunk-size 100 --exa-first-round-only --max-search-spend 10
```

That uses Exa for the company-name search only, so the corpus has somewhat
lower website coverage than the default command produces. Measured result:

| | |
|---|---|
| Profiles | 1,000 / 1,000 |
| Search spend | $5.64 in total (the $10 cap was never reached) |
| Requests | 14,450 across 10 batches of 100 (~1,445 per batch, limit 2,000) |
| Runtime | ~4.3 minutes per 100 companies of active processing; the recorded `wall_clock_seconds` also includes a ~29-minute pause while the machine slept |
| Provider failures | none |
| Available claims | 15,346 (legal name, industry, legal form and operating status for all 1,000; dated activity and workplaces for all 1,000; founding date 989; leaders 990; annual accounts 997; official website 293; company profiles 123; public brand 125; hiring signals 2) |

The default one-command run uses Exa on every search round and has no
run-wide spend cap.

## Submitting

Per `starter-briefs/signalpost.md`: email `submit@builderr.ai` with the
**repository URL and exact commit hash** (not a zip -- up to 4 revisions are
allowed before the deadline, each a new commit hash), completed-profile
count (>=1,000), the organisation-number manifest (`manifest.txt`), the
one-command run instruction above, models/APIs/licences used, and expected
cost per 100-company batch.
