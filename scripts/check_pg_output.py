#!/usr/bin/env python3
"""User-facing wiki for every PG scenario: problems, severity, what to do.

A reader should see what is wrong, how bad it is, and the next action — without
duplicate tables, playbook refute steps, or profiler-table noise.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pgprofile_confluence import (  # noqa: E402
    build_nt_prod_confluence_stub,
    build_stable_prod_confluence_stub,
    build_symptom_confluence_stub,
)
from pgprofile_nt_prod import validate_nt_prod  # noqa: E402
from pgprofile_nt_runs import analyze_nt_runs, build_nt_runs_confluence_wiki  # noqa: E402
from pgprofile_stable_prod import analyze_stable_prod  # noqa: E402
from pgprofile_symptoms import investigate_symptom  # noqa: E402

JUL31 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_07_31_09_00_to=2026_08_01_00.html"
AUG11 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html"
AUG12 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html"
THRESHOLDS = ROOT / "thresholds.yaml"


def check(cond: bool, label: str, results: list[tuple[bool, str]]) -> None:
    results.append((bool(cond), label))


def _section(wiki: str, heading: str) -> str:
    if heading not in wiki:
        return ""
    return wiki.split(heading, 1)[1].split("\nh2. ", 1)[0]


def _actions(wiki: str, heading: str = "h2. Что сделать сейчас") -> list[str]:
    block = _section(wiki, heading)
    if not block and heading == "h2. Что сделать сейчас":
        block = _section(wiki, "h2. Следующие действия")
    return [line[2:].strip() for line in block.splitlines() if line.startswith("# ")]


def test_symptom(results: list[tuple[bool, str]]) -> None:
    inv = investigate_symptom(
        "high_wal",
        [JUL31, AUG11, AUG12],
        labels=["nt1", "prod1", "prod2"],
        health_thresholds_path=THRESHOLDS,
    )
    wiki = build_symptom_confluence_stub(inv)
    head = wiki.split("h2. ", 1)[0]
    check("Подтверждено:" in head, "symptom: verdict lists confirmed causes", results)
    check("FAIL=confirmed" not in wiki, "symptom: no FAIL=confirmed jargon", results)
    check("h2. Сводка гипотез" not in wiki, "symptom: no duplicate findings table", results)
    check("h2. Гипотезы (возможные причины)" in wiki, "symptom: one hypotheses table with evidence", results)
    actions = _actions(wiki)
    check(bool(actions), "symptom: has what-to-do", results)
    check(all(" → " in a for a in actions), "symptom: each action names the cause", results)
    check(
        not any(a.lower().startswith("(опровергнуть") or "опровергнуть" in a.lower()[:20] for a in actions),
        "symptom: refute steps are not in the now-list",
        results,
    )
    check(
        any("wal_buffers" in a.lower() for a in actions),
        "symptom: confirmed wal_buffers overflow has a fix",
        results,
    )


def test_nt_prod(results: list[tuple[bool, str]]) -> None:
    validation = validate_nt_prod(AUG11, AUG12, nt_label="nt1", prod_label="prod1")
    wiki = build_nt_prod_confluence_stub(validation)
    check("h2. Что сделать сейчас" in wiki, "nt_prod: has what-to-do", results)
    actions = _actions(wiki)
    check(bool(actions), "nt_prod: actions are not empty", results)
    if not validation.settings.valid:
        check(
            any("Выровнять" in a and "shared_buffers" in a for a in actions),
            "nt_prod: invalid run tells to align the differing GUC",
            results,
        )
        before_expand = wiki.split("{expand:", 1)[0]
        check(
            "h2. Cache — предупреждения" not in before_expand,
            "nt_prod: warning metrics are not above the fold when GUC differ",
            results,
        )
    check(
        "select $3" in wiki.lower() or "object_id" in wiki.lower() or "t_participant" in wiki.lower(),
        "nt_prod: SQL tables show a query preview, not only the user name",
        results,
    )


def test_nt_runs(results: list[tuple[bool, str]]) -> None:
    analysis = analyze_nt_runs(
        [JUL31, AUG11, AUG12],
        labels=["nt1", "nt2", "nt3"],
        symptoms=["high_cpu"],
        health_thresholds_path=THRESHOLDS,
    )
    wiki = build_nt_runs_confluence_wiki(analysis)
    check("|Почему|" in wiki, "nt_runs: verdict says why GO/NO-GO", results)
    check(wiki.count("Расшифровка колонки «Связь»:") <= 1, "nt_runs: link legend is not repeated per pair", results)
    check("pg_ident_conf" not in wiki or "длинный текст" in wiki, "nt_runs: does not dump pg_ident_conf comments", results)
    pair_block = _section(wiki, "h3. Связь настроек и метрик по парам")
    check(
        "pg_conf_load_time" not in pair_block,
        "nt_runs: pair tables drop runtime metadata",
        results,
    )
    check(
        "Параметр не связан с выбранными проблемами" not in pair_block,
        "nt_runs: no NO-LINK filler rows in the metric table",
        results,
    )
    actions = _actions(wiki, "h2. Следующие действия")
    check(bool(actions), "nt_runs: has next actions", results)
    check(
        not any("(опровергнуть" in a.lower() for a in actions),
        "nt_runs: next actions are not refute playbook",
        results,
    )


def test_stable_prod_verdict(results: list[tuple[bool, str]]) -> None:
    analysis = analyze_stable_prod(
        [JUL31, AUG11, AUG12],
        labels=["prod1", "prod2", "prod3"],
        thresholds_path=THRESHOLDS,
        min_stability_ratio=1.0,
    )
    wiki = build_stable_prod_confluence_stub(analysis)
    head = wiki.split("h2. ", 1)[0]
    check("Главное:" in head, "stable_prod: verdict names the main problems", results)
    check("Min stability" not in head, "stable_prod: no stability-ratio jargon in the verdict", results)


def main() -> int:
    results: list[tuple[bool, str]] = []
    for path in (JUL31, AUG11, AUG12):
        if not path.is_file():
            print(f"missing fixture: {path}", file=sys.stderr)
            return 1
    test_symptom(results)
    test_nt_prod(results)
    test_nt_runs(results)
    test_stable_prod_verdict(results)
    failed = [label for ok, label in results if not ok]
    for ok, label in results:
        print(("OK  " if ok else "FAIL") + " " + label)
    if failed:
        print(f"FAIL: {len(failed)}/{len(results)}", file=sys.stderr)
        return 1
    print(f"OK: {len(results)} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
