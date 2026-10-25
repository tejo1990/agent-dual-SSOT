"""Dual-Scope & Session-Isolated Context Compiler MCP Package."""

from dual_ssot.engine import DualSSOTEngine
from dual_ssot.mcp_server import create_mcp_server, estimate_mcp_tool_schema_tokens
from dual_ssot.metrics import (
    CostWeights,
    SimulationScenario,
    compute_min_promotion_projects,
    evaluate_conflict_detector_roi,
    evaluate_context_token_roi,
    evaluate_strategies,
)
from dual_ssot.models import (
    DEFAULT_CORE_CANONICAL_KEYS,
    HUB_FILE_DENYLIST,
    L1_MAX_LINES,
    L1_MAX_TOKENS,
    MAX_AUTO_REGISTERED_KEYS,
    ConflictDetail,
    ConflictResolutionAction,
    MetaPreferencePayload,
    PromoteSessionResult,
    ProofOfExecution,
    RejectedHypothesisInput,
    STARLPayload,
    StagedDecisionInput,
    extract_leaf_files,
)

__all__ = [
    "DualSSOTEngine",
    "create_mcp_server",
    "estimate_mcp_tool_schema_tokens",
    "CostWeights",
    "SimulationScenario",
    "compute_min_promotion_projects",
    "evaluate_conflict_detector_roi",
    "evaluate_context_token_roi",
    "evaluate_strategies",
    "DEFAULT_CORE_CANONICAL_KEYS",
    "HUB_FILE_DENYLIST",
    "L1_MAX_LINES",
    "L1_MAX_TOKENS",
    "MAX_AUTO_REGISTERED_KEYS",
    "ConflictDetail",
    "ConflictResolutionAction",
    "MetaPreferencePayload",
    "PromoteSessionResult",
    "ProofOfExecution",
    "RejectedHypothesisInput",
    "STARLPayload",
    "StagedDecisionInput",
    "extract_leaf_files",
]
