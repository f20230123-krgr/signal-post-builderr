# Fixtures

Recorded/labeled sample data for offline tests. No live network calls in the
test suite (see docs/testing-strategy.md) -- everything the pipeline needs for
a test run should be recorded here.

Planned contents (populate as stages are implemented):
- `pages/<org_number>/*.html` -- recorded raw HTML for each fixture company's
  official site + one secondary source
- `expected/<org_number>.json` -- hand-verified expected ResolvedEntity /
  CompanyProfile for that company, used by the self-check harness and the
  integration test

Update these deliberately (by hand, or via a one-off recording script you
review), never by silently overwriting them from a live crawl -- that would
make tests non-reproducible.
