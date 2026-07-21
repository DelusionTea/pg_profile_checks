from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from jvmcheck.models import AnalysisResult, ContainerResources, PodResourcesBudget, RuntimeContext, RuntimeMetrics


def build_ai_analysis_bundle(
    analysis: AnalysisResult,
    container: ContainerResources,
    budget: PodResourcesBudget,
    runtime_metrics: RuntimeMetrics,
    runtime_context: RuntimeContext,
    system_name: str | None,
    resources_file: Path,
    jvm_config_file: Path | None,
    output_root: Path,
    model_label: str,
) -> Path:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _safe_slug(system_name or container.name or "system")
    out_dir = output_root / f"{run_id}_{slug}"
    out_dir.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "system_name": system_name,
        "model_label": model_label,
        "source_files": {
            "resources_file": str(resources_file),
            "jvm_config_file": str(jvm_config_file) if jvm_config_file else None,
        },
        "target_container": asdict(container),
        "pod_budget": asdict(budget),
        "runtime_metrics": asdict(runtime_metrics),
        "runtime_context": asdict(runtime_context),
        "analysis_result": asdict(analysis),
    }
    (out_dir / "input_snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    (out_dir / "approved_sources.md").write_text(_approved_sources_markdown(), encoding="utf-8")
    (out_dir / "gates.md").write_text(_gates_markdown(), encoding="utf-8")
    (out_dir / "response_schema.json").write_text(_response_schema_json(), encoding="utf-8")
    (out_dir / "ai_task_prompt.md").write_text(
        _task_prompt_markdown(model_label=model_label),
        encoding="utf-8",
    )
    (out_dir / "README.md").write_text(_bundle_readme_markdown(), encoding="utf-8")
    return out_dir


def _safe_slug(raw: str) -> str:
    keep = []
    for char in raw:
        if char.isalnum() or char in ("-", "_"):
            keep.append(char)
        else:
            keep.append("_")
    return "".join(keep).strip("_") or "system"


def _approved_sources_markdown() -> str:
    return """# Approved Java Community Sources

Use only these sources for JVM recommendations:

1. OpenJDK JEP index: https://openjdk.org/jeps/0
2. OpenJDK docs and mailing list archives: https://openjdk.org/
3. Oracle JVM options reference:
   - https://docs.oracle.com/en/java/
4. Eclipse Adoptium docs: https://adoptium.net/
5. Spring Boot reference docs: https://docs.spring.io/spring-boot/docs/current/reference/html/
6. CNCF Kubernetes docs (container resources): https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/

Any recommendation without citation to this list must be marked INVALID.
"""


def _gates_markdown() -> str:
    return """# Mandatory Gates For AI Output

The model must pass all gates. If a gate cannot be satisfied, return `UNKNOWN` and explain why.

## Gate 1: Source Allowlist
- Every recommendation must cite at least one approved source from `approved_sources.md`.
- Do not cite blogs, forums, or memory-only facts.

## Gate 2: Local Evidence Link
- Every recommendation must point to concrete evidence from `input_snapshot.json`:
  - metric name(s),
  - observed value(s),
  - threshold or target comparison.

## Gate 3: Version Compatibility
- Every JVM flag must include JDK applicability: `{8|11|17|21+}`.
- If JDK version is missing, label recommendation as `conditional`.

## Gate 4: No Fabrication
- Do not invent metrics, values, file paths, or sources.
- Missing data must be explicitly listed under `data_gaps`.

## Gate 5: Conflict Check
- Detect conflicting flags (duplicate or contradictory behavior).
- If conflict exists, recommend only one conflict-free variant.

## Gate 6: Risk + Rollback
- Every recommended change must include:
  - risk impact,
  - monitoring checkpoint,
  - rollback trigger.

## Gate 7: Escalation Trigger
- If expected improvement cannot be validated with existing metrics, set escalation to `required`.
- Escalation target: joint memory dump analysis with development team.

## Gate 8: Confidence Discipline
- Use `high` only if JDK version and key metrics are present and consistent.
- Otherwise use `medium` or `low` with a reason.
"""


def _response_schema_json() -> str:
    schema = {
        "type": "object",
        "required": [
            "summary",
            "recommendations",
            "data_gaps",
            "escalation",
            "gate_checklist",
        ],
        "properties": {
            "summary": {"type": "string"},
            "recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "title",
                        "change",
                        "evidence",
                        "jdk_applicability",
                        "citations",
                        "risks",
                        "monitoring",
                        "rollback_trigger",
                        "confidence",
                    ],
                    "properties": {
                        "title": {"type": "string"},
                        "change": {"type": "string"},
                        "evidence": {"type": "string"},
                        "jdk_applicability": {"type": "string"},
                        "citations": {"type": "array", "items": {"type": "string"}},
                        "risks": {"type": "array", "items": {"type": "string"}},
                        "monitoring": {"type": "array", "items": {"type": "string"}},
                        "rollback_trigger": {"type": "string"},
                        "confidence": {"type": "string"},
                    },
                },
            },
            "data_gaps": {"type": "array", "items": {"type": "string"}},
            "escalation": {
                "type": "object",
                "required": ["required", "reason"],
                "properties": {
                    "required": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
            },
            "gate_checklist": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["gate", "status", "comment"],
                    "properties": {
                        "gate": {"type": "string"},
                        "status": {"type": "string"},
                        "comment": {"type": "string"},
                    },
                },
            },
        },
    }
    return json.dumps(schema, ensure_ascii=False, indent=2)


def _task_prompt_markdown(model_label: str) -> str:
    return f"""# AI Task Prompt

Target model: {model_label}

You are analyzing JVM tuning results.
Inputs:
- `input_snapshot.json`
- `approved_sources.md`
- `gates.md`
- `response_schema.json`

Instructions:
1. Read `gates.md` first.
2. Use only approved sources.
3. If information is missing, return `UNKNOWN` in the related field.
4. Produce output strictly matching `response_schema.json`.
5. Do not add any recommendation that is not supported by both:
   - local evidence from `input_snapshot.json`,
   - at least one approved source.

Return JSON only.
"""


def _bundle_readme_markdown() -> str:
    return """# AI Analysis Bundle

This directory is prepared for a constrained/weak LLM follow-up analysis.

Suggested workflow:
1. Load `ai_task_prompt.md` as system/task prompt.
2. Attach `input_snapshot.json`, `approved_sources.md`, `gates.md`, `response_schema.json`.
3. Request JSON-only output.
4. Reject output if any gate is failed or uncited recommendations appear.
"""

