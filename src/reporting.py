"""
Static HTML results view -- "UX & interaction" (docs/success-criteria.md:
"profiles are legible/reviewable on desktop and mobile").

No server, no JS framework: one self-contained HTML file per batch, native
<details>/<summary> for per-company expansion (works with no JavaScript at
all, including on mobile), and a single CSS media query for the mobile
breakpoint. This module never invents content -- it only renders what's
already in a validated CompanyProfile plus src/synthesis.py's templated
answers, so a legible report can't drift from what was actually verified.
"""
from __future__ import annotations

import html as html_lib
from datetime import datetime

from src.models.profile import Claim, CompanyProfile, EvidenceState
from src.synthesis import answer_business_questions

_STYLE = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 1rem;
       background: #f7f7f8; color: #1a1a1a; }
h1 { font-size: 1.25rem; }
.meta { color: #666; margin-bottom: 1rem; }
.company { background: #fff; border: 1px solid #ddd; border-radius: 8px; margin-bottom: 0.75rem;
           padding: 0; overflow: hidden; }
.company > summary { cursor: pointer; padding: 0.85rem 1rem; font-weight: 600; list-style: none;
                      display: flex; justify-content: space-between; gap: 1rem; }
.company > summary::-webkit-details-marker { display: none; }
.company-body { padding: 0 1rem 1rem 1rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; margin: 0.5rem 0 1rem 0; }
th, td { text-align: left; padding: 0.35rem 0.5rem; border-bottom: 1px solid #eee; vertical-align: top; }
.state-available { color: #0a7a2f; }
.state-not_available, .state-blocked, .state-failed { color: #999; }
.state-ambiguous, .state-not_applicable { color: #b8860b; }
.synthesis dt { font-weight: 600; margin-top: 0.5rem; }
.synthesis dd { margin: 0.15rem 0 0 0; }
@media (max-width: 480px) {
  body { padding: 0.5rem; }
  table, thead, tbody, th, td, tr { display: block; }
  th { display: none; }
  td { border-bottom: none; padding: 0.15rem 0; }
  td:first-child { font-weight: 600; }
}
"""


def _esc(value: str) -> str:
    return html_lib.escape(value)


def _claim_row(label: str, claim: Claim) -> str:
    value = _esc(claim.value) if claim.value else "&mdash;"
    return (
        f"<tr><th>{_esc(label)}</th>"
        f'<td class="state-{claim.state.value}">{value} '
        f'<span class="meta">({_esc(claim.state.value)})</span></td></tr>'
    )


def _list_claim_rows(label: str, claims: list[Claim]) -> str:
    if not claims:
        return f'<tr><th>{_esc(label)}</th><td class="state-not_available">&mdash; <span class="meta">(not_available)</span></td></tr>'
    values = "; ".join(_esc(c.value) for c in claims if c.value)
    return f'<tr><th>{_esc(label)}</th><td class="state-available">{values}</td></tr>'


def _company_section(profile: CompanyProfile) -> str:
    legal_name = profile.legal_identity.legal_name
    title = _esc(legal_name.value) if legal_name.value else f"Org. {_esc(profile.org_number)}"

    rows = "".join(
        [
            _claim_row("Legal name", legal_name),
            _claim_row("Public brand", profile.legal_identity.public_brand),
            _claim_row("Official website", profile.online_presence.official_site),
            _claim_row("Latest annual accounts", profile.annual_accounts.latest),
            _list_claim_rows("Leaders", profile.leadership.leaders),
            _list_claim_rows("Workplaces", profile.leadership.workplaces),
            _list_claim_rows("Company profiles", profile.online_presence.company_profiles),
            _list_claim_rows("Hiring signals", profile.activity.hiring_signals),
            _list_claim_rows("Dated activity", profile.activity.dated_activity),
        ]
    )

    synthesis_items = "".join(
        f"<dt>{_esc(a['question'])}</dt><dd>{_esc(a['answer'])}</dd>" for a in answer_business_questions(profile)
    )

    return f"""
<details class="company">
  <summary><span>{title}</span><span class="meta">{_esc(profile.org_number)}</span></summary>
  <div class="company-body">
    <table>{rows}</table>
    <dl class="synthesis">{synthesis_items}</dl>
  </div>
</details>
"""


def render_html_report(profiles: list[CompanyProfile], generated_at: datetime) -> str:
    """One self-contained, responsive HTML page covering every profile in
    `profiles`. Never fabricates content -- purely renders existing Claims
    and src/synthesis.py's templated answers."""
    sections = "".join(_company_section(p) for p in profiles)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Signalpost batch report</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>Signalpost batch report</h1>
<p class="meta">Generated {_esc(generated_at.isoformat())} &middot; {len(profiles)} companies</p>
{sections}
</body>
</html>
"""
