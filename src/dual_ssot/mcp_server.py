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
    Creates the MCP Server instance bound to a default `DualSSOTEngine` with dynamic
    multi-workspace session routing. Keeps tool descriptions ultra-compact (<500 tokens total).
    """
    default_ws = Path(
        workspace_path or os.environ.get("DUAL_SSOT_WORKSPACE", os.getcwd())
    ).resolve()
    default_engine = DualSSOTEngine(
        workspace_path=default_ws,
        personal_dir=personal_dir,
        project_id=project_id,
    )
    engines_by_ws: Dict[Path, DualSSOTEngine] = {default_ws: default_engine}
    session_to_ws: Dict[str, Path] = {}
    state: Dict[str, Path] = {"active_ws": default_ws}

    def _get_engine(
        session_id: Optional[str] = None,
        ws_override: Optional[str] = None,
    ) -> DualSSOTEngine:
        if ws_override and ws_override.strip():
            target_ws = Path(ws_override.strip()).resolve()
            if target_ws not in engines_by_ws:
                engines_by_ws[target_ws] = DualSSOTEngine(
                    workspace_path=target_ws,
                    personal_dir=personal_dir,
                    project_id=project_id if target_ws == default_ws else None,
                )
            state["active_ws"] = target_ws
            if session_id:
                session_to_ws[session_id] = target_ws
            return engines_by_ws[target_ws]

        if session_id and session_id in session_to_ws:
            return engines_by_ws[session_to_ws[session_id]]

        return engines_by_ws[state["active_ws"]]

    mcp = _MCPServerClass("dual-ssot")

    @mcp.tool()
    def init_session(
        session_id: str,
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Init MVCC read-only snapshot & get active canonical keys + L1 context."""
        eng = _get_engine(session_id=session_id, ws_override=workspace_path)
        return eng.init_session(session_id=session_id)

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
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Stage decision in Tier 2 sandbox; auto-broadcasts new topic_key (Strategy 3C)."""
        eng = _get_engine(session_id=session_id, ws_override=workspace_path)
        return eng.stage_decision(
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
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record discarded hypothesis (Negative Knowledge) in L2 Cold Ledger."""
        eng = _get_engine(session_id=session_id, ws_override=workspace_path)
        return eng.record_rejected_hypothesis(
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
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Promote session sandbox to Base/Personal SSOT via Conflict & PoE Gates."""
        eng = _get_engine(session_id=session_id, ws_override=workspace_path)
        return eng.promote_session(
            session_id=session_id,
            proof=proof,
            allow_overwrite=allow_overwrite,
        ).model_dump()

    @mcp.tool()
    def resolve_conflict(
        conflict_id: str,
        choice: int | str,
        rejection_reason: Optional[str] = None,
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve conflict: 1=Pivot(old->Cold/Neg), 2=Brainstorm, 3=Archive Negative."""
        eng = _get_engine(ws_override=workspace_path)
        for candidate in [eng] + [e for e in engines_by_ws.values() if e is not eng]:
            try:
                return candidate.resolve_conflict(
                    conflict_id=conflict_id,
                    choice=choice,
                    rejection_reason=rejection_reason,
                ).model_dump()
            except ValueError:
                continue
        return eng.resolve_conflict(
            conflict_id=conflict_id,
            choice=choice,
            rejection_reason=rejection_reason,
        ).model_dump()

    @mcp.tool()
    def query_cold_ledger(
        topic_key: Optional[str] = None,
        search_query: Optional[str] = None,
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Query L2 Cold Ledger demoted rules, ADR history, and Negative Knowledge."""
        eng = _get_engine(ws_override=workspace_path)
        return eng.query_cold_ledger(topic_key=topic_key, search_query=search_query)

    @mcp.tool()
    def export_personal_pack(output_dir: Optional[str] = None) -> Dict[str, Any]:
        """Export Global Personal SSOT (STAR+L portfolio & cross-project habits)."""
        return default_engine.export_personal_pack(output_dir=output_dir)

    @mcp.tool()
    def rollback_transaction(
        tx_id: str,
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically revert Domain & Personal SSOT to pre-tx_id state."""
        eng = _get_engine(ws_override=workspace_path)
        for candidate in [eng] + [e for e in engines_by_ws.values() if e is not eng]:
            try:
                return candidate.rollback_transaction(tx_id=tx_id)
            except ValueError:
                continue
        return eng.rollback_transaction(tx_id=tx_id)

    @mcp.tool()
    def rollback(
        tx_id: str,
        workspace_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Alias for rollback_transaction(tx_id)."""
        return rollback_transaction(tx_id=tx_id, workspace_path=workspace_path)

    return mcp, default_engine


def estimate_mcp_tool_schema_tokens() -> int:
    """Computes the token footprint of the compact MCP tool docstrings & signatures."""
    compact_spec = (
        "init_session(session_id,workspace_path) "
        "stage_decision(session_id,topic_key,decision_value,rationale,evidence_ids,file_scope,pinned,scope_target,proof,starl_payload,habit_payload,workspace_path) "
        "record_rejected_hypothesis(session_id,topic_key,rejected_hypothesis,rejection_reason,evidence_ids,chosen_alternative,workspace_path) "
        "promote_session(session_id,proof,allow_overwrite,workspace_path) "
        "resolve_conflict(conflict_id,choice,rejection_reason,workspace_path) "
        "query_cold_ledger(topic_key,search_query,workspace_path) "
        "export_personal_pack(output_dir) "
        "rollback_transaction(tx_id,workspace_path)"
    )
    return estimate_token_count(compact_spec)


def main() -> None:  # pragma: no cover
    mcp, _ = create_mcp_server()
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
