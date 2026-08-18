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

_LINK_LEGEND_LINES = [
    "Расшифровка колонки «Связь»:",
    "* *PROVEN* — эффект подтвержден: все связанные метрики изменились в ожидаемую сторону.",
    "* *PROBABLE* — направление метрик согласуется с ожидаемым эффектом, но одновременно "
    "менялись другие параметры или нагрузка.",
    "* *WEAK* — связанные метрики не подтверждают ожидаемый эффект настройки.",
    "* *NO-LINK* — для параметра нет правила в базе знаний по выбранным проблемам, "
    "влияние на метрики не оценивалось.",
    "",
]

_TREND_LEGEND_LINES = [
    "Расшифровка колонки «Тренд»:",
    "* *становится лучше* / *становится хуже* — все переходы между прогонами изменили метрику "
    "в одну сторону по смыслу метрики.",
    "* *нестабильный результат* — переходы дали разный знак: часть улучшений, часть ухудшений.",
    "* *рост* / *снижение* / *разнонаправленно, влияние не оценивается* — метрика изменилась, "
    "но для нее не задано, что считать улучшением (например, buffers_checkpoint).",
    "* *изменения незначительны* — все изменения меньше 5%.",
    "",
]

_VERDICT_LEGEND_LINES = [
    "Расшифровка колонки «Оценка»:",
    "* *улучшение* / *ухудшение* — метрика изменилась в сторону, ожидаемую (или обратную) "
    "для этого направления изменения параметра.",
    "* *неоднозначно* — для пары «параметр + метрика» правило не задано: либо направление "
    "изменения параметра не удалось определить численно, либо для метрики не задано, "
    "что считать улучшением.",
    "",
]


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
    compare_findings: list[dict[str, Any]]
    narrative: str
    workload_match: dict[str, Any] = field(default_factory=dict)


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


def _setting_numeric(value: str) -> float | None:
    """Numeric value of a GUC, including fractions like checkpoint_completion_target=0.7.

    Floats are parsed first: parse_setting_int() truncates "0.5" and "0.7" both to 0,
    which would report a real change as no change at all.
    """
    text = str(value).strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    parsed = parse_setting_int(text)
    return float(parsed) if parsed is not None else None


def _setting_direction(old: str | None, new: str | None) -> str:
    if old is None or new is None:
        return "changed"
    old_l = old.strip().lower()
    new_l = new.strip().lower()
    if old_l in {"off", "false"} and new_l in {"on", "true"}:
        return "enabled"
    if old_l in {"on", "true"} and new_l in {"off", "false"}:
        return "disabled"
    old_num = _setting_numeric(old)
    new_num = _setting_numeric(new)
    if old_num is not None and new_num is not None:
        if new_num > old_num:
            return "increased"
        if new_num < old_num:
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
    """Return the compare finding's details plus the pg_profile section it came from."""
    suffix = f".{metric_key}"
    for finding in compare_dict.get("findings", []):
        fid = finding.get("id", "")
        if fid.endswith(suffix) or finding.get("message") == metric_key:
            details = dict(finding.get("details", {}))
            details["section"] = finding.get("category")
            return details
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
            section = details.get("section")
            correlated.append(
                {
                    "metric": metric_key,
                    "section": section,
                    "metric_id": f"{section}.{metric_key}" if section else metric_key,
                    "value_a": details.get("value_a"),
                    "value_b": details.get("value_b"),
                    "unit": details.get("unit") or "count",
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
        compare_findings=compare_dict.get("findings", []),
        narrative=narrative,
        workload_match=compare_dict.get("workload_match", {}),
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
    payload = {
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
                "compare_findings": pa.compare_findings,
                "narrative": pa.narrative,
                "workload_match": pa.workload_match,
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
    from pgprofile_influence import build_series_influence_from_nt_runs_dict

    payload["influence_series"] = build_series_influence_from_nt_runs_dict(payload)
    return payload


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
        _wiki_escape,
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

    def _fmt_metric_value(value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, (int, float)):
            abs_value = abs(float(value))
            if abs_value >= 1000:
                return f"{float(value):.0f}"
            if abs_value >= 10:
                return f"{float(value):.2f}"
            return f"{float(value):.3f}"
        return str(value)

    def _fmt_delta_cell(delta_obj: dict[str, Any] | None) -> str:
        if not isinstance(delta_obj, dict):
            return "—"
        delta = delta_obj.get("delta")
        delta_pct = delta_obj.get("delta_pct")
        if not isinstance(delta, (int, float)) and not isinstance(delta_pct, (int, float)):
            return "—"
        delta_text = ""
        if isinstance(delta, (int, float)):
            sign = "+" if float(delta) > 0 else ""
            delta_text = f"{sign}{_fmt_metric_value(delta)}"
        pct_text = ""
        if isinstance(delta_pct, (int, float)):
            sign = "+" if float(delta_pct) > 0 else ""
            pct_text = f"{sign}{float(delta_pct):.1f}%"
        if delta_text and pct_text:
            return f"{delta_text} ({pct_text})"
        return delta_text or pct_text or "—"

    def _unit_suffix(unit: Any) -> str:
        return {"sec": " с", "pct": " %", "ms": " мс"}.get(str(unit or "count"), "")

    def _fmt_bytes(value: float) -> str:
        for limit, suffix in ((1024**4, "ТБ"), (1024**3, "ГБ"), (1024**2, "МБ"), (1024, "КБ")):
            if abs(value) >= limit:
                return f"{value / limit:.1f} {suffix}"
        return f"{value:.0f} Б"

    def _fmt_with_unit(value: Any, unit: Any) -> str:
        if str(unit) == "bytes":
            return _fmt_bytes(float(value))
        return f"{_fmt_metric_value(value)}{_unit_suffix(unit)}"

    def _fmt_measured_value(value: Any, per_hour: Any, unit: Any) -> str:
        if not isinstance(value, (int, float)):
            return "—"
        text = _fmt_with_unit(value, unit)
        if isinstance(per_hour, (int, float)):
            text += f" ({_fmt_with_unit(per_hour, unit)}/ч)"
        return text

    def _fmt_signed_value(value: Any, unit: Any) -> str:
        if not isinstance(value, (int, float)):
            return "—"
        sign = "+" if float(value) > 0 else ""
        return f"{sign}{_fmt_with_unit(value, unit)}"

    def _fmt_pct(value: Any) -> str:
        if not isinstance(value, (int, float)):
            return "—"
        return f"{float(value):+.1f}%"

    def _confidence_badge(raw: str | None) -> str:
        value = str(raw or "").strip().lower()
        if value in {"proven", "high"}:
            return "{status:colour=Green|title=PROVEN|subtle=false}"
        if value in {"probable", "likely", "possible", "medium"}:
            return "{status:colour=Yellow|title=PROBABLE|subtle=false}"
        return "{status:colour=Blue|title=WEAK|subtle=false}"

    def _hypothesis_badge(status: str) -> str:
        if status == "CONFIRMED":
            return "{status:colour=Green|title=CONFIRMED|subtle=false}"
        if status == "PARTIAL":
            return "{status:colour=Yellow|title=PARTIAL|subtle=false}"
        if status == "REJECTED":
            return "{status:colour=Red|title=REJECTED|subtle=false}"
        return "{status:colour=Blue|title=INCONCLUSIVE|subtle=false}"

    def _decision_badge(decision: str) -> str:
        if decision == "GO":
            return "{status:colour=Green|title=GO|subtle=false}"
        if decision == "NO-GO":
            return "{status:colour=Red|title=NO-GO|subtle=false}"
        return "{status:colour=Yellow|title=NEED-VALIDATION|subtle=false}"

    def _metric_degraded(metric: str, delta_pct: float) -> bool | None:
        lower_metric = metric.lower()
        lower_better = (
            "time",
            "latency",
            "deadlock",
            "rollback",
            "fatal",
            "buffers_full",
            "wal_",
            "checkpoints_",
            "blks_read",
            "blk_read_time",
            "blk_write_time",
        )
        higher_better = ("hit_pct",)
        if any(token in lower_metric for token in lower_better):
            return delta_pct > 0
        if any(token in lower_metric for token in higher_better):
            return delta_pct < 0
        return None

    influence_series = nt_runs_to_dict(analysis).get("influence_series", {})
    settings_table = influence_series.get("settings_table") if isinstance(influence_series, dict) else {}
    metrics_table = influence_series.get("metrics_table") if isinstance(influence_series, dict) else {}
    functional = influence_series.get("functional_summary", {}) if isinstance(influence_series, dict) else {}
    workload_match = influence_series.get("workload_match", {}) if isinstance(influence_series, dict) else {}

    all_report_labels = [str(label) for label in analysis.report_labels] + [
        str(label) for label in analysis.prod_labels
    ]
    run_labels = list((settings_table or {}).get("run_labels") or all_report_labels)
    changed_settings_rows = list((settings_table or {}).get("rows") or [])
    equal_settings_rows = list((settings_table or {}).get("equal_rows") or [])

    metric_rows_all = (metrics_table or {}).get("rows", []) if isinstance(metrics_table, dict) else []
    metric_run_labels = (metrics_table or {}).get("run_labels", []) if isinstance(metrics_table, dict) else []
    metric_pair_labels = (metrics_table or {}).get("pair_labels", []) if isinstance(metrics_table, dict) else []
    symptom_tokens = {
        "high_cpu": ("queries.", "sessions.", "cache.", "cluster."),
        "high_wal": ("wal.", "cluster.", "dml.", "tables."),
        "high_memory": ("memory", "cache", "temp"),
        "slow_query": ("queries.",),
    }
    active_tokens = tuple(
        token for symptom in analysis.symptoms for token in symptom_tokens.get(symptom, ())
    )
    related_metric_rows: list[dict[str, Any]] = []
    other_metric_rows: list[dict[str, Any]] = []
    for row in metric_rows_all:
        metric_name = str(row.get("metric") or "").lower()
        if active_tokens and any(token in metric_name for token in active_tokens):
            related_metric_rows.append(row)
        else:
            other_metric_rows.append(row)

    # Risk and hypothesis scoring.
    warning_degradations = 0
    blocker_degradations = 0
    improved_votes = 0
    degraded_votes = 0
    for row in related_metric_rows:
        for pair_label in metric_pair_labels:
            delta_obj = (row.get("deltas") or {}).get(pair_label)
            if not isinstance(delta_obj, dict):
                continue
            delta_pct = delta_obj.get("delta_pct")
            if not isinstance(delta_pct, (int, float)):
                continue
            degraded = _metric_degraded(str(row.get("metric") or ""), float(delta_pct))
            if degraded is True:
                degraded_votes += 1
                if abs(float(delta_pct)) >= 20:
                    blocker_degradations += 1
                elif abs(float(delta_pct)) >= 10:
                    warning_degradations += 1
            elif degraded is False:
                improved_votes += 1

    improved_pairs = int(functional.get("improved_pairs") or 0)
    degraded_pairs = int(functional.get("degraded_pairs") or 0)
    changed_params_count = len(changed_settings_rows)
    blocker_many_changes = changed_params_count > 10
    workload_score = workload_match.get("workload_match_score")
    blocker_low_workload = isinstance(workload_score, (int, float)) and float(workload_score) < 0.6
    blocker_symptom_growth = degraded_pairs > improved_pairs
    blocker_metric_regression = blocker_degradations > 0

    if not related_metric_rows or (improved_votes == 0 and degraded_votes == 0):
        hypothesis_status = "INCONCLUSIVE"
    elif blocker_low_workload:
        hypothesis_status = "INCONCLUSIVE"
    elif improved_pairs > degraded_pairs and blocker_degradations == 0 and degraded_votes == 0:
        hypothesis_status = "CONFIRMED"
    elif degraded_votes > improved_votes:
        hypothesis_status = "REJECTED"
    else:
        hypothesis_status = "PARTIAL"

    blocker_count = sum(
        1
        for item in (
            blocker_metric_regression,
            blocker_symptom_growth,
            blocker_low_workload,
            blocker_many_changes,
        )
        if item
    )
    if blocker_count > 0 or hypothesis_status == "REJECTED":
        decision = "NO-GO"
    elif hypothesis_status in {"PARTIAL", "INCONCLUSIVE"}:
        decision = "NEED-VALIDATION"
    else:
        decision = "GO"

    if not isinstance(workload_score, (int, float)):
        _workload_status = "НЕТ ДАННЫХ"
        _workload_comment = "Сопоставимость прогонов не рассчитана, оценивайте выводы осторожнее."
    else:
        _workload_status = "BLOCKER" if blocker_low_workload else "OK"
        _workload_comment = (
            f"workload_match_score={float(workload_score):.2f} "
            f"(уровень {workload_match.get('level') or 'unknown'}, минимум по парам; порог 0.60)"
        )

    actions: list[str] = []
    for inv in analysis.symptom_investigations:
        for step in inv.action_plan[:8]:
            if step not in actions:
                actions.append(step)

    min_change_pct = next(
        (
            pa.compare_summary.get("min_change_pct")
            for pa in analysis.pair_analyses
            if isinstance(pa.compare_summary, dict)
            and isinstance(pa.compare_summary.get("min_change_pct"), (int, float))
        ),
        5.0,
    )

    def _append_metrics_table(rows: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        if not rows or not metric_run_labels:
            out.append("_Нет данных по изменениям метрик._")
            out.append("")
            return out
        header_cells = ["Метрика"]
        for idx, label in enumerate(metric_run_labels):
            header_cells.append(_wiki_escape(str(label)))
            if idx > 0:
                header_cells.append(_wiki_escape(f"Δ {metric_run_labels[idx - 1]}→{label}"))
        header_cells.append("Тренд")
        out.append("||" + "||".join(header_cells) + "||")
        for row in rows:
            row_cells = [f"{_wiki_escape(str(row.get('metric') or ''))}"]
            values = row.get("values", {})
            deltas = row.get("deltas", {})
            for idx, label in enumerate(metric_run_labels):
                row_cells.append(_wiki_escape(_fmt_metric_value((values or {}).get(label))))
                if idx > 0:
                    pair_label = (
                        metric_pair_labels[idx - 1]
                        if idx - 1 < len(metric_pair_labels)
                        else f"{metric_run_labels[idx - 1]}->{label}"
                    )
                    row_cells.append(_wiki_escape(_fmt_delta_cell((deltas or {}).get(pair_label))))
            row_cells.append(_wiki_escape(str(row.get("trend") or "—")))
            out.append("|" + "|".join(row_cells) + "|")
        out.append("")
        out.append(
            f"«—» означает, что по этой паре прогонов метрика не попала в сравнение: "
            f"изменение ниже порога значимости ({float(min_change_pct):g}%) "
            "или метрика отсутствует в одном из отчетов. Тренд в таком случае построен "
            "только по тем парам, где изменение зафиксировано."
        )
        out.append("")
        out.extend(_TREND_LEGEND_LINES)
        return out

    lines: list[str] = [f"h1. {title}", ""]
    lines.extend(
        [
            "h2. Краткие выводы",
            "",
            "||Параметр||Значение||",
            f"|Решение|{_decision_badge(decision)}|",
            f"|Статус гипотезы|{_hypothesis_badge(hypothesis_status)}|",
            f"|Симптомы|{_wiki_escape(symptom_titles)}|",
            f"|Прогонов НТ|{len(analysis.report_labels)}|",
            f"|Всего отчетов в сравнении|{len(all_report_labels)}|",
            f"|Изменено параметров|{changed_params_count}|",
            f"|Связанных метрик (по выбранным проблемам)|{len(related_metric_rows)}|",
            f"|Улучшений / ухудшений (голоса)|{improved_votes} / {degraded_votes}|",
            "",
        ]
    )

    lines.extend(
        [
            "h2. Отличия настроек между прогонами",
            "",
        ]
    )
    if changed_settings_rows:
        lines.append("||Параметр||" + "||".join(_wiki_escape(label) for label in run_labels) + "||")
        for row in changed_settings_rows:
            values = row.get("values", {})
            cells = [_wiki_escape(str((values or {}).get(label) or "—")) for label in run_labels]
            lines.append(f"|{_wiki_escape(str(row.get('parameter') or ''))}|{'|'.join(cells)}|")
        lines.append("")
    else:
        lines.append("_Измененных параметров между прогонами нет._")
        lines.append("")

    if equal_settings_rows:
        expand_body = ["||Параметр||" + "||".join(_wiki_escape(label) for label in run_labels) + "||"]
        for row in equal_settings_rows:
            values = row.get("values", {})
            cells = [_wiki_escape(str((values or {}).get(label) or "—")) for label in run_labels]
            expand_body.append(f"|{_wiki_escape(str(row.get('parameter') or ''))}|{'|'.join(cells)}|")
        expand_body.append("")
        lines.extend(_wiki_expand("Одинаковые настройки (без изменений)", expand_body))

    lines.append("h3. Связь настроек и метрик по парам")
    lines.append("")
    snapshot_by_label: dict[str, dict[str, Any]] = {label: {} for label in run_labels}
    for row in list(changed_settings_rows) + list(equal_settings_rows):
        param = str(row.get("parameter") or "")
        values = row.get("values") or {}
        if not param or not isinstance(values, dict):
            continue
        for label in run_labels:
            snapshot_by_label.setdefault(label, {})[param] = values.get(label)
    # NT pairs carry metric correlation; PROD is compared by settings only, because
    # cross-environment metric deltas belong to the NT vs PROD section.
    pair_specs: list[tuple[str, str, dict[str, Any], bool]] = [
        (str(pa.run_a_label), str(pa.run_b_label), {str(gi.guc): gi for gi in pa.guc_impacts}, False)
        for pa in analysis.pair_analyses
    ]
    if analysis.report_labels and analysis.prod_labels:
        last_nt_label = str(analysis.report_labels[-1])
        for prod_label in analysis.prod_labels:
            pair_specs.append((last_nt_label, str(prod_label), {}, True))
    for label_a, label_b, impact_by_guc, settings_only in pair_specs:
        suffix = " (настройки, метрики см. раздел НТ vs PROD)" if settings_only else ""
        lines.append(f"h4. {label_a} → {label_b}{suffix}")
        lines.append("")
        snap_a = snapshot_by_label.get(label_a, {})
        snap_b = snapshot_by_label.get(label_b, {})
        pair_changed_params = sorted(
            param
            for param in set(snap_a) | set(snap_b)
            if str(snap_a.get(param) or "—") != str(snap_b.get(param) or "—")
        )
        for guc in impact_by_guc:
            if guc not in pair_changed_params:
                pair_changed_params.append(guc)
        if not pair_changed_params:
            lines.append("_Отличий в настройках между прогонами пары нет._")
            lines.append("")
            continue
        lines.append("||Параметр||Было||Стало||Направление||Связь||")
        for param in pair_changed_params:
            gi = impact_by_guc.get(param)
            value_from = gi.value_from if gi else _format_guc_value(param, snap_a.get(param))
            value_to = gi.value_to if gi else _format_guc_value(param, snap_b.get(param))
            direction = gi.direction if gi else _setting_direction(snap_a.get(param), snap_b.get(param))
            link_cell = (
                _confidence_badge(gi.confidence) if gi else "{status:colour=Grey|title=NO-LINK|subtle=false}"
            )
            lines.append(
                f"|{_wiki_escape(str(param))}|{_wiki_escape(str(value_from))}|"
                f"{_wiki_escape(str(value_to))}|{_wiki_escape(str(direction))}|"
                f"{link_cell}|"
            )
        lines.append("")
        lines.extend(_LINK_LEGEND_LINES)
        if settings_only:
            lines.append(
                "_Метрики между НТ и PROD не сопоставляются напрямую: разная нагрузка. "
                "Сравнение результатов — в разделе «NT vs PROD»._"
            )
            lines.append("")
            continue
        lines.append(
            "Значения метрик взяты из указанного раздела отчета pg_profile за весь интервал прогона; "
            "в скобках — нормировка на час, если интервалы прогонов различаются. "
            f"Δ = {label_b} − {label_a}."
        )
        lines.append("")
        lines.append(
            "||Параметр||Метрика (раздел pg_profile)||"
            f"{_wiki_escape(label_a)}||{_wiki_escape(label_b)}||Δ||Δ %||Оценка||"
        )
        for param in pair_changed_params:
            gi = impact_by_guc.get(param)
            correlated = (gi.correlated_metrics or []) if gi else []
            if not correlated:
                reason = (
                    "Нет достаточных метрик для связи."
                    if gi
                    else "Параметр не связан с выбранными проблемами."
                )
                lines.append(f"|{_wiki_escape(str(param))}|—|—|—|—|—|{_wiki_escape(reason)}|")
                continue
            for row in correlated[:6]:
                improved = row.get("improved")
                verdict = "неоднозначно"
                if improved is True:
                    verdict = "улучшение"
                elif improved is False:
                    verdict = "ухудшение"
                lines.append(
                    f"|{_wiki_escape(str(param))}|"
                    f"{_wiki_escape(str(row.get('metric_id') or row.get('metric') or '—'))}|"
                    f"{_wiki_escape(_fmt_measured_value(row.get('value_a'), row.get('per_hour_a'), row.get('unit')))}|"
                    f"{_wiki_escape(_fmt_measured_value(row.get('value_b'), row.get('per_hour_b'), row.get('unit')))}|"
                    f"{_wiki_escape(_fmt_signed_value(row.get('delta'), row.get('unit')))}|"
                    f"{_wiki_escape(_fmt_pct(row.get('delta_pct')))}|"
                    f"{_wiki_escape(verdict)}|"
                )
        lines.append("")
        lines.extend(_VERDICT_LEGEND_LINES)

    lines.extend(["h2. Изменения метрик между прогонами", ""])
    if analysis.prod_labels:
        lines.append(
            "_Серия метрик построена по прогонам НТ: "
            f"{_wiki_escape(', '.join(str(label) for label in analysis.report_labels))}. "
            f"PROD ({_wiki_escape(', '.join(str(label) for label in analysis.prod_labels))}) "
            "участвует в сравнении настроек и в разделе «NT vs PROD»._"
        )
        lines.append("")
    lines.extend(["h3. Series summary (связанные метрики)", ""])
    lines.extend(_append_metrics_table(related_metric_rows))
    if other_metric_rows:
        lines.extend(_wiki_expand("Прочие метрики (не связаны напрямую с выбранными проблемами)", _append_metrics_table(other_metric_rows)))

    lines.append("h3. Pairwise детализация")
    lines.append("")
    has_pair_metrics = False
    for pa in analysis.pair_analyses:
        pair_findings = pa.compare_findings or []
        if not pair_findings:
            continue
        has_pair_metrics = True
        lines.append(f"h4. {pa.run_a_label} → {pa.run_b_label}")
        lines.append("")
        lines.append(f"||Метрика||{_wiki_escape(pa.run_a_label)}||{_wiki_escape(pa.run_b_label)}||Δ||")
        for finding in pair_findings:
            details = finding.get("details") or {}
            metric_name = f"{finding.get('category')}.{finding.get('message')}"
            lines.append(
                f"|{_wiki_escape(str(metric_name))}|{_wiki_escape(_fmt_metric_value(details.get('value_a')))}|"
                f"{_wiki_escape(_fmt_metric_value(details.get('value_b')))}|"
                f"{_wiki_escape(_fmt_delta_cell({'delta': details.get('delta'), 'delta_pct': details.get('delta_pct')}))}|"
            )
        lines.append("")
    if not has_pair_metrics:
        lines.append("_Нет значимых изменений метрик в попарных сравнениях._")
        lines.append("")

    lines.extend(
        [
            "h2. Риски и блокеры",
            "",
            "||Проверка||Статус||Комментарий||",
            f"|Деградация целевых метрик >=20%|{'BLOCKER' if blocker_metric_regression else 'OK'}|"
            f"{'Найдено ' + str(blocker_degradations) + ' критичных деградаций' if blocker_metric_regression else 'Критичных деградаций не найдено'}|",
            f"|Рост критичных симптомов после изменений|{'BLOCKER' if blocker_symptom_growth else 'OK'}|"
            f"improved_pairs={improved_pairs}, degraded_pairs={degraded_pairs}|",
            f"|Низкая сопоставимость прогонов|{_workload_status}|{_wiki_escape(_workload_comment)}|",
            f"|Слишком много одновременных изменений|{'BLOCKER' if blocker_many_changes else 'OK'}|"
            f"changed_params_count={changed_params_count}|",
            f"|Порог ухудшений >=10%|{'WARN' if warning_degradations > 0 else 'OK'}|"
            f"warning_degradations={warning_degradations}|",
            "",
        ]
    )

    lines.extend(_wiki_actions_section(actions[:12], heading="Следующие действия", limit=12))

    lines.append("h3. Проблемные запросы и варианты оптимизации")
    lines.append("")
    lines.append("||Запрос (hex/id)||Почему проблемный||Вариант оптимизации||Как проверить в следующем прогоне||")
    query_rows = 0
    for inv in analysis.symptom_investigations:
        for match in (inv.query_matches or [])[:5]:
            query_rows += 1
            lines.append(
                f"|{_wiki_escape(str(match.get('hexqueryid') or match.get('queryid') or '—'))}|"
                f"{_wiki_escape(str(match.get('preview') or inv.symptom_title))}|"
                f"Проверить индекс/план, снизить scan/IO, рассмотреть rewrite SQL.|"
                f"EXPLAIN (ANALYZE, BUFFERS) до/после и сравнить p95 latency, calls, wal_bytes.|"
            )
    if query_rows == 0:
        lines.append("|—|Недостаточно данных для выделения query-level кандидатов.|Собрать top queries по симптому.|Запустить повторный прогон и добавить query evidence.|")
    lines.append("")

    # Supporting details and evidence in expand blocks.
    finding_rows: list[tuple[str, str, str, str]] = []
    checklist: list[tuple[str, str]] = []
    for inv in analysis.symptom_investigations:
        checklist.extend(_checklist_from_symptom_causes(inv.causes))
        for cause in inv.causes:
            if cause.status.value in ("confirmed", "suspected") or cause.evidence:
                finding_rows.append(
                    (
                        "critical" if cause.status.value == "confirmed" else "warning",
                        cause.cause_id,
                        cause.title,
                        ", ".join(cause.reports_matched) or "—",
                    )
                )

    lines.extend(_wiki_expand("Сводка гипотез по симптомам", _wiki_findings_summary_table(finding_rows, heading="Сводка гипотез по симптомам")[2:]))
    lines.extend(_wiki_expand("Чеклист гипотез", _wiki_checklist_table(checklist, heading="Чеклист гипотез")[2:]))

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
