# Changelog

Notable compliance and behavior changes to Kideo. Dates are ISO format
(YYYY-MM-DD). Entries here are auditable: customers, partner banks, and
investors should be able to reconstruct what changed when, and why.

## 2026-05-27 — UTMA termination age corrections (uncertified)

Research-based corrections to `app/services/state_rules.py` based on current
statute review:

- **LA**: `termination_age_default` 18 → 25 (per Act 60 of 2023, eff. Aug 1, 2023)
- **VA**: `termination_age_default` 21 → 18 (per Va. Code §64.2-1919(A))

These values are research-grounded but **NOT yet verified by counsel**.
`is_verified_by_counsel = False` for all states. Sources documented inline in
`state_rules.py`.
