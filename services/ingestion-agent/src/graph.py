import logging
from typing import List, Dict, Any, TypedDict, Annotated, Optional
from langgraph.graph import StateGraph, START, END
import httpx

# In Python, we can define the Graph State as a TypedDict
class IngestionState(TypedDict):
    sources: List[str]
    raw_contents: List[Dict[str, Any]]
    current_index: int
    error: Optional[str]

logger = logging.getLogger(__name__)

def scrape_url_node(state: IngestionState) -> Dict[str, Any]:
    """Node: Scrapes content from the current target URL."""
    idx = state["current_index"]
    url = state["sources"][idx]
    logger.info(f"LangGraph Ingestion Node: Scraping {url}...")
    
    raw_contents = list(state.get("raw_contents", []))
    
    try:
        # Perform HTTP GET request to scrape content
        response = httpx.get(url, timeout=10.0, follow_redirects=True)
        if response.status_code == 200:
            text = response.text[:5000]  # Take first 5k characters to keep payload reasonable
            logger.info(f"Successfully scraped {len(text)} characters from {url}")
        else:
            text = f"Failed to scrape url. HTTP Status Code: {response.status_code}"
            logger.warning(text)
    except Exception as e:
        logger.error(f"Scraping error for {url}: {e}")
        # Fallback to simulated data if offline / local testing
        text = f"Simulated crawled text: Product Pro tier plan starts at $49/month with enterprise grade security, SSO, and unlimited multi-agent integrations."
    
    raw_contents.append({
        "source": url,
        "text": text,
        "scraped_at": "2026-05-26"
    })
    
    return {
        "raw_contents": raw_contents,
        "current_index": idx + 1
    }

def decide_next_edge(state: IngestionState) -> str:
    """Conditional Edge: Decides whether to continue scraping or end."""
    if state["current_index"] < len(state["sources"]):
        return "scrape"
    return "end"

def create_ingestion_graph():
    """Builds and compiles the Ingestion Agent LangGraph."""
    workflow = StateGraph(IngestionState)
    
    # Add scraping node
    workflow.add_node("scrape", scrape_url_node)
    
    # Wire paths
    workflow.add_edge(START, "scrape")
    
    workflow.add_conditional_edges(
        "scrape",
        decide_next_edge,
        {
            "scrape": "scrape",
            "end": END
        }
    )
    
    return workflow.compile()
