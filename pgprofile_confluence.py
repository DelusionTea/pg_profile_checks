"""Build Confluence Wiki Markup pages from advisor reports."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from compare_settings import DiffStatus
from pgprofile_advisor import AdvisorReport
from pgprofile_compare import (
    format_delta,
    format_value_cell,
    interval_diff_hours,
)
from pgprofile_classify import split_settings_rows
from pgprofile_nt_prod import NtProdValidation
from pgprofile_stable_prod import StableProdAnalysis, TuningRecommendation
from pgprofile_symptoms import CauseStatus, SymptomInvestigation

CONFLUENCE_PROMPT = Path(__file__).resolve().parent / "prompts" / "analyst_confluence.md"
NT_PROD_CONFLUENCE_PROMPT = (
    Path(__file__).resolve().parent / "prompts" / "analyst_confluence_nt_prod.md"
)
STABLE_PROD_CONFLUENCE_PROMPT = (
    Path(__file__).resolve().parent / "prompts" / "analyst_confluence_stable_prod.md"
)
SYMPTOM_CONFLUENCE_PROMPT = (
    Path(__file__).resolve().parent / "prompts" / "analyst_confluence_symptom.md"
)

WAL_HIGHLIGHT = ("wal_bytes", "wal_records", "wal_buffers_full", "wal_write", "wal_sync")

WAL_HIGHLIGHT = ("wal_bytes", "wal_records", "wal_buffers_full", "wal_write", "wal_sync")

_SEVERITY_STATUS = {
    "critical": "{status:colour=Red|title=CRITICAL|subtle=false}",
    "warning": "{status:colour=Yellow|title=WARNING|subtle=false}",
    "info": "{status:colour=Blue|title=INFO|subtle=false}",
}

_CHANGE_SAFETY_STATUS = {
    "safe": "{status:colour=Green|title=SAFE|subtle=false}",
    "cautious": "{status:colour=Yellow|title=CAUTIOUS|subtle=false}",
    "risky": "{status:colour=Red|title=RISKY|subtle=false}",
    "restart_required": "{status:colour=Yellow|title=RESTART|subtle=false}",
}

_CHANGE_IMPACT_STATUS = {
    "low": "{status:colour=Green|title=LOW|subtle=false}",
    "medium": "{status:colour=Yellow|title=MEDIUM|subtle=false}",
    "high": "{status:colour=Red|title=HIGH|subtle=false}",
}

_CAUSE_STATUS = {
    "confirmed": "{status:colour=Red|title=CONFIRMED|subtle=false}",
    "suspected": "{status:colour=Yellow|title=SUSPECTED|subtle=false}",
    "possible": "{status:colour=Blue|title=POSSIBLE|subtle=false}",
    "unlikely": "{status:colour=Green|title=UNLIKELY|subtle=false}",
}


def _status(severity: str) -> str:
    return _SEVERITY_STATUS.get(severity.lower(), _SEVERITY_STATUS["warning"])


def _change_safety_status(safety: str) -> str:
    return _CHANGE_SAFETY_STATUS.get(safety.lower(), _CHANGE_SAFETY_STATUS["cautious"])


def _change_impact_status(impact: str) -> str:
    return _CHANGE_IMPACT_STATUS.get(impact.lower(), _CHANGE_IMPACT_STATUS["medium"])


def _cause_status(status: str) -> str:
    return _CAUSE_STATUS.get(status.lower(), _CAUSE_STATUS["possible"])


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
    run_nt = validation.run_nt
    run_prod = validation.run_prod
    s = validation.settings
    col_nt = run_nt.run_id
    col_prod = run_prod.run_id
    show_ph = True

    title = page_title or "pg_profile: валидация НТ vs ПРОМ"
    lines: list[str] = [f"h1. {title}", ""]

    critical, informational = split_settings_rows(s.rows)

    if not s.valid:
        lines.extend(
            [
                "{warning:title=ПРОГОН НЕВАЛИДЕН}",
                f"Defined settings GUC расходятся: *{s.critical_count}* критичных отличий.",
                "Сравнение метрик *не следует* использовать до выравнивания GUC.",
                "{warning}",
                "",
            ]
        )
    elif informational:
        lines.extend(
            [
                "{status:colour=Green|title=GUC OK|subtle=false} Конфигурация GUC совпадает",
                "",
                "{info:title=Справочно: runtime-метаданные}",
                f"Есть *{s.informational_count}* отличий runtime (pg_conf_load_time, postmaster_start, in_hot_standby) — "
                "не блокируют сравнение.",
                "{info}",
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
            f"|GUC валидность|{'OK' if s.valid else 'НЕВАЛИДНО'}|",
            f"|Справочно: объём WAL/операций|{validation.info_count}|",
            f"|Предупреждения производительности|{validation.warning_count}|",
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

    if critical:
        lines.extend(["h2. Критичные расхождения GUC", "", "||Параметр||НТ||ПРОМ||Статус||"])
        for row in critical[:20]:
            status = row.status.value
            nt_v = _wiki_escape(row.nt_value or "—")
            prod_v = _wiki_escape(row.prod_value or "—")
            lines.append(f"|{_wiki_escape(row.name)}|{nt_v}|{prod_v}|{status}|")
        lines.append("")

    if informational:
        lines.extend(
            ["h2. Справочно: runtime / метаданные", "", "||Параметр||НТ||ПРОМ||Статус||"]
        )
        for row in informational[:20]:
            status = row.status.value
            nt_v = _wiki_escape(row.nt_value or "—")
            prod_v = _wiki_escape(row.prod_value or "—")
            lines.append(f"|{_wiki_escape(row.name)}|{nt_v}|{prod_v}|{status}|")
        lines.append("")

    grouped_info: dict[str, list[Any]] = {}
    for diff in validation.metric_diffs_info:
        grouped_info.setdefault(diff.section, []).append(diff)
    grouped_warn: dict[str, list[Any]] = {}
    for diff in validation.metric_diffs_warning:
        grouped_warn.setdefault(diff.section, []).append(diff)

    info_priority = [
        ("wal", "Справочно: WAL / объём"),
        ("dml", "Справочно: DML операции"),
        ("cluster", "Справочно: checkpoints (объём)"),
        ("tables", "Справочно: DML по таблицам"),
    ]
    for section, heading in info_priority:
        if section not in validation.sections:
            continue
        items = grouped_info.get(section, [])
        if not items:
            continue
        if section == "wal":
            wal_first = [d for d in items if d.key in WAL_HIGHLIGHT]
            wal_rest = [d for d in items if d.key not in WAL_HIGHLIGHT]
            items = wal_first + wal_rest
        lines.append(f"h2. {heading}")
        lines.append("")
        lines.extend(_nt_prod_metric_wiki_table(items, col_nt, col_prod, show_per_hour=show_ph))

    warn_priority = [
        ("wal", "WAL — предупреждения"),
        ("cluster", "Checkpoints — предупреждения"),
        ("cache", "Cache — предупреждения"),
    ]
    for section, heading in warn_priority:
        if section not in validation.sections:
            continue
        items = grouped_warn.get(section, [])
        if not items:
            continue
        lines.append(f"h2. {heading}")
        lines.append("")
        lines.extend(_nt_prod_metric_wiki_table(items, col_nt, col_prod, show_per_hour=show_ph))

    if validation.query_groups_info and "queries" in validation.sections:
        lines.extend(["h2. Справочно: SQL — объём и calls", ""])
        lines.append(f"||Запрос||Параметр||{col_nt}||{col_prod}||Delta||")
        row_count = 0
        for group in validation.query_groups_info[:12]:
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

    if validation.query_groups_warning and "queries" in validation.sections:
        lines.extend(["h2. SQL — производительность (mean/max time)", ""])
        lines.append(f"||Запрос||Параметр||{col_nt}||{col_prod}||Delta||")
        row_count = 0
        for group in validation.query_groups_warning[:12]:
            label = _wiki_escape(group.label)
            for diff in group.fields[:4]:
                if row_count >= 20:
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


def _stable_prod_page_title(analysis: StableProdAnalysis, page_title: str | None) -> str:
    if page_title:
        return page_title
    servers: set[str] = set()
    for snap in analysis.reports:
        name = snap.ctx.properties.get("server_name") or snap.path.stem
        servers.add(str(name))
    if len(servers) == 1:
        return f"pg_profile PROD: стабильные проблемы ({next(iter(servers))})"
    return "pg_profile PROD: стабильные проблемы"


def _guc_current_wiki(guc_items: list[Any]) -> str:
    if not guc_items:
        return "—"
    parts: list[str] = []
    for item in guc_items[:3]:
        if isinstance(item, dict):
            guc_name = item.get("guc", "?")
            current = item.get("current_values", {})
        else:
            guc_name = getattr(item, "guc", "?")
            current = getattr(item, "current_values", {})
        if current:
            cur = ", ".join(f"{k}={v}" for k, v in list(current.items())[:4])
            parts.append(f"{guc_name}: {cur}")
        else:
            parts.append(str(guc_name))
    return _wiki_escape("; ".join(parts))


def _recommendations_summary_table(recommendations: list[TuningRecommendation]) -> list[str]:
    lines = [
        "h2. Стабильные рекомендации по GUC",
        "",
        "||Критичность||Проблема||Стабильность||Безопасность||Влияние||GUC (текущие)||Finding ID||",
    ]
    if not recommendations:
        lines.append("|{status:colour=Green|title=OK|subtle=false}|Стабильных проблем нет|—|—|—|—|—|")
        lines.append("")
        return lines

    for rec in recommendations:
        sf = rec.stable_finding
        title = _wiki_escape(rec.title)
        stability = _wiki_escape(
            f"{sf.occurrence_count}/{sf.total_reports} ({sf.stability_ratio:.0%})"
        )
        guc_cur = _guc_current_wiki(rec.guc_items)
        fid = _wiki_escape(rec.finding_rule_id)
        lines.append(
            f"|{_status(rec.problem_severity)}|{title}|{stability}|"
            f"{_change_safety_status(rec.combined_safety)}|"
            f"{_change_impact_status(rec.combined_impact)}|{guc_cur}|{fid}|"
        )
    lines.append("")
    return lines


def _guc_details_table(recommendations: list[TuningRecommendation]) -> list[str]:
    lines = [
        "h2. Детали GUC",
        "",
        "||GUC||Направление||Безопасность||Влияние||Текущие значения||Postgres Pro||",
    ]
    row_count = 0
    for rec in recommendations:
        for guc in rec.guc_items:
            if row_count >= 40:
                break
            cur = ", ".join(f"{k}={v}" for k, v in guc.current_values.items()) or "—"
            pgpro = _wiki_escape(guc.postgres_pro.splitlines()[0][:120]) if guc.postgres_pro else "—"
            lines.append(
                f"|{_wiki_escape(guc.guc)}|{_wiki_escape(guc.direction)}|"
                f"{_change_safety_status(guc.change_safety)}|"
                f"{_change_impact_status(guc.change_impact)}|"
                f"{_wiki_escape(cur)}|{pgpro}|"
            )
            row_count += 1
    if row_count == 0:
        lines.append("|—|—|—|—|—|—|")
    lines.append("")
    return lines


def build_stable_prod_confluence_stub(
    analysis: StableProdAnalysis,
    *,
    page_title: str | None = None,
) -> str:
    title = _stable_prod_page_title(analysis, page_title)
    critical = sum(1 for r in analysis.recommendations if r.problem_severity == "critical")
    high = sum(1 for r in analysis.recommendations if r.problem_severity == "high")

    lines: list[str] = [
        f"h1. {title}",
        "",
        "{info:title=О документе}",
        "Сводка стабильных проблем на PROD по нескольким pg_profile отчётам. "
        "Таблицы сформированы Python (analyze_prod_stability). "
        "Критичность проблемы и безопасность/влияние изменений GUC — отдельные оси.",
        "{info}",
        "",
    ]

    if critical > 0:
        lines.extend(
            [
                "{warning:title=Стабильные critical-проблемы}",
                f"Обнаружено *{critical}* рекоменований с критичностью CRITICAL, "
                f"повторяющихся в ≥ {analysis.min_stability_ratio:.0%} отчётов.",
                "{warning}",
                "",
            ]
        )

    lines.extend(
        [
            "h2. Параметры анализа",
            "",
            "||Параметр||Значение||",
            f"|Дата формирования|{datetime.now().strftime('%Y-%m-%d %H:%M')}|",
            f"|Отчётов PROD|{len(analysis.reports)}|",
            f"|Min stability|{analysis.min_stability_ratio:.0%}|",
            f"|Стабильных findings|{len(analysis.stable_findings)}|",
            f"|Tuning-рекомендаций|{len(analysis.recommendations)}|",
            f"|Critical / High|{critical} / {high}|",
            f"|Нестабильных|{len(analysis.ephemeral_findings)}|",
            "",
            "h2. PROD-отчёты",
            "",
            "||Метка||Файл||Интервал||Длительность||Findings||",
        ]
    )
    for snap in analysis.reports:
        props = snap.ctx.properties
        interval = (
            f"{props.get('report_start1', '?')} .. {props.get('report_end1', '?')}"
        )
        lines.append(
            f"|{_wiki_escape(snap.label)}|{_wiki_escape(snap.path.name)}|"
            f"{_wiki_escape(interval)}|{snap.ctx.interval_hours:.1f} ч|{len(snap.findings)}|"
        )
    lines.append("")
    lines.extend(_recommendations_summary_table(analysis.recommendations))
    lines.extend(_guc_details_table(analysis.recommendations))

    if analysis.ephemeral_findings:
        lines.extend(["h2. Нестабильные находки (справочно)", ""])
        lines.append("||Severity||Finding||Reports||")
        for ef in analysis.ephemeral_findings[:15]:
            reports = _wiki_escape(f"{ef.occurrence_count}/{ef.total_reports}")
            labels = _wiki_escape(", ".join(ef.report_labels))
            lines.append(
                f"|{_status(ef.max_severity)}|{_wiki_escape(ef.rule_id)}|{reports} ({labels})|"
            )
        lines.append("")

    lines.extend(
        [
            "----",
            "",
            "_Ниже — вывод ИИ (Confluence Wiki Markup). Вставьте ответ gigacli или merge_confluence.py._",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_stable_prod_confluence_prompt(brief: str) -> str:
    template = (
        STABLE_PROD_CONFLUENCE_PROMPT.read_text(encoding="utf-8")
        if STABLE_PROD_CONFLUENCE_PROMPT.exists()
        else ""
    )
    return (
        f"{template}\n\n---\n\n"
        "# DATA FOR ANALYSIS\n\n"
        f"{brief}\n\n---\n\n"
        "Начни ответ сразу с `h2. Краткое резюме` (без h1 и без пояснений)."
    )


def write_stable_prod_confluence_outputs(
    analysis: StableProdAnalysis,
    output_dir: Path,
    *,
    page_title: str | None = None,
) -> None:
    from pgprofile_stable_prod import build_stable_prod_brief

    output_dir.mkdir(parents=True, exist_ok=True)
    brief = build_stable_prod_brief(analysis)
    (output_dir / "stable_prod_brief.md").write_text(brief, encoding="utf-8")
    (output_dir / "stable_prod_confluence_stub.wiki").write_text(
        build_stable_prod_confluence_stub(analysis, page_title=page_title),
        encoding="utf-8",
    )
    (output_dir / "stable_prod_confluence_prompt.txt").write_text(
        build_stable_prod_confluence_prompt(brief),
        encoding="utf-8",
    )


def _symptom_page_title(inv: SymptomInvestigation, page_title: str | None) -> str:
    if page_title:
        return page_title
    return f"pg_profile: {inv.symptom_title}"


def build_symptom_confluence_stub(
    inv: SymptomInvestigation,
    *,
    page_title: str | None = None,
) -> str:
    title = _symptom_page_title(inv, page_title)
    confirmed = sum(1 for c in inv.causes if c.status == CauseStatus.CONFIRMED)
    suspected = sum(1 for c in inv.causes if c.status == CauseStatus.SUSPECTED)

    lines: list[str] = [
        f"h1. {title}",
        "",
        "{info:title=О документе}",
        "Расследование симптома по pg_profile. Таблицы гипотез и план действий сформированы Python. "
        "Статус confirmed = прямые данные в отчёте; suspected = косвенные признаки; possible = типичная причина.",
        "{info}",
        "",
    ]

    if confirmed > 0:
        lines.extend(
            [
                "{warning:title=Подтверждённые гипотезы}",
                f"В отчёте найдены прямые признаки для *{confirmed}* гипотез(ы).",
                "{warning}",
                "",
            ]
        )

    lines.extend(
        [
            "h2. Параметры расследования",
            "",
            "||Параметр||Значение||",
            f"|Дата формирования|{datetime.now().strftime('%Y-%m-%d %H:%M')}|",
            f"|Симптом|{_wiki_escape(inv.symptom_title)} ({inv.symptom})|",
            f"|Отчётов|{len(inv.reports)}|",
            f"|Confirmed / Suspected|{confirmed} / {suspected}|",
        ]
    )
    if inv.query_target:
        lines.append(f"|Целевой запрос|{_wiki_escape(inv.query_target.describe())}|")
    lines.append("")

    lines.extend(["h2. Отчёты pg_profile", "", "||Метка||Файл||Интервал||"])
    for snap in inv.reports:
        props = snap.ctx.properties
        interval = f"{props.get('report_start1', '?')} .. {props.get('report_end1', '?')}"
        lines.append(
            f"|{_wiki_escape(snap.label)}|{_wiki_escape(snap.path.name)}|{_wiki_escape(interval)}|"
        )
    lines.append("")

    if inv.query_matches:
        lines.extend(["h2. Найденные запросы (slow_query)", ""])
        lines.append("||hexqueryid||DB||Preview||")
        for m in inv.query_matches[:5]:
            preview = _wiki_escape((m.get("preview") or "")[:100])
            lines.append(
                f"|{_wiki_escape(str(m.get('hexqueryid')))}|"
                f"{_wiki_escape(str(m.get('dbname', '?')))}|{preview}|"
            )
        lines.append("")

    lines.extend(
        [
            "h2. Гипотезы (возможные причины)",
            "",
            "||Статус||Причина||ID||Отчёты||Evidence (кратко)||",
        ]
    )
    for cause in inv.causes:
        ev = _wiki_escape("; ".join(cause.evidence[:2])) if cause.evidence else "—"
        reports = _wiki_escape(", ".join(cause.reports_matched)) if cause.reports_matched else "—"
        lines.append(
            f"|{_cause_status(cause.status.value)}|{_wiki_escape(cause.title)}|"
            f"{_wiki_escape(cause.cause_id)}|{reports}|{ev}|"
        )
    lines.append("")

    lines.extend(["h2. План действий (verify)", "", "||#||Действие||"])
    for idx, step in enumerate(inv.action_plan[:25], 1):
        lines.append(f"|{idx}|{_wiki_escape(step)}|")
    if not inv.action_plan:
        lines.append("|—|_Нет шагов — все гипотезы possible без evidence._|")
    lines.append("")

    lines.extend(["h2. Детали: confirm / refute", ""])
    for cause in inv.causes[:12]:
        if cause.status == CauseStatus.POSSIBLE and not cause.evidence:
            continue
        lines.append(f"h3. {_wiki_escape(cause.title)} ({cause.cause_id})")
        lines.append("")
        if cause.evidence:
            lines.append("*Evidence:*")
            for ev in cause.evidence[:4]:
                lines.append(f"* {_wiki_escape(ev)}")
            lines.append("")
        if cause.confirm_actions:
            lines.append("*Подтвердить:*")
            for action in cause.confirm_actions[:3]:
                lines.append(f"# {_wiki_escape(action)}")
            lines.append("")
        if cause.refute_actions:
            lines.append("*Опровергнуть:*")
            for action in cause.refute_actions[:2]:
                lines.append(f"# {_wiki_escape(action)}")
            lines.append("")

    lines.extend(
        [
            "----",
            "",
            "_Ниже — вывод ИИ (Confluence Wiki Markup). Вставьте ответ gigacli или merge_confluence.py._",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_symptom_confluence_prompt(brief: str) -> str:
    template = (
        SYMPTOM_CONFLUENCE_PROMPT.read_text(encoding="utf-8")
        if SYMPTOM_CONFLUENCE_PROMPT.exists()
        else ""
    )
    return (
        f"{template}\n\n---\n\n"
        "# DATA FOR ANALYSIS\n\n"
        f"{brief}\n\n---\n\n"
        "Начни ответ сразу с `h2. Краткое резюме` (без h1 и без пояснений)."
    )


def write_symptom_confluence_outputs(
    inv: SymptomInvestigation,
    output_dir: Path,
    *,
    page_title: str | None = None,
) -> None:
    from pgprofile_symptoms import build_symptom_brief

    output_dir.mkdir(parents=True, exist_ok=True)
    brief = build_symptom_brief(inv)
    (output_dir / "symptom_brief.md").write_text(brief, encoding="utf-8")
    (output_dir / "symptom_confluence_stub.wiki").write_text(
        build_symptom_confluence_stub(inv, page_title=page_title),
        encoding="utf-8",
    )
    (output_dir / "symptom_confluence_prompt.txt").write_text(
        build_symptom_confluence_prompt(brief),
        encoding="utf-8",
    )
