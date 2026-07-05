"""Match deterministic findings to offline PostgreSQL recommendations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_RECOMMENDATIONS = Path(__file__).resolve().parent / "knowledge" / "recommendations.yaml"
DEFAULT_GUC_GUIDANCE = Path(__file__).resolve().parent / "knowledge" / "guc_guidance.yaml"


@dataclass
class AdvisedFinding:
    finding: dict[str, Any]
    advice: dict[str, Any]
    guc_guidance: dict[str, Any] | None = None


@dataclass
class AdvisorReport:
    source_type: str
    meta: dict[str, Any]
    advised_findings: list[AdvisedFinding] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def load_recommendations(path: Path | None = None) -> dict[str, dict[str, Any]]:
    cfg_path = path or DEFAULT_RECOMMENDATIONS
    with cfg_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    items = data.get("recommendations", [])
    return {item["id"]: item for item in items}


def load_guc_guidance(path: Path | None = None) -> dict[str, dict[str, Any]]:
    cfg_path = path or DEFAULT_GUC_GUIDANCE
    with cfg_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data.get("guc_guidance", {})


def _resolve_advice(finding_id: str, rec_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if finding_id in rec_map:
        return rec_map[finding_id]
    if "." in finding_id:
        category = finding_id.split(".", 1)[0]
        generic_id = f"{category}.generic"
        if generic_id in rec_map:
            return rec_map[generic_id]
    if finding_id.startswith("run_compare."):
        return rec_map.get("run_compare.generic", {})
    if finding_id.startswith("settings."):
        return rec_map.get("settings.generic", {})
    return rec_map.get("checkpoints.generic", {
        "id": "unknown",
        "title": "Unmapped finding",
        "recommendation": "Review finding manually.",
        "actions": [],
        "references": [],
    })


def _guc_for_settings_finding(finding: dict[str, Any], guc_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if finding.get("category") != "settings":
        return None
    setting_name = finding.get("message")
    if setting_name and setting_name in guc_map:
        return guc_map[setting_name]
    return None


def advise_findings(
    analysis: dict[str, Any],
    *,
    recommendations_path: Path | None = None,
    guc_guidance_path: Path | None = None,
) -> AdvisorReport:
    rec_map = load_recommendations(recommendations_path)
    guc_map = load_guc_guidance(guc_guidance_path)

    source_type = analysis.get("type", "unknown")
    findings = analysis.get("findings", [])
    advised: list[AdvisedFinding] = []

    for finding in findings:
        finding_id = finding.get("id", "unknown")
        advice = _resolve_advice(finding_id, rec_map)
        guc = _guc_for_settings_finding(finding, guc_map)
        advised.append(AdvisedFinding(finding=finding, advice=advice, guc_guidance=guc))

    critical = sum(
        1
        for item in advised
        if item.finding.get("severity") == "critical"
        or item.advice.get("severity") == "critical"
        or item.advice.get("severity") == "high"
    )

    meta = {k: v for k, v in analysis.items() if k not in {"findings", "summary"}}
    summary = {
        "total_findings": len(advised),
        "high_priority": critical,
        "source_summary": analysis.get("summary", {}),
    }

    return AdvisorReport(
        source_type=source_type,
        meta=meta,
        advised_findings=advised,
        summary=summary,
    )


def advisor_report_to_dict(report: AdvisorReport) -> dict[str, Any]:
    return {
        "type": "advisor_report",
        "source_type": report.source_type,
        "meta": report.meta,
        "summary": report.summary,
        "advised_findings": [
            {
                "finding": item.finding,
                "advice": item.advice,
                "guc_guidance": item.guc_guidance,
            }
            for item in report.advised_findings
        ],
    }


def build_brief(report: AdvisorReport, *, max_findings: int = 25) -> str:
    lines: list[str] = []
    lines.append("# pg_profile Analysis Brief")
    lines.append("")
    lines.append(f"Source type: `{report.source_type}`")
    lines.append("")

    if report.source_type == "health_check":
        meta = report.meta.get("report_meta", {})
        lines.append(f"- Server: {meta.get('server', '?')}")
        lines.append(f"- Interval: {meta.get('report_start')} .. {meta.get('report_end')} ({meta.get('interval_hours')} h)")
        lines.append(f"- Report: {meta.get('filename')}")
    elif report.source_type == "run_comparison":
        lines.append(f"- Run A [{report.meta.get('run_a', {}).get('run_id')}]: {report.meta.get('run_a', {}).get('interval_hours')} h")
        lines.append(f"- Run B [{report.meta.get('run_b', {}).get('run_id')}]: {report.meta.get('run_b', {}).get('interval_hours')} h")
        if report.meta.get("interval_mismatch"):
            lines.append(
                f"- **Interval mismatch:** {report.meta.get('interval_diff_hours')} h difference — use /hour values"
            )
    elif report.source_type == "settings_diff":
        lines.append(f"- Run A: {report.meta.get('run_a', {}).get('run_id')}")
        lines.append(f"- Run B: {report.meta.get('run_b', {}).get('run_id')}")

    lines.append("")
    lines.append(f"Total findings: {report.summary.get('total_findings', 0)}")
    lines.append("")

    shown = 0
    for item in report.advised_findings:
        if shown >= max_findings:
            lines.append(f"... and {len(report.advised_findings) - max_findings} more findings")
            break
        shown += 1
        f = item.finding
        advice = item.advice
        lines.append(f"## [{f.get('severity', 'warning').upper()}] {advice.get('title', f.get('id'))}")
        lines.append(f"- **ID:** `{f.get('id')}`")
        lines.append(f"- **Message:** {f.get('message')}")
        lines.append(f"- **Recommendation:** {advice.get('recommendation', '').strip()}")
        if advice.get("actions"):
            lines.append("- **Actions:**")
            for action in advice["actions"]:
                lines.append(f"  - {action}")
        if item.guc_guidance:
            guc = item.guc_guidance
            lines.append(f"- **GUC note:** {guc.get('note', '').strip()}")
            if guc.get("typical_oltp"):
                lines.append(f"- **Typical OLTP:** {guc.get('typical_oltp')}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_llm_prompt(brief: str) -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / "analyst.md"
    template = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
    return f"{template}\n\n---\n\n# DATA FOR ANALYSIS\n\n{brief}"
