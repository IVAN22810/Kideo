"""Idempotent demo-data seed for fresh deployments (e.g. Render).

Runs at app startup via lifespan. If an active account already exists, this is
a no-op. Otherwise it creates the canonical "Alice -> Carol, NY UTMA, $75
balance, $30 per-withdrawal ceiling" demo couple end-to-end through the same
business logic the API uses, so the seed data is always shape-valid.

Why through the routers and not raw DB inserts: the seeded chain has to pass
COPPA consent, KYC, the state-rules ceiling computation, and the funding-source
validation — duplicating any of that in raw SQL would be a perfect place for
the seed and the API to drift apart.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models import (
    Account,
    AccountStatus,
    AccountType,
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
from app.services.compliance import log_event
from app.services.state_rules import get_termination_age


DEMO_PARENT_EMAIL = "alice.demo@minor.dev"


def seed_demo_data_if_empty(session: Session) -> None:
    """No-op if any active account exists; otherwise create the demo chain."""
    existing = session.exec(
        select(Account).where(Account.status == AccountStatus.active).limit(1)
    ).first()
    if existing is not None:
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

    session.commit()
