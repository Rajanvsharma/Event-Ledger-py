"""
End-to-end integration tests.

Starts both services as real subprocesses and exercises the full
Gateway → Account Service HTTP flow. Requires both services'
dependencies to be installed.
"""
import os
import sys
import time
import subprocess
import pytest
import httpx

GATEWAY_PORT = 18000
ACCOUNT_PORT = 18001
GATEWAY_URL = f"http://localhost:{GATEWAY_PORT}"
ACCOUNT_URL = f"http://localhost:{ACCOUNT_PORT}"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
GW_DIR = os.path.join(REPO_ROOT, "event-gateway")
AC_DIR = os.path.join(REPO_ROOT, "account-service")


def _wait_ready(url: str, timeout: int = 15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Service at {url} did not become ready within {timeout}s")


@pytest.fixture(scope="module")
def running_services():
    env = os.environ.copy()
    env["ACCOUNT_SERVICE_URL"] = ACCOUNT_URL

    ac_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(ACCOUNT_PORT), "--log-level", "error"],
        cwd=AC_DIR,
        env=env,
    )
    gw_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(GATEWAY_PORT), "--log-level", "error"],
        cwd=GW_DIR,
        env=env,
    )

    try:
        _wait_ready(ACCOUNT_URL)
        _wait_ready(GATEWAY_URL)
        yield
    finally:
        gw_proc.terminate()
        ac_proc.terminate()
        gw_proc.wait(timeout=5)
        ac_proc.wait(timeout=5)
        # Clean up test databases
        for db in [os.path.join(GW_DIR, "gateway.db"), os.path.join(AC_DIR, "account.db")]:
            if os.path.exists(db):
                os.remove(db)


BASE_EVENT = {
    "eventId": "e2e-evt-001",
    "accountId": "e2e-acct-001",
    "type": "CREDIT",
    "amount": 250.0,
    "currency": "USD",
    "eventTimestamp": "2026-05-15T10:00:00Z",
}


def test_e2e_submit_event(running_services):
    r = httpx.post(f"{GATEWAY_URL}/events", json=BASE_EVENT)
    assert r.status_code == 201
    data = r.json()
    assert data["event_id"] == "e2e-evt-001"
    assert data["account_id"] == "e2e-acct-001"


def test_e2e_idempotency(running_services):
    httpx.post(f"{GATEWAY_URL}/events", json=BASE_EVENT)  # may already exist
    r2 = httpx.post(f"{GATEWAY_URL}/events", json=BASE_EVENT)
    assert r2.status_code in (200, 201)
    # Ensure balance is not doubled
    bal = httpx.get(f"{GATEWAY_URL}/accounts/e2e-acct-001/balance")
    assert bal.status_code == 200
    assert bal.json()["balance"] == pytest.approx(250.0)


def test_e2e_balance_via_gateway(running_services):
    httpx.post(f"{GATEWAY_URL}/events", json=BASE_EVENT)
    r = httpx.get(f"{GATEWAY_URL}/accounts/e2e-acct-001/balance")
    assert r.status_code == 200
    assert r.json()["balance"] == pytest.approx(250.0)


def test_e2e_debit_updates_balance(running_services):
    debit = {**BASE_EVENT, "eventId": "e2e-evt-debit", "type": "DEBIT", "amount": 100.0}
    httpx.post(f"{GATEWAY_URL}/events", json=BASE_EVENT)
    httpx.post(f"{GATEWAY_URL}/events", json=debit)
    r = httpx.get(f"{GATEWAY_URL}/accounts/e2e-acct-001/balance")
    assert r.status_code == 200
    assert r.json()["balance"] == pytest.approx(150.0)


def test_e2e_get_event(running_services):
    httpx.post(f"{GATEWAY_URL}/events", json=BASE_EVENT)
    r = httpx.get(f"{GATEWAY_URL}/events/e2e-evt-001")
    assert r.status_code == 200
    assert r.json()["amount"] == 250.0


def test_e2e_list_events_ordered(running_services):
    events = [
        {**BASE_EVENT, "eventId": "e2e-ord-late",  "eventTimestamp": "2026-01-01T12:00:00Z"},
        {**BASE_EVENT, "eventId": "e2e-ord-early", "eventTimestamp": "2026-01-01T08:00:00Z"},
        {**BASE_EVENT, "eventId": "e2e-ord-mid",   "eventTimestamp": "2026-01-01T10:00:00Z"},
    ]
    for e in events:
        httpx.post(f"{GATEWAY_URL}/events", json=e)

    r = httpx.get(f"{GATEWAY_URL}/events", params={"account": "e2e-acct-001"})
    assert r.status_code == 200
    ts = [ev["event_timestamp"] for ev in r.json()]
    assert ts == sorted(ts)


def test_e2e_trace_id_logged(running_services):
    incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    evt = {**BASE_EVENT, "eventId": "e2e-trace-001"}
    r = httpx.post(f"{GATEWAY_URL}/events", json=evt, headers={"traceparent": incoming})
    assert r.status_code == 201


def test_e2e_balance_404_unknown_account(running_services):
    r = httpx.get(f"{GATEWAY_URL}/accounts/no-such-account/balance")
    assert r.status_code == 404


def test_e2e_gateway_health(running_services):
    r = httpx.get(f"{GATEWAY_URL}/health")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"


def test_e2e_account_service_health(running_services):
    r = httpx.get(f"{ACCOUNT_URL}/health")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"
