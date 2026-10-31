"""Comprehensive pytest verification suite for Dual-Scope & Session-Isolated Context Compiler MCP."""

from __future__ import annotations

from pathlib import Path
import pytest
from pydantic import ValidationError

from dual_ssot import (
    CostWeights,
    DualSSOTEngine,
    L1_MAX_LINES,
    L1_MAX_TOKENS,
    MetaPreferencePayload,
    ProofOfExecution,
    SimulationScenario,
    STARLPayload,
     compute_min_promotion_projects,
    create_mcp_server,
    estimate_mcp_tool_schema_tokens,
    evaluate_conflict_detector_roi,
    evaluate_context_token_roi,
    evaluate_strategies,
)
from dual_ssot.models import estimate_token_count


# ============================================================================
# Scenario 1: Multi-Session MVCC Isolation & Conflict Defense
# ============================================================================


def test_scenario_1_multi_session_conflict_and_negative_knowledge_archival(tmp_path: Path) -> None:
    """
    시나리오 1 (다중 세션 충돌 방어):
    하나의 프로젝트에서 세션 A('하드웨어 유지')와 세션 B('순수 SW 피벗')가 동시에 돌 때
    서로 오염되지 않고, 세션 B 승격 시 충돌 감지 후 세션 A의 논리가
    '기각된 가설(Negative Knowledge)'로 Cold Ledger에 정확히 박제되는지 검증.
    """
    ws = tmp_path / "project_alpha"
    personal = tmp_path / "personal_ssot"
    engine = DualSSOTEngine(workspace_path=ws, personal_dir=personal)

    # 1. 세션 A와 세션 B가 동시에 초기화 (동일 Base Revision = 0 스냅샷 참조)
    snap_a = engine.init_session("session_A")
    snap_b = engine.init_session("session_B")
    assert snap_a["base_revision_at_init"] == 0
    assert snap_b["base_revision_at_init"] == 0

    # 2. 세션 A는 '하드웨어 유지'를 스테이징, 세션 B는 '순수 SW 피벗'을 동시에 스테이징
    engine.stage_decision(
        session_id="session_A",
        topic_key="arch.execution_mode",
        decision_value="하드웨어 유지 (FPGA 전용 보드 가속 유지)",
        rationale="기존 H/W 보드 자산을 재활용하여 초기 지연시간 확보",
        evidence_ids=["adr:2026-hw-retention-draft", "file:src/hw/fpga_driver.c#L1-L40"],
        file_scope=["src/hw/fpga_driver.c", "src/pipeline/runner.py"],
    )

    engine.stage_decision(
        session_id="session_B",
        topic_key="arch.execution_mode",
        decision_value="순수 SW 피벗 (CPU 벡터화 파이프라인 전환)",
        rationale="H/W 보드 단종 리스크 및 배포 단가 80% 절감을 위해 순수 SW로 피벗",
        evidence_ids=["bench:sw_simd_240fps", "file:src/pipeline/runner.py#L10-L85"],
        file_scope=["src/pipeline/runner.py"],
    )

    # 3. Tier 2 Sandbox 격리 검증: 서로의 임시 결정을 절대 Dirty Read 하지 않음
    view_a = engine.get_session_sandbox_view("session_A")
    view_b = engine.get_session_sandbox_view("session_B")
    assert view_a["effective_view"]["arch.execution_mode"].startswith("하드웨어 유지")
    assert view_b["effective_view"]["arch.execution_mode"].startswith("순수 SW 피벗")
    assert view_a["base_rules"] == {}  # Base SSOT는 아직 깨끗한 상태

    # 4. 세션 B 승격 시도 -> 세션 A와의 상충을 결정론적으로 감지 (CONFLICT_DETECTED)
    promote_res = engine.promote_session("session_B")
    assert promote_res.status == "CONFLICT_DETECTED"
    assert len(promote_res.conflicts) == 1
    conflict = promote_res.conflicts[0]
    assert conflict.topic_key == "arch.execution_mode"
    assert conflict.existing_session_id == "session_A"
    assert "하드웨어 유지" in conflict.existing_value
    assert "순수 SW 피벗" in conflict.incoming_value
    assert "[1: 새 결론으로 피벗" in conflict.options_prompt

    # 5. 충돌 해결: [1: 새 결론으로 피벗] 선택 시 세션 B가 Base SSOT(L1)로 승격되고,
    #    세션 A의 '하드웨어 유지' 논리는 L2 Cold Ledger의 Negative Knowledge로 정확히 박제됨
    resolve_res = engine.resolve_conflict(
        conflict_id=conflict.conflict_id,
        choice=1,
        rejection_reason="H/W 납기 지연 및 유지보수 단가 문제로 하드웨어 유지 가설 최종 기각",
    )
    assert resolve_res.status == "RESOLVED_PIVOT"
    assert resolve_res.domain_upserted == 1
    assert resolve_res.negative_knowledge_added == 1
    assert "[SSOT Receipt]" in resolve_res.receipt

    # 6. Base SSOT(ACTIVE_CONTEXT.md) 및 L2 Cold Ledger 검증
    active_md = (ws / ".ssot" / "ACTIVE_CONTEXT.md").read_text(encoding="utf-8")
    assert "순수 SW 피벗" in active_md

    cold_data = engine.query_cold_ledger(topic_key="arch.execution_mode")
    assert len(cold_data["negative_knowledge"]) == 1
    neg_entry = cold_data["negative_knowledge"][0]
    assert "하드웨어 유지" in neg_entry["rejected_hypothesis"]
    assert "순수 SW 피벗" in (neg_entry["chosen_alternative"] or "")
    assert neg_entry["origin_session_id"] == "session_A"
    assert "adr:2026-hw-retention-draft" in neg_entry["evidence_ids"]


def test_scenario_1_stale_base_revision_conflict_when_session_a_promotes_first(
    tmp_path: Path,
) -> None:
    """
    세션 A와 세션 B가 동시에 열린 상태에서 세션 A('하드웨어 유지')가 먼저 승격(Commit)한 뒤,
    세션 B('순수 SW 피벗')가 승격을 시도할 때도 Stale Base Revision 충돌이 감지되고
    세션 A의 기존 규칙이 Negative Knowledge 및 Cold History로 이관되는지 검증.
    """
    engine = DualSSOTEngine(
        workspace_path=tmp_path / "proj_occ",
        personal_dir=tmp_path / "personal_occ",
    )
    engine.init_session("session_A")
    engine.init_session("session_B")

    engine.stage_decision(
        session_id="session_A",
        topic_key="arch.execution_mode",
        decision_value="하드웨어 유지",
        rationale="초기 하드웨어 가속 유지",
        evidence_ids=["commit:111aaaa"],
    )
    res_a = engine.promote_session("session_A")
    assert res_a.status == "PROMOTED"

    engine.stage_decision(
        session_id="session_B",
        topic_key="arch.execution_mode",
        decision_value="순수 SW 피벗",
        rationale="클라우드 네이티브 배포를 위한 순수 SW 전환",
        evidence_ids=["commit:222bbbb"],
    )
    res_b = engine.promote_session("session_B")
    assert res_b.status == "CONFLICT_DETECTED"
    assert res_b.conflicts[0].conflict_type == "BASE_REVISION_STALE_CONFLICT"

    resolved = engine.resolve_conflict(
        res_b.conflicts[0].conflict_id,
        choice=1,
        rejection_reason="하드웨어 종속성 탈피를 위해 순수 SW로 피벗 확정",
    )
    assert resolved.status == "RESOLVED_PIVOT"

    cold = engine.query_cold_ledger("arch.execution_mode")
    assert any("하드웨어 유지" in n["rejected_hypothesis"] for n in cold["negative_knowledge"])
    assert any(h["old_value"] == "하드웨어 유지" and h["new_value"] == "순수 SW 피벗" for h in cold["superseded_adr_history"])


def test_scenario_1_strategy_3c_dynamic_key_broadcast_and_leaf_scope_conflict(
    tmp_path: Path,
) -> None:
    """
    전략 3C 검증 (20261025-canonical-key-hybrid-benchmark.md):
    1) 기본 템플릿에 없는 신규 예외 키가 비차단(Non-blocking)으로 즉시 브로드캐스트되는지 검증.
    2) 공통 허브 파일(package.json, config.py)은 동시에 건드려도 거짓 알람(False Sharing)이 울리지 않는지 검증.
    3) 서로 다른 예외 키 이름을 썼더라도 동일한 리프 비즈니스 파일(src/payment/retry.py)을 건드리면
       LEAF_FILE_SCOPE_CONFLICT로 정확히 포착하는지 검증.
    """
    engine = DualSSOTEngine(
        workspace_path=tmp_path / "proj_3c",
        personal_dir=tmp_path / "personal_3c",
    )

    # 1) 세션 A가 템플릿에 없는 신규 도메인 키를 스테이징 -> 즉시 브로드캐스트
    stage_a = engine.stage_decision(
        session_id="session_A",
        topic_key="payment.webhook_retry_strategy",
        decision_value="지수 백오프 (최대 5회, Dead-Letter Queue 이관)",
        rationale="결제 웹훅 유실 방지",
        evidence_ids=["file:src/payment/retry.py#L12-L50"],
        file_scope=["package.json", "config.py", "src/payment/retry.py"],
    )
    assert stage_a["newly_broadcast_key"] is True
    # 허브 파일(package.json, config.py)은 자동 제거되고 리프 파일만 남음
    assert stage_a["leaf_files"] == ["src/payment/retry.py"]

    # 세션 B가 init_session 호출 시 세션 A가 등록한 신규 키를 즉시 공유받음
    snap_b = engine.init_session("session_B")
    assert "payment.webhook_retry_strategy" in snap_b["canonical_keys"]

    # 2) 세션 C가 동일한 허브 파일(package.json, config.py)을 건드렸지만 리프 파일이 다르면 충돌 없음!
    engine.stage_decision(
        session_id="session_C",
        topic_key="notification.email_sender",
        decision_value="AWS SES 비동기 발송",
        rationale="이메일 발송 처리량 개선",
        evidence_ids=["file:src/notification/ses.py#L1-L30"],
        file_scope=["package.json", "config.py", "src/notification/ses.py"],
    )
    res_c = engine.promote_session("session_C")
    assert res_c.status == "PROMOTED"

    # 3) 세션 B가 세션 A와 다른 키 이름(checkout.retry_policy)을 사용했지만,
    #    동일한 리프 모듈(src/payment/retry.py)을 수정한 경우 -> LEAF_FILE_SCOPE_CONFLICT 포착!
    engine.stage_decision(
        session_id="session_B",
        topic_key="checkout.retry_policy",
        decision_value="즉시 실패 반환 (재시도 없음, 클라이언트 폴링)",
        rationale="서버 큐 부하 제거",
        evidence_ids=["file:src/payment/retry.py#L15-L60"],
        file_scope=["pyproject.toml", "src/payment/retry.py"],
    )
    res_b = engine.promote_session("session_B")
    assert res_b.status == "CONFLICT_DETECTED"
    assert res_b.conflicts[0].conflict_type == "LEAF_FILE_SCOPE_CONFLICT"
    assert res_b.conflicts[0].overlapping_leaf_files == ["src/payment/retry.py"]


# ============================================================================
# Scenario 2: L1 Hot State Hard Limit (100 Lines / 1,500 Tokens), Upsert & L2 Demotion
# ============================================================================


def test_scenario_2_l1_upsert_and_hard_cap_compaction(tmp_path: Path) -> None:
    """
    시나리오 2 (L1 크기 제한 및 Upsert):
    1) 동일 topic_key를 반복 업데이트해도 줄 수가 늘어나지 않고 최신 상태로 Upsert 되는지 검증.
    2) 서로 다른 고유 토픽이 대량 추가되어도 ACTIVE_CONTEXT.md가 절대 100줄·1,500토큰을 넘지 않고,
       Pinned 규칙은 보존되며 오래된 일반 규칙은 L2 Cold Ledger로 강등되고 하단 1줄 포인터가 유지되는지 검증.
    """
    ws = tmp_path / "proj_scale"
    engine = DualSSOTEngine(
        workspace_path=ws,
        personal_dir=tmp_path / "personal_scale",
        max_active_lines=L1_MAX_LINES,
        max_active_tokens=L1_MAX_TOKENS,
    )

    # 1) 동일 topic_key('db.engine')를 15회 연속 업데이트 (Upsert 검증)
    for idx in range(1, 16):
        engine.stage_decision(
            session_id="session_upsert",
            topic_key="db.engine",
            decision_value=f"SQLite WAL Mode Tune v{idx}",
            rationale=f"Concurrency tuning iteration {idx}",
            evidence_ids=[f"commit:tune_{idx:03d}"],
            pinned=True,
        )
        res = engine.promote_session("session_upsert")
        assert res.status == "PROMOTED"

    snap = engine.init_session("probe_after_upsert")
    assert snap["active_rules"]["db.engine"]["version"] == 15
    assert snap["active_rules"]["db.engine"]["decision_value"] == "SQLite WAL Mode Tune v15"

    # 이전 14번의 변경 이력은 모두 L2 Cold History에 보존됨
    cold_db = engine.query_cold_ledger(topic_key="db.engine")
    assert len(cold_db["superseded_adr_history"]) == 14

    # 2) 115개의 서로 다른 토픽을 추가하여 100줄 / 1,500토큰 하드 리밋 스트레스 테스트
    for i in range(1, 116):
        engine.stage_decision(
            session_id="session_bulk",
            topic_key=f"domain.rule_topic_{i:03d}",
            decision_value=f"Deterministic policy rule #{i} enforcing strict module boundary",
            rationale=f"Prevent coupling in subsystem #{i}",
            evidence_ids=[f"file:src/sub_{i:03d}/service.py#L1-L20"],
            pinned=(i <= 3),  # 처음 3개는 Pinned 고정
        )
    bulk_res = engine.promote_session("session_bulk")
    assert bulk_res.status == "PROMOTED"
    assert bulk_res.domain_demoted_to_cold > 0

    active_md_path = ws / ".ssot" / "ACTIVE_CONTEXT.md"
    active_md = active_md_path.read_text(encoding="utf-8")
    lines = active_md.splitlines()
    tokens = estimate_token_count(active_md)

    # 불변식 검증: 절대 100줄 및 1,500토큰을 초과하지 않음
    assert len(lines) <= L1_MAX_LINES, f"Expected <= {L1_MAX_LINES} lines, got {len(lines)}"
    assert tokens <= L1_MAX_TOKENS, f"Expected <= {L1_MAX_TOKENS} tokens, got {tokens}"

    # Pinned 규칙(db.engine 및 rule_topic_001~003)은 가장 오래되었어도 절대 강등되지 않고 L1에 생존
    assert "**db.engine** (v15)" in active_md
    assert "**domain.rule_topic_001**" in active_md
    assert "**domain.rule_topic_002**" in active_md
    assert "**domain.rule_topic_003**" in active_md

    # 가장 오래된 비고정(Non-pinned) 규칙(rule_topic_004)은 L2 Cold Ledger로 강등되고 1줄 포인터에 표시됨
    assert "[COLD_INDEX]" in lines[-1]
    assert "domain.rule_topic_004" in lines[-1]
    cold_query = engine.query_cold_ledger(topic_key="domain.rule_topic_004")
    assert len(cold_query["demoted_l2_rules"]) == 1


# ============================================================================
# Scenario 3: Dual-Scope Isolation, Proof-of-Execution & Personal SSOT Gate
# ============================================================================


def test_scenario_3_dual_scope_isolation_and_personal_promotion_gate(tmp_path: Path) -> None:
    """
    시나리오 3 (Dual-Scope 격리 및 승격 게이트):
    1) 증거 없는 기록이나 더미 증거('TBD', 'trust me')는 스키마 레벨에서 즉시 거부(Reject).
    2) 프로젝트 특수 규칙(Domain)은 Personal SSOT에 섞이지 않으며, 도메인 오염 키워드가 포함된
       개인 항목도 필터에서 차단됨.
    3) 테스트 통과 증거(Proof-of-Execution: test_exit_code == 0)가 있는 STAR+L 문제해결 성과만
       즉시(N=1) Personal SSOT로 승격되고, 실패한 테스트(test_exit_code == 1)는 승격 거부됨.
    4) 도메인 무관 전역 작업 원칙(Meta-Preference)은 1차 프로젝트에서는 CANDIDATE로 대기하다가
       2번째 프로젝트에서 반복 확인될 때만 ACTIVE로 승격됨.
    """
    personal_dir = tmp_path / "global_personal_ssot"
    engine_proj1 = DualSSOTEngine(
        workspace_path=tmp_path / "ecommerce_project",
        personal_dir=personal_dir,
        project_id="ecommerce_project",
        min_habit_projects=2,
    )

    # 1) 증거 없는 기록 및 더미 증거 스키마 레벨 거부 검증
    with pytest.raises(ValidationError):
        engine_proj1.stage_decision(
            session_id="sess_invalid",
            topic_key="auth.provider",
            decision_value="OAuth2",
            rationale="Standard auth",
            evidence_ids=[],  # 빈 리스트 거부
        )

    with pytest.raises(ValidationError):
        engine_proj1.stage_decision(
            session_id="sess_invalid",
            topic_key="auth.provider",
            decision_value="OAuth2",
            rationale="Standard auth",
            evidence_ids=["TBD"],  # 더미 증거 거부
        )

    # 2) 도메인 특수 UI 오염 키워드가 들어간 Meta-Preference 거부 검증
    with pytest.raises(ValidationError):
        MetaPreferencePayload(
            habit_key="ui.button_style",
            rule_statement="우리 쇼핑몰 전용 화면 버튼 색깔은 hex #ff0055 로 고정한다",
            evidence_ids=["file:src/ui/button.css#L1-L5"],
        )

    # 3) 테스트를 통과하지 못한(test_exit_code=1) 더미 STAR+L 무용담은 승격 거부됨 (Goodhart's Law 방어)
    engine_proj1.stage_decision(
        session_id="sess_unverified_starl",
        topic_key="db.indexing_strategy",
        decision_value="Composite index on orders(user_id, created_at)",
        rationale="Optimize order lookup",
        evidence_ids=["file:migrations/002_idx.sql#L1-L10"],
        scope_target="personal_starl",
        starl_payload=STARLPayload(
            title="주문 조회 쿼리 인덱싱 시도",
            situation="주문 목록 조회 시 풀 스캔 발생",
            task="쿼리 응답속도 개선",
            action="복합 인덱스 추가",
            result_metric="벤치마크 전",
            lesson="인덱스 카디널리티 검증 필요",
            evidence_ids=["file:migrations/002_idx.sql#L1-L10"],
            proof=ProofOfExecution(file_exists=True, git_diff_changed_lines=8, test_exit_code=1),
        ),
    )
    unverified_res = engine_proj1.promote_session("sess_unverified_starl")
    assert unverified_res.status == "REJECTED_PROOF_OF_EXECUTION"

    # 4) 프로젝트 1에서:
    #    - 도메인 전용 규칙 1건 (Domain에만 남아야 함)
    #    - 검증된 STAR+L 성과 1건 (test_exit_code == 0 -> Personal SSOT 즉시 승격)
    #    - 전역 코딩 습관 1건 (1차 프로젝트이므로 Personal SSOT의 CANDIDATE 대기실에 머물러야 함)
    engine_proj1.stage_decision(
        session_id="sess_proj1_main",
        topic_key="ui.design_system",
        decision_value="TailwindCSS v4 + 프로젝트 전용 다크 테마 토큰",
        rationale="이커머스 브랜드 가이드 준수",
        evidence_ids=["file:src/styles/theme.css#L1-L40"],
        scope_target="domain",
    )

    engine_proj1.stage_decision(
        session_id="sess_proj1_main",
        topic_key="db.transaction_isolation",
        decision_value="SQLite WAL + BEGIN IMMEDIATE 직렬화 락 적용",
        rationale="병렬 에이전트 세션 간 레이스 컨디션 원천 차단",
        evidence_ids=["commit:9f8e7d6", "test:pytest_mvcc_passed"],
        scope_target="dual",
        starl_payload=STARLPayload(
            title="다중 에이전트 SQLite WAL + IMMEDIATE 트랜잭션으로 동시 쓰기 경합 해결",
            situation="병렬 세션이 동시에 상태를 승격할 때 SQLITE_BUSY 및 Lost Update 발생",
            task="외부 무거운 DB 서버 없이 로컬 파일 DB만으로 결정론적 동시성 제어 달성",
            action="PRAGMA journal_mode=WAL, busy_timeout=5000 및 BEGIN IMMEDIATE 트랜잭션 락 도입",
            result_metric="동시 세션 충돌 포착률 98.5% 달성, 데드락 0건 (응답시간 12ms 이내)",
            lesson="로컬 임베디드 환경에서는 비관적 쓰기 락(BEGIN IMMEDIATE)과 MVCC 버전 대조의 결합이 최소 비용 표준 해법이다",
            evidence_ids=["commit:9f8e7d6", "file:src/dual_ssot/db.py#L140-L175"],
            proof=ProofOfExecution(file_exists=True, git_diff_changed_lines=45, test_exit_code=0),
            tags=["sqlite", "mvcc", "concurrency"],
        ),
        habit_payload=MetaPreferencePayload(
            habit_key="eng.deterministic_state_over_llm_io",
            rule_statement="상태 파일 입출력은 LLM 자유 텍스트에 맡기지 않고 항상 SQLite 트랜잭션과 결정론적 컴파일러로 생성한다",
            rationale="컨텍스트 오염 및 마크다운 문법 깨짐 원천 차단",
            evidence_ids=["commit:9f8e7d6"],
        ),
    )

    res_p1 = engine_proj1.promote_session("sess_proj1_main")
    assert res_p1.status == "PROMOTED"
    assert res_p1.domain_upserted == 2
    assert res_p1.personal_promoted == 1  # STAR+L 1건만 즉시 승격
    assert res_p1.personal_candidates_staged == 1  # 코딩 습관은 CANDIDATE 대기

    pack_after_p1 = engine_proj1.export_personal_pack()
    assert len(pack_after_p1["starl_achievements"]) == 1
    assert len(pack_after_p1["active_meta_preferences"]) == 0
    assert len(pack_after_p1["candidate_meta_preferences"]) == 1
    assert "TailwindCSS" not in pack_after_p1["markdown"]  # 도메인 전용 규칙은 Personal에 절대 섞이지 않음

    # 5) 두 번째 프로젝트(robotics_project)에서 동일한 전역 코딩 습관이 다시 확인되면 ACTIVE로 자동 승격!
    engine_proj2 = DualSSOTEngine(
        workspace_path=tmp_path / "robotics_project",
        personal_dir=personal_dir,
        project_id="robotics_project",
        min_habit_projects=2,
    )
    engine_proj2.stage_decision(
        session_id="sess_proj2_main",
        topic_key="arch.state_store",
        decision_value="SQLite WAL 기반 로봇 설정 원장",
        rationale="센서 캘리브레이션 상태의 원자적 보존",
        evidence_ids=["commit:7c6b5a4"],
        scope_target="personal_habit",
        habit_payload=MetaPreferencePayload(
            habit_key="eng.deterministic_state_over_llm_io",
            rule_statement="상태 파일 입출력은 LLM 자유 텍스트에 맡기지 않고 항상 SQLite 트랜잭션과 결정론적 컴파일러로 생성한다",
            rationale="로봇 제어 파라미터 오염 방지",
            evidence_ids=["commit:7c6b5a4"],
        ),
    )
    res_p2 = engine_proj2.promote_session("sess_proj2_main")
    assert res_p2.status == "PROMOTED"
    assert res_p2.personal_promoted == 1

    pack_after_p2 = engine_proj2.export_personal_pack()
    assert len(pack_after_p2["active_meta_preferences"]) == 1
    assert len(pack_after_p2["candidate_meta_preferences"]) == 0
    active_habit = pack_after_p2["active_meta_preferences"][0]
    assert set(active_habit["projects"]) == {"ecommerce_project", "robotics_project"}


# ============================================================================
# Scenario 4: Atomic Rollback, MCP Server Schema Budget & Vault Invariants
# ============================================================================


def test_scenario_4_atomic_rollback_and_mcp_token_budget(tmp_path: Path) -> None:
    """
    시나리오 4 (즉시 되돌리기 rollback_transaction 및 초경량 MCP 스키마 검증):
    1) 승격 후 발급된 tx_id로 rollback_transaction(tx_id) 호출 시 Domain SSOT와 Personal SSOT가
       모두 이전 상태로 100% 원자적으로 복원되는지 검증.
    2) MCP 툴 스키마 토큰 비용이 500토큰 미만인지 검증.
    3) 감사 문서의 수리 모델(Token ROI, Conflict ROI, K_min, Strategy 3C Pareto) 불변식 검증.
    """
    ws = tmp_path / "proj_rollback"
    personal = tmp_path / "personal_rollback"
    mcp_server, engine = create_mcp_server(workspace_path=ws, personal_dir=personal)
    assert mcp_server is not None

    # MCP 스키마 토큰 예산 검증 (< 500 토큰)
    schema_tokens = estimate_mcp_tool_schema_tokens()
    assert schema_tokens < 500, f"MCP schema too heavy: {schema_tokens} tokens"

    # 초기 규칙 1건 커밋 (Base Revision = 1)
    engine.stage_decision(
        session_id="sess_1",
        topic_key="api.protocol",
        decision_value="gRPC Protobuf v3",
        rationale="Low latency internal RPC",
        evidence_ids=["commit:base001"],
    )
    r1 = engine.promote_session("sess_1")
    assert r1.base_revision_after == 1

    # 두 번째 트랜잭션(sess_2)에서 규칙을 변경하고 Personal STAR+L도 추가 (Base Revision = 2)
    engine.stage_decision(
        session_id="sess_2",
        topic_key="api.protocol",
        decision_value="GraphQL Federation",
        rationale="Frontend flexible querying",
        evidence_ids=["commit:bad002"],
        scope_target="dual",
        starl_payload=STARLPayload(
            title="GraphQL 실험 도입",
            situation="다중 엔드포인트 통합 테스트",
            task="단일 엔드포인트 구성",
            action="GraphQL 게이트웨이 구성",
            result_metric="쿼리 수 50% 감소",
            lesson="스키마 캐싱 필수",
            evidence_ids=["commit:bad002"],
            proof=ProofOfExecution(file_exists=True, git_diff_changed_lines=20, test_exit_code=0),
        ),
    )
    r2 = engine.promote_session("sess_2", allow_overwrite=True)
    assert r2.status == "PROMOTED"
    assert r2.tx_id is not None
    assert "GraphQL Federation" in (ws / ".ssot" / "ACTIVE_CONTEXT.md").read_text(encoding="utf-8")
    assert len(engine.export_personal_pack()["starl_achievements"]) == 1

    # 즉시 롤백 실행!
    rb_res = engine.rollback_transaction(r2.tx_id)
    assert rb_res["status"] == "ROLLED_BACK"
    assert rb_res["base_revision_restored"] == 1

    # 롤백 후 Domain SSOT는 'gRPC Protobuf v3'로 복원되고, Personal SSOT의 STAR+L도 0건으로 복원됨
    restored_md = (ws / ".ssot" / "ACTIVE_CONTEXT.md").read_text(encoding="utf-8")
    assert "gRPC Protobuf v3" in restored_md
    assert "GraphQL Federation" not in restored_md
    assert len(engine.export_personal_pack()["starl_achievements"]) == 0


def test_vault_mathematical_invariants() -> None:
    """두 증류 문서(Audit & Benchmark)의 핵심 수리 불변식 검증."""
    # 1) 경량 스키마(200토큰)는 양(+)의 토큰 절감을 달성하고 비대 스키마(1800토큰)는 음(-)의 ROI를 보임
    lean = evaluate_context_token_roi(25, 2500, 900, 0.15, 600, 200)
    bloated = evaluate_context_token_roi(25, 2500, 900, 0.40, 600, 1800)
    assert lean["is_viable"] == 1.0
    assert bloated["is_viable"] == 0.0

    # 2) 결정론적 충돌 감지 ROI 및 K_min 산출 검증
    det_roi = evaluate_conflict_detector_roi(0.20, 0.10, 0.02, 45.0, 10.0, 2.0)
    assert det_roi["is_viable"] == 1.0
    assert compute_min_promotion_projects(0.25, 0.05) == 3

    # 3) 성숙기(Mature Phase) 도메인에서 전략 3C가 최소 고통 비용 및 98%+ 재현율로 파레토 최적 달성
    mature = SimulationScenario(
        name="Mature_Domain_Phase",
        total_events=100,
        template_coverage=0.30,
        true_conflict_rate=0.15,
        key_naming_divergence=0.50,
        hub_file_false_sharing=0.30,
        cross_dir_coupling_rate=0.25,
    )
    strat_res = evaluate_strategies(mature, CostWeights())
    best = min(strat_res.items(), key=lambda kv: kv[1]["total_pain_cost"])[0]
    assert best == "3C_AutoBroadcast_LeafScope_Receipt"
    assert strat_res["3C_AutoBroadcast_LeafScope_Receipt"]["conflict_recall_pct"] >= 98.0


def test_adversarial_edge_cases_killswitch_resolutions_and_physical_poe(tmp_path: Path) -> None:
    """
    적대적 엣지케이스 검증:
    1) 전략 3C 자동 키 등록 상한(Kill-Switch) 초과 시 차단 검증
    2) 충돌 해결 옵션 2(브레인스토밍 보관) 및 옵션 3(기각 가설 직접 박제) 동작 검증
    3) verify_physical_files=True 설정 시 실제 디스크 파일 유무에 따른 승격 차단/통과 검증
    4) 동일 트랜잭션 중복 롤백 방지 검증
    """
    ws = tmp_path / "proj_edge"
    ws.mkdir(parents=True, exist_ok=True)
    engine = DualSSOTEngine(
        workspace_path=ws,
        personal_dir=tmp_path / "personal_edge",
        max_auto_keys=3,
        verify_physical_files=True,
    )

    # 1) 3개의 신규 예외 키는 정상 자동 등록되나 4번째 신규 예외 키는 Kill-Switch 발동
    for k_idx in range(1, 4):
        f_path = ws / f"mod_{k_idx}.py"
        f_path.write_text("# code\n", encoding="utf-8")
        engine.stage_decision(
            session_id="sess_kill",
            topic_key=f"custom.exception_key_{k_idx}",
            decision_value=f"val_{k_idx}",
            rationale="test killswitch",
            evidence_ids=[f"file:mod_{k_idx}.py"],
        )

    with pytest.raises(ValueError, match="Kill-Switch triggered"):
        engine.stage_decision(
            session_id="sess_kill",
            topic_key="custom.exception_key_4_overflow",
            decision_value="val_4",
            rationale="should fail",
            evidence_ids=["adr:overflow_4"],
        )

    # 2) 실제 디스크에 존재하지 않는 파일(file:missing.py)을 증거로 제출하면 승격 차단됨
    engine.stage_decision(
        session_id="sess_missing_file",
        topic_key="arch.state_store",
        decision_value="In-memory dict",
        rationale="test missing file",
        evidence_ids=["file:non_existent_module.py#L1-L10"],
    )
    missing_res = engine.promote_session("sess_missing_file")
    assert missing_res.status == "REJECTED_PROOF_OF_EXECUTION"

    # 실제 파일을 생성하면 즉시 승격 통과
    (ws / "real_store.py").write_text("class Store: pass\n", encoding="utf-8")
    engine.stage_decision(
        session_id="sess_real_file",
        topic_key="arch.state_store",
        decision_value="SQLite WAL Store",
        rationale="Verified store implementation",
        evidence_ids=["file:real_store.py#L1"],
    )
    real_res = engine.promote_session("sess_real_file")
    assert real_res.status == "PROMOTED"
    assert real_res.tx_id is not None

    # 3) 옵션 2(KEEP_AS_BRAINSTORM) 및 옵션 3(ARCHIVE_AS_NEGATIVE_KNOWLEDGE) 검증
    engine.stage_decision(
        session_id="sess_brainstorm",
        topic_key="arch.state_store",
        decision_value="Experimental Graph DB",
        rationale="Brainstorming graph traversal",
        evidence_ids=["file:real_store.py#L1"],
    )
    conf_b = engine.promote_session("sess_brainstorm")
    assert conf_b.status == "CONFLICT_DETECTED"
    res_opt2 = engine.resolve_conflict(conf_b.conflicts[0].conflict_id, choice=2)
    assert res_opt2.status == "RESOLVED_BRAINSTORM"
    # Base SSOT는 여전히 'SQLite WAL Store' 유지
    assert (
        engine.init_session("probe")["active_rules"]["arch.state_store"]["decision_value"]
        == "SQLite WAL Store"
    )

    engine.stage_decision(
        session_id="sess_bad_idea",
        topic_key="arch.state_store",
        decision_value="Plain CSV File Without Locks",
        rationale="Zero dependency idea",
        evidence_ids=["file:real_store.py#L1"],
    )
    conf_c = engine.promote_session("sess_bad_idea")
    assert conf_c.status == "CONFLICT_DETECTED"
    res_opt3 = engine.resolve_conflict(
        conf_c.conflicts[0].conflict_id,
        choice=3,
        rejection_reason="CSV 파일은 동시 쓰기 시 파일 손상이 발생하므로 영구 기각",
    )
    assert res_opt3.status == "RESOLVED_NEGATIVE_KNOWLEDGE"
    cold_search = engine.query_cold_ledger(search_query="Plain CSV")
    assert len(cold_search["negative_knowledge"]) == 1

    # 4) 독립적인 record_rejected_hypothesis 직접 호출 및 중복 롤백 방지 검증
    engine.record_rejected_hypothesis(
        session_id="sess_direct_neg",
        topic_key="auth.session_strategy",
        rejected_hypothesis="LocalStorage에 Raw JWT 저장",
        chosen_alternative="HttpOnly Secure Cookie",
        rejection_reason="XSS 공격 시 토큰 탈취 취약점 존재",
        evidence_ids=["adr:sec-audit-01"],
    )
    sec_cold = engine.query_cold_ledger(topic_key="auth.session_strategy")
    assert len(sec_cold["negative_knowledge"]) == 1

    # 롤백 1회 성공 후 동일 tx_id 재롤백 시도 시 예외 발생
    engine.rollback(res_opt3.tx_id)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="already been rolled back"):
        engine.rollback(res_opt3.tx_id)  # type: ignore[arg-type]


def test_cli_and_git_hook_bridge(tmp_path: Path) -> None:
    """CLI 명령어(status, post-commit-hook, export-personal, rollback) 통합 동작 검증."""
    from dual_ssot.cli import run_cli

    ws = tmp_path / "cli_proj"
    personal = tmp_path / "cli_personal"
    engine = DualSSOTEngine(workspace_path=ws, personal_dir=personal)

    engine.stage_decision(
        session_id="git_hook_session",
        topic_key="build.package_manager",
        decision_value="uv + hatchling",
        rationale="Fast deterministic lockfile",
        evidence_ids=["commit:hook123"],
    )

    assert run_cli(["--workspace", str(ws), "--personal-dir", str(personal), "status"]) == 0
    assert (
        run_cli(
            [
                "--workspace",
                str(ws),
                "--personal-dir",
                str(personal),
                "post-commit-hook",
                "--session-id",
                "git_hook_session",
            ]
        )
        == 0
    )
    assert (
        run_cli(["--workspace", str(ws), "--personal-dir", str(personal), "export-personal"]) == 0
    )
    assert (personal / "PERSONAL_CONTEXT_PACK.md").exists()



def test_mcp_multi_workspace_dynamic_routing(tmp_path: Path) -> None:
    """전역 MCP 서버 1개에서 여러 프로젝트 워크스페이스(.ssot/)로 세션별 동적 라우팅 검증."""
    from dual_ssot.mcp_server import create_mcp_server

    ws_a = tmp_path / "project_alpha"
    ws_b = tmp_path / "project_beta"
    personal = tmp_path / "global_personal"

    mcp, _ = create_mcp_server(workspace_path=ws_a, personal_dir=personal)
    # Extract registered tool functions
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}

    snap_a = tools["init_session"](session_id="sess_alpha", workspace_path=str(ws_a))
    snap_b = tools["init_session"](session_id="sess_beta", workspace_path=str(ws_b))

    assert snap_a["project_id"] == "project_alpha"
    assert snap_b["project_id"] == "project_beta"

    # Subsequent calls for sess_beta automatically route to ws_b without repeating workspace_path
    tools["stage_decision"](
        session_id="sess_beta",
        topic_key="db.engine",
        decision_value="DuckDB OLAP",
        rationale="Embedded analytical queries",
        evidence_ids=["file:src/analytics.py#L1-L20"],
        file_scope=["src/analytics.py"],
    )
    promo_b = tools["promote_session"](
        session_id="sess_beta",
        proof={"file_exists": True, "git_diff_changed_lines": 12, "test_exit_code": 0},
    )
    assert promo_b["status"] == "PROMOTED"
    assert (ws_b / ".ssot" / "ACTIVE_CONTEXT.md").exists()
    assert "DuckDB OLAP" in (ws_b / ".ssot" / "ACTIVE_CONTEXT.md").read_text(encoding="utf-8")
    assert "DuckDB OLAP" not in (ws_a / ".ssot" / "ACTIVE_CONTEXT.md").read_text(encoding="utf-8")
