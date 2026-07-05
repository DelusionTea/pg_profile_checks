"""Build Confluence Wiki Markup pages from advisor reports."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pgprofile_advisor import AdvisorReport

CONFLUENCE_PROMPT = Path(__file__).resolve().parent / "prompts" / "analyst_confluence.md"

_SEVERITY_STATUS = {
    "critical": "{status:colour=Red|title=CRITICAL|subtle=false}",
    "warning": "{status:colour=Yellow|title=WARNING|subtle=false}",
    "info": "{status:colour=Blue|title=INFO|subtle=false}",
}


def _status(severity: str) -> str:
    return _SEVERITY_STATUS.get(severity.lower(), _SEVERITY_STATUS["warning"])


def _wiki_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _page_title(reports: list[AdvisorReport]) -> str:
    for report in reports:
        if report.source_type == "health_check":
            meta = report.meta.get("report_meta", {})
            server = meta.get("server", "PostgreSQL")
            end = meta.get("report_end", "")
            date_part = str(end)[:10] if end else datetime.now().strftime("%Y-%m-%d")
            return f"pg_profile: {server} ({date_part})"
        if report.source_type == "run_comparison":
            a = report.meta.get("run_a", {}).get("run_id", "A")
            b = report.meta.get("run_b", {}).get("run_id", "B")
            return f"pg_profile: сравнение {a} vs {b}"
        if report.source_type == "settings_diff":
            a = report.meta.get("run_a", {}).get("run_id", "NT")
            b = report.meta.get("run_b", {}).get("run_id", "PROD")
            return f"pg_profile: настройки {a} vs {b}"
    return "pg_profile: отчёт анализа"


def _metadata_rows(reports: list[AdvisorReport]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = [
        ("Дата формирования", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Инструмент", "pg_profile_checks / analyze_pgprofile.py"),
    ]
    for report in reports:
        if report.source_type == "health_check":
            meta = report.meta.get("report_meta", {})
            rows.extend(
                [
                    ("Сервер", str(meta.get("server", "?"))),
                    ("Интервал", f"{meta.get('report_start', '?')} — {meta.get('report_end', '?')}"),
                    ("Длительность", f"{meta.get('interval_hours', '?')} ч"),
                    ("Файл отчёта", str(meta.get("filename", "?"))),
                ]
            )
        elif report.source_type == "run_comparison":
            run_a = report.meta.get("run_a", {})
            run_b = report.meta.get("run_b", {})
            rows.extend(
                [
                    ("Прогон A", str(run_a.get("run_id", "?"))),
                    ("Прогон B", str(run_b.get("run_id", "?"))),
                    ("Длительность A", f"{run_a.get('interval_hours', '?')} ч"),
                    ("Длительность B", f"{run_b.get('interval_hours', '?')} ч"),
                ]
            )
            if report.meta.get("interval_mismatch"):
                rows.append(
                    (
                        "Разница интервалов",
                        f"{report.meta.get('interval_diff_hours', '?')} ч — сравнивайте /час",
                    )
                )
        elif report.source_type == "settings_diff":
            rows.extend(
                [
                    ("Среда A", str(report.meta.get("run_a", {}).get("run_id", "?"))),
                    ("Среда B", str(report.meta.get("run_b", {}).get("run_id", "?"))),
                ]
            )
    total = sum(r.summary.get("total_findings", 0) for r in reports)
    high = sum(r.summary.get("high_priority", 0) for r in reports)
    rows.append(("Всего находок", str(total)))
    rows.append(("Высокий приоритет", str(high)))
    return rows


def _findings_table(reports: list[AdvisorReport]) -> str:
    lines = [
        "h2. Сводка находок",
        "",
        "||Приоритет||Находка||ID||Сообщение||",
    ]
    for report in reports:
        for item in report.advised_findings:
            f = item.finding
            advice = item.advice
            severity = f.get("severity", "warning")
            title = _wiki_escape(str(advice.get("title", f.get("id", "?"))))
            fid = _wiki_escape(str(f.get("id", "?")))
            message = _wiki_escape(str(f.get("message", "")))
            lines.append(f"|{_status(severity)}|{title}|{fid}|{message}|")
    if len(lines) == 3:
        lines.append("|{status:colour=Green|title=OK|subtle=false}|Находок нет|—|—|")
    lines.append("")
    return "\n".join(lines)


def build_confluence_stub(
    reports: list[AdvisorReport],
    *,
    page_title: str | None = None,
) -> str:
    title = page_title or _page_title(reports)
    lines = [
        f"h1. {title}",
        "",
        "{info:title=О документе}",
        "Автоматическая сводка из pg_profile. Таблица находок сформирована скриптом Python.",
        "Разделы ниже «Краткое резюме» и далее — заполняются ИИ (gigacli) и вставляются после этой части.",
        "{info}",
        "",
        "h2. Параметры анализа",
        "",
        "||Параметр||Значение||",
    ]
    for key, value in _metadata_rows(reports):
        lines.append(f"|{_wiki_escape(key)}|{_wiki_escape(value)}|")
    lines.append("")
    lines.extend(_findings_table(reports).splitlines())
    lines.extend(
        [
            "----",
            "",
            "_Ниже — вывод ИИ (Confluence Wiki Markup). Вставьте ответ gigacli или используйте merge_confluence.py._",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_confluence_llm_prompt(brief: str) -> str:
    template = CONFLUENCE_PROMPT.read_text(encoding="utf-8") if CONFLUENCE_PROMPT.exists() else ""
    return (
        f"{template}\n\n---\n\n"
        "# DATA FOR ANALYSIS\n\n"
        f"{brief}\n\n---\n\n"
        "Начни ответ сразу с `h2. Краткое резюме` (без h1 и без пояснений)."
    )


def merge_confluence_page(stub: str, llm_body: str) -> str:
    body = llm_body.strip()
    for prefix in ("```", "```wiki", "```confluence"):
        if body.startswith(prefix):
            body = body.split("\n", 1)[-1]
    if body.endswith("```"):
        body = body.rsplit("```", 1)[0]
    body = body.strip()

    marker = "_Ниже — вывод ИИ"
    if marker in stub:
        head, _ = stub.split(marker, 1)
        stub = head.rstrip() + "\n"
    return stub.rstrip() + "\n\n" + body + "\n"
