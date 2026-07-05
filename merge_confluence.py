#!/usr/bin/env python3
"""Merge confluence_stub.wiki with LLM body into a single Confluence page."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pgprofile_confluence import merge_confluence_page


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge confluence_stub.wiki and LLM output into confluence_page.wiki"
    )
    parser.add_argument(
        "stub",
        type=Path,
        nargs="?",
        default=Path("analysis_out/confluence_stub.wiki"),
        help="Stub from analyze_pgprofile.py (default: analysis_out/confluence_stub.wiki)",
    )
    parser.add_argument(
        "--body",
        "-b",
        type=Path,
        help="LLM output file (Confluence Wiki Markup). If omitted, read from stdin",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("analysis_out/confluence_page.wiki"),
        help="Output path (default: analysis_out/confluence_page.wiki)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.stub.is_file():
        print(f"error: stub not found: {args.stub}", file=sys.stderr)
        return 2

    stub = args.stub.read_text(encoding="utf-8")
    if args.body:
        if not args.body.is_file():
            print(f"error: body not found: {args.body}", file=sys.stderr)
            return 2
        body = args.body.read_text(encoding="utf-8")
    else:
        body = sys.stdin.read()

    if not body.strip():
        print("error: empty LLM body (use --body or pipe stdin)", file=sys.stderr)
        return 2

    merged = merge_confluence_page(stub, body)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(merged, encoding="utf-8")
    print(f"Written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
