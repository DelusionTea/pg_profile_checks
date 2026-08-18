#!/usr/bin/env python3
"""Validate noisy/heterogeneous series influence behavior."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _get_row(payload: dict, parameter: str) -> dict:
    for row in payload.get("rows") or []:
        if row.get("parameter") == parameter:
            return row
    raise AssertionError(f"parameter not found: {parameter}")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from pgprofile_influence import build_series_influence_from_nt_runs_dict

    fixtures = root / "resources" / "contract_fixtures"

    stable = build_series_influence_from_nt_runs_dict(
        _load(fixtures / "series_stable_case.json")
    )
    stable_row = _get_row(stable, "max_wal_size")

    noisy = build_series_influence_from_nt_runs_dict(
        _load(fixtures / "series_noisy_case.json")
    )
    noisy_row = _get_row(noisy, "work_mem")

    hetero = build_series_influence_from_nt_runs_dict(
        _load(fixtures / "series_heterogeneous_case.json")
    )
    hetero_max_wal = _get_row(hetero, "max_wal_size")
    hetero_autovac = _get_row(hetero, "autovacuum_naptime")

    magnitude = build_series_influence_from_nt_runs_dict(
        _load(fixtures / "series_noisy_magnitude_case.json")
    )
    magnitude_row = _get_row(magnitude, "max_wal_size")

    conflicting = build_series_influence_from_nt_runs_dict(
        _load(fixtures / "series_conflicting_polarity_case.json")
    )
    conflicting_row = _get_row(conflicting, "checkpoint_completion_target")
    conflicting_delta = conflicting_row.get("delta_pct")

    checks = [
        (
            "stable case reaches proven/high",
            stable_row.get("confidence") == "high"
            and stable_row.get("evidence_type") == "proven",
            stable_row,
        ),
        (
            "stable case has high stability",
            float(stable_row.get("stability_score") or 0) >= 0.7,
            stable_row.get("stability_score"),
        ),
        (
            "noisy case stays probable",
            noisy_row.get("evidence_type") == "probable",
            noisy_row,
        ),
        (
            "noisy case not high confidence",
            noisy_row.get("confidence") in {"low", "medium"},
            noisy_row,
        ),
        (
            "heterogeneous max_wal remains stable",
            hetero_max_wal.get("impact") == "improved"
            and float(hetero_max_wal.get("stability_score") or 0) >= 0.7,
            hetero_max_wal,
        ),
        (
            "heterogeneous autovacuum shows mixed stability",
            float(hetero_autovac.get("stability_score") or 0) < 0.7,
            hetero_autovac,
        ),
        (
            "noisy magnitude is not proven",
            magnitude_row.get("confidence") == "medium"
            and magnitude_row.get("evidence_type") == "probable"
            and float(magnitude_row.get("noise_ratio") or 0) >= 1.0,
            magnitude_row,
        ),
        (
            "conflicting polarity follows the dominant metric",
            conflicting_row.get("impact") == "degraded"
            and conflicting_row.get("affected_metric") == "checkpoint_write_time"
            and isinstance(conflicting_delta, (int, float))
            and float(conflicting_delta) > 0
            and conflicting_row.get("metric_direction") == "up",
            conflicting_row,
        ),
        (
            "series summary mode is statistical",
            stable.get("functional_summary", {}).get("series_mode") == "median+iqr",
            stable.get("functional_summary"),
        ),
    ]

    failed = []
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failed.append((name, detail))
            print(f"  detail: {detail}")

    if failed:
        print("\nSeries influence validation failed:")
        for name, detail in failed:
            print(f"- {name}: {detail}")
        return 1

    print("\nSERIES_INFLUENCE_CASES_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
