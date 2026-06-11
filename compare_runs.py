#!/usr/bin/env python3
"""Compare performance metrics between two pg_profile HTML test run reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pgprofile_compare import (
    ALL_SECTIONS,
    compare_runs,
    load_run,
    print_compare_report,
)
from pgprofile_parser import PgProfileParseError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare performance metrics between two pg_profile HTML test run reports."
        )
    )
    parser.add_argument("run_a_html", type=Path, help="Path to first pg_profile HTML report")
    parser.add_argument("run_b_html", type=Path, help="Path to second pg_profile HTML report")
    parser.add_argument(
        "--run-a-id",
        required=True,
        help="RunId label for the first report (column header)",
    )
    parser.add_argument(
        "--run-b-id",
        required=True,
        help="RunId label for the second report (column header)",
    )
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help=f"Comma-separated sections (default: all). Available: {', '.join(ALL_SECTIONS)}",
    )
    parser.add_argument(
        "--min-change-pct",
        type=float,
        default=5.0,
        help="Minimum percent change to include in output (default: 5)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=15,
        help="Max rows for tables/queries sections (default: 15)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full SQL text in query comparison",
    )
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit with code 1 when significant differences are found",
    )
    return parser


def parse_sections(value: str) -> list[str] | None:
    if not value.strip():
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def validate_path(path: Path, label: str) -> None:
    if not path.exists():
        raise SystemExit(f"error: {label} file not found: {path}")
    if not path.is_file():
        raise SystemExit(f"error: {label} path is not a file: {path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    validate_path(args.run_a_html, "Run A")
    validate_path(args.run_b_html, "Run B")

    sections = parse_sections(args.only)
    if sections:
        unknown = [name for name in sections if name not in ALL_SECTIONS]
        if unknown:
            print(
                f"error: unknown sections: {', '.join(unknown)}. "
                f"Available: {', '.join(ALL_SECTIONS)}",
                file=sys.stderr,
            )
            return 2

    try:
        run_a = load_run(args.run_a_html, args.run_a_id)
        run_b = load_run(args.run_b_html, args.run_b_id)
        result = compare_runs(
            run_a,
            run_b,
            sections=sections,
            min_change_pct=args.min_change_pct,
            top_n=args.top_n,
            verbose=args.verbose,
        )
    except (PgProfileParseError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print_compare_report(
        run_a,
        run_b,
        result,
        min_change_pct=args.min_change_pct,
    )

    if args.exit_code and result.significant_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
