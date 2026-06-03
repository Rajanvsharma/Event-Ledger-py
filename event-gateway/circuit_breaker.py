import pybreaker
import logging

logger = logging.getLogger("event-gateway")

account_service_breaker = pybreaker.CircuitBreaker(
    fail_max=3,
    reset_timeout=30,
    name="account-service",
    listeners=[],
)


class CircuitOpenError(Exception):
    pass
