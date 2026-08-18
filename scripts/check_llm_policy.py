#!/usr/bin/env python3
"""Checks for the LLM send-policy template.

Default is none (identity). Enabling a named profile must redact/drop data
without breaking bundle validation or dry-run generation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pgprofile_llm import DryRunProvider  # noqa: E402
from pgprofile_llm_policy import (  # noqa: E402
    LLMPolicyError,
    LlmPolicy,
    NONE_POLICY,
    apply_policy,
    describe_policy,
    load_active_policy,
    parse_policy,
)
from pgprofile_llm_tasks import (  # noqa: E402
    LLMBundleError,
    build_prompt_bundle,
    write_llm_artifacts,
)

CASE_DIR = ROOT / "analysis_out_test" / "case_matrix" / "nt_runs_3nt_one_symptom"
results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    results.append((bool(condition), label))


def check_default_none() -> None:
    previous = os.environ.pop("PGPROFILE_LLM_POLICY", None)
    try:
        policy = load_active_policy()
        check(policy.name == "none", "yaml default active policy is none")
        check(not policy.enabled, "none policy is not enabled")
        snapshot = describe_policy()
        check(snapshot["name"] == "none", "describe_policy reports none")
        check(snapshot["sanitization"] is False, "describe_policy sanitization is off")
    finally:
        if previous is not None:
            os.environ["PGPROFILE_LLM_POLICY"] = previous


def check_identity_apply() -> None:
    sections = [
        ("brief", "Сводка", "brief.md", "host 10.1.2.3 password=secret SQL"),
        ("findings", "Находки", "findings.json", "SELECT 1"),
    ]
    kept, extra, report = apply_policy(sections, NONE_POLICY, extra_instructions="keep me")
    check(kept == sections, "none policy does not drop sections")
    check(extra == "keep me", "none policy does not rewrite extras")
    check(report["unchanged"] is True, "none policy report is unchanged")


def check_disabled_profile_is_noop() -> None:
    dormant = parse_policy(
        "draft",
        {
            "sanitization": False,
            "deny_sections": ["findings"],
            "mask": [{"name": "ipv4", "pattern": r"\d+\.\d+\.\d+\.\d+", "replacement": "<ip>"}],
        },
    )
    sections = [("findings", "Находки", "findings.json", "10.0.0.1")]
    kept, extra, report = apply_policy(sections, dormant)
    check(kept == sections, "sanitization=false ignores deny_sections")
    check("10.0.0.1" in kept[0][3], "sanitization=false ignores mask")
    check(not report["enabled"], "sanitization=false is not enabled")
    check(extra == "", "noop extra stays empty")


def check_bank_redact_from_yaml() -> None:
    previous = os.environ.get("PGPROFILE_LLM_POLICY")
    os.environ["PGPROFILE_LLM_POLICY"] = "bank_redact"
    try:
        policy = load_active_policy()
        check(policy.name == "bank_redact", "env selects bank_redact")
        check(policy.enabled, "bank_redact is enabled")
        case_bundle = build_prompt_bundle(CASE_DIR, task="summary", policy=policy)
        check("# DATA:" in case_bundle.prompt, "real analysis fixture still bundles under bank_redact")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "brief.md").write_text("вердикт GO, host 10.1.2.3\n", encoding="utf-8")
            (out / "findings.json").write_text(
                json.dumps(
                    {
                        "findings": [
                            {
                                "severity": "high",
                                "id": "wal.size",
                                "message": "WAL growth on 10.1.2.3",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            baseline = build_prompt_bundle(out, task="tuning", policy=NONE_POLICY)
            redacted = build_prompt_bundle(out, task="tuning", policy=policy)
            check("findings.json" in baseline.sources, "tuning without policy includes findings")
            check("findings.json" not in redacted.sources, "bank_redact drops findings")
            check("# DATA:" in redacted.prompt, "redacted bundle still has DATA")
            check("<ip>" in redacted.prompt, "bank_redact masks IPv4 in remaining sections")
            check("10.1.2.3" not in redacted.prompt, "raw IPv4 does not leak after bank_redact")
            check(redacted.metadata["policy"]["name"] == "bank_redact", "bundle records bank_redact")
            check(
                "findings.json" in redacted.metadata["policy"]["dropped_sections"],
                "dropped findings are listed in the policy report",
            )
            provider = DryRunProvider()
            response = provider.generate(redacted.to_request())
            check(bool(response.text.strip()), "dry-run still answers after policy is on")
            written = write_llm_artifacts(
                out, redacted, response=response, provider_info=provider.describe()
            )
            request = json.loads(
                next(path for path in written if path.name.startswith("llm_request")).read_text(
                    encoding="utf-8"
                )
            )
            check(
                request["policy"]["name"] == "bank_redact",
                "request artifact keeps the enabled policy name",
            )
    finally:
        if previous is None:
            os.environ.pop("PGPROFILE_LLM_POLICY", None)
        else:
            os.environ["PGPROFILE_LLM_POLICY"] = previous


def check_mask_rules() -> None:
    policy = parse_policy(
        "mask_demo",
        {
            "sanitization": True,
            "mask": [
                {"name": "ipv4", "pattern": r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "replacement": "<ip>"},
                {
                    "name": "secret",
                    "pattern": r"(?i)\b(password|token)\s*[=:]\s*\S+",
                    "replacement": r"\1=<redacted>",
                },
            ],
        },
    )
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        (out / "brief.md").write_text(
            "сервер 10.3.81.94 password=supersecret user=nt\n",
            encoding="utf-8",
        )
        bundle = build_prompt_bundle(
            out,
            task="summary",
            policy=policy,
            overrides={"extra_instructions": "token=abc123"},
        )
        check("<ip>" in bundle.prompt, "IPv4 addresses are masked")
        check("10.3.81.94" not in bundle.prompt, "raw IPv4 does not leak into the prompt")
        check("password=<redacted>" in bundle.prompt, "password values are masked")
        check("supersecret" not in bundle.prompt, "raw password does not leak")
        check("token=<redacted>" in bundle.prompt, "extra_instructions are masked too")
        check(bundle.metadata["policy"]["mask_hits"]["ipv4"] >= 1, "mask hit count for ipv4")
        check(bundle.metadata["policy"]["unchanged"] is False, "masking marks the report as changed")


def check_allow_empties_bundle() -> None:
    policy = parse_policy(
        "only_findings",
        {"sanitization": True, "allow_sections": ["findings"]},
    )
    try:
        build_prompt_bundle(CASE_DIR, task="summary", policy=policy)
        check(False, "allow_sections that drop every DATA section are rejected")
    except LLMBundleError as exc:
        check("removed every DATA section" in str(exc), "empty payload after policy is a bundle error")


def check_unknown_policy_and_bad_regex() -> None:
    previous = os.environ.get("PGPROFILE_LLM_POLICY")
    os.environ["PGPROFILE_LLM_POLICY"] = "does-not-exist"
    try:
        load_active_policy()
        check(False, "unknown policy name is rejected")
    except LLMPolicyError as exc:
        check("unknown policy" in str(exc), "unknown policy name is rejected")
    finally:
        if previous is None:
            os.environ.pop("PGPROFILE_LLM_POLICY", None)
        else:
            os.environ["PGPROFILE_LLM_POLICY"] = previous

    try:
        parse_policy("bad", {"sanitization": True, "mask": [{"name": "x", "pattern": "("}]})
        check(False, "invalid mask regex is rejected")
    except LLMPolicyError as exc:
        check("not a valid regex" in str(exc), "invalid mask regex is rejected")

    try:
        parse_policy("bad", {"sanitization": True, "deny_sections": ["sql_text"]})
        check(False, "unknown section name is rejected")
    except LLMPolicyError as exc:
        check("unknown section" in str(exc), "unknown section name is rejected")


def check_cli_list_policy() -> None:
    import contextlib
    import io

    import run_llm

    previous = os.environ.pop("PGPROFILE_LLM_POLICY", None)
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_llm.main(["--list-policy"])
        text = out.getvalue()
        check(code == 0, "--list-policy exits 0")
        check('"name": "none"' in text, "--list-policy prints none")
    finally:
        if previous is not None:
            os.environ["PGPROFILE_LLM_POLICY"] = previous


def main() -> int:
    if not CASE_DIR.is_dir():
        print(f"Missing analysis fixture: {CASE_DIR}")
        return 1
    check_default_none()
    check_identity_apply()
    check_disabled_profile_is_noop()
    check_bank_redact_from_yaml()
    check_mask_rules()
    check_allow_empties_bundle()
    check_unknown_policy_and_bad_regex()
    check_cli_list_policy()
    failed = [label for ok, label in results if not ok]
    for ok, label in results:
        print(f"{'PASS' if ok else 'FAIL'}: {label}")
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
