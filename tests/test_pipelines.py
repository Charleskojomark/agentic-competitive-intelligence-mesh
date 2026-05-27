import sys
import os
import importlib.util
import pytest
from unittest.mock import patch
from shared.database import init_db, get_db_session, TaskRecord, FailedTaskRecord

def import_service_main(service_name):
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "services", service_name, "src", "main.py"))
    service_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "services", service_name))
    
    # Prepend directory to guarantee resolution priority
    sys.path.insert(0, service_dir)
    
    # Clear overlapping sub-modules to force reloading from current sys.path entry
    for mod in ["src", "src.graph", "src.crew"]:
        if mod in sys.modules:
            del sys.modules[mod]
            
    spec = importlib.util.spec_from_file_location(f"{service_name.replace('-', '_')}_main", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{service_name.replace('-', '_')}_main"] = module
    spec.loader.exec_module(module)
    
    # Clean up sys.path
    if service_dir in sys.path:
        sys.path.remove(service_dir)
        
    return module

ingestion_main = import_service_main("ingestion-agent")
analysis_main = import_service_main("analysis-agent")
reporting_main = import_service_main("reporting-agent")
alert_main = import_service_main("alert-agent")

run_ingestion_task = ingestion_main.run_ingestion_task
run_analysis_task = analysis_main.run_analysis_task
run_reporting_task = reporting_main.run_reporting_task
run_alert_task = alert_main.run_alert_task

@pytest.fixture(autouse=True)
def setup_test_db():
    init_db()
    with get_db_session() as db:
        db.query(TaskRecord).delete()
        db.query(FailedTaskRecord).delete()
    yield

def test_four_agent_pipeline_integration(mocker):
    """
    Orchestrates a mock horizontal pipeline run.
    Intercepts A2A client network requests and redirects them to local agent runners.
    """
    
    # Track which services get called
    invoked_agents = []

    def mock_a2a_invoke(self, method, params, request_id=1):
        nonlocal invoked_agents
        invoked_agents.append(self.service_name)
        
        # Intercept and run locally
        task_id = params.get("taskId")
        inputs = params.get("inputs", {})
        
        # Insert task into DB as 'submitted' to simulate FastAPI endpoint reception
        with get_db_session() as db:
            task = TaskRecord(
                task_id=task_id,
                skill_id=params.get("skillId"),
                status="submitted",
                inputs=inputs
            )
            db.merge(task)
            
        if self.service_name == "AnalysisAgent":
            run_analysis_task(task_id, inputs)
        elif self.service_name == "ReportingAgent":
            run_reporting_task(task_id, inputs)
        elif self.service_name == "AlertAgent":
            run_alert_task(task_id, inputs)
            
        return {"taskId": task_id, "status": "submitted"}

    # Patch A2aClient invoke method to run synchronously
    mocker.patch("shared.a2a_client.A2aClient.invoke", mock_a2a_invoke)

    # 1. Initialize Ingestion task record
    root_task_id = "test-pipeline-run-123"
    with get_db_session() as db:
        db.add(TaskRecord(
            task_id=root_task_id,
            skill_id="monitor-and-scrape",
            status="submitted",
            inputs={"sources": ["https://dummy-competitor.com/pricing"]}
        ))

    # 2. Trigger the ingestion pipeline (will trigger analysis -> reporting -> alerting)
    run_ingestion_task(root_task_id, {"sources": ["https://dummy-competitor.com/pricing"]})

    # 3. Verify downstream agent invocations
    assert "AnalysisAgent" in invoked_agents
    assert "ReportingAgent" in invoked_agents
    assert "AlertAgent" in invoked_agents

    # 4. Verify all tasks completed successfully in the database
    with get_db_session() as db:
        ingestion_task = db.query(TaskRecord).filter(TaskRecord.task_id == root_task_id).first()
        assert ingestion_task.status == "completed"
        assert "raw_contents" in ingestion_task.outputs

        analysis_task = db.query(TaskRecord).filter(TaskRecord.task_id == f"analysis-{root_task_id}").first()
        assert analysis_task.status == "completed"
        assert "insights" in analysis_task.outputs
        
        reporting_task = db.query(TaskRecord).filter(TaskRecord.task_id == f"report-analysis-{root_task_id}").first()
        assert reporting_task.status == "completed"
        assert "report_markdown" in reporting_task.outputs
        assert reporting_task.outputs["score"] >= 8.0

        alert_task = db.query(TaskRecord).filter(TaskRecord.task_id == f"alert-report-analysis-{root_task_id}").first()
        assert alert_task.status == "completed"
        assert alert_task.outputs["notified"] is True

def test_dead_letter_queue_logging():
    """
    Validates that a failing task is logged inside the failed_tasks (DLQ) table.
    """
    fail_task_id = "test-fail-task"
    
    # 1. Create a task that will fail
    with get_db_session() as db:
        db.add(TaskRecord(
            task_id=fail_task_id,
            skill_id="monitor-and-scrape",
            status="submitted",
            inputs={"sources": []}
        ))
        
    # Trigger run with invalid inputs to cause failure
    with patch.object(ingestion_main, "create_ingestion_graph", side_effect=ValueError("Graph compiler crash")):
        run_ingestion_task(fail_task_id, {"sources": []})

    # 2. Assert task state in DB is marked failed
    with get_db_session() as db:
        task = db.query(TaskRecord).filter(TaskRecord.task_id == fail_task_id).first()
        assert task.status == "failed"
        assert "Graph compiler crash" in task.error_message

        # 3. Assert task was written to dead letter queue
        dlq_entry = db.query(FailedTaskRecord).filter(FailedTaskRecord.task_id == fail_task_id).first()
        assert dlq_entry is not None
        assert dlq_entry.target_agent == "IngestionAgent"
        assert "Graph compiler crash" in dlq_entry.error_message
        assert dlq_entry.status == "PENDING_REVIEW"
