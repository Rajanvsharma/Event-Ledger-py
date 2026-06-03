# Event Ledger

Two microservices that process financial transaction events with idempotency, out-of-order tolerance, distributed tracing, and resiliency.

## Architecture

```
Browser / Client ──→  Event Gateway API (port 8000)
                             │ REST (sync)
                             ▼
                      Account Service (port 8001)
```

**Event Gateway** — public-facing. Accepts `POST /events`, enforces idempotency, stores events in its own SQLite DB, then forwards each transaction to the Account Service. Exposes `GET /events/{id}` and `GET /events?account=` that work even when the Account Service is down.

**Account Service** — internal only. Applies transactions to accounts in its own SQLite DB, computes balances, and exposes account/balance queries.

Each service has its own embedded SQLite database and runs as an independent process. They share no in-process state.

## Prerequisites

- Python 3.11+ (for running locally)
- Docker + Docker Compose (for containerised run)

## Setup — local (without Docker)

```bash
# Terminal 1 — Account Service
cd account-service
pip install -r requirements.txt
uvicorn main:app --port 8001

# Terminal 2 — Event Gateway
cd event-gateway
pip install -r requirements.txt
ACCOUNT_SERVICE_URL=http://localhost:8001 uvicorn main:app --port 8000
```

On Windows PowerShell:
```powershell
# Terminal 2 — Event Gateway
cd event-gateway
pip install -r requirements.txt
$env:ACCOUNT_SERVICE_URL="http://localhost:8001"; uvicorn main:app --port 8000
```

## Setup — Docker Compose

```bash
docker-compose up --build
```

Both services start. The gateway waits for the account service health check to pass before starting.

## Running the tests

Each service has its own test suite. Run from the repo root:

```bash
# Account Service tests
cd account-service
pip install -r requirements.txt
pytest tests/ -v

# Event Gateway tests
cd event-gateway
pip install -r requirements.txt
pytest tests/ -v
```

## Resiliency Pattern — Circuit Breaker

The Event Gateway wraps every call to the Account Service in a **circuit breaker** (via `pybreaker`):

- **Closed** (normal): requests flow through to the Account Service.
- **Open** (tripped): after 3 consecutive failures, the breaker opens for 30 seconds. The gateway immediately returns `503 Service Unavailable` instead of waiting for a timeout — protecting the gateway's resources and giving the Account Service time to recover.
- **Half-open** (probing): after the reset timeout, one request is allowed through. If it succeeds the circuit closes; if it fails the circuit stays open.

This was chosen over a simple timeout+retry because it provides automatic recovery without hammering a struggling downstream service, and it gives the gateway clear observable state (circuit state is visible in `GET /health`).

## API Quick Reference

### Event Gateway (port 8000)

| Method | Endpoint | Description |
|---|---|---|
| POST | /events | Submit a transaction event |
| GET | /events/{id} | Get event by ID |
| GET | /events?account={id} | List events for account (ordered by timestamp) |
| GET | /health | Health + circuit breaker state + metrics |

### Account Service (port 8001)

| Method | Endpoint | Description |
|---|---|---|
| POST | /accounts/{id}/transactions | Apply a transaction |
| GET | /accounts/{id}/balance | Get current balance |
| GET | /accounts/{id} | Get account + transaction history |
| GET | /health | Health check |

## Example request

```bash
curl -X POST http://localhost:8000/events \
  -H "Content-Type: application/json" \
  -d '{
    "eventId": "evt-001",
    "accountId": "acct-123",
    "type": "CREDIT",
    "amount": 150.00,
    "currency": "USD",
    "eventTimestamp": "2026-05-15T14:02:11Z",
    "metadata": {"source": "mainframe-batch", "batchId": "B-9042"}
  }'
```
