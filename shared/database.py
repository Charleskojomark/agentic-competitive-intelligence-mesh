import os
from datetime import datetime
import uuid
from typing import Generator
from sqlalchemy import create_engine, Column, String, Integer, Text, DateTime, JSON, Enum
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from contextlib import contextmanager

# Read database URL, defaulting to local sqlite for test/development speed
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./agent_tasks.db")

# Setup database engine. SQLite requires special parameters for multi-thread support.
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- Database Models ---

class TaskRecord(Base):
    __tablename__ = "tasks"

    task_id = Column(String(255), primary_key=True, index=True)
    skill_id = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False, default="submitted")  # submitted, working, completed, failed
    inputs = Column(JSON, nullable=False)
    outputs = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FailedTaskRecord(Base):
    __tablename__ = "failed_tasks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(255), nullable=False, unique=True, index=True)
    source_agent = Column(String(100), nullable=False)
    target_agent = Column(String(100), nullable=False)
    skill_id = Column(String(100), nullable=False)
    payload = Column(JSON, nullable=False)
    error_message = Column(Text, nullable=False)
    stack_trace = Column(Text, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=3)
    status = Column(String(50), nullable=False, default="PENDING_REVIEW")  # PENDING_REVIEW, REPROCESSED, IGNORED
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# --- Helper methods ---

def init_db():
    """Create all tables in the database."""
    Base.metadata.create_all(bind=engine)

@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Provide a transactional scope around a series of operations."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

def get_db() -> Generator[Session, None, None]:
    """Dependency injection helper for FastAPI endpoints."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def log_to_dlq(
    task_id: str,
    source_agent: str,
    target_agent: str,
    skill_id: str,
    payload: dict,
    error_message: str,
    stack_trace: str = None
):
    """Logs a failed task execution to the dead letter queue (failed_tasks) table."""
    import traceback
    if not stack_trace:
        stack_trace = traceback.format_exc()

    with get_db_session() as session:
        existing = session.query(FailedTaskRecord).filter(FailedTaskRecord.task_id == task_id).first()
        if existing:
            existing.error_message = error_message
            existing.stack_trace = stack_trace
            existing.attempt_count += 1
            existing.status = "PENDING_REVIEW"
        else:
            dlq_record = FailedTaskRecord(
                task_id=task_id,
                source_agent=source_agent,
                target_agent=target_agent,
                skill_id=skill_id,
                payload=payload,
                error_message=error_message,
                stack_trace=stack_trace,
                attempt_count=3,
                status="PENDING_REVIEW"
            )
            session.add(dlq_record)

