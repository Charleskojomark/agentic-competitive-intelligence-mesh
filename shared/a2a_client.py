import os
import logging
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from shared.circuit_breaker import CircuitBreaker, CircuitBreakerOpenException

logger = logging.getLogger(__name__)

# Jittered retry mechanism for handling transient connection errors
@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True
)
def _call_endpoint_with_retry(client: httpx.Client, url: str, payload: dict) -> dict:
    response = client.post(url, json=payload, timeout=30.0)
    response.raise_for_status()
    return response.json()


class A2aClient:
    def __init__(self, service_name: str, target_url: str):
        self.service_name = service_name
        self.target_url = target_url
        self.circuit_breaker = CircuitBreaker(
            service_name=service_name,
            redis_url=os.getenv("REDIS_URL")
        )

    def invoke(self, method: str, params: dict, request_id: int = 1) -> dict:
        """
        Invokes an A2A JSON-RPC 2.0 method over HTTP, protected by the Circuit Breaker.
        """
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": request_id
        }

        def execute_call():
            with httpx.Client() as client:
                return _call_endpoint_with_retry(client, self.target_url, payload)

        try:
            logger.info(f"Invoking method '{method}' on '{self.service_name}' at {self.target_url}")
            response_json = self.circuit_breaker.execute(execute_call)
            
            # Check for JSON-RPC error response format
            if "error" in response_json:
                error = response_json["error"]
                logger.error(f"A2A Server returned JSON-RPC error: {error}")
                raise ValueError(f"JSON-RPC Error: {error.get('message')} (code: {error.get('code')})")
                
            return response_json.get("result", {})
        except CircuitBreakerOpenException as e:
            logger.error(f"Circuit breaker for service '{self.service_name}' is OPEN. Blocking invocation.")
            raise e
        except Exception as e:
            logger.error(f"Failed A2A invocation to '{self.service_name}': {e}")
            raise e
