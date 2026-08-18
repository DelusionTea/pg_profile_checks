#!/usr/bin/env python3
"""Print the unified quality report for an analysis output directory.

Examples:
  python run_quality.py --output-dir analysis_out
  python run_quality.py --output-dir analysis_out --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pgprofile_quality import (
    QUALITY_JSON,
    format_quality_markdown,
    write_quality_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show the quality report for an analysis run")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("analysis_out"),
        help="Analysis output directory (default: analysis_out)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print quality_report.json instead of markdown",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Recompute oracle + quality files before printing",
    )
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if overall quality verdict is fail",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.output_dir.is_dir():
        print(f"error: output directory not found: {args.output_dir}", file=sys.stderr)
        return 2
    cached = args.output_dir / QUALITY_JSON
    if args.refresh or not cached.is_file():
        write_quality_report(args.output_dir)
    report = json.loads(cached.read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_quality_markdown(report), end="")
    if args.exit_code and report.get("verdict") == "fail":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
