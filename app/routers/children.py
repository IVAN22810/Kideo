"""POST /v1/children — register a minor beneficiary under an existing parent.

Validations:
  - Parent must exist (404 if not)
  - Child must be < 18 at request time (422 if not — UTMA/UGMA only valid for minors)

Side effects:
  - Writes a `child.created` event to ComplianceEvent in the same transaction.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, status
from sqlmodel import Session

from app.database import get_session
from app.errors import MinorAPIError
from app.models import Child, ComplianceEventType, Parent
from app.schemas import ChildCreate, ChildRead
from app.services.auth import forbid_on_production
from app.services.compliance import log_event
from app.utils.age import aware_utc, completed_years


router = APIRouter(prefix="/v1/children", tags=["children"])

MAX_CHILD_AGE_YEARS = 18


@router.post(
    "",
    response_model=ChildRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a minor beneficiary under an existing parent custodian",
    dependencies=[Depends(forbid_on_production)],
)
def create_child(
    payload: ChildCreate,
    request: Request,
    session: Session = Depends(get_session),
) -> Child:
    # 1. Parent must exist
    parent = session.get(Parent, payload.parent_id)
    if parent is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="parent_not_found",
            message=f"No parent exists with id '{payload.parent_id}'.",
        )

    # 2. Child must be < 18
    age = completed_years(aware_utc(payload.date_of_birth), datetime.now(timezone.utc))
    if age >= MAX_CHILD_AGE_YEARS:
        raise MinorAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            type="invalid_request_error",
            code="child_not_a_minor",
            message=(
                f"Children must be under {MAX_CHILD_AGE_YEARS} years old "
                f"(computed age: {age}). UTMA/UGMA accounts are only valid for minors."
            ),
        )

    # 3. Persist child + audit event in a single transaction
    child = Child(**payload.model_dump())
    session.add(child)
    session.flush()

    log_event(
        session=session,
        event_type=ComplianceEventType.child_created,
        entity_type="child",
        entity_id=child.id,
        actor_id=parent.id,
        payload={
            "parent_id": parent.id,
            "state_of_residence": child.state_of_residence,
            "relationship_to_parent": child.relationship_to_parent.value,
            "computed_age_years": age,
        },
        regulatory_reference="COPPA 16 CFR §312.5 — Parental Consent (minor identification)",
        ip_address=request.client.host if request.client else None,
    )

    session.commit()
    session.refresh(child)
    return child
