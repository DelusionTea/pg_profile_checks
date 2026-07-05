#!/usr/bin/env python3
"""Orchestrate pg_profile analysis and produce deterministic JSON + advisor brief."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pgprofile_advisor import (
    advise_findings,
    advisor_report_to_dict,
    build_brief,
    build_llm_prompt,
)
from pgprofile_confluence import build_confluence_llm_prompt, build_confluence_stub
from pgprofile_compare import compare_runs, load_run
from pgprofile_findings import (
    health_check_to_dict,
    run_comparison_to_dict,
    settings_diff_to_dict,
)
from pgprofile_health import load_report_data, load_thresholds, run_checks
from pgprofile_parser import PgProfileParseError, load_settings, parse_report_meta

from compare_settings import diff_settings

DEFAULT_CONFIG = Path(__file__).resolve().parent / "thresholds.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run pg_profile analysis pipeline and produce advisor output for LLM."
    )
    parser.add_argument("--report", type=Path, help="Single report for health check")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--compare-run",
        type=Path,
        help="Second report for run comparison (requires --report as first run)",
    )
    parser.add_argument("--run-a-id", type=str, default="run_a")
    parser.add_argument("--run-b-id", type=str, default="run_b")
    parser.add_argument("--compare-settings", type=Path, help="Second report for settings diff")
    parser.add_argument("--settings-a-id", type=str, default="NT")
    parser.add_argument("--settings-b-id", type=str, default="PROD")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for findings.json, advisor.json, brief.md, summary_prompt.txt, confluence_*",
    )
    parser.add_argument(
        "--confluence-title",
        type=str,
        help="Custom Confluence page title (default: auto from report metadata)",
    )
    parser.add_argument("--min-change-pct", type=float, default=5.0)
    parser.add_argument("--top-n", type=int, default=15)
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if any analysis has findings/issues",
    )
    return parser


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if not args.report and not args.compare_settings:
        print("error: provide --report and/or --compare-settings", file=sys.stderr)
        return 2

    analyses: list[dict[str, Any]] = []
    has_issues = False

    try:
        if args.report:
            cfg = load_thresholds(args.config)
            ctx = load_report_data(args.report)
            warnings = run_checks(ctx, cfg)
            health = health_check_to_dict(ctx, warnings)
            analyses.append(health)
            _save_json(args.output_dir / "health_check.json", health)
            if warnings:
                has_issues = True

            if args.compare_run:
                run_a = load_run(args.report, args.run_a_id)
                run_b = load_run(args.compare_run, args.run_b_id)
                result = compare_runs(
                    run_a,
                    run_b,
                    min_change_pct=args.min_change_pct,
                    top_n=args.top_n,
                )
                run_cmp = run_comparison_to_dict(
                    run_a, run_b, result, min_change_pct=args.min_change_pct
                )
                analyses.append(run_cmp)
                _save_json(args.output_dir / "run_comparison.json", run_cmp)
                if run_cmp.get("findings"):
                    has_issues = True

        if args.report and args.compare_settings:
            path_a = args.report
            path_b = args.compare_settings
            nt_settings = load_settings(path_a, defined_only=True)
            prod_settings = load_settings(path_b, defined_only=True)
            diffs = diff_settings(nt_settings, prod_settings)
            settings = settings_diff_to_dict(
                label_a=args.settings_a_id,
                label_b=args.settings_b_id,
                path_a=path_a,
                path_b=path_b,
                meta_a=parse_report_meta(path_a),
                meta_b=parse_report_meta(path_b),
                diffs=diffs,
            )
            analyses.append(settings)
            _save_json(args.output_dir / "settings_diff.json", settings)
            if settings.get("findings"):
                has_issues = True

    except (PgProfileParseError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    combined_findings: list[dict[str, Any]] = []
    for analysis in analyses:
        combined_findings.extend(analysis.get("findings", []))

    combined = {
        "type": "combined_analysis",
        "analyses": analyses,
        "findings": combined_findings,
        "summary": {
            "total_findings": len(combined_findings),
            "analysis_count": len(analyses),
        },
    }
    _save_json(args.output_dir / "findings.json", combined)

    advisor_reports = [advise_findings(a) for a in analyses]
    combined_advisor = {
        "type": "combined_advisor",
        "reports": [advisor_report_to_dict(r) for r in advisor_reports],
        "summary": {
            "total_findings": len(combined_findings),
            "reports": len(advisor_reports),
        },
    }
    _save_json(args.output_dir / "advisor.json", combined_advisor)

    brief_parts = [build_brief(r) for r in advisor_reports]
    brief = "\n\n---\n\n".join(brief_parts)
    (args.output_dir / "brief.md").write_text(brief, encoding="utf-8")

    prompt = build_llm_prompt(brief)
    (args.output_dir / "summary_prompt.txt").write_text(prompt, encoding="utf-8")

    confluence_stub = build_confluence_stub(
        advisor_reports, page_title=args.confluence_title
    )
    (args.output_dir / "confluence_stub.wiki").write_text(confluence_stub, encoding="utf-8")

    confluence_prompt = build_confluence_llm_prompt(brief)
    (args.output_dir / "confluence_prompt.txt").write_text(confluence_prompt, encoding="utf-8")

    print(f"Analysis written to {args.output_dir}/")
    print(f"  findings.json  ({len(combined_findings)} findings)")
    print(f"  advisor.json")
    print(f"  brief.md")
    print(f"  summary_prompt.txt  (ready for DeepSeek)")
    print(f"  confluence_stub.wiki  (metadata + findings table → Confluence)")
    print(f"  confluence_prompt.txt  (gigacli → Wiki Markup for Confluence)")

    if args.exit_code and has_issues:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
