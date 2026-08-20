#!/usr/bin/env python3
"""Chronological order of pg_profile reports.

Seam: parse_report_period / sort_reports_by_date in pgprofile_parser.
Adapters: session_from_request (UI) and run_pipeline (CLI) must order reports by
report period, not by upload/argument order.
UI contract: report period column present, manual order column gone.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyze_pgprofile import build_parser, run_pipeline  # noqa: E402
from pgprofile_parser import parse_report_period, sort_reports_by_date  # noqa: E402
from ui.analysis_runner import AnalyzeRequest, ReportMeta, session_from_request  # noqa: E402

JUL31 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_07_31_09_00_to=2026_08_01_00.html"
AUG11 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html"
AUG12 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html"
NO_DATE_IN_NAME = ROOT / "resources" / "counteragent_prom1.html"

CHRONOLOGICAL = [JUL31, AUG11, AUG12]
SHUFFLED = [AUG12, JUL31, AUG11]


def check(cond: bool, label: str, results: list[tuple[bool, str]]) -> None:
    results.append((bool(cond), label))


def test_parse_period(results: list[tuple[bool, str]]) -> None:
    period = parse_report_period(AUG11)
    check(period.source == "report", "period is read from report data, not the filename", results)
    check(
        period.start is not None and period.start.strftime("%Y-%m-%d %H:%M") == "2026-08-11 16:30",
        "report_start1 parsed into period.start",
        results,
    )
    check(
        period.end is not None and period.end.strftime("%Y-%m-%d %H:%M") == "2026-08-12 09:00",
        "report_end1 parsed into period.end",
        results,
    )
    check(period.label().startswith("2026-08-11 16:30"), "period.label is human readable", results)

    # Reports without from=/to= in the filename still expose their period.
    check(parse_report_period(NO_DATE_IN_NAME).start is not None, "period works without dates in the filename", results)

    # Renaming must not change the order: the period comes from report data.
    with tempfile.TemporaryDirectory() as tmp:
        renamed = Path(tmp) / "zzz_upload_0.html"
        renamed.write_bytes(AUG11.read_bytes())
        check(
            parse_report_period(renamed).start == period.start,
            "period survives an upload rename",
            results,
        )


def test_sort_reports(results: list[tuple[bool, str]]) -> None:
    order = sort_reports_by_date(SHUFFLED, ["c", "a", "b"])
    check(order.paths == CHRONOLOGICAL, "shuffled paths are sorted chronologically", results)
    check(order.labels == ["a", "b", "c"], "labels follow their own report", results)
    check(order.changed is True, "changed=True when the given order was not chronological", results)
    check(sort_reports_by_date(CHRONOLOGICAL).changed is False, "changed=False for an already sorted list", results)
    check("2026-07-31" in order.note(), "note() names the resulting order", results)

    unknown = sort_reports_by_date([AUG12, Path("/does/not/exist.html")])
    check(unknown.paths[0] == AUG12, "reports with a known period come first", results)
    check(unknown.undated == [Path("/does/not/exist.html")], "undated reports are reported, not dropped", results)


def test_ui_adapter(results: list[tuple[bool, str]]) -> None:
    out = Path(tempfile.mkdtemp(prefix="report_order_ui_"))
    metas = [
        ReportMeta(filename=p.name, env="NT", label=f"nt_{i + 1}", order=i)
        for i, p in enumerate(SHUFFLED)
    ]
    req = AnalyzeRequest(scenario="nt_runs", reports=metas, symptoms=["high_cpu"])
    ns = session_from_request(req, list(SHUFFLED), out)
    check(ns.nt_reports == CHRONOLOGICAL, "nt_runs orders НТ reports by report date", results)

    req = AnalyzeRequest(scenario="full_multi", reports=metas, symptoms=[])
    ns = session_from_request(req, list(SHUFFLED), out)
    check(ns.stable_prod_reports == CHRONOLOGICAL, "full_multi orders reports by report date", results)

    req = AnalyzeRequest(scenario="compare_runs", reports=metas, symptoms=[])
    ns = session_from_request(req, list(SHUFFLED), out)
    check(
        (ns.report, ns.compare_run) == (JUL31, AUG11),
        "compare_runs takes the two earliest reports, A earlier than B",
        results,
    )

    # PROD/НТ pairing stays env-driven: the НТ report is A even when it is later.
    mixed = [
        ReportMeta(filename=AUG12.name, env="NT", label="nt_1", order=0),
        ReportMeta(filename=JUL31.name, env="PROD", label="prom1", order=1),
    ]
    ns = session_from_request(
        AnalyzeRequest(scenario="nt_prod", reports=mixed, symptoms=[]),
        [AUG12, JUL31],
        out,
    )
    check((ns.report, ns.compare_prod) == (AUG12, JUL31), "nt_prod keeps НТ as A and ПРОМ as B", results)


def test_cli_pipeline(results: list[tuple[bool, str]]) -> None:
    out = Path(tempfile.mkdtemp(prefix="report_order_cli_"))
    parser = build_parser()
    args = parser.parse_args(
        [
            "--nt-reports",
            *[str(p) for p in SHUFFLED],
            "--nt-label",
            "c",
            "--nt-label",
            "a",
            "--nt-label",
            "b",
            "--symptoms",
            "high_cpu",
            "--output-dir",
            str(out),
        ]
    )
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = run_pipeline(args)
    check(code != 2, f"nt_runs pipeline succeeds (exit={code})", results)
    check("порядок отчётов" in stderr.getvalue(), "CLI warns on stderr that the order was fixed", results)

    nt_runs = out / "nt_runs.json"
    check(nt_runs.is_file(), "nt_runs.json is written", results)
    if nt_runs.is_file():
        payload = json.loads(nt_runs.read_text(encoding="utf-8"))
        entries = payload.get("reports") or []
        labels = [str(item.get("label")) for item in entries]
        filenames = [str(item.get("filename")) for item in entries]
        check(labels == ["a", "b", "c"], f"run labels follow report dates (got {labels})", results)
        check(
            filenames == [p.name for p in CHRONOLOGICAL],
            "nt_runs.json lists reports in chronological order",
            results,
        )


def test_no_reordering_of_explicit_pair(results: list[tuple[bool, str]]) -> None:
    """--report / --compare-run stay as given: the caller picked the baseline."""
    out = Path(tempfile.mkdtemp(prefix="report_order_pair_"))
    parser = build_parser()
    args = parser.parse_args(
        ["--report", str(AUG12), "--compare-run", str(JUL31), "--output-dir", str(out)]
    )
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        code = run_pipeline(args)
    check(code != 2, f"explicit pair still runs (exit={code})", results)
    check(
        "хронолог" in stderr.getvalue().lower(),
        "CLI warns that --compare-run is older than --report",
        results,
    )


def test_ui_contract(results: list[tuple[bool, str]]) -> None:
    html = (ROOT / "ui" / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    check("Период отчёта" in html, "reports table shows the report period column", results)
    check("Порядок" not in html, "manual order column is gone from the reports table", results)
    check("order-input" not in app, "manual order input is gone from app.js", results)
    check("btn-up" not in app and "btn-down" not in app, "manual up/down buttons are gone", results)
    check("readReportPeriod" in app, "app.js reads the report period from the uploaded file", results)
    check("periodSortKey" in app, "app.js sorts the table by report period", results)


def main() -> int:
    results: list[tuple[bool, str]] = []
    test_parse_period(results)
    test_sort_reports(results)
    test_ui_adapter(results)
    test_cli_pipeline(results)
    test_no_reordering_of_explicit_pair(results)
    test_ui_contract(results)
    failed = [(ok, label) for ok, label in results if not ok]
    for ok, label in results:
        print(("OK  " if ok else "FAIL") + " " + label)
    if failed:
        print(f"FAIL: {len(failed)}/{len(results)}", file=sys.stderr)
        return 1
    print(f"OK: {len(results)} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
