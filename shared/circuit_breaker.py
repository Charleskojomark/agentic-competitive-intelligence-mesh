import time
import logging
from typing import Callable, Any, Optional, Dict
import redis

logger = logging.getLogger(__name__)

class CircuitBreakerOpenException(Exception):
    """Raised when trying to execute a call through an open circuit breaker."""
    pass


class CircuitBreaker:
    # In-memory backup state for when Redis is not available
    _local_state: Dict[str, Dict[str, Any]] = {}

    def __init__(
        self,
        service_name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        redis_url: Optional[str] = None
    ):
        self.service_name = service_name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.redis_client = None

        # Setup Redis client if url is supplied
        if redis_url:
            try:
                self.redis_client = redis.Redis.from_url(redis_url, socket_timeout=2.0)
                # Test connectivity
                self.redis_client.ping()
                logger.info(f"CircuitBreaker for {service_name} configured with Redis.")
            except Exception as e:
                logger.warning(f"Failed to connect to Redis for CircuitBreaker. Falling back to local state: {e}")
                self.redis_client = None

        # Initialize local storage if not exists
        if service_name not in CircuitBreaker._local_state:
            CircuitBreaker._local_state[service_name] = {
                "state": "CLOSED",
                "failures": 0,
                "last_state_change": 0.0
            }

    def _get_key(self, field: str) -> str:
        return f"cb:{self.service_name}:{field}"

    def get_state(self) -> str:
        """Get the current circuit breaker state: CLOSED, OPEN, or HALF_OPEN."""
        now = time.time()
        if self.redis_client:
            try:
                state = self.redis_client.get(self._get_key("state"))
                if state:
                    state_str = state.decode("utf-8")
                else:
                    state_str = "CLOSED"
                
                if state_str == "OPEN":
                    last_change = float(self.redis_client.get(self._get_key("last_state_change")) or 0.0)
                    if now - last_change > self.recovery_timeout:
                        self.set_state("HALF_OPEN")
                        return "HALF_OPEN"
                return state_str
            except Exception as e:
                logger.error(f"Redis get_state error for {self.service_name}: {e}. Using local fallback.")

        # Fallback to local state
        local = CircuitBreaker._local_state[self.service_name]
        if local["state"] == "OPEN" and now - local["last_state_change"] > self.recovery_timeout:
            local["state"] = "HALF_OPEN"
            local["last_state_change"] = now
            logger.info(f"CircuitBreaker for {self.service_name} transitioned to HALF_OPEN (local timeout).")
        return local["state"]

    def set_state(self, state: str):
        """Update circuit state and log the change."""
        now = time.time()
        logger.warning(f"CircuitBreaker [{self.service_name}] State Change: -> {state}")
        
        if self.redis_client:
            try:
                self.redis_client.set(self._get_key("state"), state)
                self.redis_client.set(self._get_key("last_state_change"), str(now))
                if state == "CLOSED":
                    self.redis_client.set(self._get_key("failures"), "0")
                return
            except Exception as e:
                logger.error(f"Redis set_state error for {self.service_name}: {e}. Using local fallback.")

        # Local fallback
        local = CircuitBreaker._local_state[self.service_name]
        local["state"] = state
        local["last_state_change"] = now
        if state == "CLOSED":
            local["failures"] = 0

    def get_failures(self) -> int:
        if self.redis_client:
            try:
                return int(self.redis_client.get(self._get_key("failures")) or 0)
            except Exception as e:
                logger.error(f"Redis get_failures error for {self.service_name}: {e}. Using local fallback.")
        return CircuitBreaker._local_state[self.service_name]["failures"]

    def increment_failures(self) -> int:
        now = time.time()
        if self.redis_client:
            try:
                failures = self.redis_client.incr(self._get_key("failures"))
                if failures >= self.failure_threshold:
                    self.set_state("OPEN")
                return failures
            except Exception as e:
                logger.error(f"Redis increment_failures error for {self.service_name}: {e}. Using local fallback.")

        # Local fallback
        local = CircuitBreaker._local_state[self.service_name]
        local["failures"] += 1
        if local["failures"] >= self.failure_threshold:
            local["state"] = "OPEN"
            local["last_state_change"] = now
            logger.warning(f"CircuitBreaker [{self.service_name}] Tripped to OPEN (Local failure count: {local['failures']})")
        return local["failures"]

    def execute(self, func: Callable[..., Any], *args, **kwargs) -> Any:
        """
        Execute a function through the circuit breaker.
        Raises CircuitBreakerOpenException if the circuit is OPEN.
        """
        state = self.get_state()
        if state == "OPEN":
            raise CircuitBreakerOpenException(f"Circuit breaker for service '{self.service_name}' is OPEN.")

        try:
            result = func(*args, **kwargs)
            # If we succeed and we were in HALF_OPEN, close the circuit
            if state == "HALF_OPEN":
                self.set_state("CLOSED")
            return result
        except Exception as e:
            # Increment failure count
            if state in ("CLOSED", "HALF_OPEN"):
                self.increment_failures()
                if state == "HALF_OPEN":
                    # Any failure in HALF_OPEN trips it back to OPEN immediately
                    self.set_state("OPEN")
            raise e
