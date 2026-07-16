"""Health checks for a single pg_profile HTML report."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from pgprofile_parser import load_all_settings, load_report, parse_report_meta

REQUIRED_SECTIONS = (
    "checkpoints",
    "queries",
    "autovacuum",
    "wal",
    "cache",
    "sessions",
    "memory",
    "io",
    "locks",
)

CATEGORY_LABELS = {
    "checkpoints": "Checkpoints",
    "queries": "Slow queries",
    "autovacuum": "Autovacuum",
    "wal": "WAL",
    "cache": "Cache and disk reads",
    "sessions": "Sessions and transactions",
    "memory": "Memory settings",
    "io": "I/O patterns",
    "locks": "Locks",
}


@dataclass
class Warning:
    category: str
    severity: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReportContext:
    path: Path
    meta: dict[str, str]
    properties: dict[str, Any]
    cluster_stats: dict[str, Any]
    wal_stats: dict[str, Any]
    dbstat: list[dict[str, Any]]
    stat_slru: list[dict[str, Any]]
    settings: dict[str, str]
    top_statements: list[dict[str, Any]]
    queries_by_id: dict[str, str]
    top_tbl_last_sample: list[dict[str, Any]]
    top_tables: list[dict[str, Any]]
    top_io_tables: list[dict[str, Any]]
    top_io_indexes: list[dict[str, Any]]
    top_indexes: list[dict[str, Any]]
    top_rusage_statements: list[dict[str, Any]]
    statements_dbstats: list[dict[str, Any]]
    interval_hours: float
    report_end: datetime | None = None


def load_thresholds(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"thresholds config not found: {path}")
    with path.open(encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle)
    if not isinstance(cfg, dict):
        raise ValueError(f"invalid thresholds config: {path}")
    missing = [name for name in REQUIRED_SECTIONS if name not in cfg]
    if missing:
        raise ValueError(f"thresholds config missing sections: {', '.join(missing)}")
    return cfg


def parse_setting_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    if value.lower() in {"on", "true"}:
        return 1
    if value.lower() in {"off", "false"}:
        return 0
    match = re.match(r"^(-?\d+)", value.strip())
    return int(match.group(1)) if match else None


def parse_size_to_mb(text: str | None) -> float | None:
    if not text:
        return None
    match = re.match(
        r"^\s*(-?\d+(?:\.\d+)?)\s*(B|kB|KB|MB|GB|TB)?\s*$",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "B").lower()
    multipliers = {"b": 1 / (1024 * 1024), "kb": 1 / 1024, "mb": 1, "gb": 1024, "tb": 1024 * 1024}
    return value * multipliers.get(unit, 1)


def parse_report_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace(" ", "T", 1) if " " in value and "T" not in value else value
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _first_row(dataset: list[dict[str, Any]] | None) -> dict[str, Any]:
    if dataset:
        return dataset[0]
    return {}


def load_report_data(html_path: Path) -> ReportContext:
    data = load_report(html_path)
    datasets = data.get("datasets", {})
    properties = _first_row(datasets.get("properties"))
    interval_sec = float(properties.get("interval_duration_sec") or 0)
    interval_hours = interval_sec / 3600 if interval_sec else 0.0

    queries_by_id: dict[str, str] = {}
    for row in datasets.get("queries", []):
        hex_id = row.get("hexqueryid")
        texts = row.get("query_texts") or []
        if hex_id and texts:
            queries_by_id[hex_id] = texts[0]

    return ReportContext(
        path=html_path,
        meta=parse_report_meta(html_path),
        properties=properties,
        cluster_stats=_first_row(datasets.get("cluster_stats")),
        wal_stats=_first_row(datasets.get("wal_stats")),
        dbstat=list(datasets.get("dbstat", [])),
        stat_slru=list(datasets.get("stat_slru", [])),
        settings=load_all_settings(data),
        top_statements=list(datasets.get("top_statements", [])),
        queries_by_id=queries_by_id,
        top_tbl_last_sample=list(datasets.get("top_tbl_last_sample", [])),
        top_tables=list(datasets.get("top_tables", [])),
        top_io_tables=list(datasets.get("top_io_tables", [])),
        top_io_indexes=list(datasets.get("top_io_indexes", [])),
        top_indexes=list(datasets.get("top_indexes", [])),
        top_rusage_statements=list(datasets.get("top_rusage_statements", [])),
        statements_dbstats=list(datasets.get("statements_dbstats", [])),
        interval_hours=interval_hours,
        report_end=parse_report_datetime(properties.get("report_end1")),
    )


def _warn(category: str, severity: str, message: str, **details: Any) -> Warning:
    return Warning(category=category, severity=severity, message=message, details=details)


def check_checkpoints(ctx: ReportContext, cfg: dict[str, Any]) -> list[Warning]:
    warnings: list[Warning] = []
    stats = ctx.cluster_stats
    req = int(stats.get("checkpoints_req") or 0)
    timed = int(stats.get("checkpoints_timed") or 0)
    total = req + timed
    write_time = float(stats.get("checkpoint_write_time") or 0)
    maxwritten_clean = int(stats.get("maxwritten_clean") or 0)

    if total > 0:
        ratio = req / total
        if ratio > cfg["max_requested_ratio"]:
            severity = (
                "critical"
                if ratio >= cfg.get("critical_requested_ratio", 0.5)
                else "warning"
            )
            warnings.append(
                _warn(
                    "checkpoints",
                    severity,
                    f"Requested checkpoints: {req}/{total} ({ratio:.1%}), threshold {cfg['max_requested_ratio']:.0%}",
                    requested=req,
                    timed=timed,
                    ratio=ratio,
                )
            )

    if req > cfg["max_requested_count"]:
        warnings.append(
            _warn(
                "checkpoints",
                "warning",
                f"Requested checkpoints: {req} (threshold {cfg['max_requested_count']})",
                requested=req,
            )
        )

    if ctx.interval_hours > 0:
        per_hour = req / ctx.interval_hours
        if per_hour > cfg["max_requested_per_hour"]:
            warnings.append(
                _warn(
                    "checkpoints",
                    "warning",
                    f"Requested checkpoints: {per_hour:.1f}/hour (threshold {cfg['max_requested_per_hour']})",
                    per_hour=per_hour,
                )
            )
        write_per_hour = write_time / ctx.interval_hours
        if write_per_hour > cfg["max_write_time_per_hour_sec"]:
            warnings.append(
                _warn(
                    "checkpoints",
                    "warning",
                    f"Checkpoint write time: {write_per_hour:.1f}s/hour (threshold {cfg['max_write_time_per_hour_sec']}s/hour)",
                    write_per_hour=write_per_hour,
                )
            )

    if write_time > cfg["max_write_time_sec"]:
        warnings.append(
            _warn(
                "checkpoints",
                "warning",
                f"Checkpoint write time: {write_time:.1f}s over {ctx.interval_hours:.1f}h interval (threshold {cfg['max_write_time_sec']}s)",
                write_time=write_time,
            )
        )

    if maxwritten_clean > cfg["max_maxwritten_clean"]:
        warnings.append(
            _warn(
                "checkpoints",
                "warning",
                f"Bgwriter interrupts (maxwritten_clean): {maxwritten_clean} (threshold {cfg['max_maxwritten_clean']})",
                maxwritten_clean=maxwritten_clean,
            )
        )

    return warnings


def _query_preview(ctx: ReportContext, stmt: dict[str, Any], cfg: dict[str, Any], verbose: bool) -> str:
    hex_id = stmt.get("hexqueryid", "")
    text = ctx.queries_by_id.get(hex_id, "")
    if not text or not cfg.get("include_query_preview", True):
        return ""
    limit = len(text) if verbose else int(cfg.get("query_preview_chars", 120))
    preview = text if len(text) <= limit else text[: max(limit - 3, 0)] + "..."
    return preview


def check_slow_queries(
    ctx: ReportContext,
    cfg: dict[str, Any],
    *,
    verbose: bool = False,
) -> list[Warning]:
    warnings: list[Warning] = []
    seen: set[str] = set()

    for stmt in ctx.top_statements:
        hex_id = stmt.get("hexqueryid")
        if not hex_id or hex_id in seen:
            continue

        mean_ms = float(stmt.get("mean_exec_time") or 0)
        max_ms = float(stmt.get("max_exec_time") or 0)
        total_sec = float(stmt.get("total_time") or stmt.get("total_exec_time") or 0)
        calls = int(stmt.get("calls") or 0)
        reasons: list[str] = []

        if mean_ms >= cfg["max_mean_exec_ms"] and calls >= cfg["min_calls_for_mean"]:
            reasons.append(f"mean={mean_ms:.1f}ms")
        if max_ms >= cfg["max_max_exec_ms"]:
            reasons.append(f"max={max_ms:.1f}ms")
        if total_sec >= cfg["max_total_exec_sec"]:
            reasons.append(f"total={total_sec:.1f}s")

        if not reasons:
            continue

        seen.add(hex_id)
        dbname = stmt.get("dbname", "?")
        username = stmt.get("username", "?")
        message = f"{dbname}/{username}: {', '.join(reasons)}, calls={calls}"
        preview = _query_preview(ctx, stmt, cfg, verbose)
        if preview:
            message = f"{message}\n  {preview}"
        full_sql = (ctx.queries_by_id.get(hex_id) or "").strip()
        warnings.append(
            _warn(
                "queries",
                "warning",
                message,
                hexqueryid=hex_id,
                mean_exec_time=mean_ms,
                max_exec_time=max_ms,
                total_time=total_sec,
                calls=calls,
                query_text=full_sql or None,
            )
        )

    warnings.sort(key=lambda w: w.details.get("max_exec_time", 0), reverse=True)
    return warnings[: int(cfg["top_n"])]


def _is_excluded_schema(schema: str | None, excluded: list[str]) -> bool:
    return bool(schema and schema in excluded)


def check_autovacuum(ctx: ReportContext, cfg: dict[str, Any]) -> list[Warning]:
    warnings: list[Warning] = []
    settings_cfg = cfg["settings"]
    tables_cfg = cfg["tables"]
    settings = ctx.settings

    autovacuum = settings.get("autovacuum", "on").lower()
    if autovacuum in {"off", "false", "0"}:
        warnings.append(
            _warn("autovacuum", "critical", "autovacuum is disabled", setting="autovacuum")
        )

    naptime = parse_setting_int(settings.get("autovacuum_naptime"))
    if naptime is not None and naptime > settings_cfg["max_naptime_sec"]:
        warnings.append(
            _warn(
                "autovacuum",
                "warning",
                f"autovacuum_naptime={naptime}s exceeds threshold {settings_cfg['max_naptime_sec']}s (autovacuum runs too rarely)",
            )
        )

    cost_delay = parse_setting_int(settings.get("autovacuum_vacuum_cost_delay"))
    if cost_delay is not None and cost_delay > settings_cfg["max_vacuum_cost_delay_ms"]:
        warnings.append(
            _warn(
                "autovacuum",
                "warning",
                f"autovacuum_vacuum_cost_delay={cost_delay}ms exceeds threshold {settings_cfg['max_vacuum_cost_delay_ms']}ms",
            )
        )

    cost_limit = parse_setting_int(settings.get("autovacuum_vacuum_cost_limit"))
    if cost_limit is not None and cost_limit < settings_cfg["min_vacuum_cost_limit"]:
        warnings.append(
            _warn(
                "autovacuum",
                "warning",
                f"autovacuum_vacuum_cost_limit={cost_limit} below threshold {settings_cfg['min_vacuum_cost_limit']}",
            )
        )

    work_mem = parse_setting_int(settings.get("autovacuum_work_mem"))
    if work_mem is not None and work_mem > settings_cfg["max_work_mem_kb"]:
        warnings.append(
            _warn(
                "autovacuum",
                "warning",
                f"autovacuum_work_mem={work_mem}kB exceeds threshold {settings_cfg['max_work_mem_kb']}kB",
            )
        )

    table_warnings: list[Warning] = []
    excluded = tables_cfg.get("exclude_schemas", [])
    for row in ctx.top_tbl_last_sample:
        schema = row.get("schemaname")
        if _is_excluded_schema(schema, excluded):
            continue

        dbname = row.get("dbname", "?")
        relname = row.get("relname", "?")
        live = int(row.get("n_live_tup") or 0)
        dead = int(row.get("n_dead_tup") or 0)
        dead_pct = float(row.get("dead_pct") or 0)
        mods_pct = float(row.get("mods_pct") or 0)
        last_av = row.get("last_autovacuum")
        qualified = f"{dbname}.{schema}.{relname}"

        if live >= tables_cfg["min_live_tup"] and dead_pct > tables_cfg["max_dead_pct"]:
            table_warnings.append(
                _warn(
                    "autovacuum",
                    "warning",
                    f"{qualified}: dead_pct={dead_pct:.1f}%, n_dead={dead}, last_autovacuum={last_av or 'never'}",
                    dead_pct=dead_pct,
                )
            )
        elif dead >= tables_cfg["min_dead_tup"]:
            table_warnings.append(
                _warn(
                    "autovacuum",
                    "warning",
                    f"{qualified}: n_dead={dead}, last_autovacuum={last_av or 'never'}",
                    n_dead_tup=dead,
                )
            )

        if mods_pct > tables_cfg["max_mods_pct"]:
            table_warnings.append(
                _warn(
                    "autovacuum",
                    "warning",
                    f"{qualified}: mods_pct={mods_pct:.1f}%, last_autoanalyze={row.get('last_autoanalyze') or 'never'}",
                    mods_pct=mods_pct,
                )
            )

        if (
            dead_pct > tables_cfg["max_dead_pct"]
            and ctx.report_end is not None
            and last_av
        ):
            last_av_dt = parse_report_datetime(str(last_av))
            if last_av_dt is not None:
                age_hours = (ctx.report_end - last_av_dt).total_seconds() / 3600
                if age_hours > tables_cfg["stale_autovacuum_hours"]:
                    table_warnings.append(
                        _warn(
                            "autovacuum",
                            "warning",
                            f"{qualified}: stale autovacuum ({age_hours:.0f}h ago), dead_pct={dead_pct:.1f}%",
                            age_hours=age_hours,
                        )
                    )

    table_warnings.sort(
        key=lambda w: w.details.get("dead_pct", w.details.get("mods_pct", 0)),
        reverse=True,
    )
    warnings.extend(table_warnings[: int(tables_cfg["top_n"])])
    return warnings


def check_wal(ctx: ReportContext, cfg: dict[str, Any]) -> list[Warning]:
    warnings: list[Warning] = []
    wal = ctx.wal_stats
    cluster = ctx.cluster_stats

    wal_buffers_full = int(wal.get("wal_buffers_full") or 0)
    if wal_buffers_full > cfg["max_wal_buffers_full"]:
        warnings.append(
            _warn(
                "wal",
                "warning",
                f"wal_buffers_full: {wal_buffers_full} (threshold {cfg['max_wal_buffers_full']})",
            )
        )

    wal_bytes = wal.get("wal_bytes")
    if wal_bytes and ctx.interval_hours > 0:
        mb_per_sec = float(wal_bytes) / ctx.interval_hours / 3600 / (1024 * 1024)
        if mb_per_sec > cfg["max_wal_mb_per_sec"]:
            warnings.append(
                _warn(
                    "wal",
                    "warning",
                    f"WAL generation: {mb_per_sec:.1f} MB/s (threshold {cfg['max_wal_mb_per_sec']} MB/s)",
                    mb_per_sec=mb_per_sec,
                )
            )

    max_wal_size_mb = parse_setting_int(ctx.settings.get("max_wal_size"))
    if max_wal_size_mb is not None and max_wal_size_mb < cfg["min_max_wal_size_mb"]:
        warnings.append(
            _warn(
                "wal",
                "warning",
                f"max_wal_size={max_wal_size_mb}MB below threshold {cfg['min_max_wal_size_mb']}MB",
            )
        )

    backend = int(cluster.get("buffers_backend") or 0)
    checkpoint = int(cluster.get("buffers_checkpoint") or 0)
    if checkpoint > 0 and backend > checkpoint * cfg.get("warn_backend_writes_ratio", 1.0):
        warnings.append(
            _warn(
                "wal",
                "warning",
                f"Backend buffers written ({backend}) exceed checkpoint buffers written ({checkpoint})",
                buffers_backend=backend,
                buffers_checkpoint=checkpoint,
            )
        )

    return warnings


def check_cache(ctx: ReportContext, cfg: dict[str, Any]) -> list[Warning]:
    warnings: list[Warning] = []

    for row in ctx.dbstat:
        dbname = row.get("dbname")
        if not dbname or dbname == "Total":
            continue
        hit_pct = row.get("blks_hit_pct")
        if hit_pct is not None and float(hit_pct) < cfg["min_blks_hit_pct"]:
            warnings.append(
                _warn(
                    "cache",
                    "warning",
                    f"{dbname}: blks_hit_pct={float(hit_pct):.2f}% (threshold {cfg['min_blks_hit_pct']}%)",
                )
            )
        read_time = float(row.get("blk_read_time") or 0)
        if read_time > cfg["max_blk_read_time_sec"]:
            warnings.append(
                _warn(
                    "cache",
                    "warning",
                    f"{dbname}: blk_read_time={read_time:.1f}s (threshold {cfg['max_blk_read_time_sec']}s)",
                )
            )
        if row.get("temp_bytes") not in (None, 0) or row.get("temp_files") not in (None, 0):
            warnings.append(
                _warn(
                    "cache",
                    "warning",
                    f"{dbname}: temp usage detected (temp_bytes={row.get('temp_bytes')}, temp_files={row.get('temp_files')})",
                )
            )

    for row in ctx.stat_slru:
        name = row.get("name")
        if not name or name == "Total":
            continue
        hit_pct = row.get("hit_pct")
        if hit_pct is not None and float(hit_pct) < cfg["min_slru_hit_pct"]:
            warnings.append(
                _warn(
                    "cache",
                    "warning",
                    f"SLRU {name}: hit_pct={float(hit_pct):.2f}% (threshold {cfg['min_slru_hit_pct']}%)",
                )
            )

    for row in ctx.top_io_tables:
        hit_pct = row.get("hit_pct")
        if hit_pct is not None and float(hit_pct) < cfg.get("min_table_io_hit_pct", 95.0):
            qualified = f"{row.get('dbname')}.{row.get('schemaname')}.{row.get('relname')}"
            warnings.append(
                _warn(
                    "cache",
                    "warning",
                    f"{qualified}: table hit_pct={float(hit_pct):.2f}% (threshold {cfg.get('min_table_io_hit_pct', 95.0)}%)",
                )
            )

    return warnings


def check_sessions(ctx: ReportContext, cfg: dict[str, Any]) -> list[Warning]:
    warnings: list[Warning] = []
    idle_timeout = parse_setting_int(ctx.settings.get("idle_in_transaction_session_timeout")) or 0

    for row in ctx.dbstat:
        dbname = row.get("dbname")
        if not dbname or dbname == "Total":
            continue

        idle = float(row.get("idle_in_transaction_time") or 0)
        if idle > cfg["max_idle_in_transaction_sec"]:
            severity = "critical" if idle_timeout == 0 and cfg.get("warn_on_disabled_idle_timeout") else "warning"
            warnings.append(
                _warn(
                    "sessions",
                    severity,
                    f"{dbname}: idle_in_transaction_time={idle:.1f}s (threshold {cfg['max_idle_in_transaction_sec']}s), idle_in_transaction_session_timeout={idle_timeout}",
                    idle_time=idle,
                )
            )
        elif ctx.interval_hours > 0:
            idle_per_hour = idle / ctx.interval_hours
            if idle_per_hour > cfg["max_idle_in_transaction_per_hour"]:
                warnings.append(
                    _warn(
                        "sessions",
                        "warning",
                        f"{dbname}: idle_in_transaction_time={idle_per_hour:.1f}s/hour (threshold {cfg['max_idle_in_transaction_per_hour']}s/hour)",
                    )
                )

        commits = int(row.get("xact_commit") or 0)
        rollbacks = int(row.get("xact_rollback") or 0)
        total_tx = commits + rollbacks
        if total_tx > 0:
            rollback_pct = rollbacks / total_tx * 100
            if rollback_pct > cfg["max_rollback_pct"]:
                warnings.append(
                    _warn(
                        "sessions",
                        "warning",
                        f"{dbname}: rollback_pct={rollback_pct:.2f}% (threshold {cfg['max_rollback_pct']}%)",
                    )
                )

        for field_name, label in (("sessions_fatal", "fatal sessions"), ("sessions_killed", "killed sessions")):
            count = row.get(field_name)
            if count not in (None, 0):
                warnings.append(
                    _warn(
                        "sessions",
                        "critical",
                        f"{dbname}: {label}={count}",
                    )
                )

    if cfg.get("warn_on_disabled_idle_timeout") and idle_timeout == 0:
        high_idle = any(
            float(row.get("idle_in_transaction_time") or 0) > cfg["max_idle_in_transaction_sec"]
            for row in ctx.dbstat
            if row.get("dbname") not in (None, "Total")
        )
        if high_idle:
            warnings.append(
                _warn(
                    "sessions",
                    "critical",
                    "idle_in_transaction_session_timeout=0 while idle in transaction time is high",
                )
            )

    return warnings


def _shared_buffers_mb(value: str | None) -> float | None:
    blocks = parse_setting_int(value)
    if blocks is None:
        return None
    return blocks * 8 / 1024


def check_memory_settings(ctx: ReportContext, cfg: dict[str, Any]) -> list[Warning]:
    warnings: list[Warning] = []
    settings = ctx.settings

    work_mem = parse_setting_int(settings.get("work_mem"))
    max_connections = parse_setting_int(settings.get("max_connections"))
    if work_mem is not None and max_connections is not None:
        total_gb = work_mem * max_connections / 1024 / 1024
        if total_gb > cfg["warn_work_mem_x_connections_gb"]:
            warnings.append(
                _warn(
                    "memory",
                    "warning",
                    f"work_mem * max_connections = {total_gb:.1f}GB (threshold {cfg['warn_work_mem_x_connections_gb']}GB)",
                )
            )

    if work_mem is not None and work_mem > cfg["max_work_mem_kb"]:
        warnings.append(
            _warn(
                "memory",
                "warning",
                f"work_mem={work_mem}kB exceeds threshold {cfg['max_work_mem_kb']}kB",
            )
        )

    shared_mb = _shared_buffers_mb(settings.get("shared_buffers"))
    if shared_mb is not None and shared_mb < cfg["min_shared_buffers_mb"]:
        warnings.append(
            _warn(
                "memory",
                "warning",
                f"shared_buffers={shared_mb:.0f}MB below threshold {cfg['min_shared_buffers_mb']}MB",
            )
        )

    if cfg.get("warn_huge_pages_not_on"):
        min_sb = float(cfg.get("recommend_huge_pages_min_shared_buffers_mb") or 2048)
        huge = (settings.get("huge_pages") or "").strip().lower()
        if shared_mb is not None and shared_mb >= min_sb and huge in ("", "off", "try"):
            shm_hp = settings.get("shared_memory_size_in_huge_pages") or "?"
            warnings.append(
                _warn(
                    "memory",
                    "warning",
                    f"huge_pages={huge or 'unset'} with shared_buffers={shared_mb:.0f}MB "
                    f"(≥{min_sb:.0f}MB): prefer huge_pages=on; "
                    f"shared_memory_size_in_huge_pages={shm_hp}",
                    huge_pages=huge or "unset",
                    shared_buffers_mb=round(shared_mb, 1),
                    shared_memory_size_in_huge_pages=shm_hp,
                    min_shared_buffers_mb=min_sb,
                )
            )

    if cfg.get("warn_statement_timeout_zero"):
        timeout = parse_setting_int(settings.get("statement_timeout")) or 0
        if timeout == 0:
            warnings.append(
                _warn("memory", "warning", "statement_timeout=0 (queries may run indefinitely)")
            )

    if cfg.get("warn_lock_timeout_zero"):
        timeout = parse_setting_int(settings.get("lock_timeout")) or 0
        if timeout == 0:
            warnings.append(
                _warn("memory", "warning", "lock_timeout=0 (no protection against lock waits)")
            )

    return warnings


def check_io_patterns(
    ctx: ReportContext,
    cfg: dict[str, Any],
    *,
    verbose: bool = False,
) -> list[Warning]:
    query_warnings: list[Warning] = []
    table_warnings: list[Warning] = []
    index_warnings: list[Warning] = []

    queries_cfg = cfg["queries"]
    tables_cfg = cfg["tables"]
    indexes_cfg = cfg["indexes"]
    seen_queries: set[str] = set()

    for stmt in ctx.top_statements:
        hex_id = stmt.get("hexqueryid")
        if not hex_id or hex_id in seen_queries:
            continue

        io_time = float(stmt.get("io_time") or 0)
        wal_bytes = float(stmt.get("wal_bytes") or 0)
        temp_written = int(stmt.get("temp_blks_written") or 0)
        reasons: list[str] = []

        if io_time >= queries_cfg["max_io_time_ms"]:
            reasons.append(f"io_time={io_time:.1f}ms")
        wal_gb = wal_bytes / (1024**3)
        if wal_gb >= queries_cfg["max_wal_bytes_gb"]:
            reasons.append(f"wal={wal_gb:.1f}GB")
        if temp_written >= queries_cfg["max_temp_blks_written"]:
            reasons.append(f"temp_blks_written={temp_written}")

        if not reasons:
            continue

        seen_queries.add(hex_id)
        dbname = stmt.get("dbname", "?")
        username = stmt.get("username", "?")
        message = f"{dbname}/{username}: {', '.join(reasons)}"
        preview = _query_preview(ctx, stmt, queries_cfg, verbose)
        if preview:
            message = f"{message}\n  {preview}"
        full_sql = (ctx.queries_by_id.get(hex_id) or "").strip()
        query_warnings.append(
            _warn(
                "io",
                "warning",
                message,
                hexqueryid=hex_id,
                wal_bytes=wal_bytes,
                io_time=io_time,
                query_text=full_sql or None,
            )
        )

    query_warnings.sort(key=lambda w: w.details.get("wal_bytes", 0), reverse=True)
    query_warnings = query_warnings[: int(queries_cfg["top_n"])]

    for row in ctx.top_tables:
        seq_scan = int(row.get("seq_scan") or 0)
        idx_scan = int(row.get("idx_scan") or 0)
        if seq_scan < tables_cfg["min_seq_scan"]:
            continue
        ratio = seq_scan / max(idx_scan, 1)
        if ratio >= tables_cfg["max_seq_scan_to_idx_scan_ratio"]:
            qualified = f"{row.get('dbname')}.{row.get('schemaname')}.{row.get('relname')}"
            table_warnings.append(
                _warn(
                    "io",
                    "warning",
                    f"{qualified}: seq_scan={seq_scan}, idx_scan={idx_scan}, ratio={ratio:.1f}",
                    seq_scan=seq_scan,
                )
            )

    for row in ctx.top_io_tables:
        heap_reads = int(row.get("heap_blks_read") or 0)
        if heap_reads >= tables_cfg["min_heap_blks_read"]:
            qualified = f"{row.get('dbname')}.{row.get('schemaname')}.{row.get('relname')}"
            table_warnings.append(
                _warn(
                    "io",
                    "warning",
                    f"{qualified}: heap_blks_read={heap_reads}",
                    heap_blks_read=heap_reads,
                )
            )

    table_warnings.sort(
        key=lambda w: w.details.get("seq_scan", w.details.get("heap_blks_read", 0)),
        reverse=True,
    )
    table_warnings = table_warnings[: int(tables_cfg["top_n"])]

    excluded = indexes_cfg.get("exclude_schemas", [])
    for row in ctx.top_indexes:
        if row.get("ord_unused") is None:
            continue
        schema = row.get("schemaname")
        if _is_excluded_schema(schema, excluded):
            continue
        size_mb = parse_size_to_mb(row.get("indexrelsize_pretty"))
        if size_mb is not None and size_mb < indexes_cfg["min_unused_index_size_mb"]:
            continue
        qualified = f"{row.get('dbname')}.{schema}.{row.get('relname')}.{row.get('indexrelname')}"
        index_warnings.append(
            _warn(
                "io",
                "warning",
                f"Unused index {qualified}: size={row.get('indexrelsize_pretty')}",
            )
        )

    index_warnings = index_warnings[: int(indexes_cfg["top_n"])]
    return query_warnings + table_warnings + index_warnings


def check_locks(ctx: ReportContext, cfg: dict[str, Any]) -> list[Warning]:
    warnings: list[Warning] = []
    for row in ctx.dbstat:
        dbname = row.get("dbname")
        if not dbname or dbname == "Total":
            continue
        deadlocks = row.get("deadlocks")
        if deadlocks not in (None, 0) and int(deadlocks) > cfg["max_deadlocks"]:
            warnings.append(
                _warn(
                    "locks",
                    "critical",
                    f"{dbname}: deadlocks={deadlocks} (threshold {cfg['max_deadlocks']})",
                )
            )
    return warnings


CHECKERS: dict[str, Callable[..., list[Warning]]] = {
    "checkpoints": check_checkpoints,
    "queries": check_slow_queries,
    "autovacuum": check_autovacuum,
    "wal": check_wal,
    "cache": check_cache,
    "sessions": check_sessions,
    "memory": check_memory_settings,
    "io": check_io_patterns,
    "locks": check_locks,
}


def run_checks(
    ctx: ReportContext,
    cfg: dict[str, Any],
    *,
    categories: list[str] | None = None,
    verbose: bool = False,
) -> list[Warning]:
    selected = categories or list(CHECKERS.keys())
    unknown = [name for name in selected if name not in CHECKERS]
    if unknown:
        raise ValueError(f"unknown categories: {', '.join(unknown)}")

    warnings: list[Warning] = []
    for name in selected:
        checker = CHECKERS[name]
        if name in {"queries", "io"}:
            warnings.extend(checker(ctx, cfg[name], verbose=verbose))
        else:
            warnings.extend(checker(ctx, cfg[name]))
    return warnings


def print_report(ctx: ReportContext, warnings: list[Warning]) -> None:
    server = ctx.properties.get("server_name") or ctx.meta.get("server") or "unknown"
    start = ctx.properties.get("report_start1") or ctx.meta.get("from") or "?"
    end = ctx.properties.get("report_end1") or ctx.meta.get("to") or "?"

    print("pg_profile health check")
    print(f"Server: {server}")
    print(f"Interval: {start} .. {end} ({ctx.interval_hours:.1f} h)")
    print(f"Report: {ctx.path.name}")
    print()

    if not warnings:
        print("No warnings found.")
        return

    grouped: dict[str, list[Warning]] = {key: [] for key in CATEGORY_LABELS}
    for warning in warnings:
        grouped.setdefault(warning.category, []).append(warning)

    for category, label in CATEGORY_LABELS.items():
        items = grouped.get(category, [])
        if not items:
            continue
        print(f"== {label} ({len(items)}) ==")
        for item in items:
            prefix = "CRIT" if item.severity == "critical" else "WARN"
            print(f"[{prefix}] {item.message}")
        print()

    critical = sum(1 for w in warnings if w.severity == "critical")
    regular = len(warnings) - critical
    print(f"Summary: {len(warnings)} warning(s) ({critical} critical, {regular} warning)")
