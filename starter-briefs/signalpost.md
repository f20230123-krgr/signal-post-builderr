# Signalpost — participant brief

Status: Open. Round 1 runs from 23 August through 21 October 2026. Random daily evaluation begins 24 August.

## The challenge

Build an agent that turns a Norwegian organisation number into a useful, current and verifiable company profile.

The product should help someone understand a company before they apply, sell, partner or invest: what it does, who leads it, where it operates, how its latest filed numbers look, whether it appears to be hiring, and what dated public activity the checked sources reveal.

This is not a static-directory contest. The core problem is exact-entity discovery, evidence collection and repeatable refresh.

## Universe and run format

- Universe: Norway-registered entities with observed official annual-account records.
- Public universe: all 411,160 eligible organisation numbers in the frozen 2025-filer snapshot.
- Minimum entry coverage: 1,000 completed company profiles. Larger submissions—including 10,000 or the full universe—are allowed.
- Daily evaluation: the same 100 companies are randomly selected after the cutoff for every frozen agent.
- Round length: 60 days, up to 6,000 scored company checks.
- Builderr supplies organisation numbers, cutoff and output contract—not the companies' official sites or social identities.
- Every frozen submission receives the same batch and resource budget.
- Full public universe: [`signalpost-company-universe-2025.jsonl.gz`](../signalpost-company-universe-2025.jsonl.gz).
- Frozen eligible universe: 411,160 active entities whose latest submitted annual-account year was 2025. Uncompressed content SHA-256: `b82d6a3e7231d1759a958c282bc4366b80ec2fab8095053d8ed7fa9cd01bc838`. Download archive SHA-256: `1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384`.

## Required company envelope

Every input must end in one terminal envelope, even when sources are missing or blocked.

Required sections:

1. Legal identity and public brand
2. Latest annual accounts and available history
3. Leadership and registered workplaces
4. Verified official website and company-owned profiles
5. Hiring and dated public activity from permitted sources
6. Claim-level evidence and availability state
7. Refresh metadata and material changes since the previous run

Use explicit states such as `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous` and `failed`. Never turn absence into zero.

## Scoring — 100 points

Scoring version 2 applies to every Round 1 entrant from 26 August 2026. All active submissions are rescored under the same rubric and current pooled-evidence version.

- 35 — Coverage and source discovery
- 30 — Accuracy, exact identity and evidence
- 20 — Refresh and extensibility
- 10 — Decision-useful synthesis
- 5 — UX and interaction

For each external field family, its coverage score is 70% company recall and 30% individual-claim recall against the independently verified union of discoveries from every submitted crawler. The union grows when any agent contributes a new verified claim; each new version is hashed and every entrant is rescored against it. The final union freezes after final-submission verification.

Qualification requires 65/100, at least 21/35 coverage, at least 60% weighted external company recall, at least 95% external precision and every hard gate. Final ranking uses mean score across completed daily batches.

## Locked run budget

- 100 input organisation numbers per daily batch
- 45 minutes wall clock
- 8 vCPU, 16 GB RAM, 10 GB temporary disk
- 2,000 outbound requests per batch, including redirects and retries
- $10 maximum declared third-party API spend per batch
- Up to four revisions; send a new exact commit hash by 18 October

## Hard gates

- At least 21/35 coverage and 60% weighted external company recall
- At least 95% external precision and no material wrong-company publication
- Submitted public artifact contains at least 1,000 completed profiles and its exact organisation-number manifest
- Exactly 100 terminal envelopes for each daily batch
- No fabricated financial values
- Claim-level source, retrieval time and reporting period where relevant
- Honest availability states
- Idempotent refresh with prior snapshots preserved
- Reproducible setup, pinned dependencies and one evaluator command
- Declared source rights, server-side secrets and safe URL handling

## Rewards

- $2,000 main final pool: $1,200 / $500 / $300
- $500 JBOX bonus pool: $250 / $150 / $100
- Four separate $100 community-vote awards on 6 September, 20 September, 4 October and 18 October

Public voting does not alter the technical ranking. Only technically qualified agents enter the hosted gallery.

## Beyond the prize

The winning builder gets the opportunity to partner with [Håvard Liltved Dalen](https://www.linkedin.com/in/liltved/) to launch Signalpost in Norway. Håvard is a Norwegian serial entrepreneur, CPO at Fronted and co-founder of JBOX.

## Start here

Fastest path:

```bash
curl -LO https://builderr.ai/signalpost-starter-kit.tar.gz
curl -LO https://builderr.ai/signalpost-company-universe-2025.jsonl.gz
tar -xzf signalpost-starter-kit.tar.gz
cd signalpost-starter-kit
```

Then follow `README.md` in the starter kit. It walks through dependency setup, selecting at least 1,000 companies, running the baseline, testing it and producing the required submission files.

- [Download the runnable reference agent](../signalpost-starter-kit.tar.gz)
- [Download the full 411,160-company universe](../signalpost-company-universe-2025.jsonl.gz)
- [Agent playbook](./signalpost-agent-playbook.md)
- [Learning harness](./signalpost-learning-harness.md)
- [Source policy](./signalpost-sources.md)
- [Evaluation contract](../docs/signalpost-evaluation-harness.md)
- [100-company product sample](https://builderr.ai/signalpost)

## Submit

Email `submit@builderr.ai` with the repository URL, exact commit hash, completed-profile count, organisation-number manifest, one run command, models/APIs/licences, expected cost per 100-company batch, agent name and contact for results.
