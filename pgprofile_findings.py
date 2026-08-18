"""Convert analysis results to stable JSON findings for advisor and LLM."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pgprofile_health import ReportContext, Warning
from pgprofile_compare import CompareResult, MetricDiff, RunSnapshot
from pgprofile_contracts import (
    SCHEMA_VERSION,
    build_confidence_meta,
    build_run_identity_pair,
    build_workload_match,
)


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _json_safe(v) for k, v in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    return value


def infer_rule_id(warning: Warning) -> str:
    category = warning.category
    details = warning.details
    message = warning.message.lower()

    if category == "checkpoints":
        if "ratio" in details:
            return "checkpoints.high_requested_ratio"
        if "per_hour" in details:
            return "checkpoints.high_requested_per_hour"
        if "write_per_hour" in details:
            return "checkpoints.high_write_time_per_hour"
        if "write_time" in details:
            return "checkpoints.high_write_time"
        if "maxwritten_clean" in details:
            return "checkpoints.maxwritten_clean"
        if "requested" in details:
            return "checkpoints.high_requested_count"
    if category == "wal":
        if "wal_buffers_full" in message or "wal_buffers_full" in details:
            return "wal.buffers_full"
        if "mb_per_sec" in details:
            return "wal.high_generation_rate"
        if "max_wal_size" in message:
            return "wal.low_max_wal_size"
        if "backend" in message:
            return "wal.backend_writes_high"
    if category == "queries":
        return "queries.slow_execution"
    if category == "autovacuum":
        if "autovacuum is disabled" in message:
            return "autovacuum.disabled"
        if "naptime" in message:
            return "autovacuum.naptime_high"
        if "cost_delay" in message:
            return "autovacuum.cost_delay_high"
        if "cost_limit" in message:
            return "autovacuum.cost_limit_low"
        if "work_mem" in message:
            return "autovacuum.work_mem_high"
        if "dead_pct" in details or "dead_pct" in message:
            return "autovacuum.table_high_dead_pct"
        if "mods_pct" in details:
            return "autovacuum.table_high_mods_pct"
        if "stale" in message:
            return "autovacuum.stale_vacuum"
        if "n_dead" in details or "n_dead=" in message:
            return "autovacuum.table_many_dead_tuples"
    if category == "cache":
        if "slru" in message:
            return "cache.low_slru_hit_ratio"
        if "blks_hit_pct" in message:
            return "cache.low_hit_ratio"
        if "blk_read_time" in message:
            return "cache.high_read_time"
        if "temp usage" in message:
            return "cache.temp_usage"
        if "hit_pct" in message and "table" in message:
            return "cache.low_table_hit_ratio"
    if category == "sessions":
        if "idle_in_transaction_session_timeout=0 while" in message:
            return "sessions.idle_timeout_disabled"
        if "connection pressure" in message:
            return "sessions.connection_pressure"
        if "idle_in_transaction" in message:
            return "sessions.high_idle_in_transaction"
        if "rollback_pct" in message:
            return "sessions.high_rollback_ratio"
        if "fatal" in message or "killed" in message:
            return "sessions.abnormal_termination"
    if category == "memory":
        if "work_mem * max_connections" in message:
            return "memory.work_mem_connections_risk"
        if "work_mem=" in message and "exceeds" in message:
            return "memory.work_mem_high"
        if "huge_pages=" in message:
            return "memory.huge_pages_recommended"
        if "shared_buffers" in message:
            return "memory.shared_buffers_low"
        if "statement_timeout=0" in message:
            return "memory.statement_timeout_zero"
        if "lock_timeout=0" in message:
            return "memory.lock_timeout_zero"
    if category == "io":
        kind = str(details.get("kind") or "")
        if kind == "table_growth" or "table growth=" in message:
            return "io.table_growth"
        if kind == "index_growth" or "growing index" in message:
            return "io.index_growth"
        if kind == "vacuum_ops" or "vacuum_ops=" in message:
            return "io.vacuum_ops_pressure"
        if kind == "hot_index" or "hot index reads" in message:
            return "io.hot_index_reads"
        if kind == "db_dominance" or "dominates statement" in message:
            return "db.statement_dominance"
        if "wal=" in message:
            return "io.wal_heavy_query"
        if "io_time" in message:
            return "io.high_io_wait_query"
        if "temp_blks_written" in message or "temp_blk_write_time" in message:
            return "io.temp_spill_query"
        if "shared_blks_dirtied" in message:
            return "io.wal_heavy_query"
        if "seq_scan" in message:
            return "io.high_seq_scan"
        if "heap_blks_read" in message:
            return "io.high_heap_reads"
        if "unused index" in message:
            return "io.unused_index"
    if category == "disk":
        return "disk.tablespace_growth"
    if category == "locks":
        return "locks.deadlocks"

    return f"{category}.generic"


def warning_to_finding(warning: Warning) -> dict[str, Any]:
    rule_id = infer_rule_id(warning)
    return {
        "id": rule_id,
        "category": warning.category,
        "severity": warning.severity,
        "message": warning.message,
        "details": _json_safe(warning.details),
    }


def health_check_to_dict(ctx: ReportContext, warnings: list[Warning]) -> dict[str, Any]:
    critical = sum(1 for w in warnings if w.severity == "critical")
    regular = len(warnings) - critical
    return {
        "type": "health_check",
        "report_meta": {
            "path": str(ctx.path),
            "filename": ctx.path.name,
            "server": ctx.properties.get("server_name") or ctx.meta.get("server"),
            "report_start": ctx.properties.get("report_start1"),
            "report_end": ctx.properties.get("report_end1"),
            "interval_hours": ctx.interval_hours,
            "pgprofile_version": ctx.properties.get("pgprofile_version"),
        },
        "findings": [warning_to_finding(w) for w in warnings],
        "summary": {
            "total": len(warnings),
            "critical": critical,
            "warning": regular,
        },
    }


def metric_diff_to_finding(diff: MetricDiff, min_change_pct: float) -> dict[str, Any] | None:
    if diff.value_a is None and diff.value_b is None:
        return None
    significant = False
    if diff.value_a is None or diff.value_b is None:
        significant = True
    elif diff.delta is not None and diff.delta != 0:
        if diff.delta_pct is None:
            significant = True
        else:
            significant = abs(diff.delta_pct) >= min_change_pct

    if not significant:
        return None

    rule_id = f"run_compare.{diff.section}.{diff.key.replace('/', '_')}"
    return {
        "id": rule_id,
        "category": diff.section,
        "severity": "warning",
        "message": diff.key,
        "details": {
            "value_a": diff.value_a,
            "value_b": diff.value_b,
            "per_hour_a": diff.per_hour_a,
            "per_hour_b": diff.per_hour_b,
            "delta": diff.delta,
            "delta_pct": diff.delta_pct,
            "unit": diff.unit,
            "extra": diff.extra,
        },
    }


def run_comparison_to_dict(
    run_a: RunSnapshot,
    run_b: RunSnapshot,
    result: CompareResult,
    *,
    min_change_pct: float,
) -> dict[str, Any]:
    interval_diff = abs(run_a.ctx.interval_hours - run_b.ctx.interval_hours)
    findings = []
    for diff in result.diffs:
        finding = metric_diff_to_finding(diff, min_change_pct)
        if finding:
            findings.append(finding)

    run_a_meta = {
        "run_id": run_a.run_id,
        "path": str(run_a.path),
        "filename": run_a.path.name,
        "server": run_a.ctx.properties.get("server_name"),
        "report_start": run_a.ctx.properties.get("report_start1"),
        "report_end": run_a.ctx.properties.get("report_end1"),
        "interval_hours": run_a.ctx.interval_hours,
    }
    run_b_meta = {
        "run_id": run_b.run_id,
        "path": str(run_b.path),
        "filename": run_b.path.name,
        "server": run_b.ctx.properties.get("server_name"),
        "report_start": run_b.ctx.properties.get("report_start1"),
        "report_end": run_b.ctx.properties.get("report_end1"),
        "interval_hours": run_b.ctx.interval_hours,
    }
    workload_match = build_workload_match(run_a_meta, run_b_meta)

    return {
        "type": "run_comparison",
        "contract": {
            "schema": "run_comparison_v1",
            "schema_version": SCHEMA_VERSION,
        },
        "run_a": run_a_meta,
        "run_b": run_b_meta,
        "run_identity": build_run_identity_pair(run_a_meta, run_b_meta),
        "workload_match": workload_match,
        "confidence_meta": build_confidence_meta(
            changed_params_count=0,
            workload_match_score=workload_match["workload_match_score"],
            isolated_change=False,
            changed_params_threshold=10,
            mode="pair",
        ),
        "influence_rows": [],
        "interval_diff_hours": round(interval_diff, 2),
        "interval_mismatch": interval_diff > 0.01,
        "findings": findings,
        "summary": {
            "total_compared": result.total_compared,
            "significant_count": len(findings),
            "min_change_pct": min_change_pct,
        },
    }


def settings_diff_to_dict(
    *,
    label_a: str,
    label_b: str,
    path_a: Path,
    path_b: Path,
    meta_a: dict[str, str],
    meta_b: dict[str, str],
    diffs: list[Any],
) -> dict[str, Any]:
    from compare_settings import DiffStatus
    from pgprofile_classify import classify_setting_name, split_settings_rows

    critical, informational = split_settings_rows(diffs)
    findings = []
    for row in critical + informational:
        if row.status == DiffStatus.SAME:
            continue
        level = classify_setting_name(row.name)
        findings.append(
            {
                "id": f"settings.{row.status.value.lower()}.{row.name}",
                "category": "settings",
                "severity": "warning" if level.value == "critical" else "info",
                "message": row.name,
                "details": {
                    "status": row.status.value,
                    "value_a": row.nt_value,
                    "value_b": row.prod_value,
                    "issue_level": level.value,
                },
            }
        )

    differ = sum(1 for r in diffs if r.status == DiffStatus.DIFFER)
    only_a = sum(1 for r in diffs if r.status == DiffStatus.ONLY_NT)
    only_b = sum(1 for r in diffs if r.status == DiffStatus.ONLY_PROD)

    run_a_meta = {
        "run_id": label_a,
        "path": str(path_a),
        "filename": path_a.name,
        "meta": meta_a,
        "server": meta_a.get("server"),
        "report_start": meta_a.get("from"),
        "report_end": meta_a.get("to"),
        "interval_hours": None,
    }
    run_b_meta = {
        "run_id": label_b,
        "path": str(path_b),
        "filename": path_b.name,
        "meta": meta_b,
        "server": meta_b.get("server"),
        "report_start": meta_b.get("from"),
        "report_end": meta_b.get("to"),
        "interval_hours": None,
    }
    changed_params = [row for row in diffs if row.status != DiffStatus.SAME]
    workload_match = build_workload_match(run_a_meta, run_b_meta)

    return {
        "type": "settings_diff",
        "contract": {
            "schema": "settings_diff_v1",
            "schema_version": SCHEMA_VERSION,
        },
        "run_a": {
            "run_id": run_a_meta["run_id"],
            "path": run_a_meta["path"],
            "filename": run_a_meta["filename"],
            "meta": run_a_meta["meta"],
        },
        "run_b": {
            "run_id": run_b_meta["run_id"],
            "path": run_b_meta["path"],
            "filename": run_b_meta["filename"],
            "meta": run_b_meta["meta"],
        },
        "run_identity": build_run_identity_pair(run_a_meta, run_b_meta),
        "workload_match": workload_match,
        "confidence_meta": build_confidence_meta(
            changed_params_count=len(changed_params),
            workload_match_score=workload_match["workload_match_score"],
            isolated_change=len(changed_params) == 1,
            changed_params_threshold=10,
            mode="pair",
        ),
        "settings_changes": [
            {
                "parameter": row.name,
                "status": row.status.value,
                "old": row.nt_value,
                "new": row.prod_value,
            }
            for row in changed_params
        ],
        "influence_rows": [],
        "findings": findings,
        "summary": {
            "differ": differ,
            "only_a": only_a,
            "only_b": only_b,
            "total_issues": differ + only_a + only_b,
            "critical_count": len(critical),
            "informational_count": len(informational),
            "settings_valid": len(critical) == 0,
        },
    }
