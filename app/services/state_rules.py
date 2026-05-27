"""Per-state UTMA rules engine.

WARNING — LEGAL VERIFICATION REQUIRED BEFORE PRODUCTION USE.

Every termination age in this table is a PLACEHOLDER marked `# verify`.
The exact age varies by state, by amendments to each state's UTMA statute,
and by whether the donor / custodian elected an extension at account creation.
The values below are plausible defaults selected to make the engine demonstrably
per-state (different states yield different ages), but they MUST be confirmed
against the current statute by counsel before any account is opened against
real funds.

Notes for the future production version:
  - Many states allow a donor-elected extension up to age 25 (e.g. California
    Probate Code §3920.5). Model this as an optional `extended_termination_age`
    set at account-creation time, distinct from the statutory default below.
  - Some states (Louisiana, South Carolina, Vermont, Virginia) historically did
    not adopt UTMA in standard form; their custodial regimes are sui generis.
  - The data below is a 6-state subset; full coverage requires all 50 states
    plus DC and the territories.

This module is intentionally deterministic and offline (no network calls, no
external data sources) so the rules are reproducible across environments.
"""
from typing import TypedDict


class StateRules(TypedDict):
    """UTMA rules for a single US state, addressed by 2-letter code."""

    state_code: str
    state_name: str
    termination_age: int  # statutory default; extension may apply, see module docstring
    statute_reference: str


# TODO: legal review required for every entry before production use.
# All termination ages are placeholders; verify against current state statute,
# track amendments, and add the optional extension mechanism per state.
_STATE_RULES: dict[str, StateRules] = {
    "CA": {
        "state_code": "CA",
        "state_name": "California",
        "termination_age": 18,  # verify: California Probate Code §3920 (statutory default)
        "statute_reference": "California Uniform Transfers to Minors Act (Probate Code §§3900-3925)",
    },
    "NY": {
        "state_code": "NY",
        "state_name": "New York",
        "termination_age": 21,  # verify: NY EPTL §7-6.21
        "statute_reference": "New York Uniform Transfers to Minors Act (EPTL §§7-6.1 to 7-6.26)",
    },
    "FL": {
        "state_code": "FL",
        "state_name": "Florida",
        "termination_age": 21,  # verify: Fla. Stat. §710.123
        "statute_reference": "Florida Uniform Transfers to Minors Act (Fla. Stat. §§710.101-710.126)",
    },
    "NV": {
        "state_code": "NV",
        "state_name": "Nevada",
        "termination_age": 18,  # verify: NRS §167.090
        "statute_reference": "Nevada Uniform Transfers to Minors Act (NRS Chapter 167)",
    },
    "LA": {
        "state_code": "LA",
        "state_name": "Louisiana",
        "termination_age": 18,  # verify: La. R.S. §9:763 (Louisiana follows civil-law tradition)
        "statute_reference": "Louisiana Uniform Transfers to Minors Act (La. R.S. §§9:751-9:773)",
    },
    "VA": {
        "state_code": "VA",
        "state_name": "Virginia",
        "termination_age": 21,  # verify: Va. Code §64.2-1908
        "statute_reference": "Virginia Uniform Transfers to Minors Act (Va. Code §§64.2-1900 to 64.2-1922)",
    },
}


def get_state_rules(state_code: str) -> StateRules:
    """Look up UTMA rules for a US state by 2-letter code.

    Raises KeyError if the state is not in the rules table; callers should
    convert to a MinorAPIError with code='unsupported_governing_state'.
    """
    key = (state_code or "").upper()
    if key not in _STATE_RULES:
        raise KeyError(state_code)
    return _STATE_RULES[key]


def get_termination_age(state_code: str) -> int:
    """Return the statutory UTMA termination age for a state.

    See the module docstring for the verification requirement.
    """
    return get_state_rules(state_code)["termination_age"]


def supported_states() -> list[str]:
    """List the 2-letter codes the engine currently knows about."""
    return sorted(_STATE_RULES.keys())
