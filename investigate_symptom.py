#!/usr/bin/env python3
"""Investigate popular PostgreSQL symptoms using pg_profile report(s)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pgprofile_confluence import write_symptom_confluence_outputs
from pgprofile_output import write_json_output
from pgprofile_parser import PgProfileParseError
from pgprofile_symptoms import (
    QueryTarget,
    SYMPTOM_TITLES,
    investigate_symptom,
    normalize_symptom,
    print_symptom_investigation,
    symptom_investigation_to_dict,
)

DEFAULT_CONFIG = Path(__file__).resolve().parent / "thresholds.yaml"
DEFAULT_PLAYBOOK = Path(__file__).resolve().parent / "knowledge" / "symptom_playbook.yaml"

SYMPTOM_CHOICES = sorted(set(SYMPTOM_TITLES.keys()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze pg_profile report(s) for possible causes of a popular DB symptom "
            "and output a verification action plan."
        )
    )
    parser.add_argument(
        "symptom",
        type=str,
        nargs="?",
        help=(
            "Symptom: high_cpu | high_memory | high_wal | slow_query "
            "(aliases: cpu, memory, wal, query)"
        ),
    )
    parser.add_argument(
        "reports",
        nargs="*",
        type=Path,
        help="One or more pg_profile HTML reports",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        metavar="NAME",
        help="Report label (repeat per file, same order)",
    )
    parser.add_argument(
        "--query-hex",
        type=str,
        help="Target query hexqueryid (for slow_query)",
    )
    parser.add_argument(
        "--query-id",
        type=str,
        help="Target query queryid (for slow_query)",
    )
    parser.add_argument(
        "--query-text",
        type=str,
        help="Substring of SQL text to match (for slow_query)",
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
        help=f"Health thresholds for cross-checks (default: {DEFAULT_CONFIG.name})",
    )
    parser.add_argument(
        "--no-health-checks",
        action="store_true",
        help="Skip running standard health-checks alongside symptom analysis",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument(
        "--list-symptoms",
        action="store_true",
        help="List available symptoms and exit",
    )
    parser.add_argument(
        "--confluence-dir",
        type=Path,
        help="Write symptom_confluence_stub.wiki, symptom_confluence_prompt.txt, symptom_brief.md",
    )
    parser.add_argument(
        "--confluence-title",
        type=str,
        help="Custom Confluence page title",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_symptoms:
        print("Available symptoms:")
        for key, title in SYMPTOM_TITLES.items():
            print(f"  {key:15} — {title}")
        return 0

    if not args.symptom or not args.reports:
        raise SystemExit("error: symptom and at least one report HTML are required")

    if args.label and len(args.label) != len(args.reports):
        raise SystemExit(
            f"error: --label count ({len(args.label)}) must match reports ({len(args.reports)})"
        )
    for path in args.reports:
        if not path.exists():
            raise SystemExit(f"error: file not found: {path}")

    try:
        normalize_symptom(args.symptom)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc

    if normalize_symptom(args.symptom) == "slow_query":
        if not (args.query_hex or args.query_id or args.query_text):
            raise SystemExit(
                "error: slow_query requires --query-hex, --query-id, or --query-text"
            )

    query_target = QueryTarget(
        hexqueryid=args.query_hex,
        queryid=args.query_id,
        query_text=args.query_text,
    )

    try:
        investigation = investigate_symptom(
            args.symptom,
            args.reports,
            labels=args.label if args.label else None,
            query_target=query_target,
            playbook_path=args.playbook,
            health_thresholds_path=None if args.no_health_checks else args.config,
        )
    except PgProfileParseError as exc:
        raise SystemExit(f"error: {exc}") from exc
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc

    if args.format == "json":
        write_json_output(symptom_investigation_to_dict(investigation), output=args.output)
    else:
        out = args.output.open("w", encoding="utf-8") if args.output else sys.stdout
        try:
            print_symptom_investigation(investigation, out=out)
        finally:
            if args.output:
                out.close()

    if args.confluence_dir:
        write_symptom_confluence_outputs(
            investigation,
            args.confluence_dir,
            page_title=args.confluence_title,
        )
        if args.format == "text" and not args.output:
            print(f"\nConfluence artifacts written to {args.confluence_dir}/", file=sys.stderr)
            print("  symptom_confluence_stub.wiki", file=sys.stderr)
            print("  symptom_confluence_prompt.txt  (gigacli)", file=sys.stderr)
            print("  symptom_brief.md", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
