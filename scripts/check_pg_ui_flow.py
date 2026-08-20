#!/usr/bin/env python3
"""PG UI flow: scenario is step 00, several reports are the default.

Contract:
  * выбор сценария живёт вне «Расширенных настроек» и стоит до панели отчётов;
  * дефолт — «Авто»: один файл → health, два и больше → полный анализ;
  * загрузка не режет список до одного файла; один отчёт берётся только
    при явном сценарии health.
Seams: index.html (#scenario-panel), app.js (selectedScenario / addFiles /
runAnalysis), analysis_runner.suggest_scenario.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.analysis_runner import (  # noqa: E402
    AnalyzeRequest,
    ReportMeta,
    session_from_request,
    suggest_scenario,
)

JUL31 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_07_31_09_00_to=2026_08_01_00.html"
AUG11 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html"
AUG12 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html"


def check(cond: bool, label: str, results: list[tuple[bool, str]]) -> None:
    results.append((bool(cond), label))


def test_html_contract(results: list[tuple[bool, str]]) -> None:
    html = (ROOT / "ui" / "web" / "index.html").read_text(encoding="utf-8")
    scenario_at = html.find('id="scenario"')
    panel_at = html.find('id="scenario-panel"')
    report_at = html.find('id="report-panel"')
    advanced_at = html.find('id="advanced-settings"')
    check(panel_at != -1, "index.html has a scenario panel (step 00)", results)
    check("00 · СЦЕНАРИЙ" in html, "scenario panel is numbered as step 00", results)
    check(
        panel_at != -1 and report_at != -1 and panel_at < report_at,
        "scenario panel stands before the reports panel",
        results,
    )
    check(
        scenario_at != -1 and advanced_at != -1 and scenario_at < advanced_at,
        "scenario select is outside «Расширенные настройки»",
        results,
    )
    auto_option = '<option value="auto" selected>'
    check(auto_option in html, "«Авто» is the preselected scenario", results)
    check(
        '<option value="health" selected>' not in html,
        "health is no longer the default scenario",
        results,
    )
    check(
        "только первый файл" not in html,
        "upload hint no longer promises a single-file analysis",
        results,
    )
    check(
        '<option value="dml_etalon">Эталон DML с ПРОМ</option>' in html,
        "step 00 includes the DML etalon scenario",
        results,
    )
    check(
        html.count('id="scenario"') == 1 and html.count('id="auto-scenario-preview"') == 1,
        "scenario controls are not duplicated",
        results,
    )


def test_app_js_contract(results: list[tuple[bool, str]]) -> None:
    app = (ROOT / "ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    check("function selectedScenario()" in app, "app.js reads the scenario from step 00", results)
    check("function effectiveScenario()" in app, "app.js resolves «Авто» locally for hints", results)
    check(
        'adv ? els.scenario.value : "health"' not in app,
        "scenario no longer depends on the advanced-settings toggle",
        results,
    )
    check(
        'if (scenario === "health") {\n      sorted = sorted.slice(0, 1);' in app,
        "only the explicit health scenario trims the list to one report",
        results,
    )
    check(
        "простой режим: взят" not in app and "будет использован самый ранний отчёт" not in app,
        "addFiles keeps every uploaded report",
        results,
    )
    check("scenarioPanel" in app, "app.js hides the scenario panel in JVM mode", results)
    check(
        'els.scenario.addEventListener("change", renderReports)' in app,
        "changing the scenario redraws the reports table (headers match rows)",
        results,
    )
    check("SCENARIO_NEEDS_ENV" in app, "env/label columns open for НТ/ПРОМ scenarios", results)


def test_backend_defaults(results: list[tuple[bool, str]]) -> None:
    one = [ReportMeta(filename=AUG11.name, env="NT", label="a", order=0)]
    three = [
        ReportMeta(filename=JUL31.name, env="NT", label="a", order=0),
        ReportMeta(filename=AUG11.name, env="NT", label="b", order=1),
        ReportMeta(filename=AUG12.name, env="NT", label="c", order=2),
    ]
    check(suggest_scenario(one, []) == "health", "auto + 1 report → health", results)
    check(suggest_scenario(three, []) == "full_multi", "auto + 3 reports → full_multi", results)
    check(suggest_scenario(three, []) != "dml_etalon", "Авто never selects dml_etalon", results)

    out = Path(tempfile.mkdtemp(prefix="pg_ui_flow_"))
    ns = session_from_request(
        AnalyzeRequest(scenario="full_multi", reports=three, symptoms=[]),
        [JUL31, AUG11, AUG12],
        out,
    )
    check(
        len(ns.stable_prod_reports or []) == 3,
        "all three uploaded reports reach the pipeline",
        results,
    )

    ns_health = session_from_request(
        AnalyzeRequest(scenario="health", reports=one, symptoms=[]),
        [AUG11],
        out,
    )
    check(ns_health.report == AUG11, "explicit health still analyses one report", results)


def main() -> int:
    results: list[tuple[bool, str]] = []
    test_html_contract(results)
    test_app_js_contract(results)
    test_backend_defaults(results)
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
