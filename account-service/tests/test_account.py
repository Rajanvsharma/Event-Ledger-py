import pytest
from fastapi.testclient import TestClient
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database
database.DB_PATH = "file:acct_test?mode=memory&cache=shared"
database._conn = None

from main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    import database as db
    # Close and drop the shared in-memory database between tests
    if db._conn is not None:
        db._conn.close()
        db._conn = None
    db.DB_PATH = "file:acct_test?mode=memory&cache=shared"
    from main import startup
    startup()
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def _txn(event_id, txn_type, amount, account_id="acct-1", currency="USD",
         timestamp="2026-01-01T10:00:00Z"):
    return client.post(
        f"/accounts/{account_id}/transactions",
        json={
            "event_id": event_id,
            "type": txn_type,
            "amount": amount,
            "currency": currency,
            "event_timestamp": timestamp,
        },
    )


def test_apply_credit():
    r = _txn("evt-1", "CREDIT", 100.0)
    assert r.status_code == 201
    assert r.json()["amount"] == 100.0


def test_balance_after_credit_debit():
    _txn("evt-1", "CREDIT", 200.0)
    _txn("evt-2", "DEBIT", 50.0)
    r = client.get("/accounts/acct-1/balance")
    assert r.status_code == 200
    assert r.json()["balance"] == pytest.approx(150.0)


def test_idempotency():
    r1 = _txn("evt-dup", "CREDIT", 100.0)
    r2 = _txn("evt-dup", "CREDIT", 100.0)
    assert r1.status_code == 201
    assert r2.status_code == 201
    # Balance must not double-count
    r = client.get("/accounts/acct-1/balance")
    assert r.json()["balance"] == pytest.approx(100.0)


def test_out_of_order_balance():
    _txn("evt-late", "CREDIT", 300.0, timestamp="2026-01-01T12:00:00Z")
    _txn("evt-early", "DEBIT", 100.0, timestamp="2026-01-01T08:00:00Z")
    r = client.get("/accounts/acct-1/balance")
    assert r.json()["balance"] == pytest.approx(200.0)


def test_account_not_found():
    r = client.get("/accounts/no-such-account/balance")
    assert r.status_code == 404


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"
