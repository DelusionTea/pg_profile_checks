"""Investigate popular DB symptoms using pg_profile report evidence."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, TextIO

import yaml

from pgprofile_health import (
    ReportContext,
    load_report_data,
    load_thresholds,
    parse_setting_int,
    parse_size_to_mb,
    run_checks,
)

DEFAULT_PLAYBOOK = Path(__file__).resolve().parent / "knowledge" / "symptom_playbook.yaml"
DEFAULT_THRESHOLDS = Path(__file__).resolve().parent / "thresholds.yaml"

SYMPTOM_ALIASES: dict[str, str] = {
    "high_cpu": "high_cpu",
    "cpu": "high_cpu",
    "high-cpu": "high_cpu",
    "высокая_утилизация_cpu": "high_cpu",
    "high_memory": "high_memory",
    "memory": "high_memory",
    "high-memory": "high_memory",
    "ram": "high_memory",
    "память": "high_memory",
    "high_wal": "high_wal",
    "wal": "high_wal",
    "high-wal": "high_wal",
    "slow_query": "slow_query",
    "slow-query": "slow_query",
    "slow_sql": "slow_query",
    "query": "slow_query",
    "медленный_запрос": "slow_query",
}

SYMPTOM_TITLES = {
    "high_cpu": "Высокая утилизация CPU БД",
    "high_memory": "Высокое потребление памяти БД",
    "high_wal": "Высокая генерация WAL",
    "slow_query": "Медленная работа запроса",
}


class CauseStatus(str, Enum):
    CONFIRMED = "confirmed"
    SUSPECTED = "suspected"
    POSSIBLE = "possible"
    UNLIKELY = "unlikely"


STATUS_ORDER = {
    CauseStatus.CONFIRMED: 4,
    CauseStatus.SUSPECTED: 3,
    CauseStatus.POSSIBLE: 2,
    CauseStatus.UNLIKELY: 1,
}

STATUS_LABELS = {
    CauseStatus.CONFIRMED: "ПОДТВЕРЖДЕНО (есть данные в отчёте)",
    CauseStatus.SUSPECTED: "ПОДОЗРЕНИЕ (косвенные признаки)",
    CauseStatus.POSSIBLE: "ВОЗМОЖНО (типичная причина, данных мало)",
    CauseStatus.UNLIKELY: "МАЛОВЕРОЯТНО (данные против)",
}


@dataclass
class ReportSnapshot:
    label: str
    path: Path
    ctx: ReportContext
    warnings: list[Any] = field(default_factory=list)


@dataclass
class QueryTarget:
    hexqueryid: str | None = None
    queryid: str | None = None
    query_text: str | None = None

    def describe(self) -> str:
        parts: list[str] = []
        if self.hexqueryid:
            parts.append(f"hex={self.hexqueryid}")
        if self.queryid:
            parts.append(f"queryid={self.queryid}")
        if self.query_text:
            snippet = self.query_text.replace("\n", " ")[:80]
            parts.append(f"text~{snippet!r}")
        return ", ".join(parts) or "не указан"


@dataclass
class CauseHypothesis:
    cause_id: str
    title: str
    description: str
    status: CauseStatus
    evidence: list[str] = field(default_factory=list)
    confirm_actions: list[str] = field(default_factory=list)
    refute_actions: list[str] = field(default_factory=list)
    reports_matched: list[str] = field(default_factory=list)


@dataclass
class SymptomInvestigation:
    symptom: str
    symptom_title: str
    symptom_description: str
    reports: list[ReportSnapshot]
    query_target: QueryTarget | None
    causes: list[CauseHypothesis]
    action_plan: list[str]
    query_matches: list[dict[str, Any]] = field(default_factory=list)


def normalize_symptom(name: str) -> str:
    key = name.strip().lower().replace(" ", "_")
    if key not in SYMPTOM_ALIASES:
        known = ", ".join(sorted(set(SYMPTOM_ALIASES.keys())))
        raise ValueError(f"unknown symptom {name!r}; known: {known}")
    return SYMPTOM_ALIASES[key]


def load_symptom_playbook(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or DEFAULT_PLAYBOOK
    with cfg_path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _query_preview(ctx: ReportContext, hex_id: str | None, max_chars: int = 120) -> str:
    if not hex_id:
        return ""
    text = ctx.queries_by_id.get(hex_id, "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


def _float_val(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_val(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _temp_bytes_mb(db_row: dict[str, Any]) -> float:
    raw = db_row.get("temp_bytes")
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw) / (1024 * 1024)
    return parse_size_to_mb(str(raw)) or 0.0


def _wal_mb_per_hour(ctx: ReportContext) -> float | None:
    wal_bytes = ctx.wal_stats.get("wal_bytes")
    if wal_bytes and ctx.interval_hours > 0:
        return float(wal_bytes) / ctx.interval_hours / (1024 * 1024)
    per_sec = _float_val(ctx.wal_stats.get("wal_bytes_per_sec"))
    if per_sec is not None:
        return per_sec * 3600 / (1024 * 1024)
    return None


def find_query_matches(ctx: ReportContext, target: QueryTarget) -> list[dict[str, Any]]:
    matches: dict[str, dict[str, Any]] = {}

    def add_row(row: dict[str, Any], source: str) -> None:
        hex_id = row.get("hexqueryid")
        if not hex_id:
            return
        if hex_id not in matches:
            matches[hex_id] = {
                "hexqueryid": hex_id,
                "queryid": row.get("queryid"),
                "dbname": row.get("dbname"),
                "username": row.get("username"),
                "sources": [],
                "statement": None,
                "rusage": None,
            }
        matches[hex_id]["sources"].append(source)
        if source == "top_statements":
            matches[hex_id]["statement"] = row
        if source == "top_rusage_statements":
            matches[hex_id]["rusage"] = row

    for row in ctx.top_statements:
        hex_id = row.get("hexqueryid")
        text = ctx.queries_by_id.get(hex_id or "", "")
        if target.hexqueryid and hex_id == target.hexqueryid.lstrip("0x"):
            add_row(row, "top_statements")
            continue
        if target.queryid and str(row.get("queryid")) == str(target.queryid):
            add_row(row, "top_statements")
            continue
        if target.query_text and target.query_text.lower() in text.lower():
            add_row(row, "top_statements")

    for row in ctx.top_rusage_statements:
        hex_id = row.get("hexqueryid")
        text = ctx.queries_by_id.get(hex_id or "", "")
        if target.hexqueryid and hex_id == target.hexqueryid.lstrip("0x"):
            add_row(row, "top_rusage_statements")
            continue
        if target.queryid and str(row.get("queryid")) == str(target.queryid):
            add_row(row, "top_rusage_statements")
            continue
        if target.query_text and target.query_text.lower() in text.lower():
            add_row(row, "top_rusage_statements")

    for item in matches.values():
        hex_id = item["hexqueryid"]
        item["preview"] = _query_preview(ctx, hex_id)
    return list(matches.values())


def _merge_status(current: CauseStatus, new: CauseStatus) -> CauseStatus:
    return current if STATUS_ORDER[current] >= STATUS_ORDER[new] else new


def _evaluate_high_cpu(
    snap: ReportSnapshot,
    thresholds: dict[str, Any],
) -> dict[str, tuple[CauseStatus, list[str]]]:
    ctx = snap.ctx
    cfg = thresholds.get("high_cpu", {})
    results: dict[str, tuple[CauseStatus, list[str]]] = {}
    label = snap.label
    evidence_map: dict[str, list[str]] = {}

    top_cpu = sorted(
        ctx.top_rusage_statements,
        key=lambda r: _float_val(r.get("sum_cpu_time")) or 0.0,
        reverse=True,
    )[: int(cfg.get("top_n", 5))]

    if top_cpu:
        top = top_cpu[0]
        sum_cpu = _float_val(top.get("sum_cpu_time")) or 0.0
        user_pct = _float_val(top.get("user_time_pct")) or 0.0
        hex_id = top.get("hexqueryid")
        preview = _query_preview(ctx, hex_id, 80)
        status = CauseStatus.POSSIBLE
        if sum_cpu >= cfg.get("min_sum_cpu_sec_confirmed", 1200):
            status = CauseStatus.CONFIRMED
        elif sum_cpu >= cfg.get("min_sum_cpu_sec_suspected", 300) or user_pct >= cfg.get(
            "min_user_time_pct", 3.0
        ):
            status = CauseStatus.SUSPECTED
        lines = [
            f"[{label}] Топ CPU: sum_cpu_time={sum_cpu:.1f}s, user_time_pct={user_pct:.1f}%",
            f"[{label}] hex={hex_id}: {preview}",
        ]
        for row in top_cpu[1:3]:
            sc = _float_val(row.get("sum_cpu_time")) or 0.0
            lines.append(
                f"[{label}] #{row.get('ord_cpu_time')}: sum_cpu_time={sc:.1f}s "
                f"hex={row.get('hexqueryid')}"
            )
        evidence_map["cpu.dominant_queries"] = lines
        results["cpu.dominant_queries"] = (status, lines)

    for row in ctx.top_statements:
        calls = _int_val(row.get("calls")) or 0
        mean_ms = _float_val(row.get("mean_exec_time")) or 0.0
        total_sec = _float_val(row.get("total_exec_time")) or 0.0
        if calls >= cfg.get("min_calls_volume", 100000) and total_sec >= cfg.get(
            "min_sum_cpu_sec_suspected", 300
        ):
            hex_id = row.get("hexqueryid")
            lines = [
                f"[{label}] Высокий объём: calls={calls:,}, total_exec_time={total_sec:.1f}s, "
                f"mean={mean_ms:.2f}ms hex={hex_id}",
            ]
            evidence_map.setdefault("cpu.high_call_volume", []).extend(lines)
            results["cpu.high_call_volume"] = (CauseStatus.SUSPECTED, evidence_map["cpu.high_call_volume"])

    jit_rows = [
        r
        for r in ctx.top_statements
        if (_float_val(r.get("jit_total_time")) or 0.0) > 1.0
    ]
    if jit_rows:
        row = max(jit_rows, key=lambda r: _float_val(r.get("jit_total_time")) or 0.0)
        jit_t = _float_val(row.get("jit_total_time")) or 0.0
        lines = [
            f"[{label}] JIT: jit_total_time={jit_t:.2f}s hex={row.get('hexqueryid')}",
        ]
        results["cpu.jit_overhead"] = (CauseStatus.SUSPECTED, lines)

    dead_tables = [
        r
        for r in ctx.top_tbl_last_sample
        if (_float_val(r.get("dead_pct")) or 0.0) > 10.0
    ]
    if dead_tables:
        row = max(dead_tables, key=lambda r: _float_val(r.get("dead_pct")) or 0.0)
        lines = [
            f"[{label}] Bloat: {row.get('schemaname')}.{row.get('relname')} "
            f"dead_pct={row.get('dead_pct')}%",
        ]
        results["cpu.autovacuum_pressure"] = (CauseStatus.SUSPECTED, lines)

    cluster = ctx.cluster_stats
    req = _int_val(cluster.get("checkpoints_req")) or 0
    write_time = _float_val(cluster.get("checkpoint_write_time")) or 0.0
    if req > 5 or write_time > 300:
        lines = [
            f"[{label}] checkpoints_req={req}, checkpoint_write_time={write_time:.1f}s",
        ]
        results["cpu.checkpoint_bgwriter"] = (CauseStatus.SUSPECTED, lines)

    return results


def _evaluate_high_memory(
    snap: ReportSnapshot,
    thresholds: dict[str, Any],
) -> dict[str, tuple[CauseStatus, list[str]]]:
    ctx = snap.ctx
    cfg = thresholds.get("high_memory", {})
    label = snap.label
    results: dict[str, tuple[CauseStatus, list[str]]] = {}

    work_mem = parse_setting_int(ctx.settings.get("work_mem"))
    max_conn = parse_setting_int(ctx.settings.get("max_connections"))
    if work_mem and max_conn:
        gb = work_mem * max_conn / (1024 * 1024)
        if gb >= cfg.get("min_work_mem_connections_gb", 8):
            lines = [f"[{label}] work_mem={work_mem}kB × max_connections={max_conn} ≈ {gb:.1f} GB"]
            status = CauseStatus.CONFIRMED if gb >= 16 else CauseStatus.SUSPECTED
            results["memory.work_mem_risk"] = (status, lines)

    for db in ctx.dbstat:
        temp_mb = _temp_bytes_mb(db)
        if temp_mb >= cfg.get("temp_bytes_mb_suspected", 100):
            lines = [
                f"[{label}] {db.get('dbname')}: temp_bytes={db.get('temp_bytes')}, "
                f"temp_files={db.get('temp_files')}",
            ]
            status = CauseStatus.CONFIRMED if temp_mb >= 500 else CauseStatus.SUSPECTED
            results["memory.temp_spill"] = (status, lines)

    spill_queries = sorted(
        ctx.top_statements,
        key=lambda r: _int_val(r.get("temp_blks_written")) or 0,
        reverse=True,
    )
    if spill_queries:
        row = spill_queries[0]
        temp_blks = _int_val(row.get("temp_blks_written")) or 0
        if temp_blks >= cfg.get("temp_blks_written_suspected", 10000):
            lines = [
                f"[{label}] SQL temp spill: temp_blks_written={temp_blks:,} "
                f"hex={row.get('hexqueryid')}",
            ]
            if "memory.temp_spill" in results:
                prev_status, prev_ev = results["memory.temp_spill"]
                results["memory.temp_spill"] = (
                    _merge_status(prev_status, CauseStatus.SUSPECTED),
                    prev_ev + lines,
                )
            else:
                results["memory.temp_spill"] = (CauseStatus.SUSPECTED, lines)

    for db in ctx.dbstat:
        hit = _float_val(db.get("blks_hit_pct"))
        if hit is not None and hit < 95.0:
            lines = [f"[{label}] {db.get('dbname')}: blks_hit_pct={hit:.2f}%"]
            results["memory.shared_buffers_pressure"] = (CauseStatus.SUSPECTED, lines)

    dead = [
        r for r in ctx.top_tbl_last_sample if (_float_val(r.get("dead_pct")) or 0.0) > 15.0
    ]
    if dead:
        row = max(dead, key=lambda r: _float_val(r.get("dead_pct")) or 0.0)
        lines = [
            f"[{label}] {row.get('schemaname')}.{row.get('relname')}: "
            f"dead_pct={row.get('dead_pct')}%",
        ]
        results["memory.maintenance_bloat"] = (CauseStatus.SUSPECTED, lines)

    for db in ctx.dbstat:
        idle = _float_val(db.get("idle_in_transaction_time")) or 0.0
        if idle > 3600:
            lines = [f"[{label}] {db.get('dbname')}: idle_in_transaction_time={idle:.0f}s"]
            results["memory.idle_in_transaction"] = (CauseStatus.SUSPECTED, lines)

    for db in ctx.dbstat:
        sessions = _int_val(db.get("sessions")) or 0
        if max_conn and sessions > max_conn * 0.7:
            lines = [f"[{label}] {db.get('dbname')}: sessions={sessions}, max_connections={max_conn}"]
            results["memory.connection_leak"] = (CauseStatus.SUSPECTED, lines)

    return results


def _evaluate_high_wal(
    snap: ReportSnapshot,
    thresholds: dict[str, Any],
) -> dict[str, tuple[CauseStatus, list[str]]]:
    ctx = snap.ctx
    cfg = thresholds.get("high_wal", {})
    label = snap.label
    results: dict[str, tuple[CauseStatus, list[str]]] = {}

    mb_h = _wal_mb_per_hour(ctx)
    if mb_h is not None:
        status = CauseStatus.POSSIBLE
        if mb_h >= cfg.get("wal_mb_per_hour_confirmed", 2000):
            status = CauseStatus.CONFIRMED
        elif mb_h >= cfg.get("wal_mb_per_hour_suspected", 500):
            status = CauseStatus.SUSPECTED
        lines = [f"[{label}] WAL generation ≈ {mb_h:.1f} MB/h"]
        results["wal.high_generation_rate"] = (status, lines)

    wal_queries = sorted(
        ctx.top_statements,
        key=lambda r: _int_val(r.get("wal_bytes")) or 0,
        reverse=True,
    )
    if wal_queries:
        row = wal_queries[0]
        wal_b = _int_val(row.get("wal_bytes")) or 0
        wal_gb = wal_b / (1024**3)
        if wal_gb >= cfg.get("wal_heavy_gb_suspected", 1):
            wal_label = row.get("wal_bytes_fmt") or f"{wal_gb:.2f} GB"
            lines = [
                f"[{label}] WAL-heavy SQL: wal_bytes={wal_label} hex={row.get('hexqueryid')}",
            ]
            results["wal.wal_heavy_queries"] = (CauseStatus.SUSPECTED, lines)

    dml_tables = sorted(
        ctx.top_tables,
        key=lambda r: (
            (_int_val(r.get("n_tup_ins")) or 0)
            + (_int_val(r.get("n_tup_upd")) or 0)
            + (_int_val(r.get("n_tup_del")) or 0)
        ),
        reverse=True,
    )
    if dml_tables:
        row = dml_tables[0]
        total_dml = (
            (_int_val(row.get("n_tup_ins")) or 0)
            + (_int_val(row.get("n_tup_upd")) or 0)
            + (_int_val(row.get("n_tup_del")) or 0)
        )
        if total_dml > 100_000:
            lines = [
                f"[{label}] DML table {row.get('schemaname')}.{row.get('relname')}: "
                f"ins={row.get('n_tup_ins')} upd={row.get('n_tup_upd')} del={row.get('n_tup_del')}",
            ]
            results["wal.high_dml_tables"] = (CauseStatus.SUSPECTED, lines)

    wbf = _int_val(ctx.wal_stats.get("wal_buffers_full")) or 0
    if wbf > 0:
        lines = [f"[{label}] wal_buffers_full={wbf}"]
        results["wal.buffers_full"] = (CauseStatus.CONFIRMED, lines)

    req = _int_val(ctx.cluster_stats.get("checkpoints_req")) or 0
    if req > 5:
        lines = [f"[{label}] checkpoints_req={req}"]
        results["wal.checkpoint_pressure"] = (CauseStatus.SUSPECTED, lines)

    max_wal = parse_setting_int(ctx.settings.get("max_wal_size"))
    if max_wal is not None and max_wal < 1024:
        lines = [f"[{label}] max_wal_size={max_wal} (8kB pages)"]
        results["wal.small_max_wal_size"] = (CauseStatus.SUSPECTED, lines)

    return results


def _evaluate_slow_query(
    snap: ReportSnapshot,
    thresholds: dict[str, Any],
    target: QueryTarget,
    matches: list[dict[str, Any]],
) -> dict[str, tuple[CauseStatus, list[str]]]:
    cfg = thresholds.get("slow_query", {})
    label = snap.label
    results: dict[str, tuple[CauseStatus, list[str]]] = {}

    if not matches:
        lines = [f"[{label}] Запрос не найден в top_statements/top_rusage ({target.describe()})"]
        results["query.high_exec_time"] = (CauseStatus.POSSIBLE, lines)
        return results

    for match in matches:
        hex_id = match["hexqueryid"]
        stmt = match.get("statement") or {}
        rus = match.get("rusage") or {}
        preview = match.get("preview") or _query_preview(snap.ctx, hex_id)

        mean_ms = _float_val(stmt.get("mean_exec_time")) or 0.0
        max_ms = _float_val(stmt.get("max_exec_time")) or 0.0
        if mean_ms >= cfg.get("mean_exec_ms_suspected", 100):
            status = (
                CauseStatus.CONFIRMED
                if mean_ms >= cfg.get("mean_exec_ms_confirmed", 1000)
                else CauseStatus.SUSPECTED
            )
            lines = [
                f"[{label}] hex={hex_id}: mean_exec_time={mean_ms:.2f}ms, max={max_ms:.2f}ms",
                f"[{label}] {preview}",
            ]
            results["query.high_exec_time"] = (status, lines)

        io_time = _float_val(stmt.get("io_time")) or 0.0
        reads = _int_val(stmt.get("shared_blks_read")) or _int_val(stmt.get("reads")) or 0
        if io_time >= cfg.get("io_time_ms_suspected", 500) or reads >= cfg.get(
            "shared_blks_read_suspected", 100_000
        ):
            lines = [
                f"[{label}] hex={hex_id}: io_time={io_time:.2f}ms, shared_blks_read={reads:,}",
            ]
            results["query.io_bound"] = (CauseStatus.SUSPECTED, lines)

        temp_blks = _int_val(stmt.get("temp_blks_written")) or 0
        if temp_blks > 0:
            lines = [f"[{label}] hex={hex_id}: temp_blks_written={temp_blks:,}"]
            results["query.temp_spill"] = (CauseStatus.SUSPECTED, lines)

        plan_ms = _float_val(stmt.get("mean_plan_time")) or 0.0
        plans = _int_val(stmt.get("plans")) or 0
        calls = _int_val(stmt.get("calls")) or 0
        if plan_ms > 50 or (plans and calls and plans >= calls * 0.5):
            lines = [f"[{label}] hex={hex_id}: mean_plan_time={plan_ms:.2f}ms, plans={plans}, calls={calls}"]
            results["query.plan_regression"] = (CauseStatus.SUSPECTED, lines)

        sum_cpu = _float_val(rus.get("sum_cpu_time")) or 0.0
        if sum_cpu > 60:
            lines = [f"[{label}] hex={hex_id}: sum_cpu_time={sum_cpu:.1f}s"]
            results["query.cpu_bound"] = (CauseStatus.SUSPECTED, lines)

        wal_b = _int_val(stmt.get("wal_bytes")) or 0
        if wal_b > 100_000_000:
            lines = [f"[{label}] hex={hex_id}: wal_bytes={wal_b:,}"]
            results["query.wal_write_overhead"] = (CauseStatus.SUSPECTED, lines)

        for tbl in snap.ctx.top_tables:
            seq = _int_val(tbl.get("seq_scan")) or 0
            idx = _int_val(tbl.get("idx_scan")) or 0
            if seq > 1000 and (idx == 0 or seq > idx * 5):
                rel = f"{tbl.get('schemaname')}.{tbl.get('relname')}"
                lines = [f"[{label}] Высокий seq_scan на {rel}: seq={seq}, idx={idx}"]
                results["query.seq_scan"] = (CauseStatus.POSSIBLE, lines)
                break

    return results


EVALUATORS: dict[str, Callable[..., dict[str, tuple[CauseStatus, list[str]]]]] = {
    "high_cpu": _evaluate_high_cpu,
    "high_memory": _evaluate_high_memory,
    "high_wal": _evaluate_high_wal,
}


def _build_action_plan(causes: list[CauseHypothesis]) -> list[str]:
    seen: set[str] = set()
    plan: list[str] = []
    for cause in causes:
        for action in cause.confirm_actions:
            key = action.strip()
            if key and key not in seen:
                seen.add(key)
                plan.append(f"[подтвердить {cause.cause_id}] {key}")
        if cause.status in (CauseStatus.CONFIRMED, CauseStatus.SUSPECTED):
            for action in cause.refute_actions[:2]:
                key = action.strip()
                if key and key not in seen:
                    seen.add(key)
                    plan.append(f"[опровергнуть {cause.cause_id}] {key}")
    return plan


def investigate_symptom(
    symptom: str,
    report_paths: list[Path],
    *,
    labels: list[str] | None = None,
    query_target: QueryTarget | None = None,
    playbook_path: Path | None = None,
    thresholds_path: Path | None = None,
    health_thresholds_path: Path | None = None,
) -> SymptomInvestigation:
    symptom_key = normalize_symptom(symptom)
    if symptom_key == "slow_query" and not query_target:
        raise ValueError(
            "slow_query requires query target: --query-hex, --query-id, or --query-text"
        )
    if not query_target:
        query_target = QueryTarget()

    playbook = load_symptom_playbook(playbook_path)
    symptom_cfg = playbook["symptoms"][symptom_key]
    symptom_thresholds = playbook.get("thresholds", {})

    health_cfg = None
    if health_thresholds_path:
        health_cfg = load_thresholds(health_thresholds_path)

    snapshots: list[ReportSnapshot] = []
    for idx, path in enumerate(report_paths):
        label = labels[idx] if labels and idx < len(labels) else path.stem
        ctx = load_report_data(path)
        warnings = run_checks(ctx, health_cfg) if health_cfg else []
        snapshots.append(ReportSnapshot(label=label, path=path, ctx=ctx, warnings=warnings))

    all_matches: list[dict[str, Any]] = []
    if symptom_key == "slow_query":
        for snap in snapshots:
            matches = find_query_matches(snap.ctx, query_target)
            for m in matches:
                m["report_label"] = snap.label
            all_matches.extend(matches)

    merged: dict[str, dict[str, Any]] = {}
    for snap in snapshots:
        if symptom_key == "slow_query":
            per_report = _evaluate_slow_query(
                snap, symptom_thresholds, query_target, find_query_matches(snap.ctx, query_target)
            )
        else:
            per_report = EVALUATORS[symptom_key](snap, symptom_thresholds)

        for cause_id, (status, evidence) in per_report.items():
            bucket = merged.setdefault(
                cause_id,
                {"status": CauseStatus.POSSIBLE, "evidence": [], "reports": []},
            )
            bucket["status"] = _merge_status(bucket["status"], status)
            bucket["evidence"].extend(evidence)
            if snap.label not in bucket["reports"]:
                bucket["reports"].append(snap.label)

    causes: list[CauseHypothesis] = []
    for raw in symptom_cfg.get("causes", []):
        cause_id = raw["id"]
        merged_data = merged.get(cause_id, {"status": CauseStatus.POSSIBLE, "evidence": [], "reports": []})
        status = merged_data["status"]
        if not merged_data["evidence"] and status == CauseStatus.POSSIBLE:
            pass
        elif not merged_data["evidence"] and status != CauseStatus.UNLIKELY:
            status = CauseStatus.POSSIBLE

        causes.append(
            CauseHypothesis(
                cause_id=cause_id,
                title=raw["title"],
                description=raw.get("description", "").strip(),
                status=status,
                evidence=merged_data["evidence"],
                confirm_actions=list(raw.get("confirm_actions", [])),
                refute_actions=list(raw.get("refute_actions", [])),
                reports_matched=merged_data["reports"],
            )
        )

    causes.sort(
        key=lambda c: (STATUS_ORDER[c.status], c.cause_id),
        reverse=True,
    )
    action_plan = _build_action_plan(causes)

    return SymptomInvestigation(
        symptom=symptom_key,
        symptom_title=symptom_cfg.get("title", SYMPTOM_TITLES[symptom_key]),
        symptom_description=symptom_cfg.get("description", "").strip(),
        reports=snapshots,
        query_target=query_target if symptom_key == "slow_query" else None,
        causes=causes,
        action_plan=action_plan,
        query_matches=all_matches,
    )


def build_symptom_brief(
    inv: SymptomInvestigation,
    *,
    max_causes: int = 20,
    max_evidence: int = 5,
) -> str:
    """Text brief for LLM / Confluence prompt (symptom investigation)."""
    summary = {
        "confirmed": sum(1 for c in inv.causes if c.status == CauseStatus.CONFIRMED),
        "suspected": sum(1 for c in inv.causes if c.status == CauseStatus.SUSPECTED),
        "possible": sum(1 for c in inv.causes if c.status == CauseStatus.POSSIBLE),
    }
    lines: list[str] = [
        "# Symptom Investigation Brief",
        "",
        f"symptom: {inv.symptom}",
        f"symptom_title: {inv.symptom_title}",
        f"report_count: {len(inv.reports)}",
        f"confirmed_causes: {summary['confirmed']}",
        f"suspected_causes: {summary['suspected']}",
        f"possible_causes: {summary['possible']}",
        "",
    ]
    if inv.query_target:
        lines.append(f"query_target: {inv.query_target.describe()}")
        lines.append("")

    lines.append("## Reports")
    for snap in inv.reports:
        props = snap.ctx.properties
        lines.append(f"- {snap.label}: {snap.path.name}")
        lines.append(
            f"  interval: {props.get('report_start1')} .. {props.get('report_end1')} "
            f"({snap.ctx.interval_hours:.1f} h)"
        )
    lines.append("")

    if inv.query_matches:
        lines.append("## Matched queries")
        for m in inv.query_matches[:5]:
            lines.append(f"- hex={m['hexqueryid']} db={m.get('dbname')} sources={m.get('sources')}")
            if m.get("preview"):
                lines.append(f"  preview: {m['preview'][:160]}")
        lines.append("")

    lines.append("## Possible causes")
    for cause in inv.causes[:max_causes]:
        lines.append(f"### [{cause.status.value}] {cause.title} ({cause.cause_id})")
        if cause.description:
            lines.append(cause.description.strip().splitlines()[0])
        if cause.reports_matched:
            lines.append(f"- reports: {', '.join(cause.reports_matched)}")
        if cause.evidence:
            lines.append("- evidence:")
            for ev in cause.evidence[:max_evidence]:
                lines.append(f"  - {ev}")
        if cause.confirm_actions:
            lines.append("- confirm:")
            for action in cause.confirm_actions[:4]:
                lines.append(f"  - {action}")
        if cause.refute_actions:
            lines.append("- refute:")
            for action in cause.refute_actions[:3]:
                lines.append(f"  - {action}")
        lines.append("")

    if inv.action_plan:
        lines.append("## Action plan")
        for step in inv.action_plan[:25]:
            lines.append(f"- {step}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def symptom_investigation_to_dict(inv: SymptomInvestigation) -> dict[str, Any]:
    from pgprofile_findings import _json_safe

    return {
        "type": "symptom_investigation",
        "symptom": inv.symptom,
        "symptom_title": inv.symptom_title,
        "symptom_description": inv.symptom_description,
        "query_target": _json_safe(inv.query_target) if inv.query_target else None,
        "reports": [
            {"label": s.label, "path": str(s.path), "filename": s.path.name}
            for s in inv.reports
        ],
        "query_matches": inv.query_matches,
        "causes": [
            {
                "cause_id": c.cause_id,
                "title": c.title,
                "description": c.description,
                "status": c.status.value,
                "evidence": c.evidence,
                "confirm_actions": c.confirm_actions,
                "refute_actions": c.refute_actions,
                "reports_matched": c.reports_matched,
            }
            for c in inv.causes
        ],
        "action_plan": inv.action_plan,
        "summary": {
            "report_count": len(inv.reports),
            "confirmed_count": sum(1 for c in inv.causes if c.status == CauseStatus.CONFIRMED),
            "suspected_count": sum(1 for c in inv.causes if c.status == CauseStatus.SUSPECTED),
        },
    }


def print_symptom_investigation(
    inv: SymptomInvestigation,
    *,
    out: TextIO | None = None,
) -> None:
    stream = out or sys.stdout
    print(f"Symptom investigation: {inv.symptom_title}", file=stream)
    print(f"Symptom id: {inv.symptom}", file=stream)
    if inv.query_target:
        print(f"Query target: {inv.query_target.describe()}", file=stream)
    print(f"Reports: {len(inv.reports)}", file=stream)
    for snap in inv.reports:
        props = snap.ctx.properties
        print(
            f"  [{snap.label}] {snap.path.name} | "
            f"{props.get('report_start1', '?')} .. {props.get('report_end1', '?')} "
            f"({snap.ctx.interval_hours:.1f} h)",
            file=stream,
        )
    print(file=stream)

    if inv.query_matches:
        print("== Matched queries ==", file=stream)
        for m in inv.query_matches[:5]:
            print(f"  hex={m['hexqueryid']} db={m.get('dbname')} [{', '.join(m.get('sources', []))}]", file=stream)
            if m.get("preview"):
                print(f"    {m['preview'][:100]}", file=stream)
        print(file=stream)

    print("== Possible causes ==", file=stream)
    for cause in inv.causes:
        print(
            f"--- [{STATUS_LABELS[cause.status]}] {cause.title} ({cause.cause_id}) ---",
            file=stream,
        )
        if cause.description:
            print(f"  {cause.description.splitlines()[0]}", file=stream)
        if cause.reports_matched:
            print(f"  Reports: {', '.join(cause.reports_matched)}", file=stream)
        if cause.evidence:
            print("  Evidence:", file=stream)
            for line in cause.evidence[:6]:
                print(f"    • {line}", file=stream)
        print("  Confirm:", file=stream)
        for action in cause.confirm_actions[:4]:
            print(f"    + {action}", file=stream)
        print("  Refute:", file=stream)
        for action in cause.refute_actions[:3]:
            print(f"    − {action}", file=stream)
        print(file=stream)

    print("== Action plan (prioritized) ==", file=stream)
    for idx, step in enumerate(inv.action_plan[:20], 1):
        print(f"  {idx}. {step}", file=stream)
    if len(inv.action_plan) > 20:
        print(f"  ... and {len(inv.action_plan) - 20} more steps", file=stream)
    print(file=stream)

    summary = symptom_investigation_to_dict(inv)["summary"]
    print(
        f"Summary: {summary['confirmed_count']} confirmed, "
        f"{summary['suspected_count']} suspected, "
        f"{len(inv.causes)} causes total",
        file=stream,
    )
