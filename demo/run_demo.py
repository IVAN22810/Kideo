"""Interactive CLI for the Minor AI Agent — the YC demo.

Default mode is **Mock Agent Mode**: no LLM, no API key required, but tool
execution against the live FastAPI server is real (the database actually
updates). Perfect for offline video recording.

On startup:
  1. Checks the Minor API (uvicorn on port 4242) is reachable.
  2. Seeds a live demo chain via REST: parent -> child -> active account
     -> funding source -> $50.00 deposit.
  3. Launches a REPL. The agent parses each message by keywords and calls real
     tools, printing thinking/Tool Call/Response logs so the demo looks alive.

Press Ctrl+C, or type 'exit', 'quit', or 'q' to quit.

Run it (with venv activated):
  python -m demo.run_demo
"""
from __future__ import annotations

import sys
import uuid

import httpx

from demo.agent import MinorAgent


# Make emoji + Cyrillic render correctly on Windows consoles (cp1251 default).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


BASE_URL = "http://localhost:4242"


def check_server(base_url: str) -> bool:
    """Quick reachability ping on /health. Returns True if 200, False otherwise."""
    try:
        r = httpx.get(f"{base_url}/health", timeout=5.0)
        return r.status_code == 200
    except (httpx.ConnectError, httpx.HTTPError, httpx.TimeoutException):
        return False


def seed_test_chain(base_url: str) -> dict[str, str]:
    """Create a fully-active demo chain via the REST API. Returns IDs + names."""
    tag = uuid.uuid4().hex[:6]

    with httpx.Client(base_url=base_url, timeout=10.0) as c:
        r = c.post("/v1/parents", json={
            "email": f"demo-{tag}@minor.dev",
            "legal_first_name": "Sarah",
            "legal_last_name": "Johnson",
            "date_of_birth": "1985-06-15",
            "state": "CA",
        })
        r.raise_for_status()
        parent = r.json()

        r = c.post("/v1/children", json={
            "parent_id": parent["id"],
            "legal_first_name": "Liam",
            "legal_last_name": "Johnson",
            "date_of_birth": "2016-03-20",
            "state_of_residence": "CA",
        })
        r.raise_for_status()
        child = r.json()

        r = c.post("/v1/accounts", json={
            "parent_id": parent["id"],
            "child_id": child["id"],
            "governing_state": "CA",
        })
        r.raise_for_status()
        account = r.json()

        c.post("/v1/consents", json={
            "account_id": account["id"],
            "ip_address": "192.0.2.42",
            "agreed_to_terms": True,
        }).raise_for_status()

        r = c.post("/v1/funding-sources", json={
            "parent_id": parent["id"],
            "bank_name": "Chase",
            "last_four": "4242",
        })
        r.raise_for_status()
        funding = r.json()

        c.post(
            f"/v1/accounts/{account['id']}/deposits",
            json={
                "funding_source_id": funding["id"],
                "amount_minor_units": 5000,
                "description": "Initial demo deposit",
            },
        ).raise_for_status()

        return {
            "parent_id": parent["id"],
            "child_id": child["id"],
            "account_id": account["id"],
            "funding_source_id": funding["id"],
            "parent_name": f"{parent['legal_first_name']} {parent['legal_last_name']}",
            "child_name": f"{child['legal_first_name']} {child['legal_last_name']}",
        }


WELCOME = (
    "Minor AI Agent Demo. Ask me a question in plain language about accounts "
    "or transactions (e.g. 'How much money does my child have?' or "
    "'Show me the transaction history')."
)


def main() -> int:
    print("=" * 72)
    print("  Minor AI Agent Demo — YC application demo  [Mock Mode]")
    print("=" * 72)
    print("  Running without an LLM API key. Tool calls hit the real REST API,")
    print("  so the SQLite database updates as if a real agent were driving.")

    print(f"\nChecking Minor API at {BASE_URL} ...")
    if not check_server(BASE_URL):
        print()
        print(f"ERROR: Cannot reach Minor API at {BASE_URL}.")
        print("Start it in another terminal:")
        print("  .\\.venv\\Scripts\\Activate.ps1")
        print("  uvicorn app.main:app --reload --port 4242")
        print()
        return 1
    print("OK — server is responding.")

    print("\nSeeding demo data (parent + child + active account + $50 deposit) ...")
    try:
        context = seed_test_chain(BASE_URL)
    except httpx.HTTPStatusError as e:
        print(f"\nFailed to seed demo data: {e.response.status_code} {e.response.text}")
        return 1
    except Exception as e:
        print(f"\nFailed to seed demo data: {type(e).__name__}: {e}")
        return 1

    print("\nDemo entities created:")
    print(f"  Parent ({context['parent_name']}):        {context['parent_id']}")
    print(f"  Child ({context['child_name']}, 10 yrs): {context['child_id']}")
    print(f"  Active account, $50.00:               {context['account_id']}")
    print(f"  Funding source (Chase ****4242):      {context['funding_source_id']}")
    print()
    print("-" * 72)
    print(WELCOME)
    print("(Type 'exit', 'quit', or press Ctrl+C to quit.)")
    print("-" * 72)
    print()

    agent = MinorAgent(base_url=BASE_URL, context=context, mock_mode=True)

    try:
        while True:
            try:
                user_input = input("you > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                return 0

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                print("Goodbye.")
                return 0

            try:
                reply = agent.chat(user_input)
                print(f"\nagent > {reply}\n")
            except Exception as e:
                print(f"\n[error talking to LLM: {type(e).__name__}: {e}]\n")
    finally:
        agent.close()


if __name__ == "__main__":
    sys.exit(main())
