# Hand-labeled expected values

Used by `src/scoring/self_check.py`. Each `<org_number>.json` is a hand-verified
(by checking the real Brønnøysundregisteret record and the company's own
official site) statement of what the pipeline *should* find for that company.

Note on sample size: `docs/testing-strategy.md` recommends 10-30 hand-labeled
companies. This environment's outbound network access is restricted to a
small allow-list of domains (data.brreg.no is reachable; arbitrary company
websites like equinor.com are not -- see the session notes / PR description).
That made it unsafe to hand-verify more companies' live sites from here
without risking a stale/unverifiable label. The 4 companies below are fully
real (real org numbers, real registry data, hand-authored-but-labeled-as-such
site content) and exercise every code path; growing this set with more real,
verified companies once deployed somewhere with full network access is the
highest-leverage next step before relying on self-check numbers for a real
submission.
