#!/usr/bin/env python3
"""LLM answer oracle: JSON structure, grounded claims, quality score, publish gate."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pgprofile_llm import DryRunProvider, LLMRequest  # noqa: E402
from pgprofile_llm_tasks import build_prompt_bundle  # noqa: E402
from pgprofile_llm_validate import (  # noqa: E402
    evaluate_llm_answer,
    extract_json_object,
    load_evidence_catalog,
    record_llm_quality,
)
from pgprofile_oracle import LAYER_LLM, evaluate_output_dir  # noqa: E402

PAIR_DIR = ROOT / "analysis_out_test" / "pair_influence_case"
results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    results.append((bool(condition), label))


def _answer(**overrides: object) -> dict:
    payload = {
        "verdict": "need-validation",
        "summary": "shared_buffers вырос вместе с blks_read; связь вероятная.",
        "claims": [
            {
                "statement": "Рост shared_buffers совпал с ростом чтений с диска.",
                "subject": "shared_buffers",
                "evidence_type": "probable",
            }
        ],
        "recommendations": [],
        "risks": ["изменено несколько параметров сразу"],
        "missing_data": [],
    }
    payload.update(overrides)
    return payload


def main() -> int:
    if not PAIR_DIR.is_dir():
        print("Missing pair influence fixture.")
        return 1
    catalog = load_evidence_catalog(PAIR_DIR)
    check(
        "shared_buffers" in catalog["parameters"],
        "catalog sees shared_buffers from the pair table",
    )

    grounded = evaluate_llm_answer(json.dumps(_answer()), catalog, task="summary")
    check(grounded["parsed"], "grounded JSON is parsed")
    check(grounded["verdict"] == "pass", "grounded probable claim is pass")
    check(grounded["publishable"] is True, "grounded answer is publishable")
    check(grounded["score"] >= 80, "grounded answer scores at least 80")

    fenced = evaluate_llm_answer(
        "перед блоком\n```json\n" + json.dumps(_answer()) + "\n```\n",
        catalog,
        task="summary",
    )
    check(fenced["parsed"] and fenced["verdict"] == "pass", "JSON inside a fence is accepted")

    raw_text = evaluate_llm_answer("просто текст без json", catalog, task="summary")
    check(raw_text["verdict"] == "fail", "non-JSON answer is fail")
    check(raw_text["publishable"] is False, "non-JSON answer is not publishable")
    check(raw_text["score"] == 0, "non-JSON answer scores 0")

    missing = dict(_answer())
    missing.pop("claims")
    missing.pop("summary")
    broken = evaluate_llm_answer(json.dumps(missing), catalog, task="summary")
    check(broken["verdict"] == "fail", "missing required JSON fields is fail")

    fake = _answer(
        claims=[
            {
                "statement": "выдуманный эффект",
                "subject": "totally_fake_guc",
                "evidence_type": "probable",
            }
        ]
    )
    ungrounded = evaluate_llm_answer(json.dumps(fake), catalog, task="summary")
    check(ungrounded["verdict"] == "fail", "claim subject missing from artifacts is fail")
    check(
        "totally_fake_guc" in ungrounded.get("unsupported_subjects", []),
        "unsupported subject is listed",
    )

    over = _answer(
        claims=[
            {
                "statement": "доказано, что shared_buffers улучшил WAL",
                "subject": "shared_buffers",
                "evidence_type": "proven",
            }
        ]
    )
    overclaim = evaluate_llm_answer(json.dumps(over), catalog, task="summary")
    check(overclaim["verdict"] == "fail", "proven claim on a probable row is fail")
    check("shared_buffers" in overclaim.get("overclaims", []), "overclaim names the parameter")

    tiny = {
        "parameters": {"shared_buffers"},
        "metrics": {"cache.postgres.blks_read", "blks_read"},
        "evidence_by_param": {"shared_buffers": "probable"},
        "attributed_count": 1,
        "subjects": {"shared_buffers", "cache.postgres.blks_read", "blks_read"},
    }
    qualified = evaluate_llm_answer(
        json.dumps(
            _answer(
                claims=[
                    {
                        "statement": "чтения с диска выросли",
                        "subject": "blks_read",
                        "evidence_type": "probable",
                    }
                ]
            )
        ),
        tiny,
        task="summary",
    )
    check(qualified["publishable"] is True, "qualified metric last segment still grounds")
    for short in ("wal", "read", "buffers"):
        fuzzy = evaluate_llm_answer(
            json.dumps(
                _answer(
                    claims=[
                        {
                            "statement": "размытое совпадение по подстроке",
                            "subject": short,
                            "evidence_type": "probable",
                        }
                    ]
                )
            ),
            tiny,
            task="summary",
        )
        check(fuzzy["verdict"] == "fail", f"substring subject {short!r} is fail")
        check(fuzzy["publishable"] is False, f"substring subject {short!r} is not publishable")
        check(
            short in fuzzy.get("unsupported_subjects", []),
            f"substring subject {short!r} is listed as unsupported",
        )
    over_cased = evaluate_llm_answer(
        json.dumps(
            _answer(
                claims=[
                    {
                        "statement": "доказано, что Shared_Buffers всё улучшил",
                        "subject": "Shared_Buffers",
                        "evidence_type": "proven",
                    }
                ]
            )
        ),
        tiny,
        task="summary",
    )
    check(over_cased["verdict"] == "fail", "overclaim lookup uses the same key as grounding")
    check(
        "Shared_Buffers" in over_cased.get("overclaims", []),
        "cased overclaim still names the claim subject",
    )

    dry = DryRunProvider().generate(LLMRequest(prompt="данные", task="summary"))
    parsed = extract_json_object(dry.text)
    check(
        parsed is not None and parsed.get("verdict") == "need-validation",
        "dry-run emits valid answer JSON",
    )
    check("dry-run" in dry.text, "dry-run text still contains dry-run")
    dry_quality = evaluate_llm_answer(dry.text, catalog, task="summary", dry_run=True)
    check(dry_quality["publishable"] is False, "dry-run is not publishable")
    check(dry_quality["verdict"] == "warning", "valid dry-run JSON is warning, not fail")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        shutil.copy(PAIR_DIR / "influence_table.json", out / "influence_table.json")
        recorded = record_llm_quality(
            out, json.dumps(_answer()), task="summary", dry_run=False
        )
        check((out / "llm_quality_summary.json").is_file(), "writes llm_quality_summary.json")
        check(recorded.get("publishable") is True, "recorded grounded answer stays publishable")
        hybrid = evaluate_output_dir(out)
        check(
            any(item.get("name") == LAYER_LLM and not item.get("skipped") for item in hybrid.layers),
            "oracle report gains an llm layer after quality is recorded",
        )
        check((out / "oracle_report.json").is_file(), "refreshes oracle_report.json")

    bundle = build_prompt_bundle(PAIR_DIR, task="summary")
    check(
        "verdict" in bundle.system,
        "prompt asks for the JSON answer schema",
    )

    failed = [label for ok, label in results if not ok]
    for ok, label in results:
        print(f"{'PASS' if ok else 'FAIL'}: {label}")
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
