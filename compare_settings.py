#!/usr/bin/env python3
"""Compare Defined PostgreSQL settings between two pg_profile HTML reports."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pgprofile_findings import settings_diff_to_dict
from pgprofile_output import write_json_output
from pgprofile_parser import PgProfileParseError, load_settings, parse_report_meta
from pgprofile_classify import split_settings_rows


class DiffStatus(str, Enum):
    DIFFER = "DIFFER"
    ONLY_NT = "ONLY_NT"
    ONLY_PROD = "ONLY_PROD"
    SAME = "SAME"


@dataclass(frozen=True)
class DiffRow:
    name: str
    status: DiffStatus
    nt_value: str | None = None
    prod_value: str | None = None


def diff_settings(nt: dict[str, str], prod: dict[str, str]) -> list[DiffRow]:
    """Compare two settings dicts and return all non-identical rows."""
    rows: list[DiffRow] = []

    for name in sorted(set(nt) | set(prod)):
        in_nt = name in nt
        in_prod = name in prod

        if in_nt and in_prod:
            if nt[name] == prod[name]:
                rows.append(DiffRow(name=name, status=DiffStatus.SAME))
            else:
                rows.append(
                    DiffRow(
                        name=name,
                        status=DiffStatus.DIFFER,
                        nt_value=nt[name],
                        prod_value=prod[name],
                    )
                )
        elif in_nt:
            rows.append(
                DiffRow(name=name, status=DiffStatus.ONLY_NT, nt_value=nt[name])
            )
        else:
            rows.append(
                DiffRow(
                    name=name,
                    status=DiffStatus.ONLY_PROD,
                    prod_value=prod[name],
                )
            )

    return rows


def _truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    if max_len <= 3:
        return value[:max_len]
    return value[: max_len - 3] + "..."


def _format_meta_line(label: str, meta: dict[str, str], count: int) -> str:
    parts = [f"{label}: {meta['filename']} ({count} settings)"]
    if meta.get("server"):
        parts.append(f"server={meta['server']}")
    if meta.get("from") and meta.get("to"):
        parts.append(f"interval={meta['from']} .. {meta['to']}")
    if meta.get("version"):
        parts.append(f"version={meta['version']}")
    return "  ".join(parts)


def print_report(
    diffs: list[DiffRow],
    *,
    nt_meta: dict[str, str],
    prod_meta: dict[str, str],
    nt_count: int,
    prod_count: int,
    verbose: bool = False,
    value_max_len: int = 60,
) -> None:
    """Print a human-readable diff report to stdout."""
    differ = [row for row in diffs if row.status is DiffStatus.DIFFER]
    only_nt = [row for row in diffs if row.status is DiffStatus.ONLY_NT]
    only_prod = [row for row in diffs if row.status is DiffStatus.ONLY_PROD]
    same_count = sum(1 for row in diffs if row.status == DiffStatus.SAME)
    critical_rows, informational_rows = split_settings_rows(diffs)

    print("pg_profile Settings diff (Defined settings only)")
    print(_format_meta_line("NT", nt_meta, nt_count))
    print(_format_meta_line("PROD", prod_meta, prod_count))
    print()

    total_issues = len(critical_rows) + len(informational_rows)
    print(
        f"Found {total_issues} difference(s): "
        f"{len(critical_rows)} critical GUC, "
        f"{len(informational_rows)} informational (runtime metadata)"
    )
    print()

    if critical_rows:
        _print_settings_diff_table(critical_rows, verbose=verbose, value_max_len=value_max_len)
    else:
        print("No critical GUC differences.")

    if informational_rows:
        print()
        print(
            f"Informational only ({len(informational_rows)}) — runtime metadata, "
            f"does not invalidate NT vs PROD comparison:"
        )
        _print_settings_diff_table(
            informational_rows, verbose=verbose, value_max_len=value_max_len, compact=True
        )
        print()

    if not critical_rows and not informational_rows:
        print("All Defined settings match.")
        print()

    print(
        "Summary: "
        f"{len([r for r in critical_rows if r.status == DiffStatus.DIFFER])} critical differ, "
        f"{len([r for r in critical_rows if r.status == DiffStatus.ONLY_NT])} critical only NT, "
        f"{len([r for r in critical_rows if r.status == DiffStatus.ONLY_PROD])} critical only PROD, "
        f"{len(informational_rows)} informational, "
        f"{same_count} identical (hidden)"
    )


def _print_settings_diff_table(
    rows: list[DiffRow],
    *,
    verbose: bool,
    value_max_len: int,
    compact: bool = False,
) -> None:
    differ = [row for row in rows if row.status is DiffStatus.DIFFER]
    only_nt = [row for row in rows if row.status is DiffStatus.ONLY_NT]
    only_prod = [row for row in rows if row.status is DiffStatus.ONLY_PROD]

    if differ:
        name_width = max(len(row.name) for row in differ)
        name_width = max(name_width, len("Setting"))

        if verbose:
            nt_width = max(len(row.nt_value or "") for row in differ)
            prod_width = max(len(row.prod_value or "") for row in differ)
        else:
            nt_width = max(
                len(_truncate(row.nt_value or "", value_max_len)) for row in differ
            )
            prod_width = max(
                len(_truncate(row.prod_value or "", value_max_len)) for row in differ
            )

        nt_width = max(nt_width, len("NT (test)"))
        prod_width = max(prod_width, len("PROD"))

        header = (
            f"{'Setting'.ljust(name_width)} | "
            f"{'NT (test)'.ljust(nt_width)} | "
            f"{'PROD'.ljust(prod_width)}"
        )
        separator = (
            f"{'-' * name_width}-+-"
            f"{'-' * nt_width}-+-"
            f"{'-' * prod_width}"
        )
        print(header)
        print(separator)

        limit = 10 if compact else len(differ)
        for row in differ[:limit]:
            nt_value = row.nt_value or ""
            prod_value = row.prod_value or ""
            if not verbose:
                nt_value = _truncate(nt_value, value_max_len)
                prod_value = _truncate(prod_value, value_max_len)
            print(
                f"{row.name.ljust(name_width)} | "
                f"{nt_value.ljust(nt_width)} | "
                f"{prod_value.ljust(prod_width)}"
            )
        if len(differ) > limit:
            print(f"  ... and {len(differ) - limit} more")
        print()

    if only_nt and not compact:
        print(f"Only in NT ({len(only_nt)}):")
        for row in only_nt:
            value = row.nt_value or ""
            if not verbose:
                value = _truncate(value, value_max_len)
            print(f"  {row.name} = {value}")
        print()

    if only_prod:
        label = "Only in PROD"
        if compact:
            for row in only_prod[:5]:
                print(f"  {row.name} = {row.prod_value}")
            if len(only_prod) > 5:
                print(f"  ... and {len(only_prod) - 5} more")
        else:
            print(f"{label} ({len(only_prod)}):")
            for row in only_prod:
                value = row.prod_value or ""
                if not verbose:
                    value = _truncate(value, value_max_len)
                print(f"  {row.name} = {value}")
            print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare Defined PostgreSQL settings between two pg_profile HTML reports."
        )
    )
    parser.add_argument(
        "nt_html",
        nargs="?",
        type=Path,
        help="Path to NT (test) pg_profile HTML report",
    )
    parser.add_argument(
        "prod_html",
        nargs="?",
        type=Path,
        help="Path to PROD pg_profile HTML report",
    )
    parser.add_argument("--nt", type=Path, help="Path to NT (test) report")
    parser.add_argument("--prod", type=Path, help="Path to PROD report")
    parser.add_argument("--run-a-id", type=str, default="NT", help="Label for first report in JSON")
    parser.add_argument("--run-b-id", type=str, default="PROD", help="Label for second report in JSON")
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
        "--verbose",
        action="store_true",
        help="Do not truncate long setting values in output",
    )
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit with code 1 when differences are found",
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    nt_path, prod_path = resolve_paths(args)

    try:
        nt_settings = load_settings(nt_path, defined_only=True)
        prod_settings = load_settings(prod_path, defined_only=True)
        nt_meta = parse_report_meta(nt_path)
        prod_meta = parse_report_meta(prod_path)
    except PgProfileParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not nt_settings:
        print(f"warning: no Defined settings found in NT report: {nt_path}", file=sys.stderr)
    if not prod_settings:
        print(
            f"warning: no Defined settings found in PROD report: {prod_path}",
            file=sys.stderr,
        )

    diffs = diff_settings(nt_settings, prod_settings)

    if args.format == "json":
        write_json_output(
            settings_diff_to_dict(
                label_a=args.run_a_id,
                label_b=args.run_b_id,
                path_a=nt_path,
                path_b=prod_path,
                meta_a=nt_meta,
                meta_b=prod_meta,
                diffs=diffs,
            ),
            output=args.output,
        )
    else:
        if args.output:
            import io
            from contextlib import redirect_stdout
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                print_report(
                    diffs,
                    nt_meta=nt_meta,
                    prod_meta=prod_meta,
                    nt_count=len(nt_settings),
                    prod_count=len(prod_settings),
                    verbose=args.verbose,
                )
            args.output.write_text(buffer.getvalue(), encoding="utf-8")
        else:
            print_report(
                diffs,
                nt_meta=nt_meta,
                prod_meta=prod_meta,
                nt_count=len(nt_settings),
                prod_count=len(prod_settings),
                verbose=args.verbose,
            )

    critical_rows, _informational = split_settings_rows(diffs)
    has_issues = len(critical_rows) > 0
    if args.exit_code and has_issues:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
