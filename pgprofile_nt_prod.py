"""NT vs PROD validation: settings gate + performance metrics comparison."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from compare_settings import DiffRow, DiffStatus, diff_settings
from pgprofile_classify import (
    MetricIssueLevel,
    classify_metric_diff,
    split_settings_rows,
)
from pgprofile_compare import (
    ALL_SECTIONS,
    MetricDiff,
    QueryCompareGroup,
    RunSnapshot,
    compare_queries_detailed,
    compare_runs,
    format_delta,
    format_number,
    format_value_cell,
    interval_diff_hours,
    load_run,
    _is_significant,
)
from pgprofile_parser import load_settings

NT_PROD_SECTIONS = ("wal", "dml", "cluster", "queries", "sessions", "cache", "tables")

WAL_HIGHLIGHT = ("wal_bytes", "wal_records", "wal_buffers_full", "wal_write", "wal_sync")


@dataclass
class SettingsSummary:
    valid: bool
    differ: int
    only_nt: int
    only_prod: int
    same: int
    rows: list[DiffRow]
    critical_count: int = 0
    informational_count: int = 0


@dataclass
class NtProdValidation:
    run_nt: RunSnapshot
    run_prod: RunSnapshot
    settings: SettingsSummary
    metric_diffs: list[MetricDiff]
    metric_diffs_info: list[MetricDiff]
    metric_diffs_warning: list[MetricDiff]
    query_groups: list[QueryCompareGroup]
    query_groups_info: list[QueryCompareGroup]
    query_groups_warning: list[QueryCompareGroup]
    total_compared: int
    significant_count: int
    info_count: int
    warning_count: int
    min_change_pct: float
    sections: list[str] = field(default_factory=list)


def _use_color(stream: TextIO) -> bool:
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    return not (sys.platform == "win32" and "ANSICON" not in __import__("os").environ)


def _red(text: str, *, color: bool) -> str:
    if color:
        return f"\033[31m\033[1m{text}\033[0m"
    return f"!!! {text} !!!"


def _yellow(text: str, *, color: bool) -> str:
    if color:
        return f"\033[33m{text}\033[0m"
    return text


def _green(text: str, *, color: bool) -> str:
    if color:
        return f"\033[32m{text}\033[0m"
    return text


def summarize_settings(nt_settings: dict[str, str], prod_settings: dict[str, str]) -> SettingsSummary:
    rows = diff_settings(nt_settings, prod_settings)
    differ = sum(1 for r in rows if r.status is DiffStatus.DIFFER)
    only_nt = sum(1 for r in rows if r.status is DiffStatus.ONLY_NT)
    only_prod = sum(1 for r in rows if r.status is DiffStatus.ONLY_PROD)
    same = sum(1 for r in rows if r.status is DiffStatus.SAME)
    critical, informational = split_settings_rows(rows)
    return SettingsSummary(
        valid=len(critical) == 0,
        differ=differ,
        only_nt=only_nt,
        only_prod=only_prod,
        same=same,
        rows=rows,
        critical_count=len(critical),
        informational_count=len(informational),
    )


def _split_query_groups(
    groups: list[QueryCompareGroup],
) -> tuple[list[QueryCompareGroup], list[QueryCompareGroup]]:
    info_groups: list[QueryCompareGroup] = []
    warn_groups: list[QueryCompareGroup] = []
    for group in groups:
        info_fields = [
            f for f in group.fields
            if classify_metric_diff(f) is MetricIssueLevel.INFORMATIONAL
        ]
        warn_fields = [
            f for f in group.fields
            if classify_metric_diff(f) is MetricIssueLevel.WARNING
        ]
        if info_fields:
            info_groups.append(
                QueryCompareGroup(
                    query_key=group.query_key,
                    label=group.label,
                    preview=group.preview,
                    fields=info_fields,
                    score=group.score,
                )
            )
        if warn_fields:
            warn_groups.append(
                QueryCompareGroup(
                    query_key=group.query_key,
                    label=group.label,
                    preview=group.preview,
                    fields=warn_fields,
                    score=group.score,
                )
            )
    return info_groups, warn_groups


def validate_nt_prod(
    nt_path: Path,
    prod_path: Path,
    *,
    sections: list[str] | None = None,
    min_change_pct: float = 5.0,
    top_n: int = 15,
    verbose: bool = False,
    nt_label: str = "NT",
    prod_label: str = "PROD",
) -> NtProdValidation:
    selected = sections or list(NT_PROD_SECTIONS)
    unknown = [s for s in selected if s not in ALL_SECTIONS]
    if unknown:
        raise ValueError(f"unknown sections: {', '.join(unknown)}")

    run_nt = load_run(nt_path, nt_label)
    run_prod = load_run(prod_path, prod_label)

    nt_settings = load_settings(nt_path, defined_only=True)
    prod_settings = load_settings(prod_path, defined_only=True)
    settings = summarize_settings(nt_settings, prod_settings)

    compare_sections = [s for s in selected if s != "queries"]
    result = compare_runs(
        run_nt,
        run_prod,
        sections=compare_sections or None,
        min_change_pct=min_change_pct,
        top_n=top_n,
        verbose=verbose,
    )

    query_groups: list[QueryCompareGroup] = []
    if "queries" in selected:
        query_groups = compare_queries_detailed(
            run_nt,
            run_prod,
            top_n=top_n,
            min_change_pct=min_change_pct,
            verbose=verbose,
        )

    metric_diffs = [d for d in result.diffs if _is_significant(d, min_change_pct)]
    metric_diffs_info = [
        d for d in metric_diffs if classify_metric_diff(d) is MetricIssueLevel.INFORMATIONAL
    ]
    metric_diffs_warning = [
        d for d in metric_diffs if classify_metric_diff(d) is MetricIssueLevel.WARNING
    ]
    query_groups_info, query_groups_warning = _split_query_groups(query_groups)
    info_count = len(metric_diffs_info) + sum(len(g.fields) for g in query_groups_info)
    warning_count = len(metric_diffs_warning) + sum(len(g.fields) for g in query_groups_warning)

    return NtProdValidation(
        run_nt=run_nt,
        run_prod=run_prod,
        settings=settings,
        metric_diffs=metric_diffs,
        metric_diffs_info=metric_diffs_info,
        metric_diffs_warning=metric_diffs_warning,
        query_groups=query_groups,
        query_groups_info=query_groups_info,
        query_groups_warning=query_groups_warning,
        total_compared=result.total_compared,
        significant_count=len(metric_diffs) + sum(len(g.fields) for g in query_groups),
        info_count=info_count,
        warning_count=warning_count,
        min_change_pct=min_change_pct,
        sections=selected,
    )


def _print_metric_table(
    items: list[MetricDiff],
    run_nt: RunSnapshot,
    run_prod: RunSnapshot,
    *,
    show_per_hour: bool,
    out: TextIO,
) -> None:
    if not items:
        return

    col_nt = run_nt.run_id
    col_prod = run_prod.run_id
    key_width = max(len(d.key) for d in items)
    key_width = max(key_width, len("Metric"))
    col_width = max(
        14,
        max(len(format_value_cell(d, "a", show_per_hour=show_per_hour)) for d in items),
        max(len(format_value_cell(d, "b", show_per_hour=show_per_hour)) for d in items),
        len(col_nt),
        len(col_prod),
    )

    header = (
        f"{'Metric'.ljust(key_width)} | "
        f"{col_nt.ljust(col_width)} | "
        f"{col_prod.ljust(col_width)} | Delta"
    )
    sep = (
        f"{'-' * key_width}-+-"
        f"{'-' * col_width}-+-"
        f"{'-' * col_width}-+-------"
    )
    print(header, file=out)
    print(sep, file=out)
    for diff in items:
        line = (
            f"{diff.key.ljust(key_width)} | "
            f"{format_value_cell(diff, 'a', show_per_hour=show_per_hour).ljust(col_width)} | "
            f"{format_value_cell(diff, 'b', show_per_hour=show_per_hour).ljust(col_width)} | "
            f"{format_delta(diff)}"
        )
        print(line, file=out)
        if diff.key == "wal_bytes" and show_per_hour:
            nt_h = diff.per_hour_a
            prod_h = diff.per_hour_b
            if nt_h is not None or prod_h is not None:
                nt_mb = f"{nt_h / 1_048_576:.2f} MB/h" if nt_h is not None else "-"
                prod_mb = f"{prod_h / 1_048_576:.2f} MB/h" if prod_h is not None else "-"
                print(
                    f"{'  wal throughput'.ljust(key_width)} | "
                    f"{nt_mb.ljust(col_width)} | "
                    f"{prod_mb.ljust(col_width)} |",
                    file=out,
                )


def _print_settings_banner(validation: NtProdValidation, *, color: bool, out: TextIO) -> None:
    s = validation.settings
    if s.valid and s.informational_count == 0:
        print(_green("=" * 78, color=color), file=out)
        print(
            _green(
                "  Настройки НТ и ПРОМ совпадают (Defined settings) — метрики можно сравнивать",
                color=color,
            ),
            file=out,
        )
        print(_green("=" * 78, color=color), file=out)
        print(file=out)
        return

    if s.valid and s.informational_count > 0:
        print(_yellow("=" * 78, color=color), file=out)
        print(
            _yellow(
                f"  Конфигурация GUC совпадает. Справочно: {s.informational_count} отличий "
                f"runtime-метаданных (pg_conf_load_time, postmaster_start, in_hot_standby и т.п.)",
                color=color,
            ),
            file=out,
        )
        print(
            _yellow(
                "  Сравнение метрик допустимо — это не расхождение конфигурации.",
                color=color,
            ),
            file=out,
        )
        print(_yellow("=" * 78, color=color), file=out)
        print(file=out)
        return

    total = s.critical_count
    msg = (
        f"  ПРОГОН НЕВАЛИДЕН: Defined settings GUC расходятся ({total} критичных отличий)"
    )
    print(_red("=" * 78, color=color), file=out)
    print(_red(msg, color=color), file=out)
    print(
        _red(
            "  Сравнение метрик может вводить в заблуждение. Сначала выровняйте настройки.",
            color=color,
        ),
        file=out,
    )
    print(_red("=" * 78, color=color), file=out)
    print(file=out)


def _print_settings_compact(validation: NtProdValidation, *, verbose: bool, out: TextIO) -> None:
    s = validation.settings
    critical, informational = split_settings_rows(s.rows)

    if critical:
        print("== Критичные расхождения настроек (GUC) ==", file=out)
        _print_settings_table(critical, verbose=verbose, out=out)

    if informational:
        print("== Справочно: runtime / метаданные (не блокируют сравнение) ==", file=out)
        _print_settings_table(informational, verbose=verbose, out=out)


def _print_settings_table(issues: list[DiffRow], *, verbose: bool, out: TextIO) -> None:
    differ = [r for r in issues if r.status is DiffStatus.DIFFER]
    only_nt = [r for r in issues if r.status is DiffStatus.ONLY_NT]
    only_prod = [r for r in issues if r.status is DiffStatus.ONLY_PROD]

    if differ:
        name_w = max(len(r.name) for r in differ)
        name_w = max(name_w, 7)
        print(f"{'Setting'.ljust(name_w)} | NT | PROD", file=out)
        print(f"{'-' * name_w}-+----+-----", file=out)
        for row in differ[:20]:
            nt_v = row.nt_value or ""
            prod_v = row.prod_value or ""
            if not verbose and len(nt_v) > 50:
                nt_v = nt_v[:47] + "..."
            if not verbose and len(prod_v) > 50:
                prod_v = prod_v[:47] + "..."
            print(f"{row.name.ljust(name_w)} | {nt_v} | {prod_v}", file=out)
        if len(differ) > 20:
            print(f"  ... и ещё {len(differ) - 20} differ", file=out)
        print(file=out)

    if only_nt:
        print(f"Only in NT ({len(only_nt)}):", file=out)
        for row in only_nt[:10]:
            print(f"  {row.name} = {row.nt_value}", file=out)
        if len(only_nt) > 10:
            print(f"  ... и ещё {len(only_nt) - 10}", file=out)
        print(file=out)

    if only_prod:
        print(f"Only in PROD ({len(only_prod)}):", file=out)
        for row in only_prod[:10]:
            print(f"  {row.name} = {row.prod_value}", file=out)
        if len(only_prod) > 10:
            print(f"  ... и ещё {len(only_prod) - 10}", file=out)
        print(file=out)


def _print_query_groups(
    groups: list[QueryCompareGroup],
    run_nt: RunSnapshot,
    run_prod: RunSnapshot,
    *,
    show_per_hour: bool,
    out: TextIO,
    heading: str | None = None,
) -> None:
    if not groups:
        return

    title = heading or f"== SQL: сравнение по параметрам ({len(groups)} запросов) =="
    print(title, file=out)
    print(
        "  Сопоставление по query id. PROD может быть быстрее (отрицательный Delta %).",
        file=out,
    )
    print(file=out)

    param_labels = {
        "calls": "calls",
        "total_time": "total_time",
        "mean_exec_time": "mean_exec_time",
        "max_exec_time": "max_exec_time",
        "wal_bytes": "wal_bytes",
        "shared_blks_read": "shared_blks_read",
        "temp_blks_written": "temp_blks_written",
    }

    for idx, group in enumerate(groups, 1):
        print(f"--- [{idx}] {group.label}  (id={group.query_key[:16]}...) ---", file=out)
        if group.preview:
            print(f"  {group.preview}", file=out)

        key_w = max(len("Parameter"), max(len(param_labels.get(f.key, f.key)) for f in group.fields))
        col_nt = run_nt.run_id
        col_prod = run_prod.run_id
        col_w = 14

        print(
            f"  {'Parameter'.ljust(key_w)} | {col_nt.ljust(col_w)} | {col_prod.ljust(col_w)} | Delta",
            file=out,
        )
        for diff in group.fields:
            label = param_labels.get(diff.key, diff.key)
            print(
                f"  {label.ljust(key_w)} | "
                f"{format_value_cell(diff, 'a', show_per_hour=show_per_hour).ljust(col_w)} | "
                f"{format_value_cell(diff, 'b', show_per_hour=show_per_hour).ljust(col_w)} | "
                f"{format_delta(diff)}",
                file=out,
            )
        print(file=out)


def print_nt_prod_report(
    validation: NtProdValidation,
    *,
    verbose: bool = False,
    color: bool | None = None,
    out: TextIO | None = None,
) -> None:
    stream = out or sys.stdout
    use_color = color if color is not None else _use_color(stream)

    run_nt = validation.run_nt
    run_prod = validation.run_prod
    show_per_hour = True  # NT vs PROD: always normalize counters to /hour

    print("pg_profile NT vs PROD validation", file=stream)
    print(file=stream)

    _print_settings_banner(validation, color=use_color, out=stream)

    for label, run in (("NT", run_nt), ("PROD", run_prod)):
        props = run.ctx.properties
        server = props.get("server_name") or run.ctx.meta.get("server") or "?"
        start = props.get("report_start1") or "?"
        end = props.get("report_end1") or "?"
        hours = run.ctx.interval_hours
        print(
            f"{label} [{run.run_id}]: {run.path.name} | "
            f"{start} .. {end} ({hours:.1f} h) | server={server}",
            file=stream,
        )

    diff_h = interval_diff_hours(run_nt, run_prod)
    if diff_h > 0.01:
        print(file=stream)
        print(
            _yellow(
                f"[!] Длительность интервалов отличается на {diff_h:.1f} ч "
                f"({run_nt.ctx.interval_hours:.1f} h vs {run_prod.ctx.interval_hours:.1f} h)",
                color=use_color,
            ),
            file=stream,
        )
        print("    Счётчики показаны как абсолют + нормализация «/час»", file=stream)

    print(file=stream)
    print("== Краткая сводка ==", file=stream)
    if validation.settings.valid:
        if validation.settings.informational_count:
            print(
                "  Настройки GUC: OK (есть справочные отличия runtime-метаданных)",
                file=stream,
            )
        else:
            print("  Настройки GUC: OK", file=stream)
    else:
        print(
            _red(
                f"  Настройки GUC: НЕВАЛИДНО ({validation.settings.critical_count} критичных)",
                color=use_color,
            ),
            file=stream,
        )
    if validation.info_count:
        print(
            f"  Объём WAL/операций: {validation.info_count} отличий (справочно, ожидаемо при разной нагрузке)",
            file=stream,
        )
    if validation.warning_count:
        print(
            f"  Производительность: {validation.warning_count} предупреждений (>= {validation.min_change_pct:g}%)",
            file=stream,
        )
    elif validation.info_count == 0:
        print("  Метрики: значимых расхождений нет", file=stream)

    if validation.settings.valid and validation.warning_count == 0:
        print(
            _green(
                "  Вывод: конфигурация совпадает — можно сравнивать и экспериментировать на НТ",
                color=use_color,
            ),
            file=stream,
        )
    elif validation.settings.valid:
        print(
            "  Вывод: конфигурация совпадает, но есть отличия по производительности — проверьте детали",
            file=stream,
        )
    else:
        print(
            _red("  Вывод: сначала выровняйте GUC, затем повторите сравнение", color=use_color),
            file=stream,
        )
    print(file=stream)

    _print_settings_compact(validation, verbose=verbose, out=stream)

    grouped_info: dict[str, list[MetricDiff]] = {}
    for diff in validation.metric_diffs_info:
        grouped_info.setdefault(diff.section, []).append(diff)
    grouped_warn: dict[str, list[MetricDiff]] = {}
    for diff in validation.metric_diffs_warning:
        grouped_warn.setdefault(diff.section, []).append(diff)

    info_section_titles = [
        ("wal", "Справочно: WAL / объём записи"),
        ("dml", "Справочно: DML операции"),
        ("cluster", "Справочно: checkpoints (объём)"),
        ("tables", "Справочно: DML по таблицам"),
        ("sessions", "Справочно: sessions (объём)"),
        ("cache", "Справочно: cache I/O (объём)"),
    ]
    for section, title in info_section_titles:
        if section not in validation.sections:
            continue
        items = grouped_info.get(section, [])
        if not items:
            continue
        if section == "wal":
            wal_first = [d for d in items if d.key in WAL_HIGHLIGHT]
            wal_rest = [d for d in items if d.key not in WAL_HIGHLIGHT]
            items = wal_first + wal_rest
        print(f"== {title} ({len(items)} rows) ==", file=stream)
        _print_metric_table(items, run_nt, run_prod, show_per_hour=show_per_hour, out=stream)
        print(file=stream)

    if "queries" in validation.sections and validation.query_groups_info:
        _print_query_groups(
            validation.query_groups_info,
            run_nt,
            run_prod,
            show_per_hour=show_per_hour,
            out=stream,
            heading=f"== Справочно: SQL — объём и calls ({len(validation.query_groups_info)} запросов) ==",
        )

    warn_section_titles = [
        ("wal", "WAL — предупреждения"),
        ("cluster", "Checkpoints / cluster — предупреждения"),
        ("sessions", "Sessions — предупреждения"),
        ("cache", "Cache — предупреждения"),
    ]
    for section, title in warn_section_titles:
        if section not in validation.sections:
            continue
        items = grouped_warn.get(section, [])
        if not items:
            continue
        print(f"== {title} ({len(items)} rows) ==", file=stream)
        _print_metric_table(items, run_nt, run_prod, show_per_hour=show_per_hour, out=stream)
        print(file=stream)

    if "queries" in validation.sections and validation.query_groups_warning:
        _print_query_groups(
            validation.query_groups_warning,
            run_nt,
            run_prod,
            show_per_hour=show_per_hour,
            out=stream,
            heading=f"== SQL — производительность (mean/max time) ({len(validation.query_groups_warning)} запросов) ==",
        )

    print(
        f"Summary: settings GUC {'OK' if validation.settings.valid else 'INVALID'}, "
        f"{validation.info_count} informational (volume/ops), "
        f"{validation.warning_count} warnings, "
        f"threshold >= {validation.min_change_pct:g}%",
        file=stream,
    )


def build_nt_prod_brief(validation: NtProdValidation, *, max_settings: int = 15, max_queries: int = 10) -> str:
    """Text brief for LLM / Confluence prompt (NT vs PROD validation)."""
    run_nt = validation.run_nt
    run_prod = validation.run_prod
    s = validation.settings
    lines: list[str] = [
        "# NT vs PROD Validation Brief",
        "",
        f"settings_guc_valid: {str(s.valid).lower()}",
        f"settings_critical_count: {s.critical_count}",
        f"settings_informational_count: {s.informational_count}",
        f"volume_ops_diffs: {validation.info_count} (informational — WAL/DML volume, expected with different load)",
        f"performance_warnings: {validation.warning_count} (threshold >= {validation.min_change_pct:g}%)",
        "",
    ]

    for label, run in (("NT", run_nt), ("PROD", run_prod)):
        props = run.ctx.properties
        lines.append(f"- {label}: {run.path.name}")
        lines.append(f"  server: {props.get('server_name') or '?'}")
        lines.append(
            f"  interval: {props.get('report_start1')} .. {props.get('report_end1')} "
            f"({run.ctx.interval_hours:.1f} h)"
        )

    diff_h = interval_diff_hours(run_nt, run_prod)
    if diff_h > 0.01:
        lines.append(f"- interval_mismatch: {diff_h:.1f} h — compare per-hour values")
    lines.append("")

    critical, informational = split_settings_rows(s.rows)
    if critical:
        lines.append("## Critical GUC differences")
        for row in critical[:max_settings]:
            if row.status is DiffStatus.DIFFER:
                lines.append(f"- DIFFER `{row.name}`: NT={row.nt_value!r} PROD={row.prod_value!r}")
            elif row.status is DiffStatus.ONLY_NT:
                lines.append(f"- ONLY_NT `{row.name}`: {row.nt_value!r}")
            else:
                lines.append(f"- ONLY_PROD `{row.name}`: {row.prod_value!r}")
        if len(critical) > max_settings:
            lines.append(f"- ... and {len(critical) - max_settings} more critical")
        lines.append("")

    if informational:
        lines.append("## Informational settings (runtime metadata — not blocking)")
        for row in informational[:max_settings]:
            if row.status is DiffStatus.DIFFER:
                lines.append(f"- DIFFER `{row.name}`: NT={row.nt_value!r} PROD={row.prod_value!r}")
            elif row.status is DiffStatus.ONLY_NT:
                lines.append(f"- ONLY_NT `{row.name}`: {row.nt_value!r}")
            else:
                lines.append(f"- ONLY_PROD `{row.name}`: {row.prod_value!r}")
        lines.append("")

    show_ph = True
    if validation.metric_diffs_info:
        lines.append("## Volume / operations (informational)")
        for diff in validation.metric_diffs_info[:30]:
            nt_v = format_value_cell(diff, "a", show_per_hour=show_ph)
            prod_v = format_value_cell(diff, "b", show_per_hour=show_ph)
            lines.append(f"- `{diff.key}`: NT={nt_v} PROD={prod_v} delta={format_delta(diff)}")
        lines.append("")

    if validation.metric_diffs_warning:
        lines.append("## Performance warnings")
        for diff in validation.metric_diffs_warning[:20]:
            nt_v = format_value_cell(diff, "a", show_per_hour=show_ph)
            prod_v = format_value_cell(diff, "b", show_per_hour=show_ph)
            lines.append(f"- `{diff.key}`: NT={nt_v} PROD={prod_v} delta={format_delta(diff)}")
        lines.append("")

    if validation.query_groups_info:
        lines.append("## SQL volume (informational)")
        for group in validation.query_groups_info[:max_queries]:
            lines.append(f"### {group.label}")
            if group.preview:
                lines.append(f"SQL: {group.preview}")
            for diff in group.fields:
                nt_v = format_value_cell(diff, "a", show_per_hour=show_ph)
                prod_v = format_value_cell(diff, "b", show_per_hour=show_ph)
                lines.append(f"- {diff.key}: NT={nt_v} PROD={prod_v} delta={format_delta(diff)}")
            lines.append("")

    if validation.query_groups_warning:
        lines.append("## SQL performance warnings")
        for group in validation.query_groups_warning[:max_queries]:
            lines.append(f"### {group.label}")
            for diff in group.fields:
                nt_v = format_value_cell(diff, "a", show_per_hour=show_ph)
                prod_v = format_value_cell(diff, "b", show_per_hour=show_ph)
                lines.append(f"- {diff.key}: NT={nt_v} PROD={prod_v} delta={format_delta(diff)}")
            lines.append("")

    if s.valid and validation.warning_count == 0:
        lines.append("## Verdict")
        lines.append("GUC configuration matches — safe to compare and experiment on NT stand.")
    elif s.valid:
        lines.append("## Verdict")
        lines.append("GUC matches; review performance warnings. Volume/WAL differences are informational.")
    else:
        lines.append("## Verdict")
        lines.append("INVALID — align GUC Defined settings before trusting metrics.")

    return "\n".join(lines).rstrip() + "\n"


def nt_prod_validation_to_dict(validation: NtProdValidation) -> dict[str, Any]:
    from pgprofile_findings import _json_safe

    critical, informational = split_settings_rows(validation.settings.rows)

    return {
        "type": "nt_prod_validation",
        "settings_valid": validation.settings.valid,
        "settings_summary": {
            "differ": validation.settings.differ,
            "only_nt": validation.settings.only_nt,
            "only_prod": validation.settings.only_prod,
            "same": validation.settings.same,
            "critical_count": validation.settings.critical_count,
            "informational_count": validation.settings.informational_count,
        },
        "settings_issues_critical": [
            {
                "name": r.name,
                "status": r.status.value,
                "nt_value": r.nt_value,
                "prod_value": r.prod_value,
            }
            for r in critical
        ],
        "settings_issues_informational": [
            {
                "name": r.name,
                "status": r.status.value,
                "nt_value": r.nt_value,
                "prod_value": r.prod_value,
            }
            for r in informational
        ],
        "run_nt": {
            "run_id": validation.run_nt.run_id,
            "path": str(validation.run_nt.path),
            "filename": validation.run_nt.path.name,
            "interval_hours": validation.run_nt.ctx.interval_hours,
            "server": validation.run_nt.ctx.properties.get("server_name"),
        },
        "run_prod": {
            "run_id": validation.run_prod.run_id,
            "path": str(validation.run_prod.path),
            "filename": validation.run_prod.path.name,
            "interval_hours": validation.run_prod.ctx.interval_hours,
            "server": validation.run_prod.ctx.properties.get("server_name"),
        },
        "interval_diff_hours": round(
            interval_diff_hours(validation.run_nt, validation.run_prod), 2
        ),
        "metric_diffs_info": [_json_safe(d) for d in validation.metric_diffs_info],
        "metric_diffs_warning": [_json_safe(d) for d in validation.metric_diffs_warning],
        "query_comparisons_info": [
            {
                "query_key": g.query_key,
                "label": g.label,
                "preview": g.preview,
                "fields": [_json_safe(f) for f in g.fields],
            }
            for g in validation.query_groups_info
        ],
        "query_comparisons_warning": [
            {
                "query_key": g.query_key,
                "label": g.label,
                "preview": g.preview,
                "fields": [_json_safe(f) for f in g.fields],
            }
            for g in validation.query_groups_warning
        ],
        "summary": {
            "significant_count": validation.significant_count,
            "info_count": validation.info_count,
            "warning_count": validation.warning_count,
            "total_compared": validation.total_compared,
            "min_change_pct": validation.min_change_pct,
            "can_trust_metrics": validation.settings.valid,
        },
    }
