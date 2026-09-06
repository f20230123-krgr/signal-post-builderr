# Registry fixtures

`*.json` files named by org number (e.g. `923609016.json`) are REAL, recorded
responses from `data.brreg.no`'s public Enhetsregisteret API, captured with
`fixtures/record_brreg_fixtures.py`. Shape: `{"status": <http status>, "body": <parsed JSON or null>}`.

`roller/`, `regnskap/`, `underenheter/` hold the same shape, recorded from
Brreg's roles, annual-accounts, and sub-units endpoints respectively (see
`src/pipeline/registry_extras.py`) — same official registry, same recording
script, just three more endpoints per org number.

`ambiguous_synthetic.json` is NOT real -- Brønnøysundregisteret's lookup-by-org-number
endpoint is a unique-key lookup and cannot itself return an ambiguous match for
a single org number. It is hand-authored to exercise resolve()'s ambiguity
rule: a record whose `historiskeNavn` shows more than one entry with
`tilDato: null` (i.e. more than one name simultaneously claiming to be
"current" -- a data inconsistency real records don't exhibit) must resolve to
`ambiguous` rather than guessing which name is current.
