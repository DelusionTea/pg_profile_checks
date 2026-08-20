#!/usr/bin/env python3
"""Numbers behind the verdicts must be honest.

Four invariants:
  1. Series metrics table: Δ равна разности колонок прогонов, строки по запросам
     не сливаются в одну (`queries.<db>/<user>` не уникален сам по себе).
  2. «Изменено параметров» и блокер «слишком много изменений» считают только
     настраиваемые GUC: runtime-метаданные и производные не считаются.
  3. НТ vs ПРОМ: идентичность стенда (archive_command, cluster_name, ...) и
     «пусто против отсутствует» не идут в критичные расхождения.
  4. Oracle / Качество: если все слои пропущены, вердикт не «PASS».

Seams: _build_series_metrics_table (pgprofile_influence), compare_queries
(pgprofile_compare), pgprofile_classify, build_confidence_meta callers,
render в pgprofile_oracle / pgprofile_quality.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from compare_settings import DiffStatus  # noqa: E402
from pgprofile_classify import (  # noqa: E402
    SETTINGS_ENV_IDENTITY,
    is_tunable_setting,
    split_settings_rows,
    tunable_changed_names,
)
from pgprofile_influence import _build_series_metrics_table  # noqa: E402
from pgprofile_nt_prod import summarize_settings  # noqa: E402
from pgprofile_nt_runs import (  # noqa: E402
    analyze_nt_runs,
    build_nt_runs_confluence_wiki,
    nt_runs_to_dict,
)

NT = [
    ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_07_31_09_00_to=2026_08_01_00.html",
    ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html",
    ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html",
]


def check(cond: bool, label: str, results: list[tuple[bool, str]]) -> None:
    results.append((bool(cond), label))


def _wiki_row(wiki: str, prefix: str) -> str:
    for line in wiki.splitlines():
        if line.startswith(prefix):
            return line
    return ""


def test_series_metrics_consistency(results: list[tuple[bool, str]], nt_runs: dict) -> None:
    table = _build_series_metrics_table(nt_runs)
    rows = table.get("rows") or []
    pair_labels = table.get("pair_labels") or []
    check(bool(rows), "series metrics table is not empty", results)

    bad: list[str] = []
    for row in rows:
        values = row.get("values") or {}
        for pair in pair_labels:
            delta_obj = (row.get("deltas") or {}).get(pair)
            if not isinstance(delta_obj, dict):
                continue
            label_a, label_b = pair.split("->", 1)
            a, b, delta = values.get(label_a), values.get(label_b), delta_obj.get("delta")
            if not all(isinstance(v, (int, float)) for v in (a, b, delta)):
                continue
            if abs(float(delta) - (float(b) - float(a))) > max(0.02 * abs(float(delta)), 0.05):
                bad.append(f"{row.get('metric')} [{pair}]: {a} → {b}, Δ={delta}")
    check(not bad, f"Δ equals the difference of run columns ({'; '.join(bad[:3])})", results)

    query_rows = [r for r in rows if str(r.get("metric") or "").startswith("queries.")]
    distinct_queries = set()
    for pair in nt_runs.get("pair_analyses") or []:
        for finding in pair.get("compare_findings") or []:
            if str(finding.get("category")) != "queries":
                continue
            details = finding.get("details") or {}
            distinct_queries.add((finding.get("message"), details.get("item_id")))
    check(
        len(query_rows) == len(distinct_queries),
        f"one row per query, not per db/user ({len(query_rows)} rows vs {len(distinct_queries)} queries)",
        results,
    )
    check(
        all("hex=" in str(r.get("metric")) for r in query_rows),
        "query rows are labelled with the query hex",
        results,
    )


def test_changed_params_are_tunable(results: list[tuple[bool, str]], wiki: str) -> None:
    metadata = ["pg_conf_load_time", "pg_postmaster_start_time", "shared_memory_size"]
    check(
        not is_tunable_setting("pg_conf_load_time")
        and not is_tunable_setting("shared_memory_size_in_huge_pages"),
        "runtime metadata and derived values are not tunable settings",
        results,
    )
    check(
        is_tunable_setting("shared_buffers") and is_tunable_setting("max_wal_size"),
        "real GUC stays tunable",
        results,
    )
    names = ["shared_buffers", "max_wal_size", *metadata, "transaction_buffers"]
    tunable = tunable_changed_names(names)
    check(
        set(tunable) == {"shared_buffers", "max_wal_size"},
        f"tunable_changed_names drops metadata and derived (got {sorted(tunable)})",
        results,
    )

    row = _wiki_row(wiki, "|Изменено параметров")
    check(
        row.strip() == "|Изменено параметров|4|",
        f"nt_runs counts only tunable changes (got {row.strip() or 'no row'})",
        results,
    )
    check(
        "|Справочно: изменились метаданные и производные" in wiki,
        "metadata changes stay visible as a separate line",
        results,
    )
    blocker = _wiki_row(wiki, "|Слишком много одновременных изменений")
    check("BLOCKER" not in blocker, f"four tunable changes are not a blocker ({blocker})", results)


def test_nt_prod_identity_not_critical(results: list[tuple[bool, str]]) -> None:
    nt = {
        "shared_buffers": "504320",
        "archive_command": "archive-push -B /pgarclogs/05",
        "cluster_name": "pprb_test",
        "primary_slot_name": "tsldd_nt",
        "sec_admin_default_auth": "",
        "pg_conf_load_time": "2026-08-10 12:11:58",
    }
    prod = {
        "shared_buffers": "1026816",
        "archive_command": "archive-push -B /pgarclogs/06",
        "cluster_name": "pprb_prom",
        "primary_slot_name": "pslod_prod",
        "pg_conf_load_time": "2026-06-06 20:32:20",
    }
    summary = summarize_settings(nt, prod)
    critical, informational = split_settings_rows(summary.rows)
    names = [row.name for row in critical]
    check(names == ["shared_buffers"], f"only real GUC is critical (got {names})", results)
    check(summary.critical_count == 1, f"critical_count counts one (got {summary.critical_count})", results)
    check(not summary.valid, "one real GUC mismatch still invalidates the run", results)
    info_names = {row.name for row in informational}
    check(
        {"archive_command", "cluster_name", "primary_slot_name"} <= info_names,
        f"stand identity is reported as informational (got {sorted(info_names)})",
        results,
    )
    check(
        all(row.name != "sec_admin_default_auth" for row in summary.rows if row.status != DiffStatus.SAME),
        "empty vs missing value is not a difference at all",
        results,
    )
    check("archive_command" in SETTINGS_ENV_IDENTITY, "identity list names archive_command", results)


def test_one_number_for_changed_settings(results: list[tuple[bool, str]]) -> None:
    """Шапка отчёта и influence-сводка должны называть одно и то же число."""
    from compare_settings import diff_settings
    from pgprofile_findings import settings_diff_to_dict
    from pgprofile_parser import load_settings, parse_report_meta

    a, b = NT[1], NT[2]
    diff = settings_diff_to_dict(
        label_a="run1",
        label_b="run2",
        path_a=a,
        path_b=b,
        meta_a=parse_report_meta(a),
        meta_b=parse_report_meta(b),
        diffs=diff_settings(load_settings(a), load_settings(b)),
    )
    counted = diff["confidence_meta"]["changed_params_count"]
    changed_names = [row["parameter"] for row in diff["settings_changes"]]
    expected = len(tunable_changed_names(changed_names))
    check(
        counted == expected,
        f"settings_diff counts tunable GUC only ({counted} vs {expected})",
        results,
    )
    check(
        counted < len(changed_names),
        "metadata and derived params are not counted as separate changes",
        results,
    )


def test_conflicting_guc_is_flagged(results: list[tuple[bool, str]]) -> None:
    from pgprofile_confluence import build_stable_prod_confluence_stub
    from pgprofile_stable_prod import analyze_stable_prod

    analysis = analyze_stable_prod(
        NT,
        labels=["run1", "run2", "run3"],
        thresholds_path=ROOT / "thresholds.yaml",
        min_stability_ratio=1.0,
    )
    wiki = build_stable_prod_confluence_stub(analysis)
    plan = wiki.split("h2. План действий", 1)[-1].split("\nh2. ", 1)[0]
    increase = "work_mem: review_increase" in plan
    decrease = "work_mem: review_decrease" in plan
    if increase and decrease:
        check(
            "Противоречивые рекомендации по одному GUC" in plan and "work_mem" in plan,
            "plan warns when one GUC is pulled both ways",
            results,
        )
    else:
        check(True, "plan has no opposite directions for work_mem in this fixture", results)


def test_skipped_is_not_pass(results: list[tuple[bool, str]]) -> None:
    from pgprofile_oracle import OracleReport, render_oracle_markdown

    report = OracleReport(
        verdict="pass",
        layer="rule_based",
        checks=[],
        sources=[],
        skipped=True,
    )
    md = render_oracle_markdown(report)
    check(
        not md.startswith("# Oracle: PASS"),
        f"skipped oracle does not read as PASS (got {md.splitlines()[0] if md else 'empty'})",
        results,
    )
    check("не проверял" in md.lower(), "skipped oracle says it was not checked", results)


def main() -> int:
    results: list[tuple[bool, str]] = []
    for path in NT:
        if not path.is_file():
            print(f"missing fixture: {path}", file=sys.stderr)
            return 1

    analysis = analyze_nt_runs(
        NT,
        labels=["run1", "run2", "run3"],
        symptoms=["high_cpu"],
        health_thresholds_path=ROOT / "thresholds.yaml",
    )
    payload = nt_runs_to_dict(analysis)
    wiki = build_nt_runs_confluence_wiki(analysis)

    test_series_metrics_consistency(results, payload)
    test_changed_params_are_tunable(results, wiki)
    test_nt_prod_identity_not_critical(results)
    test_one_number_for_changed_settings(results)
    test_conflicting_guc_is_flagged(results)
    test_skipped_is_not_pass(results)

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
