from __future__ import annotations

from jvmcheck.models import AnalysisResult, ContainerResources, Finding, RuntimeMetrics
from jvmcheck.thresholds import JvmThresholds, load_thresholds


def analyze_jvm_health(
    container: ContainerResources,
    metrics: RuntimeMetrics,
    tuning_failed_after_previous_attempt: bool = False,
    threshold_set: JvmThresholds | None = None,
) -> AnalysisResult:
    threshold_set = threshold_set or load_thresholds("normal")
    findings: list[Finding] = []
    flags = _normalize_flags(container.java_tool_options)
    key_values = _extract_key_value_flags(flags)

    if _is_long_gc_p95(metrics, threshold_set):
        findings.append(
            Finding(
                code="gc.long_pause_p95",
                severity="high",
                message="GC p95 pause exceeds threshold.",
                threshold=f"gc_pause_p95_ms > {threshold_set.gc_pause_p95_ms}",
                evidence={"gc_pause_p95_ms": str(metrics.gc_pause_p95_ms)},
                details={
                    "gc_pause_p95_ms": str(metrics.gc_pause_p95_ms),
                },
            )
        )

    if _is_long_gc_p99(metrics, threshold_set):
        findings.append(
            Finding(
                code="gc.long_pause_p99",
                severity="high",
                message="GC p99 pause exceeds threshold.",
                threshold=f"gc_pause_p99_ms > {threshold_set.gc_pause_p99_ms}",
                evidence={"gc_pause_p99_ms": str(metrics.gc_pause_p99_ms)},
                details={
                    "gc_pause_p99_ms": str(metrics.gc_pause_p99_ms),
                },
            )
        )

    if _is_gc_time_ratio_high(metrics, threshold_set):
        findings.append(
            Finding(
                code="gc.high_time_ratio",
                severity="high",
                message="GC time ratio exceeds threshold.",
                threshold=f"gc_time_ratio_percent > {threshold_set.gc_time_ratio_percent}",
                evidence={"gc_time_ratio_percent": str(metrics.gc_time_ratio_percent)},
                details={
                    "gc_time_ratio_percent": str(metrics.gc_time_ratio_percent),
                },
            )
        )

    if _old_gen_ratio(metrics) >= threshold_set.old_gen_pressure_ratio:
        findings.append(
            Finding(
                code="heap.old_gen_pressure",
                severity="high",
                message="OldGen occupancy is high and indicates heap pressure risk.",
                threshold=f"old_gen_ratio >= {threshold_set.old_gen_pressure_ratio:.2f}",
                evidence={"old_gen_ratio": f"{_old_gen_ratio(metrics):.3f}"},
                details={
                    "old_gen_used_mib": str(metrics.old_gen_used_mib),
                    "old_gen_capacity_mib": str(metrics.old_gen_capacity_mib),
                },
            )
        )

    if _old_gen_ratio(metrics) >= threshold_set.old_gen_critical_ratio:
        findings.append(
            Finding(
                code="heap.old_gen_critical",
                severity="critical",
                message="OldGen occupancy is at critical saturation level.",
                threshold=f"old_gen_ratio >= {threshold_set.old_gen_critical_ratio:.2f}",
                evidence={"old_gen_ratio": f"{_old_gen_ratio(metrics):.3f}"},
                details={
                    "old_gen_used_mib": str(metrics.old_gen_used_mib),
                    "old_gen_capacity_mib": str(metrics.old_gen_capacity_mib),
                },
            )
        )

    if _heap_used_to_committed(metrics) >= threshold_set.heap_used_to_committed_ratio:
        findings.append(
            Finding(
                code="heap.high_used_to_committed",
                severity="warning",
                message="Heap used-to-committed ratio is high.",
                threshold=f"heap_used/heap_committed >= {threshold_set.heap_used_to_committed_ratio:.2f}",
                evidence={"heap_used_to_committed_ratio": f"{_heap_used_to_committed(metrics):.3f}"},
                details={
                    "heap_used_mib": str(metrics.heap_used_mib),
                    "heap_committed_mib": str(metrics.heap_committed_mib),
                },
            )
        )

    if _memory_limit_ratio(container, metrics) >= threshold_set.memory_limit_pressure_ratio:
        findings.append(
            Finding(
                code="memory.limit_pressure",
                severity="critical",
                message="Container memory consumption is close to memory limit.",
                threshold=f"working_set/limit >= {threshold_set.memory_limit_pressure_ratio:.2f}",
                evidence={"memory_limit_ratio": f"{_memory_limit_ratio(container, metrics):.3f}"},
                details={
                    "container_memory_working_set_mib": str(metrics.container_memory_working_set_mib),
                    "container_memory_limit_mib": str(container.limits.memory_mib),
                },
            )
        )

    if _memory_request_ratio(container, metrics) >= threshold_set.memory_request_pressure_ratio:
        findings.append(
            Finding(
                code="memory.request_pressure",
                severity="warning",
                message="Working set significantly exceeds memory request.",
                threshold=f"working_set/request >= {threshold_set.memory_request_pressure_ratio:.2f}",
                evidence={"memory_request_ratio": f"{_memory_request_ratio(container, metrics):.3f}"},
                details={
                    "container_memory_working_set_mib": str(metrics.container_memory_working_set_mib),
                    "container_memory_request_mib": str(container.requests.memory_mib),
                },
            )
        )

    if _request_to_limit_ratio(container) >= threshold_set.request_to_limit_max_ratio:
        findings.append(
            Finding(
                code="container.request_limit_skew",
                severity="warning",
                message="Container memory request is too close to memory limit.",
                threshold=f"request/limit >= {threshold_set.request_to_limit_max_ratio:.2f}",
                evidence={"request_to_limit_ratio": f"{_request_to_limit_ratio(container):.3f}"},
                details={
                    "container_memory_request_mib": str(container.requests.memory_mib),
                    "container_memory_limit_mib": str(container.limits.memory_mib),
                },
            )
        )

    if not container.limits.memory_mib:
        findings.append(
            Finding(
                code="container.missing_memory_limit",
                severity="high",
                message="Container memory limit is missing.",
                details={},
            )
        )

    if "-XX:+UseContainerSupport" not in flags:
        findings.append(
            Finding(
                code="jvm.flag_missing_container_support",
                severity="warning",
                message="JVM flag -XX:+UseContainerSupport is not explicitly configured.",
                details={},
            )
        )

    if "-XX:+ExitOnOutOfMemoryError" not in flags:
        findings.append(
            Finding(
                code="jvm.flag_missing_exit_on_oom",
                severity="warning",
                message="JVM flag -XX:+ExitOnOutOfMemoryError is not configured.",
                details={},
            )
        )

    if _has_duplicates(flags):
        findings.append(
            Finding(
                code="jvm.flag_duplicate",
                severity="warning",
                message="Duplicate JVM flags detected.",
                details={"duplicates": ", ".join(sorted(_duplicate_keys(flags)))},
            )
        )

    if _has_ram_percentage_conflict(key_values):
        findings.append(
            Finding(
                code="jvm.flag_conflict_maxram",
                severity="high",
                message="InitialRAMPercentage must be lower than MaxRAMPercentage.",
                details={
                    "InitialRAMPercentage": key_values.get("-XX:InitialRAMPercentage", ""),
                    "MaxRAMPercentage": key_values.get("-XX:MaxRAMPercentage", ""),
                },
            )
        )

    if not any(flag.startswith("-XX:+Use") and "GC" in flag for flag in flags):
        findings.append(
            Finding(
                code="jvm.gc_not_declared",
                severity="info",
                message="No explicit GC policy flag found.",
                details={},
            )
        )

    lifecycle_status = "tuning_not_effective" if tuning_failed_after_previous_attempt else "tuning_attempted"
    if tuning_failed_after_previous_attempt and findings:
        lifecycle_status = "escalate_to_dev_memory_dump"

    return AnalysisResult(findings=findings, lifecycle_status=lifecycle_status)


def _is_long_gc_p95(metrics: RuntimeMetrics, thresholds: JvmThresholds) -> bool:
    p95 = metrics.gc_pause_p95_ms or 0.0
    return p95 > thresholds.gc_pause_p95_ms


def _is_long_gc_p99(metrics: RuntimeMetrics, thresholds: JvmThresholds) -> bool:
    p99 = metrics.gc_pause_p99_ms or 0.0
    return p99 > thresholds.gc_pause_p99_ms


def _is_gc_time_ratio_high(metrics: RuntimeMetrics, thresholds: JvmThresholds) -> bool:
    ratio = metrics.gc_time_ratio_percent or 0.0
    return ratio > thresholds.gc_time_ratio_percent


def _old_gen_ratio(metrics: RuntimeMetrics) -> float:
    used = metrics.old_gen_used_mib or 0
    cap = metrics.old_gen_capacity_mib or 0
    if cap <= 0:
        return 0.0
    return used / cap


def _heap_used_to_committed(metrics: RuntimeMetrics) -> float:
    used = metrics.heap_used_mib or 0
    committed = metrics.heap_committed_mib or 0
    if committed <= 0:
        return 0.0
    return used / committed


def _memory_limit_ratio(container: ContainerResources, metrics: RuntimeMetrics) -> float:
    limit = container.limits.memory_mib or 0
    used = metrics.container_memory_working_set_mib or 0
    if limit <= 0:
        return 0.0
    return used / limit


def _memory_request_ratio(container: ContainerResources, metrics: RuntimeMetrics) -> float:
    req = container.requests.memory_mib or 0
    used = metrics.container_memory_working_set_mib or 0
    if req <= 0:
        return 0.0
    return used / req


def _request_to_limit_ratio(container: ContainerResources) -> float:
    req = container.requests.memory_mib or 0
    limit = container.limits.memory_mib or 0
    if limit <= 0:
        return 0.0
    return req / limit


def _normalize_flags(flags: list[str]) -> list[str]:
    return [str(flag).strip() for flag in flags if str(flag).strip()]


def _extract_key_value_flags(flags: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for flag in flags:
        if "=" not in flag:
            continue
        key, value = flag.split("=", 1)
        try:
            out[key] = float(value)
        except ValueError:
            continue
    return out


def _duplicate_keys(flags: list[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for flag in flags:
        key = flag.split("=", 1)[0]
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return duplicates


def _has_duplicates(flags: list[str]) -> bool:
    return bool(_duplicate_keys(flags))


def _has_ram_percentage_conflict(values: dict[str, float]) -> bool:
    init = values.get("-XX:InitialRAMPercentage")
    maxp = values.get("-XX:MaxRAMPercentage")
    if init is None or maxp is None:
        return False
    return init >= maxp

