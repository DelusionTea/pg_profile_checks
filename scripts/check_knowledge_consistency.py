#!/usr/bin/env python3
"""Lint knowledge YAML: finding_ids in prod_tuning must have recommendations; GUC coverage."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REC = ROOT / "knowledge" / "recommendations.yaml"
TUNING = ROOT / "knowledge" / "prod_tuning.yaml"
GUIDANCE = ROOT / "knowledge" / "guc_guidance.yaml"
IMPACT = ROOT / "knowledge" / "guc_impact.yaml"


def main() -> int:
    rec_ids = {
        item["id"]
        for item in (yaml.safe_load(REC.read_text(encoding="utf-8")) or {}).get(
            "recommendations", []
        )
        if isinstance(item, dict) and item.get("id")
    }
    tuning = yaml.safe_load(TUNING.read_text(encoding="utf-8")) or {}
    missing_rec: list[str] = []
    for rule in tuning.get("tuning_rules") or []:
        for fid in rule.get("finding_ids") or []:
            if fid not in rec_ids and not fid.endswith(".generic"):
                missing_rec.append(f"{fid} (rule {rule.get('id')})")

    guidance = (yaml.safe_load(GUIDANCE.read_text(encoding="utf-8")) or {}).get(
        "guc_guidance", {}
    )
    impact = (yaml.safe_load(IMPACT.read_text(encoding="utf-8")) or {}).get("guc_impact", {})
    missing_impact = sorted(set(guidance) - set(impact))

    errors = 0
    if missing_rec:
        errors += 1
        print("Missing recommendations for prod_tuning finding_ids:")
        for item in sorted(set(missing_rec)):
            print(f"  - {item}")
    if missing_impact:
        print("GUC in guidance but not in guc_impact (warn):")
        for guc in missing_impact:
            print(f"  - {guc}")

    if errors:
        print(f"FAIL: {errors} error group(s)", file=sys.stderr)
        return 1
    print(
        f"OK: {len(rec_ids)} recommendations, "
        f"{len(guidance)} guc_guidance, {len(impact)} guc_impact"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
