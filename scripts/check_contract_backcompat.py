#!/usr/bin/env python3
"""Backward compatibility check for comparison artifacts.

Two guarantees are verified:
1. Artifacts written before the contract blocks existed are still consumable
   (the UI summary builder must not require contract/run_identity/workload_match).
2. Current artifacts keep every field the legacy shape had, so older consumers
   and saved reports do not break on a renamed or dropped key.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "ui") not in sys.path:
    sys.path.insert(0, str(ROOT / "ui"))

FIXTURES_DIR = ROOT / "resources" / "contract_fixtures"
REPORT_A = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html"
REPORT_B = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html"
OUTPUT_DIR = ROOT / "analysis_out_test" / "backcompat_case"

LEGACY_FIXTURES = {
    "run_comparison.json": "legacy_run_comparison.json",
    "settings_diff.json": "legacy_settings_diff.json",
}


def _leaf_paths(payload: Any, prefix: str = "") -> set[str]:
    """Flatten dict keys into dotted paths; list items collapse to the first element."""
    paths: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.add(path)
            paths |= _leaf_paths(value, path)
    elif isinstance(payload, list) and payload:
        paths |= _leaf_paths(payload[0], f"{prefix}[]")
    return paths


def _check_legacy_is_consumable(failures: list[str]) -> None:
    from analysis_runner import _build_summary

    with tempfile.TemporaryDirectory() as tmp:
        legacy_dir = Path(tmp)
        for artifact, fixture in LEGACY_FIXTURES.items():
            shutil.copyfile(FIXTURES_DIR / fixture, legacy_dir / artifact)

        try:
            summary = _build_summary(legacy_dir)
        except Exception as exc:  # noqa: BLE001 - any failure here is a compatibility break
            failures.append(f"legacy artifacts are not consumable: {type(exc).__name__}: {exc}")
            return

    legacy_run_cmp = json.loads(
        (FIXTURES_DIR / LEGACY_FIXTURES["run_comparison.json"]).read_text(encoding="utf-8")
    )
    expected = (legacy_run_cmp.get("summary") or {}).get("significant_count")
    run_cmp = summary.get("run_comparison") or {}
    if run_cmp.get("significant_count") != expected:
        failures.append(
            f"legacy run_comparison.summary.significant_count lost: "
            f"{run_cmp.get('significant_count')!r}, expected {expected!r}"
        )
    if run_cmp.get("workload_match_score") is not None:
        failures.append("legacy artifact without workload_match must report score as None")


def _run_current_pipeline() -> None:
    import analyze_pgprofile

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    saved_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        analyze_pgprofile.main(
            [
                "--report",
                str(REPORT_A),
                "--compare-run",
                str(REPORT_B),
                "--compare-settings",
                str(REPORT_B),
                "--output-dir",
                str(OUTPUT_DIR),
            ]
        )
    finally:
        sys.stdout = saved_stdout


def _check_current_is_superset(failures: list[str]) -> None:
    for artifact, fixture in LEGACY_FIXTURES.items():
        legacy = json.loads((FIXTURES_DIR / fixture).read_text(encoding="utf-8"))
        current = json.loads((OUTPUT_DIR / artifact).read_text(encoding="utf-8"))
        missing = sorted(_leaf_paths(legacy) - _leaf_paths(current))
        if missing:
            failures.append(f"{artifact}: fields dropped since the legacy shape: {missing}")


def _check_current_summary(failures: list[str]) -> None:
    from analysis_runner import _build_summary

    try:
        summary = _build_summary(OUTPUT_DIR)
    except Exception as exc:  # noqa: BLE001 - any failure here is a compatibility break
        failures.append(f"current artifacts are not consumable: {type(exc).__name__}: {exc}")
        return
    influence = summary.get("influence") or {}
    if not influence.get("row_count"):
        failures.append("current pair summary lost influence.row_count")
    oracle = summary.get("oracle") or {}
    if not oracle.get("verdict"):
        failures.append("current pair summary lost oracle.verdict")
    quality = summary.get("quality") or {}
    if not quality.get("verdict"):
        failures.append("current pair summary lost quality.verdict")


def main() -> int:
    for report in (REPORT_A, REPORT_B):
        if not report.is_file():
            print(f"Missing report: {report}")
            return 1

    failures: list[str] = []
    _check_legacy_is_consumable(failures)
    _run_current_pipeline()
    _check_current_is_superset(failures)
    _check_current_summary(failures)

    if failures:
        print("BACKCOMPAT_CHECK_FAILED")
        for item in failures:
            print(f"- {item}")
        return 1

    print(f"Legacy fixtures: {', '.join(sorted(LEGACY_FIXTURES.values()))}")
    print(f"Current artifacts: {OUTPUT_DIR}")
    print("BACKCOMPAT_CHECK_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
