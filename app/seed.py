"""Idempotent demo-data seed for fresh deployments (e.g. Render).

Runs at app startup via lifespan. Behavior:

  • Fresh DB (no active account) — creates the canonical "Alice -> Carol, NY
    UTMA, $75 balance, $30 per-withdrawal ceiling" demo couple AND mints a
    demo API key for it.
  • Already-seeded DB (restart, persistent storage) — does NOT re-create the
    couple, but DOES mint a fresh demo API key for the existing demo account
    so /demo has a working key to display. Old key hashes stay in the table
    but become unreachable (we no longer hold the plaintext).

The plaintext key is stashed in the module-level `DEMO_API_KEY` global so the
/demo route can render it. It is never written to disk in plaintext — only the
SHA-256 hash hits the DB, same as customer-issued keys.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.models import (
    Account,
    AccountStatus,
    AccountType,
    ApiKey,
    Child,
    ComplianceEventType,
    Consent,
    ConsentMethod,
    ConsentStatus,
    FundingSource,
    KYCStatus,
    Parent,
    Transaction,
)
from app.services.api_keys import (
    extract_prefix,
    generate_plaintext_key,
    hash_key,
)
from app.services.compliance import log_event
from app.services.state_rules import get_termination_age


DEMO_PARENT_EMAIL = "alice.demo@minor.dev"

# Plaintext of the demo's current API key. Populated by seed_demo_data_if_empty
# at app startup; consumed by the /demo route to render the copy-paste banner
# and inject the key into the in-page JS so dashboard fetch() calls authenticate.
# Lives only in memory — never persisted in plaintext.
DEMO_API_KEY: Optional[str] = None


def _issue_api_key_for_account(session: Session, account_id: str) -> str:
    """Generate, persist (hashed), and audit-log a fresh API key for an account.

    Returns the plaintext exactly once. Used by the seed for the demo account;
    the same atomic-with-audit pattern lives in app/routers/accounts.py for the
    customer-issuance path. Caller is responsible for the surrounding commit.
    """
    plaintext = generate_plaintext_key()
    row = ApiKey(
        account_id=account_id,
        prefix=extract_prefix(plaintext),
        key_hash=hash_key(plaintext),
    )
    session.add(row)
    session.flush()  # populate row.id for the audit event

    log_event(
        session=session,
        event_type=ComplianceEventType.api_key_created,
        entity_type="api_key",
        entity_id=row.id,
        actor_id="system",
        payload={
            "source": "seed",
            "account_id": account_id,
            "prefix": row.prefix,
            # Intentionally never persist plaintext or hash in the audit payload.
        },
        regulatory_reference=(
            "Demo bootstrap API key issued at startup; plaintext held in process "
            "memory for /demo display only, hash persisted, never logged."
        ),
    )
    return plaintext


def seed_demo_data_if_empty(session: Session) -> None:
    """Ensure the demo couple + a fresh demo API key exist.

    On a fresh DB this runs the full seed and mints a key. On a DB that
    already has an active account, the couple is left alone but a NEW key
    is issued (the old in-memory plaintext is gone after restart and the
    /demo banner needs something to display).
    """
    global DEMO_API_KEY

    existing = session.exec(
        select(Account).where(Account.status == AccountStatus.active).limit(1)
    ).first()
    if existing is not None:
        # DB is already seeded (local dev, or a restart with persistent disk).
        # Mint a new demo key so /demo has a working one to render.
        DEMO_API_KEY = _issue_api_key_for_account(session, existing.id)
        session.commit()
        return

    now = datetime.now(timezone.utc)

    # ── Parent (Alice) — KYC verified so account creation passes ──────────
    parent = Parent(
        email=DEMO_PARENT_EMAIL,
        legal_first_name="Alice",
        legal_last_name="Carter",
        date_of_birth=datetime(1985, 6, 15, tzinfo=timezone.utc),
        state="NY",
        country="US",
        kyc_status=KYCStatus.verified,
    )
    session.add(parent)
    session.flush()

    # ── Child (Carol, ~12 years old) ──────────────────────────────────────
    child = Child(
        parent_id=parent.id,
        legal_first_name="Carol",
        legal_last_name="Carter",
        date_of_birth=datetime(2014, 4, 1, tzinfo=timezone.utc),
        state_of_residence="NY",
    )
    session.add(child)
    session.flush()

    # ── Account (NY UTMA, $30 per-withdrawal ceiling) ─────────────────────
    governing_state = "NY"
    account = Account(
        parent_id=parent.id,
        child_id=child.id,
        account_type=AccountType.utma,
        governing_state=governing_state,
        termination_age=get_termination_age(governing_state),
        status=AccountStatus.active,   # seed already-consented for instant demo
        activated_at=now,
        spending_ceiling_minor_units=3000,  # $30.00
        balance_minor_units=0,              # we'll deposit $75 below
    )
    session.add(account)
    session.flush()

    # ── Consent record (granted) — completes the COPPA audit trail ────────
    consent = Consent(
        account_id=account.id,
        parent_id=parent.id,
        method=ConsentMethod.credit_card_verification,
        status=ConsentStatus.granted,
        disclosure_version="2026-05",
        granted_at=now,
        ip_address="127.0.0.1",
        user_agent="seed",
    )
    session.add(consent)

    # ── Funding source (Chase ****4242, verified) ─────────────────────────
    funding = FundingSource(
        parent_id=parent.id,
        bank_name="Chase",
        last_four="4242",
        status="verified",
    )
    session.add(funding)
    session.flush()

    # ── Opening deposit: $75.00 — gives the demo a balance to draw from ───
    deposit = Transaction(
        account_id=account.id,
        funding_source_id=funding.id,
        amount_minor_units=7500,
        type="deposit",
        status="succeeded",
        initiator="parent",
        description="Seed: opening deposit",
    )
    session.add(deposit)
    account.balance_minor_units = 7500
    account.updated_at = now
    session.flush()

    # ── Audit events for each step (the regulatory paper trail) ───────────
    log_event(
        session=session,
        event_type=ComplianceEventType.parent_created,
        entity_type="parent",
        entity_id=parent.id,
        actor_id="system",
        payload={"source": "seed"},
        regulatory_reference="Seed data — onboarding baseline",
    )
    log_event(
        session=session,
        event_type=ComplianceEventType.child_created,
        entity_type="child",
        entity_id=child.id,
        actor_id="system",
        payload={"source": "seed", "parent_id": parent.id},
        regulatory_reference="Seed data — COPPA 16 CFR §312",
    )
    log_event(
        session=session,
        event_type=ComplianceEventType.account_created,
        entity_type="account",
        entity_id=account.id,
        actor_id="system",
        payload={
            "source": "seed",
            "governing_state": governing_state,
            "termination_age": account.termination_age,
            "spending_ceiling_minor_units": account.spending_ceiling_minor_units,
        },
        regulatory_reference=f"Seed data — UTMA {governing_state}",
    )
    log_event(
        session=session,
        event_type=ComplianceEventType.consent_granted,
        entity_type="consent",
        entity_id=consent.id,
        actor_id=parent.id,
        payload={"source": "seed", "method": consent.method.value},
        regulatory_reference="COPPA 16 CFR §312.5 — Verifiable Parental Consent",
    )
    log_event(
        session=session,
        event_type=ComplianceEventType.account_consented,
        entity_type="account",
        entity_id=account.id,
        actor_id=parent.id,
        payload={"source": "seed", "consent_id": consent.id},
        regulatory_reference="COPPA 16 CFR §312.5 — VPC granted, account activated",
    )
    log_event(
        session=session,
        event_type=ComplianceEventType.funding_source_added,
        entity_type="funding_source",
        entity_id=funding.id,
        actor_id=parent.id,
        payload={"source": "seed", "bank_name": funding.bank_name, "last_four": funding.last_four},
        regulatory_reference="Seed data — funding source linked",
    )
    log_event(
        session=session,
        event_type=ComplianceEventType.account_funded,
        entity_type="account",
        entity_id=account.id,
        actor_id=parent.id,
        payload={
            "source": "seed",
            "transaction_id": deposit.id,
            "amount_minor_units": deposit.amount_minor_units,
            "new_balance_minor_units": account.balance_minor_units,
        },
        regulatory_reference="Seed data — opening deposit",
    )

    # Mint the demo's API key in the SAME transaction as the account. Plaintext
    # is held in the module global below; commit finalizes the hash row + audit.
    DEMO_API_KEY = _issue_api_key_for_account(session, account.id)

    session.commit()
