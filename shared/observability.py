import os
import logging
from typing import Optional, Any, Dict
from langfuse import Langfuse

logger = logging.getLogger(__name__)

# Initialize Langfuse client if API keys are present in the environment
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3000")

langfuse = None

if LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY:
    try:
        langfuse = Langfuse(
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
            host=LANGFUSE_HOST
        )
        logger.info(f"Langfuse initialized pointing to: {LANGFUSE_HOST}")
    except Exception as e:
        logger.error(f"Failed to initialize Langfuse client: {e}")
else:
    logger.info("Langfuse tracking is disabled (missing credentials).")


def trace_a2a_task(
    agent_name: str,
    task_id: str,
    skill_id: str,
    inputs: Dict[str, Any],
    status: str,
    outputs: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None
):
    """
    Log an A2A task step as a trace inside Langfuse.
    """
    if not langfuse:
        return

    try:
        # Create or update a trace
        trace = langfuse.trace(
            id=task_id,
            name=f"{agent_name} - {skill_id}",
            user_id="competitive-intelligence-system",
            tags=["a2a-pipeline", agent_name.lower()],
            metadata={
                "agent": agent_name,
                "skill_id": skill_id,
                "status": status,
                "inputs": inputs
            }
        )

        if outputs:
            trace.update(output=outputs)
        if error_message:
            trace.update(
                metadata={
                    "agent": agent_name,
                    "skill_id": skill_id,
                    "status": "failed",
                    "inputs": inputs,
                    "error": error_message
                }
            )
        logger.info(f"Langfuse: Traced {agent_name} task {task_id}")
    except Exception as e:
        logger.error(f"Failed to submit trace to Langfuse: {e}")


def score_task(
    task_id: str,
    score_name: str,
    value: float,
    comment: Optional[str] = None
):
    """
    Log a custom evaluation score (e.g. LLM Critic Quality Score) linked to the task trace.
    """
    if not langfuse:
        return

    try:
        langfuse.score(
            trace_id=task_id,
            name=score_name,
            value=value,
            comment=comment
        )
        logger.info(f"Langfuse: Logged score '{score_name}' = {value} for trace {task_id}")
    except Exception as e:
        logger.error(f"Failed to log score in Langfuse: {e}")
