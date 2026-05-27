"""Pydantic schemas for the Minor API — request validation and response serialization.

Deliberately decoupled from the SQLModel ORM classes in app/models.py so the API
contract can evolve independently of the database schema and vice versa.

Stripe convention: every read response carries an `object` discriminator
("object": "parent", "object": "child", "object": "account", ...).
"""
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import (
    AccountStatus,
    AccountType,
    ConsentMethod,
    ConsentStatus,
    GuardianRelationship,
    KYCStatus,
)


# ──────────────────────────────────────────────────────────────────────────
# Parent schemas
# ──────────────────────────────────────────────────────────────────────────


class ParentCreate(BaseModel):
    """Input payload for POST /v1/parents."""

    email: EmailStr
    legal_first_name: str = Field(min_length=1, max_length=100)
    legal_last_name: str = Field(min_length=1, max_length=100)
    date_of_birth: datetime
    phone: Optional[str] = Field(default=None, max_length=32)
    ssn_last_four: Optional[str] = Field(default=None, min_length=4, max_length=4)
    address_line1: Optional[str] = Field(default=None, max_length=200)
    address_line2: Optional[str] = Field(default=None, max_length=200)
    city: Optional[str] = Field(default=None, max_length=100)
    state: Optional[str] = Field(default=None, min_length=2, max_length=2)
    postal_code: Optional[str] = Field(default=None, max_length=20)
    country: str = Field(default="US", min_length=2, max_length=2)

    @field_validator("ssn_last_four")
    @classmethod
    def _ssn_must_be_digits(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.isdigit():
            raise ValueError("ssn_last_four must be 4 numeric digits")
        return v

    @field_validator("state", "country")
    @classmethod
    def _uppercase_codes(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if v else v

    @field_validator("date_of_birth")
    @classmethod
    def _dob_must_be_past(cls, v: datetime) -> datetime:
        v_aware = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if v_aware > datetime.now(timezone.utc):
            raise ValueError("date_of_birth must be in the past")
        return v


class ParentRead(BaseModel):
    """Output payload for parent endpoints. SSN is intentionally never returned."""

    model_config = ConfigDict(from_attributes=True)

    object: Literal["parent"] = "parent"
    id: str
    email: EmailStr
    legal_first_name: str
    legal_last_name: str
    date_of_birth: datetime
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: str
    kyc_status: KYCStatus
    created_at: datetime
    updated_at: datetime


# ──────────────────────────────────────────────────────────────────────────
# Child schemas
# ──────────────────────────────────────────────────────────────────────────


class ChildCreate(BaseModel):
    """Input payload for POST /v1/children. The child must be a minor at creation."""

    parent_id: str = Field(min_length=1, description="ID of the parent custodian (par_*)")
    legal_first_name: str = Field(min_length=1, max_length=100)
    legal_last_name: str = Field(min_length=1, max_length=100)
    date_of_birth: datetime
    relationship_to_parent: GuardianRelationship = Field(default=GuardianRelationship.parent)
    state_of_residence: str = Field(min_length=2, max_length=2)
    ssn_or_tin: Optional[str] = Field(default=None, min_length=9, max_length=11)

    @field_validator("state_of_residence")
    @classmethod
    def _uppercase_state(cls, v: str) -> str:
        return v.upper()

    @field_validator("ssn_or_tin")
    @classmethod
    def _ssn_or_tin_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        digits = v.replace("-", "")
        if not digits.isdigit() or len(digits) != 9:
            raise ValueError("ssn_or_tin must be 9 digits (formats: '123456789' or '123-45-6789')")
        return v

    @field_validator("date_of_birth")
    @classmethod
    def _dob_must_be_past(cls, v: datetime) -> datetime:
        v_aware = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if v_aware > datetime.now(timezone.utc):
            raise ValueError("date_of_birth must be in the past")
        return v


class ChildRead(BaseModel):
    """Output payload for child endpoints. SSN/TIN is intentionally never returned."""

    model_config = ConfigDict(from_attributes=True)

    object: Literal["child"] = "child"
    id: str
    parent_id: str
    legal_first_name: str
    legal_last_name: str
    date_of_birth: datetime
    relationship_to_parent: GuardianRelationship
    state_of_residence: str
    created_at: datetime
    updated_at: datetime


# ──────────────────────────────────────────────────────────────────────────
# Account schemas
# ──────────────────────────────────────────────────────────────────────────


class AccountCreate(BaseModel):
    """Input payload for POST /v1/accounts.

    Balance starts at 0 and status starts at pending_consent — both server-controlled.
    `termination_age` is INTENTIONALLY absent: it is derived server-side from
    `governing_state` via app.services.state_rules. The law decides the age,
    not the API caller. See state_rules.py for the per-state table.
    `spending_ceiling_minor_units` is an optional per-withdrawal cap set by the
    custodian at account creation.
    """

    parent_id: str = Field(min_length=1, description="ID of the parent custodian (par_*)")
    child_id: str = Field(min_length=1, description="ID of the minor beneficiary (chd_*)")
    account_type: AccountType = Field(default=AccountType.utma)
    governing_state: str = Field(min_length=2, max_length=2)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    spending_ceiling_minor_units: Optional[int] = Field(
        default=None,
        ge=100,
        description=(
            "Optional per-withdrawal ceiling in minor units (cents). None = no cap. "
            "If set, every withdrawal request and every approval re-checks this limit."
        ),
    )

    @field_validator("governing_state", "currency")
    @classmethod
    def _uppercase_codes(cls, v: str) -> str:
        return v.upper()


class AccountRead(BaseModel):
    """Output payload for account endpoints.

    Balance is always reported in minor units (cents for USD) as an integer — never
    a float. Clients format for display.
    """

    model_config = ConfigDict(from_attributes=True)

    object: Literal["account"] = "account"
    id: str
    parent_id: str
    child_id: str
    account_type: AccountType
    governing_state: str
    termination_age: int  # derived server-side from governing_state; read-only for clients
    status: AccountStatus
    balance_minor_units: int
    currency: str
    spending_ceiling_minor_units: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    activated_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


# ──────────────────────────────────────────────────────────────────────────
# Consent schemas
# ──────────────────────────────────────────────────────────────────────────


class ConsentCreate(BaseModel):
    """Input payload for POST /v1/consents.

    The parent affirms `agreed_to_terms=True` via the SDK widget. The SDK is also
    responsible for capturing and forwarding the parent's IP address — this is
    primary forensic evidence under COPPA 16 CFR §312.5(b).
    """

    account_id: str = Field(min_length=1, description="ID of the custodial account (acc_*)")
    ip_address: str = Field(
        min_length=1,
        max_length=64,
        description="Parent's IP at consent time — required compliance evidence",
    )
    agreed_to_terms: bool = Field(
        description="Must be true. COPPA requires explicit affirmative consent (no implicit/default).",
    )
    method: ConsentMethod = Field(
        default=ConsentMethod.signed_form,
        description="FTC-approved VPC method (16 CFR §312.5(b)(2)). Defaults to signed_form.",
    )


class ConsentRead(BaseModel):
    """Output payload for consent endpoints. Sensitive fields (consent_token) intentionally omitted."""

    model_config = ConfigDict(from_attributes=True)

    object: Literal["consent"] = "consent"
    id: str
    account_id: str
    parent_id: str
    method: ConsentMethod
    status: ConsentStatus
    disclosure_version: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    requested_at: datetime
    granted_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


# ──────────────────────────────────────────────────────────────────────────
# Funding source schemas
# ──────────────────────────────────────────────────────────────────────────


class FundingSourceCreate(BaseModel):
    """Input for POST /v1/funding-sources. Only the last 4 digits are accepted — we
    never store full account/card numbers (PCI scope minimization)."""

    parent_id: str = Field(min_length=1, description="ID of the parent who owns this bank account (par_*)")
    bank_name: str = Field(min_length=1, max_length=200)
    last_four: str = Field(min_length=4, max_length=4, description="Last 4 digits of the bank account / card")

    @field_validator("last_four")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("last_four must be exactly 4 numeric digits")
        return v


class FundingSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    object: Literal["funding_source"] = "funding_source"
    id: str
    parent_id: str
    bank_name: str
    last_four: str
    status: str
    created_at: datetime


# ──────────────────────────────────────────────────────────────────────────
# Transaction / deposit schemas
# ──────────────────────────────────────────────────────────────────────────


class DepositRequest(BaseModel):
    """Input for POST /v1/accounts/{account_id}/deposits.

    Minimum amount is 100 minor units ($1.00 in USD) — prevents penny-spam and
    matches typical fintech minimums (Stripe, Cash App, etc.).
    """

    funding_source_id: str = Field(min_length=1, description="The bank account to debit (ba_*)")
    amount_minor_units: int = Field(ge=100, description="Amount in minor units (cents for USD). Minimum 100.")
    description: Optional[str] = Field(default=None, max_length=500)


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    object: Literal["transaction"] = "transaction"
    id: str
    account_id: str
    funding_source_id: str
    amount_minor_units: int
    type: str
    status: str
    initiator: str  # "parent" | "child" | "system"
    description: Optional[str] = None
    created_at: datetime
    decided_at: Optional[datetime] = None  # populated when a pending withdrawal is approved/rejected
    decided_by: Optional[str] = None  # parent_id of the decision maker


# ──────────────────────────────────────────────────────────────────────────
# Withdrawal request / approval / rejection (two-step custodial flow)
# ──────────────────────────────────────────────────────────────────────────


class WithdrawalRequest(BaseModel):
    """Child-initiated withdrawal request — creates a pending Transaction.

    NO balance change happens at request time. The transaction stays in
    status='pending' until the parent approves via POST /v1/transactions/{id}/approve
    or rejects via POST /v1/transactions/{id}/reject.

    The per-withdrawal `spending_ceiling_minor_units` (if set on the account)
    is enforced at request time AND re-checked at approval time.
    """

    amount_minor_units: int = Field(
        ge=100,
        description="Amount in minor units (cents). Minimum 100 ($1.00).",
    )
    description: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Purpose of the withdrawal (shown to the parent during approval).",
    )


class WithdrawalApprovalRequest(BaseModel):
    """Parent approves a pending withdrawal. All fields optional."""

    note: Optional[str] = Field(default=None, max_length=500, description="Optional parent note attached to the approval.")


class WithdrawalRejectionRequest(BaseModel):
    """Parent rejects a pending withdrawal. All fields optional."""

    reason: Optional[str] = Field(default=None, max_length=500, description="Optional reason returned to the child.")


# ──────────────────────────────────────────────────────────────────────────
# Chat (web AI assistant)
# ──────────────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    """Input for POST /v1/chat — child types a question into the dashboard."""

    account_id: str = Field(min_length=1, description="The custodial account this chat is about.")
    message: str = Field(min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    """Mock AI agent's reply, including the thinking trace and live balance."""

    reply: str
    logs: str  # newline-separated 🔄/⚙️/🟢/🔴 lines captured from the agent
    balance_minor_units: int


# ──────────────────────────────────────────────────────────────────────────
# Stripe-style error envelope
# ──────────────────────────────────────────────────────────────────────────


class ErrorDetail(BaseModel):
    type: str
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
