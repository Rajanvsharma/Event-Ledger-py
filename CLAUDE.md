# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Run both services locally

```powershell
# Terminal 1
cd account-service
uvicorn main:app --port 8001 --reload

# Terminal 2
cd event-gateway
$env:ACCOUNT_SERVICE_URL="http://localhost:8001"
uvicorn main:app --port 8000 --reload
```

### Docker Compose

```powershell
docker-compose up --build
docker-compose down
```

### Tests

```powershell
# Account service unit tests (6 tests, no services needed)
cd account-service
python -m pytest tests/test_account.py -v

# Gateway unit tests (21 tests, no services needed)
cd event-gateway
python -m pytest tests/test_gateway.py -v

# End-to-end tests (10 tests — spins up real subprocesses on ports 18000/18001)
cd event-gateway
python -m pytest tests/test_e2e.py -v

# Single test
python -m pytest tests/test_gateway.py::test_idempotency_returns_200 -v

# With coverage
python -m pytest tests/test_gateway.py --cov=. --cov-report=term-missing
```

### Install dependencies

```powershell
cd account-service; pip install -r requirements.txt
cd event-gateway;   pip install -r requirements.txt
```

## Architecture

Two independent FastAPI services. Each has its own SQLite database — they share no state.

```
Client → Event Gateway (:8000) → Account Service (:8001)
```

**Event Gateway** is the only public-facing service. It owns the `events` table (stores all raw event submissions) and proxies balance reads to the Account Service.

**Account Service** is internal-only. It owns `accounts` and `transactions` tables and is the source of truth for balances.

### Idempotency — two layers

1. Gateway checks `event_id` UNIQUE constraint in its `events` table before forwarding. Duplicates return `200` with the original record, skipping the Account Service call entirely.
2. Account Service also checks `event_id` UNIQUE in `transactions` as a safety net in case of partial failures.

### Out-of-order events

Events are inserted in arrival order. All queries that return ordered results use `ORDER BY event_timestamp ASC`. Balance is always computed as a full `SUM` across all rows (`SUM(CREDIT) - SUM(DEBIT)`), so arrival order never affects correctness.

### Circuit breaker

`circuit_breaker.py` holds a single `pybreaker.CircuitBreaker` instance (`account_service_breaker`) used by both `call_account_service()` and `fetch_account_balance()` in `main.py`. Settings: `fail_max=3`, `reset_timeout=30s`. State is exposed in `GET /health`. Both functions wrap the inner HTTP call with `@account_service_breaker` so the decorator intercepts failures before they hit the caller.

### Distributed tracing

`tracing.py` in each service sets up an OTel `TracerProvider` with a `ConsoleSpanExporter`. The Gateway middleware extracts an incoming `traceparent` header (W3C format), starts a server span, and `inject_trace_headers()` injects the current span context into outbound headers to the Account Service. Both services call `log()` — which reads `trace.get_current_span()` — so the same `trace_id` appears in every log line for a given request across both services.

### Logging

Each service calls `_make_logger()` at module load time. This creates two handlers on the named logger: a `StreamHandler` (stdout) and a `FileHandler` writing to `logs/YYYY-MM-DD_HH-MM-SS_<service>.log` at the project root. All log output goes through the `log(level, message, **kwargs)` helper which serialises a fixed-field JSON object. `LOGS_DIR` env var overrides the default path (used by Docker Compose to point both containers at the same mounted volume).

### Database

Both `database.py` modules use the same pattern: a single global `sqlite3.Connection` (`_conn`) with a `threading.Lock` (`_lock`) protecting every read/write. `check_same_thread=False` lets FastAPI worker threads share it. `get_db()` is a context manager that commits on exit or rolls back on exception. In tests, `_conn` is closed and set to `None` between tests and `DB_PATH` is set to a shared in-memory URI (`file:*?mode=memory&cache=shared`) so all threads in the test process see the same database.

### Environment variables

| Variable | Service | Default | Purpose |
|---|---|---|---|
| `ACCOUNT_SERVICE_URL` | Gateway | `http://localhost:8001` | Base URL for Account Service calls |
| `LOGS_DIR` | Both | `../logs` (relative to service dir) | Where log files are written |

## Key files

- `event-gateway/main.py` — all Gateway routes, circuit breaker error handling, `log()`, `_make_logger()`
- `event-gateway/circuit_breaker.py` — `account_service_breaker` singleton
- `event-gateway/tests/test_e2e.py` — starts real subprocesses; use `scope="module"` fixture `running_services`
- `account-service/main.py` — all Account Service routes, balance SQL, `log()`, `_make_logger()`
- `logs/` — shared log output directory; `.log` files are gitignored, `.gitkeep` tracks the folder
