#!/usr/bin/env python3
"""UX contract for health/compare Confluence stubs: verdict, what changed, what to do.

Readers (analyst + DBA) must see a Russian above-the-fold story; long tables live in Expand.
Fixtures: analysis_out_test health and pair advisor.json (produced by e2e / case matrix).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pgprofile_advisor import AdvisedFinding, AdvisorReport  # noqa: E402
from pgprofile_confluence import build_confluence_stub  # noqa: E402

HEALTH_ADVISOR = ROOT / "analysis_out_test" / "case_matrix" / "health_single_report" / "advisor.json"
PAIR_ADVISOR = ROOT / "analysis_out_test" / "e2e" / "pair" / "advisor.json"
PAIR_FALLBACK = ROOT / "analysis_out_test" / "pair_influence_case" / "advisor.json"


def check(cond: bool, label: str, results: list[tuple[bool, str]]) -> None:
    results.append((bool(cond), label))


def reports_from_advisor_json(path: Path) -> list[AdvisorReport]:
    data = json.loads(path.read_text(encoding="utf-8"))
    reports: list[AdvisorReport] = []
    for raw in data.get("reports") or []:
        advised = [
            AdvisedFinding(
                finding=item.get("finding") or {},
                advice=item.get("advice") or {},
                guc_guidance=item.get("guc_guidance"),
            )
            for item in raw.get("advised_findings") or []
        ]
        reports.append(
            AdvisorReport(
                source_type=str(raw.get("source_type") or "unknown"),
                meta=raw.get("meta") or {},
                advised_findings=advised,
                summary=raw.get("summary") or {},
            )
        )
    return reports


def _action_lines(wiki: str) -> list[str]:
    lines = wiki.splitlines()
    out: list[str] = []
    in_actions = False
    for line in lines:
        if line.startswith("h2. Что сделать"):
            in_actions = True
            continue
        if in_actions:
            if line.startswith("h2.") or line.startswith("{expand"):
                break
            if line.startswith("# "):
                out.append(line[2:].strip())
    return out


def _pair_advisor_path() -> Path:
    if PAIR_ADVISOR.is_file():
        return PAIR_ADVISOR
    return PAIR_FALLBACK


def check_health(results: list[tuple[bool, str]]) -> None:
    path = HEALTH_ADVISOR if HEALTH_ADVISOR.is_file() else _pair_advisor_path()
    reports = [
        r for r in reports_from_advisor_json(path) if r.source_type == "health_check"
    ]
    wiki = build_confluence_stub(reports)
    check("Всё ли хорошо" in wiki, "health: verdict answers «всё ли хорошо»", results)
    check("h2. Что сделать сейчас" in wiki, "health: has what-to-do section", results)
    check(
        "h2. Что изменилось" not in wiki,
        "health: single report does not pretend something changed between runs",
        results,
    )
    check("{expand:" in wiki, "health: long tables are in Expand", results)
    check(
        wiki.find("h2. Что сделать сейчас") < wiki.find("{expand:"),
        "health: actions come before Expand tables",
        results,
    )
    check("run_compare." not in wiki, "health: no raw run_compare finding ids", results)
    actions = _action_lines(wiki)
    idle = [a for a in actions if "idle_in_transaction_session_timeout" in a]
    check(len(idle) <= 1, "health: idle-in-transaction action is not repeated", results)
    check(bool(actions), "health: at least one action", results)
    check(
        all(" → " in a for a in actions),
        "health: each action names the problem it belongs to",
        results,
    )
    check(
        len({a.split(" → ", 1)[0] for a in actions}) == len(actions),
        "health: one action per problem, no duplicate problem lines",
        results,
    )
    head = wiki.split("h2. ", 1)[0]
    check(
        "Requested checkpoints" not in head and "threshold 30%" not in head,
        "health: verdict does not dump a raw English finding line",
        results,
    )
    check(
        "Контрольные точки" in head or "Сессии" in head,
        "health: verdict names problem areas in Russian",
        results,
    )
    actions_block = wiki.split("h2. Что сделать сейчас", 1)[1].split("h2.", 1)[0]
    check(
        "pgse_profile" not in actions_block,
        "health: plan does not send the user to profiler tables",
        results,
    )
    check(
        wiki.count("|queries.slow_execution|") <= 1,
        "health: slow SQL findings are collapsed to one row",
        results,
    )


def check_pair(results: list[tuple[bool, str]]) -> None:
    reports = reports_from_advisor_json(_pair_advisor_path())
    wiki = build_confluence_stub(reports)
    check("Всё ли хорошо" in wiki, "pair: verdict answers «всё ли хорошо»", results)
    check("h2. Что изменилось" in wiki, "pair: has what-changed section", results)
    check("h2. Что сделать сейчас" in wiki, "pair: has what-to-do section", results)
    check(
        wiki.find("h2. Что изменилось") < wiki.find("h2. Что сделать сейчас"),
        "pair: what-changed comes before what-to-do",
        results,
    )
    changed = wiki.split("h2. Что сделать сейчас", 1)[0]
    check("shared_buffers" in changed, "pair: GUC change is visible above the fold", results)
    check("run_compare." not in wiki, "pair: no raw run_compare finding ids in wiki", results)
    check("settings.differ." not in wiki, "pair: no raw settings.differ ids in wiki", results)
    check("{expand:" in wiki, "pair: long tables are in Expand", results)
    check(
        "|run_compare." not in wiki,
        "pair: findings tables do not dump empty compare rows",
        results,
    )
    actions = _action_lines(wiki)
    idle = [a for a in actions if "idle_in_transaction_session_timeout" in a]
    check(len(idle) <= 1, "pair: idle-in-transaction action is not repeated", results)


def check_clean_health(results: list[tuple[bool, str]]) -> None:
    report = AdvisorReport(
        source_type="health_check",
        meta={"report_meta": {"server": "db.example", "report_end": "2026-08-12"}},
        advised_findings=[],
        summary={"total_findings": 0, "high_priority": 0},
    )
    wiki = build_confluence_stub([report])
    check("Всё ли хорошо" in wiki, "clean: verdict heading present", results)
    check(
        "критичных проблем нет" in wiki.lower() or "да" in wiki.lower(),
        "clean: states that things are OK",
        results,
    )
    check("h2. Что изменилось" not in wiki, "clean: no false what-changed section", results)


def main() -> int:
    results: list[tuple[bool, str]] = []
    missing = [p for p in (_pair_advisor_path(),) if not p.is_file()]
    if missing:
        print("Missing fixtures:")
        for path in missing:
            print(f"  {path}")
        print("Run scripts/check_e2e.py or scripts/check_smoke.py --full first.")
        return 1

    check_clean_health(results)
    check_health(results)
    check_pair(results)

    failed = [label for ok, label in results if not ok]
    for ok, label in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if failed:
        print(f"\nCONFLUENCE_UX_FAILED ({len(failed)} of {len(results)})")
        return 1
    print(f"\nCONFLUENCE_UX_PASSED ({len(results)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
