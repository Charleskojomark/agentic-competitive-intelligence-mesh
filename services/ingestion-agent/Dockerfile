FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for C-libraries (psycopg2 compilation, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy pyproject.toml to install dependencies
COPY pyproject.toml .

# Install dependencies
RUN pip install --no-cache-dir .

# Copy shared library modules
COPY shared/ /app/shared/

# Copy the ingestion-agent service code
COPY services/ingestion-agent/ /app/services/ingestion-agent/

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "services.ingestion-agent.src.main:app", "--host", "0.0.0.0", "--port", "8000"]
