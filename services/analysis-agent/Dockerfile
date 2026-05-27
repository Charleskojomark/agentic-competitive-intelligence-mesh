FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .

RUN pip install --no-cache-dir .

COPY shared/ /app/shared/
COPY services/analysis-agent/ /app/services/analysis-agent/

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "services.analysis-agent.src.main:app", "--host", "0.0.0.0", "--port", "8000"]
