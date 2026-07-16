"""Map UI requests to analyze_pgprofile.run_pipeline and collect artifacts."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from analyze_pgprofile import (
    DEFAULT_CONFIG,
    DEFAULT_PLAYBOOK,
    DEFAULT_TUNING,
    run_pipeline,
)
from pgprofile_health import load_thresholds

DEFAULT_THRESHOLD_GUIDANCE = (
    Path(__file__).resolve().parent.parent / "knowledge" / "threshold_guidance.yaml"
)

WIKI_PRIORITY = (
    "multi_symptom_confluence.wiki",
    "nt_runs_confluence.wiki",
    "symptom_confluence_stub.wiki",
    "stable_prod_confluence_stub.wiki",
    "nt_prod_confluence_stub.wiki",
    "confluence_stub.wiki",
)

PROMPT_PRIORITY = (
    "multi_symptom_confluence_prompt.txt",
    "symptom_confluence_prompt.txt",
    "stable_prod_confluence_prompt.txt",
    "nt_prod_confluence_prompt.txt",
    "confluence_prompt.txt",
    "summary_prompt.txt",
)

BRIEF_PRIORITY = (
    "multi_symptom_brief.md",
    "nt_runs_brief.md",
    "symptom_brief.md",
    "stable_prod_brief.md",
    "nt_prod_brief.md",
    "brief.md",
)

AI_USAGE = """# Промпт для ИИ (gigacli пока не интегрирован)

1. Откройте файл `*_confluence_prompt.txt` или `summary_prompt.txt` из этого архива.
2. Передайте содержимое в gigacli / другой LLM.
3. Сохраните ответ как Wiki Markup (тело страницы).
4. При необходимости слейте со stub:

   python merge_confluence.py <stub.wiki> -b <body.wiki> -o confluence_page.wiki

5. В Confluence Server/DC: Insert → Wiki Markup → вставьте содержимое.

Не отправляйте в LLM исходные HTML-отчёты pg_profile — только brief/prompt из этого архива.
"""


@dataclass
class ReportMeta:
    filename: str
    env: str  # NT | PROD
    label: str
    order: int = 0


@dataclass
class AnalyzeRequest:
    scenario: str  # health | full_multi | symptom | nt_runs | stable_prod | nt_prod
    reports: list[ReportMeta]
    symptoms: list[str] = field(default_factory=list)
    query_hex: str | None = None
    query_id: str | None = None
    query_text: str | None = None
    confluence_title: str | None = None


@dataclass
class AnalyzeResult:
    exit_code: int
    error: str | None
    output_dir: Path
    wiki_path: Path | None
    prompt_path: Path | None
    brief_path: Path | None
    summary: dict[str, Any]
    findings_ui: list[dict[str, Any]] = field(default_factory=list)


def _flatten_threshold_node(prefix: str, value: Any) -> list[dict[str, str]]:
    """Flatten nested threshold dict into rows {key, value, type}."""
    rows: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten_threshold_node(path, child))
        return rows
    if isinstance(value, list):
        rendered = ", ".join(str(v) for v in value)
        rows.append(
            {
                "key": prefix,
                "value": rendered,
                "type": "list",
            }
        )
        return rows
    if isinstance(value, bool):
        type_name = "bool"
    elif isinstance(value, int) and not isinstance(value, bool):
        type_name = "int"
    elif isinstance(value, float):
        type_name = "float"
    else:
        type_name = "str"
    rows.append({"key": prefix, "value": str(value), "type": type_name})
    return rows


def load_threshold_guidance(path: Path | None = None) -> dict[str, dict[str, str]]:
    cfg_path = path or DEFAULT_THRESHOLD_GUIDANCE
    if not cfg_path.is_file():
        return {}
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    guidelines = raw.get("guidelines") or {}
    result: dict[str, dict[str, str]] = {}
    for key, body in guidelines.items():
        if not isinstance(body, dict):
            continue
        result[str(key)] = {
            "when": str(body.get("when") or "").strip(),
            "databases": str(body.get("databases") or "").strip(),
            "ref": str(body.get("ref") or "").strip(),
        }
    return result


def list_thresholds(
    config_path: Path | None = None,
    guidance_path: Path | None = None,
) -> dict[str, Any]:
    """Structured thresholds for UI: sections with flat parameter tables + hints."""
    path = config_path or DEFAULT_CONFIG
    data = load_thresholds(path)
    guidance = load_threshold_guidance(guidance_path)
    sections: list[dict[str, Any]] = []
    for section_name in sorted(data.keys()):
        body = data[section_name]
        rows = _flatten_threshold_node("", body)
        enriched: list[dict[str, Any]] = []
        for row in rows:
            if not row.get("key"):
                continue
            key = row["key"]
            hint = guidance.get(key) or {}
            enriched.append(
                {
                    **row,
                    "hint_when": hint.get("when") or "",
                    "hint_databases": hint.get("databases") or "",
                    "hint_ref": hint.get("ref") or "",
                    "has_hint": bool(hint.get("when") or hint.get("databases")),
                }
            )
        sections.append(
            {
                "id": section_name,
                "title": section_name,
                "rows": enriched,
            }
        )
    return {
        "source": str(path.resolve()),
        "filename": path.name,
        "guidance_source": str((guidance_path or DEFAULT_THRESHOLD_GUIDANCE).resolve()),
        "sections": sections,
    }


def list_symptoms(playbook_path: Path | None = None) -> list[dict[str, str]]:
    path = playbook_path or DEFAULT_PLAYBOOK
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    symptoms = data.get("symptoms") or {}
    result = []
    for sid, body in symptoms.items():
        if not isinstance(body, dict):
            continue
        result.append(
            {
                "id": sid,
                "title": str(body.get("title") or sid),
                "description": str(body.get("description") or "").strip(),
            }
        )
    return result


def _safe_label(name: str, fallback: str) -> str:
    stem = Path(name).stem
    cleaned = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")
    return cleaned or fallback


def suggest_label(filename: str, env: str, index: int) -> str:
    lower = filename.lower()
    if "prom" in lower or "prod" in lower:
        m = re.search(r"prom(\d+)", lower)
        if m:
            return f"prom{m.group(1)}"
        return f"prod_{index + 1}"
    if "before" in lower:
        return "before_settings"
    if "with_settings" in lower or "after" in lower:
        return "after_settings"
    if "old" in lower:
        return "old_app"
    prefix = "nt" if env.upper() == "NT" else "prod"
    return _safe_label(filename, f"{prefix}_{index + 1}")


def _pick_first(output_dir: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = output_dir / name
        if path.is_file() and path.stat().st_size > 0:
            return path
    # fallback: any matching pattern
    for name in names:
        matches = sorted(output_dir.glob(name.replace("*", "*")))
        for path in matches:
            if path.is_file() and path.stat().st_size > 0:
                return path
    # last resort: first *.wiki / *.txt / *.md by family
    if names is WIKI_PRIORITY:
        found = sorted(output_dir.glob("*confluence*.wiki"))
        return found[0] if found else None
    if names is PROMPT_PRIORITY:
        found = sorted(output_dir.glob("*prompt*.txt"))
        return found[0] if found else None
    if names is BRIEF_PRIORITY:
        found = sorted(output_dir.glob("*brief*.md"))
        if found:
            return found[0]
        brief = output_dir / "brief.md"
        return brief if brief.is_file() else None
    return None


def _severity_bucket(sev: str) -> str:
    s = (sev or "warning").lower()
    if s in ("critical", "high"):
        return "critical"
    if s in ("warning", "medium"):
        return "warning"
    return "info"


def _build_findings_ui(output_dir: Path) -> list[dict[str, Any]]:
    """Flatten findings for UI cards (severity, id, message, advice, threshold)."""
    cards: list[dict[str, Any]] = []

    def add(
        fid: str,
        severity: str,
        message: str,
        *,
        title: str = "",
        advice: str = "",
        threshold: str = "",
    ) -> None:
        cards.append(
            {
                "id": fid,
                "severity": severity,
                "message": message,
                "title": title or fid,
                "advice": advice,
                "threshold": threshold,
            }
        )

    advisor = output_dir / "advisor.json"
    if advisor.is_file():
        data = json.loads(advisor.read_text(encoding="utf-8"))
        reports = data if isinstance(data, list) else data.get("reports") or [data]
        for report in reports:
            for item in report.get("advised_findings") or []:
                f = item.get("finding") or {}
                advice = item.get("advice") or {}
                actions = advice.get("actions") or []
                add(
                    str(f.get("id") or "?"),
                    str(f.get("severity") or "warning"),
                    str(f.get("message") or ""),
                    title=str(advice.get("title") or f.get("id") or ""),
                    advice=str(actions[0]) if actions else str(advice.get("recommendation") or "")[:180],
                )

    stable_path = output_dir / "stable_prod.json"
    if stable_path.is_file() and not cards:
        data = json.loads(stable_path.read_text(encoding="utf-8"))
        for sf in data.get("stable_findings") or []:
            msgs = sf.get("sample_messages") or []
            add(
                str(sf.get("rule_id") or "?"),
                str(sf.get("max_severity") or "warning"),
                str(msgs[0] if msgs else sf.get("rule_id") or ""),
                title=str(sf.get("rule_id") or ""),
            )

    symptom_path = output_dir / "symptom_investigation.json"
    if symptom_path.is_file() and not cards:
        data = json.loads(symptom_path.read_text(encoding="utf-8"))
        for c in data.get("causes") or []:
            status = str(c.get("status") or "possible")
            sev = (
                "critical"
                if status == "confirmed"
                else "warning"
                if status == "suspected"
                else "info"
            )
            add(
                str(c.get("cause_id") or "?"),
                sev,
                str(c.get("title") or ""),
                title=str(c.get("title") or ""),
                advice=(c.get("confirm_actions") or [""])[0],
            )

    # severity sort
    rank = {"critical": 0, "high": 1, "warning": 2, "medium": 3, "info": 4, "low": 5}
    cards.sort(key=lambda c: (rank.get(str(c["severity"]).lower(), 9), c["id"]))
    return cards[:80]


def _build_summary(output_dir: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"files": sorted(p.name for p in output_dir.iterdir() if p.is_file())}
    findings_path = output_dir / "findings.json"
    if findings_path.is_file():
        data = json.loads(findings_path.read_text(encoding="utf-8"))
        summary["total_findings"] = data.get("summary", {}).get("total_findings", 0)
        summary["analysis_count"] = data.get("summary", {}).get("analysis_count", 0)
    symptom_path = output_dir / "symptom_investigation.json"
    if symptom_path.is_file():
        data = json.loads(symptom_path.read_text(encoding="utf-8"))
        summary["symptom"] = data.get("summary", {})
    nt_path = output_dir / "nt_runs.json"
    if nt_path.is_file():
        data = json.loads(nt_path.read_text(encoding="utf-8"))
        summary["nt_runs_symptoms"] = data.get("symptoms", [])
    stable_path = output_dir / "stable_prod.json"
    if stable_path.is_file():
        data = json.loads(stable_path.read_text(encoding="utf-8"))
        sm = data.get("summary") or {}
        summary["common_findings"] = sm.get("stable_count", len(data.get("stable_findings") or []))
        summary["specific_findings"] = len(data.get("ephemeral_findings") or [])
        summary["report_count"] = len(data.get("reports") or [])

    findings_ui = _build_findings_ui(output_dir)
    summary["findings_ui"] = findings_ui
    counts = {"critical": 0, "warning": 0, "info": 0}
    for card in findings_ui:
        counts[_severity_bucket(str(card.get("severity")))] += 1
    summary["severity_counts"] = counts
    if summary.get("total_findings") is None and findings_ui:
        summary["total_findings"] = len(findings_ui)
    return summary


def build_namespace(req: AnalyzeRequest, upload_paths: list[Path], output_dir: Path) -> argparse.Namespace:
    """Build Namespace equivalent to CLI flags from UI request + saved file paths."""
    if len(upload_paths) != len(req.reports):
        raise ValueError("upload paths count must match reports metadata")

    ordered = sorted(
        zip(req.reports, upload_paths),
        key=lambda pair: (pair[0].order, pair[0].filename),
    )
    reports = [m for m, _ in ordered]
    paths = [p for _, p in ordered]

    nt_items = [(m, p) for m, p in zip(reports, paths) if m.env.upper() == "NT"]
    prod_items = [(m, p) for m, p in zip(reports, paths) if m.env.upper() == "PROD"]

    ns = argparse.Namespace(
        report=None,
        config=DEFAULT_CONFIG,
        compare_run=None,
        run_a_id="run_a",
        run_b_id="run_b",
        compare_settings=None,
        compare_prod=None,
        stable_prod_reports=None,
        stable_prod_label=[],
        min_stability=1.0,
        tuning=DEFAULT_TUNING,
        symptom=None,
        symptom_reports=None,
        symptom_label=[],
        query_hex=req.query_hex,
        query_id=req.query_id,
        query_text=req.query_text,
        playbook=DEFAULT_PLAYBOOK,
        nt_reports=None,
        nt_label=[],
        prod_reports=None,
        prod_label=[],
        symptoms=None,
        settings_a_id="NT",
        settings_b_id="PROD",
        output_dir=output_dir,
        confluence_title=req.confluence_title,
        min_change_pct=5.0,
        top_n=15,
        exit_code=False,
    )

    scenario = req.scenario
    symptoms = [s.strip() for s in req.symptoms if s and s.strip()]

    if scenario == "nt_runs":
        if not symptoms:
            # No symptom selected → full health across reports (common + per-file).
            return build_namespace(
                AnalyzeRequest(
                    scenario="full_multi" if len(paths) >= 2 else "health",
                    reports=req.reports,
                    symptoms=[],
                    confluence_title=req.confluence_title,
                ),
                upload_paths,
                output_dir,
            )
        if len(nt_items) < 2:
            raise ValueError("сценарий «Несколько прогонов НТ» требует ≥2 файлов с меткой НТ")
        ns.nt_reports = [p for _, p in nt_items]
        ns.nt_label = [m.label or suggest_label(m.filename, "NT", i) for i, (m, _) in enumerate(nt_items)]
        ns.symptoms = ",".join(symptoms)
        if prod_items:
            ns.prod_reports = [p for _, p in prod_items]
            ns.prod_label = [
                m.label or suggest_label(m.filename, "PROD", i) for i, (m, _) in enumerate(prod_items)
            ]
        return ns

    if scenario == "symptom":
        if not symptoms:
            return build_namespace(
                AnalyzeRequest(
                    scenario="full_multi" if len(paths) >= 2 else "health",
                    reports=req.reports,
                    symptoms=[],
                    confluence_title=req.confluence_title,
                ),
                upload_paths,
                output_dir,
            )
        if not paths:
            raise ValueError("добавьте хотя бы один отчёт")
        # Single symptom → one pipeline call. Multiple → handled in run_analysis.
        ns.symptom = symptoms[0]
        ns.symptom_reports = paths
        ns.symptom_label = [
            m.label or suggest_label(m.filename, m.env, i) for i, m in enumerate(reports)
        ]
        if len(prod_items) >= 2:
            ns.stable_prod_reports = [p for _, p in prod_items]
            ns.stable_prod_label = [
                m.label or suggest_label(m.filename, "PROD", i) for i, (m, _) in enumerate(prod_items)
            ]
        return ns

    if scenario == "full_multi":
        if len(paths) < 2:
            raise ValueError("полный анализ нескольких отчётов требует ≥2 файлов")
        # All reports (НТ и ПРОМ): health on each → общие + специфичные findings.
        ns.stable_prod_reports = paths
        ns.stable_prod_label = [
            m.label or suggest_label(m.filename, m.env, i) for i, m in enumerate(reports)
        ]
        ns.min_stability = 1.0
        return ns

    if scenario == "stable_prod":
        # Prefer PROD-tagged; if fewer than 2 PROD, use all uploaded reports.
        items = prod_items if len(prod_items) >= 2 else list(zip(reports, paths))
        if len(items) < 2:
            raise ValueError("нужно ≥2 отчёта")
        ns.stable_prod_reports = [p for _, p in items]
        ns.stable_prod_label = [
            m.label or suggest_label(m.filename, m.env, i) for i, (m, _) in enumerate(items)
        ]
        ns.min_stability = 1.0
        return ns

    if scenario == "nt_prod":
        if len(nt_items) < 1 or len(prod_items) < 1:
            raise ValueError("нужен хотя бы один НТ и один ПРОМ")
        ns.report = nt_items[0][1]
        ns.compare_prod = prod_items[0][1]
        ns.settings_a_id = nt_items[0][0].label or "NT"
        ns.settings_b_id = prod_items[0][0].label or "PROD"
        return ns

    if scenario == "health":
        if not paths:
            raise ValueError("добавьте хотя бы один отчёт")
        if len(paths) > 1:
            # Multiple files without a multi scenario → full cross-report analysis.
            return build_namespace(
                AnalyzeRequest(
                    scenario="full_multi",
                    reports=req.reports,
                    symptoms=[],
                    confluence_title=req.confluence_title,
                ),
                upload_paths,
                output_dir,
            )
        ns.report = paths[0]
        return ns

    if scenario == "compare_runs":
        if len(paths) < 2:
            raise ValueError("сравнение требует ровно ≥2 отчёта (берутся первые два по порядку)")
        ns.report = paths[0]
        ns.compare_run = paths[1]
        ns.compare_settings = paths[1]
        ns.run_a_id = reports[0].label or suggest_label(reports[0].filename, reports[0].env, 0)
        ns.run_b_id = reports[1].label or suggest_label(reports[1].filename, reports[1].env, 1)
        ns.settings_a_id = ns.run_a_id
        ns.settings_b_id = ns.run_b_id
        return ns

    raise ValueError(f"неизвестный сценарий: {scenario}")


def suggest_scenario(reports: list[ReportMeta], symptoms: list[str]) -> str:
    nt = sum(1 for r in reports if r.env.upper() == "NT")
    prod = sum(1 for r in reports if r.env.upper() == "PROD")
    if symptoms:
        if nt >= 2:
            return "nt_runs"
        return "symptom"
    # No specific problem selected → analyze everything in the report(s).
    if len(reports) >= 2:
        return "full_multi"
    if len(reports) == 1:
        return "health"
    if nt >= 1 and prod >= 1:
        return "nt_prod"
    return "health"


def _run_pipeline_captured(ns: argparse.Namespace) -> tuple[int, str]:
    stderr_buf = io.StringIO()
    with contextlib.redirect_stderr(stderr_buf):
        code = run_pipeline(ns)
    err_text = stderr_buf.getvalue().strip()
    if err_text.startswith("error: "):
        err_text = err_text[len("error: ") :]
    return code, err_text


def _combine_multi_symptom_outputs(
    output_dir: Path,
    symptom_dirs: list[tuple[str, Path]],
    title: str | None,
) -> None:
    """Merge per-symptom wiki/prompt/brief into root multi_symptom_* files."""
    wiki_parts: list[str] = []
    prompt_parts: list[str] = []
    brief_parts: list[str] = []
    heading = title or "Расследование нескольких проблем"
    wiki_parts.append(f"h1. {heading}\n")
    wiki_parts.append(
        "{info}Объединённый отчёт по симптомам: "
        + ", ".join(s for s, _ in symptom_dirs)
        + "{info}\n"
    )

    confirmed = 0
    suspected = 0
    for sid, sdir in symptom_dirs:
        wiki_parts.append(f"\nh1. Симптом: {sid}\n")
        stub = sdir / "symptom_confluence_stub.wiki"
        if stub.is_file():
            wiki_parts.append(stub.read_text(encoding="utf-8").strip())
            wiki_parts.append("")
        prompt = sdir / "symptom_confluence_prompt.txt"
        if prompt.is_file():
            prompt_parts.append(f"===== СИМПТОМ: {sid} =====\n")
            prompt_parts.append(prompt.read_text(encoding="utf-8").strip())
            prompt_parts.append("")
        brief = sdir / "symptom_brief.md"
        if brief.is_file():
            brief_parts.append(f"# Симптом: {sid}\n")
            brief_parts.append(brief.read_text(encoding="utf-8").strip())
            brief_parts.append("\n---\n")
        inv = sdir / "symptom_investigation.json"
        if inv.is_file():
            data = json.loads(inv.read_text(encoding="utf-8"))
            summary = data.get("summary") or {}
            confirmed += int(summary.get("confirmed_count") or 0)
            suspected += int(summary.get("suspected_count") or 0)

    (output_dir / "multi_symptom_confluence.wiki").write_text(
        "\n".join(wiki_parts).rstrip() + "\n", encoding="utf-8"
    )
    if prompt_parts:
        (output_dir / "multi_symptom_confluence_prompt.txt").write_text(
            "\n".join(prompt_parts).rstrip() + "\n", encoding="utf-8"
        )
    if brief_parts:
        (output_dir / "multi_symptom_brief.md").write_text(
            "\n".join(brief_parts).rstrip() + "\n", encoding="utf-8"
        )
    (output_dir / "multi_symptom_summary.json").write_text(
        json.dumps(
            {
                "symptoms": [s for s, _ in symptom_dirs],
                "confirmed_count": confirmed,
                "suspected_count": suspected,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _run_multi_symptom(
    req: AnalyzeRequest,
    upload_paths: list[Path],
    output_dir: Path,
    symptoms: list[str],
) -> AnalyzeResult:
    """Run investigate_symptom once per selected problem and merge Confluence text."""
    symptom_dirs: list[tuple[str, Path]] = []
    last_code = 0

    # Optional stable PROD once (same as single-symptom path when ≥2 PROD).
    base_req = AnalyzeRequest(
        scenario="symptom",
        reports=req.reports,
        symptoms=[symptoms[0]],
        query_hex=req.query_hex,
        query_id=req.query_id,
        query_text=req.query_text,
        confluence_title=req.confluence_title,
    )
    try:
        base_ns = build_namespace(base_req, upload_paths, output_dir)
    except ValueError as exc:
        return AnalyzeResult(2, str(exc), output_dir, None, None, None, {})

    if base_ns.stable_prod_reports:
        stable_ns = argparse.Namespace(**vars(base_ns))
        stable_ns.symptom = None
        stable_ns.symptom_reports = None
        stable_ns.symptom_label = []
        code, err = _run_pipeline_captured(stable_ns)
        if code == 2:
            return AnalyzeResult(2, err or "ошибка stable PROD", output_dir, None, None, None, {})

    for sid in symptoms:
        sdir = output_dir / "by_symptom" / sid
        sdir.mkdir(parents=True, exist_ok=True)
        one = AnalyzeRequest(
            scenario="symptom",
            reports=req.reports,
            symptoms=[sid],
            query_hex=req.query_hex if sid == "slow_query" else None,
            query_id=req.query_id if sid == "slow_query" else None,
            query_text=req.query_text if sid == "slow_query" else None,
            confluence_title=req.confluence_title,
        )
        try:
            ns = build_namespace(one, upload_paths, sdir)
        except ValueError as exc:
            return AnalyzeResult(2, str(exc), output_dir, None, None, None, {})
        # Avoid repeating stable_prod inside each per-symptom run.
        ns.stable_prod_reports = None
        ns.stable_prod_label = []
        code, err = _run_pipeline_captured(ns)
        if code == 2:
            return AnalyzeResult(
                2,
                f"{sid}: {err or 'ошибка анализа'}",
                output_dir,
                None,
                None,
                None,
                {},
            )
        last_code = code
        symptom_dirs.append((sid, sdir))

    _combine_multi_symptom_outputs(output_dir, symptom_dirs, req.confluence_title)

    # Aggregate summary for UI pills.
    summary = _build_summary(output_dir)
    multi_summary_path = output_dir / "multi_symptom_summary.json"
    if multi_summary_path.is_file():
        multi = json.loads(multi_summary_path.read_text(encoding="utf-8"))
        summary["symptom"] = {
            "confirmed_count": multi.get("confirmed_count", 0),
            "suspected_count": multi.get("suspected_count", 0),
            "report_count": len(req.reports),
        }
        summary["symptoms"] = multi.get("symptoms", [])

    wiki = _pick_first(output_dir, WIKI_PRIORITY)
    prompt = _pick_first(output_dir, PROMPT_PRIORITY)
    brief = _pick_first(output_dir, BRIEF_PRIORITY)
    return AnalyzeResult(
        exit_code=last_code,
        error=None,
        output_dir=output_dir,
        wiki_path=wiki,
        prompt_path=prompt,
        brief_path=brief,
        summary=summary,
    )


def run_analysis(req: AnalyzeRequest, upload_paths: list[Path], output_dir: Path) -> AnalyzeResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    symptoms = [s.strip() for s in req.symptoms if s and s.strip()]
    scenario = req.scenario
    if scenario == "auto":
        scenario = suggest_scenario(req.reports, symptoms)
        req = AnalyzeRequest(
            scenario=scenario,
            reports=req.reports,
            symptoms=req.symptoms,
            query_hex=req.query_hex,
            query_id=req.query_id,
            query_text=req.query_text,
            confluence_title=req.confluence_title,
        )

    if scenario == "symptom" and len(symptoms) > 1:
        return _run_multi_symptom(req, upload_paths, output_dir, symptoms)

    try:
        ns = build_namespace(req, upload_paths, output_dir)
    except ValueError as exc:
        return AnalyzeResult(
            exit_code=2,
            error=str(exc),
            output_dir=output_dir,
            wiki_path=None,
            prompt_path=None,
            brief_path=None,
            summary={},
        )

    code, err_text = _run_pipeline_captured(ns)
    if code == 2:
        return AnalyzeResult(
            exit_code=code,
            error=err_text or "анализ завершился с ошибкой",
            output_dir=output_dir,
            wiki_path=None,
            prompt_path=None,
            brief_path=None,
            summary={},
        )

    wiki = _pick_first(output_dir, WIKI_PRIORITY)
    prompt = _pick_first(output_dir, PROMPT_PRIORITY)
    brief = _pick_first(output_dir, BRIEF_PRIORITY)
    return AnalyzeResult(
        exit_code=code,
        error=None,
        output_dir=output_dir,
        wiki_path=wiki,
        prompt_path=prompt,
        brief_path=brief,
        summary=_build_summary(output_dir),
    )


def build_zip(output_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README_AI.txt", AI_USAGE)
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=str(path.relative_to(output_dir)))
    return buf.getvalue()
