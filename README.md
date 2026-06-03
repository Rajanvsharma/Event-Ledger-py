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

---

## Prerequisites

- Python 3.11+
- Docker + Docker Compose (for containerised run)

---

## Option 1 — Docker Compose (recommended)

```bash
docker-compose up --build
```

Both services start automatically. The gateway waits for the account service health check to pass before starting.

- Event Gateway → `http://localhost:8000`
- Account Service → `http://localhost:8001`

To stop:
```bash
docker-compose down
```

---

## Option 2 — Local (without Docker)

### Step 1 — Install dependencies (first time only)

```bash
cd account-service && pip install -r requirements.txt
cd event-gateway   && pip install -r requirements.txt
```

### Step 2 — Start both services (two terminals)

**Terminal 1 — Account Service:**
```bash
cd account-service
uvicorn main:app --port 8001 --reload
```

**Terminal 2 — Event Gateway:**

Linux / macOS:
```bash
cd event-gateway
ACCOUNT_SERVICE_URL=http://localhost:8001 uvicorn main:app --port 8000 --reload
```

Windows PowerShell:
```powershell
cd event-gateway
$env:ACCOUNT_SERVICE_URL="http://localhost:8001"
uvicorn main:app --port 8000 --reload
```

---

## Verify both services are running

```bash
curl http://localhost:8000/health
curl http://localhost:8001/health
```

Expected:
```json
{"service": "event-gateway", "status": "ok", "database": "ok", ...}
```

---

## Try it out

**Submit a CREDIT event:**
```bash
curl -X POST http://localhost:8000/events \
  -H "Content-Type: application/json" \
  -d '{"eventId":"evt-001","accountId":"acct-123","type":"CREDIT","amount":500.00,"currency":"USD","eventTimestamp":"2026-05-15T10:00:00Z"}'
```

**Submit a DEBIT event:**
```bash
curl -X POST http://localhost:8000/events \
  -H "Content-Type: application/json" \
  -d '{"eventId":"evt-002","accountId":"acct-123","type":"DEBIT","amount":150.00,"currency":"USD","eventTimestamp":"2026-05-15T11:00:00Z"}'
```

**Check balance** (should be 350.00):
```bash
curl http://localhost:8000/accounts/acct-123/balance
```

**List events in chronological order:**
```bash
curl "http://localhost:8000/events?account=acct-123"
```

**Get a single event:**
```bash
curl http://localhost:8000/events/evt-001
```

---

## Interactive API docs

Open in your browser — no curl needed:

| URL | Description |
|---|---|
| `http://localhost:8000/docs` | Gateway Swagger UI — test all endpoints |
| `http://localhost:8001/docs` | Account Service Swagger UI |
| `http://localhost:8000/health` | Gateway health + circuit state + metrics |

---

## Check the logs

Both services write JSON logs to the `logs/` folder at the project root. A new file is created on each startup:

```
logs/
├── 2026-06-02_10-00-00_event-gateway.log
└── 2026-06-02_10-00-01_account-service.log
```

**Windows PowerShell — follow a log live:**
```powershell
Get-Content "logs\*event-gateway*"   -Wait
Get-Content "logs\*account-service*" -Wait
```

**Linux / macOS:**
```bash
tail -f logs/*event-gateway*
tail -f logs/*account-service*
```

---

## Running the tests

### All tests

```bash
# Account Service — 6 unit tests
cd account-service
python -m pytest tests/ -v

# Event Gateway — 67 tests (unit + e2e + functional)
cd event-gateway
python -m pytest tests/ -v
```

### Individual suites

```bash
cd event-gateway

# Unit tests only (fast, no services needed — ~1s)
python -m pytest tests/test_gateway.py -v

# End-to-end tests (real HTTP, ~2 min)
python -m pytest tests/test_e2e.py -v

# Functional / scenario tests (real HTTP, ~3 min)
python -m pytest tests/test_functional.py -v
```

### Single test

```bash
python -m pytest tests/test_gateway.py::test_idempotency_returns_200 -v

# By class (functional)
python -m pytest tests/test_functional.py::TestIdempotency -v

# By keyword
python -m pytest tests/ -k "balance" -v
```

### With coverage

```bash
cd account-service
python -m pytest tests/test_account.py --cov=. --cov-report=term-missing

cd event-gateway
python -m pytest tests/test_gateway.py --cov=. --cov-report=term-missing
```

### Expected results

| Suite | Tests |
|---|---|
| account-service unit | 6 |
| event-gateway unit | 21 |
| event-gateway e2e | 10 |
| event-gateway functional | 36 |
| **Total** | **73** |

---

## Resiliency Pattern — Circuit Breaker

The Event Gateway wraps every call to the Account Service in a **circuit breaker** (via `pybreaker`):

- **Closed** (normal): requests flow through to the Account Service.
- **Open** (tripped): after 3 consecutive failures, the breaker opens for 30 seconds. The gateway immediately returns `503 Service Unavailable` instead of waiting for a timeout.
- **Half-open** (probing): after the reset timeout, one request is allowed through. If it succeeds the circuit closes; if it fails the circuit stays open.

Circuit state is always visible at `GET /health` on the gateway.

---

## API Quick Reference

### Event Gateway (port 8000)

| Method | Endpoint | Description |
|---|---|---|
| POST | /events | Submit a transaction event |
| GET | /events/{id} | Get event by ID |
| GET | /events?account={id} | List events for account (ordered by timestamp) |
| GET | /accounts/{id}/balance | Get account balance (proxied to Account Service) |
| GET | /health | Health + circuit breaker state + metrics |

### Account Service (port 8001)

| Method | Endpoint | Description |
|---|---|---|
| POST | /accounts/{id}/transactions | Apply a transaction |
| GET | /accounts/{id}/balance | Get current balance |
| GET | /accounts/{id} | Get account + transaction history |
| GET | /health | Health check |
