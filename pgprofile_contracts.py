"""Shared JSON contract helpers for pg_profile analysis artifacts."""

from __future__ import annotations

from enum import Enum
from typing import Any


SCHEMA_VERSION = "1.0"
PROVEN_WORKLOAD_MATCH_THRESHOLD = 0.85


class EvidenceType(str, Enum):
    """How causal a parameter-to-metric link is allowed to claim to be."""

    PROBABLE = "probable"
    PROVEN = "proven"


class ConfidenceLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ImpactKind(str, Enum):
    IMPROVED = "improved"
    DEGRADED = "degraded"
    NEUTRAL = "neutral"


class WorkloadLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


EVIDENCE_TYPES = frozenset(item.value for item in EvidenceType)
CONFIDENCE_LEVELS = frozenset(item.value for item in ConfidenceLevel)
IMPACT_KINDS = frozenset(item.value for item in ImpactKind)
WORKLOAD_LEVELS = frozenset(item.value for item in WorkloadLevel)
METRIC_DIRECTIONS = frozenset({"up", "down", "flat"})


def workload_level_for(score: float | None) -> str:
    if score is None:
        return WorkloadLevel.UNKNOWN.value
    if score >= PROVEN_WORKLOAD_MATCH_THRESHOLD:
        return WorkloadLevel.HIGH.value
    if score >= 0.6:
        return WorkloadLevel.MEDIUM.value
    return WorkloadLevel.LOW.value


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_run_identity_pair(run_a: dict[str, Any], run_b: dict[str, Any]) -> dict[str, Any]:
    """Return normalized run identity payload for pair comparisons."""
    return {
        "mode": "pair",
        "runs": [
            {
                "role": "before",
                "run_id": run_a.get("run_id"),
                "server": run_a.get("server"),
                "report_start": run_a.get("report_start"),
                "report_end": run_a.get("report_end"),
                "interval_hours": _to_float(run_a.get("interval_hours")),
            },
            {
                "role": "after",
                "run_id": run_b.get("run_id"),
                "server": run_b.get("server"),
                "report_start": run_b.get("report_start"),
                "report_end": run_b.get("report_end"),
                "interval_hours": _to_float(run_b.get("interval_hours")),
            },
        ],
    }


def build_workload_match(run_a: dict[str, Any], run_b: dict[str, Any]) -> dict[str, Any]:
    """Estimate workload comparability for pair-mode comparisons."""
    interval_a = _to_float(run_a.get("interval_hours"))
    interval_b = _to_float(run_b.get("interval_hours"))
    interval_score: float | None = None
    if interval_a is not None and interval_b is not None:
        baseline = max(abs(interval_a), abs(interval_b), 1.0)
        interval_score = max(0.0, 1.0 - abs(interval_a - interval_b) / baseline)

    server_a = (run_a.get("server") or "").strip()
    server_b = (run_b.get("server") or "").strip()
    if not server_a or not server_b:
        server_score = 0.8
    else:
        server_score = 1.0 if server_a == server_b else 0.5

    if interval_score is None:
        score = round(server_score, 4)
    else:
        score = round((interval_score * 0.7) + (server_score * 0.3), 4)

    return {
        "workload_match_score": score,
        "level": workload_level_for(score),
        "components": {
            "interval_similarity": None if interval_score is None else round(interval_score, 4),
            "server_similarity": server_score,
        },
        "notes": "Higher score means better run comparability for influence interpretation.",
    }


def build_confidence_meta(
    *,
    changed_params_count: int,
    workload_match_score: float | None = None,
    isolated_change: bool = False,
    changed_params_threshold: int = 10,
    mode: str = "pair",
) -> dict[str, Any]:
    """Return baseline confidence metadata for influence interpretation."""
    workload_ok = (
        workload_match_score is not None
        and workload_match_score >= PROVEN_WORKLOAD_MATCH_THRESHOLD
    )
    proven_candidate = isolated_change and changed_params_count == 1 and workload_ok
    degraded = changed_params_count > changed_params_threshold
    if degraded:
        confidence = ConfidenceLevel.LOW.value
    elif proven_candidate:
        confidence = ConfidenceLevel.HIGH.value
    else:
        confidence = ConfidenceLevel.MEDIUM.value

    evidence_type = (
        EvidenceType.PROVEN.value if proven_candidate else EvidenceType.PROBABLE.value
    )
    if degraded:
        notes = "Confidence downgraded because too many parameters changed simultaneously."
    elif proven_candidate:
        notes = (
            "Evidence marked as proven: isolated single-parameter change and high workload match."
        )
    else:
        notes = "Confidence baseline for pair comparison without isolated causality proof."

    return {
        "mode": mode,
        "changed_params_count": changed_params_count,
        "changed_params_threshold": changed_params_threshold,
        "workload_match_score": workload_match_score,
        "isolated_change": isolated_change,
        "confidence": confidence,
        "evidence_type": evidence_type,
        "proven_criteria": {
            "requires_isolated_change": True,
            "requires_changed_params_count": 1,
            "requires_workload_match_score_at_least": PROVEN_WORKLOAD_MATCH_THRESHOLD,
        },
        "notes": notes,
    }


def _validate_enum_field(holder: dict[str, Any], field: str, allowed: frozenset[str], where: str) -> None:
    value = holder.get(field)
    if value is None:
        return
    if value not in allowed:
        raise ValueError(f"{where}.{field} must be one of {sorted(allowed)}, got {value!r}")


def _validate_influence_rows(rows: Any, where: str) -> None:
    """Enforce the enum vocabulary on influence rows wherever they are embedded."""
    if rows is None:
        return
    if not isinstance(rows, list):
        raise ValueError(f"{where} must be a list")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{where}[{index}] must be an object")
        location = f"{where}[{index}]"
        _validate_enum_field(row, "impact", IMPACT_KINDS, location)
        _validate_enum_field(row, "confidence", CONFIDENCE_LEVELS, location)
        _validate_enum_field(row, "evidence_type", EVIDENCE_TYPES, location)
        _validate_enum_field(row, "metric_direction", METRIC_DIRECTIONS, location)


def _validate_confidence_and_workload_enums(payload: dict[str, Any]) -> None:
    confidence = payload.get("confidence_meta")
    if isinstance(confidence, dict):
        _validate_enum_field(confidence, "confidence", CONFIDENCE_LEVELS, "confidence_meta")
        _validate_enum_field(confidence, "evidence_type", EVIDENCE_TYPES, "confidence_meta")
    workload = payload.get("workload_match")
    if isinstance(workload, dict):
        _validate_enum_field(workload, "level", WORKLOAD_LEVELS, "workload_match")


def validate_contract_payload(payload: dict[str, Any]) -> None:
    """Validate shared contract blocks for known payload types."""
    payload_type = payload.get("type")

    if payload_type in {"influence_table", "influence_table_series"}:
        # Influence tables carry no contract header yet, but their vocabulary is fixed.
        _validate_confidence_and_workload_enums(payload)
        _validate_influence_rows(payload.get("rows"), "rows")
        return

    if payload_type not in {"run_comparison", "settings_diff"}:
        return

    contract = payload.get("contract")
    if not isinstance(contract, dict):
        raise ValueError("contract block is required for run/settings artifacts")
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported contract schema_version: {contract.get('schema_version')}")

    run_identity = payload.get("run_identity")
    if not isinstance(run_identity, dict):
        raise ValueError("run_identity block is required")
    if run_identity.get("mode") not in {"pair", "series"}:
        raise ValueError("run_identity.mode must be pair or series")
    runs = run_identity.get("runs")
    if not isinstance(runs, list) or len(runs) < 2:
        raise ValueError("run_identity.runs must contain at least two runs")

    workload = payload.get("workload_match")
    if not isinstance(workload, dict):
        raise ValueError("workload_match block is required")
    score = workload.get("workload_match_score")
    if not isinstance(score, (int, float)) or not (0.0 <= float(score) <= 1.0):
        raise ValueError("workload_match.workload_match_score must be in range [0, 1]")
    _validate_enum_field(workload, "level", WORKLOAD_LEVELS, "workload_match")

    confidence = payload.get("confidence_meta")
    if not isinstance(confidence, dict):
        raise ValueError("confidence_meta block is required")
    if confidence.get("confidence") not in CONFIDENCE_LEVELS:
        raise ValueError("confidence_meta.confidence must be low|medium|high")
    if confidence.get("evidence_type") not in EVIDENCE_TYPES:
        raise ValueError("confidence_meta.evidence_type must be probable|proven")
    if not isinstance(confidence.get("changed_params_count"), int):
        raise ValueError("confidence_meta.changed_params_count must be int")
    if not isinstance(confidence.get("changed_params_threshold"), int):
        raise ValueError("confidence_meta.changed_params_threshold must be int")

    evidence_type = confidence.get("evidence_type")
    isolated_change = bool(confidence.get("isolated_change"))
    changed_count = confidence.get("changed_params_count")
    workload_score = confidence.get("workload_match_score")
    if workload_score is not None and not isinstance(workload_score, (int, float)):
        raise ValueError("confidence_meta.workload_match_score must be numeric or null")

    _validate_influence_rows(payload.get("influence_rows"), "influence_rows")

    if evidence_type == "proven":
        if changed_count != 1:
            raise ValueError("proven evidence requires changed_params_count = 1")
        if not isolated_change:
            raise ValueError("proven evidence requires isolated_change = true")
        if workload_score is None or float(workload_score) < PROVEN_WORKLOAD_MATCH_THRESHOLD:
            raise ValueError(
                f"proven evidence requires workload_match_score >= {PROVEN_WORKLOAD_MATCH_THRESHOLD}"
            )
