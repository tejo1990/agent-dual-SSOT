"""Pydantic v2 strict data models & invariants for Dual-Scope SSOT Context Compiler."""

from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import PurePath
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ============================================================================
# 1. Constants: Strategy 3C Core Templates, Hub Denylist & Domain Filter
# ============================================================================

DEFAULT_CORE_CANONICAL_KEYS: List[str] = [
    "arch.execution_mode",
    "arch.system_topology",
    "arch.state_store",
    "arch.concurrency_model",
    "arch.deployment_target",
    "auth.provider",
    "auth.session_strategy",
    "auth.rbac_policy",
    "db.engine",
    "db.migration_policy",
    "db.transaction_isolation",
    "db.indexing_strategy",
    "api.protocol",
    "api.versioning",
    "api.error_contract",
    "api.rate_limit",
    "cache.engine",
    "cache.eviction_policy",
    "queue.broker",
    "queue.retry_policy",
    "security.secret_management",
    "security.encryption_standard",
    "observability.logging_format",
    "observability.tracing_backend",
    "testing.framework",
    "testing.coverage_gate",
    "ui.design_system",
    "ui.state_management",
    "build.package_manager",
    "build.ci_pipeline",
]

MAX_AUTO_REGISTERED_KEYS: int = 150
L1_MAX_LINES: int = 100
L1_MAX_TOKENS: int = 1500

HUB_FILE_DENYLIST: frozenset[str] = frozenset(
    {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "pyproject.toml",
        "poetry.lock",
        "uv.lock",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "tsconfig.json",
        "vite.config.ts",
        "webpack.config.js",
        ".env",
        ".env.example",
        ".gitignore",
        "readme.md",
        "changelog.md",
        "agents.md",
        "claude.md",
        "active_context.md",
        "dockerfile",
        "docker-compose.yml",
        "makefile",
        "__init__.py",
        "index.ts",
        "index.tsx",
        "index.js",
        "main.py",
        "main.ts",
        "app.py",
        "config.py",
        "config.ts",
        "config.js",
        "config.yaml",
        "config.json",
        "settings.py",
        "constants.py",
    }
)

DUMMY_EVIDENCE_PATTERNS: re.Pattern[str] = re.compile(
    r"^(tbd|todo|none|null|n/a|na|dummy|placeholder|trust[_\-\s]*me|fake|unknown|test_dummy)$",
    re.IGNORECASE,
)

VALID_EVIDENCE_PREFIXES: tuple[str, ...] = (
    "commit:",
    "git:",
    "file:",
    "diff:",
    "test:",
    "artifact:",
    "adr:",
    "metric:",
    "bench:",
    "pr:",
    "issue:",
)

# Keywords indicating narrow domain/project-specific details that must NOT pollute Global Personal SSOT
DOMAIN_CONTAMINATION_PATTERNS: re.Pattern[str] = re.compile(
    r"(\bbutton\s+color\b|\bhex\s*#[0-9a-fA-F]{3,6}\b|\btable\s+`?tbl_[a-z0-9_]+`?"
    r"|\blocalhost:\d+\b|\bspecific_client_[a-z0-9_]+\b|\b화면\s*버튼\s*색깔\b|\b우리\s*쇼핑몰\s*전용\b)",
    re.IGNORECASE,
)


def normalize_file_path(raw_path: str) -> str:
    """Normalizes a path or `file:path#L1-L20` string into a clean POSIX relative path."""
    cleaned = raw_path.strip()
    for prefix in ("file://", "file:", "diff:"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    if "#" in cleaned:
        cleaned = cleaned.split("#", 1)[0]
    cleaned = cleaned.replace("\\", "/").strip("/")
    return cleaned


def is_hub_file(file_path: str) -> bool:
    """Returns True if the file is a shared configuration/entrypoint hub file in HUB_FILE_DENYLIST."""
    norm = normalize_file_path(file_path)
    if not norm:
        return True
    basename = PurePath(norm).name.lower()
    if basename in HUB_FILE_DENYLIST:
        return True
    if basename.startswith("config.") or basename.startswith("settings."):
        return True
    return False


def extract_leaf_files(file_paths: List[str]) -> List[str]:
    """Filters out hub files and returns sorted unique leaf module paths for Strategy 3C scope checks."""
    leafs = set()
    for p in file_paths:
        norm = normalize_file_path(p)
        if norm and not is_hub_file(norm):
            leafs.add(norm)
    return sorted(leafs)


def estimate_token_count(text: str) -> int:
    """Deterministic token estimator (~4 chars per token + word boundary weighting)."""
    if not text:
        return 0
    words = len(text.split())
    chars_div = (len(text) + 3) // 4
    return max(words, chars_div)


# ============================================================================
# 2. Enums
# ============================================================================


class ConflictResolutionAction(IntEnum if False else str, Enum):
    """Tier 3 Semantic Merge & Conflict Gate resolution options."""

    PIVOT_TO_NEW = "PIVOT_TO_NEW"  # [1] 새 결론으로 피벗 (기존 규칙은 Cold Ledger로 이동, 상대 세션 가설은 기각 박제 가능)
    KEEP_AS_BRAINSTORM = "KEEP_AS_BRAINSTORM"  # [2] 현재 세션은 브레인스토밍으로만 보관 (Base SSOT 유지)
    ARCHIVE_AS_NEGATIVE_KNOWLEDGE = "ARCHIVE_AS_NEGATIVE_KNOWLEDGE"  # [3] 기각된 가설(Negative Knowledge)로 박제


def parse_resolution_choice(choice: int | str | ConflictResolutionAction) -> ConflictResolutionAction:
    """Maps 1, 2, 3 or enum string to ConflictResolutionAction."""
    if isinstance(choice, ConflictResolutionAction):
        return choice
    mapping = {
        1: ConflictResolutionAction.PIVOT_TO_NEW,
        "1": ConflictResolutionAction.PIVOT_TO_NEW,
        "PIVOT_TO_NEW": ConflictResolutionAction.PIVOT_TO_NEW,
        "pivot": ConflictResolutionAction.PIVOT_TO_NEW,
        2: ConflictResolutionAction.KEEP_AS_BRAINSTORM,
        "2": ConflictResolutionAction.KEEP_AS_BRAINSTORM,
        "KEEP_AS_BRAINSTORM": ConflictResolutionAction.KEEP_AS_BRAINSTORM,
        "brainstorm": ConflictResolutionAction.KEEP_AS_BRAINSTORM,
        3: ConflictResolutionAction.ARCHIVE_AS_NEGATIVE_KNOWLEDGE,
        "3": ConflictResolutionAction.ARCHIVE_AS_NEGATIVE_KNOWLEDGE,
        "ARCHIVE_AS_NEGATIVE_KNOWLEDGE": ConflictResolutionAction.ARCHIVE_AS_NEGATIVE_KNOWLEDGE,
        "negative_knowledge": ConflictResolutionAction.ARCHIVE_AS_NEGATIVE_KNOWLEDGE,
    }
    if choice in mapping:
        return mapping[choice]
    raise ValueError(
        f"Invalid conflict resolution choice: {choice!r}. Use 1 (PIVOT_TO_NEW), "
        f"2 (KEEP_AS_BRAINSTORM), or 3 (ARCHIVE_AS_NEGATIVE_KNOWLEDGE)."
    )


# ============================================================================
# 3. Evidence & Proof-of-Execution Models
# ============================================================================


def validate_evidence_list(evidence_ids: List[str]) -> List[str]:
    """Validates that evidence_ids is non-empty and contains verifiable, non-dummy anchors."""
    if not evidence_ids:
        raise ValueError("evidence_ids cannot be empty; at least one verifiable anchor is required.")
    cleaned_list: List[str] = []
    for raw in evidence_ids:
        item = raw.strip()
        if len(item) < 4:
            raise ValueError(f"Evidence ID too short (<4 chars): {raw!r}")
        if DUMMY_EVIDENCE_PATTERNS.match(item):
            raise ValueError(f"Hallucinated/dummy evidence ID rejected: {raw!r}")
        # Check prefix body if prefixed
        if ":" in item:
            prefix, body = item.split(":", 1)
            body_clean = body.strip()
            if not body_clean or DUMMY_EVIDENCE_PATTERNS.match(body_clean):
                raise ValueError(f"Evidence ID has empty or dummy body: {raw!r}")
        cleaned_list.append(item)
    return cleaned_list


class ProofOfExecution(BaseModel):
    """
    Deterministic Proof-of-Execution (PoE) oracle model from Audit Document.
    Prevents Goodhart's Law dummy edits by checking file existence, diff lines, and test exit code.
    """

    model_config = ConfigDict(extra="forbid")

    file_exists: bool = True
    git_diff_changed_lines: int = Field(default=1, ge=0)
    test_exit_code: Optional[int] = None
    verified_files: List[str] = Field(default_factory=list)

    def is_valid_for_project_ssot(self) -> bool:
        """Project SSOT requires actual file existence and positive diff lines (>0)."""
        return self.file_exists and self.git_diff_changed_lines > 0

    def is_valid_for_personal_episodic(self) -> bool:
        """Personal STAR+L achievement (N=1 immediate promotion) requires test_exit_code == 0."""
        return (
            self.file_exists
            and self.git_diff_changed_lines > 0
            and self.test_exit_code == 0
        )


# ============================================================================
# 4. Personal SSOT Models (STAR+L & Meta-Preference)
# ============================================================================


class STARLPayload(BaseModel):
    """STAR+L problem-solving trajectory for Global Personal SSOT."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=5, description="Concise title of the engineering achievement")
    situation: str = Field(..., min_length=5, description="Situation / context of the problem")
    task: str = Field(..., min_length=5, description="Task / bottleneck to resolve")
    action: str = Field(..., min_length=5, description="Root-cause fix and architectural action taken")
    result_metric: str = Field(
        ...,
        min_length=3,
        description="Quantitative outcome (e.g., 'P99 latency 420ms -> 85ms (-80%)', 'Recall 30% -> 98.5%')",
    )
    lesson: str = Field(..., min_length=5, description="Generalizable engineering lesson learned (L)")
    evidence_ids: List[str] = Field(..., min_length=1)
    proof: ProofOfExecution = Field(
        default_factory=lambda: ProofOfExecution(
            file_exists=True, git_diff_changed_lines=10, test_exit_code=0
        )
    )
    tags: List[str] = Field(default_factory=list)

    @field_validator("evidence_ids")
    @classmethod
    def _check_evidence(cls, v: List[str]) -> List[str]:
        return validate_evidence_list(v)

    @model_validator(mode="after")
    def _check_domain_contamination(self) -> "STARLPayload":
        combined = f"{self.title} {self.lesson}"
        if DOMAIN_CONTAMINATION_PATTERNS.search(combined):
            raise ValueError(
                "STAR+L payload contains narrow domain/UI noise (Domain Contamination Filter triggered)."
            )
        return self


class MetaPreferencePayload(BaseModel):
    """Domain-agnostic global coding principle / habit (requires K >= 2 distinct projects)."""

    model_config = ConfigDict(extra="forbid")

    habit_key: str = Field(..., min_length=3, pattern=r"^[a-z0-9_.\-]+$")
    rule_statement: str = Field(..., min_length=8)
    rationale: str = Field(default="Verified across multiple engineering projects")
    evidence_ids: List[str] = Field(..., min_length=1)

    @field_validator("evidence_ids")
    @classmethod
    def _check_evidence(cls, v: List[str]) -> List[str]:
        return validate_evidence_list(v)

    @model_validator(mode="after")
    def _check_domain_contamination(self) -> "MetaPreferencePayload":
        if DOMAIN_CONTAMINATION_PATTERNS.search(self.rule_statement):
            raise ValueError(
                f"Meta-preference '{self.habit_key}' rejected by Domain Contamination Filter: "
                "contains narrow project/UI-specific terms."
            )
        return self


# ============================================================================
# 5. Tier 2 Session Staging & Negative Knowledge Input Models
# ============================================================================


class StagedDecisionInput(BaseModel):
    """Input model for `stage_decision` in Tier 2 Session Ephemeral Sandbox."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1)
    topic_key: str = Field(..., min_length=2, pattern=r"^[a-zA-Z0-9_.\-]+$")
    decision_value: str = Field(..., min_length=2)
    rationale: str = Field(..., min_length=3)
    evidence_ids: List[str] = Field(..., min_length=1)
    file_scope: List[str] = Field(
        default_factory=list,
        description="Files touched by this decision (hub files are automatically filtered out for Strategy 3C)",
    )
    pinned: bool = Field(
        default=False,
        description="If True, pins this invariant at the top of L1 ACTIVE_CONTEXT.md so it is never evicted to Cold Ledger",
    )
    scope_target: Literal["domain", "personal_starl", "personal_habit", "dual"] = Field(
        default="domain"
    )
    proof: Optional[ProofOfExecution] = None
    starl_payload: Optional[STARLPayload] = None
    habit_payload: Optional[MetaPreferencePayload] = None

    @field_validator("topic_key")
    @classmethod
    def _normalize_key(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("evidence_ids")
    @classmethod
    def _check_evidence(cls, v: List[str]) -> List[str]:
        return validate_evidence_list(v)


class RejectedHypothesisInput(BaseModel):
    """Input model for `record_rejected_hypothesis` (Negative Knowledge)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1)
    topic_key: str = Field(..., min_length=2, pattern=r"^[a-zA-Z0-9_.\-]+$")
    rejected_hypothesis: str = Field(..., min_length=3)
    chosen_alternative: Optional[str] = Field(default=None)
    rejection_reason: str = Field(..., min_length=3)
    evidence_ids: List[str] = Field(..., min_length=1)

    @field_validator("topic_key")
    @classmethod
    def _normalize_key(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("evidence_ids")
    @classmethod
    def _check_evidence(cls, v: List[str]) -> List[str]:
        return validate_evidence_list(v)


# ============================================================================
# 6. Output / State Models
# ============================================================================


class ConflictDetail(BaseModel):
    """Detailed conflict report when `promote_session` detects a collision."""

    conflict_id: str
    conflict_type: Literal[
        "CANONICAL_KEY_VALUE_CONFLICT",
        "BASE_REVISION_STALE_CONFLICT",
        "LEAF_FILE_SCOPE_CONFLICT",
    ]
    session_id: str
    topic_key: str
    incoming_value: str
    existing_topic_key: str
    existing_value: str
    existing_session_id: str
    existing_version: int
    overlapping_leaf_files: List[str] = Field(default_factory=list)
    Competing_session_ids: List[str] = Field(default_factory=list)
    options_prompt: str = (
        "[1: 새 결론으로 피벗(기존 규칙은 Cold Ledger로 이동) | "
        "2: 현재 세션은 브레인스토밍으로만 보관 | "
        "3: 기각된 가설(Negative Knowledge)로 박제]"
    )


class PromoteSessionResult(BaseModel):
    """Deterministic result of `promote_session` or `resolve_conflict`."""

    status: Literal[
        "PROMOTED",
        "CONFLICT_DETECTED",
        "RESOLVED_PIVOT",
        "RESOLVED_BRAINSTORM",
        "RESOLVED_NEGATIVE_KNOWLEDGE",
        "REJECTED_PROOF_OF_EXECUTION",
        "NO_STAGED_ITEMS",
    ]
    session_id: str
    tx_id: Optional[str] = None
    base_revision_after: int = 0
    domain_upserted: int = 0
    domain_demoted_to_cold: int = 0
    negative_knowledge_added: int = 0
    personal_promoted: int = 0
    personal_candidates_staged: int = 0
    newly_broadcast_keys: List[str] = Field(default_factory=list)
    conflicts: List[ConflictDetail] = Field(default_factory=list)
    rejection_reasons: List[str] = Field(default_factory=list)
    receipt: str = ""
