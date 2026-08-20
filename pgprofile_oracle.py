"""Rule-based oracle for influence tables: direction, magnitude sanity, field completeness."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pgprofile_contracts import (
    CONFIDENCE_LEVELS,
    EVIDENCE_TYPES,
    IMPACT_KINDS,
    PROVEN_WORKLOAD_MATCH_THRESHOLD,
    EvidenceType,
    ImpactKind,
    validate_contract_payload,
)
from pgprofile_influence import (
    CONFIDENCE_RANK,
    STABILITY_HIGH,
    expected_metric_impact,
    recommend_series_confidence,
)


VERDICT_PASS = "pass"
VERDICT_WARNING = "warning"
VERDICT_FAIL = "fail"
VERDICTS = (VERDICT_FAIL, VERDICT_WARNING, VERDICT_PASS)
LAYER_RULE = "rule_based"
LAYER_STATISTICAL = "statistical"
LAYER_LLM = "llm"
LAYER_HYBRID = "hybrid"

REQUIRED_ROW_FIELDS = (
    "parameter",
    "old",
    "new",
    "direction",
    "impact",
    "confidence",
    "evidence_type",
    "evidence_count",
    "notes",
)
METRIC_DIRECTIONS = frozenset({"up", "down", "flat"})
DELTA_PCT_WARN = 500.0
INFLUENCE_FILES = ("influence_table.json", "influence_table_series.json")


@dataclass
class OracleCheck:
    id: str
    status: str
    message: str
    where: str = ""
    layer: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "status": self.status,
            "message": self.message,
        }
        if self.where:
            payload["where"] = self.where
        if self.layer:
            payload["layer"] = self.layer
        return payload


@dataclass
class OracleReport:
    verdict: str
    checks: list[OracleCheck] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    skipped: bool = False
    layer: str = LAYER_RULE
    layers: list[dict[str, Any]] = field(default_factory=list)
    confidence_adjustments: list[dict[str, Any]] = field(default_factory=list)

    def add(self, check: OracleCheck) -> None:
        if not check.layer:
            check.layer = self.layer
        self.checks.append(check)

    def to_dict(self) -> dict[str, Any]:
        counts = {
            VERDICT_PASS: 0,
            VERDICT_WARNING: 0,
            VERDICT_FAIL: 0,
        }
        for item in self.checks:
            counts[item.status] = counts.get(item.status, 0) + 1
        reasons = [
            item.message
            for item in self.checks
            if item.status in {VERDICT_WARNING, VERDICT_FAIL}
        ]
        layers = list(self.layers) or [
            {"name": self.layer, "verdict": self.verdict, "skipped": self.skipped}
        ]
        return {
            "type": "oracle_report",
            "layer": self.layer,
            "layers": layers,
            "verdict": self.verdict,
            "skipped": self.skipped,
            "sources": list(self.sources),
            "counts": counts,
            "reasons": reasons,
            "confidence_adjustments": list(self.confidence_adjustments),
            "checks": [item.to_dict() for item in self.checks],
        }


def _worst(*statuses: str) -> str:
    for candidate in VERDICTS:
        if candidate in statuses:
            return candidate
    return VERDICT_PASS


def _is_bad_number(value: Any) -> bool:
    if not isinstance(value, (int, float)):
        return False
    return math.isnan(float(value)) or math.isinf(float(value))


def _sign_label(delta: float) -> str:
    if delta > 0:
        return "up"
    if delta < 0:
        return "down"
    return "flat"


def _check_contract(payload: dict[str, Any]) -> OracleCheck:
    try:
        validate_contract_payload(payload)
    except ValueError as exc:
        return OracleCheck(
            id="completeness.contract",
            status=VERDICT_FAIL,
            message=f"contract validation failed: {exc}",
        )
    return OracleCheck(
        id="completeness.contract",
        status=VERDICT_PASS,
        message="influence payload matches the JSON contract",
    )


def _check_payload_blocks(payload: dict[str, Any]) -> list[OracleCheck]:
    checks: list[OracleCheck] = []
    for name in ("rows", "workload_match", "confidence_meta"):
        if name == "rows":
            ok = isinstance(payload.get("rows"), list)
        else:
            ok = isinstance(payload.get(name), dict)
        checks.append(
            OracleCheck(
                id=f"completeness.{name}",
                status=VERDICT_PASS if ok else VERDICT_FAIL,
                message=(
                    f"{name} is present"
                    if ok
                    else f"required block {name} is missing or not an object"
                ),
                where=name,
            )
        )
    return checks


def _check_row_completeness(row: dict[str, Any], where: str) -> list[OracleCheck]:
    checks: list[OracleCheck] = []
    missing = [field for field in REQUIRED_ROW_FIELDS if field not in row]
    if missing:
        checks.append(
            OracleCheck(
                id="completeness.row_fields",
                status=VERDICT_FAIL,
                message=f"row is missing required fields: {', '.join(missing)}",
                where=where,
            )
        )
    if not str(row.get("parameter") or "").strip():
        checks.append(
            OracleCheck(
                id="completeness.parameter",
                status=VERDICT_FAIL,
                message="parameter name is empty",
                where=where,
            )
        )
    impact = row.get("impact")
    if impact is not None and impact not in IMPACT_KINDS:
        checks.append(
            OracleCheck(
                id="completeness.impact_enum",
                status=VERDICT_FAIL,
                message=f"impact {impact!r} is not improved|degraded|neutral",
                where=where,
            )
        )
    confidence = row.get("confidence")
    if confidence is not None and confidence not in CONFIDENCE_LEVELS:
        checks.append(
            OracleCheck(
                id="completeness.confidence_enum",
                status=VERDICT_FAIL,
                message=f"confidence {confidence!r} is not low|medium|high",
                where=where,
            )
        )
    evidence = row.get("evidence_type")
    if evidence is not None and evidence not in EVIDENCE_TYPES:
        checks.append(
            OracleCheck(
                id="completeness.evidence_enum",
                status=VERDICT_FAIL,
                message=f"evidence_type {evidence!r} is not probable|proven",
                where=where,
            )
        )
    return checks


def _check_direction(row: dict[str, Any], where: str) -> list[OracleCheck]:
    checks: list[OracleCheck] = []
    delta_pct = row.get("delta_pct")
    direction = str(row.get("direction") or "")
    metric_direction = str(row.get("metric_direction") or "")
    impact = str(row.get("impact") or "")
    metric = str(row.get("affected_metric") or "")

    if _is_bad_number(delta_pct):
        checks.append(
            OracleCheck(
                id="sanity.delta_pct_finite",
                status=VERDICT_FAIL,
                message="delta_pct is NaN or infinite",
                where=where,
            )
        )
        return checks

    if isinstance(delta_pct, (int, float)) and abs(float(delta_pct)) > DELTA_PCT_WARN:
        checks.append(
            OracleCheck(
                id="sanity.delta_pct_magnitude",
                status=VERDICT_WARNING,
                message=(
                    f"delta_pct {float(delta_pct):.1f}% looks implausible "
                    f"(threshold {DELTA_PCT_WARN:g}%)"
                ),
                where=where,
            )
        )

    axis = metric_direction if metric_direction in METRIC_DIRECTIONS else direction
    if axis in METRIC_DIRECTIONS and isinstance(delta_pct, (int, float)):
        expected_dir = _sign_label(float(delta_pct))
        if expected_dir != "flat" and axis != "flat" and axis != expected_dir:
            checks.append(
                OracleCheck(
                    id="direction.sign",
                    status=VERDICT_FAIL,
                    message=(
                        f"direction {axis!r} contradicts delta_pct "
                        f"{float(delta_pct):+.1f}%"
                    ),
                    where=where,
                )
            )

    if metric and isinstance(delta_pct, (int, float)) and impact in IMPACT_KINDS:
        _moved, expected_impact = expected_metric_impact(metric, float(delta_pct))
        if (
            expected_impact != ImpactKind.NEUTRAL.value
            and impact != ImpactKind.NEUTRAL.value
            and impact != expected_impact
        ):
            checks.append(
                OracleCheck(
                    id="direction.impact",
                    status=VERDICT_FAIL,
                    message=(
                        f"impact {impact!r} contradicts polarity of {metric} "
                        f"(expected {expected_impact} for Δ {float(delta_pct):+.1f}%)"
                    ),
                    where=where,
                )
            )
    return checks


def _check_summary_sanity(summary: dict[str, Any]) -> list[OracleCheck]:
    checks: list[OracleCheck] = []
    for bucket, expected_impact in (
        ("top_improved", ImpactKind.IMPROVED.value),
        ("top_degraded", ImpactKind.DEGRADED.value),
    ):
        for index, item in enumerate(summary.get(bucket) or []):
            if not isinstance(item, dict):
                continue
            where = f"functional_summary.{bucket}[{index}]"
            impact = item.get("impact")
            if impact and impact != expected_impact:
                checks.append(
                    OracleCheck(
                        id="direction.summary_bucket",
                        status=VERDICT_FAIL,
                        message=f"{bucket} contains impact {impact!r}",
                        where=where,
                    )
                )
            delta_pct = item.get("delta_pct")
            metric = str(item.get("metric") or "")
            if _is_bad_number(delta_pct):
                checks.append(
                    OracleCheck(
                        id="sanity.summary_delta_finite",
                        status=VERDICT_FAIL,
                        message="summary delta_pct is NaN or infinite",
                        where=where,
                    )
                )
                continue
            if isinstance(delta_pct, (int, float)) and abs(float(delta_pct)) > DELTA_PCT_WARN:
                checks.append(
                    OracleCheck(
                        id="sanity.summary_delta_magnitude",
                        status=VERDICT_WARNING,
                        message=(
                            f"{metric or 'metric'} Δ={float(delta_pct):.1f}% looks implausible"
                        ),
                        where=where,
                    )
                )
            if metric and isinstance(delta_pct, (int, float)) and impact in IMPACT_KINDS:
                _moved, expected = expected_metric_impact(metric, float(delta_pct))
                if (
                    expected != ImpactKind.NEUTRAL.value
                    and impact != ImpactKind.NEUTRAL.value
                    and impact != expected
                ):
                    checks.append(
                        OracleCheck(
                            id="direction.summary_impact",
                            status=VERDICT_FAIL,
                            message=(
                                f"summary impact {impact!r} contradicts polarity of {metric}"
                            ),
                            where=where,
                        )
                    )
    return checks


def _is_series_payload(payload: dict[str, Any]) -> bool:
    if payload.get("type") == "influence_table_series":
        return True
    identity = payload.get("run_identity") or {}
    return isinstance(identity, dict) and identity.get("mode") == "series"


def _pair_effect_labels(row: dict[str, Any]) -> list[str]:
    raw = row.get("pair_effects")
    if isinstance(raw, list) and raw:
        return [str(item) for item in raw]
    notes = str(row.get("notes") or "")
    marker = "effects="
    if marker not in notes:
        return []
    tail = notes.split(marker, 1)[1]
    tail = tail.split(";")[0]
    return [part.strip() for part in tail.split(",") if part.strip()]


def _check_proven(payload: dict[str, Any], row: dict[str, Any], where: str) -> list[OracleCheck]:
    evidence = row.get("evidence_type")
    if evidence != EvidenceType.PROVEN.value:
        return []
    if _is_series_payload(payload):
        rec = recommend_series_confidence(row)
        if rec["evidence_type"] != EvidenceType.PROVEN.value:
            return [
                OracleCheck(
                    id="direction.proven_criteria",
                    status=VERDICT_FAIL,
                    message=(
                        "evidence_type=proven without series statistical criteria: "
                        + "; ".join(rec["reasons"])
                    ),
                    where=where,
                )
            ]
        return []
    confidence = payload.get("confidence_meta") or {}
    isolated = bool(confidence.get("isolated_change"))
    changed = confidence.get("changed_params_count")
    score = row.get("workload_match_score")
    if score is None:
        score = (payload.get("workload_match") or {}).get("workload_match_score")
    reasons: list[str] = []
    if not isolated:
        reasons.append("isolated_change is not true")
    if changed != 1:
        reasons.append(f"changed_params_count is {changed!r}, expected 1")
    if not isinstance(score, (int, float)) or float(score) < PROVEN_WORKLOAD_MATCH_THRESHOLD:
        reasons.append(
            f"workload_match_score {score!r} is below {PROVEN_WORKLOAD_MATCH_THRESHOLD}"
        )
    if reasons:
        return [
            OracleCheck(
                id="direction.proven_criteria",
                status=VERDICT_FAIL,
                message="evidence_type=proven without proof criteria: " + "; ".join(reasons),
                where=where,
            )
        ]
    return []


def evaluate_influence_payload(
    payload: dict[str, Any], *, source: str = ""
) -> OracleReport:
    """Run the rule-based oracle against one influence table payload."""
    report = OracleReport(verdict=VERDICT_PASS, sources=[source] if source else [])
    if not isinstance(payload, dict):
        report.add(
            OracleCheck(
                id="completeness.payload",
                status=VERDICT_FAIL,
                message="influence payload is not an object",
            )
        )
        report.verdict = VERDICT_FAIL
        return report

    report.add(_check_contract(payload))
    for check in _check_payload_blocks(payload):
        report.add(check)

    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    if not rows:
        report.add(
            OracleCheck(
                id="completeness.rows_nonempty",
                status=VERDICT_WARNING,
                message="influence table has no rows to validate",
                where="rows",
            )
        )

    attributed = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            report.add(
                OracleCheck(
                    id="completeness.row_type",
                    status=VERDICT_FAIL,
                    message="influence row is not an object",
                    where=f"rows[{index}]",
                )
            )
            continue
        where = f"rows[{index}].{row.get('parameter') or index}"
        for check in _check_row_completeness(row, where):
            report.add(check)
        for check in _check_direction(row, where):
            report.add(check)
        for check in _check_proven(payload, row, where):
            report.add(check)
        if str(row.get("affected_metric") or "").strip():
            attributed += 1

    if rows and attributed == 0:
        report.add(
            OracleCheck(
                id="sanity.unattributed",
                status=VERDICT_WARNING,
                message="every changed parameter is unattributed; treat the table as a list of diffs only",
                where="rows",
            )
        )

    summary = payload.get("functional_summary")
    if isinstance(summary, dict):
        for check in _check_summary_sanity(summary):
            report.add(check)

    if not any(item.status == VERDICT_FAIL and item.id.startswith("completeness.") for item in report.checks):
        report.add(
            OracleCheck(
                id="completeness.rows",
                status=VERDICT_PASS,
                message=f"{len(rows)} influence row(s) have the required fields",
                where="rows",
            )
        )

    report.verdict = _worst(*(item.status for item in report.checks))
    return report


def evaluate_statistical_payload(
    payload: dict[str, Any], *, source: str = ""
) -> OracleReport:
    """Second-line oracle: series stability, noise (IQR), confidence recalculation."""
    report = OracleReport(
        verdict=VERDICT_PASS,
        sources=[source] if source else [],
        layer=LAYER_STATISTICAL,
    )
    if not isinstance(payload, dict) or not _is_series_payload(payload):
        report.skipped = True
        report.add(
            OracleCheck(
                id="statistical.series_present",
                status=VERDICT_PASS,
                message="not a series influence table; statistical oracle skipped",
            )
        )
        return report

    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    if not rows:
        report.add(
            OracleCheck(
                id="statistical.rows",
                status=VERDICT_WARNING,
                message="series influence table has no rows to score statistically",
                where="rows",
            )
        )
        report.verdict = _worst(*(item.status for item in report.checks))
        return report

    scored = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            report.add(
                OracleCheck(
                    id="statistical.row_type",
                    status=VERDICT_FAIL,
                    message="series influence row is not an object",
                    where=f"rows[{index}]",
                )
            )
            continue
        param = str(row.get("parameter") or index)
        where = f"rows[{index}].{param}"
        stability = row.get("stability_score")
        if not isinstance(stability, (int, float)):
            report.add(
                OracleCheck(
                    id="statistical.stability_present",
                    status=VERDICT_FAIL,
                    message="stability_score is missing",
                    where=where,
                )
            )
            continue
        if not 0.0 <= float(stability) <= 1.0:
            report.add(
                OracleCheck(
                    id="statistical.stability_range",
                    status=VERDICT_FAIL,
                    message=f"stability_score {stability!r} is outside [0, 1]",
                    where=where,
                )
            )

        effects = _pair_effect_labels(row)
        labels = [item.split(":")[-1] for item in effects]
        mixed = (
            ImpactKind.IMPROVED.value in labels and ImpactKind.DEGRADED.value in labels
        )
        if mixed and float(stability) >= STABILITY_HIGH:
            report.add(
                OracleCheck(
                    id="statistical.stability_consistency",
                    status=VERDICT_FAIL,
                    message="pair effects mix improved and degraded but stability_score claims a stable majority",
                    where=where,
                )
            )
        elif mixed:
            report.add(
                OracleCheck(
                    id="statistical.stability_consistency",
                    status=VERDICT_WARNING,
                    message="pair effects mix improved and degraded; treat the row as unstable",
                    where=where,
                )
            )

        rec = recommend_series_confidence(row)
        scored += 1
        claimed_conf = str(row.get("confidence") or "")
        claimed_ev = str(row.get("evidence_type") or "")
        claimed_rank = CONFIDENCE_RANK.get(claimed_conf, -1)
        recommended_rank = CONFIDENCE_RANK.get(rec["confidence"], 0)
        if claimed_rank > recommended_rank:
            report.add(
                OracleCheck(
                    id="statistical.confidence_recalc",
                    status=VERDICT_FAIL,
                    message=(
                        f"confidence {claimed_conf!r} is higher than statistical "
                        f"{rec['confidence']!r}: " + "; ".join(rec["reasons"])
                    ),
                    where=where,
                )
            )
            report.confidence_adjustments.append(
                {
                    "parameter": param,
                    "from": claimed_conf,
                    "to": rec["confidence"],
                    "evidence_type_from": claimed_ev,
                    "evidence_type_to": rec["evidence_type"],
                    "reason": "; ".join(rec["reasons"]),
                }
            )
        elif rec["noisy"]:
            report.add(
                OracleCheck(
                    id="statistical.noise",
                    status=VERDICT_WARNING,
                    message=(
                        f"effect is sensitive to noise (IQR/|Δ|={rec['noise_ratio']}); "
                        f"confidence stays {rec['confidence']}"
                    ),
                    where=where,
                )
            )
        elif claimed_conf == rec["confidence"] and claimed_ev == rec["evidence_type"]:
            report.add(
                OracleCheck(
                    id="statistical.confidence_recalc",
                    status=VERDICT_PASS,
                    message=(
                        f"confidence {claimed_conf}/{claimed_ev} matches statistical recommendation"
                    ),
                    where=where,
                )
            )
        else:
            report.add(
                OracleCheck(
                    id="statistical.confidence_recalc",
                    status=VERDICT_PASS,
                    message=(
                        f"confidence {claimed_conf} is not above statistical "
                        f"{rec['confidence']}"
                    ),
                    where=where,
                )
            )

    if scored:
        report.add(
            OracleCheck(
                id="statistical.rows",
                status=VERDICT_PASS,
                message=f"{scored} series row(s) scored for stability and noise",
                where="rows",
            )
        )
    report.verdict = _worst(*(item.status for item in report.checks))
    return report


def _llm_quality_report(output_dir: Path) -> OracleReport:
    paths = sorted(output_dir.glob("llm_quality_*.json"))
    if not paths:
        skipped = OracleReport(
            verdict=VERDICT_PASS, skipped=True, layer=LAYER_LLM
        )
        skipped.add(
            OracleCheck(
                id="llm.answer_present",
                status=VERDICT_PASS,
                message="no LLM answer in this run; llm oracle skipped",
            )
        )
        return skipped

    merged = OracleReport(verdict=VERDICT_PASS, layer=LAYER_LLM)
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            broken = OracleReport(verdict=VERDICT_FAIL, sources=[path.name], layer=LAYER_LLM)
            broken.add(
                OracleCheck(
                    id="llm.json",
                    status=VERDICT_FAIL,
                    message=f"{path.name} is not valid JSON: {exc}",
                    where=path.name,
                )
            )
            merged.checks.extend(broken.checks)
            merged.sources.append(path.name)
            continue
        merged.sources.append(path.name)
        for item in payload.get("checks") or []:
            if not isinstance(item, dict):
                continue
            merged.add(
                OracleCheck(
                    id=str(item.get("id") or "llm.check"),
                    status=str(item.get("status") or VERDICT_PASS),
                    message=str(item.get("message") or ""),
                    where=str(item.get("where") or path.name),
                )
            )
        score = payload.get("score")
        if score is not None:
            merged.add(
                OracleCheck(
                    id="llm.score",
                    status=str(payload.get("verdict") or VERDICT_PASS),
                    message=(
                        f"quality score {score}/100, publishable="
                        f"{bool(payload.get('publishable'))}"
                    ),
                    where=path.name,
                )
            )
    merged.verdict = _worst(*(item.status for item in merged.checks)) or VERDICT_PASS
    return merged


def _layer_summary(report: OracleReport) -> dict[str, Any]:
    return {
        "name": report.layer,
        "verdict": report.verdict,
        "skipped": report.skipped,
    }


def evaluate_output_dir(output_dir: Path) -> OracleReport:
    """Evaluate pair and/or series influence artifacts in an analysis output directory."""
    rule_reports: list[OracleReport] = []
    for name in INFLUENCE_FILES:
        path = output_dir / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            broken = OracleReport(verdict=VERDICT_FAIL, sources=[name], layer=LAYER_RULE)
            broken.add(
                OracleCheck(
                    id="completeness.json",
                    status=VERDICT_FAIL,
                    message=f"{name} is not valid JSON: {exc}",
                    where=name,
                )
            )
            rule_reports.append(broken)
            continue
        rule_reports.append(evaluate_influence_payload(payload, source=name))

    series_name = "influence_table_series.json"
    series_path = output_dir / series_name
    if series_path.is_file():
        try:
            series_payload = json.loads(series_path.read_text(encoding="utf-8"))
            statistical = evaluate_statistical_payload(series_payload, source=series_name)
        except json.JSONDecodeError as exc:
            statistical = OracleReport(
                verdict=VERDICT_FAIL, sources=[series_name], layer=LAYER_STATISTICAL
            )
            statistical.add(
                OracleCheck(
                    id="statistical.json",
                    status=VERDICT_FAIL,
                    message=f"{series_name} is not valid JSON: {exc}",
                    where=series_name,
                )
            )
    else:
        statistical = OracleReport(
            verdict=VERDICT_PASS, skipped=True, layer=LAYER_STATISTICAL
        )
        statistical.add(
            OracleCheck(
                id="statistical.series_present",
                status=VERDICT_PASS,
                message="no series influence table; statistical oracle skipped",
            )
        )

    llm = _llm_quality_report(output_dir)

    if not rule_reports:
        skipped = OracleReport(verdict=VERDICT_PASS, skipped=True, layer=LAYER_HYBRID)
        skipped.layers = [
            {"name": LAYER_RULE, "verdict": VERDICT_PASS, "skipped": True},
            _layer_summary(statistical),
            _layer_summary(llm),
        ]
        skipped.add(
            OracleCheck(
                id="completeness.influence_present",
                status=VERDICT_PASS,
                message="no influence table in this run; rule-based oracle skipped",
            )
        )
        skipped.checks.extend(statistical.checks)
        skipped.checks.extend(llm.checks)
        skipped.sources = list(statistical.sources) + list(llm.sources)
        skipped.verdict = _worst(skipped.verdict, statistical.verdict, llm.verdict)
        skipped.skipped = statistical.skipped and llm.skipped
        return skipped

    merged = OracleReport(
        verdict=_worst(
            *(item.verdict for item in rule_reports), statistical.verdict, llm.verdict
        ),
        sources=[name for item in rule_reports for name in item.sources]
        + list(statistical.sources)
        + list(llm.sources),
        layer=LAYER_HYBRID,
        layers=[_layer_summary(item) for item in rule_reports]
        + [_layer_summary(statistical), _layer_summary(llm)],
        confidence_adjustments=list(statistical.confidence_adjustments),
        skipped=all(item.skipped for item in rule_reports)
        and statistical.skipped
        and llm.skipped,
    )
    for item in rule_reports:
        merged.checks.extend(item.checks)
    merged.checks.extend(statistical.checks)
    merged.checks.extend(llm.checks)
    merged.verdict = _worst(*(item.status for item in merged.checks))
    return merged


def _tag_report_layer(report: OracleReport, layer: str) -> OracleReport:
    report.layer = layer
    for check in report.checks:
        if not check.layer:
            check.layer = layer
    return report


def oracle_report_from_dict(payload: dict[str, Any]) -> OracleReport:
    """Rebuild an OracleReport from oracle_report.json (additive fields allowed)."""
    report = OracleReport(
        verdict=str(payload.get("verdict") or VERDICT_PASS),
        sources=[str(item) for item in (payload.get("sources") or [])],
        skipped=bool(payload.get("skipped")),
        layer=str(payload.get("layer") or LAYER_HYBRID),
        layers=[item for item in (payload.get("layers") or []) if isinstance(item, dict)],
        confidence_adjustments=[
            item for item in (payload.get("confidence_adjustments") or []) if isinstance(item, dict)
        ],
    )
    for item in payload.get("checks") or []:
        if not isinstance(item, dict):
            continue
        report.checks.append(
            OracleCheck(
                id=str(item.get("id") or "check"),
                status=str(item.get("status") or VERDICT_PASS),
                message=str(item.get("message") or ""),
                where=str(item.get("where") or ""),
                layer=str(item.get("layer") or ""),
            )
        )
    return report


def replace_llm_layer(output_dir: Path) -> OracleReport:
    """Refresh the llm layer from llm_quality_*.json without re-scoring influence.

    Falls back to a full evaluate_output_dir when the stored oracle has no layer
    tags (artifacts written before this seam).
    """
    path = output_dir / "oracle_report.json"
    if not path.is_file():
        return evaluate_output_dir(output_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return evaluate_output_dir(output_dir)
    if not isinstance(payload, dict):
        return evaluate_output_dir(output_dir)
    stored = oracle_report_from_dict(payload)
    if stored.checks and not any(check.layer for check in stored.checks):
        return evaluate_output_dir(output_dir)

    llm = _tag_report_layer(_llm_quality_report(output_dir), LAYER_LLM)
    kept_checks = [check for check in stored.checks if check.layer != LAYER_LLM]
    kept_sources = [
        source for source in stored.sources if not str(source).startswith("llm_quality_")
    ]
    kept_layers = [item for item in stored.layers if item.get("name") != LAYER_LLM]
    merged = OracleReport(
        verdict=_worst(*(item.status for item in kept_checks), llm.verdict),
        sources=kept_sources + list(llm.sources),
        skipped=bool(stored.skipped) and llm.skipped,
        layer=stored.layer or LAYER_HYBRID,
        layers=kept_layers + [_layer_summary(llm)],
        confidence_adjustments=list(stored.confidence_adjustments),
        checks=kept_checks,
    )
    merged.checks.extend(llm.checks)
    merged.verdict = _worst(*(item.status for item in merged.checks)) or VERDICT_PASS
    return merged


def render_oracle_markdown(report: OracleReport) -> str:
    """Human-readable oracle verdict.

    Пропущенные проверки не выдаются за успешные: PASS означает «проверили и
    замечаний нет», а не «проверять было нечего».
    """
    payload = report.to_dict()
    layer_bits = []
    for item in payload.get("layers") or []:
        name = item.get("name") or "?"
        verdict = item.get("verdict") or "?"
        skipped = " (skipped)" if item.get("skipped") else ""
        layer_bits.append(f"{name}={verdict}{skipped}")
    headline = "НЕ ПРОВЕРЯЛОСЬ" if report.skipped else report.verdict.upper()
    lines = [
        f"# Oracle: {headline}",
        "",
        f"Слои: {', '.join(layer_bits) or report.layer}. "
        f"Источники: {', '.join(report.sources) or '—'}.",
        "",
    ]
    reasons = payload.get("reasons") or []
    if report.skipped:
        lines.append(
            "Автопроверка выводов не проверяла ничего: таблица влияния не строилась. "
            "Это не оценка состояния БД — смотрите находки и план действий."
        )
    elif not reasons:
        lines.append("Замечаний нет.")
    else:
        for reason in reasons:
            lines.append(f"- {reason}")
    adjustments = payload.get("confidence_adjustments") or []
    if adjustments:
        lines += ["", "## Пересчёт confidence", ""]
        for item in adjustments:
            lines.append(
                f"- {item.get('parameter')}: {item.get('from')} → {item.get('to')}"
                f" ({item.get('reason')})"
            )
    lines.append("")
    return "\n".join(lines)


def write_oracle_artifacts(output_dir: Path, report: OracleReport) -> Path:
    """Persist oracle_report.json and oracle_report.md from an already-evaluated report."""
    payload = report.to_dict()
    json_path = output_dir / "oracle_report.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "oracle_report.md").write_text(
        render_oracle_markdown(report), encoding="utf-8"
    )
    return json_path


def write_oracle_report(output_dir: Path, report: OracleReport | None = None) -> Path:
    """Persist oracle + quality reports from one snapshot."""
    from pgprofile_quality import persist_quality_snapshot

    return persist_quality_snapshot(output_dir, oracle_report=report)
