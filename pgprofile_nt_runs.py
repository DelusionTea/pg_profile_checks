"""Multi-run NT analysis: symptoms + run comparison + GUC change impact inference."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from compare_settings import DiffRow, DiffStatus, diff_settings
from pgprofile_classify import split_settings_rows
from pgprofile_compare import compare_runs, load_run
from pgprofile_findings import run_comparison_to_dict
from pgprofile_health import parse_setting_int
from pgprofile_parser import load_settings, parse_report_meta
from pgprofile_nt_prod import NtProdValidation, nt_prod_validation_to_dict, validate_nt_prod
from pgprofile_symptoms import (
    SYMPTOM_TITLES,
    SymptomInvestigation,
    investigate_symptom,
    normalize_symptom,
    symptom_investigation_to_dict,
)

DEFAULT_GUC_IMPACT = Path(__file__).resolve().parent / "knowledge" / "guc_impact.yaml"


@dataclass
class GucChangeImpact:
    guc: str
    value_from: str
    value_to: str
    direction: str
    likely_effects: list[str] = field(default_factory=list)
    correlated_metrics: list[dict[str, Any]] = field(default_factory=list)
    confidence: str = "possible"  # likely | possible | weak


@dataclass
class RunPairAnalysis:
    run_a_label: str
    run_b_label: str
    run_a_path: Path
    run_b_path: Path
    settings_changes: list[DiffRow]
    guc_impacts: list[GucChangeImpact]
    compare_summary: dict[str, Any]
    narrative: str


@dataclass
class NtRunsAnalysis:
    symptoms: list[str]
    symptom_investigations: list[SymptomInvestigation]
    pair_analyses: list[RunPairAnalysis]
    report_labels: list[str]
    report_paths: list[Path]
    prod_labels: list[str] = field(default_factory=list)
    prod_paths: list[Path] = field(default_factory=list)
    prod_symptom_investigations: list[SymptomInvestigation] = field(default_factory=list)
    nt_prod_validations: list[NtProdValidation] = field(default_factory=list)
    problem_overlap: dict[str, Any] = field(default_factory=dict)


def load_guc_impact(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or DEFAULT_GUC_IMPACT
    with cfg_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data.get("guc_impact", {})


def parse_symptom_list(raw: list[str] | str) -> list[str]:
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    else:
        parts = []
        for item in raw:
            parts.extend(p.strip() for p in item.replace(",", " ").split() if p.strip())
    if not parts:
        raise ValueError("at least one symptom is required (high_cpu, high_wal, high_memory, slow_query)")
    return [normalize_symptom(p) for p in parts]


def _setting_direction(old: str | None, new: str | None) -> str:
    if old is None or new is None:
        return "changed"
    old_l = old.strip().lower()
    new_l = new.strip().lower()
    if old_l in {"off", "false"} and new_l in {"on", "true"}:
        return "enabled"
    if old_l in {"on", "true"} and new_l in {"off", "false"}:
        return "disabled"
    old_i = parse_setting_int(old)
    new_i = parse_setting_int(new)
    if old_i is not None and new_i is not None:
        if new_i > old_i:
            return "increased"
        if new_i < old_i:
            return "decreased"
    return "changed"


def _format_guc_value(guc: str, value: str | None) -> str:
    if value is None:
        return "—"
    if guc in {"max_wal_size", "wal_buffers", "shared_buffers", "effective_cache_size"}:
        pages = parse_setting_int(value)
        if pages is not None:
            return f"{value} ({pages * 8 / 1024:.1f} MB)"
    if guc == "checkpoint_completion_target":
        return value
    return value


def _metric_improved(metric_key: str, delta: float | None, direction: str) -> bool | None:
    if delta is None:
        return None
    improves_on_increase = {
        "checkpoints_req",
        "checkpoint_write_time",
        "checkpoint_sync_time",
        "wal_buffers_full",
        "wal_bytes",
        "wal_size",
        "blks_read",
        "blk_read_time",
        "blk_write_time",
        "idle_in_transaction_time",
        "temp_blks_written",
    }
    improves_on_decrease: set[str] = set()
    pct_metrics = {"blks_hit_pct"}

    if metric_key in pct_metrics:
        if direction in ("increased", "enabled"):
            return delta > 0
        if direction in ("decreased", "disabled"):
            return delta < 0
        return None

    if metric_key in improves_on_increase:
        if direction in ("increased", "enabled"):
            return delta < 0
        if direction in ("decreased", "disabled"):
            return delta > 0
    if metric_key in improves_on_decrease:
        if direction in ("increased", "enabled"):
            return delta > 0
        if direction in ("decreased", "disabled"):
            return delta < 0
    return None


def _find_metric_delta(compare_dict: dict[str, Any], metric_key: str) -> dict[str, Any] | None:
    suffix = f".{metric_key}"
    for finding in compare_dict.get("findings", []):
        fid = finding.get("id", "")
        if fid.endswith(suffix) or finding.get("message") == metric_key:
            return finding.get("details", {})
    return None


def infer_guc_impacts(
    settings_changes: list[DiffRow],
    compare_dict: dict[str, Any],
    *,
    symptoms: list[str],
    guc_impact_cfg: dict[str, Any] | None = None,
) -> list[GucChangeImpact]:
    cfg = guc_impact_cfg or load_guc_impact()
    impacts: list[GucChangeImpact] = []

    for row in settings_changes:
        if row.status is DiffStatus.DIFFER:
            old_val, new_val = row.nt_value, row.prod_value
        elif row.status is DiffStatus.ONLY_PROD:
            old_val, new_val = None, row.prod_value
        elif row.status is DiffStatus.ONLY_NT:
            old_val, new_val = row.nt_value, None
        else:
            continue

        guc = row.name
        rule = cfg.get(guc)
        if not rule:
            continue

        direction = _setting_direction(old_val, new_val)
        if symptoms and rule.get("symptoms"):
            if not any(s in rule["symptoms"] for s in symptoms):
                continue

        narrative_key = {
            "increased": "narrative_increase",
            "decreased": "narrative_decrease",
            "enabled": "narrative_enable",
            "disabled": "narrative_disable",
        }.get(direction, "narrative_increase")

        likely: list[str] = []
        base_narrative = (rule.get(narrative_key) or rule.get("narrative_increase") or "").strip()
        if base_narrative:
            likely.append(base_narrative.splitlines()[0])

        correlated: list[dict[str, Any]] = []
        improved_count = 0
        checked_count = 0
        for metric_key in rule.get("metrics", []):
            details = _find_metric_delta(compare_dict, metric_key)
            if not details:
                continue
            delta = details.get("delta")
            delta_pct = details.get("delta_pct")
            improved = _metric_improved(metric_key, delta, direction)
            if improved is not None:
                checked_count += 1
                if improved:
                    improved_count += 1
            correlated.append(
                {
                    "metric": metric_key,
                    "delta": delta,
                    "delta_pct": delta_pct,
                    "per_hour_a": details.get("per_hour_a"),
                    "per_hour_b": details.get("per_hour_b"),
                    "improved": improved,
                }
            )

        confidence = "possible"
        if checked_count and improved_count == checked_count:
            confidence = "likely"
        elif checked_count and improved_count > 0:
            confidence = "possible"
        elif checked_count and improved_count == 0:
            confidence = "weak"

        if correlated:
            parts = []
            for c in correlated[:4]:
                pct = c.get("delta_pct")
                if pct is not None:
                    parts.append(f"{c['metric']} {pct:+.1f}%")
                elif c.get("delta") is not None:
                    parts.append(f"{c['metric']} Δ{c['delta']}")
            if parts:
                likely.append(
                    f"В этом сравнении: {', '.join(parts)} — "
                    + (
                        "направление согласуется с ожидаемым эффектом настройки"
                        if confidence in ("likely", "possible")
                        else "эффект настройки не подтверждается метриками (возможна доминирующая нагрузка приложения)"
                    )
                )

        impacts.append(
            GucChangeImpact(
                guc=guc,
                value_from=_format_guc_value(guc, old_val),
                value_to=_format_guc_value(guc, new_val),
                direction=direction,
                likely_effects=likely,
                correlated_metrics=correlated,
                confidence=confidence,
            )
        )

    return impacts


def _build_pair_narrative(
    label_a: str,
    label_b: str,
    guc_impacts: list[GucChangeImpact],
    compare_dict: dict[str, Any],
) -> str:
    if not guc_impacts:
        critical = compare_dict.get("summary", {}).get("significant_count", 0)
        if critical:
            return (
                f"Между прогонами {label_a} → {label_b} значимые изменения метрик есть "
                f"({critical} показателей), но критичных изменений Defined settings не обнаружено — "
                "рост симптомов скорее связан с нагрузкой приложения/SQL, а не с GUC."
            )
        return f"Между прогонами {label_a} → {label_b} критичных изменений настроек и значимых метрик не выявлено."

    lines = [
        f"Между прогонами {label_a} → {label_b} изменены настройки; "
        "ниже — вероятное влияние на метрики (корреляция, не доказательство причинности):"
    ]
    for impact in guc_impacts:
        lines.append(
            f"- {impact.guc}: {impact.value_from} → {impact.value_to} ({impact.direction}); "
            f"уверенность: {impact.confidence}"
        )
        for effect in impact.likely_effects:
            lines.append(f"  • {effect}")
    return "\n".join(lines)


def _problem_keys_by_status(inv: SymptomInvestigation, statuses: set[str]) -> set[str]:
    return {
        c.cause_id
        for c in inv.causes
        if c.status.value in statuses
    }


def _compute_problem_overlap(
    nt_investigations: list[SymptomInvestigation],
    prod_investigations: list[SymptomInvestigation],
) -> dict[str, Any]:
    by_symptom: dict[str, Any] = {}
    prod_by_symptom = {inv.symptom: inv for inv in prod_investigations}
    for nt_inv in nt_investigations:
        prod_inv = prod_by_symptom.get(nt_inv.symptom)
        nt_confirmed = _problem_keys_by_status(nt_inv, {"confirmed"})
        nt_suspected = _problem_keys_by_status(nt_inv, {"suspected"})
        prod_confirmed = _problem_keys_by_status(prod_inv, {"confirmed"}) if prod_inv else set()
        prod_suspected = _problem_keys_by_status(prod_inv, {"suspected"}) if prod_inv else set()

        existing_on_prod = sorted((nt_confirmed | nt_suspected) & (prod_confirmed | prod_suspected))
        nt_only = sorted((nt_confirmed | nt_suspected) - (prod_confirmed | prod_suspected))
        prod_only = sorted((prod_confirmed | prod_suspected) - (nt_confirmed | nt_suspected))
        critical_nt_only = sorted(nt_confirmed - (prod_confirmed | prod_suspected))

        criticality = "low"
        if critical_nt_only:
            criticality = "high"
        elif nt_only:
            criticality = "medium"

        by_symptom[nt_inv.symptom] = {
            "existing_on_prod": existing_on_prod,
            "nt_only": nt_only,
            "prod_only": prod_only,
            "critical_nt_only": critical_nt_only,
            "divergence_criticality": criticality,
        }
    return by_symptom


def analyze_run_pair(
    path_a: Path,
    path_b: Path,
    label_a: str,
    label_b: str,
    *,
    symptoms: list[str],
    min_change_pct: float = 5.0,
    top_n: int = 15,
    guc_impact_path: Path | None = None,
) -> RunPairAnalysis:
    run_a = load_run(path_a, label_a)
    run_b = load_run(path_b, label_b)
    result = compare_runs(run_a, run_b, min_change_pct=min_change_pct, top_n=top_n)
    compare_dict = run_comparison_to_dict(run_a, run_b, result, min_change_pct=min_change_pct)

    settings_a = load_settings(path_a, defined_only=True)
    settings_b = load_settings(path_b, defined_only=True)
    all_diffs = diff_settings(settings_a, settings_b)
    critical_rows, _ = split_settings_rows(all_diffs)
    settings_changes = [
        r
        for r in critical_rows
        if r.status in (DiffStatus.DIFFER, DiffStatus.ONLY_PROD, DiffStatus.ONLY_NT)
    ]

    guc_cfg = load_guc_impact(guc_impact_path)
    guc_impacts = infer_guc_impacts(
        settings_changes,
        compare_dict,
        symptoms=symptoms,
        guc_impact_cfg=guc_cfg,
    )
    narrative = _build_pair_narrative(label_a, label_b, guc_impacts, compare_dict)

    return RunPairAnalysis(
        run_a_label=label_a,
        run_b_label=label_b,
        run_a_path=path_a,
        run_b_path=path_b,
        settings_changes=settings_changes,
        guc_impacts=guc_impacts,
        compare_summary=compare_dict.get("summary", {}),
        narrative=narrative,
    )


def analyze_nt_runs(
    report_paths: list[Path],
    *,
    labels: list[str] | None = None,
    prod_paths: list[Path] | None = None,
    prod_labels: list[str] | None = None,
    symptoms: list[str] | str,
    playbook_path: Path | None = None,
    health_thresholds_path: Path | None = None,
    guc_impact_path: Path | None = None,
    min_change_pct: float = 5.0,
    top_n: int = 15,
    query_target: Any | None = None,
) -> NtRunsAnalysis:
    if len(report_paths) < 2:
        raise ValueError("analyze_nt_runs requires at least two reports")

    symptom_keys = parse_symptom_list(symptoms)
    resolved_labels = [
        labels[i] if labels and i < len(labels) else report_paths[i].stem
        for i in range(len(report_paths))
    ]
    if labels and len(labels) != len(report_paths):
        raise ValueError("labels count must match reports count")
    if prod_labels and not prod_paths:
        raise ValueError("prod_labels requires prod_paths")
    if prod_paths and prod_labels and len(prod_paths) != len(prod_labels):
        raise ValueError("prod_labels count must match prod_paths count")

    investigations: list[SymptomInvestigation] = []
    for symptom in symptom_keys:
        inv = investigate_symptom(
            symptom,
            report_paths,
            labels=resolved_labels,
            query_target=query_target,
            playbook_path=playbook_path,
            health_thresholds_path=health_thresholds_path,
        )
        investigations.append(inv)

    pair_analyses: list[RunPairAnalysis] = []
    for i in range(len(report_paths) - 1):
        pair_analyses.append(
            analyze_run_pair(
                report_paths[i],
                report_paths[i + 1],
                resolved_labels[i],
                resolved_labels[i + 1],
                symptoms=symptom_keys,
                min_change_pct=min_change_pct,
                top_n=top_n,
                guc_impact_path=guc_impact_path,
            )
        )

    resolved_prod_paths = prod_paths or []
    resolved_prod_labels = [
        prod_labels[i] if prod_labels and i < len(prod_labels) else resolved_prod_paths[i].stem
        for i in range(len(resolved_prod_paths))
    ]
    prod_investigations: list[SymptomInvestigation] = []
    nt_prod_validations: list[NtProdValidation] = []
    overlap: dict[str, Any] = {}

    if resolved_prod_paths:
        for symptom in symptom_keys:
            prod_investigations.append(
                investigate_symptom(
                    symptom,
                    resolved_prod_paths,
                    labels=resolved_prod_labels,
                    query_target=query_target,
                    playbook_path=playbook_path,
                    health_thresholds_path=health_thresholds_path,
                )
            )
        overlap = _compute_problem_overlap(investigations, prod_investigations)

        # Evaluate how critically each NT run diverges from PROD baseline.
        for nt_path, nt_label in zip(report_paths, resolved_labels):
            for prod_path, prod_label in zip(resolved_prod_paths, resolved_prod_labels):
                nt_prod_validations.append(
                    validate_nt_prod(
                        nt_path,
                        prod_path,
                        min_change_pct=min_change_pct,
                        top_n=top_n,
                        nt_label=nt_label,
                        prod_label=prod_label,
                    )
                )

    return NtRunsAnalysis(
        symptoms=symptom_keys,
        symptom_investigations=investigations,
        pair_analyses=pair_analyses,
        report_labels=resolved_labels,
        report_paths=report_paths,
        prod_labels=resolved_prod_labels,
        prod_paths=resolved_prod_paths,
        prod_symptom_investigations=prod_investigations,
        nt_prod_validations=nt_prod_validations,
        problem_overlap=overlap,
    )


def nt_runs_to_dict(analysis: NtRunsAnalysis) -> dict[str, Any]:
    return {
        "type": "nt_runs_analysis",
        "symptoms": analysis.symptoms,
        "reports": [
            {
                "label": label,
                "path": str(path),
                "filename": path.name,
                "meta": parse_report_meta(path),
            }
            for label, path in zip(analysis.report_labels, analysis.report_paths)
        ],
        "prod_reports": [
            {
                "label": label,
                "path": str(path),
                "filename": path.name,
                "meta": parse_report_meta(path),
            }
            for label, path in zip(analysis.prod_labels, analysis.prod_paths)
        ],
        "symptom_investigations": [
            symptom_investigation_to_dict(inv) for inv in analysis.symptom_investigations
        ],
        "prod_symptom_investigations": [
            symptom_investigation_to_dict(inv) for inv in analysis.prod_symptom_investigations
        ],
        "problem_overlap": analysis.problem_overlap,
        "nt_prod_validations": [nt_prod_validation_to_dict(v) for v in analysis.nt_prod_validations],
        "pair_analyses": [
            {
                "run_a": pa.run_a_label,
                "run_b": pa.run_b_label,
                "compare_summary": pa.compare_summary,
                "narrative": pa.narrative,
                "settings_changes": [
                    {
                        "guc": row.name,
                        "value_from": row.nt_value,
                        "value_to": row.prod_value,
                    }
                    for row in pa.settings_changes
                ],
                "guc_impacts": [
                    {
                        "guc": gi.guc,
                        "value_from": gi.value_from,
                        "value_to": gi.value_to,
                        "direction": gi.direction,
                        "confidence": gi.confidence,
                        "likely_effects": gi.likely_effects,
                        "correlated_metrics": gi.correlated_metrics,
                    }
                    for gi in pa.guc_impacts
                ],
            }
            for pa in analysis.pair_analyses
        ],
    }


def build_nt_runs_brief(analysis: NtRunsAnalysis) -> str:
    lines = [
        "# NT Multi-Run Analysis Brief",
        "",
        f"symptoms: {', '.join(analysis.symptoms)}",
        f"reports: {', '.join(analysis.report_labels)}",
        "",
        "## Reports",
    ]
    for label, path in zip(analysis.report_labels, analysis.report_paths):
        meta = parse_report_meta(path)
        lines.append(f"- {label}: {path.name} ({meta.get('from')} .. {meta.get('to')})")
    lines.append("")

    if analysis.prod_paths:
        lines.append("## PROD baseline reports")
        for label, path in zip(analysis.prod_labels, analysis.prod_paths):
            meta = parse_report_meta(path)
            lines.append(f"- {label}: {path.name} ({meta.get('from')} .. {meta.get('to')})")
        lines.append("")

    for inv in analysis.symptom_investigations:
        lines.append(f"## Symptom: {inv.symptom_title} ({inv.symptom})")
        confirmed = [c for c in inv.causes if c.status.value == "confirmed"]
        suspected = [c for c in inv.causes if c.status.value == "suspected"]
        if confirmed:
            lines.append("### Confirmed causes")
            for c in confirmed[:8]:
                lines.append(f"- [{c.cause_id}] {c.title}")
                for ev in c.evidence[:3]:
                    lines.append(f"  - {ev}")
        if suspected:
            lines.append("### Suspected causes")
            for c in suspected[:8]:
                lines.append(f"- [{c.cause_id}] {c.title}")
                for ev in c.evidence[:2]:
                    lines.append(f"  - {ev}")
        lines.append("")

    lines.append("## Settings change impact (pairwise)")
    for pa in analysis.pair_analyses:
        lines.append(f"### {pa.run_a_label} → {pa.run_b_label}")
        lines.append(pa.narrative)
        lines.append("")

    if analysis.problem_overlap:
        lines.append("## NT vs PROD problem overlap")
        for symptom, payload in analysis.problem_overlap.items():
            lines.append(f"### {SYMPTOM_TITLES.get(symptom, symptom)}")
            lines.append(f"- divergence_criticality: {payload.get('divergence_criticality')}")
            lines.append(f"- existing_on_prod: {', '.join(payload.get('existing_on_prod', [])) or 'none'}")
            lines.append(f"- nt_only: {', '.join(payload.get('nt_only', [])) or 'none'}")
            lines.append(f"- critical_nt_only: {', '.join(payload.get('critical_nt_only', [])) or 'none'}")
            lines.append("")

    if analysis.nt_prod_validations:
        lines.append("## NT vs PROD divergence summary")
        for v in analysis.nt_prod_validations:
            lines.append(
                f"- {v.run_nt.run_id} vs {v.run_prod.run_id}: "
                f"settings_valid={str(v.settings.valid).lower()}, "
                f"performance_warnings={v.warning_count}, "
                f"critical_settings={v.settings.critical_count}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_nt_runs_confluence_wiki(analysis: NtRunsAnalysis, *, page_title: str | None = None) -> str:
    from pgprofile_confluence import (
        _checklist_from_symptom_causes,
        _wiki_actions_section,
        _wiki_anchor,
        _wiki_checklist_table,
        _wiki_expand,
        _wiki_findings_summary_table,
        _wiki_panel,
        _wiki_toc,
        explain_analyze_wiki_for_symptom,
    )

    title = page_title or "НТ: анализ прогонов и влияние настроек"
    symptom_titles = ", ".join(SYMPTOM_TITLES.get(s, s) for s in analysis.symptoms)

    confirmed = 0
    suspected = 0
    finding_rows: list[tuple[str, str, str, str]] = []
    actions: list[str] = []
    checklist: list[tuple[str, str]] = []
    for inv in analysis.symptom_investigations:
        checklist.extend(_checklist_from_symptom_causes(inv.causes))
        for cause in inv.causes:
            if cause.status.value == "confirmed":
                confirmed += 1
            elif cause.status.value == "suspected":
                suspected += 1
            if cause.status.value in ("confirmed", "suspected") or cause.evidence:
                finding_rows.append(
                    (
                        "critical" if cause.status.value == "confirmed" else "warning",
                        cause.cause_id,
                        cause.title,
                        ", ".join(cause.reports_matched) or "—",
                    )
                )
        for step in inv.action_plan[:4]:
            if step not in actions:
                actions.append(step)

    fail_n = sum(1 for row in checklist if row[1] == "FAIL")
    suspect_n = sum(1 for row in checklist if row[1] == "SUSPECT")
    pass_n = sum(1 for row in checklist if row[1] == "PASS")
    guc_changed = sum(1 for pa in analysis.pair_analyses if pa.guc_impacts)
    verdict_body = [
        f"Симптомы: *{symptom_titles}*.",
        f"Чеклист гипотез: FAIL *{fail_n}* · SUSPECT *{suspect_n}* · PASS *{pass_n}*.",
        f"Прогонов НТ: *{len(analysis.report_labels)}*; пар с изменением GUC: *{guc_changed}*.",
        f"Confirmed / Suspected гипотез: *{confirmed}* / *{suspected}*.",
        "Сначала — влияние GUC и действия; детали симптомов — в expand ниже.",
    ]

    lines: list[str] = [f"h1. {title}", ""]
    lines.extend(_wiki_panel("warning" if confirmed else "info", "Краткий вердикт", verdict_body))
    lines.extend(_wiki_checklist_table(checklist, heading="Чеклист гипотез"))
    lines.extend(_wiki_toc())
    lines.extend(_wiki_actions_section(actions[:8]))

    # GUC impact first (cross-run)
    guc_body: list[str] = []
    for pa in analysis.pair_analyses:
        guc_body.append(f"h3. {pa.run_a_label} → {pa.run_b_label}")
        if not pa.guc_impacts:
            guc_body.append("{note:title=Настройки}")
            guc_body.append(pa.narrative)
            guc_body.append("{note}")
        else:
            guc_body.append("{info:title=Изменённые GUC и вероятный эффект}")
            for gi in pa.guc_impacts:
                guc_body.append(
                    f"* *{{{gi.guc}}}*: {gi.value_from} → {gi.value_to} "
                    f"({gi.direction}, уверенность: {gi.confidence})"
                )
                for effect in gi.likely_effects:
                    guc_body.append(f"** {effect}")
            guc_body.append("{info}")
        guc_body.append("")
    lines.extend(["h2. Влияние изменений настроек (попарно)", ""] + guc_body)

    lines.extend(_wiki_findings_summary_table(finding_rows, heading="Сводка гипотез по симптомам"))

    for inv in analysis.symptom_investigations:
        body: list[str] = []
        for cause in inv.causes[:10]:
            body.append(_wiki_anchor(f"sec_{cause.cause_id}"))
            title_c = cause.title.replace("|", "/")
            if cause.status.value in ("confirmed", "suspected"):
                status = "Red" if cause.status.value == "confirmed" else "Yellow"
                body.append(
                    f"* {{status:colour={status}|title={cause.status.value.upper()}}} "
                    f"{title_c} — {cause.cause_id}"
                )
                for ev in cause.evidence[:2]:
                    body.append(f"** {ev}")
            else:
                body.append(f"* {title_c} — {cause.cause_id} (possible / PASS)")
        body.append("")
        body.extend(explain_analyze_wiki_for_symptom(inv))
        lines.extend(_wiki_expand(inv.symptom_title, body))

    if analysis.problem_overlap:
        overlap_body: list[str] = []
        for symptom, payload in analysis.problem_overlap.items():
            overlap_body.append(f"h3. {SYMPTOM_TITLES.get(symptom, symptom)}")
            crit = payload.get("divergence_criticality", "low")
            color = "Red" if crit == "high" else ("Yellow" if crit == "medium" else "Green")
            overlap_body.append(
                f"* {{status:colour={color}|title={crit.upper()}}} Критичность расхождения NT vs PROD"
            )
            existing = payload.get("existing_on_prod", [])
            nt_only = payload.get("nt_only", [])
            critical_nt_only = payload.get("critical_nt_only", [])
            overlap_body.append(
                f"* Уже есть на PROD: {', '.join(existing) if existing else 'нет значимых пересечений'}"
            )
            overlap_body.append(f"* Только на НТ: {', '.join(nt_only) if nt_only else 'нет'}")
            if critical_nt_only:
                overlap_body.append(f"* Критичные только на НТ: {', '.join(critical_nt_only)}")
            overlap_body.append("")
        lines.extend(_wiki_expand("Что уже есть на PROD / расхождение НТ", overlap_body))

    runs_body = ["||Метка||Файл||Интервал||"]
    for label, path in zip(analysis.report_labels, analysis.report_paths):
        meta = parse_report_meta(path)
        interval = f"{meta.get('from', '?')} .. {meta.get('to', '?')}"
        runs_body.append(f"|{label}|{path.name}|{interval}|")
    runs_body.append("")
    if analysis.prod_paths:
        runs_body.append("h3. PROD baseline")
        runs_body.append("")
        runs_body.append("||Метка||Файл||Интервал||")
        for label, path in zip(analysis.prod_labels, analysis.prod_paths):
            meta = parse_report_meta(path)
            interval = f"{meta.get('from', '?')} .. {meta.get('to', '?')}"
            runs_body.append(f"|{label}|{path.name}|{interval}|")
        runs_body.append("")
    lines.extend(_wiki_expand("Справочно: прогоны и baseline", runs_body))

    if analysis.nt_prod_validations:
        lines.append("h2. NT vs PROD: оценка расхождения по прогонам")
        lines.append("||NT||PROD||Settings valid||Perf warnings||Critical settings||")
        for v in analysis.nt_prod_validations:
            settings = "yes" if v.settings.valid else "no"
            lines.append(
                f"|{v.run_nt.run_id}|{v.run_prod.run_id}|{settings}|{v.warning_count}|{v.settings.critical_count}|"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def print_nt_runs_report(analysis: NtRunsAnalysis, *, out: Any = None) -> None:
    import sys

    stream = out or sys.stdout
    print("NT multi-run analysis", file=stream)
    print(f"Symptoms: {', '.join(analysis.symptoms)}", file=stream)
    print(f"Reports: {', '.join(analysis.report_labels)}", file=stream)
    if analysis.prod_labels:
        print(f"PROD baseline: {', '.join(analysis.prod_labels)}", file=stream)
    print(file=stream)

    for inv in analysis.symptom_investigations:
        print(f"== {inv.symptom_title} ({inv.symptom}) ==", file=stream)
        from pgprofile_symptoms import print_symptom_investigation

        print_symptom_investigation(inv, out=stream)
        print(file=stream)

    print("== Settings change impact ==", file=stream)
    for pa in analysis.pair_analyses:
        print(f"--- {pa.run_a_label} → {pa.run_b_label} ---", file=stream)
        if pa.settings_changes:
            print("Changed GUC:", file=stream)
            for row in pa.settings_changes:
                if row.status is DiffStatus.DIFFER:
                    print(f"  {row.name}: {row.nt_value} → {row.prod_value}", file=stream)
                elif row.status is DiffStatus.ONLY_PROD:
                    print(f"  {row.name}: (added) → {row.prod_value}", file=stream)
                elif row.status is DiffStatus.ONLY_NT:
                    print(f"  {row.name}: {row.nt_value} → (removed)", file=stream)
        print(pa.narrative, file=stream)
        print(file=stream)

    if analysis.problem_overlap:
        print("== NT vs PROD overlap ==", file=stream)
        for symptom, payload in analysis.problem_overlap.items():
            print(f"--- {SYMPTOM_TITLES.get(symptom, symptom)} ---", file=stream)
            print(f"divergence_criticality: {payload.get('divergence_criticality')}", file=stream)
            print(f"existing_on_prod: {', '.join(payload.get('existing_on_prod', [])) or 'none'}", file=stream)
            print(f"nt_only: {', '.join(payload.get('nt_only', [])) or 'none'}", file=stream)
            print(
                f"critical_nt_only: {', '.join(payload.get('critical_nt_only', [])) or 'none'}",
                file=stream,
            )
            print(file=stream)

    if analysis.nt_prod_validations:
        print("== NT vs PROD divergence summary ==", file=stream)
        for v in analysis.nt_prod_validations:
            print(
                f"{v.run_nt.run_id} vs {v.run_prod.run_id}: "
                f"settings_valid={v.settings.valid}, "
                f"performance_warnings={v.warning_count}, "
                f"critical_settings={v.settings.critical_count}",
                file=stream,
            )
