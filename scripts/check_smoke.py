#!/usr/bin/env python3
"""Minimal release smoke pipeline (offline, no live Qwen).

Default: contracts, pair influence, backcompat, LLM units, e2e, oracle/quality.
--full also runs the UI case matrix (nine scenarios, three HTML reports).

Usage:
  python scripts/check_smoke.py
  python scripts/check_smoke.py --full
  python scripts/check_smoke.py --keep-going
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_JUL31 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_07_31_09_00_to=2026_08_01_00.html"
REPORT_AUG11 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html"
REPORT_AUG12 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html"

PAIR_DIR = ROOT / "analysis_out_test" / "pair_influence_case"
E2E_SERIES = ROOT / "analysis_out_test" / "e2e" / "series"
CASE_SERIES = ROOT / "analysis_out_test" / "case_matrix" / "nt_runs_3nt_one_symptom"

# Slow / optional: the nine-scenario UI wiki matrix.
FULL_STEPS = (
    (
        "report_cases",
        [
            "scripts/check_report_cases.py",
            str(REPORT_JUL31),
            str(REPORT_AUG11),
            str(REPORT_AUG12),
        ],
    ),
)

# Order matters: later checks reuse pair_influence_case and e2e/series.
DEFAULT_STEPS = (
    ("knowledge", ["scripts/check_knowledge_consistency.py"]),
    ("contract_fixtures", ["scripts/validate_contract_fixtures.py"]),
    ("series_fixtures", ["scripts/validate_series_influence_cases.py"]),
    ("pair_influence", ["scripts/check_pair_influence_case.py"]),
    ("backcompat", ["scripts/check_contract_backcompat.py"]),
    ("llm_validate", ["scripts/check_llm_validate.py"]),
    ("llm_ui", ["scripts/check_llm_ui.py"]),
    ("e2e", ["scripts/check_e2e.py"]),
    ("confluence_ux", ["scripts/check_confluence_ux.py"]),
    ("jvm_tree", ["scripts/check_jvm_diagnostic_tree.py"]),
    ("jvm_tree_cases", ["scripts/check_jvm_tree_cases.py"]),
    ("oracle", ["scripts/check_oracle.py"]),
    ("oracle_statistical", ["scripts/check_oracle_statistical.py"]),
    ("quality", ["scripts/check_quality_report.py"]),
    ("llm_provider", ["scripts/check_llm_provider_layer.py"]),
    ("llm_policy", ["scripts/check_llm_policy.py"]),
)


def _alias_series_fixture(*, refresh: bool) -> None:
    """Older checks look in case_matrix/; e2e already produced a series dir."""
    if not E2E_SERIES.is_dir():
        return
    if CASE_SERIES.is_dir() and not refresh:
        return
    CASE_SERIES.parent.mkdir(parents=True, exist_ok=True)
    if CASE_SERIES.is_dir():
        shutil.rmtree(CASE_SERIES)
    shutil.copytree(E2E_SERIES, CASE_SERIES)


def _run_step(name: str, argv: list[str]) -> tuple[bool, float, str]:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, *argv],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.monotonic() - started
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, elapsed, output.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release smoke pipeline")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also run scripts/check_report_cases.py on the three bundled reports",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Run remaining steps after a failure",
    )
    args = parser.parse_args(argv)

    missing = [path for path in (REPORT_JUL31, REPORT_AUG11, REPORT_AUG12) if not path.is_file()]
    if missing:
        print("SMOKE_FAILED")
        for path in missing:
            print(f"- missing report: {path}")
        return 1

    steps = list(DEFAULT_STEPS)
    if args.full:
        # Matrix first so case_matrix/nt_runs_3nt_one_symptom is a real UI run, not an e2e copy.
        steps = list(FULL_STEPS) + steps

    failed: list[str] = []
    for name, argv_tail in steps:
        if name in {"oracle", "oracle_statistical", "quality", "llm_provider", "llm_policy"}:
            # Default smoke: copy fresh e2e series so oracle does not score a stale dump.
            # --full already wrote a real UI matrix case; keep that.
            _alias_series_fixture(refresh=not args.full)
        ok, elapsed, output = _run_step(name, argv_tail)
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name} ({elapsed:.1f}s)")
        if not ok:
            failed.append(name)
            for line in output.splitlines()[-20:]:
                print(f"       {line}")
            if not args.keep_going:
                print("SMOKE_FAILED")
                return 1

    if failed:
        print("SMOKE_FAILED")
        print("failed: " + ", ".join(failed))
        return 1

    print(f"pair fixture: {PAIR_DIR}")
    print("SMOKE_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
