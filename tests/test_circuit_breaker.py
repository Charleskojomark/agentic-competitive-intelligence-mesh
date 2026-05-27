import pytest
import time
from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException

def test_circuit_breaker_flow():
    # Instantiate circuit breaker with threshold 3 and recovery timeout 2s
    cb = CircuitBreaker(
        service_name="test-service",
        failure_threshold=3,
        recovery_timeout=2
    )
    
    # Ensure starting in CLOSED state
    assert cb.get_state() == "CLOSED"
    assert cb.get_failures() == 0

    # Define a helper function that fails
    def failing_call():
        raise ValueError("Network partition error")

    # 1. Fail twice (below threshold)
    with pytest.raises(ValueError):
        cb.execute(failing_call)
    assert cb.get_failures() == 1
    assert cb.get_state() == "CLOSED"

    with pytest.raises(ValueError):
        cb.execute(failing_call)
    assert cb.get_failures() == 2
    assert cb.get_state() == "CLOSED"

    # 2. Fail third time (trips circuit)
    with pytest.raises(ValueError):
        cb.execute(failing_call)
    
    assert cb.get_state() == "OPEN"

    # 3. Subsequent calls should raise CircuitBreakerOpenException immediately without invoking function
    invoked = False
    def normal_call():
        nonlocal invoked
        invoked = True
        return "success"

    with pytest.raises(CircuitBreakerOpenException):
        cb.execute(normal_call)
    assert not invoked

    # 4. Wait for recovery timeout and check transition to HALF_OPEN
    time.sleep(2.1)
    assert cb.get_state() == "HALF_OPEN"

    # 5. Execute normal call successfully (closes breaker)
    res = cb.execute(normal_call)
    assert res == "success"
    assert cb.get_state() == "CLOSED"
    assert cb.get_failures() == 0
