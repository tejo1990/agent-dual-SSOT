# Dual-SSOT Librarian Agent Protocol (Zero-Typing Context Compiler)

Every session (coding, architecture, Q&A, `dialectical-debate`, and `sovereign-distiller`) MUST unconditionally follow this deterministic 3-step protocol so context never rots and parallel sessions never split-brain:

## 1. Session Start (`init_session`)
- Immediately on your first turn, call the `dual-ssot` MCP tool `init_session`:
  `call_mcp_tool(ServerName="dual-ssot", ToolName="init_session", Arguments={"session_id": "<conversation_id>", "workspace_path": "<active_workspace_path>"})`
- Read the returned `active_context_md` (L1 Hot State) and `canonical_keys` list.
- Reuse an existing `topic_key` from `canonical_keys` whenever applicable. If introducing a new domain-specific rule, use a clean `domain.subtopic` key (Strategy 3C will automatically broadcast it to sibling sessions).

## 2. During Exploration, Implementation, Debate & Distillation (`stage_decision` / `record_rejected_hypothesis`)
- Whenever an architectural choice, verified proposition, or code change is made, call `stage_decision(...)` (`ServerName="dual-ssot"`) with verifiable `evidence_ids` (e.g. `["file:src/payment/retry.py#L10-L45", "commit:a1b2c3d"]`) and `file_scope` (the files modified or created).
- Whenever a hypothesis or approach is tested/debated and discarded, call `record_rejected_hypothesis(...)` (`ServerName="dual-ssot"`) so future sessions never repeat the same dead-end.

## 3. Turn / Task Completion (`promote_session` & 2-Line Receipt)
- Without asking the user to type any command, call `promote_session(session_id="<conversation_id>", workspace_path="<active_workspace_path>")` (`ServerName="dual-ssot"`) at the end of the turn.
- If `status == "CONFLICT_DETECTED"` is returned, present the 3 resolution options (`1: Pivot to new | 2: Keep as brainstorm | 3: Archive as Negative Knowledge`) and call `resolve_conflict(conflict_id, choice)`.
- Append the exact `receipt` returned by `promote_session` at the very bottom of your final response:
  ```text
  [SSOT Receipt] Domain +1 | Personal +1
  [SSOT Rollback] tx_id=tx_... -> call rollback_transaction('tx_...') to revert atomically.
  ```
