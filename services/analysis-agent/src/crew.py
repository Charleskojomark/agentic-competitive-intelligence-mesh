import os
import logging
from typing import List, Dict, Any
from crewai import Agent, Task, Crew, Process

logger = logging.getLogger(__name__)

def run_competitor_analysis_crew(raw_contents: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Sets up and runs a CrewAI execution to extract competitive insights from competitor text.
    If GROQ_API_KEY is not available, falls back to a local rule-based extractor.
    """
    # 1. Fallback Mode (Offline/No API Key)
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.warning("No GROQ_API_KEY found. Running Analysis Agent in fallback parsing mode.")
        insights = []
        for doc in raw_contents:
            text = doc.get("text", "")
            source = doc.get("source", "")
            # Simple rule-based extraction for testing
            if "pricing" in text.lower() or "price" in text.lower() or "$" in text:
                insights.append({
                    "category": "pricing",
                    "summary": f"Detected pricing tier modification from {source}",
                    "impact": "high"
                })
            else:
                insights.append({
                    "category": "feature",
                    "summary": f"Detected product capabilities update from {source}",
                    "impact": "medium"
                })
        
        return {
            "insights": insights if insights else [{"category": "general", "summary": "Generic market update", "impact": "low"}],
            "sentiment": "neutral"
        }

    # 2. CrewAI Production Mode
    try:
        from langchain_groq import ChatGroq
        
        # Configure LLM
        llm = ChatGroq(
            temperature=0.2,
            model_name=os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"),
            groq_api_key=api_key
        )
        
        # Define Agents
        researcher = Agent(
            role="Competitor Intelligence Researcher",
            goal="Identify and extract new pricing models, product feature announcements, and market actions from raw text.",
            backstory="You are an expert analyst trained to extract facts and exclude noise from competitor press releases and landing pages.",
            llm=llm,
            verbose=True
        )

        analyst = Agent(
            role="Strategic Analyst",
            goal="Synthesize raw intelligence facts into structured strategic impacts and determine threat levels.",
            backstory="You are a seasoned product strategist who evaluates competitive moves to understand their threat level (low, medium, high).",
            llm=llm,
            verbose=True
        )

        # Format input text context
        context_str = "\n---\n".join([f"Source: {doc.get('source')}\nContent: {doc.get('text')}" for doc in raw_contents])

        # Define Tasks
        research_task = Task(
            description=f"Analyze the following competitor data and list all core updates (e.g. pricing changes, new feature additions):\n\n{context_str}",
            expected_output="A list of bullet points containing raw competitor announcements and updates.",
            agent=researcher
        )

        analysis_task = Task(
            description="Process the researcher's output. Group updates into category (pricing, feature, marketing, other), summarize them, and grade their strategic impact (low, medium, high). Decide overall sentiment (bullish, bearish, neutral).",
            expected_output="A structured JSON response with keys 'insights' (list of objects with 'category', 'summary', 'impact') and 'sentiment' (string).",
            agent=analyst
        )

        # Instantiate and run Crew
        crew = Crew(
            agents=[researcher, analyst],
            tasks=[research_task, analysis_task],
            process=Process.sequential,
            verbose=2
        )

        logger.info("Executing CrewAI competitor analysis crew...")
        result = crew.kickoff()
        
        # Return structured data (in production, parse result to JSON if needed)
        # For simplicity, we parse or convert back to dict
        import json
        try:
            # Clean Markdown if wrapped in triple backticks
            cleaned = str(result).strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            return json.loads(cleaned)
        except Exception:
            # Fallback parser if LLM returns text instead of strict JSON
            return {
                "insights": [{"category": "general", "summary": str(result)[:300], "impact": "medium"}],
                "sentiment": "neutral"
            }
            
    except Exception as e:
        logger.error(f"Error executing CrewAI crew: {e}. Falling back to rule-based parser.")
        return {
            "insights": [{"category": "error", "summary": f"CrewAI failure: {str(e)}", "impact": "low"}],
            "sentiment": "neutral"
        }
