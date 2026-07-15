#!/usr/bin/env python3
"""Compare NT vs PROD pg_profile reports: settings gate + performance metrics."""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

from pgprofile_nt_prod import (
    NT_PROD_SECTIONS,
    print_nt_prod_report,
    nt_prod_validation_to_dict,
    validate_nt_prod,
)
from pgprofile_confluence import write_nt_prod_confluence_outputs
from pgprofile_output import write_json_output
from pgprofile_parser import PgProfileParseError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate NT stand against PROD: settings must match, then compare "
            "WAL throughput, DML, SQL parameters, and other metrics."
        )
    )
    parser.add_argument("nt_html", nargs="?", type=Path, help="NT pg_profile HTML report")
    parser.add_argument("prod_html", nargs="?", type=Path, help="PROD pg_profile HTML report")
    parser.add_argument("--nt", type=Path, help="Path to NT report")
    parser.add_argument("--prod", type=Path, help="Path to PROD report")
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help=f"Sections (default: {','.join(NT_PROD_SECTIONS)}). "
        f"Available: {', '.join(NT_PROD_SECTIONS)}",
    )
    parser.add_argument("--min-change-pct", type=float, default=5.0)
    parser.add_argument("--top-n", type=int, default=15)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if settings differ or significant metric differences exist",
    )
    parser.add_argument(
        "--exit-code-settings-only",
        action="store_true",
        help="Exit 1 only when Defined settings differ",
    )
    parser.add_argument(
        "--confluence-dir",
        type=Path,
        help="Write nt_prod_confluence_stub.wiki, nt_prod_confluence_prompt.txt, nt_prod_brief.md",
    )
    parser.add_argument(
        "--confluence-title",
        type=str,
        help="Custom Confluence page title",
    )
    return parser


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    nt_path = args.nt or args.nt_html
    prod_path = args.prod or args.prod_html
    if nt_path is None or prod_path is None:
        raise SystemExit("error: both NT and PROD HTML files are required")
    for label, path in (("NT", nt_path), ("PROD", prod_path)):
        if not path.exists():
            raise SystemExit(f"error: {label} file not found: {path}")
        if not path.is_file():
            raise SystemExit(f"error: {label} path is not a file: {path}")
    return nt_path, prod_path


def parse_sections(value: str) -> list[str] | None:
    if not value.strip():
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    nt_path, prod_path = resolve_paths(args)
    sections = parse_sections(args.only)

    if sections:
        unknown = [s for s in sections if s not in NT_PROD_SECTIONS]
        if unknown:
            print(
                f"error: unknown sections: {', '.join(unknown)}. "
                f"Available: {', '.join(NT_PROD_SECTIONS)}",
                file=sys.stderr,
            )
            return 2

    try:
        validation = validate_nt_prod(
            nt_path,
            prod_path,
            sections=sections,
            min_change_pct=args.min_change_pct,
            top_n=args.top_n,
            verbose=args.verbose,
        )
    except (PgProfileParseError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        write_json_output(nt_prod_validation_to_dict(validation), output=args.output)
    else:
        if args.output:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                print_nt_prod_report(
                    validation,
                    verbose=args.verbose,
                    color=False,
                )
            args.output.write_text(buffer.getvalue(), encoding="utf-8")
        else:
            print_nt_prod_report(
                validation,
                verbose=args.verbose,
                color=False if args.no_color else None,
            )

    confluence_dir = args.confluence_dir
    if confluence_dir:
        write_nt_prod_confluence_outputs(
            validation,
            confluence_dir,
            page_title=args.confluence_title,
        )
        if args.format != "json":
            print(f"Confluence artifacts written to {confluence_dir}/", file=sys.stderr)
            print("  nt_prod_confluence_stub.wiki", file=sys.stderr)
            print("  nt_prod_confluence_prompt.txt  (gigacli)", file=sys.stderr)
            print("  nt_prod_brief.md", file=sys.stderr)

    if args.exit_code_settings_only and not validation.settings.valid:
        return 1
    if args.exit_code:
        if not validation.settings.valid:
            return 1
        if validation.warning_count > 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
