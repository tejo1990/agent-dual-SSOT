"""Token-lean MCP Server (<500 schema tokens) exposing Dual-Scope & MVCC Session-Isolated SSOT tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

try:
    from mcp.server.mcpserver import MCPServer as _MCPServerClass
except ImportError:  # pragma: no cover - fallback for mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServerClass  # type: ignore[no-redef]

from dual_ssot.engine import DualSSOTEngine
from dual_ssot.models import estimate_token_count


def create_mcp_server(
    workspace_path: Optional[str | Path] = None,
    personal_dir: Optional[str | Path] = None,
    project_id: Optional[str] = None,
) -> tuple[_MCPServerClass, DualSSOTEngine]:
    """
    Creates the MCP Server instance bound to a `DualSSOTEngine`.
    Keeps tool descriptions ultra-compact (<500 tokens total across all tools) to preserve Net Token ROI.
    """
    ws = Path(workspace_path or os.environ.get("DUAL_SSOT_WORKSPACE", os.getcwd())).resolve()
    engine = DualSSOTEngine(
        workspace_path=ws,
        personal_dir=personal_dir,
        project_id=project_id,
    )
    mcp = _MCPServerClass("dual-ssot")

    @mcp.tool()
    def init_session(session_id: str) -> Dict[str, Any]:
        """Init MVCC read-only snapshot & get active canonical keys + L1 context."""
        return engine.init_session(session_id=session_id)

    @mcp.tool()
    def stage_decision(
        session_id: str,
        topic_key: str,
        decision_value: str,
        rationale: str,
        evidence_ids: List[str],
        file_scope: Optional[List[str]] = None,
        pinned: bool = False,
        scope_target: Literal["domain", "personal_starl", "personal_habit", "dual"] = "domain",
        proof: Optional[Dict[str, Any]] = None,
        starl_payload: Optional[Dict[str, Any]] = None,
        habit_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Stage decision in Tier 2 sandbox; auto-broadcasts new topic_key (Strategy 3C)."""
        return engine.stage_decision(
            session_id=session_id,
            topic_key=topic_key,
            decision_value=decision_value,
            rationale=rationale,
            evidence_ids=evidence_ids,
            file_scope=file_scope,
            pinned=pinned,
            scope_target=scope_target,
            proof=proof,
            starl_payload=starl_payload,
            habit_payload=habit_payload,
        )

    @mcp.tool()
    def record_rejected_hypothesis(
        session_id: str,
        topic_key: str,
        rejected_hypothesis: str,
        rejection_reason: str,
        evidence_ids: List[str],
        chosen_alternative: Optional[str] = None,
        stage_only: bool = False,
    ) -> Dict[str, Any]:
        """Record discarded hypothesis (Negative Knowledge) in L2 Cold Ledger."""
        return engine.record_rejected_hypothesis(
            session_id=session_id,
            topic_key=topic_key,
            rejected_hypothesis=rejected_hypothesis,
            rejection_reason=rejection_reason,
            evidence_ids=evidence_ids,
            chosen_alternative=chosen_alternative,
            stage_only=stage_only,
        )

    @mcp.tool()
    def promote_session(
        session_id: str,
        proof: Optional[Dict[str, Any]] = None,
        allow_overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Promote session sandbox to Base/Personal SSOT via Conflict & PoE Gates."""
        return engine.promote_session(
            session_id=session_id,
            proof=proof,
            allow_overwrite=allow_overwrite,
        ).model_dump()

    @mcp.tool()
    def resolve_conflict(
        conflict_id: str,
        choice: int | str,
        rejection_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve conflict: 1=Pivot(old->Cold/Neg), 2=Brainstorm, 3=Archive Negative."""
        return engine.resolve_conflict(
            conflict_id=conflict_id,
            choice=choice,
            rejection_reason=rejection_reason,
        ).model_dump()

    @mcp.tool()
    def query_cold_ledger(
        topic_key: Optional[str] = None,
        search_query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Query L2 Cold Ledger demoted rules, ADR history, and Negative Knowledge."""
        return engine.query_cold_ledger(topic_key=topic_key, search_query=search_query)

    @mcp.tool()
    def export_personal_pack(output_dir: Optional[str] = None) -> Dict[str, Any]:
        """Export Global Personal SSOT (STAR+L portfolio & cross-project habits)."""
        return engine.export_personal_pack(output_dir=output_dir)

    @mcp.tool()
    def rollback_transaction(tx_id: str) -> Dict[str, Any]:
        """Atomically revert Domain & Personal SSOT to pre-tx_id state."""
        return engine.rollback_transaction(tx_id=tx_id)

    @mcp.tool()
    def rollback(tx_id: str) -> Dict[str, Any]:
        """Alias for rollback_transaction(tx_id)."""
        return engine.rollback_transaction(tx_id=tx_id)

    return mcp, engine


def estimate_mcp_tool_schema_tokens() -> int:
    """Computes the token footprint of the compact MCP tool docstrings & signatures."""
    compact_spec = (
        "init_session(session_id) "
        "stage_decision(session_id,topic_key,decision_value,rationale,evidence_ids,file_scope,pinned,scope_target,proof,starl_payload,habit_payload) "
        "record_rejected_hypothesis(session_id,topic_key,rejected_hypothesis,rejection_reason,evidence_ids,chosen_alternative) "
        "promote_session(session_id,proof,allow_overwrite) "
        "resolve_conflict(conflict_id,choice,rejection_reason) "
        "query_cold_ledger(topic_key,search_query) "
        "export_personal_pack(output_dir) "
        "rollback_transaction(tx_id)"
    )
    return estimate_token_count(compact_spec)


def main() -> None:  # pragma: no cover
    mcp, _ = create_mcp_server()
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
