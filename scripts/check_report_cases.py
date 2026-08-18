#!/usr/bin/env python3
"""Run the UI analysis paths over a set of pg_profile reports and assert the report contract.

Covers the scenarios reachable from the web UI (nt_runs with and without PROD,
single vs multiple symptoms, full_multi, health, nt_prod, stable_prod) and checks
the wiki output each of them produces: table columns line up, every uploaded run
is represented, legends for the status columns are present, units are rendered and
no metric is labelled as stable while it actually moved.

Usage: scripts/check_report_cases.py <report.html> <report.html> <report.html>
Outputs land in analysis_out_test/case_matrix/<case>/ for manual inspection.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "analysis_out_test" / "case_matrix"


def _load_split_cells() -> Callable[[str], list[str]]:
    spec = importlib.util.spec_from_file_location(
        "validate_wiki_tables", ROOT / "scripts" / "validate_wiki_tables.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.split_cells


split_cells = _load_split_cells()


def table_problems(text: str) -> list[str]:
    problems: list[str] = []
    header: list[str] | None = None
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if line.startswith("||"):
            header = split_cells(line)
        elif line.startswith("|"):
            cells = split_cells(line)
            if header is None:
                problems.append(f"line {line_no}: row without header")
            elif len(cells) != len(header):
                problems.append(
                    f"line {line_no}: {len(cells)} cells vs {len(header)} header cells"
                )
        elif not line.strip() or line.startswith(("h1.", "h2.", "h3.", "h4.", "{")):
            header = None
    return problems


def section(text: str, heading: str, levels: tuple[str, ...] = ("h1.", "h2.")) -> str:
    """Text between `heading` and the next heading of the same or higher level."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return ""
    for index in range(start + 1, len(lines)):
        if lines[index].startswith(levels):
            return "\n".join(lines[start:index])
    return "\n".join(lines[start:])


def headers_in(text: str) -> list[list[str]]:
    return [split_cells(line) for line in text.splitlines() if line.startswith("||")]


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 1
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from ui.analysis_runner import AnalyzeRequest, ReportMeta, run_analysis

    reports = [Path(arg) for arg in argv[:3]]
    for path in reports:
        if not path.is_file():
            print(f"missing report: {path}")
            return 1
    r1, r2, r3 = reports

    cases: list[dict[str, Any]] = [
        {
            "name": "nt_runs_3nt_two_symptoms",
            "scenario": "auto",
            "files": [("NT", r1), ("NT", r2), ("NT", r3)],
            "symptoms": ["high_cpu", "high_wal"],
            "wiki": "nt_runs_confluence.wiki",
            "run_labels": ["nt1", "nt2", "nt3"],
            "pairs": ["nt1 → nt2", "nt2 → nt3"],
            "series_labels": ["nt1", "nt2", "nt3"],
        },
        {
            "name": "nt_runs_2nt_1prod_two_symptoms",
            "scenario": "auto",
            "files": [("NT", r1), ("NT", r2), ("PROD", r3)],
            "symptoms": ["high_cpu", "high_wal"],
            "wiki": "nt_runs_confluence.wiki",
            "run_labels": ["nt1", "nt2", "prod1"],
            "pairs": ["nt1 → nt2", "nt2 → prod1"],
            "series_labels": ["nt1", "nt2"],
        },
        {
            "name": "nt_runs_2nt_1prod_one_symptom",
            "scenario": "auto",
            "files": [("NT", r1), ("NT", r2), ("PROD", r3)],
            "symptoms": ["high_wal"],
            "wiki": "nt_runs_confluence.wiki",
            "run_labels": ["nt1", "nt2", "prod1"],
            "pairs": ["nt1 → nt2", "nt2 → prod1"],
            "series_labels": ["nt1", "nt2"],
        },
        {
            "name": "nt_runs_3nt_one_symptom",
            "scenario": "nt_runs",
            "files": [("NT", r1), ("NT", r2), ("NT", r3)],
            "symptoms": ["high_cpu"],
            "wiki": "nt_runs_confluence.wiki",
            "run_labels": ["nt1", "nt2", "nt3"],
            "pairs": ["nt1 → nt2", "nt2 → nt3"],
            "series_labels": ["nt1", "nt2", "nt3"],
        },
        {
            "name": "symptom_1nt_2prod",
            "scenario": "auto",
            "files": [("NT", r1), ("PROD", r2), ("PROD", r3)],
            "symptoms": ["high_wal"],
            "wiki": None,
        },
        {
            "name": "full_multi_no_symptoms",
            "scenario": "auto",
            "files": [("NT", r1), ("NT", r2), ("PROD", r3)],
            "symptoms": [],
            "wiki": None,
        },
        {
            "name": "health_single_report",
            "scenario": "auto",
            "files": [("NT", r2)],
            "symptoms": [],
            "wiki": None,
        },
        {
            "name": "nt_prod_pair",
            "scenario": "nt_prod",
            "files": [("NT", r2), ("PROD", r3)],
            "symptoms": [],
            "wiki": None,
        },
        {
            "name": "stable_prod_3reports",
            "scenario": "stable_prod",
            "files": [("PROD", r1), ("PROD", r2), ("PROD", r3)],
            "symptoms": [],
            "wiki": None,
        },
    ]

    if OUT_ROOT.exists():
        shutil.rmtree(OUT_ROOT)
    OUT_ROOT.mkdir(parents=True)

    failures: list[str] = []
    produced_wikis: list[Path] = []

    for case in cases:
        name = case["name"]
        metas: list[ReportMeta] = []
        paths: list[Path] = []
        for index, (env, path) in enumerate(case["files"]):
            label = f"{env.lower()}{sum(1 for m in metas if m.env == env) + 1}"
            metas.append(ReportMeta(filename=path.name, env=env, label=label, order=index))
            paths.append(path)

        out_dir = OUT_ROOT / name
        result = run_analysis(
            AnalyzeRequest(scenario=case["scenario"], reports=metas, symptoms=case["symptoms"]),
            paths,
            out_dir,
        )

        problems: list[str] = []
        if result.exit_code == 2:
            problems.append(f"analysis failed: {result.error}")
        if not result.wiki_path:
            problems.append("no wiki produced")
        elif case["wiki"] and result.wiki_path.name != case["wiki"]:
            problems.append(f"expected {case['wiki']}, previewed {result.wiki_path.name}")

        for wiki in sorted(out_dir.rglob("*.wiki")):
            produced_wikis.append(wiki)
            for problem in table_problems(wiki.read_text(encoding="utf-8")):
                problems.append(f"{wiki.name}: {problem}")

        if result.wiki_path and result.wiki_path.name == "nt_runs_confluence.wiki":
            text = result.wiki_path.read_text(encoding="utf-8")
            problems.extend(check_nt_runs(text, case))

        status = "PASS" if not problems else "FAIL"
        print(f"[{status}] {name} -> {out_dir.relative_to(ROOT)}")
        for problem in problems:
            print(f"       {problem}")
        failures.extend(f"{name}: {problem}" for problem in problems)

    print(f"\nwiki files produced: {len(produced_wikis)}")
    if failures:
        print(f"FAILED checks: {len(failures)}")
        return 1
    print("ALL_REPORT_CASES_PASSED")
    return 0


def check_nt_runs(text: str, case: dict[str, Any]) -> list[str]:
    problems: list[str] = []

    settings_section = section(text, "h2. Отличия настроек между прогонами")
    if not settings_section:
        problems.append("missing settings section")
    else:
        expected = ["Параметр"] + case["run_labels"]
        # Per-pair tables also start with "Параметр"; run-comparison tables are the
        # ones whose remaining columns are run labels.
        pair_shapes = (["Было", "Стало", "Направление", "Связь"], ["Метрика (раздел pg_profile)"])
        settings_headers = [
            header
            for header in headers_in(settings_section)
            if header
            and header[0] == "Параметр"
            and header[1:] != pair_shapes[0]
            and header[1:2] != pair_shapes[1]
        ]
        if not settings_headers:
            problems.append("settings table header not found")
        for header in settings_headers:
            if header != expected:
                problems.append(f"settings header {header} != {expected}")

        pair_headings = [
            line.strip()
            for line in settings_section.splitlines()
            if line.startswith("h4. ")
        ]
        for expected_pair in case["pairs"]:
            if not any(expected_pair in heading for heading in pair_headings):
                problems.append(f"missing pair section for {expected_pair}")
        if len(pair_headings) != len(case["pairs"]):
            problems.append(f"pair sections {pair_headings} != {case['pairs']}")

        for legend in ("Расшифровка колонки «Связь»", "Расшифровка колонки «Оценка»"):
            if legend not in settings_section:
                problems.append(f"missing legend: {legend}")

    metrics_section = section(text, "h2. Изменения метрик между прогонами")
    if not metrics_section:
        problems.append("missing metrics section")
    else:
        series_headers = [h for h in headers_in(metrics_section) if h and h[0] == "Метрика"]
        if not series_headers:
            problems.append("series metrics header not found")
        else:
            header = series_headers[0]
            for label in case["series_labels"]:
                if label not in header:
                    problems.append(f"series header {header} lacks run {label}")
        if "Расшифровка колонки «Тренд»" not in metrics_section:
            problems.append("missing legend: Расшифровка колонки «Тренд»")

    # As a table cell, so the legend's "нестабильный результат" is not a match.
    if "|стабильный результат|" in text:
        problems.append("metric still labelled 'стабильный результат'")
    if "checkpoint_write_time" in text and " с (" not in text:
        problems.append("checkpoint timings rendered without a unit")
    for required in ("h2. Краткие выводы", "h2. Риски и блокеры", "h2. Следующие действия"):
        if required not in text:
            problems.append(f"missing section: {required}")
    return problems


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
