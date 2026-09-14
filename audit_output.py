"""
Standalone post-hoc audit of a submitted envelopes.jsonl, checking for the
four evaluator-flagged disqualifier classes, independent of the pytest suite.

NOTE ON SCHEMA: this checks the REAL envelope shape this project actually
produces (src/pipeline/envelope.py, matching the reference starter kit's
OUTPUT_CONTRACT.md) -- a flat `claims` LIST of {field, value, availability,
confidence, evidence_ids}, a separate deduplicated `evidence` list, and the
fixed six-state enum (available/not_available/blocked/not_applicable/
ambiguous/failed). There is no top-level `legal_name`, no `claims` dict with
keys like "social_profiles"/"job_postings"/"workplaces", no "synthesis.is_hiring"
boolean, and no "temporary_error" state -- a fetch problem is `failed` in this
schema; renaming it would break compliance with the real evaluator contract.

Usage: python audit_output.py results/envelopes.jsonl
"""
import json
import sys
from urllib.parse import urlparse

# Known property-manager / housing-federation domains that can legitimately
# be a housing co-op's OWN registered official_website (many small borettslag
# have no site of their own and list their manager's), but whose OTHER pages
# (careers, press, social links, news) belong to the MANAGER, not the specific
# co-op -- see docs/component-specs.md's src/pipeline/extract.py section.
PROPERTY_MANAGER_DOMAINS = {"obos.no", "usbl.no", "vibbo.no", "vbbl.no", "nobl.no"}
HOUSING_COOP_TERMS = ("borettslag", "boligsameie", "sameiet", " bbl")

ALLOWED_STATES = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}

GENERIC_HIRING_JUNK = {"careers", "career", "jobs", "job openings", "ledige stillinger"}

# A genuine claim value (a name, title, headline, URL) is never anywhere
# close to this long. Real-world regression, found by independent review of
# a real 100-company batch: a directory-listing domain with no HTML block
# tags to split on produced one ~8,000-character claim concatenating dozens
# of unrelated businesses' URLs/dates -- caught here as a general sanity
# check across every field, not just the one that triggered this specific
# finding (see src/pipeline/extract.py's _MAX_FREETEXT_LINE_LENGTH for the
# matching fix at the source).
MAX_SANE_CLAIM_LENGTH = 500


def _domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _is_property_manager_domain(url: str) -> bool:
    dom = _domain(url)
    return any(dom == d or dom.endswith("." + d) for d in PROPERTY_MANAGER_DOMAINS)


def audit(file_path: str) -> int:
    disqualifiers = 0
    warnings = 0
    total_records = 0
    total_workplaces = 0
    total_hiring_signals = 0
    bad_states = 0

    with open(file_path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            total_records += 1
            env = json.loads(line)
            claims = env.get("claims", [])
            evidence_by_id = {e["id"]: e for e in env.get("evidence", [])}

            legal_name = next((c.get("value") for c in claims if c.get("field") == "legal_name"), None) or ""
            is_housing_coop = any(t in legal_name.lower() for t in HOUSING_COOP_TERMS)

            for c in claims:
                state = c.get("availability")
                if state not in ALLOWED_STATES:
                    print(f"[SCHEMA VIOLATION] {env.get('organisation_number')} field={c.get('field')} "
                          f"has disallowed state {state!r} (allowed: {sorted(ALLOWED_STATES)})")
                    bad_states += 1

                value = c.get("value")
                if isinstance(value, str) and len(value) > MAX_SANE_CLAIM_LENGTH:
                    print(f"[WARNING] {env.get('organisation_number')} {legal_name!r} field={c.get('field')} "
                          f"claim value is {len(value)} chars -- likely a concatenated-blob extraction bug: "
                          f"{value[:80]!r}...")
                    warnings += 1

                if c.get("field") == "workplace" and c.get("value"):
                    total_workplaces += 1
                if c.get("field") == "hiring_signal" and c.get("value"):
                    total_hiring_signals += 1
                    if c["value"].strip().lower() in GENERIC_HIRING_JUNK:
                        print(f"[WARNING] {env.get('organisation_number')} {legal_name!r} hiring_signal is generic "
                              f"junk, not a real job ad: {c['value']!r}")
                        warnings += 1

                # 1. Housing co-op / shared-domain manager leak: a co-op's
                # OWN official_website may legitimately be its manager's
                # domain, but any company_profile/hiring_signal/dated_activity
                # sourced from that domain belongs to the MANAGER, not this
                # specific co-op (context_name is what should have rejected
                # it -- see src/pipeline/extract.py's _page_organization_name).
                if is_housing_coop and c.get("field") in ("company_profile", "hiring_signal", "dated_activity"):
                    ev_ids = c.get("evidence_ids") or []
                    source_url = evidence_by_id.get(ev_ids[0], {}).get("source_url", "") if ev_ids else ""
                    if source_url and _is_property_manager_domain(source_url):
                        print(f"[CRITICAL DISQUALIFIER] {env.get('organisation_number')} {legal_name!r} leaked "
                              f"property-manager content: field={c['field']} value={c.get('value')!r} "
                              f"source={source_url}")
                        disqualifiers += 1

            for e in env.get("errors", []):
                if e.get("state") not in ALLOWED_STATES:
                    print(f"[SCHEMA VIOLATION] {env.get('organisation_number')} error state {e.get('state')!r} "
                          f"not in allowed enum")
                    bad_states += 1

    print("\n--- AUDIT SUMMARY ---")
    print(f"Total records processed: {total_records}")
    print(f"Total workplace claims: {total_workplaces}")
    print(f"Total hiring_signal claims: {total_hiring_signals}")
    print(f"Housing-coop / property-manager leaks found: {disqualifiers}")
    print(f"Generic/junk hiring-signal warnings found: {warnings}")
    print(f"Schema (disallowed-state) violations found: {bad_states}")

    if disqualifiers > 0 or bad_states > 0:
        print("\nRESULT: FAILED - DO NOT SUBMIT TO BUILDERR.")
        return 1
    print("\nRESULT: PASSED - CLEAN FOR SUBMISSION (on this sample).")
    return 0


if __name__ == "__main__":
    sys.exit(audit(sys.argv[1] if len(sys.argv) > 1 else "results/envelopes.jsonl"))
