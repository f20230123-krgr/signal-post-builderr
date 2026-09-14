"""
Tests for src/pipeline/verify.py -- see docs/component-specs.md.

This stage enforces the 95%-precision hard gate. These tests are the closest
thing we have to a direct, fast check on that gate -- keep them honest.
"""
from datetime import datetime, timezone

import src.pipeline.verify as verify_module
from src.models.profile import EvidenceState
from src.pipeline.extract import RawFact
from src.pipeline.resolve import ResolvedEntity
from src.pipeline.verify import NAME_MATCH_THRESHOLD, verify

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _entity(legal_name="EQUINOR ASA", official_site="https://www.equinor.com"):
    return ResolvedEntity(
        org_number="923609016",
        legal_name=legal_name,
        registered_address="Forusbeen 50, 4035 STAVANGER",
        official_site_candidate=official_site,
        resolution_state=EvidenceState.AVAILABLE,
        source="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=NOW,
    )


def _fact(field_name, value, source_url="https://www.equinor.com/about", extraction_method="structured", content_hash="abc123", linked_from=None, context_name=None):
    return RawFact(
        field_name=field_name,
        value=value,
        source_url=source_url,
        extraction_method=extraction_method,
        extracted_at=NOW,
        content_hash=content_hash,
        linked_from=linked_from,
        context_name=context_name,
    )


def test_exact_name_match_is_accepted():
    fact = _fact("organization_name", "Equinor ASA")
    confirmed = verify(fact, _entity())

    assert confirmed is not None
    assert confirmed.field_name == "organization_name"
    assert confirmed.match_confidence >= NAME_MATCH_THRESHOLD
    assert confirmed.retrieved_at == NOW
    assert confirmed.content_hash == "abc123"
    assert confirmed.extraction_method == "structured"
    assert confirmed.source_class == "company_owned"


def test_similar_but_different_company_name_is_rejected():
    # Same domain, but the extracted name text doesn't actually match --
    # scores well below threshold (verified against real RapidFuzz output).
    fact = _fact("organization_name", "Equinox Holding")
    confirmed = verify(fact, _entity())

    assert confirmed is None


def test_fact_from_a_different_domain_than_the_official_site_is_rejected():
    # A look-alike/imposter site could use the exact same company name --
    # provenance (source domain) must gate independently of name similarity.
    fact = _fact("organization_name", "Equinor ASA", source_url="https://www.equinor-totally-legit.example/")
    confirmed = verify(fact, _entity())

    assert confirmed is None


def test_non_name_bearing_fact_from_official_site_is_accepted_on_provenance():
    fact = _fact("registered_address", "Forusbeen 50, 4035 Stavanger")
    confirmed = verify(fact, _entity())

    assert confirmed is not None
    assert confirmed.value == "Forusbeen 50, 4035 Stavanger"


def test_no_legal_name_on_entity_means_nothing_can_be_verified():
    fact = _fact("organization_name", "Equinor ASA")
    confirmed = verify(fact, _entity(legal_name=None))

    assert confirmed is None


def test_fact_from_ats_domain_is_accepted_when_linked_from_official_site():
    """Evaluator feedback: "Social profiles and external hiring systems ...
    should be accepted only when linked from the company's official site.
    Preserve that link chain as evidence." """
    fact = _fact(
        "hiring_signal",
        "Backend Engineer (posted 2026-06-20)",
        source_url="https://boards.greenhouse.io/equinor",
        linked_from="https://www.equinor.com/careers",
    )
    confirmed = verify(fact, _entity())

    assert confirmed is not None
    assert confirmed.source_class == "external"
    assert confirmed.linked_from == "https://www.equinor.com/careers"


def test_fact_from_ats_domain_is_rejected_without_a_linked_from_chain():
    """No linked_from at all -- an off-domain fact must still be rejected on
    provenance, exactly as before this feature existed."""
    fact = _fact("hiring_signal", "Backend Engineer", source_url="https://boards.greenhouse.io/equinor")
    confirmed = verify(fact, _entity())

    assert confirmed is None


def test_fact_from_ats_domain_is_rejected_when_linked_from_a_different_companys_site():
    fact = _fact(
        "hiring_signal",
        "Backend Engineer",
        source_url="https://boards.greenhouse.io/equinor",
        linked_from="https://some-other-company.example/careers",
    )
    confirmed = verify(fact, _entity())

    assert confirmed is None


def test_shared_domain_fact_is_rejected_when_context_name_does_not_match():
    """Real-world regression, from real evaluator feedback: a Norwegian
    housing cooperative ("Nedre Stovner Borettslag") legitimately lists its
    property manager's shared domain (obos.no) as its registered website --
    common for small housing co-ops with no site of their own. That shared
    domain's OWN JSON-LD Organization block is named "OBOS BBL", with OBOS's
    OWN social links. Provenance (same domain) alone used to be enough to
    accept these for the housing co-op -- context_name must reject them,
    since "OBOS BBL" doesn't match "NEDRE STOVNER BORETTSLAG" at all."""
    entity = _entity(legal_name="NEDRE STOVNER BORETTSLAG", official_site="https://www.obos.no")
    fact = _fact(
        "company_profile",
        "https://www.facebook.com/obosmedlem",
        source_url="https://www.obos.no",
        context_name="OBOS BBL",
    )

    confirmed = verify(fact, entity)

    assert confirmed is None


def test_shared_domain_fact_is_accepted_when_context_name_matches():
    """The same shared-domain provenance is fine when the co-located JSON-LD
    name genuinely IS the entity -- e.g. OBOS's own facts, verified against
    OBOS itself."""
    entity = _entity(legal_name="OBOS BBL", official_site="https://www.obos.no")
    fact = _fact(
        "company_profile",
        "https://www.facebook.com/obosmedlem",
        source_url="https://www.obos.no",
        context_name="OBOS BBL",
    )

    confirmed = verify(fact, entity)

    assert confirmed is not None
    assert confirmed.value == "https://www.facebook.com/obosmedlem"


def test_housing_coop_shared_manager_domain_fact_rejected_even_without_a_context_name():
    """Real-world regression, confirmed in the real 1,000-company submission
    batch (51 leaked claims): the context_name check above only fires when a
    co-located JSON-LD name (or og:site_name) is actually present on the
    SPECIFIC page a fact came from -- a per-article page on a property
    manager's site (e.g. usbl.no's own "nyheter"/news sub-pages) can carry
    neither, even though the site's homepage does. context_name is then None,
    and (per test_non_name_bearing_fact_without_context_name_still_gated_on_provenance_only
    just below) verify() falls back to domain-provenance-only acceptance --
    which is exactly right for a genuinely-owned domain, but wrong here: the
    housing co-op registered usbl.no (a shared, multi-tenant property-manager
    platform, not its own site) as its official site, so USBL's own generic
    news/social/hiring content must never be attributed to one specific
    co-op regardless of whether this particular page happened to carry
    identifying metadata. A hard domain check, gated on the entity looking
    like a housing co-op (borettslag/boligsameie/sameiet/BRL), closes this
    without depending on any page-level metadata being present at all."""
    entity = _entity(legal_name="BOLIGSAMEIET HAVEGATEN 36", official_site="https://www.usbl.no")
    fact = _fact(
        "dated_activity",
        "Usbl tar et sterkere grep om Lillestrøm regionen (2019-11-10T13:44:27+00:00)",
        source_url="https://www.usbl.no/om-oss/nyheter/usbl-tar-et-sterkere-grep-om-lillestrom-regionen",
        context_name=None,
    )

    confirmed = verify(fact, entity)

    assert confirmed is None


def test_housing_coop_own_official_site_claim_is_not_blocked_by_the_manager_domain_check():
    """The hard filter must only block company_profile/hiring_signal/
    dated_activity attribution -- it must never block the official_site
    claim itself, which is exactly how a co-op legitimately registers a
    property manager's domain as its own site in the first place."""
    entity = _entity(legal_name="BOLIGSAMEIET HAVEGATEN 36", official_site="https://www.usbl.no")
    fact = _fact("official_site", "https://www.usbl.no", source_url="https://www.usbl.no", context_name=None)

    confirmed = verify(fact, entity)

    assert confirmed is not None


def test_non_housing_entity_on_its_own_domain_is_unaffected_by_the_manager_domain_check():
    """The hard filter must be scoped to housing co-ops specifically -- an
    ordinary company on its own genuinely-owned domain must be unaffected."""
    fact = _fact("dated_activity", "Equinor wins award (2026-07-01)", context_name=None)

    confirmed = verify(fact, _entity())

    assert confirmed is not None


def test_non_name_bearing_fact_without_context_name_still_gated_on_provenance_only():
    """Backward-compatible: when there's no co-located name to check at all
    (e.g. a bare canonical link, or a fixture page with no embedded
    Organization name), fall back to the original domain-provenance-only
    behavior -- this must not regress existing, already-correct cases."""
    fact = _fact("leader", "Anders Opedal (President and CEO)", context_name=None)

    confirmed = verify(fact, _entity())

    assert confirmed is not None


def test_threshold_boundary_case_is_explicit(monkeypatch):
    monkeypatch.setattr(verify_module, "name_similarity", lambda a, b: float(NAME_MATCH_THRESHOLD))
    accepted = verify(_fact("organization_name", "anything"), _entity())
    assert accepted is not None
    assert accepted.match_confidence == NAME_MATCH_THRESHOLD

    monkeypatch.setattr(verify_module, "name_similarity", lambda a, b: NAME_MATCH_THRESHOLD - 0.01)
    rejected = verify(_fact("organization_name", "anything"), _entity())
    assert rejected is None
