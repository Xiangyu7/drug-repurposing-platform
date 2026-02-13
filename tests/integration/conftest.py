"""Integration test configuration: add both sub-projects to sys.path."""
import sys
from pathlib import Path

# Add both sub-projects to import path
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "kg_explain" / "src"))
sys.path.insert(0, str(ROOT / "LLM+RAG证据工程"))
sys.path.insert(0, str(ROOT / "LLM+RAG证据工程" / "src"))
