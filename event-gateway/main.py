import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
import pybreaker
from fastapi import FastAPI, HTTPException, Query, Request
from opentelemetry import propagate, trace
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, field_validator

from circuit_breaker import account_service_breaker
from database import get_db, init_db
from tracing import get_tracer, inject_trace_headers, setup_tracing

setup_tracing()
propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))

ACCOUNT_SERVICE_URL = os.getenv("ACCOUNT_SERVICE_URL", "http://localhost:8001")

_SERVICE_NAME = "event-gateway"

# --- Structured logger ---

def _make_logger() -> logging.Logger:
    logs_dir = os.getenv(
        "LOGS_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"),
    )
    os.makedirs(logs_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(logs_dir, f"{timestamp}_{_SERVICE_NAME}.log")

    fmt = logging.Formatter("%(message)s")
    l = logging.getLogger(_SERVICE_NAME)
    if not l.handlers:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        l.addHandler(console)

        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        l.addHandler(fh)

    l.setLevel(logging.INFO)
    l.propagate = False
    return l

logger = _make_logger()
logging.basicConfig(level=logging.INFO)

_request_count: dict[str, int] = {}
_error_count: dict[str, int] = {}


def log(level: str, message: str, **kwargs):
    span = trace.get_current_span()
    ctx = span.get_span_context()
    trace_id = format(ctx.trace_id, "032x") if ctx.trace_id else "none"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "event-gateway",
        "level": level,
        "trace_id": trace_id,
        "message": message,
        **kwargs,
    }
    getattr(logger, level.lower(), logger.info)(json.dumps(payload))


# --- App ---

app = FastAPI(title="Event Gateway API")


@app.on_event("startup")
def startup():
    init_db()
    log("INFO", "Event gateway started")


@app.middleware("http")
async def trace_and_metrics_middleware(request: Request, call_next):
    carrier = dict(request.headers)
    ctx = propagate.extract(carrier)
    tracer = get_tracer()
    path = request.url.path
    method = request.method

    _request_count[f"{method} {path}"] = _request_count.get(f"{method} {path}", 0) + 1

    with tracer.start_as_current_span(
        f"{method} {path}",
        context=ctx,
        kind=trace.SpanKind.SERVER,
    ):
        response = await call_next(request)
        if response.status_code >= 400:
            _error_count[f"{method} {path}"] = _error_count.get(f"{method} {path}", 0) + 1
        return response


# --- Models ---

class EventPayload(BaseModel):
    eventId: str
    accountId: str
    type: str
    amount: float
    currency: str
    eventTimestamp: str
    metadata: Optional[dict] = None

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        if v not in ("CREDIT", "DEBIT"):
            raise ValueError("type must be CREDIT or DEBIT")
        return v

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v):
        if v <= 0:
            raise ValueError("amount must be greater than 0")
        return v

    @field_validator("eventTimestamp")
    @classmethod
    def validate_timestamp(cls, v):
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("eventTimestamp must be a valid ISO 8601 datetime")
        return v


def row_to_event(row) -> dict:
    d = dict(row)
    if d.get("metadata"):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except Exception:
            pass
    return d


def call_account_service(event_id: str, account_id: str, txn_type: str,
                          amount: float, currency: str, event_timestamp: str) -> dict:
    headers: dict = {}
    inject_trace_headers(headers)

    @account_service_breaker
    def _call():
        url = f"{ACCOUNT_SERVICE_URL}/accounts/{account_id}/transactions"
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(url, json={
                "event_id": event_id,
                "type": txn_type,
                "amount": amount,
                "currency": currency,
                "event_timestamp": event_timestamp,
            }, headers=headers)
            resp.raise_for_status()
            return resp.json()

    return _call()


def fetch_account_balance(account_id: str) -> dict:
    headers: dict = {}
    inject_trace_headers(headers)

    @account_service_breaker
    def _call():
        url = f"{ACCOUNT_SERVICE_URL}/accounts/{account_id}/balance"
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()

    return _call()


# --- Routes ---

@app.post("/events", status_code=201)
def submit_event(body: EventPayload):
    tracer = get_tracer()
    with tracer.start_as_current_span("submit_event"):
        log("INFO", "Received event", event_id=body.eventId, account_id=body.accountId)
        now = datetime.now(timezone.utc).isoformat()

        with get_db() as conn:
            existing = conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (body.eventId,)
            ).fetchone()

            if existing:
                log("INFO", "Duplicate event", event_id=body.eventId)
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=200, content=row_to_event(existing))

            conn.execute(
                """INSERT INTO events
                   (event_id, account_id, type, amount, currency, event_timestamp, metadata, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (body.eventId, body.accountId, body.type, body.amount,
                 body.currency, body.eventTimestamp,
                 json.dumps(body.metadata) if body.metadata else None, now),
            )

        try:
            call_account_service(
                body.eventId, body.accountId, body.type,
                body.amount, body.currency, body.eventTimestamp,
            )
        except pybreaker.CircuitBreakerError:
            log("ERROR", "Circuit open — account service unavailable", event_id=body.eventId)
            raise HTTPException(status_code=503, detail="Account service unavailable (circuit open)")
        except httpx.TimeoutException:
            log("ERROR", "Timeout calling account service", event_id=body.eventId)
            raise HTTPException(status_code=503, detail="Account service timed out")
        except httpx.HTTPStatusError as exc:
            log("ERROR", "Account service error", status=exc.response.status_code, event_id=body.eventId)
            raise HTTPException(status_code=502, detail="Account service returned an error")
        except Exception as exc:
            log("ERROR", "Failed to reach account service", error=str(exc), event_id=body.eventId)
            raise HTTPException(status_code=503, detail="Account service unavailable")

        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (body.eventId,)
            ).fetchone()

        log("INFO", "Event stored and applied", event_id=body.eventId)
        return row_to_event(row)


@app.get("/events/{event_id}")
def get_event(event_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    return row_to_event(row)


@app.get("/events")
def list_events(account: str = Query(..., description="Account ID to filter by")):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE account_id = ? ORDER BY event_timestamp ASC",
            (account,),
        ).fetchall()
    return [row_to_event(r) for r in rows]


@app.get("/accounts/{account_id}/balance")
def get_balance(account_id: str):
    tracer = get_tracer()
    with tracer.start_as_current_span("get_balance_proxy"):
        log("INFO", "Proxying balance request", account_id=account_id)
        try:
            result = fetch_account_balance(account_id)
            return result
        except pybreaker.CircuitBreakerError:
            log("ERROR", "Circuit open — cannot fetch balance", account_id=account_id)
            raise HTTPException(status_code=503, detail="Account service unavailable (circuit open)")
        except httpx.TimeoutException:
            log("ERROR", "Timeout fetching balance", account_id=account_id)
            raise HTTPException(status_code=503, detail="Account service timed out")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(status_code=404, detail="Account not found")
            log("ERROR", "Account service error fetching balance", status=exc.response.status_code)
            raise HTTPException(status_code=502, detail="Account service returned an error")
        except Exception as exc:
            log("ERROR", "Failed to reach account service for balance", error=str(exc))
            raise HTTPException(status_code=503, detail="Account service unreachable")


@app.get("/health")
def health():
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"

    breaker_state = account_service_breaker.current_state
    return {
        "service": "event-gateway",
        "status": "ok",
        "database": db_status,
        "account_service_circuit": breaker_state,
        "metrics": {
            "request_counts": _request_count,
            "error_counts": _error_count,
        },
    }
