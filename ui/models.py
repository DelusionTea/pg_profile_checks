"""Shared UI request/result types for PG and JVM adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ReportMeta:
    filename: str
    env: str  # NT | PROD
    label: str
    order: int = 0


@dataclass
class AnalyzeRequest:
    scenario: str  # health | full_multi | symptom | nt_runs | stable_prod | nt_prod
    reports: list[ReportMeta]
    symptoms: list[str] = field(default_factory=list)
    query_hex: str | None = None
    query_id: str | None = None
    query_text: str | None = None
    confluence_title: str | None = None


@dataclass
class AnalyzeResult:
    exit_code: int
    error: str | None
    output_dir: Path
    wiki_path: Path | None
    prompt_path: Path | None
    brief_path: Path | None
    summary: dict[str, Any]
    findings_ui: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class JvmTreeAnswers:
    pods_per_shoulder: int | None = None
    restart_kind: str | None = None
    memory_cause_closed: str | None = None
    heap_growing: str | None = None
    heap_growth_percent: float | None = None
    heap_growth_hours: float | None = None
    growth_of: str | None = None
    gc_ran_in_window: str | None = None
    heap_used_before_gc_mib: int | None = None
    heap_used_after_gc_mib: int | None = None
    oldgen_returned_after_gc: str | None = None
    cpu_throttled: str | None = None
    cpu_pct_limits_shoulder_1: float | None = None
    cpu_pct_limits_shoulder_2: float | None = None
    user_latency_grew: str | None = None
    user_latency_p95_ms: float | None = None
    pauses_coincide_throttle: str | None = None
    post_gc_floor_rising: str | None = None
    gc_cpu_spike_sla: str | None = None


@dataclass
class JvmAnalyzeRequest:
    system_name: str
    pod_name: str | None = None
    container_name: str | None = None
    selected_problems: list[str] = field(default_factory=list)
    tree: JvmTreeAnswers = field(default_factory=JvmTreeAnswers)
    threshold_profile: str = "normal"
    jdk_version: int | None = None
    spring_boot_version: str | None = None
    confluence_title: str | None = None
    heap_used_mib: int | None = None
    heap_committed_mib: int | None = None
    old_gen_used_mib: int | None = None
    old_gen_capacity_mib: int | None = None
    gc_pause_p95_ms: float | None = None
    gc_pause_p99_ms: float | None = None
    gc_time_ratio_percent: float | None = None
    container_memory_usage_percent: float | None = None
    heap_used_percent: float | None = None
    old_gen_used_percent: float | None = None
    new_gen_used_mib: int | None = None
    new_gen_capacity_mib: int | None = None
    new_gen_used_percent: float | None = None
    container_memory_working_set_mib: int | None = None
