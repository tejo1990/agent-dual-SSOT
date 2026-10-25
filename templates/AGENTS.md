# Dual-SSOT Librarian Agent Protocol (Zero-Typing Context Compiler)

Every coding session MUST follow this deterministic 3-step protocol so context never rots and parallel sessions never split-brain:

## 1. Session Start (`init_session`)
- Immediately call `init_session(session_id="<conversation_id>")` on your first turn.
- Read the returned `active_context_md` (L1 Hot State) and `canonical_keys` list.
- Reuse an existing `topic_key` from `canonical_keys` whenever applicable. If introducing a new domain-specific rule, use a clean `domain.subtopic` key (Strategy 3C will automatically broadcast it to sibling sessions).

## 2. During Exploration & Implementation (`stage_decision` / `record_rejected_hypothesis`)
- Whenever an architectural choice or code change is made, call `stage_decision(...)` with verifiable `evidence_ids` (e.g. `["file:src/payment/retry.py#L10-L45", "commit:a1b2c3d"]`) and `file_scope` (the files modified).
- Whenever a hypothesis or approach is tested and discarded, call `record_rejected_hypothesis(...)` so future sessions never repeat the same dead-end.

## 3. Task Completion (`promote_session` & 2-Line Receipt)
- Without asking the user to type any command, call `promote_session(session_id="<conversation_id>")` once verification/tests finish.
- If `status == "CONFLICT_DETECTED"` is returned, present the 3 resolution options (`1: Pivot to new | 2: Keep as brainstorm | 3: Archive as Negative Knowledge`) and call `resolve_conflict(conflict_id, choice)`.
- Append the exact 2-line `receipt` returned by `promote_session` at the very bottom of your final response:
  ```text
  [SSOT Receipt] Domain +1 | Personal +1
  [SSOT Rollback] tx_id=tx_... -> call rollback_transaction('tx_...') to revert atomically.
  ```
