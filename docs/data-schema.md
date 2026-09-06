# Data & Evidence Schema

This is the contract `src/models/profile.py` must implement in Pydantic. The
brief specifies required *sections*, not a rigid JSON schema — this doc is our
concrete interpretation. If it's ever wrong, fix it here first, then in code.

## Evidence state enum (used on every claim, no exceptions)

```
available | not_available | blocked | not_applicable | ambiguous | failed
```

A field can never be emitted without one of these states. This is what lets the
agent be rewarded for honestly reporting a gap instead of guessing.

## Claim shape (used inside every section below)

```
Claim:
  value: <the fact itself, or null if state != available>
  state: <evidence state enum>
  source: <url or registry identifier>
  retrieved_at: <ISO 8601 timestamp>
  reporting_period: <e.g. "FY2025", or null if not time-bound>
  content_hash: <SHA-256 of the exact source content, or null>
  source_class: <"official_registry" | "company_owned" | "external", or null>
  extraction_method: <"registry" | "structured" | "text", or null>
```

`content_hash`/`source_class`/`extraction_method` were added after reading the
real `signalpost-sources.md` ("every claim records source URL..., retrieval
time,..., content hash and extraction method") and the reference agent's
`OUTPUT_CONTRACT.md`. They're populated everywhere the pipeline can (see
`resolve.py`/`extract.py`/`verify.py`/`registry_extras.py`/`assemble.py`), but
deliberately **not** added to the `Claim.value`/`retrieved_at`-style hard
validator -- doing so would require every existing test construction of
`ResolvedEntity`/`RawFact`/`ConfirmedFact` across the suite to supply them,
for a schema-strictness gain that isn't itself one of the hard gates in
`success-criteria.md`. `source_class` values map directly to
`signalpost-sources.md`'s three source tiers (Official Norwegian records /
Company-owned sources / External sources).

## CompanyProfile (top level)

```
CompanyProfile:
  org_number: str
  run_timestamp: ISO 8601 datetime          # when this snapshot was produced

  # 1. Legal identity and public brand
  legal_identity:
    legal_name: Claim
    public_brand: Claim

  # 2. Latest annual accounts and available history
  annual_accounts:
    latest: Claim
    history: list[Claim]

  # 3. Leadership and registered workplaces
  leadership:
    leaders: list[Claim]        # e.g. CEO, board chair
    workplaces: list[Claim]

  # 4. Verified official website and company-owned profiles
  online_presence:
    official_site: Claim
    company_profiles: list[Claim]   # e.g. verified LinkedIn company page

  # 5. Hiring and dated public activity from permitted sources
  activity:
    hiring_signals: list[Claim]
    dated_activity: list[Claim]

  # 6. Claim-level evidence and availability state
  # (not a separate section in practice — every Claim above already carries
  # this. This section exists in the brief to make explicit that evidence
  # is a first-class, checkable part of the output, not an afterthought.)
  evidence_log: list[Claim]     # flat list, all claims, for easy auditing

  # 7. Refresh metadata and material changes since the previous run
  refresh_metadata:
    previous_run_timestamp: ISO 8601 datetime | null
    material_changes: list[str]
    is_first_run: bool
```

## Rules encoded directly in the Pydantic model (not just documented)

- Every `Claim` requires `state`; `value` may only be non-null when
  `state == "available"`.
- `retrieved_at` is required on every claim that has `state == "available"`.
- `org_number` must match the input format used in the company universe file.
- `refresh_metadata.is_first_run == True` implies `previous_run_timestamp is None`
  and `material_changes == []` — enforce this as a model validator, not a
  convention someone has to remember.

## Why enforce this in Pydantic instead of just documenting it

Pydantic validation at assemble-time means a malformed profile physically cannot
reach the output file — the equivalent of a compiler error instead of a code
review comment. Given that precision/coverage are hard pass-fail gates, this is
worth the up-front cost of a stricter schema.
