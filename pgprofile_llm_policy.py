"""Optional sanitization of LLM prompt bundles.

The default policy is `none`: the payload is unchanged. Named profiles in
`llm_policy.yaml` (or PGPROFILE_LLM_POLICY) can mask strings and drop sections
without touching the rest of the pipeline.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pgprofile_llm import LLMError


DEFAULT_POLICY_CONFIG = Path(__file__).resolve().parent / "llm_policy.yaml"
ENV_POLICY = "PGPROFILE_LLM_POLICY"
POLICY_NONE = "none"
KNOWN_SECTIONS = frozenset({"brief", "influence", "findings"})

Section = tuple[str, str, str, str]  # key, heading, source, body


class LLMPolicyError(LLMError):
    """Policy file is missing a named profile, or a mask pattern is not valid."""


@dataclass(frozen=True)
class MaskRule:
    name: str
    pattern: str
    replacement: str
    compiled: re.Pattern[str] = field(repr=False, compare=False)

    def apply(self, text: str) -> tuple[str, int]:
        rewritten, count = self.compiled.subn(self.replacement, text)
        return rewritten, count


@dataclass(frozen=True)
class LlmPolicy:
    name: str
    sanitization: bool = False
    allow_sections: tuple[str, ...] = ()
    deny_sections: tuple[str, ...] = ()
    mask: tuple[MaskRule, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.sanitization) and self.name != POLICY_NONE

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sanitization": self.sanitization,
            "enabled": self.enabled,
            "allow_sections": list(self.allow_sections),
            "deny_sections": list(self.deny_sections),
            "mask_rules": [rule.name for rule in self.mask],
        }


NONE_POLICY = LlmPolicy(name=POLICY_NONE, sanitization=False)


def _as_str_tuple(value: Any, *, field_name: str, policy_name: str) -> tuple[str, ...]:
    if value in (None, "", []):
        return ()
    if not isinstance(value, list):
        raise LLMPolicyError(
            f"policy {policy_name}: {field_name} must be a list of strings",
            provider="",
        )
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        if field_name.endswith("sections") and text not in KNOWN_SECTIONS:
            raise LLMPolicyError(
                f"policy {policy_name}: unknown section {text!r} in {field_name}; "
                f"expected one of {', '.join(sorted(KNOWN_SECTIONS))}",
            )
        items.append(text)
    return tuple(items)


def _parse_mask(raw: Any, *, policy_name: str) -> tuple[MaskRule, ...]:
    if raw in (None, "", []):
        return ()
    if not isinstance(raw, list):
        raise LLMPolicyError(f"policy {policy_name}: mask must be a list")
    rules: list[MaskRule] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise LLMPolicyError(f"policy {policy_name}: mask[{index}] must be a mapping")
        name = str(item.get("name") or f"rule_{index + 1}").strip()
        pattern = str(item.get("pattern") or "")
        replacement = str(item.get("replacement") if item.get("replacement") is not None else "<redacted>")
        if not pattern:
            raise LLMPolicyError(f"policy {policy_name}: mask {name!r} has no pattern")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise LLMPolicyError(
                f"policy {policy_name}: mask {name!r} is not a valid regex ({exc})"
            ) from exc
        rules.append(
            MaskRule(name=name, pattern=pattern, replacement=replacement, compiled=compiled)
        )
    return tuple(rules)


def parse_policy(name: str, settings: dict[str, Any] | None) -> LlmPolicy:
    data = dict(settings or {})
    return LlmPolicy(
        name=name,
        sanitization=bool(data.get("sanitization")),
        allow_sections=_as_str_tuple(
            data.get("allow_sections"), field_name="allow_sections", policy_name=name
        ),
        deny_sections=_as_str_tuple(
            data.get("deny_sections"), field_name="deny_sections", policy_name=name
        ),
        mask=_parse_mask(data.get("mask"), policy_name=name),
    )


def load_policy_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or DEFAULT_POLICY_CONFIG
    if not config_path.exists():
        if path is not None:
            raise LLMPolicyError(f"LLM policy config not found: {config_path}")
        return {"active": POLICY_NONE, "policies": {POLICY_NONE: {"sanitization": False}}}
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise LLMPolicyError(f"invalid LLM policy config: {config_path}")
    policies = config.get("policies")
    if not isinstance(policies, dict) or not policies:
        raise LLMPolicyError(f"LLM policy config has no policies: {config_path}")
    return config


def resolve_policy_name(config: dict[str, Any], requested: str | None = None) -> str:
    name = (requested or os.environ.get(ENV_POLICY) or config.get("active") or POLICY_NONE).strip()
    if not name:
        name = POLICY_NONE
    policies = config.get("policies") or {}
    if name not in policies:
        available = ", ".join(sorted(policies)) or "<none>"
        raise LLMPolicyError(f"unknown policy {name!r}; available: {available}")
    return name


def load_active_policy(
    path: Path | None = None, *, name: str | None = None
) -> LlmPolicy:
    config = load_policy_config(path)
    resolved = resolve_policy_name(config, name)
    return parse_policy(resolved, (config.get("policies") or {}).get(resolved))


def describe_policy(path: Path | None = None) -> dict[str, Any]:
    """Public snapshot for UI/CLI: never includes regex bodies."""
    try:
        policy = load_active_policy(path)
    except LLMPolicyError as exc:
        return {
            "name": POLICY_NONE,
            "sanitization": False,
            "enabled": False,
            "allow_sections": [],
            "deny_sections": [],
            "mask_rules": [],
            "error": str(exc),
        }
    return policy.describe()


def apply_policy(
    sections: list[Section],
    policy: LlmPolicy | None = None,
    *,
    extra_instructions: str = "",
) -> tuple[list[Section], str, dict[str, Any]]:
    """Filter and mask DATA sections. `none` / sanitization=false is a no-op."""
    active = policy or NONE_POLICY
    report: dict[str, Any] = {
        **active.describe(),
        "dropped_sections": [],
        "mask_hits": {rule.name: 0 for rule in active.mask},
        "unchanged": True,
    }
    if not active.enabled:
        return list(sections), extra_instructions, report

    kept: list[Section] = []
    dropped: list[str] = []
    allow = set(active.allow_sections)
    deny = set(active.deny_sections)
    for key, heading, source, body in sections:
        if allow and key not in allow:
            dropped.append(source)
            continue
        if key in deny:
            dropped.append(source)
            continue
        kept.append((key, heading, source, body))

    rewritten_extra = extra_instructions
    mask_hits = {rule.name: 0 for rule in active.mask}
    if active.mask:
        masked: list[Section] = []
        for key, heading, source, body in kept:
            text = body
            for rule in active.mask:
                text, count = rule.apply(text)
                mask_hits[rule.name] = mask_hits.get(rule.name, 0) + count
            masked.append((key, heading, source, text))
        kept = masked
        if rewritten_extra:
            for rule in active.mask:
                rewritten_extra, count = rule.apply(rewritten_extra)
                mask_hits[rule.name] = mask_hits.get(rule.name, 0) + count

    report["dropped_sections"] = dropped
    report["mask_hits"] = mask_hits
    report["unchanged"] = not dropped and not any(mask_hits.values()) and rewritten_extra == extra_instructions
    return kept, rewritten_extra, report
