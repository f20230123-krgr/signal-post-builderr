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
| The resolved entity's own official website (+ same-domain paths: `/about`, `/careers`, `/contact`, `/news`, `/investor`, etc.) | Public brand, leadership mentions, hiring signals, dated activity, company-owned profile links (e.g. LinkedIn, read only as a self-published link, never scraped directly) | Free | None |

**No paid third-party API is used anywhere.** Expected cost per 100-company
batch: **$0** (well under the $10 cap).

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
- **Self-check hand-labeled fixture sample is 4 companies**, smaller than the
  recommended 10-30 (see `fixtures/expected/README.md`) — this development
  environment's network access is restricted to a small domain allow-list,
  which made it unsafe to hand-verify more real company websites from here.
