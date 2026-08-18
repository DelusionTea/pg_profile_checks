#!/usr/bin/env python3
"""Rule-based oracle checks: direction, magnitude sanity, required fields, verdict."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pgprofile_oracle import (  # noqa: E402
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_WARNING,
    evaluate_influence_payload,
    evaluate_output_dir,
    write_oracle_report,
)

PAIR_DIR = ROOT / "analysis_out_test" / "pair_influence_case"
SERIES_DIR = ROOT / "analysis_out_test" / "case_matrix" / "nt_runs_3nt_one_symptom"
results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    results.append((bool(condition), label))


def _base_payload(**overrides: object) -> dict:
    payload = {
        "type": "influence_table",
        "run_identity": {"mode": "pair", "runs": [{"role": "before"}, {"role": "after"}]},
        "workload_match": {"workload_match_score": 0.9, "level": "high"},
        "confidence_meta": {
            "confidence": "medium",
            "evidence_type": "probable",
            "changed_params_count": 1,
            "changed_params_threshold": 10,
            "isolated_change": True,
            "workload_match_score": 0.9,
        },
        "functional_summary": {
            "improved_count": 1,
            "degraded_count": 0,
            "top_improved": [
                {
                    "metric": "wal.wal_size",
                    "direction": "down",
                    "impact": "improved",
                    "delta_pct": -20.0,
                }
            ],
            "top_degraded": [],
        },
        "rows": [
            {
                "parameter": "max_wal_size",
                "old": "1GB",
                "new": "2GB",
                "delta_pct": -20.0,
                "affected_metric": "wal.wal_size",
                "direction": "down",
                "impact": "improved",
                "confidence": "medium",
                "evidence_type": "probable",
                "evidence_count": 1,
                "workload_match_score": 0.9,
                "notes": "ok",
            }
        ],
    }
    payload.update(overrides)
    return payload


def check_happy_path() -> None:
    report = evaluate_influence_payload(_base_payload())
    check(report.verdict == VERDICT_PASS, "consistent row is pass")
    check(not report.to_dict()["reasons"], "pass report has no reasons")


def check_missing_fields() -> None:
    payload = _base_payload()
    payload["rows"][0].pop("impact")
    payload["rows"][0].pop("confidence")
    report = evaluate_influence_payload(payload)
    check(report.verdict == VERDICT_FAIL, "missing impact/confidence is fail")
    check(
        any("missing required fields" in item.message for item in report.checks),
        "fail names the missing fields",
    )


def check_wrong_direction() -> None:
    payload = _base_payload()
    payload["rows"][0]["impact"] = "degraded"
    report = evaluate_influence_payload(payload)
    check(report.verdict == VERDICT_FAIL, "wal_size down marked degraded is fail")
    check(
        any(item.id == "direction.impact" for item in report.checks),
        "fail is tagged as direction.impact",
    )


def check_sign_mismatch() -> None:
    payload = _base_payload()
    payload["rows"][0]["direction"] = "up"
    report = evaluate_influence_payload(payload)
    check(report.verdict == VERDICT_FAIL, "direction up vs negative delta_pct is fail")


def check_false_proven() -> None:
    payload = _base_payload()
    payload["rows"][0]["evidence_type"] = "proven"
    payload["confidence_meta"]["isolated_change"] = False
    payload["confidence_meta"]["changed_params_count"] = 8
    report = evaluate_influence_payload(payload)
    check(report.verdict == VERDICT_FAIL, "proven without isolation is fail")


def check_huge_delta_warning() -> None:
    payload = _base_payload()
    payload["functional_summary"]["top_degraded"] = [
        {
            "metric": "cache.blk_write_time",
            "direction": "up",
            "impact": "degraded",
            "delta_pct": 1600.0,
        }
    ]
    payload["functional_summary"]["degraded_count"] = 1
    report = evaluate_influence_payload(payload)
    check(report.verdict == VERDICT_WARNING, "implausible 1600% delta is warning, not fail")
    check(
        any(item.id == "sanity.summary_delta_magnitude" for item in report.checks),
        "warning is tagged as magnitude sanity",
    )


def check_nan_fail() -> None:
    payload = _base_payload()
    payload["rows"][0]["delta_pct"] = float("nan")
    report = evaluate_influence_payload(payload)
    check(report.verdict == VERDICT_FAIL, "NaN delta_pct is fail")


def check_skipped_without_table() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        report = evaluate_output_dir(Path(tmp))
        check(report.skipped, "missing influence table is skipped")
        check(report.verdict == VERDICT_PASS, "skipped oracle is not a failure")
        path = write_oracle_report(Path(tmp), report)
        payload = json.loads(path.read_text(encoding="utf-8"))
        check(payload["type"] == "oracle_report", "writes oracle_report.json")
        check((Path(tmp) / "oracle_report.md").is_file(), "writes oracle_report.md")


def check_fixtures() -> None:
    pair = evaluate_output_dir(PAIR_DIR)
    check(pair.verdict != VERDICT_FAIL, "pair influence fixture is not a hard fail")
    check("influence_table.json" in pair.sources, "pair fixture is scored from influence_table.json")
    series = evaluate_output_dir(SERIES_DIR)
    check(series.verdict != VERDICT_FAIL, "series influence fixture is not a hard fail")
    check(
        "influence_table_series.json" in series.sources,
        "series fixture is scored from influence_table_series.json",
    )


def main() -> int:
    if not PAIR_DIR.is_dir() or not SERIES_DIR.is_dir():
        print("Missing influence fixtures; run scripts/check_report_cases.py first.")
        return 1
    check_happy_path()
    check_missing_fields()
    check_wrong_direction()
    check_sign_mismatch()
    check_false_proven()
    check_huge_delta_warning()
    check_nan_fail()
    check_skipped_without_table()
    check_fixtures()
    failed = [label for ok, label in results if not ok]
    for ok, label in results:
        print(f"{'PASS' if ok else 'FAIL'}: {label}")
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
