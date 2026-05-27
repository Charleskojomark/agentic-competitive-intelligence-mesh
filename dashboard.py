import os
import sys
import time
import uuid
import logging
import subprocess
import threading
import httpx
import sqlite3
from typing import Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# Ensure root is in PYTHONPATH
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from shared.database import init_db, SessionLocal, TaskRecord, FailedTaskRecord

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SystemDashboard")

# Global dict to keep track of subprocesses
processes: Dict[str, subprocess.Popen] = {}
processes_lock = threading.Lock()

def start_background_service(name: str, cmd: List[str], env: Dict[str, str]):
    """Launch a background process and log its state."""
    logger.info(f"Starting background service: {name} | Cmd: {' '.join(cmd)}")
    try:
        p = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        # Thread to read and stream logs to stdout
        def log_streamer(pipe, service_name):
            for line in iter(pipe.readline, ''):
                sys.stdout.write(f"[{service_name}] {line}")
                sys.stdout.flush()
            pipe.close()
            
        t = threading.Thread(target=log_streamer, args=(p.stdout, name), daemon=True)
        t.start()
        
        with processes_lock:
            processes[name] = p
    except Exception as e:
        logger.error(f"Failed to start service {name}: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Initialize database tables
    logger.info("Initializing database schema...")
    init_db()
    
    # 2. Build local environment
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.abspath(__file__))
    env["DATABASE_URL"] = os.getenv("DATABASE_URL", "sqlite:///./agent_tasks.db")
    env["REDIS_URL"] = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # 3. Spin up lightweight redis-server if available locally
    try:
        # Check if redis-server is installed
        subprocess.run(["redis-server", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        start_background_service(
            "redis-server",
            ["redis-server", "--port", "6379", "--protected-mode", "no"],
            env
        )
        # Give redis a moment to start
        time.sleep(1)
    except FileNotFoundError:
        logger.warning("redis-server binary not found in path. Relying on CircuitBreaker local memory fallback.")
        
    # 4. Start all 4 microservice agents
    agents = {
        "ingestion-agent": ["uvicorn", "services.ingestion-agent.src.main:app", "--host", "127.0.0.1", "--port", "8001"],
        "analysis-agent": ["uvicorn", "services.analysis-agent.src.main:app", "--host", "127.0.0.1", "--port", "8002"],
        "reporting-agent": ["uvicorn", "services.reporting-agent.src.main:app", "--host", "127.0.0.1", "--port", "8003"],
        "alert-agent": ["uvicorn", "services.alert-agent.src.main:app", "--host", "127.0.0.1", "--port", "8004"]
    }
    
    for name, cmd in agents.items():
        # Set agent-specific URL environments for proper routing
        agent_env = env.copy()
        agent_env["AGENT_URL"] = f"http://localhost:{cmd[-1]}/api/v1/agent"
        agent_env["INGESTION_AGENT_URL"] = "http://localhost:8001/api/v1/agent"
        agent_env["ANALYSIS_AGENT_URL"] = "http://localhost:8002/api/v1/agent"
        agent_env["REPORTING_AGENT_URL"] = "http://localhost:8003/api/v1/agent"
        agent_env["ALERT_AGENT_URL"] = "http://localhost:8004/api/v1/agent"
        
        start_background_service(name, cmd, agent_env)
        
    yield
    
    # 5. Shutdown all services on exit
    logger.info("Stopping all background services...")
    with processes_lock:
        for name, p in processes.items():
            logger.info(f"Terminating service: {name}")
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()

app = FastAPI(
    title="Competitive Intelligence Mesh Portal",
    description="Unified management panel and dashboard",
    version="1.0.0",
    lifespan=lifespan
)

class RunPipelineRequest(BaseModel):
    sources: List[str]
    groq_api_key: Optional[str] = None

@app.post("/api/run")
async def trigger_pipeline(req: RunPipelineRequest):
    # Set the key in environment if supplied
    if req.groq_api_key:
        os.environ["GROQ_API_KEY"] = req.groq_api_key
        
    if not os.getenv("GROQ_API_KEY") or os.getenv("GROQ_API_KEY").strip() == "":
        return JSONResponse(
            status_code=400,
            content={"detail": "Missing Groq API Key. Enter it in the form or set it in the environment."}
        )

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    
    # Payload for Ingestion Agent tasks/send method
    payload = {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "params": {
            "taskId": task_id,
            "skillId": "monitor-and-scrape",
            "inputs": {
                "sources": req.sources
            }
        },
        "id": 1
    }
    
    # Trigger Ingestion Agent
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post("http://127.0.0.1:8001/api/v1/agent", json=payload, timeout=5.0)
            resp.raise_for_status()
            result = resp.json()
            if "error" in result:
                raise HTTPException(status_code=500, detail=result["error"]["message"])
            return {"taskId": task_id, "status": "submitted"}
    except Exception as e:
        logger.error(f"Failed to communicate with IngestionAgent: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start pipeline: {str(e)}")

@app.get("/api/status/{root_task_id}")
def get_pipeline_status(root_task_id: str):
    """Gathers status for all agents involved in the pipeline mesh."""
    ingestion_id = root_task_id
    analysis_id = f"analysis-{root_task_id}"
    reporting_id = f"report-analysis-{root_task_id}"
    alert_id = f"alert-report-analysis-{root_task_id}"
    
    db = SessionLocal()
    try:
        records = db.query(TaskRecord).filter(
            TaskRecord.task_id.in_([ingestion_id, analysis_id, reporting_id, alert_id])
        ).all()
        
        # Index records
        status_map = {r.task_id: r for r in records}
        
        def format_node(task_id, name, skill):
            record = status_map.get(task_id)
            if not record:
                return {"name": name, "status": "pending", "inputs": None, "outputs": None, "error": None}
            return {
                "name": name,
                "status": record.status,
                "inputs": record.inputs,
                "outputs": record.outputs,
                "error": record.error_message
            }
            
        return {
            "ingestion": format_node(ingestion_id, "IngestionAgent (LangGraph)", "monitor-and-scrape"),
            "analysis": format_node(analysis_id, "AnalysisAgent (CrewAI)", "extract-competitive-insights"),
            "reporting": format_node(reporting_id, "ReportingAgent (LangGraph)", "generate-intelligence-report"),
            "alert": format_node(alert_id, "AlertAgent (Slack Broker)", "dispatch-notifications")
        }
    finally:
        db.close()

@app.get("/api/tasks")
def list_tasks():
    """Retrieve lists of last tasks and failed dead letter queue items."""
    db = SessionLocal()
    try:
        tasks = db.query(TaskRecord).order_by(TaskRecord.created_at.desc()).limit(15).all()
        failed = db.query(FailedTaskRecord).order_by(FailedTaskRecord.created_at.desc()).limit(15).all()
        
        return {
            "tasks": [
                {
                    "taskId": t.task_id,
                    "skillId": t.skill_id,
                    "status": t.status,
                    "inputs": t.inputs,
                    "outputs": t.outputs,
                    "error_message": t.error_message,
                    "created_at": t.created_at.strftime("%Y-%m-%d %H:%M:%S")
                } for t in tasks
            ],
            "dlq": [
                {
                    "id": f.id,
                    "taskId": f.task_id,
                    "source": f.source_agent,
                    "target": f.target_agent,
                    "skillId": f.skill_id,
                    "error_message": f.error_message,
                    "attempt_count": f.attempt_count,
                    "status": f.status,
                    "created_at": f.created_at.strftime("%Y-%m-%d %H:%M:%S")
                } for f in failed
            ]
        }
    finally:
        db.close()

@app.get("/api/services")
async def check_services():
    """Check availability of each individual A2A agent and Redis."""
    results = {}
    
    # Check agents
    agent_ports = {
        "IngestionAgent": "8001",
        "AnalysisAgent": "8002",
        "ReportingAgent": "8003",
        "AlertAgent": "8004"
    }
    
    async with httpx.AsyncClient() as client:
        for agent_name, port in agent_ports.items():
            try:
                resp = await client.get(f"http://127.0.0.1:{port}/.well-known/agent.json", timeout=1.0)
                if resp.status_code == 200:
                    results[agent_name] = {"status": "ONLINE", "details": resp.json()}
                else:
                    results[agent_name] = {"status": "ERROR", "details": f"Status code {resp.status_code}"}
            except Exception as e:
                results[agent_name] = {"status": "OFFLINE", "details": str(e)}
                
    # Check Redis
    import redis
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        r = redis.Redis.from_url(redis_url, socket_timeout=1.0)
        r.ping()
        results["RedisBroker"] = {"status": "ONLINE", "details": {"url": redis_url}}
    except Exception as e:
        results["RedisBroker"] = {"status": "OFFLINE", "details": str(e)}
        
    return results

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agentic Competitive Intelligence Mesh Portal</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800&family=Plus+Jakarta+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base: #0B0F19;
            --bg-surface: rgba(17, 24, 39, 0.7);
            --bg-card: rgba(31, 41, 55, 0.45);
            --accent-primary: #6366F1;
            --accent-secondary: #8B5CF6;
            --accent-glow: rgba(99, 102, 241, 0.15);
            --text-main: #F3F4F6;
            --text-muted: #9CA3AF;
            --border-color: rgba(255, 255, 255, 0.08);
            --success: #10B981;
            --working: #F59E0B;
            --failed: #EF4444;
            --pending: #6B7280;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Plus Jakarta Sans', sans-serif;
        }

        body {
            background-color: var(--bg-base);
            background-image: 
                radial-gradient(at 10% 20%, rgba(99, 102, 241, 0.05) 0px, transparent 50%),
                radial-gradient(at 90% 80%, rgba(139, 92, 246, 0.05) 0px, transparent 50%);
            color: var(--text-main);
            min-height: 100vh;
            padding: 2.5rem 1.5rem;
            display: flex;
            justify-content: center;
        }

        .container {
            width: 100%;
            max-width: 1250px;
            display: flex;
            flex-direction: column;
            gap: 2rem;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 1.5rem;
        }

        h1 {
            font-family: 'Outfit', sans-serif;
            font-size: 2.2rem;
            font-weight: 800;
            background: linear-gradient(135deg, #A5B4FC, #C084FC);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: -0.03em;
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .badge {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 99px;
            padding: 0.35rem 0.85rem;
            font-size: 0.8rem;
            font-weight: 600;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            color: var(--accent-primary);
        }

        .grid-main {
            display: grid;
            grid-template-columns: 1.1fr 1.9fr;
            gap: 2rem;
        }

        @media (max-width: 900px) {
            .grid-main {
                grid-template-columns: 1fr;
            }
        }

        .card {
            background: var(--bg-surface);
            backdrop-filter: blur(16px);
            border: 1px solid var(--border-color);
            border-radius: 1.25rem;
            padding: 2rem;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
            position: relative;
            overflow: hidden;
        }

        .card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 2px;
            background: linear-gradient(90deg, transparent, var(--accent-primary), var(--accent-secondary), transparent);
            opacity: 0.7;
        }

        h2 {
            font-family: 'Outfit', sans-serif;
            font-size: 1.4rem;
            font-weight: 700;
            letter-spacing: -0.02em;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        label {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            font-weight: 600;
            font-size: 0.875rem;
            color: var(--text-muted);
        }

        textarea, input[type="text"], input[type="password"] {
            background: rgba(17, 24, 39, 0.9);
            border: 1px solid var(--border-color);
            border-radius: 0.75rem;
            padding: 0.85rem 1rem;
            color: var(--text-main);
            font-size: 0.95rem;
            transition: all 0.25s ease;
            outline: none;
            width: 100%;
        }

        textarea:focus, input[type="text"]:focus, input[type="password"]:focus {
            border-color: var(--accent-primary);
            box-shadow: 0 0 12px var(--accent-glow);
        }

        button {
            background: linear-gradient(135deg, var(--accent-primary), var(--accent-secondary));
            color: white;
            border: none;
            border-radius: 0.75rem;
            padding: 1rem;
            font-weight: 700;
            font-size: 1rem;
            cursor: pointer;
            transition: all 0.25s ease;
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 0.5rem;
            box-shadow: 0 4px 15px rgba(99, 102, 241, 0.4);
        }

        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(99, 102, 241, 0.6);
        }

        button:active {
            transform: translateY(0);
        }

        /* Stepper Flow styling */
        .stepper {
            display: flex;
            flex-direction: column;
            gap: 1.25rem;
            margin-top: 0.5rem;
        }

        .step {
            display: flex;
            align-items: center;
            gap: 1rem;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 0.75rem;
            padding: 1rem;
            transition: all 0.3s ease;
        }

        .step.active {
            border-color: var(--accent-primary);
            box-shadow: 0 0 10px rgba(99, 102, 241, 0.1);
        }

        .step-icon {
            width: 2.25rem;
            height: 2.25rem;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 1.1rem;
            border: 2px solid var(--pending);
            background: var(--bg-base);
            color: var(--text-muted);
            transition: all 0.3s ease;
        }

        .step.submitted .step-icon {
            border-color: var(--working);
            background: rgba(245, 158, 11, 0.1);
            color: var(--working);
        }
        .step.working .step-icon {
            border-color: var(--working);
            background: rgba(245, 158, 11, 0.15);
            color: var(--working);
            animation: pulse 1.5s infinite;
        }
        .step.completed .step-icon {
            border-color: var(--success);
            background: rgba(16, 185, 129, 0.1);
            color: var(--success);
        }
        .step.failed .step-icon {
            border-color: var(--failed);
            background: rgba(239, 68, 68, 0.1);
            color: var(--failed);
        }

        .step-content {
            flex-grow: 1;
        }

        .step-title {
            font-size: 0.95rem;
            font-weight: 700;
        }

        .step-desc {
            font-size: 0.8rem;
            color: var(--text-muted);
            margin-top: 0.15rem;
        }

        .step-status {
            font-size: 0.8rem;
            font-weight: 700;
            padding: 0.25rem 0.6rem;
            border-radius: 4px;
            text-transform: uppercase;
        }
        .step.pending .step-status { color: var(--pending); }
        .step.submitted .step-status { color: var(--working); }
        .step.working .step-status { color: var(--working); }
        .step.completed .step-status { color: var(--success); }
        .step.failed .step-status { color: var(--failed); }

        /* Tables & Lists */
        .table-container {
            overflow-x: auto;
            border: 1px solid var(--border-color);
            border-radius: 0.75rem;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
            text-align: left;
        }

        th {
            background: rgba(31, 41, 55, 0.7);
            color: var(--text-muted);
            font-weight: 700;
            padding: 0.85rem 1.25rem;
            border-bottom: 1px solid var(--border-color);
        }

        td {
            padding: 1rem 1.25rem;
            border-bottom: 1px solid var(--border-color);
            white-space: nowrap;
        }

        tr:last-child td {
            border-bottom: none;
        }

        tr:hover td {
            background: rgba(255, 255, 255, 0.02);
        }

        .badge-status {
            font-size: 0.75rem;
            font-weight: 700;
            padding: 0.15rem 0.5rem;
            border-radius: 99px;
            display: inline-block;
            text-transform: uppercase;
        }
        .badge-status.completed { background: rgba(16, 185, 129, 0.12); color: var(--success); }
        .badge-status.working { background: rgba(245, 158, 11, 0.12); color: var(--working); }
        .badge-status.submitted { background: rgba(245, 158, 11, 0.12); color: var(--working); }
        .badge-status.failed { background: rgba(239, 68, 68, 0.12); color: var(--failed); }

        .service-status-list {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 1rem;
        }

        .service-pill {
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 0.75rem;
            padding: 0.85rem 1rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .service-name {
            font-size: 0.85rem;
            font-weight: 700;
        }

        .status-dot {
            width: 0.5rem;
            height: 0.5rem;
            border-radius: 50%;
        }
        .status-dot.online { background-color: var(--success); box-shadow: 0 0 8px var(--success); }
        .status-dot.offline { background-color: var(--failed); box-shadow: 0 0 8px var(--failed); }

        /* Modal Details */
        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.8);
            backdrop-filter: blur(8px);
            z-index: 100;
            align-items: center;
            justify-content: center;
            padding: 1.5rem;
        }

        .modal-content {
            background: #0f172a;
            border: 1px solid var(--border-color);
            border-radius: 1.25rem;
            width: 100%;
            max-width: 700px;
            max-height: 80vh;
            display: flex;
            flex-direction: column;
            box-shadow: 0 20px 50px rgba(0,0,0,0.5);
            overflow: hidden;
        }

        .modal-header {
            padding: 1.25rem 1.5rem;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .modal-body {
            padding: 1.5rem;
            overflow-y: auto;
            font-size: 0.9rem;
        }

        pre {
            background: #020617;
            padding: 1rem;
            border-radius: 0.5rem;
            border: 1px solid var(--border-color);
            overflow-x: auto;
            color: #C7D2FE;
            font-family: monospace;
            white-space: pre-wrap;
        }

        .close-btn {
            background: none;
            border: none;
            color: var(--text-muted);
            font-size: 1.5rem;
            cursor: pointer;
            box-shadow: none;
            padding: 0;
        }
        .close-btn:hover {
            color: var(--text-main);
            transform: none;
        }

        @keyframes pulse {
            0% { opacity: 0.6; }
            50% { opacity: 1; }
            100% { opacity: 0.6; }
        }

        .empty-state {
            color: var(--text-muted);
            text-align: center;
            padding: 2rem;
            font-style: italic;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>🤖 IntelMesh Portal</h1>
                <p style="color: var(--text-muted); font-size: 0.9rem; margin-top: 0.25rem;">Enterprise Competitive Intelligence Agent Mesh</p>
            </div>
            <span class="badge">A2A Mesh Running</span>
        </header>

        <!-- Service health indicator list -->
        <div class="card" style="padding: 1.25rem 2rem;">
            <h3 style="font-size: 0.95rem; font-weight: 700; margin-bottom: 0.75rem; color: var(--text-muted);">SERVICE HEALTH STATUS</h3>
            <div class="service-status-list" id="servicesList">
                <div class="service-pill">
                    <span class="service-name">IngestionAgent</span>
                    <div class="status-dot offline"></div>
                </div>
                <div class="service-pill">
                    <span class="service-name">AnalysisAgent</span>
                    <div class="status-dot offline"></div>
                </div>
                <div class="service-pill">
                    <span class="service-name">ReportingAgent</span>
                    <div class="status-dot offline"></div>
                </div>
                <div class="service-pill">
                    <span class="service-name">AlertAgent</span>
                    <div class="status-dot offline"></div>
                </div>
                <div class="service-pill">
                    <span class="service-name">RedisBroker</span>
                    <div class="status-dot offline"></div>
                </div>
            </div>
        </div>

        <div class="grid-main">
            <!-- Left Panel: Trigger Form and Stepper -->
            <div class="card">
                <h2>🚀 Trigger Pipeline</h2>
                <form id="pipelineForm" onsubmit="submitPipeline(event)">
                    <div style="display: flex; flex-direction: column; gap: 1.25rem;">
                        <label>
                            COMPETITOR SOURCES (URLs)
                            <textarea id="sources" rows="3" required placeholder="Enter competitor urls separated by commas..."></textarea>
                        </label>
                        <label>
                            GROQ API KEY (Optional if set in system env)
                            <input type="password" id="apiKey" placeholder="gsk_...">
                        </label>
                        <button type="submit" id="submitBtn">
                            Launch Agentic Pipeline
                        </button>
                    </div>
                </form>

                <hr style="border: 0; border-top: 1px solid var(--border-color); margin: 0.5rem 0;">

                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <h2>🔄 Active Pipeline Flow</h2>
                    <span id="activeTaskId" style="font-family: monospace; font-size: 0.8rem; color: var(--accent-primary); font-weight: bold;"></span>
                </div>
                <div class="stepper" id="stepper">
                    <div class="step pending" id="step-ingestion">
                        <div class="step-icon">1</div>
                        <div class="step-content">
                            <div class="step-title">Ingestion Agent</div>
                            <div class="step-desc">FastAPI + LangGraph: Crawling & scraping text content.</div>
                        </div>
                        <span class="step-status">pending</span>
                    </div>
                    <div class="step pending" id="step-analysis">
                        <div class="step-icon">2</div>
                        <div class="step-content">
                            <div class="step-title">Analysis Agent</div>
                            <div class="step-desc">CrewAI: Role-playing research for competitor positioning.</div>
                        </div>
                        <span class="step-status">pending</span>
                    </div>
                    <div class="step pending" id="step-reporting">
                        <div class="step-icon">3</div>
                        <div class="step-content">
                            <div class="step-title">Reporting Agent</div>
                            <div class="step-desc">FastAPI + LangGraph: Compiles brief markdown report.</div>
                        </div>
                        <span class="step-status">pending</span>
                    </div>
                    <div class="step pending" id="step-alert">
                        <div class="step-icon">4</div>
                        <div class="step-content">
                            <div class="step-title">Alert Agent</div>
                            <div class="step-desc">Broker Dispatcher: Sending details to Slack webhooks.</div>
                        </div>
                        <span class="step-status">pending</span>
                    </div>
                </div>
            </div>

            <!-- Right Panel: Run Logs & DLQ -->
            <div class="card">
                <h2>📋 Unified Logs & Tasks History</h2>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Task ID</th>
                                <th>Skill ID</th>
                                <th>Status</th>
                                <th>Timestamp</th>
                                <th>Action</th>
                            </tr>
                        </thead>
                        <tbody id="tasksTableBody">
                            <tr>
                                <td colspan="5" class="empty-state">No task executions found in database.</td>
                            </tr>
                        </tbody>
                    </table>
                </div>

                <h2>⚠️ Dead Letter Queue (DLQ)</h2>
                <div class="table-container">
                    <table>
                        <thead>
                            <tr>
                                <th>Task ID</th>
                                <th>Source</th>
                                <th>Target</th>
                                <th>Error details</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody id="dlqTableBody">
                            <tr>
                                <td colspan="5" class="empty-state">DLQ is clean. No task failures recorded.</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <!-- Modal for task outputs -->
    <div id="detailsModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h3 id="modalTitle" style="font-family: Outfit; font-size: 1.25rem;">Task Data Details</h3>
                <button class="close-btn" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body">
                <h4 style="margin-bottom: 0.5rem; color: var(--accent-primary);">Inputs</h4>
                <pre id="modalInputs"></pre>
                <h4 style="margin: 1.25rem 0 0.5rem 0; color: var(--accent-secondary);">Outputs / Error</h4>
                <pre id="modalOutputs"></pre>
            </div>
        </div>
    </div>

    <script>
        // Set default textarea value
        document.getElementById('sources').value = "https://news.ycombinator.com";

        let currentActiveTaskId = null;
        let pollInterval = null;

        // Fetch updates immediately and set loop
        updateDashboard();
        setInterval(updateDashboard, 4000);

        async function updateDashboard() {
            // Check health
            try {
                const healthResp = await fetch('/api/services');
                if (healthResp.ok) {
                    const health = await healthResp.json();
                    let html = '';
                    for (const [service, data] of Object.entries(health)) {
                        const statusClass = data.status === 'ONLINE' ? 'online' : 'offline';
                        html += `
                            <div class="service-pill" title="${JSON.stringify(data.details).replace(/"/g, '&quot;')}">
                                <span class="service-name">${service}</span>
                                <div class="status-dot ${statusClass}"></div>
                            </div>
                        `;
                    }
                    document.getElementById('servicesList').innerHTML = html;
                }
            } catch (err) {
                console.error("Failed to query service health status:", err);
            }

            // Fetch tasks list
            try {
                const logsResp = await fetch('/api/tasks');
                if (logsResp.ok) {
                    const data = await logsResp.json();
                    
                    // Render Tasks Table
                    const tasksBody = document.getElementById('tasksTableBody');
                    if (data.tasks.length === 0) {
                        tasksBody.innerHTML = '<tr><td colspan="5" class="empty-state">No task executions found in database.</td></tr>';
                    } else {
                        tasksBody.innerHTML = data.tasks.map(t => `
                            <tr>
                                <td style="font-family: monospace; font-weight: 700; color: var(--accent-primary);">${t.taskId}</td>
                                <td>${t.skillId}</td>
                                <td><span class="badge-status ${t.status}">${t.status}</span></td>
                                <td>${t.created_at}</td>
                                <td><button onclick='viewDetails(${JSON.stringify(t)})' style="padding: 0.35rem 0.75rem; font-size: 0.75rem; font-weight: 500; box-shadow: none;">Inspect</button></td>
                            </tr>
                        `).join('');
                    }

                    // Render DLQ Table
                    const dlqBody = document.getElementById('dlqTableBody');
                    if (data.dlq.length === 0) {
                        dlqBody.innerHTML = '<tr><td colspan="5" class="empty-state">DLQ is clean. No task failures recorded.</td></tr>';
                    } else {
                        dlqBody.innerHTML = data.dlq.map(d => `
                            <tr>
                                <td style="font-family: monospace; font-weight: 700; color: var(--failed);">${d.taskId}</td>
                                <td style="font-size:0.8rem;">${d.source}</td>
                                <td style="font-size:0.8rem;">${d.target}</td>
                                <td style="max-width: 250px; overflow: hidden; text-overflow: ellipsis; font-size: 0.8rem;" title="${d.error_message}">${d.error_message}</td>
                                <td><span class="badge-status failed">${d.status}</span></td>
                            </tr>
                        `).join('');
                    }
                }
            } catch (err) {
                console.error("Failed to query tasks list:", err);
            }
        }

        async function submitPipeline(e) {
            e.preventDefault();
            const sourcesVal = document.getElementById('sources').value.split(',').map(s => s.trim()).filter(s => s.length > 0);
            const apiKey = document.getElementById('apiKey').value;

            const btn = document.getElementById('submitBtn');
            btn.disabled = true;
            btn.textContent = "Launching...";

            try {
                const resp = await fetch('/api/run', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sources: sourcesVal, groq_api_key: apiKey || null })
                });

                if (!resp.ok) {
                    const errData = await resp.json();
                    alert("Error: " + (errData.detail || "Failed to trigger pipeline"));
                    btn.disabled = false;
                    btn.textContent = "Launch Agentic Pipeline";
                    return;
                }

                const result = await resp.json();
                currentActiveTaskId = result.taskId;
                document.getElementById('activeTaskId').textContent = `TASK ID: ${currentActiveTaskId}`;
                
                // Clear any existing poll loop
                if (pollInterval) clearInterval(pollInterval);
                
                // Poll active status
                pollActivePipeline();
                pollInterval = setInterval(pollActivePipeline, 2000);
            } catch (err) {
                alert("Network error starting pipeline: " + err.message);
            } finally {
                btn.disabled = false;
                btn.textContent = "Launch Agentic Pipeline";
            }
        }

        async function pollActivePipeline() {
            if (!currentActiveTaskId) return;

            try {
                const resp = await fetch(`/api/status/${currentActiveTaskId}`);
                if (resp.ok) {
                    const flow = await resp.json();
                    
                    let allFinished = true;
                    
                    for (const [stepKey, data] of Object.entries(flow)) {
                        const stepEl = document.getElementById(`step-${stepKey}`);
                        if (!stepEl) continue;

                        // Clear classes
                        stepEl.className = `step ${data.status}`;
                        stepEl.querySelector('.step-status').textContent = data.status;

                        if (data.status === 'pending' || data.status === 'working' || data.status === 'submitted') {
                            allFinished = false;
                        }
                    }

                    if (allFinished) {
                        clearInterval(pollInterval);
                        pollInterval = null;
                        updateDashboard();
                    }
                }
            } catch (err) {
                console.error("Error polling active pipeline:", err);
            }
        }

        function viewDetails(task) {
            document.getElementById('modalTitle').textContent = `Task ${task.taskId} JSON Inspection`;
            document.getElementById('modalInputs').textContent = JSON.stringify(task.inputs, null, 2);
            document.getElementById('modalOutputs').textContent = task.outputs ? JSON.stringify(task.outputs, null, 2) : (task.error_message || "No outputs compiled yet.");
            document.getElementById('detailsModal').style.display = 'flex';
        }

        function closeModal() {
            document.getElementById('detailsModal').style.display = 'none';
        }

        window.onclick = function(event) {
            const modal = document.getElementById('detailsModal');
            if (event.target === modal) {
                modal.style.display = "none";
            }
        }
    </script>
</body>
</html>
"""
    return html_content
