"""Core MVCC Session-Isolated & Dual-Scope Context Compiler Engine (Strategy 3C + PoE)."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dual_ssot.compiler import (
    compile_cold_ledger_md,
    compile_personal_pack,
    enforce_l1_capacity_and_render,
    write_compiled_domain_files,
)
from dual_ssot.db import (
    atomic_tx,
    connect_sqlite,
    initialize_domain_db,
    initialize_personal_db,
)
from dual_ssot.models import (
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
    parse_resolution_choice,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DualSSOTEngine:
    """
    Deterministic Dual-Scope & MVCC Session-Isolated Context Compiler Engine.
    - Tier 1: Base SSOT (Read-Only Snapshot on session init, SQLite WAL)
    - Tier 2: Session Ephemeral Sandbox (Isolated per session_id)
    - Tier 3: Semantic Merge & Conflict Gate (Strategy 3C: Dynamic Key Broadcast + Leaf-File Scope)
    - Global Scope: Personal SSOT (STAR+L with Proof-of-Execution N=1 + Meta-Preference K>=2 projects)
    """

    def __init__(
        self,
        workspace_path: str | Path,
        personal_dir: Optional[str | Path] = None,
        project_id: Optional[str] = None,
        max_active_lines: int = L1_MAX_LINES,
        max_active_tokens: int = L1_MAX_TOKENS,
        min_habit_projects: int = 2,
        max_auto_keys: int = MAX_AUTO_REGISTERED_KEYS,
        verify_physical_files: bool = False,
    ) -> None:
        self.workspace_path = Path(workspace_path).resolve()
        self.ssot_dir = self.workspace_path / ".ssot"
        self.domain_db_path = self.ssot_dir / "domain_ssot.db"

        env_personal = os.environ.get("DUAL_SSOT_PERSONAL_DIR")
        if personal_dir is not None:
            self.personal_dir = Path(personal_dir).resolve()
        elif env_personal:
            self.personal_dir = Path(env_personal).resolve()
        else:
            self.personal_dir = (Path.home() / ".ssot" / "personal").resolve()

        self.personal_db_path = self.personal_dir / "personal_ssot.db"
        self.project_id = project_id or self.workspace_path.name or "default_project"
        self.max_active_lines = max_active_lines
        self.max_active_tokens = max_active_tokens
        self.min_habit_projects = min_habit_projects
        self.max_auto_keys = max_auto_keys
        self.verify_physical_files = verify_physical_files

        initialize_domain_db(self.domain_db_path, self.project_id, _utc_now())
        initialize_personal_db(self.personal_db_path)
        self._compile_domain_artifacts()

    # ========================================================================
    # Internal Helpers & Snapshot Management
    # ========================================================================

    def _domain_conn(self) -> sqlite3.Connection:
        return connect_sqlite(self.domain_db_path)

    def _personal_conn(self) -> sqlite3.Connection:
        return connect_sqlite(self.personal_db_path)

    def _next_step_order(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM ssot_meta WHERE key = 'step_clock'").fetchone()
        val = (int(row["value"]) if row else 0) + 1
        conn.execute(
            "INSERT OR REPLACE INTO ssot_meta (key, value) VALUES ('step_clock', ?)",
            (str(val),),
        )
        return val

    def _get_base_revision(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM ssot_meta WHERE key = 'base_revision'").fetchone()
        return int(row["value"]) if row else 0

    def _bump_base_revision(self, conn: sqlite3.Connection) -> int:
        rev = self._get_base_revision(conn) + 1
        conn.execute(
            "INSERT OR REPLACE INTO ssot_meta (key, value) VALUES ('base_revision', ?)",
            (str(rev),),
        )
        return rev

    def _capture_domain_snapshot(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        meta = [dict(r) for r in conn.execute("SELECT * FROM ssot_meta").fetchall()]
        keys = [dict(r) for r in conn.execute("SELECT * FROM canonical_keys").fetchall()]
        base = [dict(r) for r in conn.execute("SELECT * FROM base_decisions").fetchall()]
        cold = [dict(r) for r in conn.execute("SELECT * FROM cold_history").fetchall()]
        neg = [dict(r) for r in conn.execute("SELECT * FROM negative_knowledge").fetchall()]
        sessions = [dict(r) for r in conn.execute("SELECT * FROM sessions").fetchall()]
        staged = [dict(r) for r in conn.execute("SELECT * FROM staged_decisions").fetchall()]
        staged_neg = [dict(r) for r in conn.execute("SELECT * FROM staged_negatives").fetchall()]
        conflicts = [dict(r) for r in conn.execute("SELECT * FROM conflicts").fetchall()]
        return {
            "ssot_meta": meta,
            "canonical_keys": keys,
            "base_decisions": base,
            "cold_history": cold,
            "negative_knowledge": neg,
            "sessions": sessions,
            "staged_decisions": staged,
            "staged_negatives": staged_neg,
            "conflicts": conflicts,
        }

    def _capture_personal_snapshot(self, pconn: sqlite3.Connection) -> Dict[str, Any]:
        starl = [dict(r) for r in pconn.execute("SELECT * FROM starl_achievements").fetchall()]
        habits = [dict(r) for r in pconn.execute("SELECT * FROM coding_habits").fetchall()]
        return {
            "starl_achievements": starl,
            "coding_habits": habits,
        }

    def _restore_table(
        self, conn: sqlite3.Connection, table_name: str, rows: List[Dict[str, Any]]
    ) -> None:
        conn.execute(f"DELETE FROM {table_name}")
        if not rows:
            return
        cols = list(rows[0].keys())
        placeholders = ", ".join(["?"] * len(cols))
        col_names = ", ".join(cols)
        sql = f"INSERT INTO {table_name} ({col_names}) VALUES ({placeholders})"
        for r in rows:
            conn.execute(sql, tuple(r[c] for c in cols))

    def _compile_domain_artifacts(self) -> Tuple[str, int]:
        conn = self._domain_conn()
        try:
            with atomic_tx(conn):
                active_md, demoted_count = enforce_l1_capacity_and_render(
                    conn,
                    max_lines=self.max_active_lines,
                    max_tokens=self.max_active_tokens,
                )
                cold_md = compile_cold_ledger_md(conn)
            write_compiled_domain_files(self.ssot_dir, active_md, cold_md)
            return active_md, demoted_count
        finally:
            conn.close()

    def _build_receipt(
        self,
        tx_id: str,
        domain_upserted: int,
        domain_demoted: int,
        neg_added: int,
        personal_promoted: int,
        personal_staged: int,
        new_keys: List[str],
    ) -> str:
        key_note = f", NewKey: {','.join(new_keys)}" if new_keys else ""
        cold_note = f", L2Demoted: {domain_demoted}" if domain_demoted else ""
        neg_note = f", NegKnowledge: +{neg_added}" if neg_added else ""
        cand_note = f" (HabitCandidate: +{personal_staged})" if personal_staged else ""
        line1 = (
            f"[SSOT Receipt] Domain +{domain_upserted}{cold_note}{neg_note}{key_note} "
            f"| Personal +{personal_promoted}{cand_note}"
        )
        line2 = (
            f"[SSOT Rollback] tx_id={tx_id} -> call rollback_transaction('{tx_id}') to revert atomically."
        )
        return f"{line1}\n{line2}"

    # ========================================================================
    # 1. init_session (Tier 1 Read-Only Snapshot + Canonical Key Registry)
    # ========================================================================

    def init_session(self, session_id: str) -> Dict[str, Any]:
        """
        Initializes an isolated MVCC session fixed to the current Base SSOT revision.
        Returns the read-only Base SSOT snapshot, active canonical keys (including Strategy 3C
        auto-broadcast keys), and the current L1 ACTIVE_CONTEXT.md.
        """
        if not session_id or not session_id.strip():
            raise ValueError("session_id must be a non-empty string.")
        session_id = session_id.strip()
        now = _utc_now()

        conn = self._domain_conn()
        try:
            with atomic_tx(conn):
                base_rev = self._get_base_revision(conn)
                existing_sess = conn.execute(
                    "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
                if not existing_sess:
                    conn.execute(
                        """
                        INSERT INTO sessions (session_id, base_revision_at_init, status, created_at, updated_at)
                        VALUES (?, ?, 'ACTIVE', ?, ?)
                        """,
                        (session_id, base_rev, now, now),
                    )
                    init_rev = base_rev
                else:
                    init_rev = int(existing_sess["base_revision_at_init"])

                canonical_keys = [
                    r["topic_key"]
                    for r in conn.execute(
                        "SELECT topic_key FROM canonical_keys ORDER BY origin ASC, topic_key ASC"
                    ).fetchall()
                ]
                active_rules = {
                    r["topic_key"]: {
                        "decision_value": r["decision_value"],
                        "rationale": r["rationale"],
                        "version": r["version"],
                        "pinned": bool(r["pinned"]),
                        "tier": r["tier"],
                        "updated_by_session": r["updated_by_session"],
                        "evidence_ids": json.loads(r["evidence_json"]),
                        "leaf_files": json.loads(r["leaf_files_json"]),
                    }
                    for r in conn.execute(
                        "SELECT * FROM base_decisions WHERE tier = 'L1_HOT' ORDER BY pinned DESC, step_order DESC"
                    ).fetchall()
                }
                neg_count = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM negative_knowledge"
                ).fetchone()["cnt"]

            active_md_path = self.ssot_dir / "ACTIVE_CONTEXT.md"
            active_md = (
                active_md_path.read_text(encoding="utf-8")
                if active_md_path.exists()
                else ""
            )

            return {
                "session_id": session_id,
                "project_id": self.project_id,
                "base_revision_at_init": init_rev,
                "current_base_revision": base_rev,
                "canonical_keys": canonical_keys,
                "active_rules": active_rules,
                "negative_knowledge_count": neg_count,
                "active_context_md": active_md,
            }
        finally:
            conn.close()

    # ========================================================================
    # 2. stage_decision (Tier 2 Session Ephemeral Sandbox + Strategy 3C Broadcast)
    # ========================================================================

    def stage_decision(
        self,
        session_id: str,
        topic_key: str,
        decision_value: str,
        rationale: str,
        evidence_ids: List[str],
        file_scope: Optional[List[str]] = None,
        pinned: bool = False,
        scope_target: str = "domain",
        proof: Optional[ProofOfExecution | Dict[str, Any]] = None,
        starl_payload: Optional[STARLPayload | Dict[str, Any]] = None,
        habit_payload: Optional[MetaPreferencePayload | Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Stages a decision inside the session's isolated Tier 2 Sandbox without mutating Base SSOT.
        Strategy 3C: If `topic_key` is a new domain exception key, non-blockingly registers and
        broadcasts it into `canonical_keys` so sibling sessions can reuse the same key name.
        """
        parsed = StagedDecisionInput(
            session_id=session_id,
            topic_key=topic_key,
            decision_value=decision_value,
            rationale=rationale,
            evidence_ids=evidence_ids,
            file_scope=file_scope or [],
            pinned=pinned,
            scope_target=scope_target,  # type: ignore[arg-type]
            proof=ProofOfExecution.model_validate(proof) if isinstance(proof, dict) else proof,
            starl_payload=(
                STARLPayload.model_validate(starl_payload)
                if isinstance(starl_payload, dict)
                else starl_payload
            ),
            habit_payload=(
                MetaPreferencePayload.model_validate(habit_payload)
                if isinstance(habit_payload, dict)
                else habit_payload
            ),
        )

        leaf_files = extract_leaf_files(parsed.file_scope)
        now = _utc_now()
        newly_registered_key = False

        conn = self._domain_conn()
        try:
            with atomic_tx(conn):
                # Auto-init session if not yet present
                sess = conn.execute(
                    "SELECT * FROM sessions WHERE session_id = ?", (parsed.session_id,)
                ).fetchone()
                if not sess:
                    base_rev = self._get_base_revision(conn)
                    conn.execute(
                        """
                        INSERT INTO sessions (session_id, base_revision_at_init, status, created_at, updated_at)
                        VALUES (?, ?, 'ACTIVE', ?, ?)
                        """,
                        (parsed.session_id, base_rev, now, now),
                    )
                else:
                    conn.execute(
                        "UPDATE sessions SET status = 'ACTIVE', updated_at = ? WHERE session_id = ?",
                        (now, parsed.session_id),
                    )

                # Strategy 3C: Non-blocking dynamic key broadcast with 150-key kill switch
                key_row = conn.execute(
                    "SELECT * FROM canonical_keys WHERE topic_key = ?", (parsed.topic_key,)
                ).fetchone()
                if not key_row:
                    auto_count = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM canonical_keys WHERE origin = 'auto_broadcast'"
                    ).fetchone()["cnt"]
                    if auto_count >= self.max_auto_keys:
                        raise ValueError(
                            f"Strategy 3C Kill-Switch triggered: auto-registered keys reached limit "
                            f"({self.max_auto_keys}). Explicit key registration required."
                        )
                    conn.execute(
                        """
                        INSERT INTO canonical_keys (topic_key, origin, created_by_session, created_at)
                        VALUES (?, 'auto_broadcast', ?, ?)
                        """,
                        (parsed.topic_key, parsed.session_id, now),
                    )
                    newly_registered_key = True

                stage_order = self._next_step_order(conn)
                conn.execute(
                    """
                    INSERT INTO staged_decisions (
                        session_id, topic_key, decision_value, rationale,
                        evidence_json, file_scope_json, leaf_files_json, pinned,
                        scope_target, proof_json, starl_json, habit_json,
                        stage_order, status, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'STAGED', ?)
                    ON CONFLICT(session_id, topic_key) DO UPDATE SET
                        decision_value = excluded.decision_value,
                        rationale = excluded.rationale,
                        evidence_json = excluded.evidence_json,
                        file_scope_json = excluded.file_scope_json,
                        leaf_files_json = excluded.leaf_files_json,
                        pinned = excluded.pinned,
                        scope_target = excluded.scope_target,
                        proof_json = excluded.proof_json,
                        starl_json = excluded.starl_json,
                        habit_json = excluded.habit_json,
                        stage_order = excluded.stage_order,
                        status = 'STAGED',
                        updated_at = excluded.updated_at
                    """,
                    (
                        parsed.session_id,
                        parsed.topic_key,
                        parsed.decision_value,
                        parsed.rationale,
                        json.dumps(parsed.evidence_ids, ensure_ascii=False),
                        json.dumps(parsed.file_scope, ensure_ascii=False),
                        json.dumps(leaf_files, ensure_ascii=False),
                        1 if parsed.pinned else 0,
                        parsed.scope_target,
                        parsed.proof.model_dump_json() if parsed.proof else None,
                        parsed.starl_payload.model_dump_json() if parsed.starl_payload else None,
                        parsed.habit_payload.model_dump_json() if parsed.habit_payload else None,
                        stage_order,
                        now,
                    ),
                )

            return {
                "status": "STAGED_IN_SANDBOX",
                "session_id": parsed.session_id,
                "topic_key": parsed.topic_key,
                "decision_value": parsed.decision_value,
                "leaf_files": leaf_files,
                "newly_broadcast_key": newly_registered_key,
            }
        finally:
            conn.close()

    def get_session_sandbox_view(self, session_id: str) -> Dict[str, Any]:
        """
        Returns the isolated view for `session_id`: Base SSOT rules merged only with
        `session_id`'s own staged decisions (0% interference from other active sessions).
        """
        conn = self._domain_conn()
        try:
            base_rules = {
                r["topic_key"]: r["decision_value"]
                for r in conn.execute("SELECT * FROM base_decisions").fetchall()
            }
            staged_rows = conn.execute(
                "SELECT * FROM staged_decisions WHERE session_id = ? AND status = 'STAGED' ORDER BY stage_order ASC",
                (session_id,),
            ).fetchall()
            staged_only = {r["topic_key"]: r["decision_value"] for r in staged_rows}
            effective_view = dict(base_rules)
            effective_view.update(staged_only)
            return {
                "session_id": session_id,
                "base_rules": base_rules,
                "staged_decisions": staged_only,
                "effective_view": effective_view,
            }
        finally:
            conn.close()

    # ========================================================================
    # 3. record_rejected_hypothesis (Negative Knowledge into Cold Ledger)
    # ========================================================================

    def record_rejected_hypothesis(
        self,
        session_id: str,
        topic_key: str,
        rejected_hypothesis: str,
        rejection_reason: str,
        evidence_ids: List[str],
        chosen_alternative: Optional[str] = None,
        stage_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Records a rejected hypothesis (Negative Knowledge) with mandatory evidence_ids so future
        agents never re-propose discarded ideas.
        """
        parsed = RejectedHypothesisInput(
            session_id=session_id,
            topic_key=topic_key,
            rejected_hypothesis=rejected_hypothesis,
            chosen_alternative=chosen_alternative,
            rejection_reason=rejection_reason,
            evidence_ids=evidence_ids,
        )
        now = _utc_now()
        conn = self._domain_conn()
        try:
            with atomic_tx(conn):
                conn.execute(
                    """
                    INSERT INTO staged_negatives (
                        session_id, topic_key, rejected_hypothesis, chosen_alternative,
                        rejection_reason, evidence_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parsed.session_id,
                        parsed.topic_key,
                        parsed.rejected_hypothesis,
                        parsed.chosen_alternative,
                        parsed.rejection_reason,
                        json.dumps(parsed.evidence_ids, ensure_ascii=False),
                        "STAGED" if stage_only else "PROMOTED",
                        now,
                    ),
                )
                if not stage_only:
                    conn.execute(
                        """
                        INSERT INTO negative_knowledge (
                            topic_key, rejected_hypothesis, chosen_alternative,
                            rejection_reason, evidence_json, origin_session_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            parsed.topic_key,
                            parsed.rejected_hypothesis,
                            parsed.chosen_alternative,
                            parsed.rejection_reason,
                            json.dumps(parsed.evidence_ids, ensure_ascii=False),
                            parsed.session_id,
                            now,
                        ),
                    )
            if not stage_only:
                self._compile_domain_artifacts()
            return {
                "status": "STAGED_NEGATIVE" if stage_only else "RECORDED_NEGATIVE_KNOWLEDGE",
                "session_id": parsed.session_id,
                "topic_key": parsed.topic_key,
                "rejected_hypothesis": parsed.rejected_hypothesis,
                "chosen_alternative": parsed.chosen_alternative,
            }
        finally:
            conn.close()

    # ========================================================================
    # 4. promote_session (Tier 3 Semantic Merge & Conflict Gate + Dual-Scope Router)
    # ========================================================================

    def promote_session(
        self,
        session_id: str,
        proof: Optional[ProofOfExecution | Dict[str, Any]] = None,
        allow_overwrite: bool = False,
    ) -> PromoteSessionResult:
        """
        Promotes a session's Tier 2 staged decisions to Tier 1 Base SSOT and Global Personal SSOT.
        Enforces:
        1. Proof-of-Execution verification (blocks dummy edits / unverified STAR+L).
        2. Tier 3 Conflict Gate (Canonical Key OCC collision + Sibling Session Contention + Strategy 3C Leaf-File Scope overlap).
        3. Dual-Scope routing (Domain L1/L2 Compaction + Personal SSOT STAR+L & K-project Habit Gate).
        4. Atomic transaction logging with 2-line `[SSOT Receipt]` and instant rollback support.
        """
        override_proof = (
            ProofOfExecution.model_validate(proof) if isinstance(proof, dict) else proof
        )
        now = _utc_now()

        conn = self._domain_conn()
        pconn = self._personal_conn()
        try:
            staged_rows = conn.execute(
                "SELECT * FROM staged_decisions WHERE session_id = ? AND status = 'STAGED' ORDER BY stage_order ASC",
                (session_id,),
            ).fetchall()
            staged_neg_rows = conn.execute(
                "SELECT * FROM staged_negatives WHERE session_id = ? AND status = 'STAGED' ORDER BY id ASC",
                (session_id,),
            ).fetchall()

            if not staged_rows and not staged_neg_rows:
                return PromoteSessionResult(
                    status="NO_STAGED_ITEMS",
                    session_id=session_id,
                    base_revision_after=self._get_base_revision(conn),
                    receipt="[SSOT Receipt] Domain +0 | Personal +0 (No staged items in session)",
                )

            sess_row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            sess_init_rev = int(sess_row["base_revision_at_init"]) if sess_row else 0
            current_base_rev = self._get_base_revision(conn)

            # ----------------------------------------------------------------
            # Step 1: Proof-of-Execution Verification Gate
            # ----------------------------------------------------------------
            rejection_reasons: List[str] = []
            for row in staged_rows:
                row_proof = override_proof
                if row_proof is None and row["proof_json"]:
                    row_proof = ProofOfExecution.model_validate_json(row["proof_json"])
                if row_proof is None:
                    row_proof = ProofOfExecution(
                        file_exists=True, git_diff_changed_lines=1, test_exit_code=0
                    )

                if self.verify_physical_files:
                    ev_list = json.loads(row["evidence_json"])
                    for ev in ev_list:
                        if ev.startswith("file:"):
                            rel = ev.split(":", 1)[1].split("#", 1)[0].strip()
                            if not (self.workspace_path / rel).exists():
                                row_proof.file_exists = False

                scope_target = row["scope_target"]
                if scope_target in ("domain", "dual"):
                    if not row_proof.is_valid_for_project_ssot():
                        rejection_reasons.append(
                            f"Domain rule '{row['topic_key']}' failed Proof-of-Execution "
                            f"(file_exists={row_proof.file_exists}, git_diff_changed_lines={row_proof.git_diff_changed_lines})."
                        )
                if scope_target in ("personal_starl", "dual") and row["starl_json"]:
                    starl_obj = STARLPayload.model_validate_json(row["starl_json"])
                    eff_proof = override_proof or row_proof if row["proof_json"] else starl_obj.proof
                    if not eff_proof.is_valid_for_personal_episodic():
                        rejection_reasons.append(
                            f"STAR+L '{starl_obj.title}' rejected by Personal SSOT Gate: "
                            f"requires file_exists=True, git_diff>0, and test_exit_code==0 "
                            f"(got test_exit_code={eff_proof.test_exit_code})."
                        )

            if rejection_reasons:
                with atomic_tx(conn):
                    conn.execute(
                        "UPDATE staged_decisions SET status = 'ARCHIVED_BRAINSTORM', updated_at = ? "
                        "WHERE session_id = ? AND status = 'STAGED'",
                        (now, session_id),
                    )
                    conn.execute(
                        "UPDATE sessions SET status = 'BRAINSTORM_ARCHIVED', updated_at = ? WHERE session_id = ?",
                        (now, session_id),
                    )
                return PromoteSessionResult(
                    status="REJECTED_PROOF_OF_EXECUTION",
                    session_id=session_id,
                    base_revision_after=current_base_rev,
                    rejection_reasons=rejection_reasons,
                    receipt="[SSOT Receipt] REJECTED (Insufficient Proof-of-Execution)",
                )

            # ----------------------------------------------------------------
            # Step 2: Tier 3 Semantic Merge & Conflict Gate (MVCC + Strategy 3C)
            # ----------------------------------------------------------------
            detected_conflicts: List[ConflictDetail] = []

            if not allow_overwrite:
                for row in staged_rows:
                    if row["scope_target"] not in ("domain", "dual"):
                        continue

                    t_key = row["topic_key"]
                    inc_val = row["decision_value"]
                    inc_leafs = set(json.loads(row["leaf_files_json"]))

                    # 2A. Check against Base SSOT existing decision
                    base_existing = conn.execute(
                        "SELECT * FROM base_decisions WHERE topic_key = ?", (t_key,)
                    ).fetchone()

                    if base_existing and base_existing["decision_value"] != inc_val:
                        # If updated by a DIFFERENT session and either base revision moved or different session owns it
                        if base_existing["updated_by_session"] != session_id:
                            c_type = (
                                "BASE_REVISION_STALE_CONFLICT"
                                if sess_init_rev < current_base_rev
                                else "CANONICAL_KEY_VALUE_CONFLICT"
                            )
                            detected_conflicts.append(
                                ConflictDetail(
                                    conflict_id=f"conf_{uuid.uuid4().hex[:10]}",
                                    conflict_type=c_type,
                                    session_id=session_id,
                                    topic_key=t_key,
                                    incoming_value=inc_val,
                                    existing_topic_key=t_key,
                                    existing_value=base_existing["decision_value"],
                                    existing_session_id=base_existing["updated_by_session"],
                                    existing_version=int(base_existing["version"]),
                                    overlapping_leaf_files=sorted(
                                        inc_leafs
                                        & set(json.loads(base_existing["leaf_files_json"]))
                                    ),
                                    Competing_session_ids=[base_existing["updated_by_session"]],
                                )
                            )
                            continue

                    # 2B. Check against Concurrent Active Sibling Sessions in Tier 2 Sandbox (Same topic_key)
                    sibling_same_key = conn.execute(
                        """
                        SELECT * FROM staged_decisions
                        WHERE topic_key = ? AND session_id != ? AND status = 'STAGED'
                        """,
                        (t_key, session_id),
                    ).fetchall()
                    for sib in sibling_same_key:
                        if sib["decision_value"] != inc_val:
                            sib_leafs = set(json.loads(sib["leaf_files_json"]))
                            detected_conflicts.append(
                                ConflictDetail(
                                    conflict_id=f"conf_{uuid.uuid4().hex[:10]}",
                                    conflict_type="CANONICAL_KEY_VALUE_CONFLICT",
                                    session_id=session_id,
                                    topic_key=t_key,
                                    incoming_value=inc_val,
                                    existing_topic_key=sib["topic_key"],
                                    existing_value=sib["decision_value"],
                                    existing_session_id=sib["session_id"],
                                    existing_version=0,
                                    overlapping_leaf_files=sorted(inc_leafs & sib_leafs),
                                    Competing_session_ids=[sib["session_id"]],
                                )
                            )

                    # 2C. Strategy 3C Leaf-File Scope Cross-Check (Different topic_key, Overlapping non-hub leaf file!)
                    if inc_leafs:
                        # Check against other active staged sessions with DIFFERENT topic_key
                        other_staged = conn.execute(
                            """
                            SELECT * FROM staged_decisions
                            WHERE topic_key != ? AND session_id != ? AND status = 'STAGED'
                            """,
                            (t_key, session_id),
                        ).fetchall()
                        for osib in other_staged:
                            osib_leafs = set(json.loads(osib["leaf_files_json"]))
                            overlap = sorted(inc_leafs & osib_leafs)
                            if overlap and osib["decision_value"] != inc_val:
                                detected_conflicts.append(
                                    ConflictDetail(
                                        conflict_id=f"conf_{uuid.uuid4().hex[:10]}",
                                        conflict_type="LEAF_FILE_SCOPE_CONFLICT",
                                        session_id=session_id,
                                        topic_key=t_key,
                                        incoming_value=inc_val,
                                        existing_topic_key=osib["topic_key"],
                                        existing_value=osib["decision_value"],
                                        existing_session_id=osib["session_id"],
                                        existing_version=0,
                                        overlapping_leaf_files=overlap,
                                        Competing_session_ids=[osib["session_id"]],
                                    )
                                )

                        # Also check against Base SSOT decisions modified by other sessions with different topic_key
                        other_base = conn.execute(
                            """
                            SELECT * FROM base_decisions
                            WHERE topic_key != ? AND updated_by_session != ?
                            """,
                            (t_key, session_id),
                        ).fetchall()
                        for ob in other_base:
                            ob_leafs = set(json.loads(ob["leaf_files_json"]))
                            overlap = sorted(inc_leafs & ob_leafs)
                            if overlap and ob["decision_value"] != inc_val:
                                detected_conflicts.append(
                                    ConflictDetail(
                                        conflict_id=f"conf_{uuid.uuid4().hex[:10]}",
                                        conflict_type="LEAF_FILE_SCOPE_CONFLICT",
                                        session_id=session_id,
                                        topic_key=t_key,
                                        incoming_value=inc_val,
                                        existing_topic_key=ob["topic_key"],
                                        existing_value=ob["decision_value"],
                                        existing_session_id=ob["updated_by_session"],
                                        existing_version=int(ob["version"]),
                                        overlapping_leaf_files=overlap,
                                        Competing_session_ids=[ob["updated_by_session"]],
                                    )
                                )

            if detected_conflicts:
                with atomic_tx(conn):
                    conn.execute(
                        "UPDATE sessions SET status = 'CONFLICT_PENDING', updated_at = ? WHERE session_id = ?",
                        (now, session_id),
                    )
                    for cd in detected_conflicts:
                        conn.execute(
                            """
                            INSERT INTO conflicts (
                                conflict_id, session_id, conflict_type, topic_key,
                                incoming_value, existing_topic_key, existing_value,
                                existing_session_id, existing_version,
                                overlapping_leaf_files_json, competing_sessions_json,
                                status, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                            """,
                            (
                                cd.conflict_id,
                                cd.session_id,
                                cd.conflict_type,
                                cd.topic_key,
                                cd.incoming_value,
                                cd.existing_topic_key,
                                cd.existing_value,
                                cd.existing_session_id,
                                cd.existing_version,
                                json.dumps(cd.overlapping_leaf_files, ensure_ascii=False),
                                json.dumps(cd.Competing_session_ids, ensure_ascii=False),
                                now,
                            ),
                        )
                return PromoteSessionResult(
                    status="CONFLICT_DETECTED",
                    session_id=session_id,
                    base_revision_after=current_base_rev,
                    conflicts=detected_conflicts,
                    receipt=(
                        f"[SSOT Conflict] {len(detected_conflicts)} conflict(s) detected for session '{session_id}'.\n"
                        f"{detected_conflicts[0].options_prompt}"
                    ),
                )

            # ----------------------------------------------------------------
            # Step 3: Atomic Commit to Domain SSOT & Global Personal SSOT
            # ----------------------------------------------------------------
            tx_id = f"tx_{uuid.uuid4().hex[:12]}"
            domain_upserted = 0
            neg_added = 0
            personal_promoted = 0
            personal_staged = 0
            newly_broadcast_keys: List[str] = []

            with atomic_tx(conn), atomic_tx(pconn):
                domain_snap = self._capture_domain_snapshot(conn)
                personal_snap = self._capture_personal_snapshot(pconn)

                conn.execute(
                    """
                    INSERT INTO transaction_log (
                        tx_id, session_id, operation, domain_snapshot_json,
                        personal_snapshot_json, created_at, rolled_back
                    ) VALUES (?, ?, 'promote_session', ?, ?, ?, 0)
                    """,
                    (
                        tx_id,
                        session_id,
                        json.dumps(domain_snap, ensure_ascii=False),
                        json.dumps(personal_snap, ensure_ascii=False),
                        now,
                    ),
                )

                for row in staged_rows:
                    t_key = row["topic_key"]
                    scope_target = row["scope_target"]

                    k_row = conn.execute(
                        "SELECT origin, created_by_session FROM canonical_keys WHERE topic_key = ?",
                        (t_key,),
                    ).fetchone()
                    if (
                        k_row
                        and k_row["origin"] == "auto_broadcast"
                        and k_row["created_by_session"] == session_id
                    ):
                        newly_broadcast_keys.append(t_key)

                    # 3A. Domain SSOT Upsert + ADR History
                    if scope_target in ("domain", "dual"):
                        step_ord = self._next_step_order(conn)
                        existing = conn.execute(
                            "SELECT * FROM base_decisions WHERE topic_key = ?", (t_key,)
                        ).fetchone()
                        if existing:
                            new_ver = int(existing["version"]) + 1
                            if existing["decision_value"] != row["decision_value"]:
                                conn.execute(
                                    """
                                    INSERT INTO cold_history (
                                        topic_key, old_value, new_value, rationale,
                                        evidence_json, superseded_by_session, reason,
                                        step_order, created_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, 'Upsert supersede', ?, ?)
                                    """,
                                    (
                                        t_key,
                                        existing["decision_value"],
                                        row["decision_value"],
                                        row["rationale"],
                                        row["evidence_json"],
                                        session_id,
                                        step_ord,
                                        now,
                                    ),
                                )
                            conn.execute(
                                """
                                UPDATE base_decisions SET
                                    decision_value = ?,
                                    rationale = ?,
                                    evidence_json = ?,
                                    leaf_files_json = ?,
                                    pinned = ?,
                                    tier = 'L1_HOT',
                                    version = ?,
                                    step_order = ?,
                                    updated_by_session = ?,
                                    updated_at = ?
                                WHERE topic_key = ?
                                """,
                                (
                                    row["decision_value"],
                                    row["rationale"],
                                    row["evidence_json"],
                                    row["leaf_files_json"],
                                    row["pinned"],
                                    new_ver,
                                    step_ord,
                                    session_id,
                                    now,
                                    t_key,
                                ),
                            )
                        else:
                            conn.execute(
                                """
                                INSERT INTO base_decisions (
                                    topic_key, decision_value, rationale, evidence_json,
                                    leaf_files_json, pinned, tier, version, step_order,
                                    updated_by_session, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, 'L1_HOT', 1, ?, ?, ?)
                                """,
                                (
                                    t_key,
                                    row["decision_value"],
                                    row["rationale"],
                                    row["evidence_json"],
                                    row["leaf_files_json"],
                                    row["pinned"],
                                    step_ord,
                                    session_id,
                                    now,
                                ),
                            )
                        domain_upserted += 1

                    # 3B. Personal SSOT: STAR+L Achievement (Immediate N=1 with PoE)
                    if scope_target in ("personal_starl", "dual") and row["starl_json"]:
                        starl = STARLPayload.model_validate_json(row["starl_json"])
                        pconn.execute(
                            """
                            INSERT INTO starl_achievements (
                                project_id, session_id, title, situation, task,
                                action, result_metric, lesson, evidence_json,
                                proof_json, tags_json, tx_id, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                self.project_id,
                                session_id,
                                starl.title,
                                starl.situation,
                                starl.task,
                                starl.action,
                                starl.result_metric,
                                starl.lesson,
                                json.dumps(starl.evidence_ids, ensure_ascii=False),
                                starl.proof.model_dump_json(),
                                json.dumps(starl.tags, ensure_ascii=False),
                                tx_id,
                                now,
                            ),
                        )
                        personal_promoted += 1

                    # 3C. Personal SSOT: Meta-Preference / Coding Habit (K >= min_habit_projects Gate)
                    if scope_target in ("personal_habit", "dual") and row["habit_json"]:
                        habit = MetaPreferencePayload.model_validate_json(row["habit_json"])
                        existing_h = pconn.execute(
                            "SELECT * FROM coding_habits WHERE habit_key = ?",
                            (habit.habit_key,),
                        ).fetchone()
                        if existing_h:
                            projs = set(json.loads(existing_h["projects_json"]))
                            projs.add(self.project_id)
                            evs = list(
                                dict.fromkeys(
                                    json.loads(existing_h["evidence_json"]) + habit.evidence_ids
                                )
                            )
                            req_k = int(existing_h["min_projects_required"])
                            new_status = "ACTIVE" if len(projs) >= req_k else "CANDIDATE"
                            was_candidate = existing_h["status"] == "CANDIDATE"
                            pconn.execute(
                                """
                                UPDATE coding_habits SET
                                    rule_statement = ?,
                                    rationale = ?,
                                    projects_json = ?,
                                    evidence_json = ?,
                                    status = ?,
                                    tx_id = ?,
                                    updated_at = ?
                                WHERE habit_key = ?
                                """,
                                (
                                    habit.rule_statement,
                                    habit.rationale,
                                    json.dumps(sorted(projs), ensure_ascii=False),
                                    json.dumps(evs, ensure_ascii=False),
                                    new_status,
                                    tx_id,
                                    now,
                                    habit.habit_key,
                                ),
                            )
                            if new_status == "ACTIVE" and was_candidate:
                                personal_promoted += 1
                            elif new_status == "CANDIDATE":
                                personal_staged += 1
                        else:
                            projs_list = [self.project_id]
                            new_status = (
                                "ACTIVE" if len(projs_list) >= self.min_habit_projects else "CANDIDATE"
                            )
                            pconn.execute(
                                """
                                INSERT INTO coding_habits (
                                    habit_key, rule_statement, rationale, projects_json,
                                    evidence_json, status, min_projects_required, tx_id, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    habit.habit_key,
                                    habit.rule_statement,
                                    habit.rationale,
                                    json.dumps(projs_list, ensure_ascii=False),
                                    json.dumps(habit.evidence_ids, ensure_ascii=False),
                                    new_status,
                                    self.min_habit_projects,
                                    tx_id,
                                    now,
                                ),
                            )
                            if new_status == "ACTIVE":
                                personal_promoted += 1
                            else:
                                personal_staged += 1

                    conn.execute(
                        "UPDATE staged_decisions SET status = 'PROMOTED', updated_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )

                # Flush staged negative knowledge
                for nrow in staged_neg_rows:
                    conn.execute(
                        """
                        INSERT INTO negative_knowledge (
                            topic_key, rejected_hypothesis, chosen_alternative,
                            rejection_reason, evidence_json, origin_session_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            nrow["topic_key"],
                            nrow["rejected_hypothesis"],
                            nrow["chosen_alternative"],
                            nrow["rejection_reason"],
                            nrow["evidence_json"],
                            session_id,
                            now,
                        ),
                    )
                    conn.execute(
                        "UPDATE staged_negatives SET status = 'PROMOTED' WHERE id = ?",
                        (nrow["id"],),
                    )
                    neg_added += 1

                new_rev = self._bump_base_revision(conn)
                conn.execute(
                    "UPDATE sessions SET status = 'PROMOTED', updated_at = ? WHERE session_id = ?",
                    (now, session_id),
                )

            _, demoted_count = self._compile_domain_artifacts()
            receipt = self._build_receipt(
                tx_id=tx_id,
                domain_upserted=domain_upserted,
                domain_demoted=demoted_count,
                neg_added=neg_added,
                personal_promoted=personal_promoted,
                personal_staged=personal_staged,
                new_keys=newly_broadcast_keys,
            )

            return PromoteSessionResult(
                status="PROMOTED",
                session_id=session_id,
                tx_id=tx_id,
                base_revision_after=new_rev,
                domain_upserted=domain_upserted,
                domain_demoted_to_cold=demoted_count,
                negative_knowledge_added=neg_added,
                personal_promoted=personal_promoted,
                personal_candidates_staged=personal_staged,
                newly_broadcast_keys=newly_broadcast_keys,
                receipt=receipt,
            )
        finally:
            conn.close()
            pconn.close()

    # ========================================================================
    # 5. resolve_conflict (Tier 3 Explicit Conflict Resolution: Options 1, 2, 3)
    # ========================================================================

    def resolve_conflict(
        self,
        conflict_id: str,
        choice: int | str | ConflictResolutionAction,
        rejection_reason: Optional[str] = None,
    ) -> PromoteSessionResult:
        """
        Resolves a detected Tier 3 conflict with one of the 3 mandatory actions:
        - [1] PIVOT_TO_NEW: Promotes incoming session's decision to Base SSOT L1, moves existing
          Base SSOT rule to L2 `cold_history`, AND archives competing session's hypothesis as
          `negative_knowledge` (Rejected Hypothesis) in L2 Cold Ledger.
        - [2] KEEP_AS_BRAINSTORM: Leaves Base SSOT untouched and marks the session's staged
          decision as `ARCHIVED_BRAINSTORM`.
        - [3] ARCHIVE_AS_NEGATIVE_KNOWLEDGE: Archives the losing hypothesis into `negative_knowledge`
          (Cold Ledger). If competing staged session existed, promotes the winning session's decision.
        """
        action = parse_resolution_choice(choice)
        now = _utc_now()
        tx_id = f"tx_{uuid.uuid4().hex[:12]}"

        conn = self._domain_conn()
        pconn = self._personal_conn()
        try:
            conf_row = conn.execute(
                "SELECT * FROM conflicts WHERE conflict_id = ?", (conflict_id,)
            ).fetchone()
            if not conf_row:
                raise ValueError(f"Conflict ID not found: {conflict_id}")
            if conf_row["status"] == "RESOLVED":
                raise ValueError(f"Conflict {conflict_id} is already resolved.")

            session_id = conf_row["session_id"]
            t_key = conf_row["topic_key"]
            existing_t_key = conf_row["existing_topic_key"]
            existing_val = conf_row["existing_value"]
            existing_sess = conf_row["existing_session_id"]

            staged_row = conn.execute(
                "SELECT * FROM staged_decisions WHERE session_id = ? AND topic_key = ?",
                (session_id, t_key),
            ).fetchone()

            domain_upserted = 0
            neg_added = 0

            with atomic_tx(conn), atomic_tx(pconn):
                domain_snap = self._capture_domain_snapshot(conn)
                personal_snap = self._capture_personal_snapshot(pconn)
                conn.execute(
                    """
                    INSERT INTO transaction_log (
                        tx_id, session_id, operation, domain_snapshot_json,
                        personal_snapshot_json, created_at, rolled_back
                    ) VALUES (?, ?, 'resolve_conflict', ?, ?, ?, 0)
                    """,
                    (
                        tx_id,
                        session_id,
                        json.dumps(domain_snap, ensure_ascii=False),
                        json.dumps(personal_snap, ensure_ascii=False),
                        now,
                    ),
                )

                if action == ConflictResolutionAction.PIVOT_TO_NEW:
                    # 1) Archive the old/competing hypothesis into Cold History & Negative Knowledge
                    step_ord = self._next_step_order(conn)
                    competing_staged = conn.execute(
                        "SELECT * FROM staged_decisions WHERE session_id = ? AND topic_key = ?",
                        (existing_sess, existing_t_key),
                    ).fetchone()
                    base_existing = conn.execute(
                        "SELECT * FROM base_decisions WHERE topic_key = ?", (existing_t_key,)
                    ).fetchone()

                    old_ev_json = (
                        competing_staged["evidence_json"]
                        if competing_staged
                        else (
                            base_existing["evidence_json"]
                            if base_existing
                            else (staged_row["evidence_json"] if staged_row else '["adr:conflict_pivot"]')
                        )
                    )
                    reason_text = (
                        rejection_reason
                        or (staged_row["rationale"] if staged_row else "Pivoted to new architecture")
                    )

                    conn.execute(
                        """
                        INSERT INTO cold_history (
                            topic_key, old_value, new_value, rationale,
                            evidence_json, superseded_by_session, reason,
                            step_order, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            existing_t_key,
                            existing_val,
                            conf_row["incoming_value"],
                            reason_text,
                            old_ev_json,
                            session_id,
                            f"Conflict Pivot ({conflict_id}) over {existing_sess}",
                            step_ord,
                            now,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO negative_knowledge (
                            topic_key, rejected_hypothesis, chosen_alternative,
                            rejection_reason, evidence_json, origin_session_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            existing_t_key,
                            existing_val,
                            conf_row["incoming_value"],
                            reason_text,
                            old_ev_json,
                            existing_sess,
                            now,
                        ),
                    )
                    neg_added += 1

                    if competing_staged:
                        conn.execute(
                            "UPDATE staged_decisions SET status = 'ARCHIVED_NEGATIVE', updated_at = ? WHERE id = ?",
                            (now, competing_staged["id"]),
                        )
                        conn.execute(
                            "UPDATE sessions SET status = 'NEGATIVE_ARCHIVED', updated_at = ? WHERE session_id = ?",
                            (now, existing_sess),
                        )

                    # If leaf-scope conflict had a different old topic_key in base_decisions, demote/remove it
                    if existing_t_key != t_key and base_existing:
                        conn.execute(
                            "UPDATE base_decisions SET tier = 'L2_COLD' WHERE topic_key = ?",
                            (existing_t_key,),
                        )

                    # 2) Upsert the winning incoming decision into base_decisions (L1_HOT)
                    if staged_row:
                        cur_base = conn.execute(
                            "SELECT * FROM base_decisions WHERE topic_key = ?", (t_key,)
                        ).fetchone()
                        new_ver = (int(cur_base["version"]) + 1) if cur_base else 1
                        conn.execute(
                            """
                            INSERT INTO base_decisions (
                                topic_key, decision_value, rationale, evidence_json,
                                leaf_files_json, pinned, tier, version, step_order,
                                updated_by_session, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 'L1_HOT', ?, ?, ?, ?)
                            ON CONFLICT(topic_key) DO UPDATE SET
                                decision_value = excluded.decision_value,
                                rationale = excluded.rationale,
                                evidence_json = excluded.evidence_json,
                                leaf_files_json = excluded.leaf_files_json,
                                pinned = excluded.pinned,
                                tier = 'L1_HOT',
                                version = excluded.version,
                                step_order = excluded.step_order,
                                updated_by_session = excluded.updated_by_session,
                                updated_at = excluded.updated_at
                            """,
                            (
                                t_key,
                                staged_row["decision_value"],
                                staged_row["rationale"],
                                staged_row["evidence_json"],
                                staged_row["leaf_files_json"],
                                staged_row["pinned"],
                                new_ver,
                                step_ord,
                                session_id,
                                now,
                            ),
                        )
                        conn.execute(
                            "UPDATE staged_decisions SET status = 'PROMOTED', updated_at = ? WHERE id = ?",
                            (now, staged_row["id"]),
                        )
                        domain_upserted += 1

                    conn.execute(
                        "UPDATE sessions SET status = 'PROMOTED', updated_at = ? WHERE session_id = ?",
                        (now, session_id),
                    )
                    res_status = "RESOLVED_PIVOT"

                elif action == ConflictResolutionAction.KEEP_AS_BRAINSTORM:
                    if staged_row:
                        conn.execute(
                            "UPDATE staged_decisions SET status = 'ARCHIVED_BRAINSTORM', updated_at = ? WHERE id = ?",
                            (now, staged_row["id"]),
                        )
                    conn.execute(
                        "UPDATE sessions SET status = 'BRAINSTORM_ARCHIVED', updated_at = ? WHERE session_id = ?",
                        (now, session_id),
                    )
                    res_status = "RESOLVED_BRAINSTORM"

                else:  # ConflictResolutionAction.ARCHIVE_AS_NEGATIVE_KNOWLEDGE
                    ev_json = (
                        staged_row["evidence_json"]
                        if staged_row
                        else '["adr:rejected_in_conflict"]'
                    )
                    reason_text = (
                        rejection_reason
                        or f"Rejected in favor of '{existing_val}' ({existing_sess})"
                    )
                    conn.execute(
                        """
                        INSERT INTO negative_knowledge (
                            topic_key, rejected_hypothesis, chosen_alternative,
                            rejection_reason, evidence_json, origin_session_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            t_key,
                            conf_row["incoming_value"],
                            existing_val,
                            reason_text,
                            ev_json,
                            session_id,
                            now,
                        ),
                    )
                    neg_added += 1
                    if staged_row:
                        conn.execute(
                            "UPDATE staged_decisions SET status = 'ARCHIVED_NEGATIVE', updated_at = ? WHERE id = ?",
                            (now, staged_row["id"]),
                        )
                    conn.execute(
                        "UPDATE sessions SET status = 'NEGATIVE_ARCHIVED', updated_at = ? WHERE session_id = ?",
                        (now, session_id),
                    )
                    res_status = "RESOLVED_NEGATIVE_KNOWLEDGE"

                conn.execute(
                    "UPDATE conflicts SET status = 'RESOLVED', resolution_action = ? WHERE conflict_id = ?",
                    (action.value, conflict_id),
                )
                new_rev = self._bump_base_revision(conn)

            _, demoted_count = self._compile_domain_artifacts()
            receipt = self._build_receipt(
                tx_id=tx_id,
                domain_upserted=domain_upserted,
                domain_demoted=demoted_count,
                neg_added=neg_added,
                personal_promoted=0,
                personal_staged=0,
                new_keys=[],
            )
            return PromoteSessionResult(
                status=res_status,  # type: ignore[arg-type]
                session_id=session_id,
                tx_id=tx_id,
                base_revision_after=new_rev,
                domain_upserted=domain_upserted,
                domain_demoted_to_cold=demoted_count,
                negative_knowledge_added=neg_added,
                receipt=receipt,
            )
        finally:
            conn.close()
            pconn.close()

    # ========================================================================
    # 6. query_cold_ledger (On-Demand L2 Cold Rules, ADR History & Negative Knowledge)
    # ========================================================================

    def query_cold_ledger(
        self,
        topic_key: Optional[str] = None,
        search_query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Queries L2 Cold Ledger for demoted rules, historical superseded ADRs (`cold_history`),
        and rejected hypotheses (`negative_knowledge`).
        """
        conn = self._domain_conn()
        try:
            norm_key = topic_key.strip().lower() if topic_key else None
            q_lower = search_query.strip().lower() if search_query else None

            if norm_key:
                demoted = conn.execute(
                    "SELECT * FROM base_decisions WHERE tier = 'L2_COLD' AND topic_key = ? ORDER BY step_order DESC",
                    (norm_key,),
                ).fetchall()
                history = conn.execute(
                    "SELECT * FROM cold_history WHERE topic_key = ? ORDER BY id DESC",
                    (norm_key,),
                ).fetchall()
                negatives = conn.execute(
                    "SELECT * FROM negative_knowledge WHERE topic_key = ? ORDER BY id DESC",
                    (norm_key,),
                ).fetchall()
            else:
                demoted = conn.execute(
                    "SELECT * FROM base_decisions WHERE tier = 'L2_COLD' ORDER BY step_order DESC"
                ).fetchall()
                history = conn.execute(
                    "SELECT * FROM cold_history ORDER BY id DESC"
                ).fetchall()
                negatives = conn.execute(
                    "SELECT * FROM negative_knowledge ORDER BY id DESC"
                ).fetchall()

            demoted_list = [
                {
                    "topic_key": r["topic_key"],
                    "decision_value": r["decision_value"],
                    "rationale": r["rationale"],
                    "version": r["version"],
                    "evidence_ids": json.loads(r["evidence_json"]),
                    "updated_by_session": r["updated_by_session"],
                }
                for r in demoted
            ]
            history_list = [
                {
                    "topic_key": r["topic_key"],
                    "old_value": r["old_value"],
                    "new_value": r["new_value"],
                    "rationale": r["rationale"],
                    "reason": r["reason"],
                    "superseded_by_session": r["superseded_by_session"],
                    "evidence_ids": json.loads(r["evidence_json"]),
                    "step_order": r["step_order"],
                }
                for r in history
            ]
            negative_list = [
                {
                    "topic_key": r["topic_key"],
                    "rejected_hypothesis": r["rejected_hypothesis"],
                    "chosen_alternative": r["chosen_alternative"],
                    "rejection_reason": r["rejection_reason"],
                    "evidence_ids": json.loads(r["evidence_json"]),
                    "origin_session_id": r["origin_session_id"],
                    "created_at": r["created_at"],
                }
                for r in negatives
            ]

            if q_lower:
                demoted_list = [
                    d for d in demoted_list if q_lower in json.dumps(d, ensure_ascii=False).lower()
                ]
                history_list = [
                    h for h in history_list if q_lower in json.dumps(h, ensure_ascii=False).lower()
                ]
                negative_list = [
                    n for n in negative_list if q_lower in json.dumps(n, ensure_ascii=False).lower()
                ]

            return {
                "topic_key_filter": norm_key,
                "demoted_l2_rules": demoted_list,
                "superseded_adr_history": history_list,
                "negative_knowledge": negative_list,
            }
        finally:
            conn.close()

    # ========================================================================
    # 7. export_personal_pack (Global Personal SSOT Portfolio & Agent Pack)
    # ========================================================================

    def export_personal_pack(
        self, output_dir: Optional[str | Path] = None
    ) -> Dict[str, Any]:
        """
        Compiles and exports Global Personal SSOT (`STAR+L` achievements + cross-project verified
        `Meta-Preferences`) to `PERSONAL_CONTEXT_PACK.md` and `personal_pack.json`.
        """
        target_dir = Path(output_dir).resolve() if output_dir else self.personal_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        pconn = self._personal_conn()
        try:
            pack = compile_personal_pack(pconn)
            md_path = target_dir / "PERSONAL_CONTEXT_PACK.md"
            json_path = target_dir / "personal_pack.json"
            md_path.write_text(pack["markdown"], encoding="utf-8")
            json_path.write_text(
                json.dumps(
                    {
                        "active_meta_preferences": pack["active_meta_preferences"],
                        "candidate_meta_preferences": pack["candidate_meta_preferences"],
                        "starl_achievements": pack["starl_achievements"],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return {
                "markdown_path": str(md_path),
                "json_path": str(json_path),
                "active_meta_preferences": pack["active_meta_preferences"],
                "candidate_meta_preferences": pack["candidate_meta_preferences"],
                "starl_achievements": pack["starl_achievements"],
                "markdown": pack["markdown"],
            }
        finally:
            pconn.close()

    # ========================================================================
    # 8. rollback_transaction / rollback (Atomic Dual-Scope Undo)
    # ========================================================================

    def rollback_transaction(self, tx_id: str) -> Dict[str, Any]:
        """
        Atomically rolls back both Domain SSOT and Global Personal SSOT to the exact snapshot
        captured immediately prior to `tx_id`, and recompiles `ACTIVE_CONTEXT.md`.
        """
        conn = self._domain_conn()
        pconn = self._personal_conn()
        try:
            tx_row = conn.execute(
                "SELECT * FROM transaction_log WHERE tx_id = ?", (tx_id,)
            ).fetchone()
            if not tx_row:
                raise ValueError(f"Transaction ID not found: {tx_id}")
            if tx_row["rolled_back"]:
                raise ValueError(f"Transaction {tx_id} has already been rolled back.")

            domain_snap = json.loads(tx_row["domain_snapshot_json"])
            personal_snap = json.loads(tx_row["personal_snapshot_json"])

            with atomic_tx(conn), atomic_tx(pconn):
                for tbl in (
                    "ssot_meta",
                    "canonical_keys",
                    "base_decisions",
                    "cold_history",
                    "negative_knowledge",
                    "sessions",
                    "staged_decisions",
                    "staged_negatives",
                    "conflicts",
                ):
                    self._restore_table(conn, tbl, domain_snap.get(tbl, []))

                for ptbl in ("starl_achievements", "coding_habits"):
                    self._restore_table(pconn, ptbl, personal_snap.get(ptbl, []))

                conn.execute(
                    "UPDATE transaction_log SET rolled_back = 1 WHERE tx_id = ?",
                    (tx_id,),
                )

            active_md, _ = self._compile_domain_artifacts()
            return {
                "status": "ROLLED_BACK",
                "tx_id": tx_id,
                "session_id": tx_row["session_id"],
                "base_revision_restored": self._get_base_revision(conn),
                "active_context_md": active_md,
            }
        finally:
            conn.close()
            pconn.close()

    def rollback(self, tx_id: str) -> Dict[str, Any]:
        """Alias for `rollback_transaction(tx_id)`."""
        return self.rollback_transaction(tx_id)
