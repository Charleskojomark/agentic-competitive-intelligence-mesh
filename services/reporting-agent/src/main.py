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

REPORTING_AGENT_CARD = AgentCard(
    name="ReportingAgent",
    description="Compiles structured market intelligence reports and SWOT analyses from extracted competitor insights.",
    version="1.0.0",
    url=os.getenv("AGENT_URL", "http://localhost:8003/api/v1/agent"),
    skills=[
        AgentSkill(
            id="generate-intelligence-report",
            name="Generate Intelligence Report",
            description="Compiles insights into a comprehensive intelligence markdown report.",
            tags=["reporting", "synthesis"],
            examples=["Compile report from competitor insights."]
        )
    ]
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    init_db()
    yield

app = FastAPI(
    title="Reporting Agent Service",
    description="A2A-compliant Reporting microservice",
    version="1.0.0",
    lifespan=lifespan
)

from src.graph import create_reporting_graph
from shared.a2a_client import A2aClient
from shared.observability import trace_a2a_task, score_task

# Reporting Logic utilizing LangGraph
def run_reporting_task(task_id: str, inputs: dict):
    logger.info(f"Starting reporting task {task_id}")
    trace_a2a_task("ReportingAgent", task_id, "generate-intelligence-report", inputs, "working")
    
    with get_db_session() as db:
        task = db.query(TaskRecord).filter(TaskRecord.task_id == task_id).first()
        if not task:
            trace_a2a_task("ReportingAgent", task_id, "generate-intelligence-report", inputs, "failed", error_message="Task not found in database")
            return
        
        try:
            task.status = "working"
            db.commit()

            insights = inputs.get("insights", [])
            sentiment = inputs.get("sentiment", "neutral")
            logger.info(f"Running LangGraph reporting graph for {len(insights)} insights...")

            # Execute LangGraph
            reporting_graph = create_reporting_graph()
            initial_state = {
                "insights": insights,
                "sentiment": sentiment,
                "report_markdown": "",
                "score": 0.0,
                "error": None
            }
            final_state = reporting_graph.invoke(initial_state)

            report_md = final_state.get("report_markdown")
            score = final_state.get("score", 8.0)

            task.status = "completed"
            task.outputs = {
                "report_id": f"rep-{task_id}",
                "report_markdown": report_md,
                "score": score
            }
            db.commit()
            logger.info(f"Reporting task {task_id} completed successfully. Grade: {score}")
            trace_a2a_task("ReportingAgent", task_id, "generate-intelligence-report", inputs, "completed", outputs=task.outputs)
            
            # Submit LLM Critic score
            score_task(task_id, "report-quality", score, f"Synthesized report critic evaluation grade for task {task_id}")

            # Handoff: Trigger downstream Alert Agent using A2A client
            alert_url = os.getenv("ALERT_AGENT_URL", "http://localhost:8004/api/v1/agent")
            alert_client = A2aClient("AlertAgent", alert_url)
            
            downstream_task_id = f"alert-{task_id}"
            logger.info(f"A2A Handoff: Delegating task '{downstream_task_id}' to AlertAgent...")
            
            alert_client.invoke("tasks/send", {
                "taskId": downstream_task_id,
                "skillId": "dispatch-notifications",
                "inputs": {
                    "report_id": f"rep-{task_id}",
                    "summary": f"New Market Intelligence Report compiled with grade {score} and {sentiment} sentiment.",
                    "severity": "critical" if sentiment == "bearish" else "info"
                }
            })
            
        except Exception as e:
            logger.error(f"Reporting task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = str(e)
            db.commit()
            trace_a2a_task("ReportingAgent", task_id, "generate-intelligence-report", inputs, "failed", error_message=str(e))
            from shared.database import log_to_dlq
            log_to_dlq(task_id, "AnalysisAgent", "ReportingAgent", "generate-intelligence-report", inputs, str(e))

# --- A2A Protocol Endpoints ---

@app.get("/.well-known/agent.json", response_model=AgentCard)
def get_agent_card():
    return REPORTING_AGENT_CARD

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

        valid_skills = [s.id for s in REPORTING_AGENT_CARD.skills]
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

        background_tasks.add_task(run_reporting_task, params.taskId, params.inputs)

        result = TaskSendResult(
            taskId=params.taskId,
            status="submitted",
            estimatedDurationSeconds=10
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
