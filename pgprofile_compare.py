"""Compare performance metrics between two pg_profile HTML reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pgprofile_health import ReportContext, load_report_data

SECTION_LABELS = {
    "cluster": "Cluster / Checkpoints",
    "wal": "WAL statistics",
    "dml": "DML by database",
    "tables": "DML by table",
    "queries": "Top SQL",
    "sessions": "Sessions",
    "cache": "Cache and disk I/O",
}

ALL_SECTIONS = tuple(SECTION_LABELS.keys())

CLUSTER_METRICS = (
    "checkpoints_req",
    "checkpoints_timed",
    "checkpoint_write_time",
    "checkpoint_sync_time",
    "buffers_checkpoint",
    "buffers_backend",
    "maxwritten_clean",
    "wal_size",
)

WAL_METRICS = (
    "wal_bytes",
    "wal_records",
    "wal_buffers_full",
    "wal_sync",
    "wal_write",
)

DML_METRICS = {
    "tup_inserted": "INSERT",
    "tup_updated": "UPDATE",
    "tup_deleted": "DELETE",
    "xact_commit": "COMMIT",
    "xact_rollback": "ROLLBACK",
    "tup_fetched": "FETCH",
}

TABLE_DML_METRICS = ("n_tup_ins", "n_tup_upd", "n_tup_del", "seq_scan", "idx_scan")

SESSION_METRICS = (
    "sessions",
    "idle_in_transaction_time",
    "active_time",
    "session_time",
    "sessions_fatal",
    "sessions_killed",
    "deadlocks",
)

CACHE_PCT_METRICS = ("blks_hit_pct",)
CACHE_RATE_METRICS = ("blks_read", "blk_read_time", "blk_write_time")

QUERY_FIELDS = (
    ("calls", "count", True),
    ("total_time", "sec", True),
    ("mean_exec_time", "ms", False),
    ("max_exec_time", "ms", False),
    ("wal_bytes", "bytes", True),
    ("shared_blks_read", "count", True),
    ("temp_blks_written", "count", True),
)


@dataclass
class RunSnapshot:
    run_id: str
    path: Path
    ctx: ReportContext


@dataclass
class MetricDiff:
    section: str
    key: str
    value_a: float | None
    value_b: float | None
    per_hour_a: float | None = None
    per_hour_b: float | None = None
    delta: float | None = None
    delta_pct: float | None = None
    unit: str = "count"
    extra: str = ""


@dataclass
class CompareResult:
    diffs: list[MetricDiff] = field(default_factory=list)
    total_compared: int = 0
    significant_count: int = 0


@dataclass
class QueryCompareGroup:
    query_key: str
    label: str
    preview: str
    fields: list[MetricDiff] = field(default_factory=list)
    score: float = 0.0


def load_run(path: Path, run_id: str) -> RunSnapshot:
    return RunSnapshot(run_id=run_id, path=path, ctx=load_report_data(path))


def per_hour(value: float | None, interval_hours: float) -> float | None:
    if value is None or interval_hours <= 0:
        return None
    return value / interval_hours


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compute_delta(
    value_a: float | None,
    value_b: float | None,
    per_hour_a: float | None = None,
    per_hour_b: float | None = None,
) -> tuple[float | None, float | None]:
    if value_a is None and value_b is None:
        return None, None
    if value_a is None:
        return value_b, None
    if value_b is None:
        return -value_a, -100.0
    delta = value_b - value_a
    if value_a == 0:
        return delta, None
    return delta, delta * 100.0 / value_a


def _is_significant(diff: MetricDiff, min_change_pct: float) -> bool:
    if diff.value_a is None and diff.value_b is None:
        return False
    if diff.value_a is None or diff.value_b is None:
        return True
    if diff.delta is not None and diff.delta == 0:
        return False
    if diff.delta_pct is None:
        return diff.delta is not None and diff.delta != 0
    return abs(diff.delta_pct) >= min_change_pct


def _make_diff(
    section: str,
    key: str,
    value_a: float | None,
    value_b: float | None,
    *,
    unit: str = "count",
    normalize: bool,
    hours_a: float,
    hours_b: float,
    extra: str = "",
) -> MetricDiff:
    ph_a = per_hour(value_a, hours_a) if normalize else None
    ph_b = per_hour(value_b, hours_b) if normalize else None
    delta, delta_pct = _compute_delta(value_a, value_b)
    return MetricDiff(
        section=section,
        key=key,
        value_a=value_a,
        value_b=value_b,
        per_hour_a=ph_a,
        per_hour_b=ph_b,
        delta=delta,
        delta_pct=delta_pct,
        unit=unit,
        extra=extra,
    )


def _dbstat_map(ctx: ReportContext) -> dict[str, dict[str, Any]]:
    return {str(row.get("dbname")): row for row in ctx.dbstat if row.get("dbname")}


def _compare_scalar_map(
    section: str,
    map_a: dict[str, float | None],
    map_b: dict[str, float | None],
    *,
    unit: str,
    normalize: bool,
    hours_a: float,
    hours_b: float,
) -> list[MetricDiff]:
    diffs: list[MetricDiff] = []
    for key in sorted(set(map_a) | set(map_b)):
        diffs.append(
            _make_diff(
                section,
                key,
                map_a.get(key),
                map_b.get(key),
                unit=unit,
                normalize=normalize,
                hours_a=hours_a,
                hours_b=hours_b,
            )
        )
    return diffs


def compare_cluster(run_a: RunSnapshot, run_b: RunSnapshot) -> list[MetricDiff]:
    map_a = {m: _to_float(run_a.ctx.cluster_stats.get(m)) for m in CLUSTER_METRICS}
    map_b = {m: _to_float(run_b.ctx.cluster_stats.get(m)) for m in CLUSTER_METRICS}
    return _compare_scalar_map(
        "cluster",
        map_a,
        map_b,
        unit="count",
        normalize=True,
        hours_a=run_a.ctx.interval_hours,
        hours_b=run_b.ctx.interval_hours,
    )


def compare_wal(run_a: RunSnapshot, run_b: RunSnapshot) -> list[MetricDiff]:
    map_a = {m: _to_float(run_a.ctx.wal_stats.get(m)) for m in WAL_METRICS}
    map_b = {m: _to_float(run_b.ctx.wal_stats.get(m)) for m in WAL_METRICS}
    return _compare_scalar_map(
        "wal",
        map_a,
        map_b,
        unit="count",
        normalize=True,
        hours_a=run_a.ctx.interval_hours,
        hours_b=run_b.ctx.interval_hours,
    )


def compare_dml(run_a: RunSnapshot, run_b: RunSnapshot) -> list[MetricDiff]:
    db_a = _dbstat_map(run_a.ctx)
    db_b = _dbstat_map(run_b.ctx)
    diffs: list[MetricDiff] = []
    for dbname in sorted(set(db_a) | set(db_b)):
        row_a = db_a.get(dbname, {})
        row_b = db_b.get(dbname, {})
        for field, label in DML_METRICS.items():
            diffs.append(
                _make_diff(
                    "dml",
                    f"{dbname}.{label}",
                    _to_float(row_a.get(field)),
                    _to_float(row_b.get(field)),
                    unit="count",
                    normalize=True,
                    hours_a=run_a.ctx.interval_hours,
                    hours_b=run_b.ctx.interval_hours,
                )
            )
    return diffs


def _table_key(row: dict[str, Any]) -> str:
    return f"{row.get('dbname')}.{row.get('schemaname')}.{row.get('relname')}"


def compare_tables(
    run_a: RunSnapshot,
    run_b: RunSnapshot,
    *,
    top_n: int,
    min_change_pct: float,
) -> list[MetricDiff]:
    map_a: dict[str, dict[str, Any]] = {_table_key(r): r for r in run_a.ctx.top_tables}
    map_b: dict[str, dict[str, Any]] = {_table_key(r): r for r in run_b.ctx.top_tables}
    diffs: list[MetricDiff] = []

    for key in set(map_a) | set(map_b):
        row_a = map_a.get(key, {})
        row_b = map_b.get(key, {})
        for metric in TABLE_DML_METRICS:
            diff = _make_diff(
                "tables",
                f"{key}.{metric}",
                _to_float(row_a.get(metric)),
                _to_float(row_b.get(metric)),
                unit="count",
                normalize=True,
                hours_a=run_a.ctx.interval_hours,
                hours_b=run_b.ctx.interval_hours,
            )
            if _is_significant(diff, min_change_pct):
                diffs.append(diff)

    diffs.sort(key=lambda d: abs(d.delta or 0), reverse=True)
    return diffs[:top_n]


def _stmt_key(stmt: dict[str, Any]) -> str:
    return str(stmt.get("hexqueryid") or stmt.get("queryid") or "")


def _query_stats_line(stmt: dict[str, Any]) -> str:
    parts = []
    for field, unit, _ in QUERY_FIELDS:
        val = _to_float(stmt.get(field))
        if val is not None:
            parts.append(f"{field}={format_number(val, unit)}")
    return ", ".join(parts)


def compare_queries(
    run_a: RunSnapshot,
    run_b: RunSnapshot,
    *,
    top_n: int,
    min_change_pct: float,
    verbose: bool = False,
) -> list[MetricDiff]:
    map_a = {_stmt_key(s): s for s in run_a.ctx.top_statements if _stmt_key(s)}
    map_b = {_stmt_key(s): s for s in run_b.ctx.top_statements if _stmt_key(s)}
    diffs: list[MetricDiff] = []

    for key in set(map_a) | set(map_b):
        stmt_a = map_a.get(key, {})
        stmt_b = map_b.get(key, {})
        dbname = stmt_b.get("dbname") or stmt_a.get("dbname") or "?"
        username = stmt_b.get("username") or stmt_a.get("username") or "?"

        total_a = _to_float(stmt_a.get("total_time"))
        total_b = _to_float(stmt_b.get("total_time"))
        diff = _make_diff(
            "queries",
            f"{dbname}/{username}",
            total_a,
            total_b,
            unit="sec",
            normalize=True,
            hours_a=run_a.ctx.interval_hours,
            hours_b=run_b.ctx.interval_hours,
        )
        if not _is_significant(diff, min_change_pct):
            continue

        stats_a = _query_stats_line(stmt_a)
        stats_b = _query_stats_line(stmt_b)
        text = run_b.ctx.queries_by_id.get(key) or run_a.ctx.queries_by_id.get(key, "")
        preview = ""
        if text:
            limit = len(text) if verbose else 100
            preview = text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."
        diff.extra = f"A: {stats_a}\nB: {stats_b}" + (f"\n  {preview}" if preview else "")
        diffs.append(diff)

    diffs.sort(key=lambda d: abs(d.delta or 0), reverse=True)
    return diffs[:top_n]


def compare_queries_detailed(
    run_a: RunSnapshot,
    run_b: RunSnapshot,
    *,
    top_n: int,
    min_change_pct: float,
    verbose: bool = False,
) -> list[QueryCompareGroup]:
    """Compare matched SQL statements field-by-field (calls, times, wal_bytes, ...)."""
    map_a = {_stmt_key(s): s for s in run_a.ctx.top_statements if _stmt_key(s)}
    map_b = {_stmt_key(s): s for s in run_b.ctx.top_statements if _stmt_key(s)}
    groups: list[QueryCompareGroup] = []

    for key in set(map_a) | set(map_b):
        stmt_a = map_a.get(key, {})
        stmt_b = map_b.get(key, {})
        dbname = stmt_b.get("dbname") or stmt_a.get("dbname") or "?"
        username = stmt_b.get("username") or stmt_a.get("username") or "?"
        label = f"{dbname}/{username}"
        text = run_b.ctx.queries_by_id.get(key) or run_a.ctx.queries_by_id.get(key, "")
        preview = ""
        if text:
            limit = len(text) if verbose else 100
            preview = text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."

        fields: list[MetricDiff] = []
        score = 0.0
        for field, unit, normalize in QUERY_FIELDS:
            diff = _make_diff(
                "queries",
                field,
                _to_float(stmt_a.get(field)),
                _to_float(stmt_b.get(field)),
                unit=unit,
                normalize=normalize,
                hours_a=run_a.ctx.interval_hours,
                hours_b=run_b.ctx.interval_hours,
            )
            if _is_significant(diff, min_change_pct):
                fields.append(diff)
                score = max(score, abs(diff.delta_pct or 0), abs(diff.delta or 0))

        if not fields:
            continue

        groups.append(
            QueryCompareGroup(
                query_key=key,
                label=label,
                preview=preview,
                fields=fields,
                score=score,
            )
        )

    groups.sort(key=lambda g: g.score, reverse=True)
    return groups[:top_n]


def compare_sessions(run_a: RunSnapshot, run_b: RunSnapshot) -> list[MetricDiff]:
    db_a = _dbstat_map(run_a.ctx)
    db_b = _dbstat_map(run_b.ctx)
    diffs: list[MetricDiff] = []
    for dbname in sorted(set(db_a) | set(db_b)):
        row_a = db_a.get(dbname, {})
        row_b = db_b.get(dbname, {})
        for metric in SESSION_METRICS:
            diffs.append(
                _make_diff(
                    "sessions",
                    f"{dbname}.{metric}",
                    _to_float(row_a.get(metric)),
                    _to_float(row_b.get(metric)),
                    unit="sec" if "time" in metric else "count",
                    normalize=True,
                    hours_a=run_a.ctx.interval_hours,
                    hours_b=run_b.ctx.interval_hours,
                )
            )
    return diffs


def compare_cache(run_a: RunSnapshot, run_b: RunSnapshot) -> list[MetricDiff]:
    db_a = _dbstat_map(run_a.ctx)
    db_b = _dbstat_map(run_b.ctx)
    diffs: list[MetricDiff] = []
    for dbname in sorted(set(db_a) | set(db_b)):
        row_a = db_a.get(dbname, {})
        row_b = db_b.get(dbname, {})
        for metric in CACHE_PCT_METRICS:
            diffs.append(
                _make_diff(
                    "cache",
                    f"{dbname}.{metric}",
                    _to_float(row_a.get(metric)),
                    _to_float(row_b.get(metric)),
                    unit="pct",
                    normalize=False,
                    hours_a=run_a.ctx.interval_hours,
                    hours_b=run_b.ctx.interval_hours,
                )
            )
        for metric in CACHE_RATE_METRICS:
            diffs.append(
                _make_diff(
                    "cache",
                    f"{dbname}.{metric}",
                    _to_float(row_a.get(metric)),
                    _to_float(row_b.get(metric)),
                    unit="count" if metric == "blks_read" else "sec",
                    normalize=True,
                    hours_a=run_a.ctx.interval_hours,
                    hours_b=run_b.ctx.interval_hours,
                )
            )
    return diffs


SECTION_COMPARERS: dict[str, Callable[..., list[MetricDiff]]] = {
    "cluster": compare_cluster,
    "wal": compare_wal,
    "dml": compare_dml,
    "tables": compare_tables,
    "queries": compare_queries,
    "sessions": compare_sessions,
    "cache": compare_cache,
}


def compare_runs(
    run_a: RunSnapshot,
    run_b: RunSnapshot,
    *,
    sections: list[str] | None = None,
    min_change_pct: float = 5.0,
    top_n: int = 15,
    verbose: bool = False,
) -> CompareResult:
    selected = sections or list(ALL_SECTIONS)
    unknown = [name for name in selected if name not in SECTION_COMPARERS]
    if unknown:
        raise ValueError(f"unknown sections: {', '.join(unknown)}")

    all_diffs: list[MetricDiff] = []
    total_compared = 0

    for name in selected:
        comparer = SECTION_COMPARERS[name]
        if name in {"tables", "queries"}:
            section_diffs = comparer(
                run_a,
                run_b,
                top_n=top_n,
                min_change_pct=min_change_pct,
                **({"verbose": verbose} if name == "queries" else {}),
            )
            total_compared += top_n
        else:
            section_diffs = comparer(run_a, run_b)
            total_compared += len(section_diffs)

        if name in {"tables", "queries"}:
            all_diffs.extend(section_diffs)
        else:
            all_diffs.extend(
                d for d in section_diffs if _is_significant(d, min_change_pct)
            )

    significant = [d for d in all_diffs if _is_significant(d, min_change_pct)]
    return CompareResult(
        diffs=all_diffs,
        total_compared=total_compared,
        significant_count=len(significant),
    )


def format_number(value: float | None, unit: str = "count") -> str:
    if value is None:
        return "-"
    if unit == "pct":
        return f"{value:.2f}%"
    if unit == "ms":
        return f"{value:.1f}ms"
    if unit == "sec":
        return f"{value:.1f}s"
    if unit == "bytes":
        return format_number(value, "count")
    abs_val = abs(value)
    sign = "-" if value < 0 else ""
    if abs_val >= 1_000_000_000:
        return f"{sign}{abs_val / 1_000_000_000:.2f}G"
    if abs_val >= 1_000_000:
        return f"{sign}{abs_val / 1_000_000:.2f}M"
    if abs_val >= 1_000:
        return f"{sign}{abs_val / 1_000:.2f}K"
    if abs_val == int(abs_val):
        return f"{sign}{int(abs_val)}"
    return f"{sign}{abs_val:.2f}"


def format_value_cell(
    diff: MetricDiff,
    side: str,
    *,
    show_per_hour: bool,
) -> str:
    value = diff.value_a if side == "a" else diff.value_b
    ph = diff.per_hour_a if side == "a" else diff.per_hour_b
    main = format_number(value, diff.unit)
    if show_per_hour and ph is not None and diff.unit in {"count", "bytes", "sec"}:
        return f"{main} ({format_number(ph, diff.unit)}/h)"
    return main


def format_delta(diff: MetricDiff) -> str:
    if diff.delta is None:
        return "-"
    sign = "+" if diff.delta > 0 else ""
    main = f"{sign}{format_number(diff.delta, diff.unit)}"
    if diff.delta_pct is not None:
        pct_sign = "+" if diff.delta_pct > 0 else ""
        return f"{main} ({pct_sign}{diff.delta_pct:.1f}%)"
    return main


def interval_diff_hours(run_a: RunSnapshot, run_b: RunSnapshot) -> float:
    return abs(run_a.ctx.interval_hours - run_b.ctx.interval_hours)


def print_compare_header(run_a: RunSnapshot, run_b: RunSnapshot) -> None:
    print("pg_profile run comparison")

    for label, run in (("Run A", run_a), ("Run B", run_b)):
        props = run.ctx.properties
        server = props.get("server_name") or run.ctx.meta.get("server") or "?"
        start = props.get("report_start1") or "?"
        end = props.get("report_end1") or "?"
        hours = run.ctx.interval_hours
        print(
            f"{label} [{run.run_id}]: {run.path.name} | "
            f"{start} .. {end} ({hours:.1f} h) | server={server}"
        )

    diff_h = interval_diff_hours(run_a, run_b)
    if diff_h > 0.01:
        print()
        print(
            f"[!] Время прогонов отличается на {diff_h:.1f} часов "
            f"({run_a.ctx.interval_hours:.1f} h vs {run_b.ctx.interval_hours:.1f} h)"
        )
        print("    Метрики-счётчики дополнительно показаны в нормализации «/час»")
    print()


def print_compare_report(
    run_a: RunSnapshot,
    run_b: RunSnapshot,
    result: CompareResult,
    *,
    min_change_pct: float,
) -> None:
    print_compare_header(run_a, run_b)

    show_per_hour = interval_diff_hours(run_a, run_b) > 0.01

    grouped: dict[str, list[MetricDiff]] = {key: [] for key in SECTION_LABELS}
    for diff in result.diffs:
        if _is_significant(diff, min_change_pct):
            grouped.setdefault(diff.section, []).append(diff)

    if not any(grouped.values()):
        print("No significant metric differences found.")
        print(
            f"\nSummary: {result.total_compared} metrics compared, "
            f"0 with significant change (>= {min_change_pct:g}%)"
        )
        return

    for section, label in SECTION_LABELS.items():
        items = grouped.get(section, [])
        if not items:
            continue

        col_a = run_a.run_id
        col_b = run_b.run_id
        key_width = max(len(d.key) for d in items)
        key_width = max(key_width, len("Metric"))
        col_width = max(
            12,
            max(len(format_value_cell(d, "a", show_per_hour=show_per_hour)) for d in items),
            max(len(format_value_cell(d, "b", show_per_hour=show_per_hour)) for d in items),
            len(col_a),
            len(col_b),
        )

        print(f"== {label} ({len(items)} rows) ==")
        header = (
            f"{'Metric'.ljust(key_width)} | "
            f"{col_a.ljust(col_width)} | "
            f"{col_b.ljust(col_width)} | Delta"
        )
        sep = (
            f"{'-' * key_width}-+-"
            f"{'-' * col_width}-+-"
            f"{'-' * col_width}-+-------"
        )
        print(header)
        print(sep)

        for diff in items:
            line = (
                f"{diff.key.ljust(key_width)} | "
                f"{format_value_cell(diff, 'a', show_per_hour=show_per_hour).ljust(col_width)} | "
                f"{format_value_cell(diff, 'b', show_per_hour=show_per_hour).ljust(col_width)} | "
                f"{format_delta(diff)}"
            )
            print(line)
            if diff.extra:
                for extra_line in diff.extra.splitlines():
                    print(f"{'':>{key_width}}   {extra_line}")
        print()

    print(
        f"Summary: {result.total_compared} metrics compared, "
        f"{result.significant_count} with significant change (>= {min_change_pct:g}%)"
    )
