from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from jvmcheck.models import (
    AnalysisResult,
    ContainerResources,
    Finding,
    PodResourcesBudget,
    Recommendation,
    RuntimeContext,
)
from jvmcheck.recommenders.quota_aware_memory_planner import build_quota_aware_memory_plan

DEFAULT_RECOMMENDATIONS = (
    Path(__file__).resolve().parents[3] / "knowledge" / "jvm_recommendations.yaml"
)
DEFAULT_FLAG_MATRIX = (
    Path(__file__).resolve().parents[3] / "knowledge" / "jvm_flag_matrix.yaml"
)

def enrich_with_recommendations(
    container: ContainerResources,
    budget: PodResourcesBudget,
    analysis: AnalysisResult,
    runtime_context: RuntimeContext | None = None,
    recommendations_path: Path | None = None,
    flag_matrix_path: Path | None = None,
) -> AnalysisResult:
    runtime_context = runtime_context or RuntimeContext()
    rec_map = load_recommendations(recommendations_path)
    flag_matrix = load_flag_matrix(flag_matrix_path)
    confidence = _confidence_level(runtime_context)

    grouped = _group_findings(analysis.findings)
    analysis.recommendations = []

    for code, finding_list in grouped.items():
        rec_body = rec_map.get(code)
        if not rec_body:
            continue
        rec = _recommendation_from_knowledge(
            rec_body,
            finding_list,
            confidence=confidence,
            jdk=runtime_context.jdk_version,
            flag_matrix=flag_matrix,
        )
        analysis.recommendations.append(rec)

    if _has_finding(analysis.findings, "memory.limit_pressure"):
        current_limit = container.limits.memory_mib or 0
        requested_delta = max(int(current_limit * 0.15), 256)
        memory_plan = build_quota_aware_memory_plan(
            budget=budget,
            target_container_name=container.name,
            requested_delta_mib=requested_delta,
        )
        analysis.memory_plan = memory_plan
        notes = list(memory_plan.notes)
        if memory_plan.status == "needs_rebalance":
            notes.append(f"Suggested donor containers: {memory_plan.donor_suggestions}")
        requires_platform_escalation = memory_plan.status == "needs_platform_escalation"
        analysis.recommendations.append(
            Recommendation(
                title="Quota-aware memory rebalance plan",
                rationale="Memory limit pressure requires container budget adjustment in addition to JVM tuning.",
                suggested_java_tool_options=[],
                confidence=confidence,
                evidence_score=70,
                risk_score=60,
                expected_gain="avoid OOM and restarts under peaks",
                verification_window="1-2 peak windows",
                rollback_plan=["Rollback pod resources to previous limits if donor containers degrade."],
                rule_ids=["memory.limit_pressure"],
                notes=["If no improvement after tuning window, escalate for heap dump analysis."],
                requires_platform_escalation=requires_platform_escalation,
                blocking_conditions=["Platform quota constraints"] if requires_platform_escalation else [],
            )
        )

    if _has_flag_conflicts(container.java_tool_options, flag_matrix):
        analysis.recommendations.append(
            Recommendation(
                title="Resolve JVM flag conflicts before tuning rollout",
                rationale="Conflicting flag configuration can make tuning effect unpredictable.",
                suggested_java_tool_options=[],
                confidence=confidence,
                evidence_score=85,
                risk_score=70,
                expected_gain="deterministic runtime behavior",
                verification_window="single canary cycle",
                rollback_plan=["Revert to last known-good JVM option set."],
                rule_ids=["jvm.flag_conflict_maxram", "jvm.flag_duplicate"],
                notes=["Normalize JVM options and remove duplicates/conflicts."],
            )
        )

    if not analysis.recommendations:
        analysis.recommendations.append(
            Recommendation(
                title="No critical JVM changes required",
                rationale="Current metrics do not indicate GC or heap pressure above thresholds.",
                suggested_java_tool_options=[],
                confidence=confidence,
                evidence_score=65,
                risk_score=10,
                expected_gain="keep baseline stable",
                verification_window="next scheduled load profile",
                rollback_plan=["No rollback required."],
                rule_ids=[],
            )
        )

    return analysis


def load_recommendations(path: Path | None = None) -> dict[str, dict[str, Any]]:
    cfg_path = path or DEFAULT_RECOMMENDATIONS
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    items = raw.get("recommendations") or []
    return {str(item["id"]): item for item in items if isinstance(item, dict) and item.get("id")}


def load_flag_matrix(path: Path | None = None) -> dict[str, dict[str, Any]]:
    cfg_path = path or DEFAULT_FLAG_MATRIX
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    items = raw.get("flags") or []
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out[name] = item
    return out


def _group_findings(findings: list[Finding]) -> dict[str, list[Finding]]:
    grouped: dict[str, list[Finding]] = {}
    for finding in findings:
        grouped.setdefault(finding.code, []).append(finding)
    return grouped


def _has_finding(findings: list[Finding], code: str) -> bool:
    return any(f.code == code for f in findings)


def _recommendation_from_knowledge(
    recommendation: dict[str, Any],
    findings: list[Finding],
    *,
    confidence: str,
    jdk: int | None,
    flag_matrix: dict[str, dict[str, Any]],
) -> Recommendation:
    suggested = [str(flag) for flag in (recommendation.get("suggested_flags") or [])]
    filtered = _filter_flags_by_jdk(suggested, jdk, flag_matrix)
    evidence = _evidence_score(findings)
    risk = int(recommendation.get("risk_score") or 50)
    return Recommendation(
        title=str(recommendation.get("title") or recommendation.get("id") or "Recommendation"),
        rationale=str(recommendation.get("rationale") or ""),
        suggested_java_tool_options=filtered,
        confidence=confidence,
        evidence_score=evidence,
        risk_score=risk,
        expected_gain=str(recommendation.get("expected_gain") or "improve JVM stability"),
        verification_window="30-60m after deploy",
        rollback_plan=["Rollback to previous JVM option set if SLA worsens."],
        rule_ids=sorted({f.code for f in findings}),
        notes=[str(note) for note in recommendation.get("actions") or []],
    )


def _filter_flags_by_jdk(
    flags: list[str],
    jdk: int | None,
    flag_matrix: dict[str, dict[str, Any]],
) -> list[str]:
    if jdk is None:
        return flags
    out: list[str] = []
    for flag in flags:
        key = flag.split("=", 1)[0]
        meta = flag_matrix.get(key)
        if not meta:
            out.append(flag)
            continue
        jdks = meta.get("jdk") or []
        if not jdks or any(int(v) == jdk or (int(v) == 21 and jdk >= 21) for v in jdks):
            out.append(flag)
    return out


def _evidence_score(findings: list[Finding]) -> int:
    if not findings:
        return 40
    severity_rank = {"critical": 30, "high": 25, "warning": 18, "info": 10}
    score = 0
    for finding in findings:
        score += severity_rank.get(finding.severity.lower(), 8)
        if finding.evidence:
            score += 8
    return min(100, max(30, score // len(findings)))


def _confidence_level(runtime_context: RuntimeContext) -> str:
    if runtime_context.jdk_version and runtime_context.spring_boot_version:
        return "high"
    if runtime_context.jdk_version:
        return "medium"
    return "low"


def _has_flag_conflicts(flags: list[str], matrix: dict[str, dict[str, Any]]) -> bool:
    normalized = [str(flag).strip() for flag in flags if str(flag).strip()]
    present = {flag.split("=", 1)[0] for flag in normalized}
    for flag in normalized:
        key = flag.split("=", 1)[0]
        meta = matrix.get(key) or {}
        conflicts = {str(conf) for conf in (meta.get("conflicts") or [])}
        if conflicts.intersection(present):
            return True
    return False

