"""
Functional tests — business scenario coverage.

Each test class represents a real user story. Both services run as real
subprocesses (ports 19000 / 19001) for the duration of the module.
"""
import os
import sys
import time
import subprocess
import uuid
import pytest
import httpx

# Unique prefix per test session so runs never clash on leftover DB data
_RUN = uuid.uuid4().hex[:8]


def _id(name: str) -> str:
    return f"{_RUN}-{name}"

GATEWAY_PORT = 19000
ACCOUNT_PORT = 19001
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
def services():
    env = os.environ.copy()
    env["ACCOUNT_SERVICE_URL"] = ACCOUNT_URL

    ac = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(ACCOUNT_PORT), "--log-level", "error"],
        cwd=AC_DIR, env=env,
    )
    gw = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(GATEWAY_PORT), "--log-level", "error"],
        cwd=GW_DIR, env=env,
    )
    try:
        _wait_ready(ACCOUNT_URL)
        _wait_ready(GATEWAY_URL)
        yield
    finally:
        gw.terminate()
        ac.terminate()
        gw.wait(timeout=5)
        ac.wait(timeout=5)
        for db in [os.path.join(GW_DIR, "gateway.db"), os.path.join(AC_DIR, "account.db")]:
            try:
                if os.path.exists(db):
                    os.remove(db)
            except OSError:
                pass  # Windows may still hold the file briefly after process termination


def post_event(event_id, account_id, txn_type, amount,
               currency="USD", timestamp="2026-01-01T10:00:00Z", metadata=None):
    payload = {
        "eventId": event_id,
        "accountId": account_id,
        "type": txn_type,
        "amount": amount,
        "currency": currency,
        "eventTimestamp": timestamp,
    }
    if metadata:
        payload["metadata"] = metadata
    return httpx.post(f"{GATEWAY_URL}/events", json=payload)


# ---------------------------------------------------------------------------
# Scenario 1: New customer makes their first deposit
# ---------------------------------------------------------------------------

class TestFirstDeposit:
    def test_credit_is_accepted(self, services):
        r = post_event(_id("s1-evt1"), _id("acct-s1"), "CREDIT", 1000.00)
        assert r.status_code == 201

    def test_event_is_retrievable_by_id(self, services):
        r = httpx.get(f"{GATEWAY_URL}/events/{_id('s1-evt1')}")
        assert r.status_code == 200
        data = r.json()
        assert data["event_id"] == _id("s1-evt1")
        assert data["amount"] == 1000.00
        assert data["type"] == "CREDIT"

    def test_balance_reflects_deposit(self, services):
        r = httpx.get(f"{GATEWAY_URL}/accounts/{_id('acct-s1')}/balance")
        assert r.status_code == 200
        assert r.json()["balance"] == pytest.approx(1000.00)

    def test_event_appears_in_account_listing(self, services):
        r = httpx.get(f"{GATEWAY_URL}/events", params={"account": _id("acct-s1")})
        assert r.status_code == 200
        ids = [e["event_id"] for e in r.json()]
        assert _id("s1-evt1") in ids


# ---------------------------------------------------------------------------
# Scenario 2: Customer makes multiple deposits and withdrawals
# ---------------------------------------------------------------------------

class TestMultipleTransactions:
    def test_series_of_transactions(self, services):
        txns = [
            (_id("s2-evt1"), "CREDIT", 500.00, "2026-03-01T09:00:00Z"),
            (_id("s2-evt2"), "CREDIT", 200.00, "2026-03-02T10:00:00Z"),
            (_id("s2-evt3"), "DEBIT",  100.00, "2026-03-03T11:00:00Z"),
            (_id("s2-evt4"), "DEBIT",   50.00, "2026-03-04T12:00:00Z"),
            (_id("s2-evt5"), "CREDIT", 300.00, "2026-03-05T13:00:00Z"),
        ]
        for evt_id, txn_type, amount, ts in txns:
            r = post_event(evt_id, _id("acct-s2"), txn_type, amount, timestamp=ts)
            assert r.status_code == 201

    def test_final_balance_is_correct(self, services):
        # 500 + 200 - 100 - 50 + 300 = 850
        r = httpx.get(f"{GATEWAY_URL}/accounts/{_id('acct-s2')}/balance")
        assert r.status_code == 200
        assert r.json()["balance"] == pytest.approx(850.00)

    def test_events_listed_in_chronological_order(self, services):
        r = httpx.get(f"{GATEWAY_URL}/events", params={"account": _id("acct-s2")})
        assert r.status_code == 200
        timestamps = [e["event_timestamp"] for e in r.json()]
        assert timestamps == sorted(timestamps)

    def test_balance_breakdown_credits_and_debits(self, services):
        r = httpx.get(f"{GATEWAY_URL}/accounts/{_id('acct-s2')}/balance")
        data = r.json()
        assert data["credits"] == pytest.approx(1000.00)
        assert data["debits"] == pytest.approx(150.00)


# ---------------------------------------------------------------------------
# Scenario 3: Upstream system sends the same event more than once
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_first_submission_accepted(self, services):
        r = post_event(_id("s3-evt1"), _id("acct-s3"), "CREDIT", 400.00)
        assert r.status_code == 201

    def test_duplicate_returns_200_not_201(self, services):
        r = post_event(_id("s3-evt1"), _id("acct-s3"), "CREDIT", 400.00)
        assert r.status_code == 200

    def test_duplicate_returns_original_record(self, services):
        r = post_event(_id("s3-evt1"), _id("acct-s3"), "CREDIT", 400.00)
        assert r.json()["event_id"] == _id("s3-evt1")

    def test_balance_unchanged_after_duplicate(self, services):
        # Submit a third time — balance must still be 400, not 1200
        post_event(_id("s3-evt1"), _id("acct-s3"), "CREDIT", 400.00)
        r = httpx.get(f"{GATEWAY_URL}/accounts/{_id('acct-s3')}/balance")
        assert r.json()["balance"] == pytest.approx(400.00)

    def test_event_count_unchanged_after_duplicates(self, services):
        r = httpx.get(f"{GATEWAY_URL}/events", params={"account": _id("acct-s3")})
        assert len(r.json()) == 1


# ---------------------------------------------------------------------------
# Scenario 4: Events arrive out of chronological order
# ---------------------------------------------------------------------------

class TestOutOfOrderEvents:
    def test_submit_events_out_of_order(self, services):
        # Intentionally submit latest timestamp first
        txns = [
            (_id("s4-evt3"), "CREDIT", 300.00, "2026-06-03T15:00:00Z"),
            (_id("s4-evt1"), "CREDIT", 100.00, "2026-06-01T09:00:00Z"),
            (_id("s4-evt2"), "DEBIT",   50.00, "2026-06-02T12:00:00Z"),
        ]
        for evt_id, txn_type, amount, ts in txns:
            r = post_event(evt_id, _id("acct-s4"), txn_type, amount, timestamp=ts)
            assert r.status_code == 201

    def test_listing_is_chronological_regardless_of_arrival(self, services):
        r = httpx.get(f"{GATEWAY_URL}/events", params={"account": _id("acct-s4")})
        timestamps = [e["event_timestamp"] for e in r.json()]
        assert timestamps == sorted(timestamps)

    def test_balance_is_correct_regardless_of_arrival_order(self, services):
        # 100 - 50 + 300 = 350
        r = httpx.get(f"{GATEWAY_URL}/accounts/{_id('acct-s4')}/balance")
        assert r.json()["balance"] == pytest.approx(350.00)


# ---------------------------------------------------------------------------
# Scenario 5: Client submits invalid events
# ---------------------------------------------------------------------------

class TestValidation:
    def test_reject_unknown_transaction_type(self, services):
        r = post_event(_id("s5-bad1"), _id("acct-s5"), "TRANSFER", 100.00)
        assert r.status_code == 422

    def test_reject_zero_amount(self, services):
        r = post_event(_id("s5-bad2"), _id("acct-s5"), "CREDIT", 0.0)
        assert r.status_code == 422

    def test_reject_negative_amount(self, services):
        r = post_event(_id("s5-bad3"), _id("acct-s5"), "CREDIT", -50.0)
        assert r.status_code == 422

    def test_reject_missing_event_id(self, services):
        r = httpx.post(f"{GATEWAY_URL}/events", json={
            "accountId": _id("acct-s5"), "type": "CREDIT",
            "amount": 100.0, "currency": "USD",
            "eventTimestamp": "2026-01-01T00:00:00Z",
        })
        assert r.status_code == 422

    def test_reject_missing_account_id(self, services):
        r = httpx.post(f"{GATEWAY_URL}/events", json={
            "eventId": _id("s5-bad5"), "type": "CREDIT",
            "amount": 100.0, "currency": "USD",
            "eventTimestamp": "2026-01-01T00:00:00Z",
        })
        assert r.status_code == 422

    def test_reject_invalid_timestamp_format(self, services):
        r = post_event(_id("s5-bad6"), _id("acct-s5"), "CREDIT", 100.0, timestamp="not-a-date")
        assert r.status_code == 422

    def test_valid_events_unaffected_by_prior_rejections(self, services):
        r = post_event(_id("s5-good1"), _id("acct-s5"), "CREDIT", 100.0)
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# Scenario 6: Two accounts are fully isolated from each other
# ---------------------------------------------------------------------------

class TestMultiAccountIsolation:
    def test_transactions_on_separate_accounts(self, services):
        post_event(_id("s6-a-evt1"), _id("acct-s6a"), "CREDIT", 1000.00)
        post_event(_id("s6-b-evt1"), _id("acct-s6b"), "CREDIT",  250.00)
        post_event(_id("s6-b-evt2"), _id("acct-s6b"), "DEBIT",    75.00)

    def test_account_a_balance_unaffected_by_account_b(self, services):
        r = httpx.get(f"{GATEWAY_URL}/accounts/{_id('acct-s6a')}/balance")
        assert r.json()["balance"] == pytest.approx(1000.00)

    def test_account_b_balance_correct(self, services):
        r = httpx.get(f"{GATEWAY_URL}/accounts/{_id('acct-s6b')}/balance")
        assert r.json()["balance"] == pytest.approx(175.00)

    def test_account_a_event_listing_excludes_account_b_events(self, services):
        r = httpx.get(f"{GATEWAY_URL}/events", params={"account": _id("acct-s6a")})
        account_ids = {e["account_id"] for e in r.json()}
        assert account_ids == {_id("acct-s6a")}

    def test_unknown_account_balance_returns_404(self, services):
        r = httpx.get(f"{GATEWAY_URL}/accounts/{_id('no-such-account')}/balance")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Scenario 7: Event metadata is stored and returned correctly
# ---------------------------------------------------------------------------

class TestMetadata:
    def test_event_with_metadata_is_accepted(self, services):
        r = post_event(
            _id("s7-evt1"), _id("acct-s7"), "CREDIT", 500.00,
            metadata={"source": "mainframe-batch", "batchId": "B-9042"},
        )
        assert r.status_code == 201

    def test_metadata_is_returned_in_get_event(self, services):
        r = httpx.get(f"{GATEWAY_URL}/events/{_id('s7-evt1')}")
        meta = r.json().get("metadata")
        assert meta == {"source": "mainframe-batch", "batchId": "B-9042"}

    def test_event_without_metadata_is_accepted(self, services):
        r = post_event(_id("s7-evt2"), _id("acct-s7"), "DEBIT", 100.00)
        assert r.status_code == 201

    def test_event_without_metadata_returns_none(self, services):
        r = httpx.get(f"{GATEWAY_URL}/events/{_id('s7-evt2')}")
        assert r.json().get("metadata") is None


# ---------------------------------------------------------------------------
# Scenario 8: Health checks report correct service state
# ---------------------------------------------------------------------------

class TestHealthChecks:
    def test_gateway_health_ok(self, services):
        r = httpx.get(f"{GATEWAY_URL}/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["database"] == "ok"

    def test_gateway_exposes_circuit_state(self, services):
        r = httpx.get(f"{GATEWAY_URL}/health")
        assert "account_service_circuit" in r.json()

    def test_gateway_exposes_request_metrics(self, services):
        r = httpx.get(f"{GATEWAY_URL}/health")
        assert "metrics" in r.json()
        assert "request_counts" in r.json()["metrics"]

    def test_account_service_health_ok(self, services):
        r = httpx.get(f"{ACCOUNT_URL}/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["database"] == "ok"
