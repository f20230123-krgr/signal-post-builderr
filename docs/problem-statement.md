# Problem Statement (verbatim capture, from builderr.ai/challenges/signalpost)

This document is the frozen source of truth for *what we were asked to build*.
If anything in code or in another doc conflicts with this file, this file wins
unless the user explicitly updates it.

## Overview

Build an autonomous research agent that constructs verified company intelligence
profiles for Norway's 411,160 registered entities — "Crunchbase for every
Norway-registered company," covering the complete national registry rather than
a curated subset.

## Core requirements — the four phases

1. **Resolve the entity** — connect registration numbers to official names and sites
2. **Crawl useful sources** — from permitted public data only
3. **Prove the claims** — timestamped, sourced evidence for every fact
4. **Refresh and explain** — repeatable, documented updates that never lose history

## Input

- 411,160 eligible active Norwegian entities (2025-filer universe), delivered as
  `signalpost-company-universe-2025.jsonl.gz` (JSONL = one JSON object per line)
- Organization numbers as the only starting point per company
- Publicly available sources only (see source policy, referenced but not fully
  detailed in the public brief — treat as "official, company-owned, licensed
  sources only" until the actual `signalpost-sources.md` is obtained and reviewed)

## Output

- Minimum 1,000 completed profiles per submission
- Exactly 100 terminal results per daily 100-company test batch
- Each profile contains seven required sections:
  1. Legal identity and public brand
  2. Latest annual accounts and available history
  3. Leadership and registered workplaces
  4. Verified official website and company-owned profiles
  5. Hiring and dated public activity from permitted sources
  6. Claim-level evidence and availability state
  7. Refresh metadata and material changes since the previous run
- Every claim requires: source, retrieval timestamp, reporting period (where
  applicable), and an availability state from the fixed set:
  `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous`, `failed`

## Resource budget (per 100-company daily batch)

- 45-minute wall-clock limit
- 8 vCPU (virtual CPU — a processing "lane"), 16 GB RAM, 10 GB temp disk
- Maximum 2,000 outbound requests (cache hits are free)
- Maximum $10 external API spend
- Up to 4 revision submissions allowed before October 18, 2026

## Hard gates (non-negotiable — fail one, fail the submission)

- Coverage score ≥ 21/35
- Weighted external company recall ≥ 60%
- External precision ≥ 95% (no wrong-company publication)
- Exactly 100 terminal results per daily batch
- No fabricated financial data
- Idempotent refresh — prior snapshots always preserved

## Scoring (100 points total; 65 to qualify)

| Category | Weight |
|---|---|
| Coverage & source discovery | 35 |
| Accuracy, identity & evidence | 30 |
| Refresh & extensibility | 20 |
| Decision-useful synthesis | 10 |
| UX & interaction | 5 |

Coverage detail: for every external field, 70% of its coverage score comes from
company-level recall (did you find the right company at all) and 30% from
claim-level recall (did you get the specific fact right). Depth on a company you
never correctly identified scores nothing — breadth and correct identity come first.

## Timeline

- Competition period: August 23 – October 21, 2026
- Daily random testing: the same 100 companies evaluated identically across all
  frozen submissions, selected by Builderr after each day's cutoff
- Fortnightly non-binding community voting

## Who this is for (target end users of the finished product, not the graders)

- Job seekers evaluating an employer before applying
- Business-development people before selling to or partnering with a company
- Investors doing due diligence before investing
- Anyone monitoring a company for material changes over time

## Who runs / judges the challenge

- **Builderr** administers the competition, selects/freezes daily batches, and
  scores evidence, freshness, and repeatability.
- **JBOX** sponsors a separate bonus prize pool for qualifying final agents built
  with JBOX — not a judge of the core competition.

## Submission mechanics

Email `submit@builderr.ai` with: agent name, repository URL, commit hash,
completed profile count (≥1,000), organization-number manifest URL, a
one-command run instruction, models/APIs/licenses used, expected cost per
100-company batch, and contact details.

## Provided resources

- Starter kit (runnable reference agent): `signalpost-starter-kit.tar.gz`
- Full brief: `starter-briefs/signalpost.md`
- Company universe: `signalpost-company-universe-2025.jsonl.gz`
- 100-company product sample (viewable, not embedded in the brief)
- Source policy file: `signalpost-sources.md` (referenced, not yet reviewed —
  **action item**: fetch and review before building the crawler's allow-list)
- Agent playbook: `signalpost-agent-playbook.md` (referenced, not yet reviewed —
  **action item**: fetch before finalizing the crawling strategy)

## Open items to resolve before/while building

- [ ] Obtain and review `signalpost-sources.md` — exact permitted/forbidden source list
- [ ] Obtain and review `signalpost-agent-playbook.md` — any prescribed crawling approach
- [ ] Obtain the 100-company sample output to see the graders' expected shape in practice
- [ ] Confirm exact registry API (Brønnøysundregistrene) access method/rate limits
