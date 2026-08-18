#!/usr/bin/env python3
"""Validate contract fixture files (valid_* must pass, invalid_* must fail)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

def main() -> int:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from pgprofile_contracts import validate_contract_payload

    fixtures_dir = root / "resources" / "contract_fixtures"
    fixtures = sorted(fixtures_dir.glob("*.json"))
    if not fixtures:
        print("No fixtures found.")
        return 1

    failures: list[str] = []
    for fixture in fixtures:
        if not (fixture.name.startswith("valid_") or fixture.name.startswith("invalid_")):
            print(f"[SKIP] {fixture.name}")
            continue
        expect_valid = fixture.name.startswith("valid_")
        payload = json.loads(fixture.read_text(encoding="utf-8"))

        try:
            validate_contract_payload(payload)
            is_valid = True
        except ValueError:
            is_valid = False

        if expect_valid != is_valid:
            failures.append(
                f"{fixture.name}: expected {'valid' if expect_valid else 'invalid'}, "
                f"got {'valid' if is_valid else 'invalid'}"
            )
            status = "FAIL"
        else:
            status = "PASS"

        print(f"[{status}] {fixture.name}")

    if failures:
        print("\nValidation failures:")
        for item in failures:
            print(f"- {item}")
        return 1

    print("\nALL_FIXTURE_CHECKS_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
