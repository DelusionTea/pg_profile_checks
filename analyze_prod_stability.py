#!/usr/bin/env python3
"""Compare multiple PROD pg_profile reports and recommend stable GUC tuning."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pgprofile_confluence import write_stable_prod_confluence_outputs
from pgprofile_output import write_json_output
from pgprofile_parser import PgProfileParseError
from pgprofile_stable_prod import (
    analyze_stable_prod,
    print_stable_prod_report,
    stable_prod_to_dict,
)

DEFAULT_CONFIG = Path(__file__).resolve().parent / "thresholds.yaml"
DEFAULT_TUNING = Path(__file__).resolve().parent / "knowledge" / "prod_tuning.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze several PROD pg_profile reports, detect findings stable across "
            "all (or most) periods, and output GUC tuning recommendations with "
            "problem severity and change safety/impact labels."
        )
    )
    parser.add_argument(
        "reports",
        nargs="+",
        type=Path,
        help="Two or more PROD pg_profile HTML reports",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="NAME",
        help="Label for corresponding report (repeat per file, same order)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Thresholds YAML (default: {DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--tuning",
        type=Path,
        default=DEFAULT_TUNING,
        help=f"PROD tuning rules YAML (default: {DEFAULT_TUNING.name})",
    )
    parser.add_argument(
        "--min-stability",
        type=float,
        default=1.0,
        metavar="RATIO",
        help=(
            "Minimum fraction of reports where a finding must appear "
            "(1.0 = all reports, default; 0.5 = half or more)"
        ),
    )
    parser.add_argument(
        "--show-ephemeral",
        action="store_true",
        help="Include findings that appear in only some reports",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if any stable high/critical findings exist",
    )
    parser.add_argument(
        "--confluence-dir",
        type=Path,
        help="Write stable_prod_confluence_stub.wiki, stable_prod_confluence_prompt.txt, stable_prod_brief.md",
    )
    parser.add_argument(
        "--confluence-title",
        type=str,
        help="Custom Confluence page title",
    )
    return parser


def validate_args(args: argparse.Namespace) -> tuple[list[Path], list[str] | None]:
    if len(args.reports) < 2:
        raise SystemExit("error: at least two PROD HTML reports are required")
    if args.min_stability <= 0 or args.min_stability > 1:
        raise SystemExit("error: --min-stability must be in (0, 1]")
    if args.label and len(args.label) != len(args.reports):
        raise SystemExit(
            f"error: --label count ({len(args.label)}) must match reports ({len(args.reports)})"
        )
    for path in args.reports:
        if not path.exists():
            raise SystemExit(f"error: file not found: {path}")
        if not path.is_file():
            raise SystemExit(f"error: not a file: {path}")
    labels = args.label if args.label else None
    return args.reports, labels


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths, labels = validate_args(args)

    try:
        analysis = analyze_stable_prod(
            paths,
            labels=labels,
            thresholds_path=args.config,
            min_stability_ratio=args.min_stability,
            tuning_path=args.tuning,
        )
    except PgProfileParseError as exc:
        raise SystemExit(f"error: {exc}") from exc
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc

    if args.format == "json":
        payload = stable_prod_to_dict(analysis)
        write_json_output(payload, output=args.output)
    else:
        out = args.output.open("w", encoding="utf-8") if args.output else sys.stdout
        try:
            print_stable_prod_report(
                analysis,
                show_ephemeral=args.show_ephemeral,
                out=out,
            )
        finally:
            if args.output:
                out.close()

    if args.confluence_dir:
        write_stable_prod_confluence_outputs(
            analysis,
            args.confluence_dir,
            page_title=args.confluence_title,
        )
        if args.format == "text" and not args.output:
            print(f"\nConfluence artifacts written to {args.confluence_dir}/", file=sys.stderr)
            print("  stable_prod_confluence_stub.wiki", file=sys.stderr)
            print("  stable_prod_confluence_prompt.txt  (gigacli)", file=sys.stderr)
            print("  stable_prod_brief.md", file=sys.stderr)

    if args.exit_code:
        critical = {"critical", "high"}
        if any(r.problem_severity in critical for r in analysis.recommendations):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
