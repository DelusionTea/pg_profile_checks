#!/usr/bin/env python3
"""Analyze multiple NT pg_profile runs: symptoms + GUC change impact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pgprofile_nt_runs import (
    analyze_nt_runs,
    build_nt_runs_brief,
    build_nt_runs_confluence_wiki,
    nt_runs_to_dict,
    parse_symptom_list,
    print_nt_runs_report,
)
from pgprofile_output import write_json_output
from pgprofile_parser import PgProfileParseError
from pgprofile_symptoms import SYMPTOM_TITLES, QueryTarget

DEFAULT_CONFIG = Path(__file__).resolve().parent / "thresholds.yaml"
DEFAULT_PLAYBOOK = Path(__file__).resolve().parent / "knowledge" / "symptom_playbook.yaml"
DEFAULT_GUC_IMPACT = Path(__file__).resolve().parent / "knowledge" / "guc_impact.yaml"


def build_parser() -> argparse.ArgumentParser:
    symptom_help = ", ".join(sorted(set(SYMPTOM_TITLES.keys())))
    parser = argparse.ArgumentParser(
        description=(
            "Analyze 2+ NT pg_profile reports: selected symptoms (CPU/WAL/...) "
            "and likely impact of settings changes between consecutive runs."
        )
    )
    parser.add_argument(
        "reports",
        nargs="+",
        type=Path,
        help="Two or more pg_profile HTML reports in chronological order",
    )
    parser.add_argument(
        "--symptoms",
        required=True,
        help=f"Comma/space-separated symptoms to analyze: {symptom_help}",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="NAME",
        help="Report label (repeat per file, same order)",
    )
    parser.add_argument(
        "--prod-reports",
        nargs="+",
        type=Path,
        metavar="HTML",
        help="Optional PROD baseline reports (old app/settings) for overlap and NT-vs-PROD divergence",
    )
    parser.add_argument(
        "--prod-label",
        action="append",
        default=[],
        metavar="NAME",
        help="Label for --prod-reports (repeat per file, same order)",
    )
    parser.add_argument(
        "--playbook",
        type=Path,
        default=DEFAULT_PLAYBOOK,
        help=f"Symptom playbook YAML (default: {DEFAULT_PLAYBOOK.name})",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Health thresholds (default: {DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--guc-impact",
        type=Path,
        default=DEFAULT_GUC_IMPACT,
        help=f"GUC impact mapping YAML (default: {DEFAULT_GUC_IMPACT.name})",
    )
    parser.add_argument("--min-change-pct", type=float, default=5.0)
    parser.add_argument("--top-n", type=int, default=15)
    parser.add_argument("--query-hex", type=str, help="For slow_query symptom")
    parser.add_argument("--query-id", type=str, help="For slow_query symptom")
    parser.add_argument("--query-text", type=str, help="For slow_query symptom")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("-o", "--output", type=Path, help="Write text/json to file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Write nt_runs.json, nt_runs_brief.md, nt_runs_confluence.wiki",
    )
    parser.add_argument("--confluence-title", type=str, help="Confluence page title")
    parser.add_argument(
        "--list-symptoms",
        action="store_true",
        help="List available symptoms and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_symptoms:
        print("Available symptoms:")
        for key, title in SYMPTOM_TITLES.items():
            print(f"  {key:15} — {title}")
        return 0

    if len(args.reports) < 2:
        print("error: at least two reports are required", file=sys.stderr)
        return 2
    if args.label and len(args.label) != len(args.reports):
        print("error: --label count must match reports", file=sys.stderr)
        return 2
    if args.prod_reports and args.prod_label and len(args.prod_reports) != len(args.prod_label):
        print("error: --prod-label count must match --prod-reports", file=sys.stderr)
        return 2
    for path in args.reports:
        if not path.exists():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2
    for path in args.prod_reports or []:
        if not path.exists():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2

    try:
        parse_symptom_list(args.symptoms)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    query_target = QueryTarget(
        hexqueryid=args.query_hex,
        queryid=args.query_id,
        query_text=args.query_text,
    )

    try:
        analysis = analyze_nt_runs(
            args.reports,
            labels=args.label if args.label else None,
            prod_paths=args.prod_reports,
            prod_labels=args.prod_label if args.prod_label else None,
            symptoms=args.symptoms,
            playbook_path=args.playbook,
            health_thresholds_path=args.config,
            guc_impact_path=args.guc_impact,
            min_change_pct=args.min_change_pct,
            top_n=args.top_n,
            query_target=query_target,
        )
    except (PgProfileParseError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        payload = nt_runs_to_dict(analysis)
        if args.output:
            args.output.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        else:
            write_json_output(payload, output=args.output)
    else:
        out = args.output.open("w", encoding="utf-8") if args.output else sys.stdout
        try:
            print_nt_runs_report(analysis, out=out)
        finally:
            if args.output:
                out.close()

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        payload = nt_runs_to_dict(analysis)
        (args.output_dir / "nt_runs.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (args.output_dir / "nt_runs_brief.md").write_text(
            build_nt_runs_brief(analysis), encoding="utf-8"
        )
        (args.output_dir / "nt_runs_confluence.wiki").write_text(
            build_nt_runs_confluence_wiki(analysis, page_title=args.confluence_title),
            encoding="utf-8",
        )
        if args.format == "text" and not args.output:
            print(f"\nArtifacts written to {args.output_dir}/", file=sys.stderr)
            print("  nt_runs.json", file=sys.stderr)
            print("  nt_runs_brief.md", file=sys.stderr)
            print("  nt_runs_confluence.wiki", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
