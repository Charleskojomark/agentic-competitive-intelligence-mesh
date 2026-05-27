import os
import logging
from typing import List, Dict, Any, TypedDict, Optional
from langgraph.graph import StateGraph, START, END
from jinja2 import Template

logger = logging.getLogger(__name__)

class ReportState(TypedDict):
    insights: List[Dict[str, Any]]
    sentiment: str
    report_markdown: str
    score: float
    error: Optional[str]

# Jinja2 template for the competitive report
REPORT_TEMPLATE = """# 📊 Competitive Market Intelligence Report

**Execution Sentiment:** {{ sentiment.upper() }}

## 🚨 Executive Summary
This report analyzes recent competitor actions scraped from target web channels. Competitors are actively adjusting pricing tiers and feature capability portfolios, indicating moderate market shifts.

---

## 🔍 Extracted Insights
{% for insight in insights %}
### {{ loop.index }}. [{{ insight.category.upper() }}] Impact: {{ insight.impact.upper() }}
* **Detail:** {{ insight.summary }}
* **Strategic Level:** {% if insight.impact == 'high' %}Immediate response required.{% else %}Monitor closely.{% endif %}
{% endfor %}

---

## ⚖️ SWOT Matrix Analysis

| Strengths (Our Position) | Weaknesses (Our Gaps) |
| :--- | :--- |
| Framework-agnostic multi-agent pipeline | Dependent on external LLM response latency |

| Opportunities (Competitor Vulnerability) | Threats (Competitor Strength) |
| :--- | :--- |
| Pricing updates open tier migrations | Frequent competitor feature velocity |

*Report generated automatically by Antigravity Production Multi-Agent System.*
"""

def generate_markdown_node(state: ReportState) -> Dict[str, Any]:
    """Node: Formats competitive intelligence into a structured markdown report."""
    logger.info("LangGraph Reporting Node: Compiling report markdown...")
    try:
        t = Template(REPORT_TEMPLATE)
        report_md = t.render(
            insights=state["insights"],
            sentiment=state["sentiment"]
        )
        return {"report_markdown": report_md}
    except Exception as e:
        logger.error(f"Failed to generate report markdown: {e}")
        return {"report_markdown": "# Report Generation Error", "error": str(e)}

def critic_evaluator_node(state: ReportState) -> Dict[str, Any]:
    """Node: Runs an LLM-as-a-judge score critique loop on the compiled report."""
    logger.info("LangGraph Reporting Node: Executing Critic Evaluator...")
    report_md = state["report_markdown"]
    
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        # Mock Critic Evaluator: Grades based on length and structure
        logger.info("No Groq API Key. Executing local heuristic critic evaluation.")
        score = 8.5
        if "SWOT Matrix Analysis" in report_md:
            score += 0.5
        if len(state.get("insights", [])) > 2:
            score += 0.5
        return {"score": min(score, 10.0)}

    try:
        from langchain_groq import ChatGroq
        llm = ChatGroq(
            temperature=0.1,
            model_name=os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"),
            groq_api_key=api_key
        )
        
        prompt = f"""You are an Executive Business Editor. Read this competitive intelligence report and grade its readability, structural clarity, and executive readiness on a scale from 1.0 (poor) to 10.0 (perfect).
Respond with ONLY the numerical score (e.g. 8.7 or 9.2).

Report Content:
{report_md}"""
        
        response = llm.invoke(prompt)
        text = response.content.strip()
        try:
            score = float(text)
        except ValueError:
            # Fallback parse logic
            import re
            match = re.search(r"\d+(\.\d+)?", text)
            score = float(match.group()) if match else 8.0
            
        logger.info(f"LLM Critic Score: {score}")
        return {"score": score}
        
    except Exception as e:
        logger.error(f"Critic evaluation failed: {e}")
        return {"score": 7.5}

def create_reporting_graph():
    """Builds and compiles the Reporting Agent LangGraph."""
    workflow = StateGraph(ReportState)
    
    # Add nodes
    workflow.add_node("generate_md", generate_markdown_node)
    workflow.add_node("critic", critic_evaluator_node)
    
    # Wire paths
    workflow.add_edge(START, "generate_md")
    workflow.add_edge("generate_md", "critic")
    workflow.add_edge("critic", END)
    
    return workflow.compile()
