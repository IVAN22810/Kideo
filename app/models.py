"""SQLModel tables for Minor's UTMA/UGMA compliance schema.

Domain:
  Parent           — adult custodian (legal authority)
  Child            — minor beneficiary
  Account          — UTMA/UGMA container linking one Parent + one Child
  Consent          — COPPA Verifiable Parental Consent audit record
  ComplianceEvent  — immutable, append-only regulatory event log

IDs: Stripe-style {prefix}_{24-char base62 token}, e.g. acc_8KqWp...
"""
import secrets
import string
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, Relationship, SQLModel


# ──────────────────────────────────────────────────────────────────────────
# ID generation — Stripe-style prefixed base62 tokens
# ──────────────────────────────────────────────────────────────────────────

_ID_ALPHABET = string.ascii_letters + string.digits  # base62
_ID_BODY_LENGTH = 24


def generate_id(prefix: str) -> str:
    """{prefix}_{24-char base62 token}. Cryptographically secure."""
    body = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_BODY_LENGTH))
    return f"{prefix}_{body}"


def parent_id() -> str:
    return generate_id("par")


def child_id() -> str:
    return generate_id("chd")


def account_id() -> str:
    return generate_id("acc")


def consent_id() -> str:
    return generate_id("con")


def event_id() -> str:
    return generate_id("evt")


def funding_source_id() -> str:
    return generate_id("ba")


def transaction_id() -> str:
    return generate_id("tx")


def api_key_id() -> str:
    return generate_id("ak")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────────


class KYCStatus(str, Enum):
    not_started = "not_started"
    submitted = "submitted"
    verified = "verified"
    failed = "failed"


class GuardianRelationship(str, Enum):
    parent = "parent"
    legal_guardian = "legal_guardian"
    grandparent = "grandparent"


class AccountType(str, Enum):
    utma = "utma"
    ugma = "ugma"


class AccountStatus(str, Enum):
    pending_consent = "pending_consent"
    pending_verification = "pending_verification"
    active = "active"
    frozen = "frozen"
    closed = "closed"
    transferred = "transferred"  # to beneficiary at age of majority


class ConsentMethod(str, Enum):
    """FTC-approved Verifiable Parental Consent methods per 16 CFR §312.5(b)(2)."""

    signed_form = "signed_form"
    credit_card_verification = "credit_card_verification"
    phone_call = "phone_call"
    video_conference = "video_conference"
    government_id_check = "government_id_check"
    knowledge_based_auth = "knowledge_based_auth"
    face_match_to_id = "face_match_to_id"


class ConsentStatus(str, Enum):
    initiated = "initiated"
    pending = "pending"
    granted = "granted"
    revoked = "revoked"
    expired = "expired"


class ComplianceEventType(str, Enum):
    parent_created = "parent.created"
    parent_kyc_submitted = "parent.kyc_submitted"
    parent_kyc_verified = "parent.kyc_verified"
    child_created = "child.created"
    account_created = "account.created"
    account_activated = "account.activated"
    account_frozen = "account.frozen"
    account_closed = "account.closed"
    consent_requested = "consent.requested"
    consent_granted = "consent.granted"
    consent_revoked = "consent.revoked"
    consent_expired = "consent.expired"
    account_consented = "account.consented"  # emitted atomically when VPC grants flip account pending_consent --> active
    account_funded = "account.funded"  # successful deposit; balance moved
    funding_source_added = "funding_source.added"  # parent linked a bank account
    withdrawal_requested = "withdrawal.requested"  # child-initiated; pending parent approval; NO balance change yet
    withdrawal_approved = "withdrawal.approved"  # parent approved; balance decremented atomically
    withdrawal_rejected = "withdrawal.rejected"  # parent rejected; NO balance change
    api_key_created = "api_key.created"  # API key issued for an account; plaintext returned to customer exactly once
    api_key_revoked = "api_key.revoked"  # reserved for future POST /v1/api-keys/{id}/revoke (not yet implemented)


# ──────────────────────────────────────────────────────────────────────────
# Tables
# ──────────────────────────────────────────────────────────────────────────


class Parent(SQLModel, table=True):
    """Adult custodian. Holds UTMA custodianship until the child reaches majority."""

    __tablename__ = "parents"

    id: str = Field(default_factory=parent_id, primary_key=True)
    email: str = Field(index=True, unique=True)
    legal_first_name: str
    legal_last_name: str
    date_of_birth: datetime  # must be >=18 at account creation — validated at service layer
    phone: Optional[str] = None
    ssn_last_four: Optional[str] = Field(default=None, max_length=4)
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = Field(default=None, max_length=2)
    postal_code: Optional[str] = None
    country: str = Field(default="US", max_length=2)

    kyc_status: KYCStatus = Field(default=KYCStatus.not_started)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=utc_now, nullable=False)

    children: list["Child"] = Relationship(back_populates="parent")
    accounts: list["Account"] = Relationship(back_populates="parent")
    consents: list["Consent"] = Relationship(back_populates="parent")
    funding_sources: list["FundingSource"] = Relationship(back_populates="parent")


class Child(SQLModel, table=True):
    """Minor beneficiary. Must be <18 at account creation."""

    __tablename__ = "children"

    id: str = Field(default_factory=child_id, primary_key=True)
    parent_id: str = Field(foreign_key="parents.id", index=True)
    legal_first_name: str
    legal_last_name: str
    date_of_birth: datetime  # must be <18 at account creation — validated at service layer
    relationship_to_parent: GuardianRelationship = Field(default=GuardianRelationship.parent)
    state_of_residence: str = Field(max_length=2)
    ssn_or_tin: Optional[str] = None  # required for UTMA tax reporting (1099 issuance)

    created_at: datetime = Field(default_factory=utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=utc_now, nullable=False)

    parent: Optional[Parent] = Relationship(back_populates="children")
    accounts: list["Account"] = Relationship(back_populates="child")


class Account(SQLModel, table=True):
    """UTMA/UGMA custodial account. One custodian (Parent), one beneficiary (Child)."""

    __tablename__ = "accounts"

    id: str = Field(default_factory=account_id, primary_key=True)
    parent_id: str = Field(foreign_key="parents.id", index=True)
    child_id: str = Field(foreign_key="children.id", index=True)

    account_type: AccountType = Field(default=AccountType.utma)
    governing_state: str = Field(max_length=2)  # drives termination_age via app.services.state_rules
    termination_age: int = Field(default=18)  # ALWAYS overwritten at create time by the state_rules engine; default is the most conservative fallback
    status: AccountStatus = Field(default=AccountStatus.pending_consent, index=True)
    balance_minor_units: int = Field(default=0)  # cents — never float for money
    currency: str = Field(default="USD", max_length=3)
    # Per-withdrawal ceiling in minor units (cents). None = no cap. Set by the
    # custodian at account creation; enforced at withdrawal-request time AND
    # re-checked at approval time so changes between the two cannot bypass it.
    spending_ceiling_minor_units: Optional[int] = Field(default=None)

    created_at: datetime = Field(default_factory=utc_now, nullable=False)
    updated_at: datetime = Field(default_factory=utc_now, nullable=False)
    activated_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None

    parent: Optional[Parent] = Relationship(back_populates="accounts")
    child: Optional[Child] = Relationship(back_populates="accounts")
    consents: list["Consent"] = Relationship(back_populates="account")
    transactions: list["Transaction"] = Relationship(back_populates="account")
    api_keys: list["ApiKey"] = Relationship(back_populates="account")


class Consent(SQLModel, table=True):
    """COPPA Verifiable Parental Consent record — the audit proof we obtained VPC."""

    __tablename__ = "consents"

    id: str = Field(default_factory=consent_id, primary_key=True)
    account_id: str = Field(foreign_key="accounts.id", index=True)
    parent_id: str = Field(foreign_key="parents.id", index=True)

    method: ConsentMethod
    status: ConsentStatus = Field(default=ConsentStatus.initiated, index=True)
    disclosure_version: Optional[str] = None  # which version of the consent text the parent saw
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    consent_token: Optional[str] = None  # single-use, sent to parent for confirmation

    requested_at: datetime = Field(default_factory=utc_now, nullable=False)
    granted_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    account: Optional[Account] = Relationship(back_populates="consents")
    parent: Optional[Parent] = Relationship(back_populates="consents")


class ComplianceEvent(SQLModel, table=True):
    """Immutable, append-only regulatory audit log. NEVER UPDATE OR DELETE rows here."""

    __tablename__ = "compliance_events"

    id: str = Field(default_factory=event_id, primary_key=True)
    event_type: str = Field(index=True, max_length=100)  # store enum .value (e.g. "parent.created"), not the Python identifier — lets us add types without DB migrations
    entity_type: str = Field(index=True)  # parent | child | account | consent
    entity_id: str = Field(index=True)
    actor_id: Optional[str] = None  # parent_id or "system"
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    regulatory_reference: Optional[str] = None  # e.g. "COPPA 16 CFR §312.5"
    ip_address: Optional[str] = None
    occurred_at: datetime = Field(default_factory=utc_now, nullable=False, index=True)


class FundingSource(SQLModel, table=True):
    """A parent's bank account, linked as a deposit source for their child's accounts.

    Status starts at "verified" in this sandbox; production would gate via microdeposit
    or Plaid verification before allowing the linkage to be usable for deposits.
    """

    __tablename__ = "funding_sources"

    id: str = Field(default_factory=funding_source_id, primary_key=True)
    parent_id: str = Field(foreign_key="parents.id", index=True)
    bank_name: str = Field(max_length=200)
    last_four: str = Field(min_length=4, max_length=4)  # last 4 of account/card — only these digits are stored
    status: str = Field(default="verified", max_length=32, index=True)  # "verified" | "pending" | "failed" | "removed"
    created_at: datetime = Field(default_factory=utc_now, nullable=False)

    parent: Optional[Parent] = Relationship(back_populates="funding_sources")
    transactions: list["Transaction"] = Relationship(back_populates="funding_source")


class Transaction(SQLModel, table=True):
    """Money movement on a custodial account. Amount is always positive (in minor units);
    direction is determined by `type` ("deposit" credits, "withdrawal" debits).

    This is the financial ledger. Pair every row with a `compliance_events` audit record.
    """

    __tablename__ = "transactions"

    id: str = Field(default_factory=transaction_id, primary_key=True)
    account_id: str = Field(foreign_key="accounts.id", index=True)
    funding_source_id: str = Field(foreign_key="funding_sources.id", index=True)
    amount_minor_units: int = Field(ge=0)  # always positive cents — direction lives in `type`
    type: str = Field(index=True, max_length=32)  # "deposit" | "withdrawal" | "dividend" | "fee"
    status: str = Field(index=True, max_length=32)  # "pending" | "succeeded" | "failed" | "rejected" | "reversed"
    # Who started this transaction. Withdrawals MUST have initiator="child" (the
    # custodial-control invariant — child can request but never execute). Deposits
    # default to "parent". "system" is reserved for automated entries (dividends, fees).
    initiator: str = Field(default="parent", index=True, max_length=32)  # "parent" | "child" | "system"
    description: Optional[str] = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=utc_now, nullable=False)
    # Set when a pending withdrawal moves to a terminal state (succeeded/rejected).
    decided_at: Optional[datetime] = Field(default=None)
    decided_by: Optional[str] = Field(default=None, max_length=64)  # parent_id of the approver / rejecter

    account: Optional[Account] = Relationship(back_populates="transactions")
    funding_source: Optional[FundingSource] = Relationship(back_populates="transactions")


class ApiKey(SQLModel, table=True):
    """Customer API key for authenticating against /v1/* endpoints.

    Plaintext is shown to the customer EXACTLY ONCE in the response of
    POST /v1/accounts and is never recoverable after that — we store only
    the SHA-256 hash. Auth at request time hashes the incoming X-API-Key
    header and looks it up against the indexed `key_hash` column.

    `prefix` is the first 15 chars of the plaintext (`kideo_live_AbCd`) and
    is safe to display — it lets us show "your key starts with kideo_live_AbCd"
    in dashboards without ever holding the full secret server-side.
    """

    __tablename__ = "api_keys"

    id: str = Field(default_factory=api_key_id, primary_key=True)
    account_id: str = Field(foreign_key="accounts.id", index=True)
    prefix: str = Field(max_length=32)  # safe-to-display first 15 chars of the plaintext
    key_hash: str = Field(index=True, unique=True, max_length=128)  # SHA-256 hex of the full plaintext
    created_at: datetime = Field(default_factory=utc_now, nullable=False)
    last_used_at: Optional[datetime] = Field(default=None)
    revoked_at: Optional[datetime] = Field(default=None, index=True)

    account: Optional[Account] = Relationship(back_populates="api_keys")