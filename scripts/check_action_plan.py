#!/usr/bin/env python3
"""Action plan in the «Стабильные / общие проблемы» Confluence stub.

The reader must get a plan per problem: чем подтвердить, что изменить, по какому
признаку убедиться. A flat list of eight lines about one problem is not a plan.

Seam: _stable_prod_action_plan_wiki / build_stable_prod_confluence_stub.
Fixtures: three demo pg_profile reports from resources/.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pgprofile_confluence import build_stable_prod_confluence_stub  # noqa: E402
from pgprofile_stable_prod import analyze_stable_prod  # noqa: E402

REPORTS = [
    ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_07_31_09_00_to=2026_08_01_00.html",
    ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html",
    ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html",
]
THRESHOLDS = ROOT / "thresholds.yaml"


def check(cond: bool, label: str, results: list[tuple[bool, str]]) -> None:
    results.append((bool(cond), label))


def _section(wiki: str, heading: str) -> str:
    """Text of a h2 section, up to the next h2."""
    if heading not in wiki:
        return ""
    tail = wiki.split(heading, 1)[1]
    return tail.split("\nh2. ", 1)[0]


def _numbered_items(text: str) -> list[str]:
    return [line[2:].strip() for line in text.splitlines() if line.startswith("# ")]


def main() -> int:
    results: list[tuple[bool, str]] = []
    for path in REPORTS:
        if not path.is_file():
            print(f"missing fixture: {path}", file=sys.stderr)
            return 1

    analysis = analyze_stable_prod(
        REPORTS,
        labels=["prod1", "prod2", "prod3"],
        thresholds_path=THRESHOLDS,
        min_stability_ratio=1.0,
    )
    wiki = build_stable_prod_confluence_stub(analysis)

    check("h2. План действий" in wiki, "stable_prod: wiki has an action plan section", results)
    plan = _section(wiki, "h2. План действий")
    problems = [line for line in plan.splitlines() if line.startswith("h3. ")]
    check(len(problems) >= 3, f"plan covers several problems (got {len(problems)})", results)
    check(
        any("CRITICAL" in line for line in problems),
        "plan puts a CRITICAL problem among the steps",
        results,
    )
    check(
        problems and "CRITICAL" in problems[0],
        "plan starts with the most severe problem",
        results,
    )
    check("*Подтвердить:*" in plan, "each step says how to confirm the problem", results)
    check("*Изменить:*" in plan, "each step says what to change", results)
    check("*Убедиться:*" in plan, "each step says how to verify the fix", results)

    # The verification line must name a checkable signal: the finding id.
    verify_lines = [line for line in plan.splitlines() if line.startswith("*Убедиться:*")]
    check(
        any("." in line and "finding" in line.lower() for line in verify_lines),
        "verification names the finding that must stop reproducing",
        results,
    )

    # GUC changes carry current values and how risky the change is.
    check(
        "сейчас prod1=" in plan,
        "plan shows the current GUC values per report",
        results,
    )
    check(
        "reload" in plan or "restart" in plan,
        "plan says whether the change needs reload or restart",
        results,
    )

    # The short list above the fold must not be eight lines about one problem.
    now = _section(wiki, "h2. Что сделать сейчас")
    items = _numbered_items(now)
    check(bool(items), "«Что сделать сейчас» is not empty", results)
    problems_named = {i.split(" → ", 1)[0] for i in items if " → " in i}
    check(
        len(problems_named) == len(items) and len(problems_named) >= 3,
        f"«Что сделать сейчас» gives one action per problem (got {len(problems_named)} "
        f"problems in {len(items)} lines)",
        results,
    )
    check(
        len(set(items)) == len(items),
        "«Что сделать сейчас» has no repeated lines",
        results,
    )
    check(
        all(" → " in i for i in items),
        "each line says which problem it belongs to",
        results,
    )
    # A caveat must not be filed under «Изменить».
    change_block = plan.split("*Изменить:*")
    misfiled = [
        line
        for block in change_block[1:]
        for line in block.split("*")[0].splitlines()
        if line.startswith("# ") and line[2:].strip().lower().startswith("не ")
    ]
    check(not misfiled, f"no warning is filed as a change (got {misfiled[:1]})", results)

    # Subset duplicates ("Установить X" vs "Установить X (например 60s–300s)") are dropped.
    plan_actions = _numbered_items(plan) + items
    lowered = [a.lower().rstrip(".") for a in plan_actions]
    subset_dupes = [
        (a, b)
        for a in lowered
        for b in lowered
        if a != b and b.startswith(a)
    ]
    check(
        not subset_dupes,
        f"no action repeats a shorter version of itself (got {subset_dupes[:1]})",
        results,
    )

    check(
        wiki.find("h2. План действий") < wiki.find("h2. Сводка общих findings"),
        "plan stands above the long findings tables",
        results,
    )
    verdict = wiki.split("h2. ", 1)[0]
    check("Min stability" not in verdict, "verdict does not expose internal stability jargon", results)
    check("tuning:" not in verdict, "verdict does not say «tuning: N»", results)
    check("Главное:" in verdict, "verdict names the main problems", results)
    check("Idle in transaction" in verdict or "idle" in verdict.lower(), "verdict names the critical idle-in-transaction problem", results)

    first_change = plan.split("*Изменить:*", 1)[1].split("*", 1)[0]
    pooling = [
        line
        for line in first_change.splitlines()
        if "pgbouncer" in line.lower() or "connection pool" in line.lower()
    ]
    check(len(pooling) <= 1, f"one pooling action per problem (got {pooling})", results)

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
