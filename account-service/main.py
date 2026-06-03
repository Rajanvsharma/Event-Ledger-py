import logging
import json
import os
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from opentelemetry import trace
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry import propagate

from database import init_db, get_db
from tracing import setup_tracing, get_tracer

setup_tracing()
propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))

_SERVICE_NAME = "account-service"

def _make_logger() -> logging.Logger:
    logs_dir = os.getenv(
        "LOGS_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"),
    )
    os.makedirs(logs_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(logs_dir, f"{timestamp}_{_SERVICE_NAME}.log")

    fmt = logging.Formatter("%(message)s")
    logger = logging.getLogger(_SERVICE_NAME)
    if not logger.handlers:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        logger.addHandler(console)

        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger

logger = _make_logger()
logging.basicConfig(level=logging.INFO)


def log(level: str, message: str, **kwargs):
    span = trace.get_current_span()
    ctx = span.get_span_context()
    trace_id = format(ctx.trace_id, "032x") if ctx.trace_id else "none"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "account-service",
        "level": level,
        "trace_id": trace_id,
        "message": message,
        **kwargs,
    }
    getattr(logger, level.lower(), logger.info)(json.dumps(payload))


app = FastAPI(title="Account Service")


@app.on_event("startup")
def startup():
    init_db()
    log("INFO", "Account service started")


class TransactionRequest(BaseModel):
    event_id: str
    type: str
    amount: float
    currency: str
    event_timestamp: str

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


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    carrier = dict(request.headers)
    ctx = propagate.extract(carrier)
    tracer = get_tracer()
    with tracer.start_as_current_span(
        f"{request.method} {request.url.path}",
        context=ctx,
        kind=trace.SpanKind.SERVER,
    ):
        response = await call_next(request)
        return response


@app.post("/accounts/{account_id}/transactions", status_code=201)
def apply_transaction(account_id: str, body: TransactionRequest, request: Request):
    tracer = get_tracer()
    with tracer.start_as_current_span("apply_transaction"):
        log("INFO", "Applying transaction", account_id=account_id, event_id=body.event_id)
        now = datetime.now(timezone.utc).isoformat()

        with get_db() as conn:
            # Upsert account
            conn.execute(
                "INSERT OR IGNORE INTO accounts (account_id, created_at) VALUES (?, ?)",
                (account_id, now),
            )

            # Check idempotency
            existing = conn.execute(
                "SELECT * FROM transactions WHERE event_id = ?", (body.event_id,)
            ).fetchone()
            if existing:
                log("INFO", "Duplicate transaction ignored", event_id=body.event_id)
                return dict(existing)

            conn.execute(
                """INSERT INTO transactions
                   (account_id, event_id, type, amount, currency, event_timestamp, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (account_id, body.event_id, body.type, body.amount,
                 body.currency, body.event_timestamp, now),
            )

        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE event_id = ?", (body.event_id,)
            ).fetchone()

        log("INFO", "Transaction applied", event_id=body.event_id)
        return dict(row)


@app.get("/accounts/{account_id}/balance")
def get_balance(account_id: str):
    tracer = get_tracer()
    with tracer.start_as_current_span("get_balance"):
        with get_db() as conn:
            account = conn.execute(
                "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            if not account:
                raise HTTPException(status_code=404, detail="Account not found")

            row = conn.execute(
                """SELECT
                     COALESCE(SUM(CASE WHEN type='CREDIT' THEN amount ELSE 0 END), 0) as credits,
                     COALESCE(SUM(CASE WHEN type='DEBIT'  THEN amount ELSE 0 END), 0) as debits
                   FROM transactions WHERE account_id = ?""",
                (account_id,),
            ).fetchone()

        balance = round(row["credits"] - row["debits"], 10)
        log("INFO", "Balance retrieved", account_id=account_id, balance=balance)
        return {"account_id": account_id, "balance": balance, "credits": row["credits"], "debits": row["debits"]}


@app.get("/accounts/{account_id}")
def get_account(account_id: str):
    tracer = get_tracer()
    with tracer.start_as_current_span("get_account"):
        with get_db() as conn:
            account = conn.execute(
                "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
            if not account:
                raise HTTPException(status_code=404, detail="Account not found")

            txns = conn.execute(
                "SELECT * FROM transactions WHERE account_id = ? ORDER BY event_timestamp ASC",
                (account_id,),
            ).fetchall()

            balance_row = conn.execute(
                """SELECT
                     COALESCE(SUM(CASE WHEN type='CREDIT' THEN amount ELSE 0 END), 0) as credits,
                     COALESCE(SUM(CASE WHEN type='DEBIT'  THEN amount ELSE 0 END), 0) as debits
                   FROM transactions WHERE account_id = ?""",
                (account_id,),
            ).fetchone()

        return {
            "account_id": account_id,
            "created_at": account["created_at"],
            "balance": round(balance_row["credits"] - balance_row["debits"], 10),
            "transactions": [dict(t) for t in txns],
        }


@app.get("/health")
def health():
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"
    return {"service": "account-service", "status": "ok", "database": db_status}
