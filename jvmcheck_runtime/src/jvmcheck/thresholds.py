from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_THRESHOLDS = Path(__file__).resolve().parents[2] / "thresholds_jvm.yaml"


@dataclass(frozen=True)
class JvmThresholds:
    gc_pause_p95_ms: float
    gc_pause_p99_ms: float
    gc_time_ratio_percent: float
    old_gen_pressure_ratio: float
    old_gen_critical_ratio: float
    heap_used_to_committed_ratio: float
    memory_limit_pressure_ratio: float
    memory_request_pressure_ratio: float
    request_to_limit_max_ratio: float


def load_thresholds(profile: str = "normal", path: Path | None = None) -> JvmThresholds:
    cfg_path = path or DEFAULT_THRESHOLDS
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}
    profiles = raw.get("profiles") or {}
    selected = profiles.get(profile)
    if selected is None:
        known = ", ".join(sorted(profiles.keys()))
        raise ValueError(f"Unknown JVM threshold profile '{profile}'. Known: {known}")
    merged = {**defaults, **selected}
    return _build_thresholds(merged)


def _build_thresholds(data: dict[str, Any]) -> JvmThresholds:
    return JvmThresholds(
        gc_pause_p95_ms=float(data["gc_pause_p95_ms"]),
        gc_pause_p99_ms=float(data["gc_pause_p99_ms"]),
        gc_time_ratio_percent=float(data["gc_time_ratio_percent"]),
        old_gen_pressure_ratio=float(data["old_gen_pressure_ratio"]),
        old_gen_critical_ratio=float(data["old_gen_critical_ratio"]),
        heap_used_to_committed_ratio=float(data["heap_used_to_committed_ratio"]),
        memory_limit_pressure_ratio=float(data["memory_limit_pressure_ratio"]),
        memory_request_pressure_ratio=float(data["memory_request_pressure_ratio"]),
        request_to_limit_max_ratio=float(data["request_to_limit_max_ratio"]),
    )
