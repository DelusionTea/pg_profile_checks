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
    scenario: str  # health | symptom | nt_runs | stable_prod | nt_prod
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
        if len(nt_items) < 2:
            raise ValueError("сценарий «Несколько прогонов НТ» требует ≥2 файлов с меткой НТ")
        if not symptoms:
            raise ValueError("выберите хотя бы одну проблему (симптом)")
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
            raise ValueError("выберите хотя бы одну проблему для расследования")
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

    if scenario == "stable_prod":
        if len(prod_items) < 2:
            raise ValueError("нужно ≥2 отчёта с меткой ПРОМ")
        ns.stable_prod_reports = [p for _, p in prod_items]
        ns.stable_prod_label = [
            m.label or suggest_label(m.filename, "PROD", i) for i, (m, _) in enumerate(prod_items)
        ]
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
        if len(paths) != 1:
            raise ValueError("health-check принимает ровно один отчёт")
        ns.report = paths[0]
        return ns

    raise ValueError(f"неизвестный сценарий: {scenario}")


def suggest_scenario(reports: list[ReportMeta], symptoms: list[str]) -> str:
    nt = sum(1 for r in reports if r.env.upper() == "NT")
    prod = sum(1 for r in reports if r.env.upper() == "PROD")
    if nt >= 2 and symptoms:
        return "nt_runs"
    if symptoms and reports:
        return "symptom"
    if prod >= 2 and not symptoms:
        return "stable_prod"
    if nt >= 1 and prod >= 1 and not symptoms:
        return "nt_prod"
    if len(reports) == 1:
        return "health"
    if nt >= 2:
        return "nt_runs"
    return "symptom" if symptoms else "health"


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
