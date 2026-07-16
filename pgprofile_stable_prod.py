"""Multi-report PROD stability analysis and GUC tuning recommendations."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import yaml

from pgprofile_advisor import load_guc_guidance, load_recommendations
from pgprofile_findings import health_check_to_dict, infer_rule_id
from pgprofile_health import ReportContext, Warning, load_report_data, load_thresholds, run_checks
from pgprofile_parser import load_settings

DEFAULT_TUNING = Path(__file__).resolve().parent / "knowledge" / "prod_tuning.yaml"

_SEVERITY_RANK = {"critical": 4, "high": 3, "warning": 2, "medium": 2, "low": 1, "info": 0}
_SAFETY_RANK = {"safe": 1, "cautious": 2, "risky": 3, "restart_required": 4}
_IMPACT_RANK = {"low": 1, "medium": 2, "high": 3}


@dataclass
class ProdReportSnapshot:
    label: str
    path: Path
    ctx: ReportContext
    warnings: list[Warning]
    findings: list[dict[str, Any]]


@dataclass
class StableFinding:
    rule_id: str
    category: str
    occurrence_count: int
    total_reports: int
    stability_ratio: float
    max_severity: str
    sample_messages: list[str]
    report_labels: list[str]


@dataclass
class GucTuningItem:
    guc: str
    direction: str
    change_safety: str
    change_impact: str
    reload: bool
    postgres_pro: str
    rationale: str
    current_values: dict[str, str] = field(default_factory=dict)


@dataclass
class TuningRecommendation:
    tuning_rule_id: str
    finding_rule_id: str
    title: str
    problem_severity: str
    stability_ratio: float
    occurrence_count: int
    total_reports: int
    stable_finding: StableFinding
    problem_advice: dict[str, Any]
    guc_items: list[GucTuningItem]
    operational: list[str]
    combined_safety: str
    combined_impact: str


@dataclass
class StableProdAnalysis:
    reports: list[ProdReportSnapshot]
    stable_findings: list[StableFinding]
    ephemeral_findings: list[StableFinding]
    recommendations: list[TuningRecommendation]
    settings_consensus: dict[str, str]
    min_stability_ratio: float


def load_prod_tuning(
    path: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str], dict[str, int]]:
    cfg_path = path or DEFAULT_TUNING
    with cfg_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    rules: dict[str, dict[str, Any]] = {}
    finding_to_rule: dict[str, str] = {}
    for rule in data.get("tuning_rules", []):
        rule_id = rule["id"]
        rules[rule_id] = rule
        for fid in rule.get("finding_ids", []):
            finding_to_rule[fid] = rule_id
    severity_order = data.get("severity_order", {})
    return rules, finding_to_rule, severity_order


def _severity_rank(severity: str, extra: dict[str, int] | None = None) -> int:
    base = dict(_SEVERITY_RANK)
    if extra:
        base.update(extra)
    return base.get(severity.lower(), 1)


def _max_severity(severities: list[str], extra: dict[str, int] | None = None) -> str:
    if not severities:
        return "warning"
    return max(severities, key=lambda s: _severity_rank(s, extra))


def load_prod_reports(
    paths: list[Path],
    labels: list[str] | None = None,
    *,
    thresholds_path: Path,
) -> list[ProdReportSnapshot]:
    cfg = load_thresholds(thresholds_path)
    snapshots: list[ProdReportSnapshot] = []
    for idx, path in enumerate(paths):
        label = labels[idx] if labels and idx < len(labels) else path.stem
        ctx = load_report_data(path)
        warnings = run_checks(ctx, cfg)
        health = health_check_to_dict(ctx, warnings)
        snapshots.append(
            ProdReportSnapshot(
                label=label,
                path=path,
                ctx=ctx,
                warnings=warnings,
                findings=health.get("findings", []),
            )
        )
    return snapshots


def aggregate_findings(
    reports: list[ProdReportSnapshot],
    *,
    min_stability_ratio: float = 1.0,
) -> tuple[list[StableFinding], list[StableFinding]]:
    total = len(reports)
    if total == 0:
        return [], []

    by_rule: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for snap in reports:
        seen_in_report: set[str] = set()
        for finding in snap.findings:
            rule_id = finding.get("id") or infer_rule_id_from_finding(finding)
            if rule_id in seen_in_report:
                continue
            seen_in_report.add(rule_id)
            by_rule.setdefault(rule_id, []).append((snap.label, finding))

    stable: list[StableFinding] = []
    ephemeral: list[StableFinding] = []

    min_count = max(1, int(total * min_stability_ratio + 0.999 - 1e-9))

    for rule_id, occurrences in sorted(by_rule.items()):
        count = len(occurrences)
        ratio = count / total
        severities = [f.get("severity", "warning") for _, f in occurrences]
        sf = StableFinding(
            rule_id=rule_id,
            category=occurrences[0][1].get("category", rule_id.split(".", 1)[0]),
            occurrence_count=count,
            total_reports=total,
            stability_ratio=round(ratio, 4),
            max_severity=_max_severity(severities),
            sample_messages=[f.get("message", "") for _, f in occurrences[:3]],
            report_labels=[label for label, _ in occurrences],
        )
        if count >= min_count:
            stable.append(sf)
        else:
            ephemeral.append(sf)

    stable.sort(key=lambda x: (_severity_rank(x.max_severity), -x.stability_ratio), reverse=True)
    ephemeral.sort(key=lambda x: -x.occurrence_count)
    return stable, ephemeral


def infer_rule_id_from_finding(finding: dict[str, Any]) -> str:
    if finding.get("id"):
        return finding["id"]
    return f"{finding.get('category', 'unknown')}.generic"


def _settings_consensus(reports: list[ProdReportSnapshot]) -> dict[str, str]:
    """Most common defined setting value across reports (mode)."""
    from collections import Counter

    counts: dict[str, Counter[str]] = {}
    for snap in reports:
        settings = load_settings(snap.path, defined_only=True)
        for name, value in settings.items():
            counts.setdefault(name, Counter())[value] += 1
    consensus: dict[str, str] = {}
    for name, counter in counts.items():
        consensus[name] = counter.most_common(1)[0][0]
    return consensus


def _resolve_tuning_rule(
    finding_id: str,
    tuning_rules: dict[str, dict[str, Any]],
    finding_to_rule: dict[str, str],
) -> dict[str, Any] | None:
    if finding_id in finding_to_rule:
        return tuning_rules.get(finding_to_rule[finding_id])
    category = finding_id.split(".", 1)[0] if "." in finding_id else finding_id
    generic = f"{category}.generic"
    if generic in finding_to_rule:
        return tuning_rules.get(finding_to_rule[generic])
    return None


def _combined_guc_safety(items: list[GucTuningItem]) -> str:
    if not items:
        return "safe"
    return max(items, key=lambda i: _SAFETY_RANK.get(i.change_safety, 2)).change_safety


def _combined_guc_impact(items: list[GucTuningItem]) -> str:
    if not items:
        return "low"
    return max(items, key=lambda i: _IMPACT_RANK.get(i.change_impact, 2)).change_impact


def build_tuning_recommendations(
    stable_findings: list[StableFinding],
    reports: list[ProdReportSnapshot],
    *,
    tuning_path: Path | None = None,
    recommendations_path: Path | None = None,
) -> list[TuningRecommendation]:
    tuning_rules, finding_to_rule, _ = load_prod_tuning(tuning_path)
    rec_map = load_recommendations(recommendations_path)
    guc_map = load_guc_guidance()
    consensus = _settings_consensus(reports)

    per_report_settings: list[tuple[str, dict[str, str]]] = []
    for snap in reports:
        per_report_settings.append(
            (snap.label, load_settings(snap.path, defined_only=False))
        )

    recommendations: list[TuningRecommendation] = []

    for sf in stable_findings:
        tune_rule = _resolve_tuning_rule(sf.rule_id, tuning_rules, finding_to_rule)
        problem_advice = rec_map.get(sf.rule_id) or rec_map.get(
            f"{sf.category}.generic", {}
        )
        problem_severity = (
            tune_rule.get("problem_severity")
            if tune_rule
            else problem_advice.get("severity", sf.max_severity)
        )

        guc_items: list[GucTuningItem] = []
        if tune_rule:
            for raw in tune_rule.get("guc_recommendations", []):
                guc_name = raw["guc"]
                current: dict[str, str] = {}
                for label, settings in per_report_settings:
                    if guc_name in settings:
                        current[label] = settings[guc_name]
                    elif guc_name in consensus:
                        current[label] = consensus[guc_name]
                guidance = guc_map.get(guc_name, {})
                postgres_pro = raw.get("postgres_pro", "").strip()
                if guidance.get("typical_oltp"):
                    postgres_pro += f"\nTypical OLTP: {guidance['typical_oltp']}"
                guc_items.append(
                    GucTuningItem(
                        guc=guc_name,
                        direction=raw.get("direction", "review"),
                        change_safety=raw.get("change_safety", "cautious"),
                        change_impact=raw.get("change_impact", "medium"),
                        reload=bool(raw.get("reload", False)),
                        postgres_pro=postgres_pro.strip(),
                        rationale=raw.get("rationale", "").strip(),
                        current_values=current,
                    )
                )

        operational = list(tune_rule.get("operational", [])) if tune_rule else []
        if problem_advice.get("actions"):
            operational.extend(problem_advice["actions"])

        recommendations.append(
            TuningRecommendation(
                tuning_rule_id=tune_rule["id"] if tune_rule else f"advise.{sf.rule_id}",
                finding_rule_id=sf.rule_id,
                title=(
                    tune_rule.get("title")
                    if tune_rule
                    else problem_advice.get("title", sf.rule_id)
                ),
                problem_severity=str(problem_severity),
                stability_ratio=sf.stability_ratio,
                occurrence_count=sf.occurrence_count,
                total_reports=sf.total_reports,
                stable_finding=sf,
                problem_advice=problem_advice,
                guc_items=guc_items,
                operational=_dedupe(operational),
                combined_safety=_combined_guc_safety(guc_items),
                combined_impact=_combined_guc_impact(guc_items),
            )
        )

    recommendations.sort(
        key=lambda r: (
            _severity_rank(r.problem_severity),
            -r.stability_ratio,
            _SAFETY_RANK.get(r.combined_safety, 2),
        ),
        reverse=True,
    )
    return _merge_recommendations_by_tuning_rule(recommendations)


def _merge_recommendations_by_tuning_rule(
    recommendations: list[TuningRecommendation],
) -> list[TuningRecommendation]:
    """Combine multiple stable findings that map to the same tuning rule."""
    by_rule: dict[str, TuningRecommendation] = {}
    order: list[str] = []

    for rec in recommendations:
        key = rec.tuning_rule_id
        if key not in by_rule:
            by_rule[key] = rec
            order.append(key)
            continue

        existing = by_rule[key]
        merged_labels = _dedupe(existing.stable_finding.report_labels + rec.stable_finding.report_labels)
        merged_messages = _dedupe(
            existing.stable_finding.sample_messages + rec.stable_finding.sample_messages
        )
        existing.stable_finding = StableFinding(
            rule_id=", ".join(
                _dedupe([existing.finding_rule_id, rec.finding_rule_id])
            ),
            category=existing.stable_finding.category,
            occurrence_count=max(
                existing.stable_finding.occurrence_count,
                rec.stable_finding.occurrence_count,
            ),
            total_reports=existing.stable_finding.total_reports,
            stability_ratio=min(existing.stability_ratio, rec.stability_ratio),
            max_severity=_max_severity(
                [existing.stable_finding.max_severity, rec.stable_finding.max_severity]
            ),
            sample_messages=merged_messages[:5],
            report_labels=merged_labels,
        )
        existing.finding_rule_id = existing.stable_finding.rule_id
        existing.problem_severity = _max_severity(
            [existing.problem_severity, rec.problem_severity]
        )
        existing.operational = _dedupe(existing.operational + rec.operational)

    return [by_rule[k] for k in order]


def _normalize_action(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        parts = [f"{k}: {v}" for k, v in item.items()]
        return "; ".join(parts).strip()
    return str(item).strip()


def _dedupe(items: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = _normalize_action(item)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def analyze_stable_prod(
    paths: list[Path],
    *,
    labels: list[str] | None = None,
    thresholds_path: Path,
    min_stability_ratio: float = 1.0,
    tuning_path: Path | None = None,
) -> StableProdAnalysis:
    reports = load_prod_reports(paths, labels, thresholds_path=thresholds_path)
    stable, ephemeral = aggregate_findings(reports, min_stability_ratio=min_stability_ratio)
    recommendations = build_tuning_recommendations(
        stable, reports, tuning_path=tuning_path
    )
    return StableProdAnalysis(
        reports=reports,
        stable_findings=stable,
        ephemeral_findings=ephemeral,
        recommendations=recommendations,
        settings_consensus=_settings_consensus(reports),
        min_stability_ratio=min_stability_ratio,
    )


def _safety_label(safety: str) -> str:
    labels = {
        "safe": "БЕЗОПАСНО (reload)",
        "cautious": "ОСТОРОЖНО (тест + мониторинг)",
        "risky": "РИСК (может ухудшить нагрузку)",
        "restart_required": "RESTART (плановое окно)",
    }
    return labels.get(safety, safety)


def _impact_label(impact: str) -> str:
    labels = {"low": "низкое", "medium": "среднее", "high": "высокое"}
    return labels.get(impact, impact)


def _severity_badge(severity: str) -> str:
    return severity.upper()


def print_stable_prod_report(
    analysis: StableProdAnalysis,
    *,
    show_ephemeral: bool = False,
    out: TextIO | None = None,
) -> None:
    stream = out or sys.stdout
    total = len(analysis.reports)

    print("pg_profile stable PROD analysis", file=stream)
    print(f"Reports: {total} | min stability: {analysis.min_stability_ratio:.0%}", file=stream)
    print(file=stream)

    for snap in analysis.reports:
        props = snap.ctx.properties
        print(
            f"  [{snap.label}] {snap.path.name} | "
            f"{props.get('report_start1', '?')} .. {props.get('report_end1', '?')} "
            f"({snap.ctx.interval_hours:.1f} h) | "
            f"findings={len(snap.findings)}",
            file=stream,
        )
    print(file=stream)

    print(
        f"== Стабильные проблемы ({len(analysis.stable_findings)} — "
        f"во всех ≥{analysis.min_stability_ratio:.0%} отчётов) ==",
        file=stream,
    )
    if not analysis.stable_findings:
        print("  Стабильных проблем не найдено.", file=stream)
    print(file=stream)

    for rec in analysis.recommendations:
        sf = rec.stable_finding
        print(
            f"--- [{_severity_badge(rec.problem_severity)}] {rec.title} ---",
            file=stream,
        )
        print(
            f"  Finding: {rec.finding_rule_id} | "
            f"stable {sf.occurrence_count}/{sf.total_reports} "
            f"({sf.stability_ratio:.0%}) | reports: {', '.join(sf.report_labels)}",
            file=stream,
        )
        if sf.sample_messages:
            print(f"  Example: {sf.sample_messages[0][:120]}", file=stream)

        advice = rec.problem_advice.get("recommendation", "")
        if advice:
            for line in advice.strip().splitlines()[:3]:
                print(f"  Problem: {line.strip()}", file=stream)

        if rec.guc_items:
            print(
                f"  GUC tuning | change safety: {_safety_label(rec.combined_safety)} | "
                f"potential impact: {_impact_label(rec.combined_impact)}",
                file=stream,
            )
            for guc in rec.guc_items:
                cur = ", ".join(f"{k}={v}" for k, v in guc.current_values.items()) or "—"
                print(
                    f"    • {guc.guc} → {guc.direction} | "
                    f"[{_safety_label(guc.change_safety)}] "
                    f"[impact: {_impact_label(guc.change_impact)}]",
                    file=stream,
                )
                print(f"      Current: {cur}", file=stream)
                if guc.rationale:
                    print(f"      Why: {guc.rationale}", file=stream)
                if guc.postgres_pro:
                    first_line = guc.postgres_pro.splitlines()[0]
                    print(f"      Postgres Pro: {first_line}", file=stream)
        else:
            print("  GUC tuning: нет (операционные действия ниже)", file=stream)

        if rec.operational:
            print("  Actions:", file=stream)
            for action in rec.operational[:6]:
                print(f"    - {action}", file=stream)
        print(file=stream)

    if show_ephemeral and analysis.ephemeral_findings:
        print(f"== Нестабильные (не во всех отчётах) ({len(analysis.ephemeral_findings)}) ==", file=stream)
        for ef in analysis.ephemeral_findings[:20]:
            print(
                f"  [{ef.max_severity}] {ef.rule_id}: "
                f"{ef.occurrence_count}/{ef.total_reports} — {', '.join(ef.report_labels)}",
                file=stream,
            )
        print(file=stream)

    print(
        f"Summary: {len(analysis.stable_findings)} stable findings, "
        f"{len(analysis.recommendations)} tuning recommendations, "
        f"{len(analysis.ephemeral_findings)} ephemeral",
        file=stream,
    )


def build_stable_prod_brief(
    analysis: StableProdAnalysis,
    *,
    max_recommendations: int = 25,
    max_ephemeral: int = 10,
) -> str:
    """Text brief for LLM / Confluence prompt (stable PROD tuning)."""
    lines: list[str] = [
        "# Stable PROD Analysis Brief",
        "",
        f"min_stability_ratio: {analysis.min_stability_ratio}",
        f"report_count: {len(analysis.reports)}",
        f"stable_findings_count: {len(analysis.stable_findings)}",
        f"tuning_recommendations_count: {len(analysis.recommendations)}",
        f"ephemeral_findings_count: {len(analysis.ephemeral_findings)}",
        "",
        "## Reports",
    ]
    for snap in analysis.reports:
        props = snap.ctx.properties
        lines.append(f"- {snap.label}: {snap.path.name}")
        lines.append(f"  server: {props.get('server_name') or '?'}")
        lines.append(
            f"  interval: {props.get('report_start1')} .. {props.get('report_end1')} "
            f"({snap.ctx.interval_hours:.1f} h)"
        )
        lines.append(f"  findings_in_report: {len(snap.findings)}")
    lines.append("")

    if analysis.recommendations:
        lines.append("## Stable tuning recommendations (sorted by problem severity)")
        for rec in analysis.recommendations[:max_recommendations]:
            sf = rec.stable_finding
            lines.append(f"### [{rec.problem_severity}] {rec.title}")
            lines.append(f"- tuning_rule_id: {rec.tuning_rule_id}")
            lines.append(f"- finding_ids: {rec.finding_rule_id}")
            lines.append(
                f"- stability: {sf.occurrence_count}/{sf.total_reports} "
                f"({sf.stability_ratio:.0%}) in {', '.join(sf.report_labels)}"
            )
            lines.append(
                f"- change_safety: {rec.combined_safety} | change_impact: {rec.combined_impact}"
            )
            if sf.sample_messages:
                lines.append(f"- example: {sf.sample_messages[0][:200]}")
            advice = rec.problem_advice.get("recommendation", "")
            if advice:
                lines.append(f"- problem: {advice.strip().splitlines()[0][:200]}")
            for guc in rec.guc_items:
                cur = ", ".join(f"{k}={v}" for k, v in guc.current_values.items()) or "—"
                lines.append(
                    f"- GUC `{guc.guc}` → {guc.direction} "
                    f"[safety={guc.change_safety}, impact={guc.change_impact}] current: {cur}"
                )
                if guc.rationale:
                    lines.append(f"  rationale: {guc.rationale[:180]}")
                if guc.postgres_pro:
                    lines.append(f"  postgres_pro: {guc.postgres_pro.splitlines()[0][:180]}")
            if rec.operational:
                lines.append("- actions:")
                for action in rec.operational[:5]:
                    lines.append(f"  - {action}")
            lines.append("")
        if len(analysis.recommendations) > max_recommendations:
            lines.append(f"- ... and {len(analysis.recommendations) - max_recommendations} more")
            lines.append("")

    if analysis.ephemeral_findings:
        lines.append("## Report-specific findings (not in all reports)")
        partial = [ef for ef in analysis.ephemeral_findings if ef.occurrence_count > 1]
        single = [ef for ef in analysis.ephemeral_findings if ef.occurrence_count == 1]
        if partial:
            lines.append("### In some reports (not all)")
            for ef in partial[:max_ephemeral]:
                lines.append(
                    f"- [{ef.max_severity}] {ef.rule_id}: "
                    f"{ef.occurrence_count}/{ef.total_reports} — {', '.join(ef.report_labels)}"
                )
        if single:
            lines.append("### Only in one report")
            by_label: dict[str, list[StableFinding]] = {}
            for ef in single:
                for label in ef.report_labels:
                    by_label.setdefault(label, []).append(ef)
            shown = 0
            for label in sorted(by_label):
                lines.append(f"#### {label}")
                for ef in by_label[label]:
                    if shown >= max_ephemeral:
                        break
                    msg = ef.sample_messages[0][:160] if ef.sample_messages else ""
                    lines.append(f"- [{ef.max_severity}] {ef.rule_id}: {msg}")
                    shown += 1
                if shown >= max_ephemeral:
                    break
        if len(analysis.ephemeral_findings) > max_ephemeral:
            lines.append(f"- ... and more report-specific findings")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def stable_prod_to_dict(analysis: StableProdAnalysis) -> dict[str, Any]:
    from pgprofile_findings import _json_safe

    return {
        "type": "stable_prod_analysis",
        "min_stability_ratio": analysis.min_stability_ratio,
        "reports": [
            {
                "label": s.label,
                "path": str(s.path),
                "filename": s.path.name,
                "interval_hours": s.ctx.interval_hours,
                "findings_count": len(s.findings),
            }
            for s in analysis.reports
        ],
        "stable_findings": [_json_safe(sf) for sf in analysis.stable_findings],
        "ephemeral_findings": [_json_safe(ef) for ef in analysis.ephemeral_findings],
        "recommendations": [
            {
                "tuning_rule_id": r.tuning_rule_id,
                "finding_rule_id": r.finding_rule_id,
                "title": r.title,
                "problem_severity": r.problem_severity,
                "stability_ratio": r.stability_ratio,
                "combined_change_safety": r.combined_safety,
                "combined_change_impact": r.combined_impact,
                "guc_items": [_json_safe(g) for g in r.guc_items],
                "operational": r.operational,
                "problem_advice": r.problem_advice,
                "stable_finding": _json_safe(r.stable_finding),
            }
            for r in analysis.recommendations
        ],
        "summary": {
            "report_count": len(analysis.reports),
            "stable_count": len(analysis.stable_findings),
            "recommendation_count": len(analysis.recommendations),
            "ephemeral_count": len(analysis.ephemeral_findings),
        },
    }
