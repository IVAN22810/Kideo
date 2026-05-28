# Changelog

Notable compliance and behavior changes to Kideo. Dates are ISO format
(YYYY-MM-DD). Entries here are auditable: customers, partner banks, and
investors should be able to reconstruct what changed when, and why.

## 2026-05-28 — Wordmark cleanup: Minor / Minor AI → Kideo (user-facing UI + API response identifiers)

Replaced the lingering "Minor" / "Minor AI" brand references with "Kideo" on
user-facing surfaces:

- `app/main.py`: `FastAPI(title="Minor API")` → `title="Kideo API"` (renders on `/docs` and in `/openapi.json`)
- `app/templates/base.html`: default `<title>` block
- `app/templates/index.html`: `<title>` + hero wordmark
- `app/templates/parent_dashboard.html`: `<title>` + navbar wordmark
- `app/templates/child_dashboard.html`: `<title>` + navbar wordmark + "Minor AI Mentor" → "Kideo AI Mentor" (twice: heading and greeting)

Two **API response body values** also updated (previously flagged as ambiguous
in the first wordmark commit; now resolved — no consumers were found that
read or assert these strings, verified via repo-wide grep):

- `GET /health`: `{"service": "minor-api", ...}` → `{"service": "kideo-api", ...}`
- `GET /v1`: `{"api": "minor", ...}` → `{"api": "kideo", ...}`

These are minor breaking changes to API contract — any external monitoring or
client that asserted `service == "minor-api"` or `api == "minor"` will need
an update. Search of this repo (frontend JS, `demo/agent.py`, `demo/run_demo.py`,
`tests/`, `render.yaml`) showed only status-code checks on `/health`; no
external consumers known.

The legal/domain term **"minor"** (meaning a person under 18) remains
**untouched** everywhere it appears as such — including the `MinorAPIError`
class and all its imports/raises, `MinorAgent`, every `*_minor_units` API/DB
field, the "Uniform Transfers to Minors Act" statute citations in
`state_rules.py`, the lowercase "custodial accounts for minors" tagline on
`/`, and all Python docstrings and comments that use "minor" in its legal sense.

## 2026-05-27 — UTMA termination age corrections (uncertified)

Research-based corrections to `app/services/state_rules.py` based on current
statute review:

- **LA**: `termination_age_default` 18 → 25 (per Act 60 of 2023, eff. Aug 1, 2023)
- **VA**: `termination_age_default` 21 → 18 (per Va. Code §64.2-1919(A))

These values are research-grounded but **NOT yet verified by counsel**.
`is_verified_by_counsel = False` for all states. Sources documented inline in
`state_rules.py`.
