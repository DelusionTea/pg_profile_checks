from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


METRIC_KEYS = (
    "heap_used_mib",
    "heap_committed_mib",
    "old_gen_used_mib",
    "old_gen_capacity_mib",
    "gc_pause_p95_ms",
    "gc_pause_p99_ms",
    "gc_time_ratio_percent",
    "container_memory_working_set_mib",
)


def validate_ai_response(bundle_dir: Path, response_file: Path) -> dict[str, Any]:
    snapshot = json.loads((bundle_dir / "input_snapshot.json").read_text(encoding="utf-8"))
    response = json.loads(response_file.read_text(encoding="utf-8"))
    approved_urls = _extract_approved_urls((bundle_dir / "approved_sources.md").read_text(encoding="utf-8"))

    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, str]] = []

    _check_required_top_level(response, errors, checks)
    _check_recommendation_shapes(response, errors, checks)
    _check_citations_allowlist(response, approved_urls, errors, checks)
    _check_evidence_link(response, errors, checks)
    _check_gate_checklist(response, errors, checks)
    _check_confidence_discipline(response, snapshot, errors, warnings, checks)

    return {
        "pass": not errors,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "summary": "PASS" if not errors else "FAIL",
    }


def _check_required_top_level(response: dict[str, Any], errors: list[str], checks: list[dict[str, str]]) -> None:
    required = ("summary", "recommendations", "data_gaps", "escalation", "gate_checklist")
    missing = [name for name in required if name not in response]
    if missing:
        errors.append(f"Missing top-level fields: {missing}")
        checks.append({"check": "schema.top_level", "status": "fail", "comment": "Missing required fields."})
        return
    checks.append({"check": "schema.top_level", "status": "pass", "comment": "Top-level fields exist."})


def _check_recommendation_shapes(response: dict[str, Any], errors: list[str], checks: list[dict[str, str]]) -> None:
    recs = response.get("recommendations")
    if not isinstance(recs, list):
        errors.append("Field 'recommendations' must be a list.")
        checks.append({"check": "schema.recommendations", "status": "fail", "comment": "Wrong type."})
        return
    required = (
        "title",
        "change",
        "evidence",
        "jdk_applicability",
        "citations",
        "risks",
        "monitoring",
        "rollback_trigger",
        "confidence",
    )
    for idx, rec in enumerate(recs):
        if not isinstance(rec, dict):
            errors.append(f"Recommendation {idx} is not an object.")
            continue
        missing = [name for name in required if name not in rec]
        if missing:
            errors.append(f"Recommendation {idx} missing fields: {missing}")
    checks.append(
        {
            "check": "schema.recommendations",
            "status": "pass" if not any("Recommendation" in e and "missing fields" in e for e in errors) else "fail",
            "comment": "Recommendation objects validated.",
        }
    )


def _check_citations_allowlist(
    response: dict[str, Any],
    approved_urls: list[str],
    errors: list[str],
    checks: list[dict[str, str]],
) -> None:
    bad_refs: list[str] = []
    for idx, rec in enumerate(response.get("recommendations", [])):
        citations = rec.get("citations", [])
        if not isinstance(citations, list) or not citations:
            bad_refs.append(f"Recommendation {idx}: citations missing.")
            continue
        for citation in citations:
            if not isinstance(citation, str) or not _starts_with_any(citation, approved_urls):
                bad_refs.append(f"Recommendation {idx}: unapproved citation '{citation}'.")
    if bad_refs:
        errors.extend(bad_refs)
        checks.append({"check": "gate.source_allowlist", "status": "fail", "comment": "Found unapproved citations."})
        return
    checks.append({"check": "gate.source_allowlist", "status": "pass", "comment": "All citations are allowlisted."})


def _check_evidence_link(response: dict[str, Any], errors: list[str], checks: list[dict[str, str]]) -> None:
    weak_evidence = []
    for idx, rec in enumerate(response.get("recommendations", [])):
        evidence = str(rec.get("evidence", "")).lower()
        if not evidence:
            weak_evidence.append(f"Recommendation {idx}: evidence is empty.")
            continue
        if not any(metric in evidence for metric in METRIC_KEYS):
            weak_evidence.append(
                f"Recommendation {idx}: evidence does not reference known metric keys ({', '.join(METRIC_KEYS)})."
            )
    if weak_evidence:
        errors.extend(weak_evidence)
        checks.append({"check": "gate.local_evidence_link", "status": "fail", "comment": "Evidence is not metric-linked."})
        return
    checks.append({"check": "gate.local_evidence_link", "status": "pass", "comment": "Evidence links metrics."})


def _check_gate_checklist(response: dict[str, Any], errors: list[str], checks: list[dict[str, str]]) -> None:
    gate_checklist = response.get("gate_checklist", [])
    if not isinstance(gate_checklist, list) or not gate_checklist:
        errors.append("gate_checklist must be a non-empty list.")
        checks.append({"check": "gate.checklist", "status": "fail", "comment": "Checklist missing."})
        return
    failed = []
    for item in gate_checklist:
        if not isinstance(item, dict):
            failed.append("invalid item")
            continue
        status = str(item.get("status", "")).strip().lower()
        if status not in {"pass", "ok"}:
            failed.append(str(item.get("gate", "unknown_gate")))
    if failed:
        errors.append(f"gate_checklist has failed gates: {failed}")
        checks.append({"check": "gate.checklist", "status": "fail", "comment": "Checklist contains failed gates."})
        return
    checks.append({"check": "gate.checklist", "status": "pass", "comment": "Checklist gates are passed."})


def _check_confidence_discipline(
    response: dict[str, Any],
    snapshot: dict[str, Any],
    errors: list[str],
    warnings: list[str],
    checks: list[dict[str, str]],
) -> None:
    context = snapshot.get("runtime_context", {})
    metrics = snapshot.get("runtime_metrics", {})
    has_jdk = context.get("jdk_version") is not None
    has_key_metrics = any(metrics.get(key) is not None for key in METRIC_KEYS)

    for idx, rec in enumerate(response.get("recommendations", [])):
        confidence = str(rec.get("confidence", "")).strip().lower()
        if confidence == "high" and (not has_jdk or not has_key_metrics):
            errors.append(
                f"Recommendation {idx}: confidence='high' violates confidence discipline (missing jdk/metrics)."
            )
        if confidence in {"medium", "low"} and not rec.get("change"):
            warnings.append(f"Recommendation {idx}: low/medium confidence with empty change field.")

    failed = any("confidence='high'" in err for err in errors)
    checks.append(
        {
            "check": "gate.confidence_discipline",
            "status": "fail" if failed else "pass",
            "comment": "Confidence levels are consistent with available context.",
        }
    )


def _extract_approved_urls(text: str) -> list[str]:
    urls = re.findall(r"https?://[^\s)]+", text)
    return sorted(set(urls))


def _starts_with_any(value: str, prefixes: list[str]) -> bool:
    return any(value.startswith(prefix) for prefix in prefixes)

