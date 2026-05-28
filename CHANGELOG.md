# Changelog

Notable compliance and behavior changes to Kideo. Dates are ISO format
(YYYY-MM-DD). Entries here are auditable: customers, partner banks, and
investors should be able to reconstruct what changed when, and why.

## 2026-05-28 — Wordmark cleanup: Minor / Minor AI → Kideo (user-facing UI only)

Replaced the lingering "Minor" / "Minor AI" brand references with "Kideo" on
user-facing surfaces only:

- `app/main.py`: `FastAPI(title="Minor API")` → `title="Kideo API"` (renders on `/docs` and in `/openapi.json`)
- `app/templates/base.html`: default `<title>` block
- `app/templates/index.html`: `<title>` + hero wordmark
- `app/templates/parent_dashboard.html`: `<title>` + navbar wordmark
- `app/templates/child_dashboard.html`: `<title>` + navbar wordmark + "Minor AI Mentor" → "Kideo AI Mentor" (twice: heading and greeting)

The legal/domain term **"minor"** (meaning a person under 18) is **untouched**
everywhere it appears as such — including the `MinorAPIError` class and all
its imports/raises, `MinorAgent`, every `*_minor_units` API/DB field, the
"Uniform Transfers to Minors Act" statute citations in `state_rules.py`, the
lowercase "custodial accounts for minors" tagline on `/`, and all Python
docstrings and comments that use "minor" in its legal sense.

Two values inside API response bodies remain unchanged pending product
decision (flagged in PR notes): `GET /health` returns `"service":
"minor-api"` and `GET /v1` returns `"api": "minor"`. Changing these would be
a contract change for any external monitoring; left as-is until a deliberate
breaking-change decision is made.

## 2026-05-27 — UTMA termination age corrections (uncertified)

Research-based corrections to `app/services/state_rules.py` based on current
statute review:

- **LA**: `termination_age_default` 18 → 25 (per Act 60 of 2023, eff. Aug 1, 2023)
- **VA**: `termination_age_default` 21 → 18 (per Va. Code §64.2-1919(A))

These values are research-grounded but **NOT yet verified by counsel**.
`is_verified_by_counsel = False` for all states. Sources documented inline in
`state_rules.py`.
