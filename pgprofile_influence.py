"""Build run comparison influence table and functional summary artifacts."""

from __future__ import annotations

import csv
import io
import math
from pathlib import Path
from typing import Any

from pgprofile_contracts import (
    ConfidenceLevel,
    EvidenceType,
    ImpactKind,
    WorkloadLevel,
    workload_level_for,
)


LOWER_IS_BETTER_TOKENS = (
    "time",
    "latency",
    "deadlock",
    "rollback",
    "fatal",
    "killed",
    "buffers_full",
    "maxwritten_clean",
    "blks_read",
    "blk_read_time",
    "blk_write_time",
    "wal_size",
    "wal_bytes",
    "wal_records",
    "wal_sync",
    "wal_write",
    "checkpoints_req",
    "checkpoint_write_time",
    "checkpoint_sync_time",
)

HIGHER_IS_BETTER_TOKENS = (
    "hit_pct",
)

STABILITY_HIGH = 0.7
NOISE_IQR_RATIO = 1.0
MIN_PAIRS_FOR_PROVEN = 3
CONFIDENCE_RANK = {
    ConfidenceLevel.LOW.value: 0,
    ConfidenceLevel.MEDIUM.value: 1,
    ConfidenceLevel.HIGH.value: 2,
}


PARAM_METRIC_HINTS: dict[str, tuple[str, ...]] = {
    "max_wal_size": ("checkpoints_req", "checkpoint_", "wal_size", "wal_bytes"),
    "wal_buffers": ("wal_buffers_full", "wal_write", "wal_sync"),
    "shared_buffers": ("blks_hit_pct", "blks_read", "blk_read_time"),
    "effective_cache_size": ("blks_hit_pct", "blks_read", "blk_read_time"),
    "random_page_cost": ("seq_scan", "idx_scan", "blks_read"),
    "effective_io_concurrency": ("blks_read", "blk_read_time", "blk_write_time"),
    "work_mem": ("temp_blks_written", "mean_exec_time", "total_time"),
    "autovacuum_naptime": ("idle_in_transaction_time", "checkpoints_req"),
    "autovacuum_vacuum_cost_delay": ("blk_write_time", "blk_read_time"),
    "autovacuum_vacuum_cost_limit": ("blk_write_time", "blk_read_time"),
    "checkpoint_timeout": ("checkpoints_", "checkpoint_write_time"),
    "checkpoint_completion_target": ("checkpoint_write_time", "checkpoint_sync_time"),
}


def _series_trend(metric: str, deltas: list[float]) -> str:
    if not deltas:
        return "изменения незначительны"
    if all(abs(d) < 5 for d in deltas):
        return "изменения незначительны"
    impacts = []
    for delta in deltas:
        _direction, impact = _metric_direction(metric, delta)
        impacts.append(impact)
    improved = sum(1 for item in impacts if item == ImpactKind.IMPROVED.value)
    degraded = sum(1 for item in impacts if item == ImpactKind.DEGRADED.value)
    if improved > 0 and degraded == 0:
        return "становится лучше"
    if degraded > 0 and improved == 0:
        return "становится хуже"
    if improved == 0 and degraded == 0:
        # The metric moved, but nothing tells us whether up is good or bad here.
        if all(delta > 0 for delta in deltas):
            return "рост, влияние не оценивается"
        if all(delta < 0 for delta in deltas):
            return "снижение, влияние не оценивается"
        return "разнонаправленно, влияние не оценивается"
    return "нестабильный результат"


def _series_workload_match(pair_analyses: list[dict[str, Any]]) -> dict[str, Any]:
    """Worst pair comparability, so a single incomparable pair cannot hide in an average."""
    scores: list[float] = []
    for pair in pair_analyses:
        score = (pair.get("workload_match") or {}).get("workload_match_score")
        if isinstance(score, (int, float)):
            scores.append(float(score))
    if not scores:
        return {
            "workload_match_score": None,
            "level": WorkloadLevel.UNKNOWN.value,
            "notes": "Нет данных о сопоставимости прогонов.",
        }
    worst = round(min(scores), 4)
    return {
        "workload_match_score": worst,
        "level": workload_level_for(worst),
        "pair_scores": scores,
        "notes": "Минимальный балл по парам прогонов (худшая сопоставимость).",
    }


def _build_series_settings_table(nt_runs: dict[str, Any]) -> dict[str, Any]:
    reports = list(nt_runs.get("reports") or []) + list(nt_runs.get("prod_reports") or [])
    html_table = _settings_table_from_html(reports)
    if html_table is not None:
        return html_table
    return _settings_table_from_pair_changes(nt_runs)


def _settings_table_from_html(reports: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Full GUC grid from report HTML. None if paths are missing (unit fixtures)."""
    from pgprofile_parser import load_settings

    labels: list[str] = []
    snapshots: list[dict[str, Any]] = []
    for report in reports:
        raw_path = report.get("path")
        if not raw_path:
            return None
        path = Path(str(raw_path))
        if not path.is_file():
            return None
        labels.append(str(report.get("label") or path.name))
        snapshots.append(load_settings(path, defined_only=True))
    if not labels:
        return None
    all_params = sorted({param for snap in snapshots for param in snap.keys()})
    changed_rows: list[dict[str, Any]] = []
    equal_rows: list[dict[str, Any]] = []
    for param in all_params:
        values = [snap.get(param) for snap in snapshots]
        normalized = ["—" if value is None else str(value) for value in values]
        row = {
            "parameter": param,
            "values": {labels[idx]: normalized[idx] for idx in range(len(labels))},
        }
        if len(set(normalized)) > 1:
            changed_rows.append(row)
        else:
            equal_rows.append(row)
    return {
        "run_labels": labels,
        "rows": changed_rows,
        "equal_rows": equal_rows,
    }


def _settings_table_from_pair_changes(nt_runs: dict[str, Any]) -> dict[str, Any]:
    reports = nt_runs.get("reports") or []
    pair_analyses = nt_runs.get("pair_analyses") or []
    labels = [str(r.get("label") or "") for r in reports]
    by_param: dict[str, dict[str, Any]] = {}

    for pair in pair_analyses:
        run_a = str(pair.get("run_a") or "")
        run_b = str(pair.get("run_b") or "")
        for change in pair.get("settings_changes") or []:
            param = str(change.get("guc") or "")
            if not param:
                continue
            row = by_param.setdefault(
                param,
                {
                    "parameter": param,
                    "values": {label: None for label in labels},
                },
            )
            values = row["values"]
            if run_a in values and values[run_a] is None:
                values[run_a] = change.get("value_from")
            if run_b in values:
                values[run_b] = change.get("value_to")

    rows = []
    for row in by_param.values():
        values = [row["values"].get(label) for label in labels]
        # Forward fill then backward fill to infer unchanged neighboring runs.
        for i in range(1, len(values)):
            if values[i] is None:
                values[i] = values[i - 1]
        for i in range(len(values) - 2, -1, -1):
            if values[i] is None:
                values[i] = values[i + 1]
        normalized = ["—" if value is None else value for value in values]
        if len(set(normalized)) <= 1:
            continue
        rows.append(
            {
                "parameter": row["parameter"],
                "values": {label: normalized[idx] for idx, label in enumerate(labels)},
            }
        )

    rows.sort(key=lambda item: item["parameter"])
    return {
        "run_labels": labels,
        "rows": rows,
        "equal_rows": [],
    }


def _build_series_metrics_table(nt_runs: dict[str, Any]) -> dict[str, Any]:
    reports = nt_runs.get("reports") or []
    pair_analyses = nt_runs.get("pair_analyses") or []
    labels = [str(r.get("label") or "") for r in reports]
    pair_labels = [f"{labels[i]}->{labels[i + 1]}" for i in range(len(labels) - 1)]
    by_metric: dict[str, dict[str, Any]] = {}

    for pair in pair_analyses:
        run_a = str(pair.get("run_a") or "")
        run_b = str(pair.get("run_b") or "")
        pair_label = f"{run_a}->{run_b}"
        for finding in pair.get("compare_findings") or []:
            details = finding.get("details") or {}
            metric = f"{finding.get('category')}.{finding.get('message')}"
            item_id = str(details.get("item_id") or "")
            if item_id:
                metric = f"{metric} · hex={item_id}"
            row = by_metric.setdefault(
                metric,
                {
                    "metric": metric,
                    "values": {label: None for label in labels},
                    "deltas": {pl: None for pl in pair_labels},
                    "trend": "изменения незначительны",
                },
            )
            # Колонки прогонов и Δ должны описывать один и тот же объект,
            # иначе в таблице Δ не сходится с разностью колонок.
            if pair_label not in row["deltas"] or row["deltas"][pair_label] is not None:
                continue
            row["deltas"][pair_label] = {
                "delta": details.get("delta"),
                "delta_pct": details.get("delta_pct"),
            }
            if run_a in row["values"] and row["values"][run_a] is None:
                row["values"][run_a] = details.get("value_a")
            if run_b in row["values"]:
                row["values"][run_b] = details.get("value_b")

    rows = []
    for row in by_metric.values():
        deltas = []
        for pair_label in pair_labels:
            item = row["deltas"].get(pair_label)
            if isinstance(item, dict) and isinstance(item.get("delta_pct"), (int, float)):
                deltas.append(float(item["delta_pct"]))
        row["trend"] = _series_trend(row["metric"], deltas)
        rows.append(row)

    rows.sort(key=lambda item: item["metric"])
    return {
        "run_labels": labels,
        "pair_labels": pair_labels,
        "rows": rows,
    }


def _extract_run_findings(run_comparison: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for finding in run_comparison.get("findings") or []:
        details = finding.get("details") or {}
        out.append(
            {
                "id": str(finding.get("id") or ""),
                "section": str(finding.get("category") or ""),
                "metric": str(finding.get("message") or ""),
                "delta": details.get("delta"),
                "delta_pct": details.get("delta_pct"),
            }
        )
    return out


def expected_metric_impact(metric: str, delta: float | None) -> tuple[str, str]:
    """Public wrapper: metric movement (`up`/`down`/`flat`) and impact polarity."""
    return _metric_direction(metric, delta)


def _impact_from_metric_row(metric: str, metric_row: dict[str, Any]) -> str:
    """Polarity of one correlated metric: numeric Δ wins over the `improved` flag."""
    delta = metric_row.get("delta_pct")
    if isinstance(delta, (int, float)):
        return expected_metric_impact(metric, float(delta))[1]
    improved = metric_row.get("improved")
    if improved is True:
        return ImpactKind.IMPROVED.value
    if improved is False:
        return ImpactKind.DEGRADED.value
    return ImpactKind.NEUTRAL.value


def _pair_impact_for_dominant(
    correlated: list[Any],
    dominant: str,
    correlated_majority: str,
) -> tuple[str, bool]:
    """Impact of the series-dominant metric in one pair.

    Returns (impact, used_majority_fallback). Fallback is only when that
    metric is missing from the pair's correlated list.
    """
    if dominant:
        for metric_row in correlated:
            if not isinstance(metric_row, dict):
                continue
            if str(metric_row.get("metric") or "") == dominant:
                return _impact_from_metric_row(dominant, metric_row), False
    return correlated_majority, True


def series_noise_ratio(row: dict[str, Any]) -> float | None:
    """IQR / |median Δ%|. None when either side is missing. Floor |Δ| at 1%."""
    median = row.get("delta_pct")
    iqr = row.get("delta_iqr_pct")
    if not isinstance(median, (int, float)) or not isinstance(iqr, (int, float)):
        return None
    return round(abs(float(iqr)) / max(abs(float(median)), 1.0), 3)


def recommend_series_confidence(row: dict[str, Any]) -> dict[str, Any]:
    """Statistical confidence for one series influence row.

    High/proven requires ≥3 pairs, majority-stable direction, and IQR not larger
    than the median effect. Noise never raises evidence_type above probable.
    """
    try:
        evidence = int(row.get("evidence_count") or 0)
    except (TypeError, ValueError):
        evidence = 0
    try:
        stability = float(row.get("stability_score") or 0.0)
    except (TypeError, ValueError):
        stability = 0.0
    impact = str(row.get("impact") or ImpactKind.NEUTRAL.value)
    ratio = series_noise_ratio(row)
    noisy = bool(evidence >= 2 and ratio is not None and ratio >= NOISE_IQR_RATIO)
    reasons: list[str] = []

    if evidence <= 1:
        confidence = ConfidenceLevel.LOW.value
        evidence_type = EvidenceType.PROBABLE.value
        reasons.append("only one pair; statistical proof is not available")
    elif impact == ImpactKind.NEUTRAL.value:
        confidence = ConfidenceLevel.MEDIUM.value
        evidence_type = EvidenceType.PROBABLE.value
        reasons.append("mixed or neutral pair effects")
    elif noisy:
        confidence = (
            ConfidenceLevel.LOW.value
            if stability < STABILITY_HIGH
            else ConfidenceLevel.MEDIUM.value
        )
        evidence_type = EvidenceType.PROBABLE.value
        reasons.append(f"effect is noisy (IQR/|Δ|={ratio})")
    elif evidence >= MIN_PAIRS_FOR_PROVEN and stability >= STABILITY_HIGH:
        confidence = ConfidenceLevel.HIGH.value
        evidence_type = EvidenceType.PROVEN.value
        reasons.append(
            f"stable across {evidence} pairs (stability={stability:.3f})"
        )
    elif stability >= STABILITY_HIGH:
        confidence = ConfidenceLevel.MEDIUM.value
        evidence_type = EvidenceType.PROBABLE.value
        reasons.append("direction is consistent but fewer than 3 pairs")
    else:
        confidence = ConfidenceLevel.LOW.value
        evidence_type = EvidenceType.PROBABLE.value
        reasons.append(f"unstable across pairs (stability={stability:.3f})")

    return {
        "confidence": confidence,
        "evidence_type": evidence_type,
        "noisy": noisy,
        "noise_ratio": ratio,
        "reasons": reasons,
    }


def _metric_direction(metric: str, delta: float | None) -> tuple[str, str]:
    if delta is None or delta == 0:
        return "flat", ImpactKind.NEUTRAL.value
    lower = metric.lower()
    moved = "up" if delta > 0 else "down"
    if any(token in lower for token in LOWER_IS_BETTER_TOKENS):
        worse = delta > 0
    elif any(token in lower for token in HIGHER_IS_BETTER_TOKENS):
        worse = delta < 0
    else:
        return moved, ImpactKind.NEUTRAL.value
    return moved, (ImpactKind.DEGRADED.value if worse else ImpactKind.IMPROVED.value)


def build_functional_summary(
    run_comparison: dict[str, Any],
    *,
    top_n: int = 10,
) -> dict[str, Any]:
    findings = _extract_run_findings(run_comparison)
    improved: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    neutral = 0

    for row in findings:
        direction, impact = _metric_direction(row["metric"], row["delta"])
        candidate = {
            "metric": f"{row['section']}.{row['metric']}",
            "direction": direction,
            "impact": impact,
            "delta": row["delta"],
            "delta_pct": row["delta_pct"],
        }
        if impact == ImpactKind.IMPROVED.value:
            improved.append(candidate)
        elif impact == ImpactKind.DEGRADED.value:
            degraded.append(candidate)
        else:
            neutral += 1

    def _score(item: dict[str, Any]) -> float:
        delta_pct = item.get("delta_pct")
        if isinstance(delta_pct, (int, float)):
            return abs(float(delta_pct))
        delta = item.get("delta")
        return abs(float(delta)) if isinstance(delta, (int, float)) else 0.0

    improved.sort(key=_score, reverse=True)
    degraded.sort(key=_score, reverse=True)

    return {
        "total_metrics_analyzed": len(findings),
        "improved_count": len(improved),
        "degraded_count": len(degraded),
        "neutral_count": neutral,
        "top_improved": improved[:top_n],
        "top_degraded": degraded[:top_n],
    }


def build_influence_rows(
    *,
    settings_diff: dict[str, Any],
    run_comparison: dict[str, Any],
) -> list[dict[str, Any]]:
    settings_changes = settings_diff.get("settings_changes") or []
    findings = _extract_run_findings(run_comparison)
    workload = run_comparison.get("workload_match", {})
    workload_score = workload.get("workload_match_score")
    workload_level = workload.get("level")
    confidence_meta = settings_diff.get("confidence_meta") or {}
    global_confidence = str(confidence_meta.get("confidence") or ConfidenceLevel.MEDIUM.value)
    global_evidence_type = str(
        confidence_meta.get("evidence_type") or EvidenceType.PROBABLE.value
    )
    global_notes = str(confidence_meta.get("notes") or "")

    rows: list[dict[str, Any]] = []
    for change in settings_changes:
        param = str(change.get("parameter") or "")
        hints = PARAM_METRIC_HINTS.get(param, ())
        # Without a known parameter->metric link we leave the row unattributed:
        # falling back to "the metric that moved most" invents causality.
        candidates = [
            finding
            for finding in findings
            if any(token in finding["metric"].lower() for token in hints)
        ]

        candidates.sort(
            key=lambda item: abs(item["delta_pct"]) if isinstance(item["delta_pct"], (int, float)) else abs(item["delta"]) if isinstance(item["delta"], (int, float)) else 0.0,
            reverse=True,
        )
        top = candidates[0] if candidates else None
        direction = "flat"
        impact = ImpactKind.NEUTRAL.value
        delta_pct = None
        affected_metric = ""
        if top:
            direction, impact = _metric_direction(top["metric"], top["delta"])
            delta_pct = top["delta_pct"]
            affected_metric = f"{top['section']}.{top['metric']}"

        notes = [global_notes] if global_notes else []
        reasons: list[str] = []
        if workload_level == WorkloadLevel.LOW.value:
            notes.append("Low workload match; treat attribution as hypothesis.")
            reasons.append("low workload match; treat attribution as hypothesis")
        if len(settings_changes) > 10:
            notes.append("Many settings changed simultaneously; causal confidence is reduced.")
            reasons.append("too many parameters changed simultaneously")
        if not candidates:
            notes.append("No known link between this parameter and the compared metrics.")
            reasons.append("unattributed; confidence downgraded to low")
        elif global_notes:
            reasons.append(global_notes)
        if len(candidates) > 1:
            notes.append(
                f"Hint matched {len(candidates)} metrics; evidence_count is 1 if attributed."
            )

        rows.append(
            {
                "parameter": param,
                "old": change.get("old"),
                "new": change.get("new"),
                "delta_pct": delta_pct,
                "affected_metric": affected_metric,
                "direction": direction,
                "metric_direction": direction,
                "impact": impact,
                "confidence": (
                    global_confidence if candidates else ConfidenceLevel.LOW.value
                ),
                "evidence_type": (
                    global_evidence_type if candidates else EvidenceType.PROBABLE.value
                ),
                "evidence_count": 1 if candidates else 0,
                "workload_match_score": workload_score,
                "notes": " ".join(n for n in notes if n).strip(),
                "confidence_reasons": reasons,
            }
        )
    return rows


def build_influence_payload(
    *,
    settings_diff: dict[str, Any],
    run_comparison: dict[str, Any],
) -> dict[str, Any]:
    rows = build_influence_rows(settings_diff=settings_diff, run_comparison=run_comparison)
    summary = build_functional_summary(run_comparison)
    return {
        "type": "influence_table",
        "mode": "pair",
        "run_identity": run_comparison.get("run_identity"),
        "workload_match": run_comparison.get("workload_match"),
        "confidence_meta": settings_diff.get("confidence_meta"),
        "functional_summary": summary,
        "rows": rows,
    }


_IMPACT_RU = {
    ImpactKind.IMPROVED.value: "улучшение",
    ImpactKind.DEGRADED.value: "ухудшение",
    ImpactKind.NEUTRAL.value: "без оценки",
}

_EVIDENCE_RU = {
    EvidenceType.PROVEN.value: "PROVEN",
    EvidenceType.PROBABLE.value: "PROBABLE",
}

_CONFIDENCE_RU = {
    ConfidenceLevel.HIGH.value: "высокая",
    ConfidenceLevel.MEDIUM.value: "средняя",
    ConfidenceLevel.LOW.value: "низкая",
}

_METRIC_DIR_RU = {
    "up": "вверх",
    "down": "вниз",
    "flat": "без сдвига",
}

_GUC_DIR_RU = {
    "increased": "увеличился",
    "decreased": "уменьшился",
    "changed": "изменён",
    "enabled": "включён",
    "disabled": "выключен",
}

_WORKLOAD_RU = {
    WorkloadLevel.HIGH.value: "высокая",
    WorkloadLevel.MEDIUM.value: "средняя",
    WorkloadLevel.LOW.value: "низкая",
    WorkloadLevel.UNKNOWN.value: "нет данных",
}


def _fmt_pct(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{float(value):+.1f}%"


def _summary_run_labels(payload: dict[str, Any]) -> list[str]:
    identity = payload.get("run_identity") or {}
    labels: list[str] = []
    for run in identity.get("runs") or []:
        label = str(run.get("run_id") or run.get("role") or "").strip()
        if not label:
            continue
        window = " → ".join(
            str(run.get(field)) for field in ("report_start", "report_end") if run.get(field)
        )
        labels.append(f"{label} ({window})" if window else label)
    for report in identity.get("reports") or []:
        label = report.get("label") or report.get("path")
        if label:
            labels.append(str(label))
    return labels


def _influence_is_series(payload: dict[str, Any]) -> bool:
    return payload.get("type") == "influence_table_series" or payload.get("mode") == "series"


def _evidence_count_heading(payload: dict[str, Any]) -> str:
    return "Пар" if _influence_is_series(payload) else "Есть связь"


def _guc_direction_label(row: dict[str, Any], *, series: bool) -> str:
    if not series:
        return "—"
    raw = str(row.get("direction") or "").strip()
    return _GUC_DIR_RU.get(raw, raw or "—")


def _metric_direction_label(row: dict[str, Any]) -> str:
    raw = str(row.get("metric_direction") or "").strip()
    if not raw:
        # Pair rows historically stored metric axis in `direction`.
        maybe = str(row.get("direction") or "").strip()
        if maybe in _METRIC_DIR_RU:
            raw = maybe
    return _METRIC_DIR_RU.get(raw, raw or "—")


def build_influence_summary_lines(payload: dict[str, Any], *, top_n: int = 10) -> list[dict[str, Any]]:
    """Structured lines of the influence summary, shared by markdown and wikitext renderers."""
    series = _influence_is_series(payload)
    rows = [
        row
        for row in (payload.get("rows") or [])
        if isinstance(row, dict) and str(row.get("affected_metric") or "").strip()
    ]

    def _weight(row: dict[str, Any]) -> float:
        delta_pct = row.get("delta_pct")
        return abs(float(delta_pct)) if isinstance(delta_pct, (int, float)) else 0.0

    ranked = sorted(rows, key=_weight, reverse=True)[:top_n]
    return [
        {
            "parameter": str(row.get("parameter") or ""),
            "old": row.get("old"),
            "new": row.get("new"),
            "guc_direction": _guc_direction_label(row, series=series),
            "affected_metric": str(row.get("affected_metric") or "—"),
            "metric_direction": _metric_direction_label(row),
            "delta_pct": row.get("delta_pct"),
            "impact": _IMPACT_RU.get(str(row.get("impact")), "без оценки"),
            "confidence": _CONFIDENCE_RU.get(str(row.get("confidence")), "средняя"),
            "evidence_type": _EVIDENCE_RU.get(str(row.get("evidence_type")), "PROBABLE"),
            "evidence_count": row.get("evidence_count"),
        }
        for row in ranked
    ]


def _unlinked_parameters(payload: dict[str, Any]) -> list[str]:
    """Changed parameters we could not tie to any compared metric."""
    return [
        str(row.get("parameter") or "")
        for row in (payload.get("rows") or [])
        if isinstance(row, dict) and not str(row.get("affected_metric") or "").strip()
    ]


def _summary_header_lines(payload: dict[str, Any]) -> list[str]:
    mode = "серия прогонов" if payload.get("type") == "influence_table_series" else "пара прогонов"
    workload = payload.get("workload_match") or {}
    confidence_meta = payload.get("confidence_meta") or {}
    functional = payload.get("functional_summary") or {}

    score = workload.get("workload_match_score")
    score_text = f"{float(score):.2f}" if isinstance(score, (int, float)) else "нет данных"
    level_text = _WORKLOAD_RU.get(str(workload.get("level")), "нет данных")

    lines = [
        f"Режим: {mode}",
        f"Прогоны: {', '.join(_summary_run_labels(payload)) or 'не указаны'}",
        f"Сопоставимость нагрузки: {score_text} ({level_text})",
        (
            f"Достоверность: {_CONFIDENCE_RU.get(str(confidence_meta.get('confidence')), 'средняя')}"
            f" / {_EVIDENCE_RU.get(str(confidence_meta.get('evidence_type')), 'PROBABLE')}"
        ),
    ]

    changed = confidence_meta.get("changed_params_count")
    threshold = confidence_meta.get("changed_params_threshold")
    if isinstance(changed, int):
        suffix = f" (порог понижения достоверности: {threshold})" if isinstance(threshold, int) else ""
        lines.append(f"Изменено параметров: {changed}{suffix}")

    if functional.get("total_pairs") is not None:
        lines.append(
            "Пары прогонов: улучшение — {improved}, ухудшение — {degraded}, "
            "без изменений — {neutral} из {total}".format(
                improved=functional.get("improved_pairs", 0),
                degraded=functional.get("degraded_pairs", 0),
                neutral=functional.get("neutral_pairs", 0),
                total=functional.get("total_pairs", 0),
            )
        )
    elif functional.get("total_metrics_analyzed") is not None:
        lines.append(
            "Метрики: улучшилось — {improved}, ухудшилось — {degraded}, "
            "без оценки — {neutral} из {total}".format(
                improved=functional.get("improved_count", 0),
                degraded=functional.get("degraded_count", 0),
                neutral=functional.get("neutral_count", 0),
                total=functional.get("total_metrics_analyzed", 0),
            )
        )

    notes = str(confidence_meta.get("notes") or "").strip()
    if notes:
        lines.append(f"Ограничения: {notes}")
    return lines


_SUMMARY_RULES = (
    "PROVEN — эффект устойчив на нескольких парах прогонов; PROBABLE — только корреляция.",
    "Для PROBABLE нельзя утверждать причинность: это гипотеза, требующая отдельного прогона.",
    "Колонка GUC — направление настройки; колонка «Метрика Δ» — куда сдвинулся показатель.",
    "Все числа берутся из influence_table*.json; новых значений добавлять нельзя.",
)


def build_influence_summary_markdown(payload: dict[str, Any], *, top_n: int = 10) -> str:
    """Compact influence digest: readable in Confluence and usable as LLM prompt context."""
    lines = ["# Влияние настроек на метрики", ""]
    lines += [f"- {item}" for item in _summary_header_lines(payload)]
    lines += ["", "## Топ связей параметр → метрика", ""]

    summary_rows = build_influence_summary_lines(payload, top_n=top_n)
    count_heading = _evidence_count_heading(payload)
    if not summary_rows:
        lines.append("Связей параметр → метрика не найдено.")
    else:
        lines += [
            f"| Параметр | Было | Стало | GUC | Метрика | Метрика Δ | Δ % | Итог | Достоверность | Уверенность | {count_heading} |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in summary_rows:
            lines.append(
                "| {parameter} | {old} | {new} | {guc} | {metric} | {metric_dir} | {delta} | {impact} | "
                "{evidence} | {confidence} | {count} |".format(
                    parameter=row["parameter"],
                    old=row["old"] if row["old"] not in (None, "") else "—",
                    new=row["new"] if row["new"] not in (None, "") else "—",
                    guc=row["guc_direction"],
                    metric=row["affected_metric"],
                    metric_dir=row["metric_direction"],
                    delta=_fmt_pct(row["delta_pct"]),
                    impact=row["impact"],
                    evidence=row["evidence_type"],
                    confidence=row["confidence"],
                    count=row["evidence_count"] if row["evidence_count"] is not None else "—",
                )
            )

    unlinked = _unlinked_parameters(payload)
    if unlinked:
        lines += ["", "## Изменены, но связь с метриками неизвестна", ""]
        lines.append(f"- {', '.join(unlinked[:25])}")
        if len(unlinked) > 25:
            lines.append(f"- и ещё {len(unlinked) - 25} параметров")

    lines += ["", "## Правила трактовки", ""]
    lines += [f"- {rule}" for rule in _SUMMARY_RULES]
    return "\n".join(lines) + "\n"


def build_influence_summary_wiki(payload: dict[str, Any], *, top_n: int = 10) -> str:
    """Same digest in Confluence wiki markup, ready to paste into a page."""
    from pgprofile_confluence import _wiki_escape

    lines = ["h2. Влияние настроек на метрики", ""]
    lines += [f"* {_wiki_escape(item)}" for item in _summary_header_lines(payload)]
    lines += ["", "h3. Топ связей параметр -> метрика", ""]

    summary_rows = build_influence_summary_lines(payload, top_n=top_n)
    count_heading = _evidence_count_heading(payload)
    if not summary_rows:
        lines.append("Связей параметр -> метрика не найдено.")
    else:
        lines.append(
            f"||Параметр||Было||Стало||GUC||Метрика||Метрика Δ||Δ %||Итог||Достоверность||Уверенность||{count_heading}||"
        )
        for row in summary_rows:
            cells = [
                row["parameter"],
                row["old"] if row["old"] not in (None, "") else "—",
                row["new"] if row["new"] not in (None, "") else "—",
                row["guc_direction"],
                row["affected_metric"],
                row["metric_direction"],
                _fmt_pct(row["delta_pct"]),
                row["impact"],
                row["evidence_type"],
                row["confidence"],
                row["evidence_count"] if row["evidence_count"] is not None else "—",
            ]
            lines.append("|" + "|".join(_wiki_escape(cell) for cell in cells) + "|")

    unlinked = _unlinked_parameters(payload)
    if unlinked:
        lines += ["", "h3. Изменены, но связь с метриками неизвестна", ""]
        lines.append(f"* {_wiki_escape(', '.join(unlinked[:25]))}")
        if len(unlinked) > 25:
            lines.append(f"* и ещё {len(unlinked) - 25} параметров")

    lines += ["", "h3. Правила трактовки", ""]
    lines += [f"* {_wiki_escape(rule)}" for rule in _SUMMARY_RULES]
    return "\n".join(lines) + "\n"


def influence_rows_to_csv(rows: list[dict[str, Any]]) -> str:
    fieldnames = [
        "parameter",
        "old",
        "new",
        "delta_pct",
        "delta_iqr_pct",
        "affected_metric",
        "direction",
        "metric_direction",
        "impact",
        "stability_score",
        "noise_ratio",
        "confidence",
        "evidence_type",
        "evidence_count",
        "workload_match_score",
        "notes",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def build_series_influence_from_nt_runs_dict(nt_runs: dict[str, Any]) -> dict[str, Any]:
    """Aggregate influence across multiple consecutive run pairs."""
    pair_analyses = nt_runs.get("pair_analyses") or []
    agg: dict[str, dict[str, Any]] = {}
    improved_pairs = 0
    degraded_pairs = 0
    neutral_pairs = 0

    def _median(values: list[float]) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    def _percentile(values: list[float], p: float) -> float | None:
        if not values:
            return None
        if len(values) == 1:
            return values[0]
        ordered = sorted(values)
        k = (len(ordered) - 1) * p
        low = math.floor(k)
        high = math.ceil(k)
        if low == high:
            return ordered[int(k)]
        return ordered[low] + (ordered[high] - ordered[low]) * (k - low)

    for pair in pair_analyses:
        run_a = str(pair.get("run_a") or "")
        run_b = str(pair.get("run_b") or "")
        pair_improved_votes = 0
        pair_degraded_votes = 0
        for impact in pair.get("guc_impacts") or []:
            param = str(impact.get("guc") or "")
            if not param:
                continue
            row = agg.setdefault(
                param,
                {
                    "parameter": param,
                    "old": impact.get("value_from"),
                    "new": impact.get("value_to"),
                    "delta_pct": None,
                    "affected_metric": "",
                    "direction": str(impact.get("direction") or "changed"),
                    "impact": ImpactKind.NEUTRAL.value,
                    "confidence": ConfidenceLevel.MEDIUM.value,
                    "evidence_type": EvidenceType.PROBABLE.value,
                    "evidence_count": 0,
                    "workload_match_score": None,
                    "notes": "",
                    "_pairs": [],
                    "_metric_deltas": [],
                    "_metric_hits": {},
                    "_metric_deltas_by_name": {},
                },
            )
            row["new"] = impact.get("value_to")
            row["evidence_count"] += 1
            correlated = impact.get("correlated_metrics") or []
            improved_votes = 0
            degraded_votes = 0
            for metric_row in correlated:
                metric = str(metric_row.get("metric") or "")
                if metric:
                    row["_metric_hits"][metric] = row["_metric_hits"].get(metric, 0) + 1
                delta_pct = metric_row.get("delta_pct")
                if isinstance(delta_pct, (int, float)):
                    row["_metric_deltas"].append(float(delta_pct))
                    if metric:
                        row["_metric_deltas_by_name"].setdefault(metric, []).append(
                            float(delta_pct)
                        )
                improved = metric_row.get("improved")
                if improved is True:
                    improved_votes += 1
                elif improved is False:
                    degraded_votes += 1

            pair_effect = ImpactKind.NEUTRAL.value
            if improved_votes > degraded_votes:
                pair_effect = ImpactKind.IMPROVED.value
                pair_improved_votes += 1
            elif degraded_votes > improved_votes:
                pair_effect = ImpactKind.DEGRADED.value
                pair_degraded_votes += 1
            row["_pairs"].append(
                {
                    "label": f"{run_a}->{run_b}",
                    "correlated": list(correlated),
                    "correlated_majority": pair_effect,
                }
            )

        if pair_improved_votes > pair_degraded_votes:
            improved_pairs += 1
        elif pair_degraded_votes > pair_improved_votes:
            degraded_pairs += 1
        else:
            neutral_pairs += 1

    rows: list[dict[str, Any]] = []
    for row in agg.values():
        pairs = row.pop("_pairs")
        metric_deltas = row.pop("_metric_deltas")
        metric_hits = row.pop("_metric_hits")
        metric_by_name = row.pop("_metric_deltas_by_name")

        # Dominant metric: most pair hits; on a tie, first inserted name.
        if metric_hits:
            row["affected_metric"] = max(metric_hits.items(), key=lambda item: item[1])[0]
        dominant = str(row.get("affected_metric") or "")
        stats_deltas = metric_by_name.get(dominant) or metric_deltas
        median_delta = _median(stats_deltas)
        p25 = _percentile(stats_deltas, 0.25)
        p75 = _percentile(stats_deltas, 0.75)
        iqr = None
        if p25 is not None and p75 is not None:
            iqr = p75 - p25
        if stats_deltas:
            row["delta_pct"] = round(float(median_delta or 0.0), 2)

        if dominant and isinstance(row.get("delta_pct"), (int, float)):
            moved, impact_kind = expected_metric_impact(dominant, float(row["delta_pct"]))
            row["impact"] = impact_kind
            row["metric_direction"] = moved
        else:
            row["impact"] = ImpactKind.NEUTRAL.value
            row["metric_direction"] = "flat"

        effects: list[str] = []
        improved_count = 0
        degraded_count = 0
        majority_disagrees = False
        for pair in pairs:
            majority = str(pair.get("correlated_majority") or ImpactKind.NEUTRAL.value)
            pair_impact, used_fallback = _pair_impact_for_dominant(
                list(pair.get("correlated") or []),
                dominant,
                majority,
            )
            if (
                not used_fallback
                and pair_impact != majority
                and majority != ImpactKind.NEUTRAL.value
            ):
                majority_disagrees = True
            effects.append(f"{pair.get('label')}:{pair_impact}")
            if pair_impact == ImpactKind.IMPROVED.value:
                improved_count += 1
            elif pair_impact == ImpactKind.DEGRADED.value:
                degraded_count += 1

        dominant_votes = max(improved_count, degraded_count)
        stability_score = (
            round(dominant_votes / row["evidence_count"], 3)
            if row["evidence_count"] > 0
            else 0.0
        )
        row["stability_score"] = stability_score
        row["delta_iqr_pct"] = round(iqr, 2) if isinstance(iqr, (int, float)) else None
        row["pair_effects"] = list(effects)
        rec = recommend_series_confidence(row)
        row["confidence"] = rec["confidence"]
        row["evidence_type"] = rec["evidence_type"]
        row["noise_ratio"] = rec["noise_ratio"]
        row["confidence_reasons"] = list(rec["reasons"])
        row["notes"] = (
            f"pairs={row['evidence_count']}; stability={stability_score:.3f}; "
            f"effects={', '.join(effects[:4])}"
        )
        if rec["noisy"] and rec["noise_ratio"] is not None:
            row["notes"] += f"; noise={rec['noise_ratio']}"
        if majority_disagrees:
            row["notes"] += (
                "; other correlated metrics voted against the dominant metric"
            )
        rows.append(row)

    rows.sort(key=lambda item: abs(item.get("delta_pct") or 0), reverse=True)
    total_pairs = len(pair_analyses)
    return {
        "type": "influence_table_series",
        "mode": "series",
        "row_count": len(rows),
        "run_identity": {
            "mode": "series",
            "reports": nt_runs.get("reports") or [],
        },
        "workload_match": _series_workload_match(pair_analyses),
        "confidence_meta": {
            "mode": "series",
            "confidence": ConfidenceLevel.MEDIUM.value,
            "evidence_type": EvidenceType.PROBABLE.value,
            "notes": "Confidence is aggregated from repeated pair evidence.",
            "statistical": {
                "stability_threshold": STABILITY_HIGH,
                "noise_iqr_ratio_threshold": NOISE_IQR_RATIO,
                "min_pairs_for_proven": MIN_PAIRS_FOR_PROVEN,
            },
        },
        "functional_summary": {
            "total_pairs": total_pairs,
            "improved_pairs": improved_pairs,
            "degraded_pairs": degraded_pairs,
            "neutral_pairs": neutral_pairs,
            "series_mode": "median+iqr",
        },
        "settings_table": _build_series_settings_table(nt_runs),
        "metrics_table": _build_series_metrics_table(nt_runs),
        "rows": rows,
    }
