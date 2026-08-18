#!/usr/bin/env python3
"""Reproduce a UI nt_runs run and report which runs land in the wiki tables.

Runs the same code path as the web UI (ui.analysis_runner.run_analysis) for a
given set of reports, then prints the wiki file that the UI would preview plus
the header of every table in it.

Usage:
  scripts/check_ui_nt_runs_case.py <symptoms> <ENV:report.html> [ENV:report.html ...]
Example:
  scripts/check_ui_nt_runs_case.py high_cpu,high_wal NT:a.html NT:b.html PROD:c.html
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from ui.analysis_runner import AnalyzeRequest, ReportMeta, run_analysis

    symptoms = [s for s in argv[0].replace(" ", ",").split(",") if s]
    reports: list[ReportMeta] = []
    upload_paths: list[Path] = []
    for index, item in enumerate(argv[1:]):
        env, _, raw_path = item.partition(":")
        path = Path(raw_path)
        if not path.is_file():
            print(f"missing report: {path}")
            return 1
        label = f"{env.lower()}{sum(1 for r in reports if r.env == env.upper()) + 1}"
        reports.append(ReportMeta(filename=path.name, env=env.upper(), label=label, order=index))
        upload_paths.append(path)

    out_dir = Path(tempfile.mkdtemp(prefix="ui_nt_runs_case_"))
    result = run_analysis(
        AnalyzeRequest(scenario="auto", reports=reports, symptoms=symptoms),
        upload_paths,
        out_dir,
    )

    print(f"exit_code={result.exit_code} error={result.error}")
    print(f"output_dir={out_dir}")
    print(f"wiki previewed by UI: {result.wiki_path.name if result.wiki_path else None}")
    print("generated files: " + ", ".join(sorted(p.name for p in out_dir.iterdir())))
    if not result.wiki_path:
        return 1

    print("\ntable headers in previewed wiki:")
    section = ""
    for line in result.wiki_path.read_text(encoding="utf-8").splitlines():
        if line.startswith(("h1.", "h2.", "h3.", "h4.")):
            section = line
        elif line.startswith("||"):
            columns = [cell for cell in line.split("||") if cell.strip()]
            print(f"  [{section}] {len(columns)} cols: {columns}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
