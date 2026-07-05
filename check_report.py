#!/usr/bin/env python3
"""Run health checks against a single pg_profile HTML report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pgprofile_findings import health_check_to_dict
from pgprofile_health import (
    CHECKERS,
    load_report_data,
    load_thresholds,
    print_report,
    run_checks,
)
from pgprofile_output import write_json_output
from pgprofile_parser import PgProfileParseError

DEFAULT_CONFIG = Path(__file__).resolve().parent / "thresholds.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze a pg_profile HTML report and print health warnings."
    )
    parser.add_argument("report_html", type=Path, help="Path to pg_profile HTML report")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to thresholds YAML config (default: {DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help="Comma-separated categories to run (default: all)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full SQL text in query-related warnings",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write output to file (default: stdout)",
    )
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit with code 1 when warnings are found",
    )
    return parser


def parse_categories(value: str) -> list[str] | None:
    if not value.strip():
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.report_html.exists():
        print(f"error: report file not found: {args.report_html}", file=sys.stderr)
        return 2
    if not args.report_html.is_file():
        print(f"error: report path is not a file: {args.report_html}", file=sys.stderr)
        return 2

    try:
        cfg = load_thresholds(args.config)
        ctx = load_report_data(args.report_html)
        categories = parse_categories(args.only)
        if categories:
            unknown = [name for name in categories if name not in CHECKERS]
            if unknown:
                print(
                    f"error: unknown categories: {', '.join(unknown)}. "
                    f"Available: {', '.join(CHECKERS)}",
                    file=sys.stderr,
                )
                return 2
        warnings = run_checks(ctx, cfg, categories=categories, verbose=args.verbose)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (PgProfileParseError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        write_json_output(health_check_to_dict(ctx, warnings), output=args.output)
    else:
        if args.output:
            import io
            from contextlib import redirect_stdout
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                print_report(ctx, warnings)
            args.output.write_text(buffer.getvalue(), encoding="utf-8")
        else:
            print_report(ctx, warnings)

    if args.exit_code and warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
