"""NT vs PROD validation: settings gate + performance metrics comparison."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from compare_settings import DiffRow, DiffStatus, diff_settings
from pgprofile_compare import (
    ALL_SECTIONS,
    MetricDiff,
    QueryCompareGroup,
    RunSnapshot,
    SECTION_LABELS,
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


@dataclass
class NtProdValidation:
    run_nt: RunSnapshot
    run_prod: RunSnapshot
    settings: SettingsSummary
    metric_diffs: list[MetricDiff]
    query_groups: list[QueryCompareGroup]
    total_compared: int
    significant_count: int
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
    issues = differ + only_nt + only_prod
    return SettingsSummary(
        valid=issues == 0,
        differ=differ,
        only_nt=only_nt,
        only_prod=only_prod,
        same=same,
        rows=rows,
    )


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
    return NtProdValidation(
        run_nt=run_nt,
        run_prod=run_prod,
        settings=settings,
        metric_diffs=metric_diffs,
        query_groups=query_groups,
        total_compared=result.total_compared,
        significant_count=len(metric_diffs) + sum(len(g.fields) for g in query_groups),
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
    if s.valid:
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

    total = s.differ + s.only_nt + s.only_prod
    msg = (
        f"  ПРОГОН НЕВАЛИДЕН: Defined settings НТ и ПРОМ расходятся "
        f"({total} отличий: {s.differ} differ, {s.only_nt} only NT, {s.only_prod} only PROD)"
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
    if s.valid:
        return

    issues = [r for r in s.rows if r.status is not DiffStatus.SAME]
    print("== Расхождения настроек (Defined settings) ==", file=out)
    differ = [r for r in issues if r.status is DiffStatus.DIFFER]
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

    only_nt = [r for r in issues if r.status is DiffStatus.ONLY_NT]
    if only_nt:
        print(f"Only in NT ({len(only_nt)}):", file=out)
        for row in only_nt[:10]:
            print(f"  {row.name} = {row.nt_value}", file=out)
        if len(only_nt) > 10:
            print(f"  ... и ещё {len(only_nt) - 10}", file=out)
        print(file=out)

    only_prod = [r for r in issues if r.status is DiffStatus.ONLY_PROD]
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
) -> None:
    if not groups:
        return

    print(f"== SQL: сравнение по параметрам ({len(groups)} запросов) ==", file=out)
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
        print("  Настройки: OK (Defined settings совпадают)", file=stream)
    else:
        print(
            _red("  Настройки: НЕВАЛИДНО — есть расхождения", color=use_color),
            file=stream,
        )
    sig = validation.significant_count
    if sig == 0:
        print("  Метрики: значимых расхождений нет", file=stream)
    else:
        print(
            f"  Метрики: {sig} значимых расхождений (>= {validation.min_change_pct:g}%)",
            file=stream,
        )
    if validation.settings.valid and sig == 0:
        print(
            _green("  Вывод: НТ отражает ПРОМ — можно экспериментировать на НТ стенде", color=use_color),
            file=stream,
        )
    elif validation.settings.valid:
        print(
            "  Вывод: настройки совпадают, но метрики различаются — проверьте нагрузку и интервалы",
            file=stream,
        )
    else:
        print(
            _red("  Вывод: сначала выровняйте настройки, затем повторите сравнение", color=use_color),
            file=stream,
        )
    print(file=stream)

    _print_settings_compact(validation, verbose=verbose, out=stream)

    grouped: dict[str, list[MetricDiff]] = {key: [] for key in SECTION_LABELS}
    for diff in validation.metric_diffs:
        grouped.setdefault(diff.section, []).append(diff)

    priority_sections = [
        ("wal", "WAL / скорость генерации"),
        ("dml", "DML операции"),
        ("cluster", "Checkpoints / cluster"),
        ("tables", "DML по таблицам"),
        ("sessions", "Sessions"),
        ("cache", "Cache и I/O"),
    ]

    for section, title in priority_sections:
        if section not in validation.sections:
            continue
        items = grouped.get(section, [])
        if not items:
            continue
        if section == "wal":
            wal_first = [d for d in items if d.key in WAL_HIGHLIGHT]
            wal_rest = [d for d in items if d.key not in WAL_HIGHLIGHT]
            items = wal_first + wal_rest
        print(f"== {title} ({len(items)} rows) ==", file=stream)
        _print_metric_table(items, run_nt, run_prod, show_per_hour=show_per_hour, out=stream)
        print(file=stream)

    if "queries" in validation.sections:
        _print_query_groups(
            validation.query_groups,
            run_nt,
            run_prod,
            show_per_hour=show_per_hour,
            out=stream,
        )

    print(
        f"Summary: settings {'OK' if validation.settings.valid else 'INVALID'}, "
        f"{validation.total_compared} metrics compared, "
        f"{validation.significant_count} significant differences "
        f"(>= {validation.min_change_pct:g}%)",
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
        f"settings_valid: {str(s.valid).lower()}",
        f"settings_issues: {s.differ} differ, {s.only_nt} only NT, {s.only_prod} only PROD",
        f"significant_metric_diffs: {validation.significant_count} (threshold >= {validation.min_change_pct:g}%)",
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

    if not s.valid:
        lines.append("## Settings issues (Defined settings)")
        issues = [r for r in s.rows if r.status is not DiffStatus.SAME]
        for row in issues[:max_settings]:
            if row.status is DiffStatus.DIFFER:
                lines.append(f"- DIFFER `{row.name}`: NT={row.nt_value!r} PROD={row.prod_value!r}")
            elif row.status is DiffStatus.ONLY_NT:
                lines.append(f"- ONLY_NT `{row.name}`: {row.nt_value!r}")
            else:
                lines.append(f"- ONLY_PROD `{row.name}`: {row.prod_value!r}")
        if len(issues) > max_settings:
            lines.append(f"- ... and {len(issues) - max_settings} more")
        lines.append("")

    grouped: dict[str, list[MetricDiff]] = {}
    for diff in validation.metric_diffs:
        grouped.setdefault(diff.section, []).append(diff)

    section_titles = {
        "wal": "WAL metrics",
        "dml": "DML operations",
        "cluster": "Cluster / checkpoints",
        "tables": "Tables",
        "sessions": "Sessions",
        "cache": "Cache / I/O",
    }
    show_ph = True
    for section, title in section_titles.items():
        items = grouped.get(section, [])
        if not items:
            continue
        lines.append(f"## {title}")
        for diff in items[:20]:
            nt_v = format_value_cell(diff, "a", show_per_hour=show_ph)
            prod_v = format_value_cell(diff, "b", show_per_hour=show_ph)
            lines.append(f"- `{diff.key}`: NT={nt_v} PROD={prod_v} delta={format_delta(diff)}")
            if diff.key == "wal_bytes":
                ph_a = diff.per_hour_a
                ph_b = diff.per_hour_b
                if ph_a is not None or ph_b is not None:
                    nt_mb = f"{ph_a / 1_048_576:.2f} MB/h" if ph_a else "-"
                    prod_mb = f"{ph_b / 1_048_576:.2f} MB/h" if ph_b else "-"
                    lines.append(f"  wal_throughput: NT={nt_mb} PROD={prod_mb}")
        lines.append("")

    if validation.query_groups:
        lines.append("## SQL by parameter")
        for group in validation.query_groups[:max_queries]:
            lines.append(f"### {group.label} (id={group.query_key[:20]}...)")
            if group.preview:
                lines.append(f"SQL: {group.preview}")
            for diff in group.fields:
                nt_v = format_value_cell(diff, "a", show_per_hour=show_ph)
                prod_v = format_value_cell(diff, "b", show_per_hour=show_ph)
                lines.append(f"- {diff.key}: NT={nt_v} PROD={prod_v} delta={format_delta(diff)}")
            lines.append("")

    if s.valid and validation.significant_count == 0:
        lines.append("## Verdict")
        lines.append("NT reflects PROD — safe to experiment on NT stand.")
    elif s.valid:
        lines.append("## Verdict")
        lines.append("Settings match but metrics differ — review workload and intervals.")
    else:
        lines.append("## Verdict")
        lines.append("INVALID — align Defined settings before trusting metrics.")

    return "\n".join(lines).rstrip() + "\n"


def nt_prod_validation_to_dict(validation: NtProdValidation) -> dict[str, Any]:
    from pgprofile_findings import _json_safe

    settings_issues = [
        {
            "name": r.name,
            "status": r.status.value,
            "nt_value": r.nt_value,
            "prod_value": r.prod_value,
        }
        for r in validation.settings.rows
        if r.status is not DiffStatus.SAME
    ]

    return {
        "type": "nt_prod_validation",
        "settings_valid": validation.settings.valid,
        "settings_summary": {
            "differ": validation.settings.differ,
            "only_nt": validation.settings.only_nt,
            "only_prod": validation.settings.only_prod,
            "same": validation.settings.same,
        },
        "settings_issues": settings_issues,
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
        "metric_diffs": [_json_safe(d) for d in validation.metric_diffs],
        "query_comparisons": [
            {
                "query_key": g.query_key,
                "label": g.label,
                "preview": g.preview,
                "fields": [_json_safe(f) for f in g.fields],
            }
            for g in validation.query_groups
        ],
        "summary": {
            "significant_count": validation.significant_count,
            "total_compared": validation.total_compared,
            "min_change_pct": validation.min_change_pct,
            "can_trust_metrics": validation.settings.valid,
        },
    }
