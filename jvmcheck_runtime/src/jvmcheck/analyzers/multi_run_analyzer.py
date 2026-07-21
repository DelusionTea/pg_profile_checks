from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

import json

from jvmcheck.models import AnalysisResult, Finding, FindingTrend, MultiRunAnalysis


def analyze_multi_run_stability(
    current_analysis: AnalysisResult,
    snapshot_paths: list[Path],
    *,
    min_stability_ratio: float = 0.6,
    baseline_path: Path | None = None,
) -> MultiRunAnalysis:
    runs = [_analysis_to_findings(current_analysis)]
    for path in snapshot_paths:
        runs.append(_load_snapshot_findings(path))

    total_runs = len(runs)
    by_code: dict[str, list[Finding]] = defaultdict(list)
    occurrences: dict[str, set[int]] = defaultdict(set)

    for run_idx, findings in enumerate(runs):
        for finding in findings:
            by_code[finding.code].append(finding)
            occurrences[finding.code].add(run_idx)

    stable: list[FindingTrend] = []
    ephemeral: list[FindingTrend] = []
    for code, seen in occurrences.items():
        ratio = len(seen) / total_runs if total_runs else 0.0
        severity = _max_severity(by_code[code])
        trend = FindingTrend(
            code=code,
            severity=severity,
            occurrences=len(seen),
            total_runs=total_runs,
            stability_ratio=round(ratio, 3),
            sample_messages=sorted({f.message for f in by_code[code] if f.message})[:3],
        )
        if ratio >= min_stability_ratio:
            stable.append(trend)
        else:
            ephemeral.append(trend)

    regression: list[FindingTrend] = []
    if baseline_path:
        baseline_codes = {f.code for f in _load_snapshot_findings(baseline_path)}
        for trend in stable:
            if trend.code not in baseline_codes:
                regression.append(trend)

    effectiveness = _tuning_effectiveness(stable, regression)
    stable.sort(key=lambda t: (_severity_rank(t.severity), -t.stability_ratio, t.code))
    ephemeral.sort(key=lambda t: (_severity_rank(t.severity), -t.stability_ratio, t.code))
    regression.sort(key=lambda t: (_severity_rank(t.severity), -t.stability_ratio, t.code))
    return MultiRunAnalysis(
        total_runs=total_runs,
        stable_findings=stable,
        ephemeral_findings=ephemeral,
        regression_findings=regression,
        tuning_effectiveness=effectiveness,
    )


def multi_run_to_dict(result: MultiRunAnalysis) -> dict[str, Any]:
    return {
        "total_runs": result.total_runs,
        "stable_findings": [asdict(item) for item in result.stable_findings],
        "ephemeral_findings": [asdict(item) for item in result.ephemeral_findings],
        "regression_findings": [asdict(item) for item in result.regression_findings],
        "tuning_effectiveness": result.tuning_effectiveness,
    }


def _analysis_to_findings(analysis: AnalysisResult) -> list[Finding]:
    return list(analysis.findings or [])


def _load_snapshot_findings(path: Path) -> list[Finding]:
    data = json.loads(path.read_text(encoding="utf-8"))
    findings = data.get("findings") if isinstance(data, dict) else []
    out: list[Finding] = []
    for item in findings or []:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        out.append(
            Finding(
                code=str(item.get("code")),
                severity=str(item.get("severity") or "warning"),
                message=str(item.get("message") or ""),
                evidence={str(k): str(v) for k, v in (item.get("evidence") or {}).items()},
                threshold=str(item.get("threshold") or ""),
                details={str(k): str(v) for k, v in (item.get("details") or {}).items()},
            )
        )
    return out


def _max_severity(findings: list[Finding]) -> str:
    if not findings:
        return "info"
    return sorted(findings, key=lambda item: _severity_rank(item.severity))[0].severity


def _severity_rank(severity: str) -> int:
    rank = {"critical": 0, "high": 1, "warning": 2, "info": 3, "low": 4}
    return rank.get((severity or "warning").lower(), 9)


def _tuning_effectiveness(stable: list[FindingTrend], regression: list[FindingTrend]) -> str:
    if regression:
        return "regression"
    has_critical = any(item.severity in ("critical", "high") for item in stable)
    if has_critical:
        return "not_effective"
    if stable:
        return "partial"
    return "effective"
