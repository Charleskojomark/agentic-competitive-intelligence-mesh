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

ANALYSIS_AGENT_CARD = AgentCard(
    name="AnalysisAgent",
    description="Processes and analyzes raw crawled competitor contents using a specialized CrewAI agent crew.",
    version="1.0.0",
    url=os.getenv("AGENT_URL", "http://localhost:8002/api/v1/agent"),
    skills=[
        AgentSkill(
            id="extract-competitive-insights",
            name="Extract Competitive Insights",
            description="Analyzes competitor text to spot pricing tiers, product feature launches, and marketing posture.",
            tags=["analysis", "crewai"],
            examples=["Extract insights from pricing page scrape content."]
        )
    ]
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    init_db()
    yield

app = FastAPI(
    title="Analysis Agent Service",
    description="A2A-compliant Analysis microservice",
    version="1.0.0",
    lifespan=lifespan
)

from src.crew import run_competitor_analysis_crew
from shared.a2a_client import A2aClient
from shared.observability import trace_a2a_task

# Analysis Logic utilizing CrewAI
def run_analysis_task(task_id: str, inputs: dict):
    logger.info(f"Starting analysis task {task_id}")
    trace_a2a_task("AnalysisAgent", task_id, "extract-competitive-insights", inputs, "working")
    
    with get_db_session() as db:
        task = db.query(TaskRecord).filter(TaskRecord.task_id == task_id).first()
        if not task:
            trace_a2a_task("AnalysisAgent", task_id, "extract-competitive-insights", inputs, "failed", error_message="Task not found in database")
            return
        
        try:
            task.status = "working"
            db.commit()

            raw_contents = inputs.get("raw_contents", [])
            logger.info(f"Executing CrewAI analysis on {len(raw_contents)} contents...")

            # Run CrewAI crew
            analysis_result = run_competitor_analysis_crew(raw_contents)
            insights = analysis_result.get("insights", [])
            sentiment = analysis_result.get("sentiment", "neutral")

            task.status = "completed"
            task.outputs = {
                "insights": insights,
                "sentiment": sentiment
            }
            db.commit()
            logger.info(f"Analysis task {task_id} completed successfully.")
            trace_a2a_task("AnalysisAgent", task_id, "extract-competitive-insights", inputs, "completed", outputs=task.outputs)

            # Handoff: Trigger downstream Reporting Agent using A2A client
            reporting_url = os.getenv("REPORTING_AGENT_URL", "http://localhost:8003/api/v1/agent")
            reporting_client = A2aClient("ReportingAgent", reporting_url)
            
            downstream_task_id = f"report-{task_id}"
            logger.info(f"A2A Handoff: Delegating task '{downstream_task_id}' to ReportingAgent...")
            
            reporting_client.invoke("tasks/send", {
                "taskId": downstream_task_id,
                "skillId": "generate-intelligence-report",
                "inputs": {
                    "insights": insights,
                    "sentiment": sentiment
                }
            })
            
        except Exception as e:
            logger.error(f"Analysis task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = str(e)
            db.commit()
            trace_a2a_task("AnalysisAgent", task_id, "extract-competitive-insights", inputs, "failed", error_message=str(e))
            from shared.database import log_to_dlq
            log_to_dlq(task_id, "IngestionAgent", "AnalysisAgent", "extract-competitive-insights", inputs, str(e))

# --- A2A Protocol Endpoints ---

@app.get("/.well-known/agent.json", response_model=AgentCard)
def get_agent_card():
    return ANALYSIS_AGENT_CARD

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

        valid_skills = [s.id for s in ANALYSIS_AGENT_CARD.skills]
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

        background_tasks.add_task(run_analysis_task, params.taskId, params.inputs)

        result = TaskSendResult(
            taskId=params.taskId,
            status="submitted",
            estimatedDurationSeconds=15
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
