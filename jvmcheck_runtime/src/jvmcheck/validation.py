from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jvmcheck.models import RuntimeMetrics


@dataclass
class InputValidationError(ValueError):
    message: str
    file_path: Path | None = None
    line: int | None = None
    column: int | None = None
    hint: str | None = None

    def __str__(self) -> str:
        parts = [self.message]
        if self.file_path:
            parts.append(f"file={self.file_path}")
        if self.line is not None:
            loc = f"line={self.line}"
            if self.column is not None:
                loc += f", column={self.column}"
            parts.append(loc)
        if self.hint:
            parts.append(f"hint={self.hint}")
        return " | ".join(parts)


def validate_runtime_metrics(metrics: RuntimeMetrics) -> list[InputValidationError]:
    errors: list[InputValidationError] = []

    def check_non_negative(value: int | float | None, name: str) -> None:
        if value is None:
            return
        if value < 0:
            errors.append(InputValidationError(f"{name} must be >= 0"))

    check_non_negative(metrics.heap_used_mib, "heap_used_mib")
    check_non_negative(metrics.heap_committed_mib, "heap_committed_mib")
    check_non_negative(metrics.old_gen_used_mib, "old_gen_used_mib")
    check_non_negative(metrics.old_gen_capacity_mib, "old_gen_capacity_mib")
    check_non_negative(metrics.gc_pause_p95_ms, "gc_pause_p95_ms")
    check_non_negative(metrics.gc_pause_p99_ms, "gc_pause_p99_ms")
    check_non_negative(metrics.gc_time_ratio_percent, "gc_time_ratio_percent")
    check_non_negative(
        metrics.container_memory_working_set_mib,
        "container_memory_working_set_mib",
    )

    if (
        metrics.old_gen_used_mib is not None
        and metrics.old_gen_capacity_mib is not None
        and metrics.old_gen_used_mib > metrics.old_gen_capacity_mib
    ):
        errors.append(
            InputValidationError(
                "old_gen_used_mib cannot exceed old_gen_capacity_mib",
                hint="Verify units and snapshot timestamps.",
            )
        )

    if (
        metrics.heap_used_mib is not None
        and metrics.heap_committed_mib is not None
        and metrics.heap_used_mib > metrics.heap_committed_mib
    ):
        errors.append(
            InputValidationError(
                "heap_used_mib cannot exceed heap_committed_mib",
                hint="Verify units from the monitoring source.",
            )
        )
    return errors
