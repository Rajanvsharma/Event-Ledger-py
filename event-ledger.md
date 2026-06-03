# Take-Home Project: Event Ledger

## Overview

 You'll create two microservices that work together to process financial transaction events, with attention to observability, resiliency, and operational readiness.

**You are expected and encouraged to use AI tools** (GitHub Copilot, ChatGPT, Claude, Cursor, or any other tool) to help you work — just as you would on the job. What we're evaluating is the quality of the end result and the engineering decisions you make along the way.

**Time expectation:** 3–4 hours to produce a working solution.

---

## The Problem

You are building an **Event Ledger** system composed of two microservices. The system receives financial transaction events from multiple upstream systems. These upstream systems are not perfectly synchronized, so:

- **Events may arrive out of order** — an event with an earlier timestamp may arrive after one with a later timestamp.
- **Events may be delivered more than once** — the same event could be sent to your API multiple times.

Your system must handle both of these scenarios correctly, and it must behave gracefully when parts of the system are unavailable.

---

## Architecture

```
                          ┌──────────────────────┐
Browser / Client ──────→  │  Event Gateway API    │
                          │  (public-facing)      │
                          └──────┬───────────────┘
                                 │ REST (sync)
                                 ▼
                          ┌──────────────────────┐
                          │  Account Service      │
                          │  (internal)           │
                          └──────────────────────┘
```

### Event Gateway API (public-facing)

The entry point for all client requests. Receives transaction events, validates input, enforces idempotency, stores event records, and calls the Account Service to apply transactions.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/events` | Submit a transaction event |
| `GET` | `/events/{id}` | Retrieve a single event by its ID |
| `GET` | `/events?account={accountId}` | List events for an account, ordered by event timestamp |
| `GET` | `/health` | Health check |

### Account Service (internal)

Manages account state — balances, transaction history, and account-level queries. Only called by the Gateway, not exposed to external clients.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/accounts/{accountId}/transactions` | Apply a transaction to an account |
| `GET` | `/accounts/{accountId}/balance` | Get the current balance for an account |
| `GET` | `/accounts/{accountId}` | Get account details and recent transactions |
| `GET` | `/health` | Health check |

---

## Event Payload

Submitted to `POST /events` on the Gateway:

```json
{
  "eventId": "evt-001",
  "accountId": "acct-123",
  "type": "CREDIT",
  "amount": 150.00,
  "currency": "USD",
  "eventTimestamp": "2026-05-15T14:02:11Z",
  "metadata": {
    "source": "mainframe-batch",
    "batchId": "B-9042"
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `eventId` | string | Yes | Unique identifier for the event |
| `accountId` | string | Yes | The account this event belongs to |
| `type` | string | Yes | Must be `"CREDIT"` or `"DEBIT"` |
| `amount` | number | Yes | Must be greater than 0 |
| `currency` | string | Yes | e.g., `"USD"` |
| `eventTimestamp` | string (ISO 8601) | Yes | When the event originally occurred |
| `metadata` | object | No | Optional additional context |

---

## Requirements

### 1. Core Functionality

- **Idempotency**: Submitting the same `eventId` more than once must not create a duplicate event or alter the account balance. On a duplicate submission, return the original event with an appropriate status code.
- **Out-of-order tolerance**: Events may arrive out of chronological order. Event listings must be in chronological order by `eventTimestamp`. Balances must always be correct regardless of arrival order.
- **Balance computation**: Net balance = sum of CREDITs − sum of DEBITs.
- **Validation**: Reject events with missing required fields, negative/zero amounts, or unknown event types. Return meaningful error messages with appropriate HTTP status codes.

### 2. Service Separation

- The Event Gateway and Account Service must be **independently runnable processes**, each with its own embedded/in-memory database.
- They must not share a database or in-process state.
- Define clear API contracts between the services.

### 3. Distributed Tracing

Implement trace propagation across the Gateway → Account Service call. OpenTelemetry is preferred but not required. At minimum:

- Generate a **trace ID** at the Gateway for each incoming request.
- **Propagate** the trace ID to the Account Service via HTTP headers.
- Both services must **log the trace ID** in their structured log output.
- A single client request should produce a traceable path across both services.

### 4. Observability

- **Structured logging**: JSON-formatted logs with trace ID, timestamp, log level, and service name.
- **Health check endpoints**: `GET /health` on both services, returning service status and basic diagnostics (e.g., database connectivity).
- **At least one custom metric**: For example, request count by endpoint, error rate, or latency histogram. Expose via logs, an endpoint, or an observability library.

### 5. Resiliency

The Gateway must implement **at least one** resiliency pattern on its call to the Account Service:

| Pattern | Description |
|---|---|
| **Circuit breaker** | If the Account Service is repeatedly failing, stop calling it temporarily and return a meaningful error to the client |
| **Bulkhead** | Isolate the Account Service call so its failures don't exhaust the Gateway's thread pool or resources |
| **Timeout + retry with backoff** | Handle slow responses gracefully; don't retry indefinitely |

Implement at least one. Be prepared to explain your choice.

### 6. Graceful Degradation

When the Account Service is unavailable:

- `POST /events` — Return an appropriate error (e.g., `503 Service Unavailable`) rather than hanging or returning a `500`.
- `GET /events/{id}` and `GET /events?account=...` — Should **still work**, as they only depend on the Gateway's local data.
- Balance queries — Return a clear error indicating the Account Service is unreachable.

### 7. Docker Compose (Preferred)

Provide a `docker-compose.yml` that starts both services. If you choose not to use Docker, provide clear step-by-step instructions for starting both services locally.

### 8. Automated Tests

Include tests that cover:

- All core functionality (idempotency, out-of-order, balance, validation)
- **Resiliency behavior**: Simulate Account Service failure and verify the Gateway handles it correctly (circuit breaker opens, proper error responses, etc.)
- **Trace propagation**: Verify trace IDs flow from Gateway to Account Service
- **Integration**: At least one test that exercises the full Gateway → Account Service flow

Tests must be runnable with a standard command (e.g.,  `pytest`, `dotnet test`).

### 9. README

Include a `README.md` with:

- Architecture overview (a brief description of both services and how they interact)
- Setup instructions (prerequisites, how to install dependencies)
- How to start both services (Docker Compose or manual)
- How to run the tests
- A brief explanation of your resiliency pattern choice

---

## Constraints

- **Language:** Python, or C# (your choice)
- **Database:** Each service uses its own in-memory or embedded database (SQLite, etc.)
- **Communication:** Synchronous REST calls between services
- **Tracing:** OpenTelemetry preferred (not required)
- **Docker:** Docker Compose preferred for running (not required)
- **Framework:** Your choice (Flask/FastAPI, ASP.NET, etc.)

---

## Submission

- Submit your solution as a **Git repository** (GitHub, GitLab, Bitbucket, etc.)
- Your **commit history should reflect your working process** — please don't squash everything into a single commit

---

## Bonus Opportunities (Not Required)

If you finish early or want to go further, consider:

- OpenTelemetry Collector + Jaeger or Zipkin for trace visualization
- Prometheus metrics endpoint
- Retry with exponential backoff + jitter
- Rate limiting on the Gateway
- Contract tests (Pact or similar) between the two services
- Async fallback: queue events locally when Account Service is down, process when it recovers

---

## Questions?

If anything is unclear, please reach out to your recruiter or hiring manager. We'd rather you ask than guess.

Good luck, and have fun with it!
