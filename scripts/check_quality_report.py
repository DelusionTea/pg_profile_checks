#!/usr/bin/env python3
"""Unified quality report: layers, confidence trail, CLI markdown."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pgprofile_quality import (  # noqa: E402
    build_quality_report,
    evaluate_quality,
    format_quality_markdown,
    persist_quality_snapshot,
    write_quality_report,
)
from pgprofile_oracle import LAYER_LLM, evaluate_output_dir, replace_llm_layer  # noqa: E402

PAIR_DIR = ROOT / "analysis_out_test" / "pair_influence_case"
SERIES_DIR = ROOT / "analysis_out_test" / "case_matrix" / "nt_runs_3nt_one_symptom"
results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    results.append((bool(condition), label))


def main() -> int:
    if not PAIR_DIR.is_dir() or not SERIES_DIR.is_dir():
        print("Missing influence fixtures.")
        return 1

    pair = build_quality_report(PAIR_DIR)
    check(pair.get("type") == "quality_report", "pair payload type is quality_report")
    check(pair.get("verdict") != "fail", "pair quality is not a hard fail")
    check(any(item.get("name") == "rule_based" for item in pair.get("layers") or []), "pair report lists rule_based layer")
    trail = pair.get("confidence_trail") or []
    check(any(item.get("parameter") == "*" for item in trail), "pair trail has a run-level confidence row")
    check(
        any(item.get("parameter") == "shared_buffers" for item in trail),
        "pair trail includes shared_buffers",
    )
    check(
        any(
            item.get("parameter") != "*" and item.get("change") == "downgrade"
            for item in trail
        ),
        "unattributed pair rows are recorded as confidence downgrade",
    )
    md = format_quality_markdown(pair)
    check("Качество:" in md, "markdown has the quality heading")
    check("## Confidence" in md, "markdown has a Confidence section")
    check("## Слои" in md, "markdown has a layers section")

    series = build_quality_report(SERIES_DIR)
    check(series.get("verdict") != "fail", "series quality is not a hard fail")
    check(
        any(item.get("name") == "statistical" for item in series.get("layers") or []),
        "series report lists statistical layer",
    )
    check(
        any(item.get("parameter") == "max_wal_size" for item in series.get("confidence_trail") or []),
        "series trail includes max_wal_size",
    )

    path = write_quality_report(PAIR_DIR)
    check(path.name == "quality_report.json", "write_quality_report returns quality_report.json")
    check((PAIR_DIR / "quality_report.md").is_file(), "writes quality_report.md")

    proc = subprocess.run(
        [sys.executable, str(ROOT / "run_quality.py"), "--output-dir", str(PAIR_DIR)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    check(proc.returncode == 0, "run_quality.py exits 0 on the pair fixture")
    check("Качество:" in proc.stdout, "run_quality.py prints markdown")
    check("Confidence" in proc.stdout, "run_quality.py prints the confidence trail")

    json_proc = subprocess.run(
        [sys.executable, str(ROOT / "run_quality.py"), "--output-dir", str(PAIR_DIR), "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(json_proc.stdout)
    check(payload.get("type") == "quality_report", "run_quality.py --json emits quality_report")

    from analyze_pgprofile import build_parser, quality_fail_exit

    ns = build_parser().parse_args(
        ["--output-dir", str(PAIR_DIR), "--exit-code-quality"]
    )
    check(ns.exit_code_quality is True, "--exit-code-quality is a recognized flag")
    check(ns.exit_code is False, "--exit-code-quality does not imply --exit-code")
    check(not quality_fail_exit(PAIR_DIR), "pair fixture does not trip --exit-code-quality")
    with tempfile.TemporaryDirectory() as tmp:
        fail_dir = Path(tmp)
        (fail_dir / "quality_report.json").write_text(
            json.dumps({"type": "quality_report", "verdict": "fail"}),
            encoding="utf-8",
        )
        check(quality_fail_exit(fail_dir), "fail quality_report trips --exit-code-quality")
    quality_proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "run_quality.py"),
            "--output-dir",
            str(PAIR_DIR),
            "--exit-code",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    check(quality_proc.returncode == 0, "run_quality.py --exit-code is 0 on the pair fixture")

    snap = evaluate_quality(PAIR_DIR)
    check(snap.get("type") == "quality_report", "evaluate_quality returns quality_report")
    full = evaluate_output_dir(PAIR_DIR)
    spliced = replace_llm_layer(PAIR_DIR)
    check(spliced.verdict == full.verdict, "llm refresh keeps oracle verdict")
    check(
        any(item.get("name") == "rule_based" for item in spliced.layers),
        "llm refresh keeps rule_based layer",
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        shutil.copyfile(PAIR_DIR / "influence_table.json", tmp_dir / "influence_table.json")
        persist_quality_snapshot(tmp_dir)
        first = json.loads((tmp_dir / "oracle_report.json").read_text(encoding="utf-8"))
        rule_checks = [item for item in first.get("checks") or [] if item.get("layer") == "rule_based"]
        check(bool(rule_checks), "persisted oracle tags rule_based checks")
        (tmp_dir / "llm_quality_summary.json").write_text(
            json.dumps(
                {
                    "type": "llm_quality",
                    "verdict": "pass",
                    "score": 90,
                    "publishable": True,
                    "checks": [{"id": "llm.score", "status": "pass", "message": "ok"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        refreshed = replace_llm_layer(tmp_dir)
        check(
            any(
                item.get("name") == LAYER_LLM and not item.get("skipped")
                for item in refreshed.layers
            ),
            "replace_llm_layer attaches llm without a full re-score",
        )
        check(
            sum(1 for item in refreshed.checks if item.layer == "rule_based") == len(rule_checks),
            "replace_llm_layer keeps the same rule-based checks",
        )

    failed = [label for ok, label in results if not ok]
    for ok, label in results:
        print(f"{'PASS' if ok else 'FAIL'}: {label}")
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
