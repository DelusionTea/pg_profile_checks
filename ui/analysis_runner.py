"""Map UI requests to analyze_pgprofile.run_pipeline and collect artifacts."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

1. Откройте файл `*_confluence_prompt.txt`, `summary_prompt.txt` или `jvm_prompt.txt` из этого архива.
2. Передайте содержимое в gigacli / другой LLM.
3. Сохраните ответ как Wiki Markup (тело страницы).
4. При необходимости слейте со stub:

   python merge_confluence.py <stub.wiki> -b <body.wiki> -o confluence_page.wiki

5. В Confluence Server/DC: Insert → Wiki Markup → вставьте содержимое.

Не отправляйте в LLM исходные HTML-отчёты pg_profile или сырые конфиги АС — только brief/prompt из этого архива.
"""

def _detect_jvmcheck_root() -> Path:
    env = os.environ.get("JVMCHECK_ROOT")
    candidates = []
    if env:
        candidates.append(Path(env))
    # Preferred self-contained runtime bundled with this repository.
    candidates.append(Path(__file__).resolve().parent.parent / "jvmcheck_runtime")
    candidates.extend(
        [
            Path.home() / "jvmcheck",
            Path(__file__).resolve().parents[3] / "jvmcheck",
            Path(__file__).resolve().parents[2] / "jvmcheck",
        ]
    )
    for candidate in candidates:
        if (candidate / "src" / "jvmcheck").is_dir() and (candidate / "resources").is_dir():
            return candidate
    return Path.home() / "jvmcheck"


DEFAULT_JVMCHECK_ROOT = _detect_jvmcheck_root()
DEMO_JVM_ROOT = Path(__file__).resolve().parent.parent / "resources" / "jvm_demo"

JVM_PROBLEM_CATALOG: list[dict[str, str]] = [
    {
        "id": "gc_latency",
        "title": "Долгие GC паузы",
        "description": "Проблемы p95/p99 и GC time ratio.",
    },
    {
        "id": "heap_pressure",
        "title": "Heap / OldGen pressure",
        "description": "Высокая утилизация OldGen, риск деградации или OOM.",
    },
    {
        "id": "memory_pressure",
        "title": "Контейнерная память под давлением",
        "description": "Working set близко к limit/request, риск рестартов.",
    },
    {
        "id": "jvm_flags",
        "title": "Некорректные JVM флаги",
        "description": "Конфликты/дубликаты/критичные отсутствующие флаги.",
    },
    {
        "id": "container_config",
        "title": "Проблемы конфигурации контейнера",
        "description": "Плохая связка requests/limits и runtime policy.",
    },
]

JVM_PROBLEM_RULE_PREFIXES: dict[str, tuple[str, ...]] = {
    "gc_latency": ("gc.",),
    "heap_pressure": ("heap.", "oldgen.", "newgen."),
    "memory_pressure": ("memory.",),
    "jvm_flags": ("jvm.",),
    "container_config": ("container.",),
}

JVM_PROBLEM_REQUIRED_INPUTS: dict[str, tuple[str, ...]] = {
    "gc_latency": ("gc_pause_p95_ms",),
    "memory_pressure": ("container_memory_usage_percent",),
    "heap_pressure": ("heap_used_mib", "heap_used_percent", "old_gen_used_percent"),
}

JVM_ALWAYS_REQUIRED_INPUTS: tuple[str, ...] = (
    "gc_pause_p95_ms",
    "heap_used_mib",
    "container_memory_usage_percent",
)

JVM_PROBLEM_SEED_FINDINGS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "gc_latency": (
        ("gc.long_pause_p95", "warning", "Выбрана проблема долгих GC пауз, требуется tuning tail latency."),
    ),
    "heap_pressure": (
        ("heap.old_gen_pressure", "warning", "Выбрано давление по heap/old gen, требуется стабилизация occupancy."),
    ),
    "memory_pressure": (
        ("memory.limit_pressure", "critical", "Выбрано memory pressure в контейнере, требуется снижение риска OOM."),
    ),
    "jvm_flags": (
        ("jvm.flag_conflict_maxram", "warning", "Выбрана проблема конфигурации JVM флагов, требуется нормализация options."),
    ),
    "container_config": (
        ("container.request_limit_skew", "warning", "Выбрана проблема container resources, требуется корректировка request/limit."),
    ),
}

JVM_PROBLEM_REQUIRED_METRICS: dict[str, tuple[tuple[str, str], ...]] = {
    "gc_latency": (
        ("gc_pause_p95_ms", "GC pause p95"),
        ("gc_pause_p99_ms", "GC pause p99"),
        ("gc_time_ratio_percent", "GC time ratio"),
    ),
    "heap_pressure": (
        ("heap_used_mib", "Heap used"),
        ("heap_committed_mib", "Heap committed"),
        ("old_gen_used_mib", "OldGen used"),
        ("old_gen_capacity_mib", "OldGen capacity"),
    ),
    "memory_pressure": (
        ("container_memory_usage_percent", "Container memory usage (%)"),
    ),
}

JVM_PROBLEM_STRATEGIES: dict[str, dict[str, str]] = {
    "gc_latency": {
        "safe": "Зафиксировать G1 и умеренно снизить MaxGCPauseMillis без изменения общего heap budget.",
        "balanced": "Снизить pause target и скорректировать InitiatingHeapOccupancyPercent c проверкой p95/p99.",
        "aggressive": "Агрессивно оптимизировать GC-параметры только после стабилизации heap pressure.",
    },
    "heap_pressure": {
        "safe": "Сначала проверить retention/old-gen тренд и не уменьшать MaxRAMPercentage при высоком heap usage.",
        "balanced": "Тонко скорректировать G1ReservePercent + InitiatingHeapOccupancyPercent, удерживая headroom.",
        "aggressive": "Перестраивать memory budget контейнера и JVM вместе с планом отката.",
    },
    "memory_pressure": {
        "safe": "Снизить JVM memory footprint и проверить лимиты/requests без резких изменений.",
        "balanced": "Комбинировать JVM tuning с quota-aware перераспределением container memory.",
        "aggressive": "Эскалировать на платформу для увеличения budget при стабильно высоком working set.",
    },
}

DEMO_JVM_SYSTEMS: dict[str, dict[str, Any]] = {
    "DEMO_CounterAgent": {
        "containers": ["application", "istioProxy", "vaultAgent"],
        "resources_file": "resources.yaml",
        "jvm_file": "jvm-config.txt",
    },
    "DEMO_CreditHistory": {
        "containers": ["application", "agent", "fluentbit"],
        "resources_file": "resources.yaml",
        "jvm_file": "jvm-config.txt",
    },
}


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


@dataclass
class JvmAnalyzeRequest:
    system_name: str
    pod_name: str | None = None
    container_name: str | None = None
    selected_problems: list[str] = field(default_factory=list)
    threshold_profile: str = "normal"
    jdk_version: int | None = None
    spring_boot_version: str | None = None
    confluence_title: str | None = None
    heap_used_mib: int | None = None
    heap_committed_mib: int | None = None
    old_gen_used_mib: int | None = None
    old_gen_capacity_mib: int | None = None
    gc_pause_p95_ms: float | None = None
    gc_pause_p99_ms: float | None = None
    gc_time_ratio_percent: float | None = None
    container_memory_usage_percent: float | None = None
    heap_used_percent: float | None = None
    old_gen_used_percent: float | None = None
    new_gen_used_mib: int | None = None
    new_gen_capacity_mib: int | None = None
    new_gen_used_percent: float | None = None
    container_memory_working_set_mib: int | None = None


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


def list_jvm_systems(root: Path | None = None) -> list[str]:
    jroot = root or DEFAULT_JVMCHECK_ROOT
    resources = jroot / "resources"
    real: list[str] = []
    if resources.is_dir():
        dirs = sorted(p.name for p in resources.iterdir() if p.is_dir() and not p.name.startswith("."))
        if dirs:
            real = dirs
        elif any(p.is_file() for p in resources.iterdir()):
            real = ["__root__"]
    demo = sorted(DEMO_JVM_SYSTEMS.keys())
    return sorted(set(real + demo))


def list_jvm_problems() -> list[dict[str, str]]:
    return JVM_PROBLEM_CATALOG


def list_jvm_containers(system_name: str, root: Path | None = None) -> list[dict[str, str]]:
    if not system_name:
        return []
    if system_name in DEMO_JVM_SYSTEMS:
        return [
            {
                "pod_name": "",
                "container_name": name,
                "display_name": name,
            }
            for name in DEMO_JVM_SYSTEMS[system_name]["containers"]
        ]

    jroot = root or DEFAULT_JVMCHECK_ROOT
    resources_root = jroot / "resources"
    try:
        if system_name == "__root__":
            resources_file, _ = _resolve_root_jvm_input_files(resources_root)
        else:
            system_dir = resources_root / system_name
            resources_file = system_dir / "resources.yaml"
            if not resources_file.is_file():
                resources_file = system_dir / "resources.yml"
            if not resources_file.is_file():
                resources_file, _ = _resolve_root_jvm_input_files(system_dir)
        import sys

        src_dir = jroot / "src"
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
        from jvmcheck.parsers.k8s_yaml_parser import parse_k8s_or_stand_yaml

        budget = parse_k8s_or_stand_yaml(
            resources_file.read_text(encoding="utf-8"),
            source_path=resources_file,
        )
        targets: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for container in budget.containers or []:
            name = str(container.name or "").strip()
            if not name:
                continue
            pod = str(container.pod_name or "").strip()
            key = (pod, name)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "pod_name": pod,
                    "container_name": name,
                    "display_name": f"{pod} / {name}" if pod else name,
                }
            )
        targets.sort(key=lambda item: (item["display_name"].lower(), item["container_name"].lower()))
        return targets
    except Exception:
        return []


def load_jvm_last_input(
    system_name: str,
    container_name: str,
    pod_name: str | None = None,
    *,
    root: Path | None = None,
) -> dict[str, Any] | None:
    if not system_name or not container_name:
        return None
    jroot = root or DEFAULT_JVMCHECK_ROOT
    resources_root = jroot / "resources"
    system_dir = _resolve_jvm_system_dir(system_name, resources_root)
    path = system_dir / "last_input.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    containers = payload.get("containers") if isinstance(payload, dict) else None
    if not isinstance(containers, dict):
        return None
    composite_key = _last_input_key(container_name, pod_name)
    entry = containers.get(composite_key) or containers.get(container_name)
    return entry if isinstance(entry, dict) else None


def save_jvm_last_input(
    req: JvmAnalyzeRequest,
    *,
    root: Path | None = None,
) -> None:
    if not req.system_name or not req.container_name:
        return
    jroot = root or DEFAULT_JVMCHECK_ROOT
    resources_root = jroot / "resources"
    system_dir = _resolve_jvm_system_dir(req.system_name, resources_root)
    system_dir.mkdir(parents=True, exist_ok=True)
    path = system_dir / "last_input.json"
    payload: dict[str, Any] = {"containers": {}}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("containers"), dict):
                payload = existing
        except Exception:
            payload = {"containers": {}}
    payload.setdefault("containers", {})
    payload["containers"][_last_input_key(req.container_name, req.pod_name)] = {
        "gc_pause_p95_ms": req.gc_pause_p95_ms,
        "gc_pause_p99_ms": req.gc_pause_p99_ms,
        "gc_time_ratio_percent": req.gc_time_ratio_percent,
        "container_memory_usage_percent": req.container_memory_usage_percent,
        "heap_used_mib": req.heap_used_mib,
        "heap_used_percent": req.heap_used_percent,
        "old_gen_used_mib": req.old_gen_used_mib,
        "old_gen_capacity_mib": req.old_gen_capacity_mib,
        "old_gen_used_percent": req.old_gen_used_percent,
        "new_gen_used_mib": req.new_gen_used_mib,
        "new_gen_capacity_mib": req.new_gen_capacity_mib,
        "new_gen_used_percent": req.new_gen_used_percent,
        "updated_at": _utc_now_iso(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_jvm_analysis(
    req: JvmAnalyzeRequest,
    upload_paths: list[Path],
    output_dir: Path,
    *,
    jvmcheck_root: Path | None = None,
) -> AnalyzeResult:
    root = jvmcheck_root or DEFAULT_JVMCHECK_ROOT
    src_dir = root / "src"
    if not src_dir.is_dir():
        return AnalyzeResult(
            exit_code=2,
            error=f"jvmcheck src not found: {src_dir}",
            output_dir=output_dir,
            wiki_path=None,
            prompt_path=None,
            brief_path=None,
            summary={},
        )

    import sys

    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    try:
        from jvmcheck.analyzers.jvm_health_analyzer import analyze_jvm_health
        from jvmcheck.cli import (
            _build_analysis_budget,
            _build_tuning_target_snapshot,
            _choose_target_container,
        )
        from jvmcheck.formatters.confluence_formatter import format_analysis_for_confluence
        from jvmcheck.input_resolver import resolve_system_input_files
        from jvmcheck.models import Finding, Recommendation, RuntimeContext, RuntimeMetrics
        from jvmcheck.parsers.custom_config_parser import parse_jvm_options_file
        from jvmcheck.parsers.k8s_yaml_parser import parse_k8s_or_stand_yaml
        from jvmcheck.recommenders.java_tool_options_recommender import enrich_with_recommendations
        from jvmcheck.thresholds import load_thresholds as load_jvm_thresholds
    except Exception as exc:  # noqa: BLE001
        return AnalyzeResult(
            exit_code=2,
            error=f"failed to import jvmcheck modules: {exc}",
            output_dir=output_dir,
            wiki_path=None,
            prompt_path=None,
            brief_path=None,
            summary={},
        )

    resources_root = root / "resources"
    input_contract_error = _validate_jvm_problem_input_contract(req)
    if input_contract_error:
        return AnalyzeResult(
            exit_code=2,
            error=input_contract_error,
            output_dir=output_dir,
            wiki_path=None,
            prompt_path=None,
            brief_path=None,
            summary={},
        )

    system_dir = _resolve_jvm_system_dir(req.system_name, resources_root)
    if not system_dir.exists():
        return AnalyzeResult(
            exit_code=2,
            error=f"АС не найдена: {req.system_name} ({system_dir})",
            output_dir=output_dir,
            wiki_path=None,
            prompt_path=None,
            brief_path=None,
            summary={},
        )

    try:
        _apply_jvm_uploads(system_dir, upload_paths)
        resources_file, jvm_cfg_file = _resolve_jvm_input_files_for_system(
            req.system_name,
            resources_root,
            resolve_system_input_files=resolve_system_input_files,
        )
        budget = parse_k8s_or_stand_yaml(
            resources_file.read_text(encoding="utf-8"),
            source_path=resources_file,
        )
        if not budget.containers:
            raise ValueError("No containers with resources found in input file.")
        custom_options = parse_jvm_options_file(jvm_cfg_file) if jvm_cfg_file else {}
        container = _choose_target_container(
            budget,
            requested_container=req.container_name,
            requested_pod=req.pod_name,
        )
        analysis_budget = _build_analysis_budget(budget, container.pod_name)
        if container.name in custom_options:
            container.java_tool_options = custom_options[container.name]

        _enrich_runtime_metrics_from_context(req, container)
        metrics = RuntimeMetrics(
            heap_used_mib=req.heap_used_mib,
            heap_committed_mib=req.heap_committed_mib,
            old_gen_used_mib=req.old_gen_used_mib,
            old_gen_capacity_mib=req.old_gen_capacity_mib,
            gc_pause_p95_ms=req.gc_pause_p95_ms,
            gc_pause_p99_ms=req.gc_pause_p99_ms,
            gc_time_ratio_percent=req.gc_time_ratio_percent,
            container_memory_working_set_mib=req.container_memory_working_set_mib,
        )
        context = RuntimeContext(
            jdk_version=req.jdk_version,
            spring_boot_version=req.spring_boot_version,
            framework_hints={},
        )
        thresholds = load_jvm_thresholds(req.threshold_profile)
        analysis = analyze_jvm_health(
            container=container,
            metrics=metrics,
            threshold_set=thresholds,
        )
        _seed_selected_problem_findings(analysis, req.selected_problems, finding_cls=Finding)
        _add_contextual_signal_findings(analysis, req, finding_cls=Finding)
        analysis = enrich_with_recommendations(
            container=container,
            budget=analysis_budget,
            analysis=analysis,
            runtime_context=context,
        )
        _add_missing_input_recommendations(analysis, req, recommendation_cls=Recommendation)
        _filter_jvm_analysis_by_selected_problems(analysis, req.selected_problems)
        input_audit = _audit_selected_problem_inputs(req)
        guardrails = _apply_contextual_jvm_guardrails(
            analysis=analysis,
            selected_problems=req.selected_problems,
            container=container,
            req=req,
        )
        _annotate_recommendation_diffs(analysis, container)
        output_dir.mkdir(parents=True, exist_ok=True)
        analysis_dict = dataclasses.asdict(analysis)
        analysis_dict["tuning_target_snapshot"] = _build_tuning_target_snapshot(container)
        (output_dir / "jvm_analysis.json").write_text(
            json.dumps(analysis_dict, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        base_wiki_text = format_analysis_for_confluence(
            analysis=analysis,
            container=container,
            runtime_metrics=metrics,
            runtime_context=context,
            system_name=req.system_name,
        )
        base_wiki_text = _localize_jvm_wiki_text(base_wiki_text)
        wiki_text = _build_jvm_problem_statement_block(req, container) + "\n\n" + base_wiki_text
        wiki_text += "\n\n" + _build_jvm_targeted_context_section(
            req=req,
            container_name=container.name,
            input_audit=input_audit,
            guardrails=guardrails,
            analysis=analysis_dict,
            current_java_options=container.java_tool_options or [],
        )
        wiki_path = output_dir / "jvm_confluence.wiki"
        wiki_path.write_text(wiki_text, encoding="utf-8")
        brief = _build_jvm_brief(
            req,
            container.name,
            analysis_dict,
            input_audit=input_audit,
            guardrails=guardrails,
        )
        brief_path = output_dir / "jvm_brief.md"
        brief_path.write_text(brief, encoding="utf-8")
        prompt_path = output_dir / "jvm_prompt.txt"
        prompt_path.write_text(_build_jvm_prompt(brief), encoding="utf-8")
        summary = _build_jvm_summary(req, analysis_dict)
        save_jvm_last_input(req, root=root)
        return AnalyzeResult(
            exit_code=0,
            error=None,
            output_dir=output_dir,
            wiki_path=wiki_path,
            prompt_path=prompt_path,
            brief_path=brief_path,
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001
        return AnalyzeResult(
            exit_code=2,
            error=str(exc),
            output_dir=output_dir,
            wiki_path=None,
            prompt_path=None,
            brief_path=None,
            summary={},
        )


def _apply_jvm_uploads(system_dir: Path, uploads: list[Path]) -> None:
    if not uploads:
        return
    for src in uploads:
        role = _classify_jvm_upload(src.name)
        if role == "resources":
            target = system_dir / "resources.yaml"
        elif role == "jvm":
            target = system_dir / f"jvm-config{src.suffix.lower() or '.txt'}"
        else:
            raise ValueError(
                f"неизвестный тип файла для jvm: {src.name}. "
                "Ожидаются resources*.yml/.yaml и jvm-config*.txt/.yml/.yaml"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())


def _classify_jvm_upload(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith((".yaml", ".yml")) and ("resource" in lower or "values" in lower):
        return "resources"
    if lower.endswith((".yaml", ".yml", ".txt")) and ("jvm" in lower or "java" in lower):
        return "jvm"
    return ""


def _resolve_jvm_system_dir(system_name: str, resources_root: Path) -> Path:
    if system_name in DEMO_JVM_SYSTEMS:
        return DEMO_JVM_ROOT / system_name
    if system_name == "__root__":
        return resources_root
    return resources_root / system_name


def _resolve_jvm_input_files_for_system(
    system_name: str,
    resources_root: Path,
    *,
    resolve_system_input_files: Any,
) -> tuple[Path, Path | None]:
    if system_name in DEMO_JVM_SYSTEMS:
        return _resolve_demo_jvm_input_files(system_name)
    if system_name == "__root__":
        return _resolve_root_jvm_input_files(resources_root)
    return resolve_system_input_files(
        systems_root=resources_root,
        system_name=system_name,
        resources_file=None,
        jvm_config_file=None,
    )


def _resolve_root_jvm_input_files(resources_root: Path) -> tuple[Path, Path | None]:
    yaml_files = sorted(
        p for p in resources_root.iterdir() if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
    )
    txt_files = sorted(
        p for p in resources_root.iterdir() if p.is_file() and p.suffix.lower() == ".txt"
    )
    resources_file = None
    for p in yaml_files:
        name = p.name.lower()
        if "resource" in name or "values" in name:
            resources_file = p
            break
    if resources_file is None and yaml_files:
        resources_file = yaml_files[0]
    if resources_file is None:
        raise ValueError("В resources/ не найден resources YAML файл")
    jvm_cfg = None
    for p in yaml_files + txt_files:
        name = p.name.lower()
        if "jvm" in name or "java" in name or "option" in name or "tool" in name:
            jvm_cfg = p
            break
    return resources_file, jvm_cfg


def _resolve_demo_jvm_input_files(system_name: str) -> tuple[Path, Path | None]:
    spec = DEMO_JVM_SYSTEMS.get(system_name)
    if not spec:
        raise ValueError(f"Неизвестная demo система: {system_name}")
    ddir = DEMO_JVM_ROOT / system_name
    resources_file = ddir / str(spec["resources_file"])
    jvm_file = ddir / str(spec["jvm_file"])
    if not resources_file.is_file():
        raise ValueError(f"Для demo системы не найден resources файл: {resources_file}")
    return resources_file, (jvm_file if jvm_file.is_file() else None)


def _last_input_key(container_name: str, pod_name: str | None) -> str:
    pod = str(pod_name or "").strip()
    if not pod:
        return container_name
    return f"{pod}::{container_name}"


def _filter_jvm_analysis_by_selected_problems(
    analysis: Any,
    selected_problems: list[str],
) -> None:
    if not selected_problems:
        return
    prefixes: tuple[str, ...] = tuple(
        p for sid in selected_problems for p in JVM_PROBLEM_RULE_PREFIXES.get(sid, ())
    )
    if not prefixes:
        return
    selected_findings = []
    for f in analysis.findings:
        source = str((getattr(f, "evidence", {}) or {}).get("source") or "")
        if source == "context_metric":
            selected_findings.append(f)
            continue
        if f.code.startswith(prefixes):
            selected_findings.append(f)
    analysis.findings = selected_findings
    if not selected_findings:
        analysis.recommendations = []
        return
    selected_codes = {f.code for f in selected_findings}
    filtered = []
    for rec in analysis.recommendations:
        rule_ids = set(getattr(rec, "rule_ids", []) or [])
        if "input.metric_missing" in rule_ids:
            filtered.append(rec)
            continue
        if not rule_ids or rule_ids.intersection(selected_codes):
            filtered.append(rec)
    analysis.recommendations = filtered


def _seed_selected_problem_findings(
    analysis: Any,
    selected_problems: list[str],
    *,
    finding_cls: Any,
) -> None:
    if not selected_problems:
        return
    existing_codes = {f.code for f in analysis.findings}
    # We treat selected problems as already observed facts for targeted impact analysis.
    for pid in selected_problems:
        for code, severity, message in JVM_PROBLEM_SEED_FINDINGS.get(pid, ()):
            if code in existing_codes:
                continue
            analysis.findings.append(
                finding_cls(
                    code=code,
                    severity=severity,
                    message=message,
                    evidence={"source": "selected_problem"},
                    threshold="selected by operator",
                    details={"source": "ui.selected_problems"},
                )
            )
            existing_codes.add(code)


def _audit_selected_problem_inputs(req: JvmAnalyzeRequest) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pid in req.selected_problems:
        required = JVM_PROBLEM_REQUIRED_METRICS.get(pid, ())
        missing: list[str] = []
        if pid == "heap_pressure":
            heap_any = any(
                getattr(req, field, None) is not None
                for field in ("heap_used_mib", "heap_used_percent", "old_gen_used_percent")
            )
            if not heap_any:
                missing.append("Heap used (MiB) or Heap used (%) or OldGen used (%)")
        else:
            for metric_field, metric_label in required:
                if getattr(req, metric_field, None) is None:
                    missing.append(metric_label)
        rows.append(
            {
                "problem_id": pid,
                "required_total": len(required),
                "missing": missing,
                "status": "ok" if not missing else "partial",
            }
        )
    return rows


def _validate_jvm_problem_input_contract(req: JvmAnalyzeRequest) -> str | None:
    missing_global = [
        field_name for field_name in JVM_ALWAYS_REQUIRED_INPUTS if getattr(req, field_name, None) is None
    ]
    if missing_global:
        return (
            "Заполните обязательные поля JVM: gc_pause_p95_ms, "
            "heap_used_mib, container_memory_usage_percent."
        )
    for pid in req.selected_problems:
        required_fields = JVM_PROBLEM_REQUIRED_INPUTS.get(pid, ())
        if not required_fields:
            continue
        if any(getattr(req, field, None) is not None for field in required_fields):
            continue
        return (
            f"Для проблемы '{pid}' укажите обязательные значения: "
            + ", ".join(required_fields)
        )
    return None


def _has_any_context_metrics(req: JvmAnalyzeRequest) -> bool:
    fields = (
        "gc_pause_p95_ms",
        "gc_pause_p99_ms",
        "gc_time_ratio_percent",
        "container_memory_usage_percent",
        "heap_used_mib",
        "heap_used_percent",
        "old_gen_used_mib",
        "old_gen_capacity_mib",
        "old_gen_used_percent",
        "new_gen_used_mib",
        "new_gen_capacity_mib",
        "new_gen_used_percent",
    )
    return any(getattr(req, field, None) is not None for field in fields)


def _enrich_runtime_metrics_from_context(req: JvmAnalyzeRequest, container: Any) -> None:
    if (
        req.container_memory_working_set_mib is None
        and req.container_memory_usage_percent is not None
        and container.limits.memory_mib
    ):
        req.container_memory_working_set_mib = int(
            max(0.0, req.container_memory_usage_percent) * float(container.limits.memory_mib) / 100.0
        )
    if (
        req.old_gen_used_mib is None
        and req.old_gen_used_percent is not None
        and req.old_gen_capacity_mib
    ):
        req.old_gen_used_mib = int(
            max(0.0, req.old_gen_used_percent) * float(req.old_gen_capacity_mib) / 100.0
        )
    if (
        req.heap_used_mib is None
        and req.heap_used_percent is not None
        and req.heap_committed_mib
    ):
        req.heap_used_mib = int(
            max(0.0, req.heap_used_percent) * float(req.heap_committed_mib) / 100.0
        )


def _add_contextual_signal_findings(analysis: Any, req: JvmAnalyzeRequest, *, finding_cls: Any) -> None:
    existing = {f.code for f in analysis.findings}

    def add(code: str, severity: str, message: str, threshold: str) -> None:
        if code in existing:
            return
        analysis.findings.append(
            finding_cls(
                code=code,
                severity=severity,
                message=message,
                evidence={"source": "context_metric"},
                threshold=threshold,
                details={"source": "ui.metric_input"},
            )
        )
        existing.add(code)

    if req.heap_used_percent is None and req.heap_used_mib is None:
        add(
            "heap.metric_missing",
            "info",
            "Heap utilization is not provided. Recommendations may be less precise.",
            "provide heap_used_mib or heap_used_percent",
        )
    elif req.heap_used_percent is not None and req.heap_used_percent >= 85:
        add(
            "heap.high_utilization_signal",
            "warning",
            f"Heap utilization is high ({req.heap_used_percent:.1f}%).",
            "heap_used_percent >= 85",
        )

    if req.old_gen_used_percent is None and req.old_gen_used_mib is None:
        add(
            "oldgen.metric_missing",
            "info",
            "OldGen utilization is not provided. OldGen recommendations may be less precise.",
            "provide old_gen_used_percent or old_gen_used_mib",
        )
    elif req.old_gen_used_percent is not None and req.old_gen_used_percent >= 80:
        add(
            "oldgen.high_utilization_signal",
            "warning",
            f"OldGen utilization is high ({req.old_gen_used_percent:.1f}%).",
            "old_gen_used_percent >= 80",
        )

    if req.new_gen_used_percent is not None and req.new_gen_used_percent >= 75:
        add(
            "newgen.high_utilization_signal",
            "info",
            f"NewGen utilization is elevated ({req.new_gen_used_percent:.1f}%).",
            "new_gen_used_percent >= 75",
        )


def _add_missing_input_recommendations(
    analysis: Any,
    req: JvmAnalyzeRequest,
    *,
    recommendation_cls: Any,
) -> None:
    warnings: list[str] = []
    if req.heap_used_percent is None and req.heap_used_mib is None:
        warnings.append("Добавьте heap_used_mib или heap_used_percent для точной оценки pressure.")
    if req.old_gen_used_percent is None and req.old_gen_used_mib is None:
        warnings.append("Добавьте old_gen_used_percent или old_gen_used_mib для точной оценки oldgen pressure.")
    if req.gc_pause_p95_ms is None and "gc_latency" in req.selected_problems:
        warnings.append("Для gc_latency укажите gc_pause_p95_ms.")
    if req.container_memory_usage_percent is None and "memory_pressure" in req.selected_problems:
        warnings.append("Для memory_pressure укажите container_memory_usage_percent.")
    if not warnings:
        return
    analysis.recommendations.append(
        recommendation_cls(
            title="Input data quality warning",
            rationale="Some problem-specific inputs are missing; analysis confidence is reduced.",
            suggested_java_tool_options=[],
            confidence="low",
            evidence_score=35,
            risk_score=10,
            expected_gain="higher recommendation accuracy after metric enrichment",
            verification_window="before next tuning cycle",
            rollback_plan=["No rollback required."],
            rule_ids=["input.metric_missing"],
            notes=warnings,
        )
    )


def _apply_contextual_jvm_guardrails(
    *,
    analysis: Any,
    selected_problems: list[str],
    container: Any,
    req: JvmAnalyzeRequest,
) -> list[str]:
    messages: list[str] = []
    if not selected_problems:
        return messages

    heap_ratio = _ratio(req.heap_used_mib, req.heap_committed_mib)
    old_gen_ratio = _ratio(req.old_gen_used_mib, req.old_gen_capacity_mib)
    high_heap_pressure = any(
        value is not None and value >= 0.82 for value in (heap_ratio, old_gen_ratio)
    )
    if req.heap_used_percent is not None and req.heap_used_percent >= 82:
        high_heap_pressure = True
    if req.old_gen_used_percent is not None and req.old_gen_used_percent >= 80:
        high_heap_pressure = True
    current_max_ram = _read_flag_float(container.java_tool_options, "-XX:MaxRAMPercentage")

    if "gc_latency" in selected_problems and high_heap_pressure:
        messages.append(
            "GC latency observed with already high heap utilization: avoid heap shrinking and prioritize GC policy tuning."
        )
        for rec in analysis.recommendations:
            original = list(rec.suggested_java_tool_options or [])
            kept: list[str] = []
            removed: list[str] = []
            for flag in original:
                if not flag.startswith("-XX:MaxRAMPercentage="):
                    kept.append(flag)
                    continue
                proposed = _flag_float_value(flag)
                if proposed is None:
                    kept.append(flag)
                    continue
                lower_than_current = (
                    current_max_ram is not None and proposed < current_max_ram
                )
                too_low_without_baseline = (
                    current_max_ram is None and proposed < 65.0
                )
                if lower_than_current or too_low_without_baseline:
                    removed.append(flag)
                else:
                    kept.append(flag)
            if removed:
                rec.suggested_java_tool_options = kept
                rec.blocking_conditions = list(rec.blocking_conditions or []) + [
                    "High heap utilization: do not reduce heap budget while fixing GC latency."
                ]
                rec.notes = list(rec.notes or []) + [
                    "Removed risky option(s): " + ", ".join(removed)
                ]
                messages.append(
                    "GC latency selected with high heap usage: recommendations that reduce "
                    "MaxRAMPercentage were removed."
                )
    return sorted(set(messages))


def _annotate_recommendation_diffs(analysis: Any, container: Any) -> None:
    current = _flag_map(container.java_tool_options or [])
    for rec in analysis.recommendations:
        diffs = _recommendation_diff_lines(current, rec.suggested_java_tool_options or [])
        if not diffs:
            continue
        rec.notes = list(rec.notes or []) + ["Config diff:"] + diffs


def _recommendation_diff_lines(
    current_map: dict[str, str],
    proposed_flags: list[str],
) -> list[str]:
    lines: list[str] = []
    for flag in proposed_flags:
        key, raw_value = _split_flag(flag)
        if not key:
            continue
        old = current_map.get(key)
        new = raw_value if raw_value is not None else "enabled"
        if old is None:
            lines.append(f"- add {key}={new}")
            continue
        if old == new:
            lines.append(f"- keep {key}={new} (already set)")
            continue
        lines.append(f"- change {key}: {old} -> {new}")
    return lines


def _flag_map(flags: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for flag in flags:
        key, value = _split_flag(flag)
        if not key:
            continue
        result[key] = value if value is not None else "enabled"
    return result


def _split_flag(flag: str) -> tuple[str, str | None]:
    raw = str(flag or "").strip()
    if not raw:
        return "", None
    if "=" in raw:
        key, value = raw.split("=", 1)
        return key.strip(), value.strip()
    return raw, None


def _read_flag_float(flags: list[str], key: str) -> float | None:
    for flag in flags:
        fkey, raw = _split_flag(flag)
        if fkey != key:
            continue
        try:
            return float(raw) if raw is not None else None
        except Exception:
            return None
    return None


def _flag_float_value(flag: str) -> float | None:
    _, raw = _split_flag(flag)
    try:
        return float(raw) if raw is not None else None
    except Exception:
        return None


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def _build_jvm_problem_statement_block(req: JvmAnalyzeRequest, container: Any) -> str:
    container_name = str(getattr(container, "name", "") or "")
    pod_name = str(getattr(container, "pod_name", "") or "")
    lines = [
        "h2. Выбранные JVM-проблемы и входные значения",
        f"*Система:* {req.system_name}",
        f"*Pod:* {pod_name or 'N/A'}",
        f"*Контейнер:* {container_name}",
        f"*Выбранные проблемы:* {', '.join(req.selected_problems) if req.selected_problems else 'нет (только контекстные метрики)'}",
        "",
        "|| Параметр || Значение ||",
        f"| gc_pause_p95_ms | {_display_or_na(req.gc_pause_p95_ms)} |",
        f"| gc_pause_p99_ms | {_display_or_na(req.gc_pause_p99_ms)} |",
        f"| gc_time_ratio_percent | {_display_or_na(req.gc_time_ratio_percent)} |",
        f"| container_memory_usage_percent | {_display_or_na(req.container_memory_usage_percent)} |",
        f"| heap_used_mib | {_display_or_na(req.heap_used_mib)} |",
        f"| heap_used_percent | {_display_or_na(req.heap_used_percent)} |",
        f"| old_gen_used_mib | {_display_or_na(req.old_gen_used_mib)} |",
        f"| old_gen_capacity_mib | {_display_or_na(req.old_gen_capacity_mib)} |",
        f"| old_gen_used_percent | {_display_or_na(req.old_gen_used_percent)} |",
        f"| new_gen_used_mib | {_display_or_na(req.new_gen_used_mib)} |",
        f"| new_gen_capacity_mib | {_display_or_na(req.new_gen_capacity_mib)} |",
        f"| new_gen_used_percent | {_display_or_na(req.new_gen_used_percent)} |",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _display_or_na(value: Any) -> str:
    return "N/A" if value is None else str(value)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_jvm_targeted_context_section(
    *,
    req: JvmAnalyzeRequest,
    container_name: str,
    input_audit: list[dict[str, Any]],
    guardrails: list[str],
    analysis: dict[str, Any],
    current_java_options: list[str],
) -> str:
    lines = [
        "h3. Проверка контекста по выбранным проблемам",
        f"*Выбранные проблемы:* {', '.join(req.selected_problems) or '-'}",
        f"*Целевой контейнер:* {container_name}",
        "",
        "|| Проблема || Статус входных данных || Не хватает метрик ||",
    ]
    for row in input_audit:
        missing = ", ".join(row["missing"]) if row["missing"] else "-"
        status = "OK" if row["status"] == "ok" else "ЧАСТИЧНО"
        lines.append(f"| {row['problem_id']} | {status} | {missing} |")
    lines.append("")
    lines.append("h3. Стратегия рекомендаций (Safe/Balanced/Aggressive)")
    for pid in req.selected_problems:
        strategy = JVM_PROBLEM_STRATEGIES.get(pid)
        if not strategy:
            continue
        lines.append(f"h4. {pid}")
        lines.append(f"* Safe: {strategy['safe']}")
        lines.append(f"* Balanced: {strategy['balanced']}")
        lines.append(f"* Aggressive: {strategy['aggressive']}")
        lines.append("")
    if guardrails:
        lines.append("h3. Ограничения и защитные правила")
        for msg in guardrails:
            lines.append(f"* {msg}")
        lines.append("")
    if any(row["status"] == "partial" for row in input_audit):
        lines.append("h3. Какие данные добавить для точности")
        lines.append("* GC-метрики: соберите p95/p99 pause и GC time ratio из APM/Prometheus или GC logs.")
        lines.append("* Heap-метрики: добавьте heap used/committed и old-gen used/capacity из JMX.")
        lines.append("* Memory pressure: добавьте container working set/usage и свяжите с memory limit.")
        lines.append("")
    lines.append("h3. Результат таргетного анализа")
    lines.append(
        f"* Findings в области анализа: {len(analysis.get('findings') or [])}; рекомендаций: {len(analysis.get('recommendations') or [])}."
    )
    lines.append("")
    lines.extend(_build_pod_scale_out_guidance(req, analysis))
    lines.append("")
    lines.extend(
        _build_jvm_copy_paste_section(
            analysis=analysis,
            container_name=container_name,
            current_java_options=current_java_options,
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def _build_jvm_copy_paste_section(
    *,
    analysis: dict[str, Any],
    container_name: str,
    current_java_options: list[str],
) -> list[str]:
    lines: list[str] = ["h3. Предлагаемые изменения (Copy/Paste)"]
    proposed = _collect_proposed_flags(analysis)
    if not proposed:
        lines.append("* Для выбранных проблем нет прямых изменений JVM-флагов.")
    else:
        effective = _merge_java_options(current_java_options, proposed)
        lines.append("h4. jvm-config")
        lines.append("{code:yaml}")
        lines.append(f"{container_name}:")
        lines.append("  javaToolOptions: >")
        for flag in effective:
            lines.append(f"    {flag}")
        lines.append("{code}")
        lines.append("")
        lines.append("h4. Сводка изменений")
        for item in _copy_paste_diff_lines(current_java_options, effective):
            lines.append(f"* {item}")
    memory_plan = analysis.get("memory_plan") or {}
    if memory_plan:
        lines.append("")
        lines.append("h4. resources.yaml (подсказки по memory budget)")
        status = str(memory_plan.get("status") or "")
        delta = memory_plan.get("requested_delta_mib")
        donors = memory_plan.get("donor_suggestions") or {}
        lines.append(f"* Статус memory plan: {status or 'unknown'}")
        if delta is not None:
            lines.append(f"* Рекомендуемая прибавка памяти целевому контейнеру: +{delta}Mi")
        if donors:
            donor_text = ", ".join(f"{k}: -{v}Mi" for k, v in donors.items())
            lines.append(f"* Кандидаты-доноры памяти: {donor_text}")
    return lines


def _collect_proposed_flags(analysis: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for rec in analysis.get("recommendations") or []:
        for flag in rec.get("suggested_java_tool_options") or []:
            text = str(flag).strip()
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)
            ordered.append(text)
    return ordered


def _merge_java_options(current: list[str], proposed: list[str]) -> list[str]:
    merged: list[str] = []
    key_to_idx: dict[str, int] = {}
    for flag in current:
        key, _ = _split_flag(flag)
        if not key:
            continue
        key_to_idx[key] = len(merged)
        merged.append(flag)
    for flag in proposed:
        key, _ = _split_flag(flag)
        if not key:
            continue
        if key in key_to_idx:
            merged[key_to_idx[key]] = flag
        else:
            key_to_idx[key] = len(merged)
            merged.append(flag)
    return merged


def _copy_paste_diff_lines(current: list[str], effective: list[str]) -> list[str]:
    before = _flag_map(current)
    after = _flag_map(effective)
    lines: list[str] = []
    for key, value in after.items():
        prev = before.get(key)
        if prev is None:
            lines.append(f"добавить {key}={value}")
        elif prev != value:
            lines.append(f"изменить {key}: {prev} -> {value}")
    if not lines:
        lines.append("дельта отсутствует (все предложенные флаги уже заданы)")
    return lines


def _build_pod_scale_out_guidance(
    req: JvmAnalyzeRequest,
    analysis: dict[str, Any],
) -> list[str]:
    finding_codes = {str(f.get("code") or "") for f in (analysis.get("findings") or [])}
    lines: list[str] = ["h3. Когда помогает увеличение количества pod'ов"]
    lines.append("* Масштабирование pod'ов обычно помогает при CPU/GC перегрузке под стабильной высокой нагрузкой, когда память одного pod не упирается в лимит.")
    if "gc_latency" in req.selected_problems:
        lines.append("* Для GC latency scale-out полезен, если паузы растут из-за высокой конкурентной нагрузки и при этом нет выраженного memory pressure.")
    if "memory_pressure" in req.selected_problems or "memory.limit_pressure" in finding_codes:
        lines.append("* При memory pressure scale-out помогает ограниченно: если каждый pod уже близко к memory limit, сначала правят memory budget/JVM, а потом масштабируют.")
    if "heap_pressure" in req.selected_problems:
        lines.append("* При высоком heap/oldgen pressure scale-out уместен после проверки, что профиль нагрузки горизонтально делится и heap каждого pod действительно снизится.")
    lines.append("* Не рекомендуется рассчитывать только на scale-out, если проблема вызвана утечкой памяти или некорректными JVM-флагами.")
    return lines


def _localize_jvm_wiki_text(text: str) -> str:
    mapping = {
        "h2. JVM Tuning Recommendation": "h2. Рекомендации по JVM-тюнингу",
        "*System:*": "*Система:*",
        "*Target container:*": "*Целевой контейнер:*",
        "*Lifecycle status:*": "*Статус жизненного цикла:*",
        "h3. Runtime Context": "h3. Контекст рантайма",
        "|| Parameter || Value ||": "|| Параметр || Значение ||",
        "h3. Findings": "h3. Findings",
        "* No critical findings detected.": "* Критичные findings не обнаружены.",
        "h3. Recommended Java Tool Options": "h3. Рекомендованные Java Tool Options",
        "* No recommendations.": "* Рекомендации не сформированы.",
        "*Rationale:*": "*Обоснование:*",
        "*Confidence:*": "*Уверенность:*",
        "*Evidence score:*": "*Оценка доказательности:*",
        "*Risk score:*": "*Оценка риска:*",
        "*Expected gain:*": "*Ожидаемый эффект:*",
        "*Verification window:*": "*Окно верификации:*",
        "*Platform escalation required:*": "*Нужна платформенная эскалация:*",
        "*Suggested options:*": "*Предлагаемые опции:*",
        "*Rollback plan:*": "*План отката:*",
        "*Blocking conditions:*": "*Блокирующие условия:*",
        "*Notes:*": "*Примечания:*",
        "h3. Pod Memory Quota Plan": "h3. План по pod memory quota",
        "h3. Multi-run Stability": "h3. Стабильность на нескольких прогонах",
        "h3. Engineer Validation Runbook": "h3. Инженерный план валидации",
        "h3. Change Risks and Side Effects": "h3. Риски и побочные эффекты изменений",
        "h3. Escalation Rule": "h3. Правило эскалации",
    }
    localized = text
    for src, dst in mapping.items():
        localized = localized.replace(src, dst)
    return localized


def _build_jvm_summary(req: JvmAnalyzeRequest, analysis: dict[str, Any]) -> dict[str, Any]:
    cards = []
    for finding in analysis.get("findings") or []:
        code = str(finding.get("code") or "?")
        cards.append(
            {
                "id": code,
                "severity": str(finding.get("severity") or "warning"),
                "message": str(finding.get("message") or ""),
                "title": code,
                "advice": "",
                "threshold": str(finding.get("threshold") or ""),
            }
        )
    counts = {"critical": 0, "warning": 0, "info": 0}
    for c in cards:
        bucket = _severity_bucket(c.get("severity", "warning"))
        counts[bucket] += 1
    return {
        "mode": "jvm",
        "system_name": req.system_name,
        "selected_problems": req.selected_problems,
        "threshold_profile": req.threshold_profile,
        "total_findings": len(cards),
        "severity_counts": counts,
        "findings_ui": cards,
    }


def _build_jvm_brief(
    req: JvmAnalyzeRequest,
    container_name: str,
    analysis: dict[str, Any],
    *,
    input_audit: list[dict[str, Any]] | None = None,
    guardrails: list[str] | None = None,
) -> str:
    lines = [
        "# JVM Analysis Brief",
        "",
        f"- System: `{req.system_name}`",
        f"- Container: `{container_name}`",
        f"- Selected problems: `{', '.join(req.selected_problems)}`",
        f"- Threshold profile: `{req.threshold_profile}`",
        f"- Findings: `{len(analysis.get('findings') or [])}`",
        "",
    ]
    for finding in analysis.get("findings") or []:
        sev = str(finding.get("severity") or "warning").upper()
        lines.append(f"## [{sev}] {finding.get('code')}")
        lines.append(f"- Message: {finding.get('message')}")
        threshold = str(finding.get("threshold") or "")
        if threshold:
            lines.append(f"- Threshold: {threshold}")
        lines.append("")
    if input_audit:
        lines.append("## Problem input coverage")
        for row in input_audit:
            missing = ", ".join(row["missing"]) if row["missing"] else "-"
            lines.append(
                f"- {row['problem_id']}: status={row['status']}, missing_metrics={missing}"
            )
        lines.append("")
    if guardrails:
        lines.append("## Guardrails")
        for item in guardrails:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_jvm_prompt(brief: str) -> str:
    return (
        "Подготовь инженерную сводку по JVM-анализу на русском языке.\n"
        "Обязательно учитывай выбранные проблемы и введённые пользователем метрики как входные факты.\n"
        "Не предлагай шаги, противоречащие guardrails (например, не уменьшай heap budget при высокой утилизации heap/oldgen).\n"
        "Если данных недостаточно, явно выдели это как ограничение точности и предложи, какие метрики собрать.\n"
        "Отдельно укажи, когда уместен scale-out по pod'ам, а когда сначала нужен JVM/memory tuning.\n"
        "Структурируй ответ как: Риски -> Рекомендованные изменения -> План проверки -> Условия эскалации.\n\n"
        "DATA:\n\n"
        f"{brief}"
    )


def build_zip(output_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README_AI.txt", AI_USAGE)
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=str(path.relative_to(output_dir)))
    return buf.getvalue()
