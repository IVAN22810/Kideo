"""Regression suite for per-account authorization.

Runs 16 assertions against a live server: cross-account 403s, same-account
2xxs, bootstrap endpoints staying open, /demo + agent loop unaffected, and
the ungated routes (/health, /demo) still reachable without a key.

Defaults to a local uvicorn on 127.0.0.1:8000. Point at the cloud deploy via
the BASE env var:

    BASE=https://kideo.onrender.com python tests/smoke_per_account_auth.py
"""
import os
import re
import time

import httpx

BASE = os.environ.get("BASE", "http://127.0.0.1:8000").rstrip("/")


def err_code(r):
    try:
        b = r.json()
        return b.get("error", {}).get("code") if isinstance(b, dict) else None
    except Exception:
        return None


def main() -> int:
    with httpx.Client(timeout=30) as c:
        # Bootstrap: pull SEED key from /demo, then onboard a customer (acc B)
        html = c.get(f"{BASE}/demo").text
        SEED_KEY = re.search(r"(kideo_live_[A-Za-z0-9]{32})", html).group(1)
        SH = {"X-API-Key": SEED_KEY}
        SEED_ACC = re.search(r'"(acc_[A-Za-z0-9]{24})"', html).group(1)
        print(f"SEED key: {SEED_KEY[:25]}...  SEED acc: {SEED_ACC[:18]}...")

        # Unique email so reruns against a persistent DB (cloud!) don't 409.
        tag = int(time.time() * 1000)
        p = c.post(
            f"{BASE}/v1/parents",
            headers=SH,
            json={"email": f"cust-{tag}@x.com", "legal_first_name": "B", "legal_last_name": "B",
                  "date_of_birth": "1990-01-01T00:00:00", "state": "CA"},
        ).json()
        ch = c.post(
            f"{BASE}/v1/children",
            headers=SH,
            json={"parent_id": p["id"], "legal_first_name": "C", "legal_last_name": "C",
                  "date_of_birth": "2014-01-01T00:00:00", "state_of_residence": "CA"},
        ).json()
        a = c.post(
            f"{BASE}/v1/accounts",
            headers=SH,
            json={"parent_id": p["id"], "child_id": ch["id"], "governing_state": "CA",
                  "spending_ceiling_minor_units": 5000},
        ).json()
        CUST_ACC, CUST_KEY, CUST_PARENT = a["account"]["id"], a["api_key"], p["id"]
        CH = {"X-API-Key": CUST_KEY}
        c.post(f"{BASE}/v1/consents", headers=CH,
               json={"account_id": CUST_ACC, "ip_address": "127.0.0.1", "agreed_to_terms": True})
        fs = c.post(f"{BASE}/v1/funding-sources", headers=CH,
                    json={"parent_id": p["id"], "bank_name": "X", "last_four": "1234"}).json()
        c.post(f"{BASE}/v1/accounts/{CUST_ACC}/deposits", headers=CH,
               json={"funding_source_id": fs["id"], "amount_minor_units": 10000})
        print(f"CUST key: {CUST_KEY[:25]}...  CUST acc: {CUST_ACC[:18]}...  CUST parent: {CUST_PARENT[:18]}...")

        failures = []

        def check(label, cond, detail=""):
            marker = "OK  " if cond else "FAIL"
            print(f"  {marker} {label}" + (f"   ({detail})" if detail else ""))
            if not cond:
                failures.append((label, detail))

        print("\n=== 1. Seed key -> seed account: 200 (/demo depends on this) ===")
        r = c.get(f"{BASE}/v1/accounts/{SEED_ACC}", headers=SH)
        check("GET /v1/accounts/<seed_acc> with seed key", r.status_code == 200, str(r.status_code))

        print("\n=== 2. Seed key -> customer account: 403 ===")
        r = c.get(f"{BASE}/v1/accounts/{CUST_ACC}", headers=SH)
        check("GET /v1/accounts/<cust_acc> with seed key",
              r.status_code == 403 and err_code(r) == "account_access_forbidden",
              f"{r.status_code} {err_code(r)}")

        print("\n=== 3. Customer key -> customer account: 200 ===")
        r = c.get(f"{BASE}/v1/accounts/{CUST_ACC}", headers=CH)
        check("GET /v1/accounts/<cust_acc> with cust key", r.status_code == 200, str(r.status_code))

        print("\n=== 4. Customer key -> seed account (GET): 403 ===")
        r = c.get(f"{BASE}/v1/accounts/{SEED_ACC}", headers=CH)
        check("GET /v1/accounts/<seed_acc> with cust key",
              r.status_code == 403 and err_code(r) == "account_access_forbidden",
              f"{r.status_code} {err_code(r)}")

        print("\n=== 5. Customer key -> approve/reject TX from seed account: 403 ===")
        seed_tx = c.post(f"{BASE}/v1/accounts/{SEED_ACC}/withdrawals", headers=SH,
                         json={"amount_minor_units": 500}).json()["id"]
        r = c.post(f"{BASE}/v1/transactions/{seed_tx}/approve", headers=CH, json={})
        check("approve seed tx with cust key",
              r.status_code == 403 and err_code(r) == "account_access_forbidden",
              f"{r.status_code} {err_code(r)}")
        r = c.post(f"{BASE}/v1/transactions/{seed_tx}/reject", headers=CH, json={})
        check("reject seed tx with cust key",
              r.status_code == 403 and err_code(r) == "account_access_forbidden",
              f"{r.status_code} {err_code(r)}")

        print("\n=== 6. Customer key -> consent for seed account: 403 ===")
        r = c.post(f"{BASE}/v1/consents", headers=CH,
                   json={"account_id": SEED_ACC, "ip_address": "127.0.0.1", "agreed_to_terms": True})
        check("POST /v1/consents for seed acc with cust key",
              r.status_code == 403 and err_code(r) == "account_access_forbidden",
              f"{r.status_code} {err_code(r)}")

        print("\n=== 7. Customer key -> chat about seed account: 403 ===")
        r = c.post(f"{BASE}/v1/chat", headers=CH, json={"account_id": SEED_ACC, "message": "hi"})
        check("POST /v1/chat for seed acc with cust key",
              r.status_code == 403 and err_code(r) == "account_access_forbidden",
              f"{r.status_code} {err_code(r)}")

        print("\n=== 8. Bootstrap endpoints with any valid key: 200/201 ===")
        r = c.post(f"{BASE}/v1/parents", headers=CH,
                   json={"email": f"t{int(time.time())}@x.com", "legal_first_name": "T",
                         "legal_last_name": "T", "date_of_birth": "1990-01-01T00:00:00", "state": "CA"})
        check("POST /v1/parents with cust key (bootstrap)", r.status_code == 201, str(r.status_code))
        r = c.post(f"{BASE}/v1/funding-sources", headers=SH,
                   json={"parent_id": p["id"], "bank_name": "Y", "last_four": "5678"})
        check("POST /v1/funding-sources with seed key (bootstrap)", r.status_code == 201, str(r.status_code))

        print("\n=== 9. GET /v1/parents/<own>/accounts: 200, list contains only own acc ===")
        r = c.get(f"{BASE}/v1/parents/{CUST_PARENT}/accounts", headers=CH)
        body = r.json() if r.status_code == 200 else None
        ok = r.status_code == 200 and isinstance(body, list) and [x["id"] for x in body] == [CUST_ACC]
        check("list cust parent with cust key -> only own acc",
              ok,
              f"{r.status_code} ids={[x['id'][:18]+'...' for x in body]}" if body else str(r.status_code))

        print("\n=== 10. GET /v1/parents/<other>/accounts: 403 ===")
        r = c.get(f"{BASE}/v1/parents/{CUST_PARENT}/accounts", headers=SH)
        check("list cust parent with SEED key -> 403",
              r.status_code == 403 and err_code(r) == "account_access_forbidden",
              f"{r.status_code} {err_code(r)}")

        print("\n=== 11. /demo end-to-end with auth on (request -> approve -> balance) ===")
        bal_before = c.get(f"{BASE}/v1/accounts/{SEED_ACC}", headers=SH).json()["balance_minor_units"]
        rq = c.post(f"{BASE}/v1/accounts/{SEED_ACC}/withdrawals", headers=SH,
                    json={"amount_minor_units": 1000, "description": "F: demo flow"})
        check("child request -> 201", rq.status_code == 201, str(rq.status_code))
        tx_id = rq.json()["id"]
        ra = c.post(f"{BASE}/v1/transactions/{tx_id}/approve", headers=SH, json={})
        check("parent approve -> 200", ra.status_code == 200, str(ra.status_code))
        bal_after = c.get(f"{BASE}/v1/accounts/{SEED_ACC}", headers=SH).json()["balance_minor_units"]
        check(
            f"balance decremented atomically (${bal_before/100:.2f} -> ${bal_after/100:.2f})",
            bal_after == bal_before - 1000,
        )

        print("\n=== 12. /v1/chat agent loop still works (seed key -> seed account) ===")
        r = c.post(f"{BASE}/v1/chat", headers=SH,
                   json={"account_id": SEED_ACC, "message": "How much money do I have on my account?"})
        body = r.json() if r.status_code == 200 else {"reply": ""}
        expected = f"${bal_after/100:.2f}"
        ok = (
            r.status_code == 200
            and "Cannot reach" not in body.get("reply", "")
            and "401" not in body.get("reply", "")
            and expected in body.get("reply", "")
        )
        check(
            "agent loop returns live balance via authenticated round-trip",
            ok,
            body.get("reply", "")[:100] if r.status_code == 200 else str(r.status_code),
        )

        print("\n=== 13. /demo and /health remain ungated ===")
        r = c.get(f"{BASE}/demo")
        check("GET /demo (no key) -> 200", r.status_code == 200)
        r = c.get(f"{BASE}/health")
        check("GET /health (no key) -> 200", r.status_code == 200)

        print(f"\n{len(failures)} failure(s) of 16 cases")
        if failures:
            for f in failures:
                print(f"  {f}")
            return 1
        print("\n=== LOCAL CHECKPOINT F PASSED ===")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
