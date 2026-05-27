"""Per-state UTMA rules engine.

WARNING — LEGAL VERIFICATION REQUIRED BEFORE PRODUCTION USE.

Every termination age in this table is a PLACEHOLDER. None of the entries
have been signed off by counsel — that's exactly what the
`is_verified_by_counsel` boolean tracks. Use `is_state_verified(state)` as a
guard before letting real money flow against an account in a given state.

Schema:
  termination_age_default      — Statutory baseline. The age at which a
                                 vanilla UTMA custodianship terminates absent
                                 a donor-elected extension.
  extended_termination_age_max — Highest age to which the donor may have
                                 elected to extend custodianship at the time
                                 of the transfer (state-statute cap). `None`
                                 means the state does not permit extension.
                                 The PER-ACCOUNT extended age (when one is
                                 elected) belongs on the Account model, not
                                 here — see app/models.py Account.
  statute_reference            — Plain-English statute citation.
  statute_last_checked_at      — ISO date this entry was last read against
                                 a primary source. `None` until research lands.
  statute_last_checked_source_url
                               — The URL of the source that was actually
                                 read on `statute_last_checked_at`. `None`
                                 until research lands.
  is_verified_by_counsel       — FALSE until an attorney signs off on this
                                 specific entry against current statute. The
                                 verify markers (`# verify`) stay until this
                                 flips True.
  verified_by_counsel_at       — ISO date of the counsel memo. `None` until
                                 verification.
  verified_by_counsel_notes    — Free-text memo reference (matter number,
                                 attorney name, etc.). `None` until verified.
  caveats                      — Free-text state-specific notes that don't
                                 fit other fields (e.g. "non-UTMA, sui generis").

This module is deterministic and offline (no network, no external data
sources) so the rules are reproducible across environments. Web research that
populates `statute_last_checked_*` runs at edit time, not at runtime.

# ─────────────────────────────────────────────────────────────────────────
# RESEARCH METHODOLOGY NOTE
# ─────────────────────────────────────────────────────────────────────────
# The RESEARCH NOTES block above each entry below was populated on
# 2026-05-27 via WebSearch + WebFetch against the cited primary sources.
# This is NOT legal advice and NOT counsel verification — it's a
# best-effort audit trail intended to make counsel review faster (~30
# minutes vs ~4 hours of fresh statute research). Every entry still has
# is_verified_by_counsel=False until an attorney signs off.
#
# Two material corrections were identified during research:
#   • LA: placeholder default was 18; current statute (R.S. 9:770(1) as
#         amended by Act 60 of 2023, effective Aug 1, 2023) is 25.
#   • VA: placeholder default was 21; current statute (§64.2-1919(A)) is
#         18 — the 21/25 ages only apply if the transferor specifically
#         elected an extension under §64.2-1908(D) or (E).
# ─────────────────────────────────────────────────────────────────────────
"""
from typing import Optional, TypedDict


class StateRules(TypedDict):
    """UTMA rules for a single US state, addressed by 2-letter code."""

    state_code: str
    state_name: str
    termination_age_default: int            # statutory baseline; see module docstring
    extended_termination_age_max: Optional[int]  # None = state does not permit donor-elected extension
    statute_reference: str
    statute_last_checked_at: Optional[str]      # ISO date (e.g. "2026-05-27") of last source read
    statute_last_checked_source_url: Optional[str]
    is_verified_by_counsel: bool                # FALSE until an attorney signs off
    verified_by_counsel_at: Optional[str]       # ISO date
    verified_by_counsel_notes: Optional[str]    # memo reference / matter number
    caveats: Optional[str]                      # state-specific quirks not captured elsewhere


# Every entry below carries is_verified_by_counsel=False. The `# verify`
# marker on each termination_age_default reinforces that until counsel
# confirms, these values are best-effort placeholders.
_STATE_RULES: dict[str, StateRules] = {

    # RESEARCH NOTES (counsel review required):
    # - Statutory default: 18 per Cal. Prob. Code §3920 (silent transfers default to 18).
    # - Donor extension permitted: YES. §3920.5 caps depend on transfer type:
    #     §3903/§3905 transfers → up to age 25
    #     §3904 irrevocable gift → up to age 21
    #     §3906 trustee transfer → up to age 25
    # - Last checked: 2026-05-27
    # - Source: https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=PROB&sectionNum=3920.5
    # - Notes: extended_termination_age_max here is 25 (overall statutory ceiling),
    #   but for our parent-funded deposit flow the most relevant transfer type is
    #   likely §3904 irrevocable gift, which caps at 21. Counsel should confirm
    #   which CA Prob. Code section governs our deposit mechanic before relying
    #   on the 25-year ceiling. No material amendments noted in 2024-2026 cycle.
    "CA": {
        "state_code": "CA",
        "state_name": "California",
        "termination_age_default": 18,                  # verify
        "extended_termination_age_max": 25,             # verify — see notes for per-section caps
        "statute_reference": "California Uniform Transfers to Minors Act (Probate Code §§3900-3925); extension under §3920.5",
        "statute_last_checked_at": "2026-05-27",
        "statute_last_checked_source_url": "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=PROB&sectionNum=3920.5",
        "is_verified_by_counsel": False,
        "verified_by_counsel_at": None,
        "verified_by_counsel_notes": None,
        "caveats": None,
    },

    # RESEARCH NOTES (counsel review required):
    # - Statutory default: 21 per NY EPTL §7-6.20 for transfers under §7-6.4
    #   (lifetime irrevocable gift) and §7-6.5. For §7-6.6 (obligor) and §7-6.7
    #   (court-ordered) transfers the termination age is 18.
    # - Donor extension permitted: NO. NY does NOT permit extension beyond 21.
    #   §7-6.21 provides the OPPOSITE — an opt-DOWN election where the donor
    #   can elect "until age eighteen" at gift creation, making the gift
    #   administered under the part as if "eighteen" were substituted for
    #   "twenty-one".
    # - Last checked: 2026-05-27
    # - Source: https://www.nysenate.gov/legislation/laws/EPT/7-6.20
    # - Notes: NY is the unusual state — no extension upward. For our deposit
    #   flow (parent funds account = lifetime irrevocable gift, §7-6.4) the
    #   default is 21 with no extension option. Counsel should also confirm:
    #   (1) whether the §7-6.21 opt-down to 18 needs UI support, and (2)
    #   whether our deposit mechanic counts as §7-6.4 (irrevocable gift) or
    #   another section that would change the default age.
    "NY": {
        "state_code": "NY",
        "state_name": "New York",
        "termination_age_default": 21,                  # verify — assumes §7-6.4 irrevocable gift transfers
        "extended_termination_age_max": None,           # NY does not permit extension upward
        "statute_reference": "New York Uniform Transfers to Minors Act (EPTL §§7-6.1 to 7-6.26); termination per §7-6.20; opt-down election per §7-6.21",
        "statute_last_checked_at": "2026-05-27",
        "statute_last_checked_source_url": "https://www.nysenate.gov/legislation/laws/EPT/7-6.20",
        "is_verified_by_counsel": False,
        "verified_by_counsel_at": None,
        "verified_by_counsel_notes": None,
        "caveats": (
            "NY does not permit extension above statutory default. Donor may "
            "elect opt-down to 18 at gift creation via the §7-6.21 'until age "
            "eighteen' phrase — needs UI support if exposed to customers."
        ),
    },

    # RESEARCH NOTES (counsel review required):
    # - Statutory default: 21 per Fla. Stat. §710.123 for transfers under
    #   §710.105 (lifetime irrevocable gift) and §710.106. For §710.107/§710.108
    #   (obligor / court-ordered) transfers the termination age is 18.
    # - Donor extension permitted: YES, up to 25 per §710.123 — BUT for
    #   custodianships created by irrevocable gift, the minor has an absolute
    #   statutory right to compel immediate distribution at age 21 regardless
    #   of any age-25 election. The custodian must give the minor written
    #   notice in a narrow window (delivered at least 30 days before, and not
    #   later than 30 days after, the minor's 21st birthday) for the age-25
    #   election to be enforceable.
    # - Last checked: 2026-05-27
    # - Source: https://www.leg.state.fl.us/Statutes/index.cfm?App_mode=Display_Statute&URL=0700-0799/0710/Sections/0710.123.html
    # - Notes: The "25" extension is partly illusory for irrevocable gifts —
    #   minor can override at 21. If our deposit flow = irrevocable gift, the
    #   effective max age for unilateral custodian control is 21, not 25, and
    #   the notification window must be tracked by our system. Counsel should
    #   decide whether to expose the age-25 option at all or simplify to 21.
    "FL": {
        "state_code": "FL",
        "state_name": "Florida",
        "termination_age_default": 21,                  # verify — assumes §710.105 irrevocable gift
        "extended_termination_age_max": 25,             # verify — minor may compel at 21 for irrevocable gifts
        "statute_reference": "Florida Uniform Transfers to Minors Act (Fla. Stat. §§710.101-710.126); termination per §710.123",
        "statute_last_checked_at": "2026-05-27",
        "statute_last_checked_source_url": "https://www.leg.state.fl.us/Statutes/index.cfm?App_mode=Display_Statute&URL=0700-0799/0710/Sections/0710.123.html",
        "is_verified_by_counsel": False,
        "verified_by_counsel_at": None,
        "verified_by_counsel_notes": None,
        "caveats": (
            "Extension to 25 is partly illusory for irrevocable gifts — minor "
            "has absolute right under §710.123 to compel immediate distribution "
            "at 21. A custodial-notification window (30 days before/after the "
            "21st birthday) must be tracked if the age-25 election is to remain "
            "enforceable."
        ),
    },

    # RESEARCH NOTES (counsel review required):
    # - Statutory default: 18 per NRS 167.095 / NRS 167.030 (custodial property
    #   transferred under NRS 167.023, 167.025, 167.027 or 167.029).
    # - Donor extension permitted: YES, up to 25 per NRS 167.034. Specifically:
    #     NRS 167.025 / 167.033 transfers → may delay until age 25
    #     NRS 167.023 transfers → may delay until 25 ONLY if the transfer is by
    #         irrevocable exercise of a power of appointment
    #   Specific transfer-designation language is required to elect the delay.
    # - Last checked: 2026-05-27
    # - Source: https://law.justia.com/codes/nevada/chapter-167/statute-167-034/
    # - Notes: NV is unusual as a "default 18" state — most UTMA jurisdictions
    #   default to 21. Counsel should confirm which NRS section governs our
    #   deposit mechanic — if §167.027 or §167.029, no extension is available
    #   per §167.034 and the account terminates at 18 with no override.
    "NV": {
        "state_code": "NV",
        "state_name": "Nevada",
        "termination_age_default": 18,                  # verify
        "extended_termination_age_max": 25,             # verify — restrictions per transfer type
        "statute_reference": "Nevada Uniform Transfers to Minors Act (NRS Chapter 167); default per §167.095, extension per §167.034",
        "statute_last_checked_at": "2026-05-27",
        "statute_last_checked_source_url": "https://law.justia.com/codes/nevada/chapter-167/statute-167-034/",
        "is_verified_by_counsel": False,
        "verified_by_counsel_at": None,
        "verified_by_counsel_notes": None,
        "caveats": None,
    },

    # TODO: counsel must decide whether to reject LA accounts at creation or
    # model UGTA separately. See caveats — the historical "non-UTMA, sui
    # generis" framing in earlier versions of this file appears to have been
    # overstated; LA does have a named UTMA statute, and Act 60 of 2023 raised
    # the termination age to 25. But Louisiana's civil-law tradition may
    # introduce material differences (usufruct, forced heirship) that are not
    # captured by simply tracking termination_age_default.
    #
    # RESEARCH NOTES (counsel review required):
    # - Statutory default: 25 per La. R.S. 9:770(1), as amended by Act 60 of
    #   the 2023 Regular Session (HB 142, Beaullieu), effective Aug 1, 2023.
    #   This is a MATERIAL CORRECTION — previous placeholder was 18.
    # - Donor extension permitted: not separately modeled by Act 60 — the act
    #   raised the default itself from 18 to 25 rather than establishing a
    #   default-with-extension framework. Counsel should verify whether the
    #   full amended statute contains a separate extension provision.
    # - Last checked: 2026-05-27
    # - Source: https://www.legis.la.gov/Legis/ViewDocument.aspx?d=1331228 (Act 60 enrolled text)
    # - Notes: Act 60 applies prospectively to all UTMA accounts and to any
    #   custodial property held under the LA UTMA still to transfer (i.e.
    #   pre-Act-60 accounts get the new age 25 too — counsel verify). The
    #   earlier characterization of LA as "non-UTMA / sui generis" is at least
    #   partially incorrect: LA has named UTMA in current statute. However,
    #   counsel must still verify whether (a) LA UTMA has Louisiana-specific
    #   quirks (usufruct interactions, forced heirship), (b) whether to keep
    #   accepting LA accounts under our standard flow, and (c) whether the
    #   25-year termination causes any tax-reporting (1099) timing issues.
    "LA": {
        "state_code": "LA",
        "state_name": "Louisiana",
        "termination_age_default": 25,                  # verify — CHANGED FROM PLACEHOLDER (was 18); Act 60 of 2023
        "extended_termination_age_max": None,           # verify — Act 60 raised default; no extension framework noted
        "statute_reference": "Louisiana Uniform Transfers to Minors Act (La. R.S. §§9:751-9:773); termination per §9:770(1) as amended by Act 60 of 2023 Regular Session (HB 142)",
        "statute_last_checked_at": "2026-05-27",
        "statute_last_checked_source_url": "https://www.legis.la.gov/Legis/ViewDocument.aspx?d=1331228",
        "is_verified_by_counsel": False,
        "verified_by_counsel_at": None,
        "verified_by_counsel_notes": None,
        "caveats": (
            "LA UTMA termination age was 18 before Act 60 of 2023 (effective "
            "Aug 1, 2023); now 25. Act applies prospectively to new and pre-Act "
            "accounts whose property has not yet transferred. Louisiana's "
            "civil-law tradition (usufruct, forced heirship) may introduce "
            "material differences from common-law UTMA states. Counsel must "
            "decide: (a) keep accepting LA accounts under standard flow, "
            "(b) reject LA at account creation, or (c) model UGTA-equivalent "
            "as a separate account_type with LA-specific behavior."
        ),
    },

    # RESEARCH NOTES (counsel review required):
    # - Statutory default: 18 per Va. Code §64.2-1919(A) — "the earlier of:
    #   1. The minor's attainment of 18 years of age". This is a MATERIAL
    #   CORRECTION — previous placeholder was 21.
    # - Donor extension permitted: YES, up to 21 or 25 depending on which
    #   subsection of §64.2-1908 was elected at transfer time (subsection D
    #   permits up to 21; subsection E up to 25). For age-25 transfers, the
    #   minor has a right under §64.2-1919(B) to compel termination at 21 by
    #   delivering a written request within a 60-day window centered on the
    #   21st birthday.
    # - Last checked: 2026-05-27
    # - Source: https://law.lis.virginia.gov/vacode/title64.2/chapter19/section64.2-1919/
    # - Notes: Most recent amendment per source: 2019, Chapter 527. The
    #   placeholder 21 was likely chosen as the "common case" age for VA UTMA
    #   accounts where parents elect extension, but the statute's default is
    #   clearly 18. If our deposit flow does not explicitly elect §64.2-1908(D)
    #   or (E), accounts will terminate at 18 — counsel should confirm whether
    #   we want to default-elect extension or accept the statutory 18.
    "VA": {
        "state_code": "VA",
        "state_name": "Virginia",
        "termination_age_default": 18,                  # verify — CHANGED FROM PLACEHOLDER (was 21); Va. Code §64.2-1919(A)
        "extended_termination_age_max": 25,             # verify — minor may compel at 21 for §64.2-1908(E) age-25 transfers
        "statute_reference": "Virginia Uniform Transfers to Minors Act (Va. Code §§64.2-1900 to 64.2-1922); termination per §64.2-1919; extension election per §64.2-1908(D)/(E)",
        "statute_last_checked_at": "2026-05-27",
        "statute_last_checked_source_url": "https://law.lis.virginia.gov/vacode/title64.2/chapter19/section64.2-1919/",
        "is_verified_by_counsel": False,
        "verified_by_counsel_at": None,
        "verified_by_counsel_notes": None,
        "caveats": (
            "Va. Code §64.2-1919(B) gives the minor an absolute right to compel "
            "termination at 21 for age-25 transfers, via written request in a "
            "60-day window centered on the 21st birthday. The notification "
            "window must be tracked by our system if age-25 transfers are exposed."
        ),
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

    This is the DEFAULT age (no donor-elected extension). For the per-account
    extended age, see Account.extended_termination_age (when that field is
    added) or query `get_extended_termination_age_max(state)` for the cap.

    See the module docstring for the verification requirement.
    """
    return get_state_rules(state_code)["termination_age_default"]


def get_extended_termination_age_max(state_code: str) -> Optional[int]:
    """Return the statutory maximum age to which a donor may extend custodianship.

    `None` means the state's statute does not permit extension (or the value
    is not yet known — check `is_state_verified(state)` to disambiguate).
    """
    return get_state_rules(state_code)["extended_termination_age_max"]


def is_state_verified(state_code: str) -> bool:
    """True iff an attorney has signed off on this state's entry against current statute.

    Use this as a guard before letting real money flow against an account in
    a given state. Returns False for every state until counsel verification
    lands and the `is_verified_by_counsel` flag flips True in `_STATE_RULES`.
    """
    return get_state_rules(state_code)["is_verified_by_counsel"]


def supported_states() -> list[str]:
    """List the 2-letter codes the engine currently knows about.

    Note: presence here means we model the state, NOT that counsel has
    verified its entry. Always pair with `is_state_verified()` before
    production use.
    """
    return sorted(_STATE_RULES.keys())
