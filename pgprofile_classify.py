"""Classify settings and metric diffs: critical vs informational (runtime / volume)."""

from __future__ import annotations

from enum import Enum

from pgprofile_compare import MetricDiff

# Runtime / snapshot metadata — not GUC configuration mismatches.
SETTINGS_INFORMATIONAL: frozenset[str] = frozenset(
    {
        "pg_conf_load_time",
        "pg_postmaster_start_time",
        "in_hot_standby",
        "is_superuser",
        "session_authorization",
        "server_version",
        "server_version_num",
    }
)

SETTINGS_INFORMATIONAL_PREFIXES: tuple[str, ...] = ("pg_stat_",)


class SettingIssueLevel(str, Enum):
    CRITICAL = "critical"
    INFORMATIONAL = "informational"


class MetricIssueLevel(str, Enum):
    INFORMATIONAL = "informational"
    WARNING = "warning"


def classify_setting_name(name: str) -> SettingIssueLevel:
    if name in SETTINGS_INFORMATIONAL:
        return SettingIssueLevel.INFORMATIONAL
    for prefix in SETTINGS_INFORMATIONAL_PREFIXES:
        if name.startswith(prefix):
            return SettingIssueLevel.INFORMATIONAL
    return SettingIssueLevel.CRITICAL


VOLUME_METRIC_KEYS: frozenset[str] = frozenset(
    {
        "wal_bytes",
        "wal_records",
        "wal_write",
        "wal_sync",
        "wal_size",
        "checkpoints_req",
        "checkpoints_timed",
        "buffers_checkpoint",
        "buffers_backend",
        "buffers_clean",
        "buffers_alloc",
        "n_tup_ins",
        "n_tup_upd",
        "n_tup_del",
        "seq_scan",
        "idx_scan",
        "calls",
        "total_time",
        "shared_blks_read",
        "shared_blks_written",
        "temp_blks_written",
        "sessions",
    }
)

VOLUME_SECTIONS: frozenset[str] = frozenset({"dml", "tables"})

VOLUME_QUERY_FIELDS: frozenset[str] = frozenset(
    {"calls", "total_time", "wal_bytes", "shared_blks_read", "temp_blks_written"}
)

DML_OPS: frozenset[str] = frozenset(
    {"INSERT", "UPDATE", "DELETE", "COMMIT", "ROLLBACK", "FETCH"}
)


def classify_metric_diff(diff: MetricDiff) -> MetricIssueLevel:
    if diff.section in VOLUME_SECTIONS:
        return MetricIssueLevel.INFORMATIONAL
    if diff.section == "wal" and diff.key in VOLUME_METRIC_KEYS:
        return MetricIssueLevel.INFORMATIONAL
    if diff.section == "queries" and diff.key in VOLUME_QUERY_FIELDS:
        return MetricIssueLevel.INFORMATIONAL
    if diff.section == "cluster" and diff.key in VOLUME_METRIC_KEYS:
        return MetricIssueLevel.INFORMATIONAL
    if diff.section == "dml" or is_dml_metric_key(diff.key):
        return MetricIssueLevel.INFORMATIONAL
    if diff.section == "sessions":
        for op in DML_OPS:
            if diff.key.endswith(f".{op}") or diff.key.endswith(op):
                return MetricIssueLevel.INFORMATIONAL
    return MetricIssueLevel.WARNING


def is_dml_metric_key(key: str) -> bool:
    return any(key.endswith(f".{op}") for op in DML_OPS)


def split_settings_rows(rows: list) -> tuple[list, list]:
    """Return (critical_rows, informational_rows) for non-SAME diffs."""
    from compare_settings import DiffStatus

    critical: list = []
    informational: list = []
    for row in rows:
        if row.status == DiffStatus.SAME:
            continue
        if classify_setting_name(row.name) is SettingIssueLevel.INFORMATIONAL:
            informational.append(row)
        else:
            critical.append(row)
    return critical, informational
