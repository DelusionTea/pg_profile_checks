"""Build Confluence Wiki Markup pages from advisor reports."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from compare_settings import DiffStatus
from pgprofile_advisor import AdvisorReport
from pgprofile_compare import (
    format_delta,
    format_number,
    format_value_cell,
    interval_diff_hours,
)
from pgprofile_nt_prod import NtProdValidation

CONFLUENCE_PROMPT = Path(__file__).resolve().parent / "prompts" / "analyst_confluence.md"
NT_PROD_CONFLUENCE_PROMPT = (
    Path(__file__).resolve().parent / "prompts" / "analyst_confluence_nt_prod.md"
)

WAL_HIGHLIGHT = ("wal_bytes", "wal_records", "wal_buffers_full", "wal_write", "wal_sync")

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


def _nt_prod_metric_wiki_table(
    items: list[Any],
    col_nt: str,
    col_prod: str,
    *,
    show_per_hour: bool = True,
    max_rows: int = 25,
) -> list[str]:
    if not items:
        return ["_Нет значимых расхождений._", ""]

    lines = [
        f"||Метрика||{col_nt}||{col_prod}||Delta||",
    ]
    for diff in items[:max_rows]:
        key = _wiki_escape(diff.key)
        nt_v = _wiki_escape(format_value_cell(diff, "a", show_per_hour=show_per_hour))
        prod_v = _wiki_escape(format_value_cell(diff, "b", show_per_hour=show_per_hour))
        delta = _wiki_escape(format_delta(diff))
        lines.append(f"|{key}|{nt_v}|{prod_v}|{delta}|")
        if diff.key == "wal_bytes" and show_per_hour:
            ph_a, ph_b = diff.per_hour_a, diff.per_hour_b
            if ph_a is not None or ph_b is not None:
                nt_mb = f"{ph_a / 1_048_576:.2f} MB/h" if ph_a is not None else "-"
                prod_mb = f"{ph_b / 1_048_576:.2f} MB/h" if ph_b is not None else "-"
                lines.append(
                    f"|{_wiki_escape('  wal throughput')}|{nt_mb}|{prod_mb}| |"
                )
    if len(items) > max_rows:
        lines.append(f"|...|ещё {len(items) - max_rows} строк| | |")
    lines.append("")
    return lines


def build_nt_prod_confluence_stub(
    validation: NtProdValidation,
    *,
    page_title: str | None = None,
) -> str:
    from pgprofile_nt_prod import WAL_HIGHLIGHT

    run_nt = validation.run_nt
    run_prod = validation.run_prod
    s = validation.settings
    col_nt = run_nt.run_id
    col_prod = run_prod.run_id
    show_ph = True

    title = page_title or "pg_profile: валидация НТ vs ПРОМ"
    lines: list[str] = [f"h1. {title}", ""]

    if not s.valid:
        total = s.differ + s.only_nt + s.only_prod
        lines.extend(
            [
                "{warning:title=ПРОГОН НЕВАЛИДЕН}",
                f"Defined settings НТ и ПРОМ расходятся: *{total}* отличий "
                f"({s.differ} differ, {s.only_nt} only NT, {s.only_prod} only PROD).",
                "Сравнение метрик *не следует* использовать до выравнивания настроек.",
                "{warning}",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "{status:colour=Green|title=OK|subtle=false} "
                "Настройки НТ и ПРОМ совпадают (Defined settings)",
                "",
            ]
        )

    lines.extend(
        [
            "h2. Параметры сравнения",
            "",
            "||Параметр||Значение||",
            f"|Дата формирования|{datetime.now().strftime('%Y-%m-%d %H:%M')}|",
            f"|НТ отчёт|{_wiki_escape(run_nt.path.name)}|",
            f"|ПРОМ отчёт|{_wiki_escape(run_prod.path.name)}|",
            f"|НТ интервал|{run_nt.ctx.interval_hours:.1f} ч|",
            f"|ПРОМ интервал|{run_prod.ctx.interval_hours:.1f} ч|",
            f"|Порог расхождения|>= {validation.min_change_pct:g}%|",
            f"|Валидность настроек|{'OK' if s.valid else 'НЕВАЛИДНО'}|",
            f"|Значимых расхождений метрик|{validation.significant_count}|",
            "",
        ]
    )

    diff_h = interval_diff_hours(run_nt, run_prod)
    if diff_h > 0.01:
        lines.extend(
            [
                "{note:title=Разная длительность интервалов}",
                f"Интервалы отличаются на *{diff_h:.1f} ч*. Счётчики в таблицах показаны с нормализацией */час*.",
                "{note}",
                "",
            ]
        )

    if not s.valid:
        issues = [r for r in s.rows if r.status is not DiffStatus.SAME]
        lines.extend(["h2. Расхождения настроек", "", "||Параметр||НТ||ПРОМ||Статус||"])
        for row in issues[:20]:
            status = row.status.value
            nt_v = _wiki_escape(row.nt_value or "—")
            prod_v = _wiki_escape(row.prod_value or "—")
            lines.append(f"|{_wiki_escape(row.name)}|{nt_v}|{prod_v}|{status}|")
        if len(issues) > 20:
            lines.append(f"|...|ещё {len(issues) - 20}| | |")
        lines.append("")

    grouped: dict[str, list[Any]] = {}
    for diff in validation.metric_diffs:
        grouped.setdefault(diff.section, []).append(diff)

    priority = [
        ("wal", "WAL / скорость генерации"),
        ("dml", "DML операции"),
        ("cluster", "Checkpoints / cluster"),
        ("sessions", "Sessions"),
        ("cache", "Cache и I/O"),
        ("tables", "DML по таблицам"),
    ]
    for section, heading in priority:
        if section not in validation.sections:
            continue
        items = grouped.get(section, [])
        if not items and section != "wal":
            continue
        if section == "wal":
            wal_first = [d for d in items if d.key in WAL_HIGHLIGHT]
            wal_rest = [d for d in items if d.key not in WAL_HIGHLIGHT]
            items = wal_first + wal_rest
        lines.append(f"h2. {heading}")
        lines.append("")
        if items:
            lines.extend(_nt_prod_metric_wiki_table(items, col_nt, col_prod, show_per_hour=show_ph))
        else:
            lines.append("_Нет значимых расхождений._")
            lines.append("")

    if validation.query_groups and "queries" in validation.sections:
        lines.extend(["h2. SQL: сводка расхождений", ""])
        lines.append(f"||Запрос||Параметр||{col_nt}||{col_prod}||Delta||")
        row_count = 0
        for group in validation.query_groups[:12]:
            label = _wiki_escape(group.label)
            for diff in group.fields[:4]:
                if row_count >= 30:
                    break
                nt_v = _wiki_escape(format_value_cell(diff, "a", show_per_hour=show_ph))
                prod_v = _wiki_escape(format_value_cell(diff, "b", show_per_hour=show_ph))
                delta = _wiki_escape(format_delta(diff))
                lines.append(
                    f"|{label}|{_wiki_escape(diff.key)}|{nt_v}|{prod_v}|{delta}|"
                )
                row_count += 1
        lines.append("")

    lines.extend(
        [
            "{info:title=О документе}",
            "Таблицы сформированы Python (compare_nt_prod). Краткая интерпретация — разделы ниже от ИИ.",
            "{info}",
            "",
            "----",
            "",
            "_Ниже — вывод ИИ (Confluence Wiki Markup). Вставьте ответ gigacli или merge_confluence.py._",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_nt_prod_confluence_prompt(brief: str) -> str:
    template = (
        NT_PROD_CONFLUENCE_PROMPT.read_text(encoding="utf-8")
        if NT_PROD_CONFLUENCE_PROMPT.exists()
        else ""
    )
    return (
        f"{template}\n\n---\n\n"
        "# DATA FOR ANALYSIS\n\n"
        f"{brief}\n\n---\n\n"
        "Начни ответ сразу с `h2. Краткое резюме` (без h1 и без пояснений)."
    )


def write_nt_prod_confluence_outputs(
    validation: NtProdValidation,
    output_dir: Path,
    *,
    page_title: str | None = None,
) -> None:
    from pgprofile_nt_prod import build_nt_prod_brief

    output_dir.mkdir(parents=True, exist_ok=True)
    brief = build_nt_prod_brief(validation)
    (output_dir / "nt_prod_brief.md").write_text(brief, encoding="utf-8")
    (output_dir / "nt_prod_confluence_stub.wiki").write_text(
        build_nt_prod_confluence_stub(validation, page_title=page_title),
        encoding="utf-8",
    )
    (output_dir / "nt_prod_confluence_prompt.txt").write_text(
        build_nt_prod_confluence_prompt(brief),
        encoding="utf-8",
    )
