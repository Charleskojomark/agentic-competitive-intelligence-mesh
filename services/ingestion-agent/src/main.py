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

# Define the Agent Card metadata for Ingestion Agent
INGESTION_AGENT_CARD = AgentCard(
    name="IngestionAgent",
    description="Monitors and scrapes raw text content from competitor URLs and data feeds.",
    version="1.0.0",
    url=os.getenv("AGENT_URL", "http://localhost:8001/api/v1/agent"),
    skills=[
        AgentSkill(
            id="monitor-and-scrape",
            name="Monitor and Scrape",
            description="Scrapes raw text from a list of target competitor URLs.",
            tags=["ingestion", "scraping"],
            examples=["Scrape competitor press pages: https://competitor.com/news"]
        )
    ]
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize shared database tables
    logger.info("Initializing database...")
    init_db()
    yield

app = FastAPI(
    title="Ingestion Agent Service",
    description="A2A-compliant Ingestion microservice",
    version="1.0.0",
    lifespan=lifespan
)

from src.graph import create_ingestion_graph
from shared.a2a_client import A2aClient
from shared.observability import trace_a2a_task

# Ingestion Logic utilizing LangGraph
def run_ingestion_task(task_id: str, inputs: dict):
    logger.info(f"Starting ingestion task {task_id} with inputs: {inputs}")
    trace_a2a_task("IngestionAgent", task_id, "monitor-and-scrape", inputs, "working")
    
    with get_db_session() as db:
        task = db.query(TaskRecord).filter(TaskRecord.task_id == task_id).first()
        if not task:
            logger.error(f"Task {task_id} not found in database.")
            trace_a2a_task("IngestionAgent", task_id, "monitor-and-scrape", inputs, "failed", error_message="Task not found in database")
            return
        
        try:
            task.status = "working"
            db.commit()

            urls = inputs.get("sources", [])
            logger.info(f"Running LangGraph scraping for URLs: {urls}")
            
            # Execute LangGraph
            ingestion_graph = create_ingestion_graph()
            initial_state = {
                "sources": urls,
                "raw_contents": [],
                "current_index": 0,
                "error": None
            }
            final_state = ingestion_graph.invoke(initial_state)

            scraped_data = final_state.get("raw_contents", [])

            task.status = "completed"
            task.outputs = {"raw_contents": scraped_data}
            db.commit()
            logger.info(f"Ingestion task {task_id} completed successfully.")
            trace_a2a_task("IngestionAgent", task_id, "monitor-and-scrape", inputs, "completed", outputs={"raw_contents": scraped_data})

            # Handoff: Trigger downstream Analysis Agent using A2A client
            analysis_url = os.getenv("ANALYSIS_AGENT_URL", "http://localhost:8002/api/v1/agent")
            analysis_client = A2aClient("AnalysisAgent", analysis_url)
            
            downstream_task_id = f"analysis-{task_id}"
            logger.info(f"A2A Handoff: Delegating task '{downstream_task_id}' to AnalysisAgent...")
            
            analysis_client.invoke("tasks/send", {
                "taskId": downstream_task_id,
                "skillId": "extract-competitive-insights",
                "inputs": {
                    "raw_contents": scraped_data
                }
            })
            
        except Exception as e:
            logger.error(f"Task {task_id} failed: {e}")
            task.status = "failed"
            task.error_message = str(e)
            db.commit()
            trace_a2a_task("IngestionAgent", task_id, "monitor-and-scrape", inputs, "failed", error_message=str(e))
            from shared.database import log_to_dlq
            log_to_dlq(task_id, "client/orchestrator", "IngestionAgent", "monitor-and-scrape", inputs, str(e))

# --- A2A Protocol Endpoints ---

@app.get("/.well-known/agent.json", response_model=AgentCard, summary="A2A Discovery Agent Card")
def get_agent_card():
    return INGESTION_AGENT_CARD

@app.post("/api/v1/agent", response_model=JsonRpcResponse, summary="A2A JSON-RPC Handler")
def handle_agent_request(
    request: JsonRpcRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    logger.info(f"Received JSON-RPC request. Method: {request.method}")

    # 1. Dispatch tasks/send
    if request.method == "tasks/send":
        try:
            params = TaskSendParams(**request.params)
        except Exception as e:
            return JsonRpcResponse(
                error=JsonRpcErrorDetails(code=-32602, message="Invalid params schema", data=str(e)),
                id=request.id
            )

        # Ensure skill is supported
        valid_skills = [s.id for s in INGESTION_AGENT_CARD.skills]
        if params.skillId not in valid_skills:
            return JsonRpcResponse(
                error=JsonRpcErrorDetails(code=-32601, message=f"Skill '{params.skillId}' not supported by this agent"),
                id=request.id
            )

        # Check if task already exists
        existing_task = db.query(TaskRecord).filter(TaskRecord.task_id == params.taskId).first()
        if existing_task:
            return JsonRpcResponse(
                error=JsonRpcErrorDetails(code=-32000, message="Task ID already exists"),
                id=request.id
            )

        # Create Task Record
        new_task = TaskRecord(
            task_id=params.taskId,
            skill_id=params.skillId,
            status="submitted",
            inputs=params.inputs
        )
        db.add(new_task)
        db.commit()

        # Dispatch background processing execution
        background_tasks.add_task(run_ingestion_task, params.taskId, params.inputs)

        result = TaskSendResult(
            taskId=params.taskId,
            status="submitted",
            estimatedDurationSeconds=10
        )
        return JsonRpcResponse(result=result.model_dump(), id=request.id)

    # 2. Dispatch tasks/get
    elif request.method == "tasks/get":
        task_id = request.params.get("taskId")
        if not task_id:
            return JsonRpcResponse(
                error=JsonRpcErrorDetails(code=-32602, message="Missing parameter 'taskId'"),
                id=request.id
            )

        task = db.query(TaskRecord).filter(TaskRecord.task_id == task_id).first()
        if not task:
            return JsonRpcResponse(
                error=JsonRpcErrorDetails(code=-32001, message=f"Task {task_id} not found"),
                id=request.id
            )

        task_info = {
            "taskId": task.task_id,
            "status": task.status,
            "inputs": task.inputs,
            "outputs": task.outputs,
            "error_message": task.error_message
        }
        return JsonRpcResponse(result=task_info, id=request.id)

    # 3. Method Not Found
    else:
        return JsonRpcResponse(
            error=JsonRpcErrorDetails(code=-32601, message=f"Method '{request.method}' not found"),
            id=request.id
        )
