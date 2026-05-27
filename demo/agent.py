"""Minor AI Agent — tool-using agent over the Minor REST API.

CUSTODIAL INVARIANT (must hold in BOTH mock and LLM mode):
The child-facing agent can never execute a balance change directly. Money out
requires a two-step custodial flow:
  1) Child requests a withdrawal -> Transaction(type="withdrawal",
     status="pending"). No funds move yet.
  2) Parent approves separately -> status="succeeded", balance debited
     atomically. (Or the parent rejects -> status="rejected", no movement.)

Therefore this agent does NOT expose `create_deposit` as a tool. Deposits are
the custodian's own funding action and live on the parent dashboard, not in the
child chat. If the child says "deposit $X" (any deposit verb), the agent politely
refuses and points them to the request flow.

Two operating modes:
  - Mock mode (default) — keyword/regex intent dispatch; no LLM, no API key.
  - LLM mode — Anthropic tool-use loop with claude-opus-4-7 (mock_mode=False).

Both modes share the same `execute_tool` HTTP layer, so the custodial invariant
holds at the HTTP boundary as well: the agent's available tools simply do not
include a direct-debit endpoint.
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

# Optional — only needed in LLM mode. Mock mode works without it.
try:
    import anthropic  # type: ignore
except ImportError:
    anthropic = None  # type: ignore


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_parent_accounts",
        "description": (
            "List every UTMA/UGMA custodial account opened by a specific parent custodian. "
            "Returns a JSON array; each item contains id, balance_minor_units (cents), "
            "status, child_id, governing_state, currency, spending_ceiling_minor_units, "
            "and timestamps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_id": {
                    "type": "string",
                    "description": "The parent's ID (e.g. 'par_AbCd1234...').",
                },
            },
            "required": ["parent_id"],
        },
    },
    {
        "name": "get_account_details",
        "description": (
            "Get the full live state of one custodial account, including the current "
            "balance_minor_units (cents, ALWAYS integer), status, spending_ceiling, "
            "and timestamps. ALWAYS divide balance_minor_units by 100 when showing dollars."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
            },
            "required": ["account_id"],
        },
    },
    {
        "name": "get_account_transactions",
        "description": (
            "List all transactions on an account, ordered newest first. Each entry has "
            "id, amount_minor_units (cents), type (deposit/withdrawal), status "
            "(pending/succeeded/rejected/failed), initiator (parent/child), description, "
            "and created_at."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
            },
            "required": ["account_id"],
        },
    },
    {
        "name": "create_withdrawal_request",
        "description": (
            "Create a CHILD-initiated withdrawal REQUEST against an active custodial "
            "account. The request enters status='pending' and NO money moves yet — "
            "the parent must approve via POST /v1/transactions/{id}/approve before "
            "the balance is debited. The per-withdrawal spending ceiling on the "
            "account (if set) is enforced at request time. Amount is in minor units "
            "(cents); minimum 100."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string"},
                "amount_minor_units": {"type": "integer", "minimum": 100},
                "description": {
                    "type": "string",
                    "description": "Why the child wants the money (shown to the parent during approval).",
                },
            },
            "required": ["account_id", "amount_minor_units"],
        },
    },
]


# Amount-extraction regex patterns, applied in order. (pattern, cents_multiplier).
_AMOUNT_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    # Explicit cents: "100 cents" — store directly
    (re.compile(r"(\d+)\s*cents?\b", re.IGNORECASE), 1),
    # Dollar sign prefix: "$20", "$ 100.50"
    (re.compile(r"\$\s*(\d+(?:[.,]\d{1,2})?)"), 100),
    # Number followed by dollar word/sign: "20 dollars", "100$", "50 USD"
    (
        re.compile(
            r"(\d+(?:[.,]\d{1,2})?)\s*(?:\$|dollars?|usd)",
            re.IGNORECASE,
        ),
        100,
    ),
    # Bare number after a withdrawal/deposit verb (guards against accidentally
    # grabbing digits from inside IDs like par_WaAb60Eb...)
    (
        re.compile(
            r"(?:withdraw|spend|buy|request|deposit|add|fund|take|put)"
            r"\D{0,15}(\d+(?:[.,]\d{1,2})?)",
            re.IGNORECASE,
        ),
        100,
    ),
]


# Keyword sets for intent detection. ORDER MATTERS — see _parse_intent for why.
_BALANCE_KEYWORDS = (
    "balance", "how much", "what's the balance", "whats the balance",
    "remaining", "details", "account state",
)
_HISTORY_KEYWORDS = (
    "history", "transactions", "transaction", "operations", "operation",
    "activity", "what happened",
)
_LIST_ACCOUNTS_KEYWORDS = (
    "list accounts", "all accounts", "show accounts", "my accounts",
    "which accounts", "accounts",
)
# Custodial invariant — the child cannot deposit to their own account. Any
# deposit verb from the child triggers a polite refusal that explains why
# and redirects to the withdrawal-request flow.
_DEPOSIT_REFUSE_KEYWORDS = (
    "deposit", "add money", "add cash", "fund", "top up", "top-up",
    "put money",
)
# Child-initiated withdrawal request — goes through parent approval.
_WITHDRAWAL_KEYWORDS = (
    "withdraw", "spend", "buy", "request", "give me",
    "i want", "want to", "take out",
)


class MinorAgent:
    """Stateful agent. Holds context + httpx client. Default mode is mock."""

    def __init__(
        self,
        *,
        base_url: str,
        context: dict[str, str],
        client: Any | None = None,
        model: str = "claude-opus-4-7",
        mock_mode: bool = True,
    ) -> None:
        self.mock_mode = mock_mode
        self.context = context
        self.model = model
        self.http = httpx.Client(base_url=base_url, timeout=10.0)
        self.messages: list[dict[str, Any]] = []

        if mock_mode:
            self.client = None
        else:
            if anthropic is None:
                raise RuntimeError(
                    "anthropic SDK is not installed; mock_mode=False requires it. "
                    "Install with: pip install anthropic"
                )
            self.client = client if client is not None else anthropic.Anthropic()

        self.system = self._build_system_prompt()

    # ───────────────────────────────────────────────────────────────────────
    # Public API
    # ───────────────────────────────────────────────────────────────────────

    def chat(self, user_message: str, max_iterations: int = 8) -> str:
        if self.mock_mode:
            return self._mock_chat(user_message)
        return self._llm_chat(user_message, max_iterations)

    def execute_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool against the Minor REST API. (content, is_error).

        NOTE: there is intentionally no `create_deposit` branch here — deposits
        are a parent action and are not exposed to the child-facing agent.
        See the module docstring for the custodial invariant.
        """
        try:
            if name == "get_parent_accounts":
                r = self.http.get(f"/v1/parents/{args['parent_id']}/accounts")
            elif name == "get_account_details":
                r = self.http.get(f"/v1/accounts/{args['account_id']}")
            elif name == "get_account_transactions":
                r = self.http.get(f"/v1/accounts/{args['account_id']}/transactions")
            elif name == "create_withdrawal_request":
                body: dict[str, Any] = {
                    "amount_minor_units": int(args["amount_minor_units"]),
                }
                if args.get("description"):
                    body["description"] = args["description"]
                r = self.http.post(
                    f"/v1/accounts/{args['account_id']}/withdrawals",
                    json=body,
                )
            else:
                return json.dumps({"error": f"unknown tool: {name}"}), True

            if r.status_code >= 400:
                return r.text, True
            return r.text, False
        except httpx.ConnectError:
            return (
                "ERROR: Cannot reach Minor API at localhost:4242. "
                "Restart uvicorn with `uvicorn app.main:app --reload --port 4242`."
            ), True
        except httpx.TimeoutException:
            return "ERROR: Request to Minor API timed out.", True
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}", True

    def close(self) -> None:
        self.http.close()

    # ───────────────────────────────────────────────────────────────────────
    # Mock mode — keyword-based intent dispatch with REAL tool execution
    # ───────────────────────────────────────────────────────────────────────

    def _mock_chat(self, user_message: str) -> str:
        print("\n🔄 [AI Agent]: Analyzing request...")

        intent = self._parse_intent(user_message)

        if intent == "balance":
            return self._handle_balance()
        if intent == "history":
            return self._handle_history()
        if intent == "deposit_refused":
            return self._handle_deposit_refused()
        if intent == "withdrawal":
            amount = self._extract_amount(user_message)
            return self._handle_withdrawal_request(amount, user_message)
        if intent == "list_accounts":
            return self._handle_list_accounts()
        return self._handle_unknown()

    def _parse_intent(self, text: str) -> str:
        """Map a message to one of: balance | history | deposit_refused |
        withdrawal | list_accounts | unknown.

        Order rationale:
          1. Balance/history/list — clear positive intents, no ambiguity
          2. Deposit-refusal BEFORE withdrawal so a deposit verb (e.g. "deposit",
             "fund", "add money") isn't mistakenly matched by a "want + deposit"
             phrase — positive child-deposit attempts must be refused, not
             silently converted to withdrawals.
          3. Withdrawal LAST among the verbs
        """
        lower = text.lower()
        if any(kw in lower for kw in _BALANCE_KEYWORDS):
            return "balance"
        if any(kw in lower for kw in _HISTORY_KEYWORDS):
            return "history"
        if any(kw in lower for kw in _LIST_ACCOUNTS_KEYWORDS):
            return "list_accounts"
        if any(kw in lower for kw in _DEPOSIT_REFUSE_KEYWORDS):
            return "deposit_refused"
        if any(kw in lower for kw in _WITHDRAWAL_KEYWORDS):
            return "withdrawal"
        return "unknown"

    def _extract_amount(self, text: str) -> int:
        """Extract a money amount → minor units (cents). 0 if not found."""
        for pattern, multiplier in _AMOUNT_PATTERNS:
            m = pattern.search(text)
            if m:
                value = m.group(1).replace(",", ".")
                try:
                    return int(round(float(value) * multiplier))
                except ValueError:
                    continue
        return 0

    @staticmethod
    def _money(cents: int) -> str:
        sign = "-" if cents < 0 else ""
        return f"{sign}${abs(cents) / 100:,.2f}"

    # ── Intent handlers — each prints Tool Call/Response logs + returns text ──

    def _handle_balance(self) -> str:
        account_id = self.context["account_id"]
        print(f'⚙️ [Tool Call]: get_account_details(account_id="{account_id}")')
        result, is_err = self.execute_tool(
            "get_account_details", {"account_id": account_id}
        )
        if is_err:
            print(f"🔴 [Error]: {result[:200]}")
            return f"Could not fetch account state: {result}"
        data = json.loads(result)
        balance = data["balance_minor_units"]
        ceiling = data.get("spending_ceiling_minor_units")
        print(
            f"🟢 [Response]: HTTP 200 — balance={balance} cents, "
            f"status={data['status']}, currency={data['currency']}"
        )
        child = self.context.get("child_name", "the child")
        activated = (data.get("activated_at") or "")[:10] or "not activated"
        ceiling_line = (
            f"  • Per-withdrawal ceiling: {self._money(ceiling)}\n"
            if ceiling is not None
            else "  • Per-withdrawal ceiling: none\n"
        )
        return (
            f"{child}'s account currently holds {self._money(balance)} USD.\n"
            f"  • Status: {data['status']}\n"
            f"  • Governing state: {data['governing_state']} "
            f"(termination at age {data.get('termination_age', '?')})\n"
            f"{ceiling_line}"
            f"  • Activated: {activated}"
        )

    def _handle_history(self) -> str:
        account_id = self.context["account_id"]
        print(f'⚙️ [Tool Call]: get_account_transactions(account_id="{account_id}")')
        result, is_err = self.execute_tool(
            "get_account_transactions", {"account_id": account_id}
        )
        if is_err:
            print(f"🔴 [Error]: {result[:200]}")
            return f"Could not fetch transaction history: {result}"
        txs = json.loads(result)
        print(f"🟢 [Response]: HTTP 200 — {len(txs)} transaction(s) returned")
        if not txs:
            return "No transactions on this account yet."
        lines = [f"Transaction history ({len(txs)}), newest first:"]
        for t in txs:
            amt = self._money(t["amount_minor_units"])
            when = t["created_at"][:19].replace("T", " ")
            desc = t.get("description") or ""
            initiator = t.get("initiator", "?")
            line = (
                f"  • {when}  {t['type']:10} {amt:>10}  "
                f"({t['status']}, by {initiator})"
            )
            if desc:
                line += f"  — {desc}"
            lines.append(line)
        return "\n".join(lines)

    def _handle_deposit_refused(self) -> str:
        """Custodial invariant: the child cannot deposit to their own account.
        Explain it and redirect to the withdrawal-request flow."""
        print("🔴 [Response]: deposit refused — child cannot fund own account")
        return (
            "I can't deposit money into your own account — only your parent (the "
            "custodian) can do that. UTMA custodial accounts work that way by law: "
            "the parent funds, you can request to spend.\n\n"
            "If you'd like to spend or withdraw money, try:\n"
            "  • \"I want to spend $20\"\n"
            "  • \"Withdraw $10 for a book\"\n"
            "  • \"Spend $5 on a snack\"\n\n"
            "Your request will be sent to your parent for approval."
        )

    def _handle_withdrawal_request(self, amount_minor_units: int, original_text: str) -> str:
        """Create a pending withdrawal request via the API. NO balance change yet."""
        if amount_minor_units <= 0:
            print("🔴 [Error]: amount not detected in user input")
            return (
                "I couldn't tell how much you want to withdraw. Try something like "
                "\"I want to spend $20\" or \"withdraw $10\"."
            )
        if amount_minor_units < 100:
            print(f"🔴 [Error]: amount {amount_minor_units} cents < $1 minimum")
            return (
                f"The minimum withdrawal is $1.00 (100 cents). "
                f"You asked for {self._money(amount_minor_units)}."
            )

        account_id = self.context["account_id"]
        # Pass the original child message as the description so the parent
        # sees the actual ask during approval.
        description = original_text.strip()[:500] if original_text else None
        print(
            f'⚙️ [Tool Call]: create_withdrawal_request(account_id="{account_id}", '
            f"amount_minor_units={amount_minor_units}, "
            f"description={description!r})"
        )
        result, is_err = self.execute_tool(
            "create_withdrawal_request",
            {
                "account_id": account_id,
                "amount_minor_units": amount_minor_units,
                "description": description,
            },
        )
        if is_err:
            # Friendly translation for the most common failure codes
            print(f"🔴 [Error]: {result[:300]}")
            try:
                err = json.loads(result).get("error", {})
                code = err.get("code", "")
                msg = err.get("message", result)
            except (json.JSONDecodeError, AttributeError):
                code, msg = "", result
            if code == "withdrawal_exceeds_ceiling":
                return (
                    f"Your parent set a per-withdrawal ceiling on this account "
                    f"and {self._money(amount_minor_units)} is above it. "
                    f"Try a smaller amount, or ask your parent to raise the ceiling.\n\n"
                    f"({msg})"
                )
            if code == "insufficient_balance":
                return (
                    f"You don't have enough on the account to withdraw "
                    f"{self._money(amount_minor_units)}.\n\n({msg})"
                )
            return f"Could not create the withdrawal request: {msg}"

        tx = json.loads(result)
        print(
            f"🟢 [Response]: HTTP 201 — tx={tx['id']}, status={tx['status']}, "
            f"amount={tx['amount_minor_units']} cents (NO balance change yet)"
        )
        return (
            f"Your request to withdraw {self._money(amount_minor_units)} has been "
            f"sent to your parent for approval. No money has moved yet.\n"
            f"  • Request ID: {tx['id']}\n"
            f"  • Status: {tx['status']} (waiting on parent)\n"
            f"  • Your parent will approve or reject this request.\n"
            f"  • Reminder: the balance changes only after approval."
        )

    def _handle_list_accounts(self) -> str:
        parent_id = self.context["parent_id"]
        print(f'⚙️ [Tool Call]: get_parent_accounts(parent_id="{parent_id}")')
        result, is_err = self.execute_tool(
            "get_parent_accounts", {"parent_id": parent_id}
        )
        if is_err:
            print(f"🔴 [Error]: {result[:200]}")
            return f"Could not fetch the account list: {result}"
        accounts = json.loads(result)
        print(f"🟢 [Response]: HTTP 200 — {len(accounts)} account(s) returned")
        if not accounts:
            return "No custodial accounts open for this parent yet."
        parent_name = self.context.get("parent_name", "the parent")
        lines = [f"{parent_name} has {len(accounts)} account(s):"]
        for a in accounts:
            balance = self._money(a["balance_minor_units"])
            lines.append(
                f"  • {a['id']}  {balance:>10}  "
                f"({a['status']}, {a['account_type'].upper()}, {a['governing_state']})"
            )
        return "\n".join(lines)

    def _handle_unknown(self) -> str:
        print("🔴 [Response]: intent not recognized in mock mode")
        return (
            "I didn't quite get that. In demo mode I understand:\n"
            "  • \"How much money is in my account?\" — current balance\n"
            "  • \"Show me the transaction history\" — list of transactions\n"
            "  • \"I want to spend $20\" — create a withdrawal request "
            "(parent must approve)\n"
            "  • \"Show all my accounts\" — list parent's accounts\n\n"
            "Note: you cannot deposit money into your own account — only your "
            "parent can do that."
        )

    # ───────────────────────────────────────────────────────────────────────
    # LLM mode (claude-opus-4-7) — preserved for when an API key is available.
    # Switch on via MinorAgent(..., mock_mode=False).
    # ───────────────────────────────────────────────────────────────────────

    def _llm_chat(self, user_message: str, max_iterations: int) -> str:
        self.messages.append({"role": "user", "content": user_message})
        for _ in range(max_iterations):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=self.system,
                tools=TOOL_DEFINITIONS,
                messages=self.messages,
            )
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                return "\n".join(b.text for b in response.content if b.type == "text")

            if response.stop_reason == "tool_use":
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if block.type == "tool_use":
                        result, is_error = self.execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                            "is_error": is_error,
                        })
                self.messages.append({"role": "user", "content": tool_results})
                continue

            final = "\n".join(b.text for b in response.content if b.type == "text")
            return final or f"[Stopped: {response.stop_reason}]"

        return "[Tool loop exceeded max iterations.]"

    def _build_system_prompt(self) -> str:
        return (
            "You are the Minor AI assistant — a helpful financial agent for the "
            "CHILD on an UTMA custodial account.\n\n"
            "CUSTODIAL INVARIANT (non-negotiable): the child cannot move money "
            "directly. You have read access (balance, history, account list) and "
            "you can CREATE a withdrawal REQUEST on the child's behalf. You do "
            "NOT have a deposit tool — the parent funds the account through a "
            "separate flow. If the user asks to deposit money, explain that only "
            "the parent can do that, and redirect them to the withdrawal-request "
            "flow if appropriate.\n\n"
            "Tools are real and hit a live REST API. Never fabricate numbers — "
            "ALWAYS call a tool to fetch state. Money is stored as integer minor "
            "units (cents). Format as $X.YY when showing the user.\n\n"
            "When a withdrawal request fails:\n"
            "  - code=withdrawal_exceeds_ceiling → tell the user the parent's "
            "ceiling is lower; suggest a smaller amount.\n"
            "  - code=insufficient_balance → tell them how much is actually available.\n"
            "  - code=account_not_active → the account hasn't completed VPC; the parent must finish it.\n\n"
            f"Current demo context:\n"
            f"  - Parent ID: {self.context.get('parent_id')}\n"
            f"  - Child ID: {self.context.get('child_id')}\n"
            f"  - Account ID: {self.context.get('account_id')}\n"
            f"  - Funding source ID: {self.context.get('funding_source_id')}\n\n"
            "Respond in clear, simple English."
        )
