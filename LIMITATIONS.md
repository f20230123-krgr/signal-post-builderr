# Declared assumptions, models/APIs/licences, and known limitations

Required by the submission contract (`starter-briefs/signalpost.md`,
starter-kit `README.md`: "declared models, APIs, licences and source-rights
assumptions"). This is the canonical answer for the submission email.

## Models

**None.** This agent is fully deterministic — no LLM is used anywhere in the
pipeline (resolve, crawl, extract, verify, assemble). Identity matching uses
RapidFuzz string similarity; extraction uses `extruct` (structured data) and
`trafilatura` (text), not a language model. This is a conscious choice: the
agent playbook explicitly limits what an LLM may do ("must not decide exact
identity, invent a missing field, or silently override deterministic
evidence"), and the simplest way to honor that is not to use one at all yet.

## APIs / external data sources used

| Source | What for | Cost | Auth |
|---|---|---|---|
| `data.brreg.no` (Brønnøysundregisteret Enhetsregisteret, `roller`, `regnskapsregisteret`, `underenheter`) | Legal identity, official site, leadership roles, filed annual accounts, registered sub-units | Free, public | None |
| `signalpost-company-universe-2025.jsonl.gz` (Builderr-provided) | Zero-cost, byte-identical identity anchoring when the org number is covered | Free (already downloaded) | None |
| The resolved entity's own official website (+ same-domain paths: `/about`, `/careers`, `/contact`, `/news`, `/investor`, etc., plus sitemap.xml/robots.txt-discovered pages) | Public brand, leadership mentions, hiring signals, dated activity, company-owned profile links (e.g. LinkedIn, read only as a self-published link, never scraped directly) | Free | None |
| Official ATS platforms (Greenhouse, Lever, Workable, Teamtailor, Workday) -- only ever fetched when linked directly from the entity's own official site, link chain preserved as evidence | Hiring signals | Free | None |
| Exa Search API (`api.exa.ai/search`) | Candidate website discovery for companies with none on file -- candidate generation only, independently re-verified via the same name-match gate before acceptance (never trusted as evidence itself). Tried first in the discovery chain. | Free tier: 20,000 requests/month, no card required | `EXA_API_KEY` env var |
| Parallel Search API (`api.parallel.ai/v1/search`) | Same candidate-generation role as Exa, tried second (only if Exa is unconfigured or returns nothing) | Free tier: ~5,000 requests/month, no card required | `PARALLEL_API_KEY` env var |
| NAV job-vacancy feed (`pam-stilling-feed.nav.no`) | Hiring signals: NAV's official national job board, published as a feed for outside developers with a public token. An ad is published only when its own employer org number equals the company's; only the job title and dates are published, never contact details. | Free, public token | None |
| DuckDuckGo Instant Answer API (`api.duckduckgo.com`) | Same candidate-generation role, last-resort fallback if neither key above is configured | Free, no key | None |

Per `EVALUATION_HARNESS.md`: *"server-side secrets supplied through
documented environment variables only"* -- `EXA_API_KEY` / `PARALLEL_API_KEY`
are the two documented variables above; neither is committed to the repo or
hardcoded anywhere (`src/pipeline/discovery.py` reads them via `os.environ`
only). Both providers' free tiers comfortably cover a full daily
100-company batch (~89 lookups/day) at **$0** -- see "Known limitations"
below for the measured per-provider hit-rate difference this made.

## Source-rights assumptions

Checked against the real `signalpost-sources.md` before implementing each
connector:

- Brreg endpoints: official Norwegian government registry, explicitly listed
  as a preferred "Official Norwegian records" source.
- Official site + same-domain paths: explicitly listed as "Company-owned
  sources."
- **Not implemented, checked and deliberately excluded**: direct scraping of
  purehelp.no (its own `robots.txt` explicitly disallows `/company/board_roles/`,
  `/company/report/`, `/company/news/` — exactly the pages that would have
  been useful) and proff.no (returns `403 Forbidden` even on `robots.txt`,
  indicating active bot-blocking). LinkedIn/Meta/Glassdoor/Indeed are never
  scraped directly — the only LinkedIn data captured is a `sameAs` link the
  company's own official site already self-published.
- **Not yet implemented**: NAV's job board (`arbeidsplassen.nav.no`) has an
  open `robots.txt` and would be a reasonable next connector, but its search
  results are loaded client-side via JavaScript and the actual API contract
  couldn't be reverse-engineered without a browser from this development
  environment.

## Direct dependencies and licences

| Package | Licence |
|---|---|
| pydantic | MIT |
| httpx | BSD-3-Clause |
| extruct | BSD-3-Clause (per its repository; PyPI metadata field is blank) |
| trafilatura | Apache-2.0 |
| rapidfuzz | MIT |
| pytest, pytest-asyncio | MIT / Apache-2.0 |

All permissive; no copyleft (GPL-style) obligations.

## Known limitations (not silently hidden — see `docs/testing-strategy.md`'s "no fabricated data" ethos)

- **NAV hiring signals cover ~70% of currently active job ads, not all of
  them.** The feed replays changes oldest-first; covering every active ad
  needs a ~60-day window (~104 list pages, measured), which would eat too
  much of a 2,000-request batch. The run reads ads modified in the last 30
  days (~64 pages, ~93% of active ads; a 14-day window missed live ads that were
  last modified longer ago) with a hard cap of 80 pages; if the cap
  is ever hit, the newest ads are the ones cut off, and `run-report.json`'s
  `nav_job_index.truncated_at_page_cap` says so. Ads are matched to a company
  by exact employer name first, then confirmed by org number, so an ad listed
  under a brand name that differs from the legal name is missed.
- **Former-name website search is limited to one former name per company**,
  is skipped once the batch has used 90% of its request budget, and is only
  used when no other registered entity currently holds that name.

- **Per-company `operations`** (request count/cost/runtime) in each
  submission envelope report as zero. `BudgetGovernor` is intentionally one
  shared counter per 100-company chunk (that's what makes the 2,000-request
  cap enforceable at all), so an exact per-company attribution under
  concurrency isn't implemented. Accurate **aggregate** totals are in
  `run-report.json`.
- **Group/parent/subsidiary/franchise labelling** (required by
  `signalpost-sources.md`: "must be labelled, not collapsed") is not yet
  implemented — `verify.py`'s domain-provenance + name-match gate prevents
  wrong-company publication, but doesn't yet distinguish "this is the parent
  company's page" from "this is the exact subsidiary."
- **Playwright JS-rendering fallback** is optional and not installed by
  default. A page requiring JS rendering that isn't caught as an obvious
  "shell" degrades to its static content rather than failing outright, but
  true JS-rendered content isn't captured without installing it.
- **Self-check fixture sample is 10 companies**, at the low end of the
  recommended 10-30. Four have hand-verified websites; six were added with no
  website on file, to represent the ~89% majority case. Those six carry
  registry-derived labels, so their registry fields test wiring and
  regressions rather than independent discovery accuracy.
- **DuckDuckGo-only discovery had a near-zero real-world hit rate** for the
  actual gap it targets — ~89% of a random sample of the company universe has
  no website on file in the registry, and DuckDuckGo's free Instant Answer
  API only returns one when the query matches a Wikipedia infobox (0/15 real
  companies with no registry website returned any candidate, in a test
  biased toward the more-likely-notable end of that population). This is why
  the provider chain now tries Exa and Parallel first (see above) — both are
  genuine general web search, not Wikipedia-notability-gated. DuckDuckGo
  stays wired in only as the zero-cost last resort when neither key is
  configured.
- **`verify_discovered_site` (the identity-confirmation half of search
  discovery) can't confirm identity on heavy-JS/JavaScript-framework
  official sites** — it does a simple static fetch, not the JS-shell
  detection + Playwright fallback `crawl.py` has. Observed directly on
  `equinor.com` (a real, heavy-JS corporate site): a valid candidate was
  found, but verification correctly declined to confirm it rather than guess,
  because the static HTML had no extractable company-name signal. Correct,
  safe behavior (precision preserved), but it further lowers this mechanism's
  already-low effective hit rate.

## Known limits added 2026-09-21

- **Request counting is the real wire count.** Builderr counts redirects and retries, so
  every client goes through `src/pipeline/net.py`. Measured on 100 unseen companies:
  1,467 real requests for the default command, 1,187 with Exa limited to the first round. A run that would exceed the limit degrades (1,800) and stops fetching (1,940)
  rather than overshooting; companies processed after that point get thinner profiles.
- **The guessed page paths were cut from 13 to 4** (`/kontakt`, `/om-oss`, `/about`,
  `/contact`) to save ~9 requests per site. On the 1,000-company corpus the nine dropped
  paths had produced claims on 5 pages (~0.1% of claims). Pages under those names are still
  found through the sitemap and the pages' own links.
- **Redirect chains are capped at 5 hops and never retried.** One real site's `robots.txt`
  redirected to itself and cost 192 requests before this.
- **NAV hiring signals depend on NAV's public feed being reachable.** During testing on
  2026-09-21 the feed timed out; the agent then simply publishes no NAV signals (and spends
  2-4 requests finding out). Nothing else is affected.
- **Guessing company domains was measured and not built.** On 87 companies with no
  registered website it verified one site that search missed (about +1 per 100), at the
  cost of new code and precision risk.
- **The outbound URL guard resolves a name before the request and the HTTP client resolves
  it again**, so a hostile DNS server could in principle answer differently the second
  time (DNS rebinding). Names that do not resolve are let through (the request just fails).
- **Employee count is now the live registry value** (falling back to the universe file's
  2025 figure), so it can differ from the manifest for companies that changed size.
- **A site that bounces every page through a login redirect still costs requests.** One
  measured example spent ~58 requests on 13 pages (five hops each, the cap). The chain is
  bounded, never retried, and yields nothing published; at most ~3% of a batch.
- **If data.brreg.no is unavailable while a batch runs, the affected companies get FAILED
  registry claims** (accounts, workplaces, founding date, leaders). This happened once during
  the corpus run (58 profiles in one batch, replaced by re-running those 100 companies).
  Registry calls are retried three times over about a second and a half, which cannot outlast
  an outage of minutes. Builderr's contract treats a shared-source failure as void and rerun.
- **NAV's job feed is intermittently slow or unreachable** (it timed out for whole stretches on
  2026-09-21/22). The corpus therefore has 0 hiring signals (the previous one had 2).
- **NAV hiring signals depend on when an ad was last modified.** The old 14-day window found
  2 signals in the first corpus and 0 in the regenerated one, because two still-live ads
  had aged out of the window; the window is now 30 days. The NAV list endpoint was too slow
  to re-verify this live on 2026-09-22, so the corpus in this repo was generated with the
  14-day window and has 0 NAV hiring signals.
- **The social-link refresh only touches companies whose site was reachable at
  refresh time.** 9 companies with a known official site could not be re-crawled
  in three attempts on 2026-09-25 and were left at their original (pre-fix)
  social-profile claims rather than risk wiping real data on a transient failure.
