# 🤖 Agentic Competitive Intelligence Mesh

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2.0-orange.svg)](https://github.com/langchain-ai/langgraph)
[![CrewAI](https://img.shields.io/badge/CrewAI-0.28.0-red.svg)](https://github.com/crewAIInc/crewAI)
[![Docker Compose](https://img.shields.io/badge/Docker%20Compose-Ready-blue.svg)](#running-with-docker-compose)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An enterprise-grade, stateful, and resilient **Production Multi-Agent Competitive Intelligence System**. This platform automates target crawling, analysis of competitor strategic shifts, synthesis of business intelligence, and real-time slack notifications using a decoupled microservices mesh.

The services communicate asynchronously via Google's open-standard **Agent-to-Agent (A2A) protocol** over JSON-RPC 2.0 and incorporate advanced resilience patterns like distributed circuit breaking, centralized observability, and structured database durability.

---

## 🏗️ System Architecture & Data Flow

The system is decoupled into isolated, single-responsibility microservices. They run on a virtual network mesh, using Redis as a distributed circuit breaker and coordinator, PostgreSQL as the source-of-truth datastore, and Langfuse for end-to-end tracing.

```mermaid
flowchart TD
    subgraph Services Mesh (A2A Protocol)
        Ingestion[Ingestion Agent <br/> FastAPI + LangGraph] -->|JSON-RPC: tasks/send| Analysis[Analysis Agent <br/> FastAPI + CrewAI]
        Analysis -->|JSON-RPC: tasks/send| Reporting[Reporting Agent <br/> FastAPI + LangGraph]
        Reporting -->|JSON-RPC: tasks/send| Alert[Alert Agent <br/> Notification Broker]
    end
    
    subgraph Core Infrastructure & Observability
        Analysis & Ingestion & Reporting & Alert -->|OpenTelemetry Traces & Cost| Langfuse[Langfuse Observability]
        Analysis & Ingestion & Reporting & Alert -->|State, Logs & Retry DLQ| Postgres[(PostgreSQL Database)]
        Analysis & Ingestion & Reporting & Alert -->|Circuit Breakers & Locks| Redis[(Redis Broker)]
    end
    
    style Ingestion fill:#2a9d8f,stroke:#fff,stroke-width:2px,color:#fff
    style Analysis fill:#e76f51,stroke:#fff,stroke-width:2px,color:#fff
    style Reporting fill:#f4a261,stroke:#fff,stroke-width:2px,color:#fff
    style Alert fill:#e9c46a,stroke:#fff,stroke-width:2px,color:#fff
    style Redis fill:#588157,stroke:#fff,stroke-width:1px,color:#fff
    style Postgres fill:#3d5a80,stroke:#fff,stroke-width:1px,color:#fff
    style Langfuse fill:#7209b7,stroke:#fff,stroke-width:1px,color:#fff
```

---

## 🚀 Key Highlights & Architectural Decisions

*   **Decoupled Agent-to-Agent (A2A) Protocol**: Implements JSON-RPC 2.0 validation payloads using Pydantic, allowing different agent frameworks (LangGraph, CrewAI) to collaborate seamlessly.
*   **Distributed Circuit Breaker Pattern**: Integrated with Redis to monitor error rates for downstream agent calls. If a service experiences downtime or LLM rate limits (429s), the breaker trips, saving credits and preventing cascading failures.
*   **Production-Grade Observability**: Integrates Langfuse out-of-the-box, giving developers full visibility into LLM token counts, latency, prompt templates, and nested agent execution traces.
*   **Multi-Framework Integration**:
    *   **LangGraph** is used for structured, cyclic processes (Crawling & Report Synthesis) where strict control flow and state management are required.
    *   **CrewAI** is used for creative, role-playing, and research-intensive workflows (Competitor Strategy Analysis) where agents need to collaborate dynamically.
*   **Robust Database Strategy**: All state modifications, execution runs, and raw crawling data are durably logged to PostgreSQL with schema migrations.

---

## 📂 Microservice Directory Structure

```bash
├── .github/workflows/      # CI/CD deployment configuration
├── services/
│   ├── ingestion-agent/    # FastAPI + LangGraph crawling competitor websites & press releases
│   ├── analysis-agent/     # FastAPI + CrewAI researching competitor data for strategic shifts
│   ├── reporting-agent/    # FastAPI + LangGraph compiling synthesized executive briefs
│   └── alert-agent/        # Notification dispatcher dispatching Slack/webhook updates
├── shared/
│   ├── a2a_client.py       # Client implementation for A2A communication
│   ├── circuit_breaker.py  # Redis-backed distributed circuit breaker
│   ├── database.py         # SQLAlchemy connection & database models
│   ├── observability.py    # Langfuse tracing wrapper & instrumentations
│   └── protocol_a2a.py     # JSON-RPC 2.0 Pydantic models for A2A schemas
├── docker-compose.yml      # Orchestrates all microservices, DBs, and caching nodes
├── pyproject.toml          # Workspace dependencies metadata
└── uv.lock                 # Fast python lockfile dependency tracking
```

---

## ⚡ Quick Start

### Prerequisites
*   Python 3.11+
*   Docker & Docker Compose
*   [Groq API Key](https://console.groq.com/) (or another supported LLM provider)

### Local Development Setup

1. **Clone & Setup Environment**
   ```bash
   git clone https://github.com/Charleskojomark/agentic-competitive-intelligence-mesh.git
   cd agentic-competitive-intelligence-mesh
   ```

2. **Configure Environment Variables**
   ```bash
   cp .env.example .env
   # Edit .env and enter your credentials (e.g. GROQ_API_KEY)
   ```

3. **Install Dependencies**
   It's recommended to use `uv` for ultra-fast dependency installation, but standard pip works:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e .
   ```

### Run with Docker Compose
To spin up the entire system including databases, Redis, and Langfuse tracing:
```bash
docker compose up --build -d
```

#### Service Ports Map:
*   **Ingestion Agent Service:** `http://localhost:8001`
*   **Analysis Agent Service:** `http://localhost:8002`
*   **Reporting Agent Service:** `http://localhost:8003`
*   **Alert Agent Service:** `http://localhost:8004`
*   **Langfuse Dashboard:** `http://localhost:3000`

---

## 🔍 Agent Discovery (A2A Protocol)

Every microservice implements a Discovery endpoint at `GET /.well-known/agent.json` which describes its routing capabilities, metadata, and skills:

```bash
curl http://localhost:8001/.well-known/agent.json
```

**Example JSON Response:**
```json
{
  "name": "ingestion-agent",
  "version": "1.0.0",
  "endpoint": "http://ingestion-agent:8000/api/v1/agent",
  "skills": ["crawl_competitor_pages", "parse_press_releases"]
}
```

---

## 🧪 Running Tests

Ensure all pipeline components are working correctly using pytest:
```bash
pytest
```

---

## 💬 Interview talking points (For Hiring Managers)
If you are reviewing this project for a backend or AI Engineering role, here are the architectural choices built to reflect production-grade expertise:
*   **How do you handle API failures/Rate limits (429s) with LLMs?**  
    We implement a Redis-backed `CircuitBreaker`. When failures cross the threshold, downstream calls are short-circuited instantly to prevent API billing waste and system choke.
*   **Why separate the agents into microservices instead of a single monoprocess?**  
    Separation allows us to scale services independently (e.g., scaling the ingestion crawling instances without scaling high-memory LLM analysis agents). It also prevents runtime locks when mixing synchronous web-scraping with asynchronous LLM calls.
*   **How do you trace state across asynchronous agent nodes?**  
    Each run propagates a trace context ID in the JSON-RPC payload. We hook into Langfuse with matching parent-child span IDs to track execution flows, token usage, and costs from the moment a URL is ingested to when the Slack notification goes out.
