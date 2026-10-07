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
curl -LO https://builderr.ai/signalpost-company-universe-2025.jsonl.gz  # optional: fetched automatically if missing
python -m src.run_batch --input batch.jsonl --out results/   # THE one command: no keys, no browser, $0
python -m src.scoring.self_check --fixtures fixtures/   # coverage/recall/precision vs our targets
```

The run needs no API key, no model and no browser. Two things are optional and off unless
you ask for them: a headless browser for pages whose content is drawn by script (`--browser`,
needs `pip install playwright==1.55.0 && python -m playwright install chromium`), and search
keys (`EXA_API_KEY`, `PARALLEL_API_KEY`), used only if set -- see "Cost and optional keys".

`batch.jsonl` is one Norwegian organization number per line, e.g.:

```
923609016
997770234
```

`run_batch` writes into `--out`:
- `envelopes.jsonl` -- one submission envelope per input, matching the
  reference agent's `OUTPUT_CONTRACT.md` shape (`claims`/`evidence`/`run`/
  `changes`/`errors`/`operations`) plus a `summary` -- this is the file to submit
- `report.html` -- **Signalpost Atlas**, a self-contained viewer of those same
  envelopes (open it in any browser, no server): search and filter the
  companies, compare up to four side by side, and press *Evidence* on any fact
  to see its source link, retrieval time, reporting period, content hash and
  the exact quoted text. Works on desktop and phone, light and dark.
- `manifest.txt` -- the exact organisation-number manifest processed
- `run-report.json` -- machine-readable requests/spend/runtime totals, and which
  optional search keys the run had (`search_keys`)
- `snapshots/` -- append-only `CompanyProfile` history (idempotent refresh)

**The summary.** Each envelope's `summary` is one short, dated narrative: what
the company is and what it registered that it does (quoted), how its latest filed
year went (revenue trend, profit or loss and margin, equity, all computed from the
cited filings), who runs it, where it operates, its web presence, hiring and latest
dated news, what changed since the previous run, and what could not be found, plus a
one-line `headline`. Every sentence lists the
claim fields it rests on and the ids of the evidence records that support it;
a gap is named under `unknowns`, never guessed or turned into zero.

```json
{"as_of": "2026-10-07", "headline": "ELOPAK ASA: active, revenue 753.0 m EUR (FY2025, up 7%)",
 "text": "As of 2026-10-07: ELOPAK ASA (org. no. 811413682) is a public limited company (ASA) in manufacture of paper and paperboard ... In FY2025 (year to 2025-12-31), revenue grew 6.6% on FY2024 to 753.0 m EUR and the company made a net profit of 65.5 m EUR (8.7% of revenue), turning round from a loss of 465 k EUR in FY2024 ...",
 "sentences": [{"text": "...", "fields": ["legal_name", "..."], "evidence_ids": ["ev-1", "ev-4"]}],
 "changes": [], "unknowns": ["hiring signals"]}
```

**No keys.** Builderr's official runs use the credential-free public-source path and never
participant-owned keys, so the agent is built and measured for exactly that: the registry,
company-owned sites and free public sources. Without keys it prints one informational line
and carries on; it never prompts or waits for input.

If `signalpost-company-universe-2025.jsonl.gz` is present in the working
directory (or passed via `--universe`), identity resolution for any covered
org number is free and network-free (Builderr's own frozen, hash-verified
manifest) -- see `src/pipeline/universe.py`.

The file is not stored in this repository. If it is missing, the run downloads
it once from `builderr.ai` (one request), keeps it only if its SHA-256 matches
the hash Builderr publishes, and saves it next to where you ran the command. If
the download fails, the run carries on with live registry lookups: identity,
industry, legal form, employee count and operating status then come from the
live Brønnøysund record instead (same fields, same source class).

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
- **Pinned dependencies**, per the "reproducible setup" official-run check.
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

**Synthesis (earlier passes).** The first submission carried no synthesis in its envelopes;
the second added templated answers with sources; since October each envelope carries one
short dated `summary` instead (below and in "The summary" above), and the `answers` list
is gone.

**Social links (September).** Profile links are read from anchors, `data-href` attributes,
embed widgets and JSON-LD `sameAs`, all through the same profile rules
(`src/pipeline/social.py`).

A fourth pass (October 2026) followed Builderr's per-family coverage feedback on the
third submission (websites 4.5%, hiring 0.0%, social 72.7%, dated news 46.8%; "turn the
facts into a shorter dated summary instead of a list"). Every external family starts from
the company's own site, and the scored run cannot be assumed to have search keys, so the
work is keyless first:

- **Hiring signals (was 0%).** The crawler reads the careers link a company's own pages
  already contain (never a guessed path; same site, its subdomains or a known
  applicant-tracking platform). It publishes "Careers page lists N open roles, e.g. ..."
  when the page's HTML lists job-detail links, otherwise "Careers page: <url>", which says
  the page exists and nothing about openings. No-openings pages, error pages, footer links
  and general job boards publish nothing. On 100 real companies with sites, 0 -> 10 with a
  hiring claim.
- **Website evidence.** A registered website used to be cited to the registry snapshot with
  the URL repeated as its span. It is now sourced from the company's own page (hash,
  retrieval time, the org-number line or the page's own name for the company), at the
  address the site resolves to.
- **Keyless website discovery.** For companies with no registered website: NAV's employer
  homepage, the registered e-mail's own domain, and a few name-derived domains that resolve
  in DNS. Every candidate passes the same identity gate as a search hit, which now also
  accepts a page showing the registered phone or street address; a name-derived guess
  additionally needs proof (org number, phone, address, or registered postcode and town with
  the name). 100 random companies with no registered website: 9 sites found, all correct.
- **Dated news.** Norwegian and English date formats and `<time datetime>`, each date paired
  with its headline ("Headline (YYYY-MM-DD)", the headline as the span), plus the site's own
  news link. Companies with a site-sourced news claim: 8 -> 23 of 100.
- **Financial figures.** Revenue, operating result, result before tax, annual result, assets,
  equity and debt per filed year (newest two), each with its period, period end date and the
  filing's own text; a link to the filed accounts for years the registry lists. A figure the
  filing does not state is omitted, never zero.
- **Social profiles.** Only a company's own profile: no staff members' personal LinkedIn pages,
  Instagram posts, share links or videos; tracking parameters stripped; one claim per profile.
- **Roles and workplaces.** Roles carry the registry's last-changed date and the holder's name
  as written; workplaces carry their full registered address.
- **Websites from the live registry record.** The live Brønnøysund record's `hjemmeside`
  is read for every company (the universe file lists a website for far fewer). Against
  Builderr's own 100-company reference sample, the keyless run matches 77 of the 97
  registered domains, with 6 more being the same company under another domain.
- **Crawling the site's own links.** Contact and about pages, careers and news are reached
  through links the site itself publishes (and its sitemap, careers and news first), never
  by guessing paths; WordPress sites' own post list gives dated news; social profiles a
  site ships as script data are accepted only when the account name resembles the company.
  Requests carry an identifiable crawler name with a link to this repository.

After the official feedback of 2026-10-06 (websites 4.0%, hiring 0.0%; "explain the
financial trend instead of listing values; show the period behind ... claims"):

- **The financial trend in words.** The summary says how revenue moved on the previous
  filing, whether the company made a profit or a loss and its share of revenue, whether that
  is a turnaround or a swing into loss, and which way equity moved. Every figure and
  percentage is computed only from the two cited filings, and a test checks each one.
- **Dates on every figure.** The head count carries the date the registry recorded it, news
  items their publication date, accounts their period and period end; an upcoming event is
  never called the latest news.
- **One record per fact.** One claim per social profile however the site spells the link,
  one per careers page across language copies; a single job ad is published as the role
  (title and posting date) and never labelled a careers page; HTML entities in job titles
  read as plain text while the span keeps the page's own text.

Final pass for the fourth revision (2026-10-07):

- **Every registry role.** Deputy board members, auditor, accountant, partners and business
  manager are published as well as management and the board, as Builderr's own reference data
  does (a median of six roles per company); an organisation holding a role carries its
  organisation number. The summary's "Led by" names only people who run the company.
- **Spans a checker can find.** Every registry claim quotes the exact text of the response it
  cites (an address object, a filing's figure, an update's date and type, a JSON key and
  value), checked by a test against the served text. Identity claims cite the live registry
  record, an address a reader can open, whenever it was read.
- **What the company does.** The registry's own description of the company's activity (or its
  statutory purpose) is published and quoted in the summary; the industry reads in English
  using Statistics Norway's official labels (shipped as `src/data/industry_en.json`).
- **Only the company's own social profiles.** A named account must resemble the company's
  name or domain, so a parent group's account, an owner's personal brand or a supplier's
  channel is not published as the company's; Facebook feed posts are not profiles. Tested
  against the 76 profiles Builderr's own crawl confirmed: none removed.

On the self-check harness's fixture sample (10 companies: 4 hand-verified
with websites, plus 6 with no website on file to represent the ~89% majority
case), all three of our self-check targets are met: coverage 34.56/35, weighted
company recall 100%, external precision 100%.

Use `/self-score` to re-run the harness and quote fresh numbers, and
`/guard-check` before any PR to confirm no standard has regressed.

## Cost and optional keys

**Expected cost per official run: $0.** No model, no paid API and no key is used.
Builderr's official runs use the credential-free public-source path, and this agent is
built and measured for exactly that: the Brønnøysund registry, NAV's public job-vacancy
feed (its access token is published by NAV and fetched at run time) and each company's
own website.

**Measured on 2026-10-06** (default command, no keys, no browser, universe file present;
two sets of 100 companies drawn at random from the universe, never seen by the agent
before; real outbound requests, redirect hops and retries included):

| Run | Companies | Real requests | Wall clock | Errors |
|---|---|---|---|---|
| Random set A, default (10 at a time) | 100 / 100 | 934 | 290 s | 0 |
| Random set B, `--concurrency 20` | 100 / 100 | 907 | 260 s | 0 |
| Builderr's 100-company reference sample (larger sites) | 100 / 100 | 1,563 | 360 s | 0 |

So a 1,500-company official run takes roughly 75 minutes at the default, and every
100-company chunk stays far under its 2,000-request and 45-minute limits. The agent starts
skipping optional extras at 1,800 requests and stops fetching at 1,940, so a heavy chunk
degrades instead of overshooting. Raising `--concurrency` buys little (the slowest sites
set the pace) and adds load on the registry, so the default stays at 10.

**Optional search keys.** If `EXA_API_KEY` or `PARALLEL_API_KEY` is set, it is used after
the keyless candidates to find a website for companies whose registry record has none;
every candidate still has to pass the same identity check. They are never needed, and an
official run does not have them. A dead or exhausted key is detected at startup and
mid-run, switched off for the rest of the run, and reported in `run-report.json` under
`degraded_providers`; `--exa-first-round-only` and `--max-search-spend USD` limit your own
spend when you do use one.

### Requests, secrets, caches and outbound URLs

**Request limit.** Builderr's limit is 2,000 outbound requests per run,
*including redirects and retries*. Every client the agent builds goes through
`src/pipeline/net.py`, which counts each request that is actually sent, redirect
hops and retries included, and the budget governor enforces that real number
(not one per page). The agent starts degrading (skipping optional extras) at 90%
and stops fetching 60 requests short of 2,000, so requests already in flight
when it stops cannot push the run over. `run-report.json` records
`requests_used` (real count, startup requests included) and
`outbound_requests_measured` (an independent count of everything sent).

**Secrets.** The optional keys are read from the environment variables named above
and nowhere else. They are never written to `run-report.json`, envelopes,
snapshots, caches or logs, and no key is stored in this repository.

**Caches.** The only caches are: (1) `cache.sqlite3` in the `--out` directory,
created empty for each output directory and never shipped, holding fetched
public web pages and search-API responses keyed by URL, request and date (never
a key); and (2) the company-universe file described above, a public Builderr
file verified against its published SHA-256. Nothing else is cached or
pre-populated.

**Outbound URL policy.** The agent follows URLs it finds on third-party pages,
so every request is checked before it is sent, redirect hops included: only
`http` and `https` are allowed, and a URL is refused if its host is
`localhost`, an internal-looking name (`.local`, `.internal`, ...), a
private, loopback, link-local (including the cloud metadata address) or
carrier-grade-NAT IP address, or a name that resolves to one. A refused request
is not sent and not counted, and only costs that one fetch. Known limit: the
name is resolved once for the check and again by the HTTP client, so a hostile
DNS server could in principle answer differently the second time.

### The 1,000-company corpus

Builderr scores the agent on companies it supplies at run time, not on precomputed
profiles, so the corpus demonstrates scale; it is not a submission requirement. The hosted
corpus (`corpus/` on the site, with its raw envelopes, run report and manifest to download)
was generated keyless on 2026-10-07 by the submitted code with
`python -m src.run_batch --input results/manifest.txt --out <dir>`: 1,000 / 1,000 profiles,
8,720 real requests across 10 chunks, 42.5 minutes, $0; output audit clean. Websites 161,
company-owned social profiles 62, hiring signals 15, site news 60, registered activity 973,
roles for 997 companies, workplaces for all 1,000 (most companies in a random sample have no
website). `results/` in this repository is the earlier 2026-10-04 run of the same companies,
kept unchanged as history.

## Submitting

Per Builderr's current submission template: email `submit@builderr.ai` with the agent name,
repository URL, exact commit hash, a 100-company smoke-test result or report URL, the one
command (`python -m src.run_batch --input batch.jsonl --out results/`), models / APIs /
licences (`LIMITATIONS.md`: no model; free public sources; no keys), expected cost per
official run ($0) and a contact for results.

The hosted viewer is built from finished runs with
`python -m src.build_site --corpus results --smoke <smoke-run-dir> --out site` and served
from the `gh-pages` branch: the smoke test at the root (with its raw envelopes, run report
and manifest to download) and the full corpus under `corpus/`.
