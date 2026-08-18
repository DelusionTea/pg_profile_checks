"""Unified quality report: oracle layers + confidence trail + LLM publish gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pgprofile_influence import CONFIDENCE_RANK
from pgprofile_oracle import INFLUENCE_FILES, evaluate_output_dir, write_oracle_artifacts


QUALITY_JSON = "quality_report.json"
QUALITY_MD = "quality_report.md"
BASELINE_CONFIDENCE = "medium"


def _change_kind(from_level: str, to_level: str) -> str:
    left = CONFIDENCE_RANK.get(from_level, CONFIDENCE_RANK[BASELINE_CONFIDENCE])
    right = CONFIDENCE_RANK.get(to_level, CONFIDENCE_RANK[BASELINE_CONFIDENCE])
    if right > left:
        return "upgrade"
    if right < left:
        return "downgrade"
    return "unchanged"


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def collect_confidence_trail(output_dir: Path) -> list[dict[str, Any]]:
    """Why each parameter (and the run as a whole) ended up at this confidence."""
    trail: list[dict[str, Any]] = []
    for name in INFLUENCE_FILES:
        payload = _load_json(output_dir / name)
        if payload is None:
            continue
        meta = payload.get("confidence_meta") if isinstance(payload.get("confidence_meta"), dict) else {}
        meta_conf = str(meta.get("confidence") or BASELINE_CONFIDENCE)
        meta_ev = str(meta.get("evidence_type") or "probable")
        meta_notes = str(meta.get("notes") or "").strip()
        is_series = payload.get("type") == "influence_table_series"
        trail.append(
            {
                "parameter": "*",
                "scope": "run",
                "source": name,
                "from": BASELINE_CONFIDENCE,
                "to": meta_conf,
                "change": _change_kind(BASELINE_CONFIDENCE, meta_conf),
                "confidence": meta_conf,
                "evidence_type": meta_ev,
                "reason": meta_notes
                or (
                    "series confidence is aggregated from pair evidence"
                    if is_series
                    else "pair confidence baseline"
                ),
            }
        )
        for row in payload.get("rows") or []:
            if not isinstance(row, dict):
                continue
            param = str(row.get("parameter") or "").strip()
            if not param:
                continue
            conf = str(row.get("confidence") or BASELINE_CONFIDENCE)
            evidence = str(row.get("evidence_type") or "probable")
            reasons = row.get("confidence_reasons")
            if not isinstance(reasons, list) or not reasons:
                notes = str(row.get("notes") or "").strip()
                reasons = [notes] if notes else []
            baseline = BASELINE_CONFIDENCE if is_series else meta_conf
            if not str(row.get("affected_metric") or "").strip() and not is_series:
                baseline = meta_conf
            trail.append(
                {
                    "parameter": param,
                    "scope": "row",
                    "source": name,
                    "from": baseline,
                    "to": conf,
                    "change": _change_kind(baseline, conf),
                    "confidence": conf,
                    "evidence_type": evidence,
                    "reason": "; ".join(str(item) for item in reasons if item)
                    or str(row.get("notes") or "row confidence"),
                }
            )
    return trail


def _llm_block(output_dir: Path) -> dict[str, Any]:
    files = sorted(output_dir.glob("llm_quality_*.json"))
    if not files:
        return {"present": False, "publishable": None, "score": None, "verdict": None, "task": None}
    latest = _load_json(files[-1]) or {}
    return {
        "present": True,
        "publishable": latest.get("publishable"),
        "score": latest.get("score"),
        "verdict": latest.get("verdict"),
        "task": latest.get("task"),
        "reasons": list(latest.get("reasons") or [])[:8],
        "source": files[-1].name,
    }


def build_quality_report(
    output_dir: Path, *, oracle_payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Single quality snapshot for one analysis (and optional later LLM) run."""
    oracle = oracle_payload if isinstance(oracle_payload, dict) else evaluate_output_dir(output_dir).to_dict()
    llm = _llm_block(output_dir)
    trail = collect_confidence_trail(output_dir)
    return {
        "type": "quality_report",
        "verdict": oracle.get("verdict") or "pass",
        "skipped": bool(oracle.get("skipped")),
        "publishable": llm.get("publishable"),
        "layers": list(oracle.get("layers") or []),
        "reasons": list(oracle.get("reasons") or []),
        "counts": dict(oracle.get("counts") or {}),
        "confidence_trail": trail,
        "llm": llm,
        "sources": list(oracle.get("sources") or []),
    }


def format_quality_markdown(report: dict[str, Any]) -> str:
    verdict = str(report.get("verdict") or "pass").upper()
    lines = [f"# Качество: {verdict}", ""]
    pub = report.get("publishable")
    if pub is True:
        lines.append("Публикация ответа LLM: да.")
    elif pub is False:
        lines.append("Публикация ответа LLM: нет (quality gate).")
    else:
        lines.append("Публикация ответа LLM: нет ответа модели в этом прогоне.")
    lines.append("")

    lines.append("## Слои")
    lines.append("")
    layers = report.get("layers") or []
    if not layers:
        lines.append("- нет слоёв")
    for layer in layers:
        name = layer.get("name") or "?"
        status = layer.get("verdict") or "?"
        skipped = " (пропущен)" if layer.get("skipped") else ""
        lines.append(f"- `{name}`: {status}{skipped}")
    lines.append("")

    lines.append("## Почему")
    lines.append("")
    reasons = report.get("reasons") or []
    if report.get("skipped") and not reasons:
        lines.append("Таблица влияния не строилась — проверки направления пропущены.")
    elif not reasons:
        lines.append("Замечаний нет.")
    else:
        for reason in reasons:
            lines.append(f"- {reason}")
    lines.append("")

    lines.append("## Confidence")
    lines.append("")
    trail = report.get("confidence_trail") or []
    if not trail:
        lines.append("Нет данных о пересчёте confidence.")
    else:
        shown = 0
        for item in trail:
            if item.get("scope") != "run" and item.get("change") == "unchanged" and shown >= 12:
                continue
            arrow = f"{item.get('from')} → {item.get('to')}"
            change = item.get("change") or "unchanged"
            param = item.get("parameter") or "?"
            label = "прогон" if param == "*" else param
            lines.append(
                f"- `{label}` [{change}]: {arrow} ({item.get('evidence_type') or '—'}). "
                f"{item.get('reason') or ''}".rstrip()
            )
            shown += 1
            if shown >= 25:
                hidden = len(trail) - shown
                if hidden > 0:
                    lines.append(f"- … и ещё {hidden} строк")
                break
    lines.append("")

    llm = report.get("llm") or {}
    lines.append("## LLM")
    lines.append("")
    if not llm.get("present"):
        lines.append("Ответа модели нет — слой llm пропущен.")
    else:
        score = llm.get("score")
        lines.append(
            f"Задача `{llm.get('task') or '?'}`: score {score if score is not None else '—'}/100, "
            f"verdict {llm.get('verdict') or '—'}, "
            f"publishable={'yes' if llm.get('publishable') else 'no'}."
        )
        for reason in llm.get("reasons") or []:
            lines.append(f"- {reason}")
    lines.append("")
    return "\n".join(lines)


def evaluate_quality(
    output_dir: Path, *, oracle_payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One quality snapshot for an analysis directory (oracle layers + trail + llm)."""
    return build_quality_report(output_dir, oracle_payload=oracle_payload)


def persist_quality_snapshot(
    output_dir: Path,
    *,
    oracle_report: Any | None = None,
    oracle_payload: dict[str, Any] | None = None,
) -> Path:
    """Write oracle_report.* and quality_report.* from a single evaluation."""
    from pgprofile_oracle import OracleReport, oracle_report_from_dict

    if isinstance(oracle_report, OracleReport):
        active = oracle_report
        payload = active.to_dict()
    elif isinstance(oracle_payload, dict):
        payload = oracle_payload
        active = oracle_report_from_dict(payload)
    else:
        active = evaluate_output_dir(output_dir)
        payload = active.to_dict()
    write_oracle_artifacts(output_dir, active)
    write_quality_report(output_dir, oracle_payload=payload)
    return output_dir / "oracle_report.json"


def write_quality_report(
    output_dir: Path, *, oracle_payload: dict[str, Any] | None = None
) -> Path:
    """Persist quality_report.json and quality_report.md next to oracle artifacts."""
    report = build_quality_report(output_dir, oracle_payload=oracle_payload)
    json_path = output_dir / QUALITY_JSON
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / QUALITY_MD).write_text(format_quality_markdown(report), encoding="utf-8")
    return json_path
