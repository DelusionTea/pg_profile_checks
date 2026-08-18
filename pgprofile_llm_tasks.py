"""Assemble LLM prompt bundles from analysis artifacts.

The engineer picks a task preset; everything else is collected from the output directory,
so no analysis numbers are typed by hand. Manual input is limited to `ALLOWED_OVERRIDES`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pgprofile_llm import LLMError, LLMRequest, new_trace_id
from pgprofile_llm_policy import LlmPolicy, apply_policy, load_active_policy


class LLMBundleError(LLMError):
    """Bundle cannot be built or fails validation before anything is sent."""


ALLOWED_OVERRIDES = frozenset({"extra_instructions", "temperature", "max_tokens"})
MAX_EXTRA_INSTRUCTIONS_CHARS = 1000
DEFAULT_MAX_CHARS = 60000

_BRIEF_CANDIDATES = (
    "multi_symptom_brief.md",
    "nt_runs_brief.md",
    "symptom_brief.md",
    "stable_prod_brief.md",
    "nt_prod_brief.md",
    "brief.md",
)

_INFLUENCE_CANDIDATES = (
    "influence_summary_series.md",
    "influence_summary.md",
)

_ANSWER_FORMAT = (
    "Верни один JSON-объект (можно в блоке ```json), без текста вокруг. Схема:\n"
    "{\n"
    '  "verdict": "go" | "no-go" | "need-validation",\n'
    '  "summary": "краткий вывод",\n'
    '  "claims": [{"statement": "...", "subject": "параметр или метрика из DATA",'
    ' "evidence_type": "probable|proven|none"}],\n'
    '  "recommendations": [{"parameter": "...", "action": "...",'
    ' "subject": "метрика из DATA"}],\n'
    '  "risks": ["..."],\n'
    '  "missing_data": ["..."]\n'
    "}\n"
    "subject и parameter обязаны встречаться в DATA. "
    "Не ставь evidence_type=proven, если в DATA у параметра PROBABLE."
)

_SHARED_RULES = (
    "Используй только числа и факты из блоков DATA ниже; ничего не додумывай.",
    "Если данных для вывода не хватает, скажи об этом прямо вместо предположения.",
    "Связь параметра и метрики с меткой PROBABLE называй гипотезой, а не причиной.",
    "Отвечай по-русски.",
    _ANSWER_FORMAT,
)


@dataclass(frozen=True)
class TaskPreset:
    task: str
    title: str
    goal: str
    sections: tuple[str, ...]
    findings_limit: int


TASK_PRESETS: dict[str, TaskPreset] = {
    "summary": TaskPreset(
        task="summary",
        title="Краткий вывод по прогонам",
        goal=(
            "Дай короткий вывод для инженера НТ: подтвердилась ли гипотеза по изменённым "
            "настройкам, что стало лучше и что хуже, какие риски мешают принять результат. "
            "Не более 15 строк."
        ),
        sections=("brief", "influence"),
        findings_limit=0,
    ),
    "tuning": TaskPreset(
        task="tuning",
        title="Рекомендации по настройке",
        goal=(
            "Предложи изменения параметров PostgreSQL. Для каждого: параметр, текущее и "
            "целевое значение, обоснование числами из DATA, ожидаемый эффект, риск и как "
            "проверить результат на следующем прогоне. Сначала самые безопасные изменения."
        ),
        sections=("brief", "influence", "findings"),
        findings_limit=25,
    ),
    "detailed_rca": TaskPreset(
        task="detailed_rca",
        title="Детальный разбор причин",
        goal=(
            "Разбери причинно-следственную цепочку: от симптома к метрикам и к изменённым "
            "настройкам. Отдели подтверждённые связи от гипотез, укажи каких данных не "
            "хватает для доказательства и какой прогон это закроет."
        ),
        sections=("brief", "influence", "findings"),
        findings_limit=60,
    ),
}


@dataclass
class PromptBundle:
    task: str
    system: str
    prompt: str
    sources: list[str] = field(default_factory=list)
    trace_id: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def char_count(self) -> int:
        return len(self.system) + len(self.prompt)

    def to_request(self) -> LLMRequest:
        return LLMRequest(
            prompt=self.prompt,
            system=self.system,
            task=self.task,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            metadata={"trace_id": self.trace_id, **self.metadata},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "trace_id": self.trace_id,
            "sources": list(self.sources),
            "char_count": self.char_count(),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "metadata": dict(self.metadata),
            "system": self.system,
            "prompt": self.prompt,
        }


def list_tasks() -> list[dict[str, str]]:
    return [
        {"task": preset.task, "title": preset.title, "goal": preset.goal}
        for preset in TASK_PRESETS.values()
    ]


def _first_existing(output_dir: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = output_dir / name
        if candidate.is_file() and candidate.read_text(encoding="utf-8").strip():
            return candidate
    return None


def _findings_section(output_dir: Path, limit: int) -> tuple[str, str] | None:
    """Compact findings list: severity, id and message only, so the prompt stays small."""
    path = output_dir / "findings.json"
    if limit <= 0 or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LLMBundleError(f"findings.json is not readable: {exc}") from exc

    rank = {"critical": 0, "high": 1, "warning": 2, "medium": 3, "info": 4, "low": 5}
    findings = [item for item in (payload.get("findings") or []) if isinstance(item, dict)]
    findings.sort(key=lambda item: rank.get(str(item.get("severity") or "").lower(), 9))
    if not findings:
        return None

    lines = []
    for item in findings[:limit]:
        severity = str(item.get("severity") or "info").upper()
        lines.append(
            f"- [{severity}] {item.get('id') or '?'}: {str(item.get('message') or '').strip()}"
        )
    hidden = len(findings) - len(lines)
    if hidden > 0:
        lines.append(f"- ... ещё {hidden} находок опущено")
    return "findings.json", "\n".join(lines)


def _collect_sections(output_dir: Path, preset: TaskPreset) -> list[tuple[str, str, str, str]]:
    """Return (section_key, heading, source_name, body) in prompt order."""
    collected: list[tuple[str, str, str, str]] = []
    for section in preset.sections:
        if section == "brief":
            path = _first_existing(output_dir, _BRIEF_CANDIDATES)
            if path is None:
                raise LLMBundleError(
                    f"no brief artifact in {output_dir}: expected one of "
                    f"{', '.join(_BRIEF_CANDIDATES)}"
                )
            collected.append(
                ("brief", "Сводка анализа", path.name, path.read_text(encoding="utf-8").strip())
            )
        elif section == "influence":
            path = _first_existing(output_dir, _INFLUENCE_CANDIDATES)
            if path is not None:
                collected.append(
                    (
                        "influence",
                        "Влияние настроек на метрики",
                        path.name,
                        path.read_text(encoding="utf-8").strip(),
                    )
                )
        elif section == "findings":
            found = _findings_section(output_dir, preset.findings_limit)
            if found is not None:
                collected.append(("findings", "Находки анализа", found[0], found[1]))
    return collected


def _check_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(overrides) - ALLOWED_OVERRIDES)
    if unknown:
        raise LLMBundleError(
            f"unsupported overrides: {', '.join(unknown)};"
            f" allowed: {', '.join(sorted(ALLOWED_OVERRIDES))}"
        )
    clean: dict[str, Any] = {}

    extra = str(overrides.get("extra_instructions") or "").strip()
    if extra:
        if len(extra) > MAX_EXTRA_INSTRUCTIONS_CHARS:
            raise LLMBundleError(
                f"extra_instructions is too long: {len(extra)} chars,"
                f" limit {MAX_EXTRA_INSTRUCTIONS_CHARS}"
            )
        clean["extra_instructions"] = extra

    for key in ("temperature", "max_tokens"):
        if overrides.get(key) is None:
            continue
        try:
            value = float(overrides[key]) if key == "temperature" else int(overrides[key])
        except (TypeError, ValueError) as exc:
            raise LLMBundleError(f"{key} must be numeric") from exc
        if key == "temperature" and not (0.0 <= value <= 1.0):
            raise LLMBundleError("temperature must be within [0, 1]")
        if key == "max_tokens" and not (256 <= value <= 32768):
            raise LLMBundleError("max_tokens must be within [256, 32768]")
        clean[key] = value

    return clean


def build_prompt_bundle(
    output_dir: Path,
    *,
    task: str = "summary",
    overrides: dict[str, Any] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    policy: LlmPolicy | None = None,
) -> PromptBundle:
    """Collect artifacts from `output_dir` into a ready-to-send bundle for one task."""
    preset = TASK_PRESETS.get(task)
    if preset is None:
        raise LLMBundleError(
            f"unknown task {task!r}; available: {', '.join(sorted(TASK_PRESETS))}"
        )
    if not output_dir.is_dir():
        raise LLMBundleError(f"analysis output directory not found: {output_dir}")

    clean_overrides = _check_overrides(dict(overrides or {}))
    sections = _collect_sections(output_dir, preset)
    extra = str(clean_overrides.get("extra_instructions") or "")
    active_policy = policy if policy is not None else load_active_policy()
    sections, extra, policy_report = apply_policy(
        sections, active_policy, extra_instructions=extra
    )
    if extra:
        clean_overrides["extra_instructions"] = extra
    elif "extra_instructions" in clean_overrides:
        clean_overrides.pop("extra_instructions")
    if not sections:
        raise LLMBundleError(
            f"policy {policy_report.get('name')!r} removed every DATA section; "
            "relax allow_sections/deny_sections"
        )
    sections, trimmed = _fit_sections(sections, preset=preset, max_chars=max_chars)

    system_lines = [
        "Ты инженер по производительности PostgreSQL и помогаешь инженеру нагрузочного тестирования.",
        *_SHARED_RULES,
    ]
    prompt_lines = [f"# ЗАДАЧА: {preset.title}", "", preset.goal]
    extra = clean_overrides.get("extra_instructions")
    if extra:
        prompt_lines += ["", "Дополнительное требование от инженера:", extra]
    for _key, heading, source, body in sections:
        prompt_lines += ["", f"# DATA: {heading} (источник: {source})", "", body]

    bundle = PromptBundle(
        task=preset.task,
        system="\n".join(system_lines),
        prompt="\n".join(prompt_lines).strip() + "\n",
        sources=[source for _key, _heading, source, _body in sections],
        trace_id=new_trace_id(),
        temperature=clean_overrides.get("temperature"),
        max_tokens=clean_overrides.get("max_tokens"),
        metadata={
            "output_dir": str(output_dir),
            "task_title": preset.title,
            "trimmed_sections": trimmed,
            "max_chars": max_chars,
            "policy": policy_report,
        },
    )
    validate_bundle(bundle, max_chars=max_chars)
    return bundle


def _fit_sections(
    sections: list[tuple[str, str, str, str]],
    *,
    preset: TaskPreset,
    max_chars: int,
) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """Trim the least important sections first; the brief is never dropped entirely."""
    budget = max(1000, max_chars - 2000)  # instructions and headings also cost characters
    total = sum(len(body) for _key, _heading, _source, body in sections)
    if total <= budget:
        return sections, []

    trimmed: list[str] = []
    result = list(sections)
    # Last section is the least important one for every preset.
    for index in range(len(result) - 1, -1, -1):
        if total <= budget:
            break
        key, heading, source, body = result[index]
        overflow = total - budget
        keep = max(500, len(body) - overflow)
        if keep >= len(body):
            continue
        result[index] = (
            key,
            heading,
            source,
            body[:keep].rstrip() + "\n... (блок сокращён, полные данные в артефактах)",
        )
        total -= len(body) - keep
        trimmed.append(source)
    return result, trimmed


def validate_bundle(bundle: PromptBundle, *, max_chars: int = DEFAULT_MAX_CHARS) -> None:
    """Last gate before the payload leaves the process."""
    if bundle.task not in TASK_PRESETS:
        raise LLMBundleError(f"unknown task {bundle.task!r}")
    if not bundle.system.strip():
        raise LLMBundleError("bundle has no system instructions")
    if not bundle.prompt.strip():
        raise LLMBundleError("bundle has no prompt")
    if not bundle.sources:
        raise LLMBundleError("bundle references no analysis artifacts")
    if "# DATA:" not in bundle.prompt:
        raise LLMBundleError("bundle has no DATA section with analysis facts")
    if bundle.char_count() > max_chars:
        raise LLMBundleError(
            f"bundle is too large: {bundle.char_count()} chars, limit {max_chars}"
        )
    if not bundle.trace_id:
        raise LLMBundleError("bundle has no trace_id")


def write_llm_artifacts(
    output_dir: Path,
    bundle: PromptBundle,
    *,
    response: Any = None,
    error: LLMError | None = None,
    provider_info: dict[str, Any] | None = None,
) -> list[Path]:
    """Persist request, outcome and answer text so every run is auditable by trace_id."""
    written: list[Path] = []

    request_path = output_dir / f"llm_request_{bundle.task}.json"
    request_payload = {
        "type": "llm_request",
        "provider": dict(provider_info or {}),
        "policy": dict((bundle.metadata or {}).get("policy") or {}),
        **bundle.to_dict(),
    }
    request_path.write_text(
        json.dumps(request_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    written.append(request_path)

    response_path = output_dir / f"llm_response_{bundle.task}.json"
    if error is not None:
        outcome: dict[str, Any] = {"status": "failed", **error.as_dict()}
    elif response is not None:
        outcome = {"status": "success", **response.to_dict()}
    else:
        outcome = {"status": "skipped", "trace_id": bundle.trace_id}
    outcome.setdefault("trace_id", bundle.trace_id)
    outcome["task"] = bundle.task
    response_path.write_text(
        json.dumps(outcome, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    written.append(response_path)

    if error is None and response is not None:
        answer_path = output_dir / f"llm_{bundle.task}.md"
        answer_path.write_text(str(response.text).rstrip() + "\n", encoding="utf-8")
        written.append(answer_path)

    return written
