"""Validate LLM answers against analysis artifacts: structure, grounded claims, quality score."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pgprofile_contracts import EVIDENCE_TYPES, EvidenceType


ANSWER_VERDICTS = frozenset({"go", "no-go", "need-validation"})
REQUIRED_ANSWER_FIELDS = (
    "verdict",
    "summary",
    "claims",
    "recommendations",
    "risks",
    "missing_data",
)
PROVEN_WORDS = ("proven", "доказан", "доказано", "доказанная", "доказанный")
SCORE_PASS = 80
SCORE_FAIL = 50
LAYER_LLM = "llm"

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from a raw model reply (plain JSON or a fenced block)."""
    raw = (text or "").strip()
    if not raw:
        return None
    candidates: list[str] = []
    if raw.startswith("{"):
        candidates.append(raw)
    fenced = _JSON_FENCE.findall(raw)
    candidates.extend(fenced)
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start : end + 1])
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", str(value).lower())


def _name_parts(value: str) -> tuple[str, str]:
    raw = str(value).strip()
    full = _norm(raw)
    last = _norm(raw.split(".")[-1] if raw else "")
    return full, last


def _names_equivalent(left: str, right: str) -> bool:
    """Exact normalized equality, or a full qualified metric vs its last segment."""
    left_full, left_last = _name_parts(left)
    right_full, right_last = _name_parts(right)
    if not left_full or not right_full:
        return False
    if left_full == right_full:
        return True
    return left_full == right_last or left_last == right_full


def _matched_catalog_key(subject: str, candidates: Any) -> str | None:
    needle_full, _needle_last = _name_parts(subject)
    if not needle_full or len(needle_full) < 3:
        return None
    for token in candidates or []:
        if _names_equivalent(subject, str(token)):
            return str(token)
    return None


def _subject_in_catalog(subject: str, catalog: dict[str, Any]) -> bool:
    return _matched_catalog_key(subject, catalog.get("subjects") or []) is not None


def load_evidence_catalog(output_dir: Path) -> dict[str, Any]:
    """Collect parameters, metrics and evidence types the model is allowed to cite."""
    parameters: set[str] = set()
    metrics: set[str] = set()
    evidence_by_param: dict[str, str] = {}
    attributed_count = 0
    for name in ("influence_table.json", "influence_table_series.json"):
        path = output_dir / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for row in payload.get("rows") or []:
            if not isinstance(row, dict):
                continue
            param = str(row.get("parameter") or "").strip()
            if param:
                parameters.add(param)
                evidence_by_param[param] = str(
                    row.get("evidence_type") or EvidenceType.PROBABLE.value
                )
            metric = str(row.get("affected_metric") or "").strip()
            if metric:
                metrics.add(metric)
                metrics.add(metric.split(".")[-1])
                attributed_count += 1
        summary = payload.get("functional_summary") or {}
        for bucket_name in ("top_improved", "top_degraded"):
            for item in summary.get(bucket_name) or []:
                if isinstance(item, dict) and item.get("metric"):
                    metrics.add(str(item["metric"]))
                    metrics.add(str(item["metric"]).split(".")[-1])
    findings_path = output_dir / "findings.json"
    if findings_path.is_file():
        try:
            findings = json.loads(findings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            findings = {}
        for item in (findings.get("findings") or []) if isinstance(findings, dict) else []:
            if not isinstance(item, dict):
                continue
            if item.get("id"):
                metrics.add(str(item["id"]))
            message = str(item.get("message") or "")
            if message:
                metrics.add(message.split()[0])
    return {
        "parameters": parameters,
        "metrics": metrics,
        "evidence_by_param": evidence_by_param,
        "attributed_count": attributed_count,
        "subjects": parameters | metrics,
    }


def _check(
    checks: list[dict[str, str]],
    check_id: str,
    status: str,
    message: str,
    where: str = "",
) -> None:
    item = {"id": check_id, "status": status, "message": message}
    if where:
        item["where"] = where
    checks.append(item)


def evaluate_llm_answer(
    text: str,
    catalog: dict[str, Any] | None = None,
    *,
    task: str = "summary",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Score one model reply. Does not call the provider."""
    catalog = catalog or {
        "parameters": set(),
        "metrics": set(),
        "evidence_by_param": {},
        "attributed_count": 0,
        "subjects": set(),
    }
    checks: list[dict[str, str]] = []
    score = 100
    parsed = extract_json_object(text)
    if parsed is None:
        _check(checks, "llm.structure", "fail", "answer is not a JSON object")
        return _quality_payload(
            task=task,
            checks=checks,
            score=0,
            parsed=False,
            dry_run=dry_run,
            publishable=False,
        )

    missing = [field for field in REQUIRED_ANSWER_FIELDS if field not in parsed]
    if missing:
        _check(
            checks,
            "llm.structure",
            "fail",
            f"JSON is missing required fields: {', '.join(missing)}",
        )
        score = 0
    else:
        _check(checks, "llm.structure", "pass", "answer JSON has the required fields")

    verdict = str(parsed.get("verdict") or "")
    if "verdict" in parsed and verdict not in ANSWER_VERDICTS:
        _check(
            checks,
            "llm.verdict_enum",
            "fail",
            f"verdict {verdict!r} is not go|no-go|need-validation",
        )
        score -= 20
    elif "verdict" in parsed:
        _check(checks, "llm.verdict_enum", "pass", "verdict is an allowed value")

    summary = str(parsed.get("summary") or "").strip()
    if "summary" in parsed and not summary:
        _check(checks, "llm.summary", "fail", "summary is empty")
        score -= 15
    elif summary:
        _check(checks, "llm.summary", "pass", "summary is present")

    claims = parsed.get("claims")
    if claims is None:
        claims = []
    if not isinstance(claims, list):
        _check(checks, "llm.claims_type", "fail", "claims must be a list")
        score -= 20
        claims = []

    recs = parsed.get("recommendations")
    if recs is None:
        recs = []
    if not isinstance(recs, list):
        _check(checks, "llm.recommendations_type", "fail", "recommendations must be a list")
        score -= 20
        recs = []

    unsupported: list[str] = []
    overclaims: list[str] = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            _check(
                checks,
                "llm.claim_type",
                "fail",
                "claim is not an object",
                where=f"claims[{index}]",
            )
            score -= 15
            continue
        subject = str(claim.get("subject") or claim.get("metric_or_parameter") or "").strip()
        statement = str(claim.get("statement") or "")
        where = f"claims[{index}].{subject or index}"
        if not subject:
            _check(checks, "llm.claim_subject", "fail", "claim has no subject", where=where)
            score -= 15
            continue
        matched_subject = _matched_catalog_key(subject, catalog.get("subjects") or [])
        if catalog.get("subjects") and not matched_subject:
            unsupported.append(subject)
            _check(
                checks,
                "llm.claim_grounded",
                "fail",
                f"claim subject {subject!r} is not in the analysis artifacts",
                where=where,
            )
            score -= 25
        claimed_evidence = str(claim.get("evidence_type") or "").strip().lower()
        evidence_by_param = catalog.get("evidence_by_param") or {}
        matched_param = _matched_catalog_key(subject, evidence_by_param)
        catalog_evidence = evidence_by_param.get(matched_param) if matched_param else None
        statement_claims_proof = any(word in statement.lower() for word in PROVEN_WORDS)
        if claimed_evidence and claimed_evidence not in EVIDENCE_TYPES and claimed_evidence != "none":
            _check(
                checks,
                "llm.claim_evidence_enum",
                "fail",
                f"evidence_type {claimed_evidence!r} is not probable|proven|none",
                where=where,
            )
            score -= 10
        if (
            catalog_evidence == EvidenceType.PROBABLE.value
            and (claimed_evidence == EvidenceType.PROVEN.value or statement_claims_proof)
        ):
            overclaims.append(subject)
            _check(
                checks,
                "llm.claim_overconfident",
                "fail",
                f"claim treats {subject} as proven, but the table marks it probable",
                where=where,
            )
            score -= 30

    for index, rec in enumerate(recs):
        if not isinstance(rec, dict):
            continue
        param = str(rec.get("parameter") or rec.get("subject") or "").strip()
        if not param:
            continue
        if catalog.get("parameters") and not _matched_catalog_key(param, catalog.get("parameters")):
            unsupported.append(param)
            _check(
                checks,
                "llm.recommendation_grounded",
                "fail",
                f"recommendation parameter {param!r} is not in the analysis artifacts",
                where=f"recommendations[{index}]",
            )
            score -= 25

    if catalog.get("attributed_count") and not claims:
        _check(
            checks,
            "llm.claims_present",
            "warning",
            "influence table has attributed rows, but the answer cites no claims",
        )
        score -= 10
    elif isinstance(parsed.get("claims"), list) and not unsupported and not overclaims:
        _check(
            checks,
            "llm.claim_grounded",
            "pass",
            f"{len(claims)} claim(s) match analysis artifacts" if claims else "no claims to ground",
        )

    score = max(0, min(100, score))
    fail_checks = [item for item in checks if item["status"] == "fail"]
    warn_checks = [item for item in checks if item["status"] == "warning"]
    if fail_checks or score < SCORE_FAIL:
        quality_verdict = "fail"
    elif warn_checks or score < SCORE_PASS or dry_run:
        quality_verdict = "warning"
    else:
        quality_verdict = "pass"

    if dry_run:
        quality_verdict = "warning" if quality_verdict == "pass" else quality_verdict

    publishable = quality_verdict != "fail" and not dry_run
    if not publishable:
        _check(
            checks,
            "llm.publish",
            "fail" if quality_verdict == "fail" else "warning",
            "dry-run answer is not publishable"
            if dry_run and quality_verdict != "fail"
            else "final publish status is blocked",
        )
    else:
        _check(checks, "llm.publish", "pass", "answer may be published")

    return _quality_payload(
        task=task,
        checks=checks,
        score=score,
        parsed=True,
        dry_run=dry_run,
        publishable=publishable,
        extra={
            "answer_verdict": verdict,
            "unsupported_subjects": unsupported,
            "overclaims": overclaims,
        },
        forced_verdict=quality_verdict,
    )


def _quality_payload(
    *,
    task: str,
    checks: list[dict[str, str]],
    score: int,
    parsed: bool,
    dry_run: bool,
    publishable: bool,
    extra: dict[str, Any] | None = None,
    forced_verdict: str | None = None,
) -> dict[str, Any]:
    statuses = {item["status"] for item in checks}
    if forced_verdict:
        verdict = forced_verdict
    elif "fail" in statuses or score < SCORE_FAIL:
        verdict = "fail"
    elif "warning" in statuses or score < SCORE_PASS:
        verdict = "warning"
    else:
        verdict = "pass"
    reasons = [
        item["message"] for item in checks if item["status"] in {"fail", "warning"}
    ]
    payload = {
        "type": "llm_quality",
        "layer": LAYER_LLM,
        "task": task,
        "verdict": verdict,
        "score": score,
        "publishable": publishable,
        "parsed": parsed,
        "dry_run": dry_run,
        "reasons": reasons,
        "checks": checks,
        "counts": {
            "pass": sum(1 for item in checks if item["status"] == "pass"),
            "warning": sum(1 for item in checks if item["status"] == "warning"),
            "fail": sum(1 for item in checks if item["status"] == "fail"),
        },
    }
    if extra:
        payload.update(extra)
    return payload


def record_llm_quality(
    output_dir: Path,
    text: str,
    *,
    task: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Persist llm_quality_<task>.json and refresh the llm oracle layer."""
    report = evaluate_llm_answer(
        text,
        load_evidence_catalog(output_dir),
        task=task,
        dry_run=dry_run,
    )
    path = output_dir / f"llm_quality_{task}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from pgprofile_oracle import replace_llm_layer, write_oracle_report

    write_oracle_report(output_dir, replace_llm_layer(output_dir))
    report["quality_file"] = path.name
    return report
