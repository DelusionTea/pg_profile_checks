#!/usr/bin/env python3
"""DML etalon: max raw insert/update/delete from application tables.

Seam: pgprofile_dml_etalon.py. Adapters: analyze_pgprofile.run_pipeline,
session_from_request. Fixtures: resources/counter_nt_*.html (debug stand-in for PROD).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analyze_pgprofile import run_pipeline  # noqa: E402
from pgprofile_dml_etalon import (  # noqa: E402
    build_dml_etalon,
    build_dml_etalon_wiki,
    etalon_from_table_rows,
    is_app_table,
)
from pgprofile_session import AnalysisSession  # noqa: E402
from ui.analysis_runner import (  # noqa: E402
    AnalyzeRequest,
    ReportMeta,
    _build_findings_ui,
    _build_summary,
    session_from_request,
    suggest_scenario,
)

BEFORE = ROOT / "resources" / "counter_nt_before_settings.html"
OLD = ROOT / "resources" / "counter_nt_old_version.html"
WITH = ROOT / "resources" / "counter_nt_with_settings.html"


def check(cond: bool, label: str, results: list[tuple[bool, str]]) -> None:
    results.append((bool(cond), label))


def _row(relname: str, **kwargs: object) -> dict[str, object]:
    data: dict[str, object] = {
        "dbname": "counteragent",
        "schemaname": "counteragent",
        "relname": relname,
    }
    data.update(kwargs)
    return data


def test_filter_and_max(results: list[tuple[bool, str]]) -> None:
    check(
        is_app_table(_row("t_client")),
        "application table in the main DB is kept",
        results,
    )
    check(
        not is_app_table(_row("t_repl_agglock_client")),
        "t_repl_* tables are technical and dropped",
        results,
    )
    check(
        not is_app_table(
            {
                "dbname": "postgres",
                "schemaname": "pgse_profile",
                "relname": "statements",
            }
        ),
        "postgres / pgse_profile is dropped",
        results,
    )
    check(
        not is_app_table(
            {
                "dbname": "counteragent",
                "schemaname": "pg_catalog",
                "relname": "pg_class",
            }
        ),
        "pg_catalog is dropped",
        results,
    )

    tables = etalon_from_table_rows(
        [
            (
                "a.html",
                [
                    _row("t_client", n_tup_ins=10, n_tup_upd=20),
                    _row("t_repl_agglock_x", n_tup_ins=999),
                    _row("t_empty"),
                    {
                        "dbname": "postgres",
                        "schemaname": "pgse_profile",
                        "relname": "noise",
                        "n_tup_ins": 1,
                    },
                ],
            ),
            (
                "b.html",
                [
                    _row("t_client", n_tup_ins=7, n_tup_upd=50, n_tup_del=3),
                    _row("t_only_b", n_tup_ins=4),
                ],
            ),
        ]
    )
    by_name = {row.relname: row for row in tables}
    check("t_repl_agglock_x" not in by_name, "etalon does not keep t_repl_*", results)
    check("noise" not in by_name, "etalon does not keep profiler tables", results)
    check("t_empty" not in by_name, "row with no DML numbers is omitted", results)
    client = by_name.get("t_client")
    check(client is not None, "t_client is in the etalon", results)
    check(client is not None and client.insert == 10, "insert is max(10, 7)", results)
    check(client is not None and client.update == 50, "update is max(20, 50)", results)
    check(client is not None and client.delete == 3, "delete present in one report is kept", results)
    only_b = by_name.get("t_only_b")
    check(only_b is not None and only_b.insert == 4, "union includes a table seen in one report", results)
    check(only_b is not None and only_b.update == 0 and only_b.delete == 0, "missing ops become 0", results)


def test_debug_html_max(results: list[tuple[bool, str]]) -> None:
    etalon = build_dml_etalon([BEFORE, OLD, WITH])
    by_name = {row.relname: row for row in etalon.tables}
    names = set(by_name)
    check(etalon.report_count == 3, "three HTML files are sources", results)
    check(not etalon.single_report, "three files are not a single-report etalon", results)
    check("t_feedbackwaitinglist" in names, "t_feedbackwaitinglist is in the etalon", results)
    check("t_repl_agglock_feedbackwaitinglist" not in names, "t_repl_* absent from HTML etalon", results)
    check("t_auditevent" not in names, "table with no DML numbers is omitted", results)
    check(
        not any(name.startswith("pg_") or "pgse" in name for name in names),
        "no catalog/profiler relnames in the etalon",
        results,
    )
    waiting = by_name["t_feedbackwaitinglist"]
    check(waiting.insert == 3291458, "waitinglist insert max from before_settings", results)
    check(waiting.update == 126531975, "waitinglist update max from before_settings", results)
    check(waiting.delete == 2462400, "waitinglist delete max from old_version", results)
    client = by_name["t_client"]
    check(client.insert == 4000000, "t_client insert max from old_version", results)
    check(client.update == 31000000, "t_client update max", results)
    check(client.delete == 0, "t_client delete missing → 0", results)
    txn = by_name["t_transaction"]
    check(txn.insert == 2000000, "t_transaction insert max", results)
    check(txn.update == 65000000, "t_transaction update max from with_settings", results)
    check(txn.delete == 2000000, "t_transaction delete max from with_settings", results)
    check(len(names) == 9, "nine application tables have at least one DML number", results)
    check("t_loadertask" in names, "t_loadertask stays in the etalon", results)

    wiki = build_dml_etalon_wiki(etalon)
    check("||имя таблицы бд||insert||update||delete||" in wiki, "wiki has the four agreed columns", results)
    check("t_repl_agglock" not in wiki, "wiki has no t_repl_* rows", results)
    check("pgse_profile" not in wiki, "wiki has no profiler schema", results)
    check("источник" not in wiki, "wiki table has no source column", results)
    header_line = [line for line in wiki.splitlines() if line.startswith("||")][0]
    check(header_line.count("||") == 5, "wiki header is exactly four columns", results)
    check("max по нескольким проливкам не считался" not in wiki, "multi-file wiki is not marked as single-report", results)

    one = build_dml_etalon([BEFORE])
    one_wiki = build_dml_etalon_wiki(one)
    check(one.single_report, "one file is a single-report etalon", results)
    check("max по нескольким проливкам не считался" in one_wiki, "one-file wiki states max was not taken", results)
    check(one.tables[0].relname == "t_feedbackwaitinglist", "single-file order still puts the heaviest table first", results)


def test_pipeline_and_ui_adapter(results: list[tuple[bool, str]]) -> None:
    with tempfile.TemporaryDirectory(prefix="dml_etalon_") as tmp:
        out = Path(tmp)
        ns = AnalysisSession(
            output_dir=out,
            dml_etalon_reports=[WITH, BEFORE, OLD],
        )
        code = run_pipeline(ns)
        check(code == 0, "run_pipeline dml_etalon exits 0", results)
        payload = json.loads((out / "dml_etalon.json").read_text(encoding="utf-8"))
        check(payload.get("type") == "dml_etalon", "JSON type is dml_etalon", results)
        check(payload.get("report_count") == 3, "JSON report_count is 3", results)
        wiki = (out / "dml_etalon_confluence_stub.wiki").read_text(encoding="utf-8")
        check("|t_feedbackwaitinglist|3291458|126531975|2462400|" in wiki, "wiki row matches JSON max", results)
        check(not (out / "advisor.json").is_file(), "dml_etalon does not write advisor findings", results)
        check(_build_findings_ui(out) == [], "UI findings cards stay empty", results)
        summary = _build_summary(out)
        check(summary.get("dml_etalon", {}).get("table_count", 0) > 0, "summary exposes etalon tables", results)
        by_name = {row["relname"]: row for row in payload["tables"]}
        check(by_name["t_client"]["delete"] == 0, "JSON writes 0 for a missing delete", results)

        metas = [
            ReportMeta(filename=WITH.name, env="NT", label="c", order=0),
            ReportMeta(filename=BEFORE.name, env="NT", label="a", order=1),
            ReportMeta(filename=OLD.name, env="NT", label="b", order=2),
        ]
        session = session_from_request(
            AnalyzeRequest(scenario="dml_etalon", reports=metas, symptoms=[]),
            [WITH, BEFORE, OLD],
            out / "ui",
        )
        check(session.dml_etalon_reports is not None, "UI scenario maps to dml_etalon_reports", results)
        ordered_names = [p.name for p in session.dml_etalon_reports or []]
        check(
            ordered_names == [OLD.name, BEFORE.name, WITH.name],
            "UI adapter orders etalon reports by period, ignoring NT labels",
            results,
        )

    three = [
        ReportMeta(filename=BEFORE.name, env="NT", label="a", order=0),
        ReportMeta(filename=OLD.name, env="NT", label="b", order=1),
        ReportMeta(filename=WITH.name, env="NT", label="c", order=2),
    ]
    check(suggest_scenario(three, []) == "full_multi", "Авто never selects dml_etalon", results)


def test_ui_contract(results: list[tuple[bool, str]]) -> None:
    html = (ROOT / "ui" / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    check(
        '<option value="dml_etalon">Эталон DML с ПРОМ</option>' in html,
        "step 00 has the DML etalon scenario",
        results,
    )
    check('id="dml-etalon-panel"' in html, "result panel has the etalon table", results)
    check("имя таблицы бд" in html, "UI table header uses the agreed column name", results)
    check("dml_etalon:" in app and "SCENARIO_HELP" in app, "app.js has help text for dml_etalon", results)
    check("function renderDmlEtalon(" in app, "app.js renders the etalon table instead of findings", results)
    check(
        'data.scenario === "dml_etalon"' in app,
        "dml_etalon hides the findings checklist / Qwen panel",
        results,
    )
    auto_fn = app.split("function suggestAutoScenario()")[1].split("function ")[0]
    check("dml_etalon" not in auto_fn, "Авто JS does not pick dml_etalon", results)
    needs_env = app.split("const SCENARIO_NEEDS_ENV")[1].split(";")[0]
    check("dml_etalon" not in needs_env, "etalon scenario does not require NT/PROD columns", results)


def main() -> int:
    results: list[tuple[bool, str]] = []
    test_filter_and_max(results)
    test_debug_html_max(results)
    test_pipeline_and_ui_adapter(results)
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
