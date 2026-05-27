FROM python:3.11-slim

# Install system dependencies (build-essential, postgres libraries, and redis-server)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    redis-server \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user (UID 1000) for Hugging Face Space security
RUN useradd -m -u 1000 user

WORKDIR /app

# Copy dependency configs and project files needed for the build
COPY pyproject.toml README.md /app/
COPY shared/ /app/shared/
COPY services/ /app/services/

# Install the workspace package and all its dependencies
RUN pip install --no-cache-dir .

# Copy the unified dashboard orchestrator
COPY dashboard.py /app/

# Set correct permissions for Hugging Face container user
RUN chown -R user:user /app

# Switch to the non-root user
USER user

# Set PYTHONPATH environment variable to resolve workspace imports
ENV PYTHONPATH=/app
ENV DATABASE_URL=sqlite:////tmp/agent_tasks.db
ENV REDIS_URL=redis://localhost:6379/0

# Hugging Face Spaces expects port 7860 to be exposed
EXPOSE 7860

# Launch the unified dashboard which spawns Redis and the 4 agent microservices internally
CMD ["uvicorn", "dashboard:app", "--host", "0.0.0.0", "--port", "7860"]
