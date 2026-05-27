"""FastAPI entry point — REST API + server-rendered web dashboard.

The HTML routes (`/`, `/parent/{id}`, `/child/{id}`) serve a dark-themed
Tailwind/FontAwesome dashboard. The web layer talks to the same versioned
REST API (/v1/*) via fetch, plus a small `POST /v1/chat` endpoint that runs
the Mock AI agent from demo/agent.py against the live backend.
"""
import contextlib
import io
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.database import engine, get_session, init_database
from app.errors import MinorAPIError, minor_api_error_handler
from app.models import Account, AccountStatus, Child, FundingSource, Parent, Transaction
from app.routers import accounts, children, consents, funding, parents, transactions
from app.schemas import ChatRequest, ChatResponse
from app import seed as _seed
from app.seed import seed_demo_data_if_empty
from app.services.auth import require_api_key


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    # Idempotent: only seeds when no active account exists. On fresh deploys
    # (Render's first boot, dev machines that nuked the DB) this gives /demo
    # something real to render against; on already-seeded DBs it's a no-op.
    with Session(engine) as session:
        seed_demo_data_if_empty(session)
    yield


app = FastAPI(
    title="Minor API",
    description="Stripe for Under-18 Finance - UTMA UGMA custodial account API",
    version="0.1.0",
    lifespan=lifespan,
)

# ── Static files + Jinja2 templates ─────────────────────────────────────────

_APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_APP_DIR / "templates")
app.mount("/static", StaticFiles(directory=_APP_DIR / "static"), name="static")

# ── Stripe-style error envelope handler ─────────────────────────────────────

app.add_exception_handler(MinorAPIError, minor_api_error_handler)

# ── Versioned API routers ───────────────────────────────────────────────────
# Every /v1/* router is gated behind X-API-Key via the router-level dependency
# below. ONE change point — adding a new router automatically inherits auth.
# Exceptions (/health, /demo, /docs, HTML pages) live OUTSIDE /v1/* by design.

_v1_auth = [Depends(require_api_key)]

app.include_router(parents.router, dependencies=_v1_auth)
app.include_router(children.router, dependencies=_v1_auth)
app.include_router(accounts.router, dependencies=_v1_auth)
app.include_router(accounts.parent_accounts_router, dependencies=_v1_auth)  # GET /v1/parents/{id}/accounts
app.include_router(consents.router, dependencies=_v1_auth)
app.include_router(funding.router, dependencies=_v1_auth)
app.include_router(transactions.router, dependencies=_v1_auth)
app.include_router(transactions.actions_router, dependencies=_v1_auth)  # POST /v1/transactions/{id}/{approve|reject}


# ── Health / API root ───────────────────────────────────────────────────────


@app.get("/health")
def health():
    return {"status": "ok", "service": "minor-api", "version": "0.1.0"}


@app.get("/v1")
def api_root():
    return {"api": "minor", "version": "2026-05-01"}


# ── Web UI routes ───────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/parent/{parent_id}", response_class=HTMLResponse)
def parent_dashboard(
    parent_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    parent = session.get(Parent, parent_id)
    if parent is None:
        # Friendlier than 404 — bounce back to the role picker
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    accounts_list = session.exec(
        select(Account)
        .where(Account.parent_id == parent_id)
        .order_by(Account.created_at.desc())
    ).all()

    children_by_id = {
        c.id: c
        for c in session.exec(select(Child).where(Child.parent_id == parent_id)).all()
    }

    funding_sources_list = session.exec(
        select(FundingSource).where(
            FundingSource.parent_id == parent_id,
            FundingSource.status == "verified",
        )
    ).all()

    total_balance = sum(a.balance_minor_units for a in accounts_list)

    return templates.TemplateResponse(
        request=request,
        name="parent_dashboard.html",
        context={
            "parent": parent,
            "accounts": accounts_list,
            "children": children_by_id,
            "funding_sources": funding_sources_list,
            "total_balance": total_balance,
            # Sandbox key used by in-page JS for /v1/* fetches (deposit modal etc.).
            # In a real customer dashboard this would be the customer's own key,
            # scoped per-account; here every page borrows the universal seed key.
            "demo_api_key": _seed.DEMO_API_KEY,
        },
    )


# ── Single-page split-screen demo ───────────────────────────────────────────
# Public-facing /demo route used for pitch decks and shareable links.
# Shows the entire two-step withdrawal flow without sign-in/swap: child panel
# (request) on the left, parent panel (approve/reject) on the right, live
# balance + compliance citation in between. Pass ?account_id=... to point it
# at a different account; defaults to the most recently active one so a fresh
# Render deploy (with seed data) just works.


@app.get("/demo", response_class=HTMLResponse)
def demo(
    request: Request,
    session: Session = Depends(get_session),
    account_id: Optional[str] = None,
):
    if account_id is not None:
        account = session.get(Account, account_id)
    else:
        # Pick the most recently activated active account with a spending
        # ceiling set — that's the shape the demo is designed for.
        account = session.exec(
            select(Account)
            .where(Account.status == AccountStatus.active)
            .order_by(Account.created_at.desc())
            .limit(1)
        ).first()

    if account is None:
        return HTMLResponse(
            "<h1 style='font-family:sans-serif;padding:2rem'>No active demo account found.</h1>"
            "<p style='font-family:sans-serif;padding:0 2rem'>Seed one via POST /v1/parents, "
            "/v1/children, /v1/accounts, /v1/consents — or pass <code>?account_id=acc_...</code>.</p>",
            status_code=404,
        )

    child = session.get(Child, account.child_id)
    parent = session.get(Parent, account.parent_id)

    return templates.TemplateResponse(
        request=request,
        name="demo.html",
        context={
            "account": account,
            "child": child,
            "parent": parent,
            # Plaintext lives in process memory only; refreshes on every restart.
            # /demo is OUTSIDE the /v1/* auth gate per the spec, so this is safe
            # to render — the in-page JS uses it to authenticate its fetch calls.
            "demo_api_key": _seed.DEMO_API_KEY,
        },
    )


@app.get("/child/{account_id}", response_class=HTMLResponse)
def child_dashboard(
    account_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    account = session.get(Account, account_id)
    if account is None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    child = session.get(Child, account.child_id)
    if child is None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    transactions_list = session.exec(
        select(Transaction)
        .where(Transaction.account_id == account_id)
        .order_by(Transaction.created_at.desc())
        .limit(10)
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="child_dashboard.html",
        context={
            "account": account,
            "child": child,
            "transactions": transactions_list,
            # Sandbox key for in-page JS (chat POST and any future fetches).
            "demo_api_key": _seed.DEMO_API_KEY,
        },
    )


# ── Chat endpoint — Mock AI agent over the live API ─────────────────────────

# Serialize chat requests; `contextlib.redirect_stdout` mutates sys.stdout
# globally, so concurrent calls would interleave each other's thinking logs.
# For a 1-user demo this is plenty.
_chat_lock = threading.Lock()


@app.post(
    "/v1/chat",
    response_model=ChatResponse,
    dependencies=[Depends(require_api_key)],
)
def chat(
    payload: ChatRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> ChatResponse:
    # Lazy import — avoids a circular dependency at module load time
    from demo.agent import MinorAgent

    # 1. Account must exist
    account = session.get(Account, payload.account_id)
    if account is None:
        raise MinorAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            type="resource_missing",
            code="account_not_found",
            message=f"No account exists with id '{payload.account_id}'.",
        )

    # 2. Resolve the rest of the agent's context
    parent = session.get(Parent, account.parent_id)
    child = session.get(Child, account.child_id)
    funding_source = session.exec(
        select(FundingSource)
        .where(
            FundingSource.parent_id == account.parent_id,
            FundingSource.status == "verified",
        )
        .limit(1)
    ).first()

    context = {
        "parent_id": account.parent_id,
        "child_id": account.child_id,
        "account_id": account.id,
        "funding_source_id": funding_source.id if funding_source else "",
        "parent_name": (
            f"{parent.legal_first_name} {parent.legal_last_name}"
            if parent else "the parent"
        ),
        "child_name": (
            f"{child.legal_first_name} {child.legal_last_name}"
            if child else "the child"
        ),
    }

    # 3. Run the mock agent. It loops back via HTTP to this same server, so it
    # has to send the demo's API key on every call — the /v1/* routers are
    # gated now. We use the same seed-mint plaintext that the /demo banner shows.
    base_url = str(request.base_url).rstrip("/")
    agent = MinorAgent(
        base_url=base_url,
        context=context,
        api_key=_seed.DEMO_API_KEY,
        mock_mode=True,
    )
    buf = io.StringIO()
    try:
        with _chat_lock, contextlib.redirect_stdout(buf):
            reply = agent.chat(payload.message)
        # Fetch latest balance via the agent's own HTTP path so we see
        # any commits the agent just made.
        bal_result, bal_err = agent.execute_tool(
            "get_account_details", {"account_id": account.id}
        )
    finally:
        agent.close()

    logs = buf.getvalue().strip()

    if not bal_err:
        latest_balance = json.loads(bal_result).get(
            "balance_minor_units", account.balance_minor_units
        )
    else:
        latest_balance = account.balance_minor_units

    return ChatResponse(
        reply=reply,
        logs=logs,
        balance_minor_units=latest_balance,
    )
