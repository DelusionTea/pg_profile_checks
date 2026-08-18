"""JVM analysis adapter for the UI — separate from the PG pipeline."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ui.models import AnalyzeResult, JvmAnalyzeRequest, JvmTreeAnswers

def _detect_jvmcheck_root() -> Path:
    env = os.environ.get("JVMCHECK_ROOT")
    candidates = []
    if env:
        candidates.append(Path(env))
    # Preferred self-contained runtime bundled with this repository.
    candidates.append(Path(__file__).resolve().parent.parent / "jvmcheck_runtime")
    candidates.extend(
        [
            Path.home() / "jvmcheck",
            Path(__file__).resolve().parents[3] / "jvmcheck",
            Path(__file__).resolve().parents[2] / "jvmcheck",
        ]
    )
    for candidate in candidates:
        if (candidate / "src" / "jvmcheck").is_dir() and (candidate / "resources").is_dir():
            return candidate
    return Path.home() / "jvmcheck"


DEFAULT_JVMCHECK_ROOT = _detect_jvmcheck_root()
DEMO_JVM_ROOT = Path(__file__).resolve().parent.parent / "resources" / "jvm_demo"

JVM_PROBLEM_CATALOG: list[dict[str, str]] = [
    {
        "id": "gc_latency",
        "title": "Долгие GC паузы",
        "description": "Проблемы p95/p99 и GC time ratio.",
    },
    {
        "id": "heap_pressure",
        "title": "Heap / OldGen pressure",
        "description": "Высокая утилизация OldGen, риск деградации или OOM.",
    },
    {
        "id": "memory_pressure",
        "title": "Контейнерная память под давлением",
        "description": "Working set близко к limit/request, риск рестартов.",
    },
    {
        "id": "jvm_flags",
        "title": "Некорректные JVM флаги",
        "description": "Конфликты/дубликаты/критичные отсутствующие флаги.",
    },
    {
        "id": "container_config",
        "title": "Проблемы конфигурации контейнера",
        "description": "Плохая связка requests/limits и runtime policy.",
    },
]

JVM_PROBLEM_RULE_PREFIXES: dict[str, tuple[str, ...]] = {
    "gc_latency": ("gc.",),
    "heap_pressure": ("heap.", "oldgen.", "newgen."),
    "memory_pressure": ("memory.",),
    "jvm_flags": ("jvm.",),
    "container_config": ("container.",),
}

JVM_PROBLEM_REQUIRED_INPUTS: dict[str, tuple[str, ...]] = {
    "gc_latency": ("gc_pause_p95_ms",),
    "memory_pressure": ("container_memory_usage_percent",),
    "heap_pressure": ("heap_used_mib", "heap_used_percent", "old_gen_used_percent"),
}

JVM_ALWAYS_REQUIRED_INPUTS: tuple[str, ...] = (
    "gc_pause_p95_ms",
    "heap_used_mib",
    "container_memory_usage_percent",
)

JVM_PROBLEM_SEED_FINDINGS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "gc_latency": (
        ("gc.long_pause_p95", "warning", "Выбрана проблема долгих GC пауз, требуется tuning tail latency."),
    ),
    "heap_pressure": (
        ("heap.old_gen_pressure", "warning", "Выбрано давление по heap/old gen, требуется стабилизация occupancy."),
    ),
    "memory_pressure": (
        ("memory.limit_pressure", "critical", "Выбрано memory pressure в контейнере, требуется снижение риска OOM."),
    ),
    "jvm_flags": (
        ("jvm.flag_conflict_maxram", "warning", "Выбрана проблема конфигурации JVM флагов, требуется нормализация options."),
    ),
    "container_config": (
        ("container.request_limit_skew", "warning", "Выбрана проблема container resources, требуется корректировка request/limit."),
    ),
}

JVM_PROBLEM_REQUIRED_METRICS: dict[str, tuple[tuple[str, str], ...]] = {
    "gc_latency": (
        ("gc_pause_p95_ms", "GC pause p95"),
        ("gc_pause_p99_ms", "GC pause p99"),
        ("gc_time_ratio_percent", "GC time ratio"),
    ),
    "heap_pressure": (
        ("heap_used_mib", "Heap used"),
        ("heap_committed_mib", "Heap committed"),
        ("old_gen_used_mib", "OldGen used"),
        ("old_gen_capacity_mib", "OldGen capacity"),
    ),
    "memory_pressure": (
        ("container_memory_usage_percent", "Container memory usage (%)"),
    ),
}

JVM_PROBLEM_STRATEGIES: dict[str, dict[str, str]] = {
    "gc_latency": {
        "safe": "Зафиксировать G1 и умеренно снизить MaxGCPauseMillis без изменения общего heap budget.",
        "balanced": "Снизить pause target и скорректировать InitiatingHeapOccupancyPercent c проверкой p95/p99.",
        "aggressive": "Агрессивно оптимизировать GC-параметры только после стабилизации heap pressure.",
    },
    "heap_pressure": {
        "safe": "Сначала проверить retention/old-gen тренд и не уменьшать MaxRAMPercentage при высоком heap usage.",
        "balanced": "Тонко скорректировать G1ReservePercent + InitiatingHeapOccupancyPercent, удерживая headroom.",
        "aggressive": "Перестраивать memory budget контейнера и JVM вместе с планом отката.",
    },
    "memory_pressure": {
        "safe": "Снизить JVM memory footprint и проверить лимиты/requests без резких изменений.",
        "balanced": "Комбинировать JVM tuning с quota-aware перераспределением container memory.",
        "aggressive": "Эскалировать на платформу для увеличения budget при стабильно высоком working set.",
    },
}

DEMO_JVM_SYSTEMS: dict[str, dict[str, Any]] = {
    "DEMO_CounterAgent": {
        "containers": ["application", "istioProxy", "vaultAgent"],
        "resources_file": "resources.yaml",
        "jvm_file": "jvm-config.txt",
    },
    "DEMO_CreditHistory": {
        "containers": ["application", "agent", "fluentbit"],
        "resources_file": "resources.yaml",
        "jvm_file": "jvm-config.txt",
    },
}



def list_jvm_systems(root: Path | None = None) -> list[str]:
    jroot = root or DEFAULT_JVMCHECK_ROOT
    resources = jroot / "resources"
    real: list[str] = []
    if resources.is_dir():
        dirs = sorted(p.name for p in resources.iterdir() if p.is_dir() and not p.name.startswith("."))
        if dirs:
            real = dirs
        elif any(p.is_file() for p in resources.iterdir()):
            real = ["__root__"]
    demo = sorted(DEMO_JVM_SYSTEMS.keys())
    return sorted(set(real + demo))


JVM_NEW_SYSTEM = "__new__"
_RESERVED_JVM_SYSTEM_NAMES = frozenset({"__new__", "__root__", "", ".", ".."})


def normalize_jvm_system_name(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        raise ValueError("укажите имя новой АС")
    if raw in _RESERVED_JVM_SYSTEM_NAMES or raw in DEMO_JVM_SYSTEMS:
        raise ValueError(f"имя «{raw}» зарезервировано")
    if "/" in raw or "\\" in raw or "\x00" in raw:
        raise ValueError("в имени АС нельзя путь")
    if raw.startswith("."):
        raise ValueError("имя АС не должно начинаться с точки")
    return raw


def create_jvm_system(
    system_name: str,
    upload_paths: list[Path],
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    name = normalize_jvm_system_name(system_name)
    jroot = root or DEFAULT_JVMCHECK_ROOT
    system_dir = jroot / "resources" / name
    if not upload_paths:
        raise ValueError("для новой АС нужен хотя бы resources.yaml")
    roles = {_classify_jvm_upload(path.name) for path in upload_paths}
    if "resources" not in roles:
        raise ValueError(
            "нужен файл resources.yaml (в имени должно быть resource или values)"
        )
    created = not system_dir.exists()
    system_dir.mkdir(parents=True, exist_ok=True)
    _apply_jvm_uploads(system_dir, upload_paths)
    return {
        "system": name,
        "created": created,
        "containers": list_jvm_containers(name, root=jroot),
    }


def list_jvm_problems() -> list[dict[str, str]]:
    return []


def _ensure_jvmcheck_src(root: Path | None = None) -> Path:
    jroot = root or DEFAULT_JVMCHECK_ROOT
    src_dir = jroot / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    return src_dir


def list_jvm_playbook() -> dict[str, Any]:
    _ensure_jvmcheck_src()
    from jvmcheck.diagnostic_tree import load_playbook

    return load_playbook()


def list_jvm_containers(system_name: str, root: Path | None = None) -> list[dict[str, str]]:
    if not system_name:
        return []
    if system_name in DEMO_JVM_SYSTEMS:
        return [
            {
                "pod_name": "",
                "container_name": name,
                "display_name": name,
            }
            for name in DEMO_JVM_SYSTEMS[system_name]["containers"]
        ]

    jroot = root or DEFAULT_JVMCHECK_ROOT
    resources_root = jroot / "resources"
    try:
        if system_name == "__root__":
            resources_file, _ = _resolve_root_jvm_input_files(resources_root)
        else:
            system_dir = resources_root / system_name
            resources_file = system_dir / "resources.yaml"
            if not resources_file.is_file():
                resources_file = system_dir / "resources.yml"
            if not resources_file.is_file():
                resources_file, _ = _resolve_root_jvm_input_files(system_dir)
        import sys

        src_dir = jroot / "src"
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
        from jvmcheck.input_resolver import resolve_system_input_files
        from jvmcheck.parsers.custom_config_parser import parse_jvm_options_file
        from jvmcheck.parsers.k8s_yaml_parser import parse_k8s_or_stand_yaml

        budget = parse_k8s_or_stand_yaml(
            resources_file.read_text(encoding="utf-8"),
            source_path=resources_file,
        )
        jvm_cfg_file: Path | None = None
        try:
            _, jvm_cfg_file = _resolve_jvm_input_files_for_system(
                system_name,
                resources_root,
                resolve_system_input_files=resolve_system_input_files,
            )
        except Exception:
            jvm_cfg_file = None
        custom_options = parse_jvm_options_file(jvm_cfg_file) if jvm_cfg_file else {}
        targets: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for container in budget.containers or []:
            name = str(container.name or "").strip()
            if not name:
                continue
            pod = str(container.pod_name or "").strip()
            key = (pod, name)
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                {
                    "pod_name": pod,
                    "container_name": name,
                    "display_name": f"{pod} / {name}" if pod else name,
                    "java_tool_options_count": str(len(custom_options.get(name) or [])),
                    "java_tool_options_preview": " ".join((custom_options.get(name) or [])[:8]),
                }
            )
        targets.sort(key=lambda item: (item["display_name"].lower(), item["container_name"].lower()))
        return targets
    except Exception:
        return []


def load_jvm_last_input(
    system_name: str,
    container_name: str,
    pod_name: str | None = None,
    *,
    root: Path | None = None,
) -> dict[str, Any] | None:
    if not system_name or not container_name:
        return None
    jroot = root or DEFAULT_JVMCHECK_ROOT
    resources_root = jroot / "resources"
    system_dir = _resolve_jvm_system_dir(system_name, resources_root)
    path = system_dir / "last_input.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    containers = payload.get("containers") if isinstance(payload, dict) else None
    if not isinstance(containers, dict):
        return None
    composite_key = _last_input_key(container_name, pod_name)
    entry = containers.get(composite_key) or containers.get(container_name)
    return entry if isinstance(entry, dict) else None


def save_jvm_last_input(
    req: JvmAnalyzeRequest,
    *,
    root: Path | None = None,
) -> None:
    if not req.system_name or not req.container_name:
        return
    jroot = root or DEFAULT_JVMCHECK_ROOT
    resources_root = jroot / "resources"
    system_dir = _resolve_jvm_system_dir(req.system_name, resources_root)
    system_dir.mkdir(parents=True, exist_ok=True)
    path = system_dir / "last_input.json"
    payload: dict[str, Any] = {"containers": {}}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("containers"), dict):
                payload = existing
        except Exception:
            payload = {"containers": {}}
    payload.setdefault("containers", {})
    payload["containers"][_last_input_key(req.container_name, req.pod_name)] = {
        "gc_pause_p95_ms": req.gc_pause_p95_ms,
        "heap_used_mib": req.heap_used_mib,
        "heap_used_percent": req.heap_used_percent,
        "old_gen_used_mib": req.old_gen_used_mib,
        "old_gen_capacity_mib": req.old_gen_capacity_mib,
        "old_gen_used_percent": req.old_gen_used_percent,
        "container_memory_usage_percent": req.container_memory_usage_percent,
        "tree": dataclasses.asdict(req.tree),
        "updated_at": _utc_now_iso(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_jvm_analysis(
    req: JvmAnalyzeRequest,
    upload_paths: list[Path],
    output_dir: Path,
    *,
    jvmcheck_root: Path | None = None,
) -> AnalyzeResult:
    root = jvmcheck_root or DEFAULT_JVMCHECK_ROOT
    src_dir = root / "src"
    if not src_dir.is_dir():
        return AnalyzeResult(
            exit_code=2,
            error=f"jvmcheck src not found: {src_dir}",
            output_dir=output_dir,
            wiki_path=None,
            prompt_path=None,
            brief_path=None,
            summary={},
        )

    import sys

    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    try:
        from jvmcheck.analyzers.jvm_health_analyzer import analyze_jvm_health
        from jvmcheck.cli import (
            _build_analysis_budget,
            _build_tuning_target_snapshot,
            _choose_target_container,
        )
        from jvmcheck.diagnostic_tree import (
            apply_tree_gates,
            evaluate_jvm_diagnostic_tree,
            format_tree_wiki,
            propose_memory_heap_resize,
        )
        from jvmcheck.formatters.confluence_formatter import format_analysis_for_confluence
        from jvmcheck.input_resolver import resolve_system_input_files
        from jvmcheck.models import Finding, RuntimeContext, RuntimeMetrics
        from jvmcheck.parsers.custom_config_parser import parse_jvm_options_file
        from jvmcheck.parsers.k8s_yaml_parser import parse_k8s_or_stand_yaml
        from jvmcheck.recommenders.java_tool_options_recommender import enrich_with_recommendations
        from jvmcheck.thresholds import load_thresholds as load_jvm_thresholds
    except Exception as exc:  # noqa: BLE001
        return AnalyzeResult(
            exit_code=2,
            error=f"failed to import jvmcheck modules: {exc}",
            output_dir=output_dir,
            wiki_path=None,
            prompt_path=None,
            brief_path=None,
            summary={},
        )

    resources_root = root / "resources"
    tree_answers = _tree_answers_from_request(req)
    tree_eval = evaluate_jvm_diagnostic_tree(tree_answers)
    system_dir = _resolve_jvm_system_dir(req.system_name, resources_root)
    if not system_dir.exists():
        return AnalyzeResult(
            exit_code=2,
            error=f"АС не найдена: {req.system_name} ({system_dir})",
            output_dir=output_dir,
            wiki_path=None,
            prompt_path=None,
            brief_path=None,
            summary={},
        )

    try:
        _apply_jvm_uploads(system_dir, upload_paths)
        resources_file, jvm_cfg_file = _resolve_jvm_input_files_for_system(
            req.system_name,
            resources_root,
            resolve_system_input_files=resolve_system_input_files,
        )
        budget = parse_k8s_or_stand_yaml(
            resources_file.read_text(encoding="utf-8"),
            source_path=resources_file,
        )
        if not budget.containers:
            raise ValueError("No containers with resources found in input file.")
        custom_options = parse_jvm_options_file(jvm_cfg_file) if jvm_cfg_file else {}
        container = _choose_target_container(
            budget,
            requested_container=req.container_name,
            requested_pod=req.pod_name,
        )
        analysis_budget = _build_analysis_budget(budget, container.pod_name)
        if container.name in custom_options:
            container.java_tool_options = custom_options[container.name]

        _enrich_runtime_metrics_from_context(req, container)
        metrics = RuntimeMetrics(
            heap_used_mib=req.heap_used_mib if tree_eval.heap_branch_open else None,
            heap_committed_mib=req.heap_committed_mib if tree_eval.heap_branch_open else None,
            old_gen_used_mib=req.old_gen_used_mib if tree_eval.heap_branch_open else None,
            old_gen_capacity_mib=req.old_gen_capacity_mib if tree_eval.heap_branch_open else None,
            gc_pause_p95_ms=req.gc_pause_p95_ms,
            gc_pause_p99_ms=None,
            gc_time_ratio_percent=None,
            container_memory_working_set_mib=req.container_memory_working_set_mib,
        )
        context = RuntimeContext(
            jdk_version=req.jdk_version,
            spring_boot_version=req.spring_boot_version,
            framework_hints={},
        )
        thresholds = load_jvm_thresholds(req.threshold_profile)
        analysis = analyze_jvm_health(
            container=container,
            metrics=metrics,
            threshold_set=thresholds,
        )
        if tree_eval.heap_branch_open:
            _add_contextual_signal_findings(analysis, req, finding_cls=Finding)
        analysis = enrich_with_recommendations(
            container=container,
            budget=analysis_budget,
            analysis=analysis,
            runtime_context=context,
        )
        analysis = apply_tree_gates(analysis, tree_eval)
        copyable = bool(tree_eval.pause_copyable_allowed)
        resize = propose_memory_heap_resize(
            limit_mib=container.limits.memory_mib,
            request_mib=container.requests.memory_mib,
            java_tool_options=container.java_tool_options,
            need_resize=bool(
                (tree_eval.non_heap_accumulation and not tree_eval.sla_not_critical)
                or (
                    tree_eval.hours_to_oom is not None
                    and tree_eval.hours_to_oom <= 24
                )
            ),
        )
        input_audit = _audit_selected_problem_inputs(req)
        guardrails = _apply_contextual_jvm_guardrails(
            analysis=analysis,
            selected_problems=req.selected_problems,
            container=container,
            req=req,
        )
        _annotate_recommendation_diffs(analysis, container)
        output_dir.mkdir(parents=True, exist_ok=True)
        analysis_dict = dataclasses.asdict(analysis)
        analysis_dict["tuning_target_snapshot"] = _build_tuning_target_snapshot(container)
        analysis_dict["tree"] = {
            "copyable_allowed": copyable,
            "pause_copyable_allowed": tree_eval.pause_copyable_allowed,
            "ha_exceeded": tree_eval.ha_exceeded,
            "block_reasons": tree_eval.block_reasons,
        }
        (output_dir / "jvm_analysis.json").write_text(
            json.dumps(analysis_dict, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        base_wiki_text = format_analysis_for_confluence(
            analysis=analysis,
            container=container,
            runtime_metrics=metrics,
            runtime_context=context,
            system_name=req.system_name,
            resize=resize,
            copyable=copyable,
        )
        base_wiki_text = _localize_jvm_wiki_text(base_wiki_text)
        wiki_text = format_tree_wiki(
            tree_eval,
            tree_answers,
            analysis=analysis,
            java_tool_options=container.java_tool_options,
            resize=resize,
        )
        wiki_text += "\n" + _build_jvm_problem_statement_block(req, container)
        wiki_text += "\n\n" + base_wiki_text
        wiki_text += "\n\n" + _build_jvm_targeted_context_section(
            req=req,
            container_name=container.name,
            input_audit=input_audit,
            guardrails=guardrails,
            analysis=analysis_dict,
            current_java_options=container.java_tool_options or [],
            copyable=copyable,
        )
        wiki_path = output_dir / "jvm_confluence.wiki"
        wiki_path.write_text(wiki_text, encoding="utf-8")
        brief = _build_jvm_brief(
            req,
            container.name,
            analysis_dict,
            input_audit=input_audit,
            guardrails=guardrails,
        )
        brief_path = output_dir / "jvm_brief.md"
        brief_path.write_text(brief, encoding="utf-8")
        prompt_path = output_dir / "jvm_prompt.txt"
        prompt_path.write_text(_build_jvm_prompt(brief), encoding="utf-8")
        summary = _build_jvm_summary(req, analysis_dict)
        save_jvm_last_input(req, root=root)
        return AnalyzeResult(
            exit_code=0,
            error=None,
            output_dir=output_dir,
            wiki_path=wiki_path,
            prompt_path=prompt_path,
            brief_path=brief_path,
            summary=summary,
        )
    except Exception as exc:  # noqa: BLE001
        return AnalyzeResult(
            exit_code=2,
            error=str(exc),
            output_dir=output_dir,
            wiki_path=None,
            prompt_path=None,
            brief_path=None,
            summary={},
        )


def _apply_jvm_uploads(system_dir: Path, uploads: list[Path]) -> None:
    if not uploads:
        return
    for src in uploads:
        role = _classify_jvm_upload(src.name)
        if role == "resources":
            target = system_dir / "resources.yaml"
        elif role == "jvm":
            target = system_dir / f"jvm-config{src.suffix.lower() or '.txt'}"
        else:
            raise ValueError(
                f"неизвестный тип файла для jvm: {src.name}. "
                "Ожидаются resources*.yml/.yaml и jvm-config*.txt/.yml/.yaml"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())


def _classify_jvm_upload(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith((".yaml", ".yml")) and ("resource" in lower or "values" in lower):
        return "resources"
    if lower.endswith((".yaml", ".yml", ".txt")) and ("jvm" in lower or "java" in lower):
        return "jvm"
    return ""


def _resolve_jvm_system_dir(system_name: str, resources_root: Path) -> Path:
    if system_name in DEMO_JVM_SYSTEMS:
        return DEMO_JVM_ROOT / system_name
    if system_name == "__root__":
        return resources_root
    return resources_root / system_name


def _resolve_jvm_input_files_for_system(
    system_name: str,
    resources_root: Path,
    *,
    resolve_system_input_files: Any,
) -> tuple[Path, Path | None]:
    if system_name in DEMO_JVM_SYSTEMS:
        return _resolve_demo_jvm_input_files(system_name)
    if system_name == "__root__":
        return _resolve_root_jvm_input_files(resources_root)
    return resolve_system_input_files(
        systems_root=resources_root,
        system_name=system_name,
        resources_file=None,
        jvm_config_file=None,
    )


def _resolve_root_jvm_input_files(resources_root: Path) -> tuple[Path, Path | None]:
    yaml_files = sorted(
        p for p in resources_root.iterdir() if p.is_file() and p.suffix.lower() in (".yaml", ".yml")
    )
    txt_files = sorted(
        p for p in resources_root.iterdir() if p.is_file() and p.suffix.lower() == ".txt"
    )
    resources_file = None
    for p in yaml_files:
        name = p.name.lower()
        if "resource" in name or "values" in name:
            resources_file = p
            break
    if resources_file is None and yaml_files:
        resources_file = yaml_files[0]
    if resources_file is None:
        raise ValueError("В resources/ не найден resources YAML файл")
    jvm_cfg = None
    for p in yaml_files + txt_files:
        name = p.name.lower()
        if "jvm" in name or "java" in name or "option" in name or "tool" in name:
            jvm_cfg = p
            break
    return resources_file, jvm_cfg


def _resolve_demo_jvm_input_files(system_name: str) -> tuple[Path, Path | None]:
    spec = DEMO_JVM_SYSTEMS.get(system_name)
    if not spec:
        raise ValueError(f"Неизвестная demo система: {system_name}")
    ddir = DEMO_JVM_ROOT / system_name
    resources_file = ddir / str(spec["resources_file"])
    jvm_file = ddir / str(spec["jvm_file"])
    if not resources_file.is_file():
        raise ValueError(f"Для demo системы не найден resources файл: {resources_file}")
    return resources_file, (jvm_file if jvm_file.is_file() else None)


def _last_input_key(container_name: str, pod_name: str | None) -> str:
    pod = str(pod_name or "").strip()
    if not pod:
        return container_name
    return f"{pod}::{container_name}"


def _filter_jvm_analysis_by_selected_problems(
    analysis: Any,
    selected_problems: list[str],
) -> None:
    if not selected_problems:
        return
    prefixes: tuple[str, ...] = tuple(
        p for sid in selected_problems for p in JVM_PROBLEM_RULE_PREFIXES.get(sid, ())
    )
    if not prefixes:
        return
    selected_findings = []
    for f in analysis.findings:
        source = str((getattr(f, "evidence", {}) or {}).get("source") or "")
        if source == "context_metric":
            selected_findings.append(f)
            continue
        if f.code.startswith(prefixes):
            selected_findings.append(f)
    analysis.findings = selected_findings
    if not selected_findings:
        analysis.recommendations = []
        return
    selected_codes = {f.code for f in selected_findings}
    filtered = []
    for rec in analysis.recommendations:
        rule_ids = set(getattr(rec, "rule_ids", []) or [])
        if "input.metric_missing" in rule_ids:
            filtered.append(rec)
            continue
        if not rule_ids or rule_ids.intersection(selected_codes):
            filtered.append(rec)
    analysis.recommendations = filtered


def _seed_selected_problem_findings(
    analysis: Any,
    selected_problems: list[str],
    *,
    finding_cls: Any,
) -> None:
    if not selected_problems:
        return
    existing_codes = {f.code for f in analysis.findings}
    # We treat selected problems as already observed facts for targeted impact analysis.
    for pid in selected_problems:
        for code, severity, message in JVM_PROBLEM_SEED_FINDINGS.get(pid, ()):
            if code in existing_codes:
                continue
            analysis.findings.append(
                finding_cls(
                    code=code,
                    severity=severity,
                    message=message,
                    evidence={"source": "selected_problem"},
                    threshold="selected by operator",
                    details={"source": "ui.selected_problems"},
                )
            )
            existing_codes.add(code)


def _audit_selected_problem_inputs(req: JvmAnalyzeRequest) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pid in req.selected_problems:
        required = JVM_PROBLEM_REQUIRED_METRICS.get(pid, ())
        missing: list[str] = []
        if pid == "heap_pressure":
            heap_any = any(
                getattr(req, field, None) is not None
                for field in ("heap_used_mib", "heap_used_percent", "old_gen_used_percent")
            )
            if not heap_any:
                missing.append("Heap used (MiB) or Heap used (%) or OldGen used (%)")
        else:
            for metric_field, metric_label in required:
                if getattr(req, metric_field, None) is None:
                    missing.append(metric_label)
        rows.append(
            {
                "problem_id": pid,
                "required_total": len(required),
                "missing": missing,
                "status": "ok" if not missing else "partial",
            }
        )
    return rows


def _tree_answers_from_request(req: JvmAnalyzeRequest) -> Any:
    from jvmcheck.diagnostic_tree import TreeAnswers

    tree = req.tree or JvmTreeAnswers()
    return TreeAnswers(
        pods_per_shoulder=tree.pods_per_shoulder,
        restart_kind=tree.restart_kind,
        memory_cause_closed=tree.memory_cause_closed,
        heap_growing=tree.heap_growing,
        heap_growth_percent=tree.heap_growth_percent,
        heap_growth_hours=tree.heap_growth_hours,
        growth_of=tree.growth_of,
        gc_ran_in_window=tree.gc_ran_in_window,
        heap_used_before_gc_mib=tree.heap_used_before_gc_mib,
        heap_used_after_gc_mib=tree.heap_used_after_gc_mib,
        oldgen_returned_after_gc=tree.oldgen_returned_after_gc,
        current_usage_percent=_current_usage_percent(req, tree),
        cpu_throttled=tree.cpu_throttled,
        cpu_pct_limits_shoulder_1=tree.cpu_pct_limits_shoulder_1,
        cpu_pct_limits_shoulder_2=tree.cpu_pct_limits_shoulder_2,
        gc_pause_p95_ms=req.gc_pause_p95_ms,
        user_latency_grew=tree.user_latency_grew,
        user_latency_p95_ms=tree.user_latency_p95_ms,
        pauses_coincide_throttle=tree.pauses_coincide_throttle,
        post_gc_floor_rising=tree.post_gc_floor_rising,
        gc_cpu_spike_sla=tree.gc_cpu_spike_sla,
    )


def _old_gen_used_percent(req: JvmAnalyzeRequest) -> float | None:
    if req.old_gen_used_percent is not None:
        return req.old_gen_used_percent
    if req.old_gen_used_mib is not None and req.old_gen_capacity_mib:
        return 100.0 * float(req.old_gen_used_mib) / float(req.old_gen_capacity_mib)
    return None


def _current_usage_percent(req: JvmAnalyzeRequest, tree: JvmTreeAnswers) -> float | None:
    growth_of = str(tree.growth_of or "").strip().lower()
    if growth_of == "oldgen":
        return _old_gen_used_percent(req)
    if growth_of == "container_rss":
        return req.container_memory_usage_percent
    if growth_of == "heap":
        return req.heap_used_percent or req.container_memory_usage_percent
    return req.container_memory_usage_percent or _old_gen_used_percent(req)


def _validate_jvm_problem_input_contract(req: JvmAnalyzeRequest) -> str | None:
    return None


def _has_any_context_metrics(req: JvmAnalyzeRequest) -> bool:
    fields = (
        "gc_pause_p95_ms",
        "gc_pause_p99_ms",
        "gc_time_ratio_percent",
        "container_memory_usage_percent",
        "heap_used_mib",
        "heap_used_percent",
        "old_gen_used_mib",
        "old_gen_capacity_mib",
        "old_gen_used_percent",
        "new_gen_used_mib",
        "new_gen_capacity_mib",
        "new_gen_used_percent",
    )
    return any(getattr(req, field, None) is not None for field in fields)


def _enrich_runtime_metrics_from_context(req: JvmAnalyzeRequest, container: Any) -> None:
    if (
        req.container_memory_working_set_mib is None
        and req.container_memory_usage_percent is not None
        and container.limits.memory_mib
    ):
        req.container_memory_working_set_mib = int(
            max(0.0, req.container_memory_usage_percent) * float(container.limits.memory_mib) / 100.0
        )
    if (
        req.old_gen_used_mib is None
        and req.old_gen_used_percent is not None
        and req.old_gen_capacity_mib
    ):
        req.old_gen_used_mib = int(
            max(0.0, req.old_gen_used_percent) * float(req.old_gen_capacity_mib) / 100.0
        )
    if (
        req.old_gen_used_percent is None
        and req.old_gen_used_mib is not None
        and req.old_gen_capacity_mib
    ):
        req.old_gen_used_percent = (
            100.0 * float(req.old_gen_used_mib) / float(req.old_gen_capacity_mib)
        )
    if (
        req.heap_used_mib is None
        and req.heap_used_percent is not None
        and req.heap_committed_mib
    ):
        req.heap_used_mib = int(
            max(0.0, req.heap_used_percent) * float(req.heap_committed_mib) / 100.0
        )


def _add_contextual_signal_findings(analysis: Any, req: JvmAnalyzeRequest, *, finding_cls: Any) -> None:
    existing = {f.code for f in analysis.findings}

    def add(code: str, severity: str, message: str, threshold: str) -> None:
        if code in existing:
            return
        analysis.findings.append(
            finding_cls(
                code=code,
                severity=severity,
                message=message,
                evidence={"source": "context_metric"},
                threshold=threshold,
                details={"source": "ui.metric_input"},
            )
        )
        existing.add(code)

    if req.heap_used_percent is None and req.heap_used_mib is None:
        add(
            "heap.metric_missing",
            "info",
            "Heap utilization is not provided. Recommendations may be less precise.",
            "provide heap_used_mib or heap_used_percent",
        )
    elif req.heap_used_percent is not None and req.heap_used_percent >= 85:
        add(
            "heap.high_utilization_signal",
            "warning",
            f"Heap utilization is high ({req.heap_used_percent:.1f}%).",
            "heap_used_percent >= 85",
        )

    if req.old_gen_used_percent is None and req.old_gen_used_mib is None:
        add(
            "oldgen.metric_missing",
            "info",
            "OldGen utilization is not provided. OldGen recommendations may be less precise.",
            "provide old_gen_used_percent or old_gen_used_mib",
        )
    elif req.old_gen_used_percent is not None and req.old_gen_used_percent >= 80:
        add(
            "oldgen.high_utilization_signal",
            "warning",
            f"OldGen utilization is high ({req.old_gen_used_percent:.1f}%).",
            "old_gen_used_percent >= 80",
        )

    if req.new_gen_used_percent is not None and req.new_gen_used_percent >= 75:
        add(
            "newgen.high_utilization_signal",
            "info",
            f"NewGen utilization is elevated ({req.new_gen_used_percent:.1f}%).",
            "new_gen_used_percent >= 75",
        )


def _add_missing_input_recommendations(
    analysis: Any,
    req: JvmAnalyzeRequest,
    *,
    recommendation_cls: Any,
) -> None:
    warnings: list[str] = []
    if req.heap_used_percent is None and req.heap_used_mib is None:
        warnings.append("Добавьте heap_used_mib или heap_used_percent для точной оценки pressure.")
    if req.old_gen_used_percent is None and req.old_gen_used_mib is None:
        warnings.append("Добавьте old_gen_used_percent или old_gen_used_mib для точной оценки oldgen pressure.")
    if req.gc_pause_p95_ms is None and "gc_latency" in req.selected_problems:
        warnings.append("Для gc_latency укажите gc_pause_p95_ms.")
    if req.container_memory_usage_percent is None and "memory_pressure" in req.selected_problems:
        warnings.append("Для memory_pressure укажите container_memory_usage_percent.")
    if not warnings:
        return
    analysis.recommendations.append(
        recommendation_cls(
            title="Input data quality warning",
            rationale="Some problem-specific inputs are missing; analysis confidence is reduced.",
            suggested_java_tool_options=[],
            confidence="low",
            evidence_score=35,
            risk_score=10,
            expected_gain="higher recommendation accuracy after metric enrichment",
            verification_window="before next tuning cycle",
            rollback_plan=["No rollback required."],
            rule_ids=["input.metric_missing"],
            notes=warnings,
        )
    )


def _apply_contextual_jvm_guardrails(
    *,
    analysis: Any,
    selected_problems: list[str],
    container: Any,
    req: JvmAnalyzeRequest,
) -> list[str]:
    messages: list[str] = []
    if not selected_problems:
        return messages

    heap_ratio = _ratio(req.heap_used_mib, req.heap_committed_mib)
    old_gen_ratio = _ratio(req.old_gen_used_mib, req.old_gen_capacity_mib)
    high_heap_pressure = any(
        value is not None and value >= 0.82 for value in (heap_ratio, old_gen_ratio)
    )
    if req.heap_used_percent is not None and req.heap_used_percent >= 82:
        high_heap_pressure = True
    if req.old_gen_used_percent is not None and req.old_gen_used_percent >= 80:
        high_heap_pressure = True
    current_max_ram = _read_flag_float(container.java_tool_options, "-XX:MaxRAMPercentage")

    if "gc_latency" in selected_problems and high_heap_pressure:
        messages.append(
            "GC latency observed with already high heap utilization: avoid heap shrinking and prioritize GC policy tuning."
        )
        for rec in analysis.recommendations:
            original = list(rec.suggested_java_tool_options or [])
            kept: list[str] = []
            removed: list[str] = []
            for flag in original:
                if not flag.startswith("-XX:MaxRAMPercentage="):
                    kept.append(flag)
                    continue
                proposed = _flag_float_value(flag)
                if proposed is None:
                    kept.append(flag)
                    continue
                lower_than_current = (
                    current_max_ram is not None and proposed < current_max_ram
                )
                too_low_without_baseline = (
                    current_max_ram is None and proposed < 65.0
                )
                if lower_than_current or too_low_without_baseline:
                    removed.append(flag)
                else:
                    kept.append(flag)
            if removed:
                rec.suggested_java_tool_options = kept
                rec.blocking_conditions = list(rec.blocking_conditions or []) + [
                    "High heap utilization: do not reduce heap budget while fixing GC latency."
                ]
                rec.notes = list(rec.notes or []) + [
                    "Removed risky option(s): " + ", ".join(removed)
                ]
                messages.append(
                    "GC latency selected with high heap usage: recommendations that reduce "
                    "MaxRAMPercentage were removed."
                )
    return sorted(set(messages))


def _annotate_recommendation_diffs(analysis: Any, container: Any) -> None:
    # Was/to already lives in the resources and suggested-options tables.
    return


def _recommendation_diff_lines(
    current_map: dict[str, str],
    proposed_flags: list[str],
) -> list[str]:
    lines: list[str] = []
    for flag in proposed_flags:
        key, raw_value = _split_flag(flag)
        if not key:
            continue
        old = current_map.get(key)
        new = raw_value if raw_value is not None else "enabled"
        if old is None:
            lines.append(f"- add {key}={new}")
            continue
        if old == new:
            lines.append(f"- keep {key}={new} (already set)")
            continue
        lines.append(f"- change {key}: {old} -> {new}")
    return lines


def _flag_map(flags: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for flag in flags:
        key, value = _split_flag(flag)
        if not key:
            continue
        result[key] = value if value is not None else "enabled"
    return result


def _split_flag(flag: str) -> tuple[str, str | None]:
    raw = str(flag or "").strip()
    if not raw:
        return "", None
    if "=" in raw:
        key, value = raw.split("=", 1)
        return key.strip(), value.strip()
    return raw, None


def _read_flag_float(flags: list[str], key: str) -> float | None:
    for flag in flags:
        fkey, raw = _split_flag(flag)
        if fkey != key:
            continue
        try:
            return float(raw) if raw is not None else None
        except Exception:
            return None
    return None


def _flag_float_value(flag: str) -> float | None:
    _, raw = _split_flag(flag)
    try:
        return float(raw) if raw is not None else None
    except Exception:
        return None


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def _build_jvm_problem_statement_block(req: JvmAnalyzeRequest, container: Any) -> str:
    container_name = str(getattr(container, "name", "") or "")
    pod_name = str(getattr(container, "pod_name", "") or "")
    tree = req.tree or JvmTreeAnswers()
    lines = [
        "h2. Цель и ответы дерева",
        f"*Система:* {req.system_name}",
        f"*Pod:* {pod_name or 'не задан'}",
        f"*Контейнер:* {container_name}",
        f"*Подов на одном плече:* {_display_or_na(tree.pods_per_shoulder)} (плеч всегда 2)",
        f"*Рестарт:* {_display_or_na(tree.restart_kind)}",
        f"*CPU throttle:* {_display_or_na(tree.cpu_throttled)}",
        "",
    ]
    metric_rows = [
        ("gc_pause_p95_ms", req.gc_pause_p95_ms),
        ("cpu_pct_limits_shoulder_1", tree.cpu_pct_limits_shoulder_1),
        ("cpu_pct_limits_shoulder_2", tree.cpu_pct_limits_shoulder_2),
        ("user_latency_grew", tree.user_latency_grew),
        ("heap_used_mib", req.heap_used_mib),
        ("old_gen_used_percent", req.old_gen_used_percent),
    ]
    filled = [(name, value) for name, value in metric_rows if value is not None]
    if filled:
        lines.append("|| Параметр || Значение ||")
        for name, value in filled:
            lines.append(f"| {name} | {_display_or_na(value)} |")
    return "\n".join(lines).rstrip() + "\n"


def _display_or_na(value: Any) -> str:
    if value is None:
        return "не задан"
    text = str(value)
    mapped = {
        "yes": "да",
        "no": "нет",
        "unknown": "не знаю",
        "none": "не рестартовал",
        "oomkilled": "OOMKilled",
        "evicted": "Evicted",
        "java_oome": "Java OutOfMemoryError",
    }.get(text.strip().lower())
    return mapped or text


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_jvm_targeted_context_section(
    *,
    req: JvmAnalyzeRequest,
    container_name: str,
    input_audit: list[dict[str, Any]],
    guardrails: list[str],
    analysis: dict[str, Any],
    current_java_options: list[str],
    copyable: bool = False,
) -> str:
    lines = [
        "h3. Контекст прогона",
        f"*Целевой контейнер:* {container_name}",
        "",
    ]
    if guardrails:
        lines.append("h3. Ограничения и защитные правила")
        for msg in guardrails:
            lines.append(f"* {msg}")
        lines.append("")
    lines.append("h3. Результат анализа")
    lines.append(
        f"* Findings: {len(analysis.get('findings') or [])}; рекомендаций: {len(analysis.get('recommendations') or [])}."
    )
    lines.append("")
    lines.extend(
        _build_jvm_copy_paste_section(
            analysis=analysis,
            container_name=container_name,
            current_java_options=current_java_options,
            copyable=copyable,
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def _build_jvm_copy_paste_section(
    *,
    analysis: dict[str, Any],
    container_name: str,
    current_java_options: list[str],
    copyable: bool = False,
) -> list[str]:
    lines: list[str] = ["h3. Предлагаемые изменения (Copy/Paste)"]
    proposed = _collect_proposed_flags(analysis) if copyable else []
    if not copyable:
        lines.append("* Копируемую строку JAVA_TOOL_OPTIONS не даём — см. кандидатов в Expand выше.")
        return lines
    if not proposed:
        lines.append("* Нет прямых изменений JVM-флагов для копирования.")
    else:
        effective = _merge_java_options(current_java_options, proposed)
        lines.append("h4. jvm-config")
        lines.append("{code:yaml}")
        lines.append(f"{container_name}:")
        lines.append("  javaToolOptions: >")
        for flag in effective:
            lines.append(f"    {flag}")
        lines.append("{code}")
        lines.append("")
        lines.append("h4. Сводка изменений")
        for item in _copy_paste_diff_lines(current_java_options, effective):
            lines.append(f"* {item}")
    return lines


def _collect_proposed_flags(analysis: dict[str, Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for rec in analysis.get("recommendations") or []:
        for flag in rec.get("suggested_java_tool_options") or []:
            text = str(flag).strip()
            if not text:
                continue
            if text in seen:
                continue
            seen.add(text)
            ordered.append(text)
    return ordered


def _merge_java_options(current: list[str], proposed: list[str]) -> list[str]:
    merged: list[str] = []
    key_to_idx: dict[str, int] = {}
    for flag in current:
        key, _ = _split_flag(flag)
        if not key:
            continue
        key_to_idx[key] = len(merged)
        merged.append(flag)
    for flag in proposed:
        key, _ = _split_flag(flag)
        if not key:
            continue
        if key in key_to_idx:
            merged[key_to_idx[key]] = flag
        else:
            key_to_idx[key] = len(merged)
            merged.append(flag)
    return merged


def _copy_paste_diff_lines(current: list[str], effective: list[str]) -> list[str]:
    before = _flag_map(current)
    after = _flag_map(effective)
    lines: list[str] = []
    for key, value in after.items():
        prev = before.get(key)
        if prev is None:
            lines.append(f"добавить {key}={value}")
        elif prev != value:
            lines.append(f"изменить {key}: {prev} -> {value}")
    if not lines:
        lines.append("дельта отсутствует (все предложенные флаги уже заданы)")
    return lines


def _build_pod_scale_out_guidance(
    req: JvmAnalyzeRequest,
    analysis: dict[str, Any],
) -> list[str]:
    finding_codes = {str(f.get("code") or "") for f in (analysis.get("findings") or [])}
    lines: list[str] = ["h3. Когда помогает увеличение количества pod'ов"]
    lines.append("* Масштабирование pod'ов обычно помогает при CPU/GC перегрузке под стабильной высокой нагрузкой, когда память одного pod не упирается в лимит.")
    if "gc_latency" in req.selected_problems:
        lines.append("* Для GC latency scale-out полезен, если паузы растут из-за высокой конкурентной нагрузки и при этом нет выраженного memory pressure.")
    if "memory_pressure" in req.selected_problems or "memory.limit_pressure" in finding_codes:
        lines.append("* При memory pressure scale-out помогает ограниченно: если каждый pod уже близко к memory limit, сначала правят memory budget/JVM, а потом масштабируют.")
    if "heap_pressure" in req.selected_problems:
        lines.append("* При высоком heap/oldgen pressure scale-out уместен после проверки, что профиль нагрузки горизонтально делится и heap каждого pod действительно снизится.")
    lines.append("* Не рекомендуется рассчитывать только на scale-out, если проблема вызвана утечкой памяти или некорректными JVM-флагами.")
    return lines


def _localize_jvm_wiki_text(text: str) -> str:
    mapping = {
        "h2. JVM Tuning Recommendation": "h2. Рекомендации по JVM-тюнингу",
        "*System:*": "*Система:*",
        "*Target container:*": "*Целевой контейнер:*",
        "No critical JVM changes required": "Критичных изменений JVM не требуется",
        "Current metrics do not indicate GC or heap pressure above thresholds.": "Текущие метрики не показывают давление GC или heap выше порогов.",
        "keep baseline stable": "оставить baseline без изменений",
        "lower tail latency": "короче хвост задержки",
        "No rollback required.": "Откат не требуется.",
        "30-60m after deploy": "30–60 мин после выкатки",
        "next scheduled load profile": "следующий профильный прогон нагрузки",
        "Heap utilization is not provided. Recommendations may be less precise.": "Heap used не задан. Рекомендации могут быть менее точными.",
        "OldGen utilization is not provided. OldGen recommendations may be less precise.": "OldGen used не задан. Рекомендации по OldGen могут быть менее точными.",
        "*Lifecycle status:*": "*Статус:*",
        "tuning_not_effective": "предыдущая попытка тюнинга не дала эффекта",
        "escalate_to_dev_memory_dump": "нужен memory dump разработке",
        "h3. Runtime Context": "h3. Контекст рантайма",
        "|| Parameter || Value ||": "|| Параметр || Значение ||",
        "h3. Findings": "h3. Findings",
        "* No critical findings detected.": "* Критичные findings не обнаружены.",
        "h3. Recommended Java Tool Options": "h3. Рекомендованные Java Tool Options",
        "* No recommendations.": "* Рекомендации не сформированы.",
        "Raise container memory request to match actual usage": "Поднимите memory request контейнера под фактическое потребление",
        "Working set is above memory request. Raise request toward observed usage (still below limit). This is not a G1 change.": "Процесс ест больше, чем memory request. Поднимите request к фактическому потреблению (ниже limit). Это не настройка G1.",
        "Increase memory request closer to baseline working set, keeping it below limit.": "Поднимите memory request ближе к фактическому working set, но оставьте его ниже limit.",
        "Do not change JAVA_TOOL_OPTIONS for this finding.": "JAVA_TOOL_OPTIONS из-за этого finding не меняйте.",
        "Reduce GC p95 pause": "Снизьте GC p95",
        "p95 pause is above profile threshold.": "GC p95 выше порога профиля.",
        "Reduce GC p99 pause spikes": "Снизьте всплески GC p99",
        "Lower GC CPU overhead": "Снизьте долю CPU на GC",
        "Stabilize OldGen occupancy": "Стабилизируйте заполнение OldGen",
        "Critical OldGen saturation risk": "OldGen почти заполнен",
        "Heap headroom is low": "Мало запаса в heap",
        "Reduce memory limit pressure": "Снизьте давление на memory limit",
        "Working set is near container memory limit.": "Working set близко к memory limit контейнера.",
        "Request-to-limit ratio is too tight": "Memory request слишком близко к limit",
        "Memory request is almost equal to limit.": "Memory request почти равен limit.",
        "Leave headroom between request and limit for burst tolerance.": "Оставьте запас между request и limit под всплески.",
        "Missing memory limit": "Не задан memory limit",
        "Container has no memory limit and may impact pod stability.": "У контейнера нет memory limit — это бьёт по стабильности pod.",
        "Set explicit memory limit and request.": "Задайте memory limit и request явно.",
        "Container support flag is missing": "Нет флага container support",
        "ExitOnOutOfMemoryError is missing": "Нет ExitOnOutOfMemoryError",
        "Duplicate JVM flags found": "Дублируются JVM-флаги",
        "Conflicting RAM percentage flags": "Конфликт MaxRAMPercentage и InitialRAMPercentage",
        "GC strategy not explicitly declared": "Сборщик GC не задан явно",
        "Quota-aware memory rebalance plan": "Перераспределите memory quota pod",
        "GC p95 pause exceeds threshold.": "GC p95 выше порога.",
        "Working set significantly exceeds memory request.": "Working set заметно выше memory request.",
        "Container memory consumption is close to memory limit.": "Потребление памяти близко к memory limit.",
        "*Target pod:*": "*Целевой pod:*",
        "*Rationale:*": "*Обоснование:*",
        "*Confidence:*": "*Уверенность:*",
        "*Evidence score:*": "*Оценка доказательности:*",
        "*Risk score:*": "*Оценка риска:*",
        "*Expected gain:*": "*Ожидаемый эффект:*",
        "*Verification window:*": "*Окно верификации:*",
        "*Platform escalation required:*": "*Нужна платформенная эскалация:*",
        "*Suggested options:*": "*Предлагаемые опции:*",
        "*Rollback plan:*": "*План отката:*",
        "*Blocking conditions:*": "*Блокирующие условия:*",
        "*Notes:*": "*Примечания:*",
        "h3. Pod Memory Quota Plan": "h3. План по pod memory quota",
        "h3. Multi-run Stability": "h3. Стабильность на нескольких прогонах",
        "h3. Engineer Validation Runbook": "h3. Инженерный план валидации",
        "h3. Change Risks and Side Effects": "h3. Риски и побочные эффекты изменений",
        "h3. Escalation Rule": "h3. Правило эскалации",
    }
    localized = text
    for src, dst in mapping.items():
        localized = localized.replace(src, dst)
    return localized


def _severity_bucket(severity: str) -> str:
    raw = str(severity or "warning").lower()
    if raw in {"critical", "high"}:
        return "critical"
    if raw in {"info", "low"}:
        return "info"
    return "warning"


def _build_jvm_summary(req: JvmAnalyzeRequest, analysis: dict[str, Any]) -> dict[str, Any]:
    cards = []
    for finding in analysis.get("findings") or []:
        code = str(finding.get("code") or "?")
        cards.append(
            {
                "id": code,
                "severity": str(finding.get("severity") or "warning"),
                "message": str(finding.get("message") or ""),
                "title": code,
                "advice": "",
                "threshold": str(finding.get("threshold") or ""),
            }
        )
    counts = {"critical": 0, "warning": 0, "info": 0}
    for c in cards:
        bucket = _severity_bucket(c.get("severity", "warning"))
        counts[bucket] += 1
    return {
        "mode": "jvm",
        "system_name": req.system_name,
        "selected_problems": req.selected_problems,
        "threshold_profile": req.threshold_profile,
        "total_findings": len(cards),
        "severity_counts": counts,
        "findings_ui": cards,
    }


def _build_jvm_brief(
    req: JvmAnalyzeRequest,
    container_name: str,
    analysis: dict[str, Any],
    *,
    input_audit: list[dict[str, Any]] | None = None,
    guardrails: list[str] | None = None,
) -> str:
    lines = [
        "# JVM Analysis Brief",
        "",
        f"- System: `{req.system_name}`",
        f"- Container: `{container_name}`",
        f"- Selected problems: `{', '.join(req.selected_problems)}`",
        f"- Threshold profile: `{req.threshold_profile}`",
        f"- Findings: `{len(analysis.get('findings') or [])}`",
        "",
    ]
    for finding in analysis.get("findings") or []:
        sev = str(finding.get("severity") or "warning").upper()
        lines.append(f"## [{sev}] {finding.get('code')}")
        lines.append(f"- Message: {finding.get('message')}")
        threshold = str(finding.get("threshold") or "")
        if threshold:
            lines.append(f"- Threshold: {threshold}")
        lines.append("")
    if input_audit:
        lines.append("## Problem input coverage")
        for row in input_audit:
            missing = ", ".join(row["missing"]) if row["missing"] else "-"
            lines.append(
                f"- {row['problem_id']}: status={row['status']}, missing_metrics={missing}"
            )
        lines.append("")
    if guardrails:
        lines.append("## Guardrails")
        for item in guardrails:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_jvm_prompt(brief: str) -> str:
    return (
        "Подготовь инженерную сводку по JVM-анализу на русском языке.\n"
        "Обязательно учитывай выбранные проблемы и введённые пользователем метрики как входные факты.\n"
        "Не предлагай шаги, противоречащие guardrails (например, не уменьшай heap budget при высокой утилизации heap/oldgen).\n"
        "Если данных недостаточно, явно выдели это как ограничение точности и предложи, какие метрики собрать.\n"
        "Отдельно укажи, когда уместен scale-out по pod'ам, а когда сначала нужен JVM/memory tuning.\n"
        "Структурируй ответ как: Риски -> Рекомендованные изменения -> План проверки -> Условия эскалации.\n\n"
        "DATA:\n\n"
        f"{brief}"
    )
