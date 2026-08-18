"""Map UI requests to analyze_pgprofile.run_pipeline and collect artifacts."""

from __future__ import annotations

import contextlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from dataclasses import replace

import yaml

from analyze_pgprofile import (
    DEFAULT_CONFIG,
    DEFAULT_PLAYBOOK,
    DEFAULT_TUNING,
    run_pipeline,
)
from pgprofile_health import load_thresholds
from pgprofile_session import AnalysisSession
from ui.models import AnalyzeRequest, AnalyzeResult, ReportMeta

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

AI_USAGE = """# Как отдать этот архив в ИИ

Архив — это уже посчитанный анализ (Python), не плагин и не «функции» для модели.
Модель не ходит в базу и не читает исходный HTML pg_profile. Ей можно только
пересказывать и оформлять то, что уже лежит в файлах.

Есть два рабочих пути. Не смешивайте их в одном письме коллегам.

## Путь 1. Внутри приложения (Qwen) — с автопроверкой качества

В UI после анализа: блок «Headless Qwen» → «Запросить Qwen».
Промпт собирается сам из brief и таблицы влияния.

Качество ответа проверяется автоматически (структура JSON, ссылки только на
параметры/метрики из influence_table, запрет писать proven там, где probable).
Если проверка не прошла или выбран dry_run — статус «нельзя публиковать».
Это не сбой анализа: цифры в wiki уже готовы без модели.

## Путь 2. ZIP во внешнюю модель (gigacli, GigaChat, ChatGPT, Cursor)

Модель НЕ получает новых функций от архива. Вы просто прикладываете файлы
как контекст.

Как передать:

1. Скачайте ZIP в UI (вкладка Confluence → ZIP) или возьмите каталог analysis_out.
2. Распакуйте (или приложите zip целиком, если чат принимает вложения).
3. В чат напишите: «Следуй README_AI.txt и файлу промпта. Цифры не выдумывай».
4. Вложите ОДИН промпт (первый найденный из списка):
   - confluence_prompt.txt          — страница Confluence (Wiki Markup)
   - nt_runs_confluence.wiki + influence_summary*.wiki — уже готовые таблицы,
     модели нужен только narrative, не пересчёт
   - summary_prompt.txt            — текстовый отчёт
   - jvm_prompt.txt                — только JVM-режим
5. Ответ модели — черновик текста. Таблицы берите из *.wiki этого архива,
   не просите модель их пересчитать.

Копировать промпт без ZIP: вкладка UI «Промпт для ИИ» → «Скопировать промпт».

Не отправляйте в модель: исходные HTML pg_profile, knowledge/*.yaml, пароли,
полный settings dump. Рекомендации из knowledge уже внутри brief.

## Что в архиве уже проверено, а что — нет

Проверено Python (смотрите quality_report.md и oracle_report.md):

- таблица влияния GUC → метрики собрана по правилам;
- oracle: pass / warning / fail по знаку Δ и полноте полей;
- confidence: probable = гипотеза, proven = изолированный эффект.

НЕ проверяется автоматически, если вы отдали ZIP в gigacli/ChatGPT:

- не выдумала ли модель новые находки и GUC;
- не завысила ли она proven.

Такую проверку делает только путь 1 (Qwen в UI) или человек: сверка ответа
с brief.md, influence_table.json и вкладкой «Качество».

Готовую wiki из архива можно вставить в Confluence сразу, без ИИ:
Insert → Wiki Markup. ИИ нужен только для связного текста вокруг таблиц.

Слить ответ модели со stub:

  python merge_confluence.py confluence_stub.wiki -b body.wiki -o confluence_page.wiki
"""

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
        influence_series = data.get("influence_series")
        if isinstance(influence_series, dict):
            summary["influence"] = {
                "type": influence_series.get("type") or "influence_table_series",
                "mode": influence_series.get("mode") or "series",
                "row_count": influence_series.get("row_count", len(influence_series.get("rows") or [])),
                "rows": (influence_series.get("rows") or [])[:50],
                "functional_summary": influence_series.get("functional_summary") or {},
                "workload_match": influence_series.get("workload_match") or {},
                "confidence_meta": influence_series.get("confidence_meta") or {},
                "settings_table": influence_series.get("settings_table") or {},
                "metrics_table": influence_series.get("metrics_table") or {},
            }
    stable_path = output_dir / "stable_prod.json"
    if stable_path.is_file():
        data = json.loads(stable_path.read_text(encoding="utf-8"))
        sm = data.get("summary") or {}
        summary["common_findings"] = sm.get("stable_count", len(data.get("stable_findings") or []))
        summary["specific_findings"] = len(data.get("ephemeral_findings") or [])
        summary["report_count"] = len(data.get("reports") or [])

    run_cmp_path = output_dir / "run_comparison.json"
    if run_cmp_path.is_file():
        data = json.loads(run_cmp_path.read_text(encoding="utf-8"))
        summary["run_comparison"] = {
            "significant_count": (data.get("summary") or {}).get("significant_count", 0),
            "total_compared": (data.get("summary") or {}).get("total_compared", 0),
            "workload_match_score": ((data.get("workload_match") or {}).get("workload_match_score")),
            "workload_match_level": ((data.get("workload_match") or {}).get("level")),
            "functional_summary": data.get("functional_summary") or {},
        }

    influence_path = output_dir / "influence_table.json"
    if influence_path.is_file():
        data = json.loads(influence_path.read_text(encoding="utf-8"))
        rows = data.get("rows") or []
        pair_influence = {
            "type": data.get("type") or "influence_table",
            "mode": data.get("mode") or "pair",
            "row_count": len(rows),
            "rows": rows[:50],
            "functional_summary": data.get("functional_summary") or {},
            "workload_match": data.get("workload_match") or {},
            "confidence_meta": data.get("confidence_meta") or {},
        }
        existing = summary.get("influence")
        # A series NT run already filled influence from nt_runs.json. Do not let a
        # leftover pair table hide settings/metrics-by-run.
        if not (
            isinstance(existing, dict)
            and (
                existing.get("mode") == "series"
                or existing.get("type") == "influence_table_series"
            )
        ):
            summary["influence"] = pair_influence

    findings_ui = _build_findings_ui(output_dir)
    summary["findings_ui"] = findings_ui
    counts = {"critical": 0, "warning": 0, "info": 0}
    for card in findings_ui:
        counts[_severity_bucket(str(card.get("severity")))] += 1
    summary["severity_counts"] = counts
    if summary.get("total_findings") is None and findings_ui:
        summary["total_findings"] = len(findings_ui)
    oracle_path = output_dir / "oracle_report.json"
    if oracle_path.is_file():
        oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
        summary["oracle"] = {
            "verdict": oracle.get("verdict"),
            "skipped": bool(oracle.get("skipped")),
            "reasons": (oracle.get("reasons") or [])[:8],
            "counts": oracle.get("counts") or {},
            "layers": oracle.get("layers") or [],
            "confidence_adjustments": (oracle.get("confidence_adjustments") or [])[:8],
        }
    quality_path = output_dir / "quality_report.json"
    if quality_path.is_file():
        quality = json.loads(quality_path.read_text(encoding="utf-8"))
        summary["quality"] = {
            "verdict": quality.get("verdict"),
            "publishable": quality.get("publishable"),
            "layers": quality.get("layers") or [],
            "reasons": (quality.get("reasons") or [])[:8],
            "confidence_trail": (quality.get("confidence_trail") or [])[:20],
            "llm": quality.get("llm") or {},
        }
    _attach_compare_view(summary)
    return summary


def _influence_mode(influence: dict[str, Any]) -> str:
    if influence.get("mode") == "series" or influence.get("type") == "influence_table_series":
        return "series"
    labels = (influence.get("settings_table") or {}).get("run_labels") or []
    if influence.get("mode") != "pair" and len(labels) >= 2:
        return "series"
    return "pair"


def _workload_is_weak(workload: dict[str, Any]) -> bool:
    if str(workload.get("level") or "").lower() == "low":
        return True
    score = workload.get("workload_match_score")
    return isinstance(score, (int, float)) and score < 0.6


def _changed_params(influence: dict[str, Any]) -> list[str]:
    settings_rows = (influence.get("settings_table") or {}).get("rows") or []
    from_settings = [
        str(row.get("parameter") or "").strip()
        for row in settings_rows
        if isinstance(row, dict)
    ]
    from_settings = [name for name in from_settings if name]
    if from_settings:
        return from_settings
    seen: list[str] = []
    for row in influence.get("rows") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("parameter") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _confidence_hints(summary: dict[str, Any], influence: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(text: Any) -> None:
        token = str(text or "").strip()
        if not token or token in seen or len(out) >= 2:
            return
        seen.add(token)
        out.append(token)

    for item in (summary.get("quality") or {}).get("confidence_trail") or []:
        if isinstance(item, dict) and item.get("change") == "downgrade":
            add(item.get("reason"))
    for row in influence.get("rows") or []:
        if not isinstance(row, dict):
            continue
        for reason in row.get("confidence_reasons") or []:
            add(reason)
    add((influence.get("confidence_meta") or {}).get("notes"))
    return out


def _attach_compare_view(summary: dict[str, Any]) -> None:
    influence = summary.get("influence")
    if not isinstance(influence, dict):
        return
    summary["compare"] = {
        "mode": _influence_mode(influence),
        "workload_weak": _workload_is_weak(influence.get("workload_match") or {}),
        "changed_params": _changed_params(influence),
        "confidence_hints": _confidence_hints(summary, influence),
    }


def session_from_request(req: AnalyzeRequest, upload_paths: list[Path], output_dir: Path) -> AnalysisSession:
    """UI adapter: map AnalyzeRequest + saved files to an AnalysisSession."""
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

    ns = AnalysisSession(
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
        exit_code_quality=False,
    )

    scenario = req.scenario
    symptoms = [s.strip() for s in req.symptoms if s and s.strip()]

    if scenario == "nt_runs":
        if not symptoms:
            raise ValueError(
                "Для сценария «Несколько прогонов НТ» выберите хотя бы один симптом "
                "(например, high_cpu или high_wal)."
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
            return session_from_request(
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
            return session_from_request(
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


def _run_pipeline_captured(ns: AnalysisSession) -> tuple[int, str]:
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
        base_ns = session_from_request(base_req, upload_paths, output_dir)
    except ValueError as exc:
        return AnalyzeResult(2, str(exc), output_dir, None, None, None, {})

    if base_ns.stable_prod_reports:
        stable_ns = replace(
            base_ns, symptom=None, symptom_reports=None, symptom_label=[]
        )
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
            ns = session_from_request(one, upload_paths, sdir)
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
        ns = session_from_request(req, upload_paths, output_dir)
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
