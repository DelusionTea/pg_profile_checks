#!/usr/bin/env python3
"""Check that wiki tables render without shifted cells.

Mirrors the cell splitting used by ui/web/js/wiki_preview.js: pipes inside
macros `{...}`, links `[...]` or escaped as `&#124;` are content, not
separators. A row with a different cell count than its header shifts columns
both in Confluence and in the UI preview.

Usage: scripts/validate_wiki_tables.py [path ...]
Defaults to every *.wiki file under analysis_out_test/.
"""

from __future__ import annotations

import sys
from pathlib import Path


def split_cells(line: str) -> list[str]:
    cells: list[str] = []
    current = ""
    depth = 0
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line) and line[index + 1] == "|":
            current += "|"
            index += 2
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth = max(0, depth - 1)
        if char == "|" and depth == 0:
            if index + 1 < len(line) and line[index + 1] == "|":
                index += 1
            cells.append(current)
            current = ""
            index += 1
            continue
        current += char
        index += 1
    cells.append(current)
    if cells and not cells[0].strip():
        cells.pop(0)
    if cells and not cells[-1].strip():
        cells.pop()
    return cells


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    header: list[str] | None = None
    header_line_no = 0
    in_code = False
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.rstrip()
        if line.startswith("{code"):
            in_code = not in_code or line.strip() != "{code}"
            continue
        if in_code:
            if line.strip() == "{code}":
                in_code = False
            continue
        if line.startswith("||"):
            header = split_cells(line)
            header_line_no = line_no
            if len(header) < 2:
                problems.append(f"{path.name}:{line_no}: header has {len(header)} column(s)")
            continue
        if line.startswith("|"):
            cells = split_cells(line)
            if header is None:
                problems.append(f"{path.name}:{line_no}: row without a header row")
                continue
            if len(cells) != len(header):
                problems.append(
                    f"{path.name}:{line_no}: {len(cells)} cells vs {len(header)} header cells "
                    f"(header at line {header_line_no}): {line[:120]}"
                )
            continue
        if line.strip() == "" or line.startswith(("h1.", "h2.", "h3.", "h4.", "{expand", "{info", "{warning", "{note")):
            header = None
    return problems


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    if argv:
        targets = [Path(arg) for arg in argv]
    else:
        targets = sorted((root / "analysis_out_test").rglob("*.wiki"))
    if not targets:
        print("No .wiki files found.")
        return 1

    failures: list[str] = []
    for target in targets:
        if not target.exists():
            failures.append(f"{target}: not found")
            continue
        problems = check_file(target)
        status = "OK" if not problems else "FAIL"
        print(f"[{status}] {target}")
        failures.extend(problems)

    if failures:
        print("\nProblems:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\nAll {len(targets)} wiki file(s) have consistent table columns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
