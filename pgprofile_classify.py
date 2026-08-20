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

# Идентичность стенда: эти значения обязаны отличаться между НТ и ПРОМ
# (имя кластера, свой слот репликации, свой каталог архива). Расхождение здесь
# не мешает сравнивать метрики, поэтому в критичные GUC оно не идёт.
SETTINGS_ENV_IDENTITY: frozenset[str] = frozenset(
    {
        "archive_command",
        "archive_library",
        "cluster_name",
        "primary_conninfo",
        "primary_slot_name",
        "restore_command",
        "data_directory",
        "log_directory",
        "log_filename",
        "hba_file",
        "ident_file",
        "config_file",
        "ssl_ca_file",
        "ssl_cert_file",
        "ssl_key_file",
        "ssl_crl_file",
        "unix_socket_directories",
        "stats_temp_directory",
        "sec_admin_default_auth",
        "enabled_extra_auth_methods",
        "performance_insights.directory",
    }
)

SETTINGS_ENV_IDENTITY_PREFIXES: tuple[str, ...] = ("performance_insights.",)

# Postgres только показывает эти значения: задать их в конфиге нельзя.
SETTINGS_READONLY_DERIVED: frozenset[str] = frozenset(
    {
        "shared_memory_size",
        "shared_memory_size_in_huge_pages",
    }
)

# Автоподбираются от shared_buffers, если не заданы явно. Считать их
# отдельными изменениями можно только когда shared_buffers не менялся.
SETTINGS_DERIVED_FROM_SHARED_BUFFERS: frozenset[str] = frozenset(
    {
        "commit_timestamp_buffers",
        "subtransaction_buffers",
        "transaction_buffers",
    }
)

SETTINGS_DERIVED: frozenset[str] = (
    SETTINGS_READONLY_DERIVED | SETTINGS_DERIVED_FROM_SHARED_BUFFERS
)


class SettingIssueLevel(str, Enum):
    CRITICAL = "critical"
    INFORMATIONAL = "informational"


class MetricIssueLevel(str, Enum):
    INFORMATIONAL = "informational"
    WARNING = "warning"


def is_env_identity_setting(name: str) -> bool:
    """Параметр описывает конкретный стенд, а не его настройку."""
    if name in SETTINGS_ENV_IDENTITY:
        return True
    return any(name.startswith(prefix) for prefix in SETTINGS_ENV_IDENTITY_PREFIXES)


def classify_setting_name(name: str) -> SettingIssueLevel:
    if name in SETTINGS_INFORMATIONAL:
        return SettingIssueLevel.INFORMATIONAL
    for prefix in SETTINGS_INFORMATIONAL_PREFIXES:
        if name.startswith(prefix):
            return SettingIssueLevel.INFORMATIONAL
    if is_env_identity_setting(name):
        return SettingIssueLevel.INFORMATIONAL
    return SettingIssueLevel.CRITICAL


def is_tunable_setting(name: str) -> bool:
    """True для GUC, который инженер может осознанно поменять в конфиге."""
    if name in SETTINGS_READONLY_DERIVED:
        return False
    return classify_setting_name(name) is SettingIssueLevel.CRITICAL


def tunable_changed_names(names: list[str] | tuple[str, ...]) -> list[str]:
    """Настраиваемые GUC среди изменившихся параметров.

    Автоподбираемые буферы отбрасываются, когда рядом менялся shared_buffers:
    это одно изменение, а не четыре.
    """
    plain = [str(name) for name in names]
    drop_derived = "shared_buffers" in plain
    out: list[str] = []
    for name in plain:
        if not is_tunable_setting(name):
            continue
        if drop_derived and name in SETTINGS_DERIVED_FROM_SHARED_BUFFERS:
            continue
        out.append(name)
    return out


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
    """Return (critical_rows, informational_rows) for non-SAME diffs.

    Автоподбираемые буферы уходят в справочные, если расходится и shared_buffers:
    иначе одно расхождение конфигурации выглядит как четыре.
    """
    from compare_settings import DiffStatus

    changed = [row for row in rows if row.status != DiffStatus.SAME]
    derived_are_consequence = any(row.name == "shared_buffers" for row in changed)

    critical: list = []
    informational: list = []
    for row in changed:
        is_info = classify_setting_name(row.name) is SettingIssueLevel.INFORMATIONAL
        if not is_info and row.name in SETTINGS_READONLY_DERIVED:
            is_info = True
        if (
            not is_info
            and derived_are_consequence
            and row.name in SETTINGS_DERIVED_FROM_SHARED_BUFFERS
        ):
            is_info = True
        if is_info:
            informational.append(row)
        else:
            critical.append(row)
    return critical, informational
