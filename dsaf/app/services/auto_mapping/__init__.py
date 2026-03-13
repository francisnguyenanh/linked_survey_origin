"""
Auto-mapping engine package.

Components:
  SurveyGraph        — NetworkX directed graph model
  SafetyGuard        — Terminal page detection + final-submit interception
  TriggerAnalyzer    — Identifies questions that cause branching
  RateLimitManager   — Inter-branch delays and proxy rotation
  DFSExplorer        — Recursive DFS traversal of survey tree
  PatternExtractor   — Generates patterns from all explored paths
  AutoMappingEngine  — Orchestrator with SocketIO progress events
  HybridMapper       — Real-UID mapper combining Back-button + restart strategies
  ShadowObserver     — Passive observation of user-driven survey navigation
  ShadowMappingSession — Opens visible browser, attaches ShadowObserver
  AssistantOverlay   — Injects hint overlay into the survey page
"""

from .survey_graph import SurveyGraph
from .safety_guard import SafetyGuard
from .trigger_analyzer import TriggerAnalyzer
from .rate_limit_manager import RateLimitManager
from .dfs_explorer import DFSExplorer
from .pattern_extractor import PatternExtractor
from .auto_mapping_engine import AutoMappingEngine
from .hybrid_mapper import HybridMapper
from .shadow_observer import ShadowObserver, ShadowMappingSession, AssistantOverlay

__all__ = [
    "SurveyGraph",
    "SafetyGuard",
    "TriggerAnalyzer",
    "RateLimitManager",
    "DFSExplorer",
    "PatternExtractor",
    "AutoMappingEngine",
    "HybridMapper",
    "ShadowObserver",
    "ShadowMappingSession",
    "AssistantOverlay",
]
