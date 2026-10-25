# Dual-Scope & Session-Isolated Context Compiler (`agent-dual-SSOT`)

Deterministic MVCC State Ledger & Context Compiler MCP Server that eliminates **Context Rot**, **Multi-Session Split-Brain**, and **Post-Project Engineering Knowledge Evaporation**.

---

## 1. Architectural Pillars & Invariants

### ① Artifact-Diff & Proof-of-Execution Anchored (Not Chat-Dump)
- **Tier 2 Sandbox (`stage_decision`, `record_rejected_hypothesis`)**: Rejects empty or dummy `evidence_ids` (`"tbd"`, `"todo"`, `"trust me"`, `"none"`) at the Pydantic v2 schema boundary.
- **Tier 3 Promotion (`promote_session`)**:
  - **Domain SSOT**: Requires `ProofOfExecution(file_exists=True, git_diff_changed_lines > 0)`.
  - **Personal SSOT (`STAR+L`)**: Requires `ProofOfExecution(file_exists=True, git_diff_changed_lines > 0, test_exit_code == 0)` to block Goodhart's Law dummy edits.

### ② Dual-Scope Automatic Routing (`Domain SSOT` ↔ `Personal SSOT`)
1. **Local Scope (`<workspace>/.ssot/`)**:
   - **L1 Hot State (`ACTIVE_CONTEXT.md` — Hard Cap: 100 lines / 1,500 tokens)**: Upserts matching `topic_key` in-place. Preserves `[PIN]` invariants at the top and automatically demotes oldest `[ACT]` rules to L2 Cold Ledger when capacity is reached, leaving a 1-line `[COLD_INDEX]` pointer at the bottom.
   - **L2 Cold Ledger (`COLD_LEDGER.md` & SQLite tables)**: Stores demoted L2 rules, time-series superseded ADR history (`cold_history`), and **Negative Knowledge** (`negative_knowledge` — rejected hypotheses and reasons so agents never re-propose discarded ideas).
2. **Global Scope (`~/.ssot/personal/`)**:
   - **Domain Contamination Filter**: Regex + structural validator blocks project/UI-specific noise (e.g., button hex colors, local table names).
   - **STAR+L Achievements**: Promoted immediately ($N=1$) when backed by `ProofOfExecution` (`test_exit_code == 0`) and quantitative `result_metric`.
   - **Meta-Preferences (Coding Habits)**: Quarantined in `CANDIDATE` status until observed across $K \ge 2$ (configurable via `compute_min_promotion_projects`) distinct `project_id`s.

### ③ MVCC 3-Tier Session Isolation & Strategy 3C Conflict Gate
- **Tier 1 (`Base SSOT`)**: Read-only snapshot (`base_revision_at_init`) captured on `init_session`.
- **Tier 2 (`Session Ephemeral Sandbox`)**: Physically isolated staging per `session_id` in SQLite WAL mode. Concurrent sessions A and B never dirty-read each other.
- **Tier 3 (`Semantic Merge & Conflict Gate` — Strategy 3C)**:
  1. **Core Canonical Template (30 Keys)** seeded on initialization.
  2. **Non-blocking Dynamic Key Broadcast**: Out-of-template `topic_key`s are automatically registered and broadcast to `canonical_keys` (capped at 150 auto-keys Kill Switch).
  3. **Hub-Excluded Leaf-File Scope Check**: Strips hub files (`package.json`, `pyproject.toml`, `config.*`, `__init__.py`, `index.*`) via `HUB_FILE_DENYLIST` and detects collisions when two sessions touch the same leaf business-logic file (`src/payment/retry.py`) even under different `topic_key` names.
  4. **Mandatory 3-Way Resolution (`resolve_conflict`)**:
     - `1` (`PIVOT_TO_NEW`): Promotes new conclusion to L1, moves old rule to `cold_history`, and archives competing session's hypothesis as `negative_knowledge`.
     - `2` (`KEEP_AS_BRAINSTORM`): Keeps current session staged as brainstorm without mutating Base SSOT.
     - `3` (`ARCHIVE_AS_NEGATIVE_KNOWLEDGE`): Archives the rejected hypothesis into L2 `negative_knowledge`.

### ④ Deterministic Compiler, 2-Line Receipt & Atomic Rollback
- All markdown artifacts (`ACTIVE_CONTEXT.md`, `COLD_LEDGER.md`, `PERSONAL_CONTEXT_PACK.md`) are compiled 100% deterministically from SQLite WAL state.
- Every commit emits a 2-line `[SSOT Receipt]` with a `tx_id` that can be atomically undone via `rollback_transaction(tx_id)`.

---

## 2. Database Schema (`SQLite WAL`)

### Domain SSOT (`<workspace>/.ssot/domain_ssot.db`)
| Table | Primary Key | Purpose |
| :--- | :--- | :--- |
| `ssot_meta` | `key` | Tracks `base_revision`, `step_clock`, and `project_id` |
| `canonical_keys` | `topic_key` | 30 core template keys + Strategy 3C dynamically broadcast keys |
| `base_decisions` | `topic_key` | Active (`L1_HOT`) and demoted (`L2_COLD`) rules with `pinned`, `version`, `leaf_files_json` |
| `cold_history` | `id` | Time-series log of superseded ADR values (`old_value -> new_value`) |
| `negative_knowledge` | `id` | Rejected hypotheses (`rejected_hypothesis`, `chosen_alternative`, `rejection_reason`, `evidence_json`) |
| `sessions` | `session_id` | MVCC session tracking (`base_revision_at_init`, `status`) |
| `staged_decisions` | `id` (`UNIQUE(session_id, topic_key)`) | Tier 2 Ephemeral Sandbox per session |
| `conflicts` | `conflict_id` | Tier 3 detected conflicts (`CANONICAL_KEY_VALUE_CONFLICT`, `BASE_REVISION_STALE_CONFLICT`, `LEAF_FILE_SCOPE_CONFLICT`) |
| `transaction_log` | `tx_id` | Full pre-mutation Domain + Personal snapshots for atomic `rollback_transaction` |

### Global Personal SSOT (`~/.ssot/personal/personal_ssot.db`)
| Table | Primary Key | Purpose |
| :--- | :--- | :--- |
| `starl_achievements` | `id` | Proof-of-Execution verified STAR+L problem-solving records |
| `coding_habits` | `habit_key` | Cross-project Meta-Preferences (`CANDIDATE` until `len(projects) >= min_projects_required`, then `ACTIVE`) |

---

## 3. MCP Tool Interface (<500 Schema Tokens)

1. `init_session(session_id)`
2. `stage_decision(session_id, topic_key, decision_value, rationale, evidence_ids, file_scope, pinned, scope_target, proof, starl_payload, habit_payload)`
3. `record_rejected_hypothesis(session_id, topic_key, rejected_hypothesis, rejection_reason, evidence_ids, chosen_alternative, stage_only)`
4. `promote_session(session_id, proof, allow_overwrite)`
5. `resolve_conflict(conflict_id, choice, rejection_reason)`
6. `query_cold_ledger(topic_key, search_query)`
7. `export_personal_pack(output_dir)`
8. `rollback_transaction(tx_id)` (alias: `rollback(tx_id)`)
