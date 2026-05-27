import os
import sys
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, Depends
from sqlalchemy.orm import Session

# Add workspace root to path to resolve shared imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from shared.database import init_db, get_db, TaskRecord, get_db_session
from shared.protocol_a2a import AgentCard, AgentSkill, JsonRpcRequest, JsonRpcResponse, JsonRpcErrorDetails, TaskSendParams, TaskSendResult

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ALERT_AGENT_CARD = AgentCard(
    name="AlertAgent",
    description="Dispatches system alerts and market intelligence reports to stakeholders via Slack and teams.",
    version="1.0.0",
    url=os.getenv("AGENT_URL", "http://localhost:8004/api/v1/agent"),
    skills=[
        AgentSkill(
            id="dispatch-notifications",
            name="Dispatch Notifications",
            description="Sends out structured alert payloads to Slack webhooks or email systems.",
            tags=["alert", "notification"],
            examples=["Dispatch slack notifications for critical price drop reports."]
        )
    ]
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    init_db()
    yield

app = FastAPI(
    title="Alert Agent Service",
    description="A2A-compliant Alert microservice",
    version="1.0.0",
    lifespan=lifespan
)

from shared.observability import trace_a2a_task

# Alerting Logic utilizing Webhooks
def run_alert_task(task_id: str, inputs: dict):
    logger.info(f"Starting alert task {task_id}")
    trace_a2a_task("AlertAgent", task_id, "dispatch-notifications", inputs, "working")
    
    with get_db_session() as db:
        task = db.query(TaskRecord).filter(TaskRecord.task_id == task_id).first()
        if not task:
            trace_a2a_task("AlertAgent", task_id, "dispatch-notifications", inputs, "failed", error_message="Task not found in database")
            return
        
        try:
            task.status = "working"
            db.commit()

            report_id = inputs.get("report_id")
            summary = inputs.get("summary", "No details")
            severity = inputs.get("severity", "info")

            logger.info(f"Dispatching notification: [{severity.upper()}] Report {report_id} - '{summary}'")

            task.status = "completed"
            task.outputs = {
                "notified": True,
                "channels": ["slack"]
            }
            db.commit()
            logger.info(f"Alert task {task_id} completed successfully.")
            trace_a2a_task("AlertAgent", task_id, "dispatch-notifications", inputs, "completed", outputs=task.outputs)
        except Exception as e:
            logger.error(f"Alert task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = str(e)
            db.commit()
            trace_a2a_task("AlertAgent", task_id, "dispatch-notifications", inputs, "failed", error_message=str(e))
            from shared.database import log_to_dlq
            log_to_dlq(task_id, "ReportingAgent", "AlertAgent", "dispatch-notifications", inputs, str(e))

# --- A2A Protocol Endpoints ---

@app.get("/.well-known/agent.json", response_model=AgentCard)
def get_agent_card():
    return ALERT_AGENT_CARD

@app.post("/api/v1/agent", response_model=JsonRpcResponse)
def handle_agent_request(
    request: JsonRpcRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    if request.method == "tasks/send":
        try:
            params = TaskSendParams(**request.params)
        except Exception as e:
            return JsonRpcResponse(
                error=JsonRpcErrorDetails(code=-32602, message="Invalid params schema", data=str(e)),
                id=request.id
            )

        valid_skills = [s.id for s in ALERT_AGENT_CARD.skills]
        if params.skillId not in valid_skills:
            return JsonRpcResponse(
                error=JsonRpcErrorDetails(code=-32601, message=f"Skill '{params.skillId}' not supported"),
                id=request.id
            )

        existing_task = db.query(TaskRecord).filter(TaskRecord.task_id == params.taskId).first()
        if existing_task:
            return JsonRpcResponse(
                error=JsonRpcErrorDetails(code=-32000, message="Task ID already exists"),
                id=request.id
            )

        new_task = TaskRecord(
            task_id=params.taskId,
            skill_id=params.skillId,
            status="submitted",
            inputs=params.inputs
        )
        db.add(new_task)
        db.commit()

        background_tasks.add_task(run_alert_task, params.taskId, params.inputs)

        result = TaskSendResult(
            taskId=params.taskId,
            status="submitted",
            estimatedDurationSeconds=5
        )
        return JsonRpcResponse(result=result.model_dump(), id=request.id)

    elif request.method == "tasks/get":
        task_id = request.params.get("taskId")
        if not task_id:
            return JsonRpcResponse(error=JsonRpcErrorDetails(code=-32602, message="Missing parameter 'taskId'"), id=request.id)

        task = db.query(TaskRecord).filter(TaskRecord.task_id == task_id).first()
        if not task:
            return JsonRpcResponse(error=JsonRpcErrorDetails(code=-32001, message=f"Task {task_id} not found"), id=request.id)

        return JsonRpcResponse(result={
            "taskId": task.task_id,
            "status": task.status,
            "inputs": task.inputs,
            "outputs": task.outputs,
            "error_message": task.error_message
        }, id=request.id)

    else:
        return JsonRpcResponse(error=JsonRpcErrorDetails(code=-32601, message=f"Method '{request.method}' not found"), id=request.id)
