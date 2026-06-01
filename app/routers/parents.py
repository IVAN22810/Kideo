"""POST /v1/parents — create a parent custodian.

Validations:
  - Email must be unique (409 if duplicate)
  - Parent must be >= 18 years old at request time (422 if underage)

Side effects:
  - Writes a `parent.created` event to ComplianceEvent in the same transaction.
    If the commit fails, both the parent row AND the audit row roll back together.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, status
from sqlmodel import Session, select

from app.database import get_session
from app.errors import MinorAPIError
from app.models import ComplianceEventType, Parent
from app.schemas import ParentCreate, ParentRead
from app.services.auth import forbid_on_production
from app.services.compliance import log_event
from app.utils.age import aware_utc, completed_years


router = APIRouter(prefix="/v1/parents", tags=["parents"])

MINIMUM_PARENT_AGE_YEARS = 18


@router.post(
    "",
    response_model=ParentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a parent custodian",
    dependencies=[Depends(forbid_on_production)],
)
def create_parent(
    payload: ParentCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> Parent:
    # 1. Email uniqueness
    existing = session.exec(
        select(Parent).where(Parent.email == payload.email)
    ).first()
    if existing is not None:
        raise MinorAPIError(
            status_code=status.HTTP_409_CONFLICT,
            type="resource_already_exists",
            code="parent_email_exists",
            message=f"A parent with email '{payload.email}' already exists.",
        )

    # 2. Age >= 18
    age = completed_years(aware_utc(payload.date_of_birth), datetime.now(timezone.utc))
    if age < MINIMUM_PARENT_AGE_YEARS:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="parent_underage",
            message=(
                f"Parent custodians must be at least {MINIMUM_PARENT_AGE_YEARS} years old "
                f"(computed age: {age})."
            ),
        )

    # 3. Persist parent + audit event in a single transaction
    parent = Parent(**payload.model_dump())
    session.add(parent)
    session.flush()  # populate parent.id before the audit event references it

    log_event(
        session=session,
        event_type=ComplianceEventType.parent_created,
        entity_type="parent",
        entity_id=parent.id,
        actor_id=parent.id,
        payload={
            "email": parent.email,
            "country": parent.country,
            "kyc_status": parent.kyc_status.value,
        },
        regulatory_reference="COPPA 16 CFR §312.4 — Notice and identification of operator",
        ip_address=request.client.host if request.client else None,
    )

    session.commit()
    session.refresh(parent)
    return parent
