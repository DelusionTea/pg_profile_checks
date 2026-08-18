#!/usr/bin/env python3
"""Statistical oracle: series stability, IQR noise, confidence recalculation."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pgprofile_influence import (  # noqa: E402
    build_series_influence_from_nt_runs_dict,
    recommend_series_confidence,
)
from pgprofile_oracle import (  # noqa: E402
    LAYER_STATISTICAL,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_WARNING,
    evaluate_output_dir,
    evaluate_statistical_payload,
    write_oracle_report,
)

FIXTURES = ROOT / "resources" / "contract_fixtures"
PAIR_DIR = ROOT / "analysis_out_test" / "pair_influence_case"
SERIES_DIR = ROOT / "analysis_out_test" / "case_matrix" / "nt_runs_3nt_one_symptom"
results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    results.append((bool(condition), label))


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _row(payload: dict, parameter: str) -> dict:
    for item in payload.get("rows") or []:
        if item.get("parameter") == parameter:
            return item
    raise AssertionError(f"parameter not found: {parameter}")


def check_builder_confidence() -> None:
    stable = build_series_influence_from_nt_runs_dict(_load("series_stable_case.json"))
    stable_row = _row(stable, "max_wal_size")
    check(
        stable_row.get("confidence") == "high"
        and stable_row.get("evidence_type") == "proven",
        "stable series reaches high/proven",
    )
    check(
        float(stable_row.get("stability_score") or 0) >= 0.7,
        "stable series has high stability_score",
    )

    noisy = build_series_influence_from_nt_runs_dict(_load("series_noisy_case.json"))
    noisy_row = _row(noisy, "work_mem")
    check(
        noisy_row.get("confidence") != "high"
        and noisy_row.get("evidence_type") == "probable",
        "sign-flipping series stays below high/proven",
    )

    hetero = build_series_influence_from_nt_runs_dict(
        _load("series_heterogeneous_case.json")
    )
    max_wal = _row(hetero, "max_wal_size")
    autovac = _row(hetero, "autovacuum_naptime")
    check(
        max_wal.get("impact") == "improved"
        and float(max_wal.get("stability_score") or 0) >= 0.7,
        "heterogeneous max_wal_size stays stable",
    )
    check(
        float(autovac.get("stability_score") or 0) < 0.7,
        "heterogeneous autovacuum_naptime is unstable",
    )

    magnitude = build_series_influence_from_nt_runs_dict(
        _load("series_noisy_magnitude_case.json")
    )
    mag_row = _row(magnitude, "max_wal_size")
    rec = recommend_series_confidence(mag_row)
    check(rec["noisy"], "same-sign but huge IQR is marked noisy")
    check(
        mag_row.get("confidence") == "medium"
        and mag_row.get("evidence_type") == "probable",
        "noisy magnitude is capped at medium/probable, not proven",
    )


def check_statistical_oracle() -> None:
    stable = build_series_influence_from_nt_runs_dict(_load("series_stable_case.json"))
    report = evaluate_statistical_payload(stable, source="stable")
    check(report.verdict == VERDICT_PASS, "stable series statistical oracle is pass")
    check(report.layer == LAYER_STATISTICAL, "statistical layer is tagged")
    check(not report.confidence_adjustments, "stable series needs no confidence downgrade")

    magnitude = build_series_influence_from_nt_runs_dict(
        _load("series_noisy_magnitude_case.json")
    )
    noisy_report = evaluate_statistical_payload(magnitude, source="magnitude")
    check(
        noisy_report.verdict == VERDICT_WARNING,
        "noisy-but-honest confidence is warning, not fail",
    )
    check(
        any(item.id == "statistical.noise" for item in noisy_report.checks),
        "noise warning is tagged as statistical.noise",
    )

    hetero = build_series_influence_from_nt_runs_dict(
        _load("series_heterogeneous_case.json")
    )
    hetero_report = evaluate_statistical_payload(hetero, source="hetero")
    check(
        hetero_report.verdict in {VERDICT_PASS, VERDICT_WARNING},
        "heterogeneous series is not a hard fail",
    )
    check(
        any(
            item.id == "statistical.stability_consistency"
            and item.status == VERDICT_WARNING
            for item in hetero_report.checks
        ),
        "mixed pair effects on autovacuum produce a stability warning",
    )


def check_overconfident_fail() -> None:
    payload = build_series_influence_from_nt_runs_dict(
        _load("series_noisy_magnitude_case.json")
    )
    payload["rows"][0]["confidence"] = "high"
    payload["rows"][0]["evidence_type"] = "proven"
    report = evaluate_statistical_payload(payload, source="overconfident")
    check(report.verdict == VERDICT_FAIL, "overconfident noisy row is fail")
    check(
        any(item.id == "statistical.confidence_recalc" for item in report.checks),
        "fail is tagged as confidence recalculation",
    )
    check(
        report.confidence_adjustments
        and report.confidence_adjustments[0]["to"] == "medium",
        "adjustment records high → medium",
    )


def check_output_dirs() -> None:
    pair = evaluate_output_dir(PAIR_DIR)
    stat_layers = [item for item in pair.layers if item.get("name") == LAYER_STATISTICAL]
    check(stat_layers and stat_layers[0].get("skipped"), "pair-only run skips statistical layer")
    check(pair.verdict != VERDICT_FAIL, "pair-only hybrid oracle is not a hard fail")

    series = evaluate_output_dir(SERIES_DIR)
    check(series.verdict != VERDICT_FAIL, "real 3-NT series oracle is not a hard fail")
    check(
        any(item.get("name") == LAYER_STATISTICAL and not item.get("skipped") for item in series.layers),
        "real 3-NT series runs the statistical layer",
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = write_oracle_report(Path(tmp), series)
        payload = json.loads(path.read_text(encoding="utf-8"))
        check("layers" in payload, "oracle_report.json includes layers")
        check(
            "confidence_adjustments" in payload,
            "oracle_report.json includes confidence_adjustments",
        )


def main() -> int:
    if not PAIR_DIR.is_dir() or not SERIES_DIR.is_dir():
        print("Missing influence fixtures; run scripts/check_report_cases.py first.")
        return 1
    check_builder_confidence()
    check_statistical_oracle()
    check_overconfident_fail()
    check_output_dirs()
    failed = [label for ok, label in results if not ok]
    for ok, label in results:
        print(f"{'PASS' if ok else 'FAIL'}: {label}")
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
