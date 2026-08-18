#!/usr/bin/env python3
"""Integration check: pair-mode influence artifacts on two real pg_profile reports.

Runs the same code path as the CLI (`--compare-run` + `--compare-settings`) and asserts
the influence contract, the functional summary arithmetic and the short summary export.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_A = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html"
REPORT_B = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html"
OUTPUT_DIR = ROOT / "analysis_out_test" / "pair_influence_case"

REQUIRED_ROW_FIELDS = (
    "parameter",
    "old",
    "new",
    "delta_pct",
    "affected_metric",
    "direction",
    "impact",
    "confidence",
    "evidence_type",
    "evidence_count",
    "workload_match_score",
    "notes",
    "metric_direction",
)


def _run_pipeline() -> None:
    import analyze_pgprofile

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    argv = [
        "--report",
        str(REPORT_A),
        "--compare-run",
        str(REPORT_B),
        "--compare-settings",
        str(REPORT_B),
        "--output-dir",
        str(OUTPUT_DIR),
    ]
    saved_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        exit_code = analyze_pgprofile.main(argv)
    finally:
        sys.stdout = saved_stdout
    if exit_code not in (0, 1):
        raise AssertionError(f"pipeline failed with exit code {exit_code}")


def _check_contract(failures: list[str]) -> None:
    from pgprofile_contracts import validate_contract_payload

    for name in ("run_comparison.json", "settings_diff.json", "influence_table.json"):
        payload = json.loads((OUTPUT_DIR / name).read_text(encoding="utf-8"))
        try:
            validate_contract_payload(payload)
        except ValueError as exc:
            failures.append(f"{name}: contract validation failed: {exc}")


def _check_rows(failures: list[str]) -> list[dict]:
    from pgprofile_contracts import CONFIDENCE_LEVELS, EVIDENCE_TYPES, IMPACT_KINDS

    payload = json.loads((OUTPUT_DIR / "influence_table.json").read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    if not rows:
        failures.append("influence_table.json: no rows for a real pair of reports")
        return []

    for row in rows:
        param = row.get("parameter") or "<empty>"
        missing = [field for field in REQUIRED_ROW_FIELDS if field not in row]
        if missing:
            failures.append(f"influence row {param}: missing fields {missing}")
        if row.get("impact") not in IMPACT_KINDS:
            failures.append(f"influence row {param}: bad impact {row.get('impact')!r}")
        if row.get("confidence") not in CONFIDENCE_LEVELS:
            failures.append(f"influence row {param}: bad confidence {row.get('confidence')!r}")
        if row.get("evidence_type") not in EVIDENCE_TYPES:
            failures.append(f"influence row {param}: bad evidence_type {row.get('evidence_type')!r}")
        if not isinstance(row.get("workload_match_score"), (int, float)):
            failures.append(f"influence row {param}: workload_match_score is not numeric")
        if row.get("evidence_count") not in (0, 1):
            failures.append(
                f"influence row {param}: pair evidence_count must be 0 or 1, "
                f"got {row.get('evidence_count')!r}"
            )
        if row.get("metric_direction") not in {"up", "down", "flat"}:
            failures.append(
                f"influence row {param}: bad metric_direction {row.get('metric_direction')!r}"
            )
        if not str(row.get("affected_metric") or "").strip():
            # Unattributed rows must not claim an effect.
            if row.get("impact") != "neutral":
                failures.append(
                    f"influence row {param}: no affected metric but impact={row.get('impact')!r}"
                )
            if row.get("delta_pct") is not None:
                failures.append(f"influence row {param}: no affected metric but delta_pct is set")
            if row.get("evidence_count"):
                failures.append(
                    f"influence row {param}: no affected metric but evidence_count="
                    f"{row.get('evidence_count')}"
                )
        elif row.get("evidence_count") != 1:
            failures.append(
                f"influence row {param}: attributed row must have evidence_count=1, "
                f"got {row.get('evidence_count')!r}"
            )

    functional = payload.get("functional_summary") or {}
    total = functional.get("total_metrics_analyzed")
    parts = (
        functional.get("improved_count", 0)
        + functional.get("degraded_count", 0)
        + functional.get("neutral_count", 0)
    )
    if total != parts:
        failures.append(
            f"functional_summary: improved+degraded+neutral={parts} does not match total={total}"
        )
    return rows


def _check_csv(rows: list[dict], failures: list[str]) -> None:
    text = (OUTPUT_DIR / "influence_table.csv").read_text(encoding="utf-8")
    parsed = list(csv.DictReader(io.StringIO(text)))
    if len(parsed) != len(rows):
        failures.append(f"influence_table.csv: {len(parsed)} rows, expected {len(rows)}")
    header = parsed[0].keys() if parsed else []
    missing = [field for field in REQUIRED_ROW_FIELDS if field not in header]
    if missing:
        failures.append(f"influence_table.csv: missing columns {missing}")


def _check_summary_export(rows: list[dict], failures: list[str]) -> None:
    markdown = (OUTPUT_DIR / "influence_summary.md").read_text(encoding="utf-8")
    wiki = (OUTPUT_DIR / "influence_summary.wiki").read_text(encoding="utf-8")

    for required in ("Сопоставимость нагрузки", "Достоверность", "Правила трактовки", "Есть связь"):
        if required not in markdown:
            failures.append(f"influence_summary.md: missing section {required!r}")
        if required not in wiki:
            failures.append(f"influence_summary.wiki: missing section {required!r}")
    if "Наблюдений" in markdown:
        failures.append("influence_summary.md: pair count column is still labelled Наблюдений")
    if "Наблюдений" in wiki:
        failures.append("influence_summary.wiki: pair count column is still labelled Наблюдений")

    linked = [row for row in rows if str(row.get("affected_metric") or "").strip()]
    unlinked = [row for row in rows if not str(row.get("affected_metric") or "").strip()]
    for row in linked:
        if row["parameter"] not in markdown:
            failures.append(f"influence_summary.md: linked parameter {row['parameter']} is missing")
    if unlinked and "связь с метриками неизвестна" not in markdown:
        failures.append("influence_summary.md: unlinked parameters are not reported")

    for line in wiki.splitlines():
        if line.startswith("|") and not line.startswith("||"):
            # Escaped pipes keep Confluence table columns aligned.
            if line.count("|") - 1 != 11:
                failures.append(f"influence_summary.wiki: row has wrong column count: {line}")

    oracle_path = OUTPUT_DIR / "oracle_report.json"
    if not oracle_path.is_file():
        failures.append("oracle_report.json is missing")
    else:
        oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
        if oracle.get("type") != "oracle_report":
            failures.append("oracle_report.json: unexpected type")
        if oracle.get("verdict") == "fail":
            failures.append("oracle_report.json: pair fixture must not be a hard fail")

    quality_path = OUTPUT_DIR / "quality_report.json"
    if not quality_path.is_file():
        failures.append("quality_report.json is missing")
    else:
        quality = json.loads(quality_path.read_text(encoding="utf-8"))
        if quality.get("type") != "quality_report":
            failures.append("quality_report.json: unexpected type")
        if not quality.get("confidence_trail"):
            failures.append("quality_report.json: confidence_trail is empty")
        if not (OUTPUT_DIR / "quality_report.md").is_file():
            failures.append("quality_report.md is missing")


def main() -> int:
    for report in (REPORT_A, REPORT_B):
        if not report.is_file():
            print(f"Missing report: {report}")
            return 1

    _run_pipeline()

    failures: list[str] = []
    _check_contract(failures)
    rows = _check_rows(failures)
    if rows:
        _check_csv(rows, failures)
        _check_summary_export(rows, failures)

    if failures:
        print("PAIR_INFLUENCE_CHECK_FAILED")
        for item in failures:
            print(f"- {item}")
        return 1

    print(f"Checked pair influence artifacts in {OUTPUT_DIR}")
    print(f"  rows={len(rows)}")
    print("PAIR_INFLUENCE_CHECK_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
