"""Parametric bound evaluators & Strategy 3C benchmark calculators from Audit Vault."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict


def evaluate_context_token_roi(
    turns: int,
    unbounded_tokens_per_turn: int,
    capped_tokens_per_turn: int,
    cold_deref_prob: float,
    cold_roundtrip_tokens: int,
    mcp_schema_tokens_per_turn: int,
    receipt_tokens: int = 40,
) -> Dict[str, float]:
    """Calculates Net Token ROI of L1 100-line capping vs MCP tool schema overhead."""
    gross_saved_per_turn = unbounded_tokens_per_turn - capped_tokens_per_turn
    overhead_per_turn = (cold_deref_prob * cold_roundtrip_tokens) + mcp_schema_tokens_per_turn
    net_savings = turns * (gross_saved_per_turn - overhead_per_turn) - receipt_tokens
    breakeven_deref_prob = (
        (gross_saved_per_turn - mcp_schema_tokens_per_turn - (receipt_tokens / max(turns, 1)))
        / max(cold_roundtrip_tokens, 1)
    )
    return {
        "net_token_savings": round(net_savings, 2),
        "is_viable": float(net_savings > 0),
        "max_allowable_deref_prob": round(max(0.0, breakeven_deref_prob), 4),
    }


def evaluate_conflict_detector_roi(
    conflict_prob: float,
    fn_rate: float,
    fp_rate: float,
    rework_cost_min: float,
    manual_triage_cost_min: float,
    schema_maint_cost_min: float,
) -> Dict[str, float]:
    """Calculates expected developer time saved (in minutes) by deterministic conflict detection."""
    baseline_cost = conflict_prob * rework_cost_min
    with_detector_cost = (
        conflict_prob * fn_rate * rework_cost_min
        + (1.0 - conflict_prob) * fp_rate * manual_triage_cost_min
        + schema_maint_cost_min
    )
    net_benefit_min = baseline_cost - with_detector_cost
    return {
        "baseline_expected_cost_min": round(baseline_cost, 2),
        "detector_expected_cost_min": round(with_detector_cost, 2),
        "net_time_saved_min": round(net_benefit_min, 2),
        "is_viable": float(net_benefit_min > 0),
    }


def compute_min_promotion_projects(noise_prob: float, max_false_promotion_alpha: float) -> int:
    """Computes K_min projects needed to bound Personal SSOT false promotion below alpha."""
    if not (0.0 < noise_prob < 1.0) or not (0.0 < max_false_promotion_alpha < 1.0):
        raise ValueError("Probabilities must be strictly between 0 and 1.")
    return max(1, math.ceil(math.log(max_false_promotion_alpha) / math.log(noise_prob)))


@dataclass
class SimulationScenario:
    name: str
    total_events: int
    template_coverage: float
    true_conflict_rate: float
    key_naming_divergence: float
    hub_file_false_sharing: float
    cross_dir_coupling_rate: float


@dataclass
class CostWeights:
    sync_approval_cost: float = 1.0
    false_alarm_triage_cost: float = 3.0
    silent_split_brain_cost: float = 25.0
    lazy_receipt_review_cost: float = 0.2


def evaluate_strategies(
    scenario: SimulationScenario, weights: CostWeights | None = None
) -> Dict[str, Dict[str, float]]:
    """Evaluates S1..S3C canonical key strategies across project lifecycle phases."""
    if weights is None:
        weights = CostWeights()

    N = scenario.total_events
    c = scenario.template_coverage
    e = 1.0 - c
    p_conf = scenario.true_conflict_rate
    p_div = scenario.key_naming_divergence
    p_hub = scenario.hub_file_false_sharing
    p_xdir = scenario.cross_dir_coupling_rate

    total_true_conflicts = N * p_conf
    core_conflicts = N * c * p_conf
    exc_conflicts = N * e * p_conf
    unique_new_keys = N * e * 0.50

    results: Dict[str, Dict[str, float]] = {}

    # Strategy 1: Drop exceptions
    s1_caught = core_conflicts
    s1_missed = exc_conflicts
    s1_cost = s1_missed * weights.silent_split_brain_cost
    results["1_Fixed_Drop_Exceptions"] = {
        "conflict_recall_pct": round((s1_caught / total_true_conflicts) * 100, 1),
        "missed_conflicts": round(s1_missed, 2),
        "blocking_interrupts": 0.0,
        "false_alarms": 0.0,
        "total_pain_cost": round(s1_cost, 1),
    }

    # Strategy 2: Strict sync approval on every exception
    s2_interrupts = N * e
    s2_missed = exc_conflicts * 0.10
    s2_caught = core_conflicts + exc_conflicts * 0.90
    s2_cost = (s2_interrupts * weights.sync_approval_cost) + (s2_missed * weights.silent_split_brain_cost)
    results["2_Strict_Every_Exception"] = {
        "conflict_recall_pct": round((s2_caught / total_true_conflicts) * 100, 1),
        "missed_conflicts": round(s2_missed, 2),
        "blocking_interrupts": round(s2_interrupts, 1),
        "false_alarms": 0.0,
        "total_pain_cost": round(s2_cost, 1),
    }

    # Strategy 3A: Sync hybrid on new key
    s3a_missed = exc_conflicts * (p_div * 0.15)
    s3a_caught = total_true_conflicts - s3a_missed
    s3a_cost = (unique_new_keys * weights.sync_approval_cost) + (s3a_missed * weights.silent_split_brain_cost)
    results["3A_Hybrid_Sync_On_New_Key"] = {
        "conflict_recall_pct": round((s3a_caught / total_true_conflicts) * 100, 1),
        "missed_conflicts": round(s3a_missed, 2),
        "blocking_interrupts": round(unique_new_keys, 1),
        "false_alarms": 0.0,
        "total_pain_cost": round(s3a_cost, 1),
    }

    # Strategy 3B: Naive file scope (includes hub files)
    s3b_missed = exc_conflicts * p_div * p_xdir
    s3b_caught = total_true_conflicts - s3b_missed
    s3b_false_alarms = (N * e * (1.0 - p_conf)) * p_hub
    s3b_cost = (s3b_false_alarms * weights.false_alarm_triage_cost) + (
        s3b_missed * weights.silent_split_brain_cost
    )
    results["3B_Naive_FileScope_Hybrid"] = {
        "conflict_recall_pct": round((s3b_caught / total_true_conflicts) * 100, 1),
        "missed_conflicts": round(s3b_missed, 2),
        "blocking_interrupts": round(s3b_false_alarms, 1),
        "false_alarms": round(s3b_false_alarms, 1),
        "total_pain_cost": round(s3b_cost, 1),
    }

    # Strategy 3C: AutoBroadcast + LeafScope (Hub Denylisted) + Lazy Receipt
    s3c_missed = exc_conflicts * (p_div * 0.20) * p_xdir
    s3c_caught = total_true_conflicts - s3c_missed
    s3c_false_alarms = (N * e * (1.0 - p_conf)) * (p_hub * 0.10)
    s3c_cost = (
        (unique_new_keys * weights.lazy_receipt_review_cost)
        + (s3c_false_alarms * weights.false_alarm_triage_cost)
        + (s3c_missed * weights.silent_split_brain_cost)
    )
    results["3C_AutoBroadcast_LeafScope_Receipt"] = {
        "conflict_recall_pct": round((s3c_caught / total_true_conflicts) * 100, 1),
        "missed_conflicts": round(s3c_missed, 2),
        "blocking_interrupts": round(s3c_false_alarms, 1),
        "false_alarms": round(s3c_false_alarms, 1),
        "total_pain_cost": round(s3c_cost, 1),
    }

    return results
