from __future__ import annotations

from pathlib import Path


RESOURCE_FILE_NAMES_HINTS = ("resource", "resources", "values")
JVM_CONFIG_FILE_NAME_HINTS = ("jvm", "java", "tool", "opts", "options")
YAML_SUFFIXES = {".yaml", ".yml"}
JVM_CONFIG_SUFFIXES = {".yaml", ".yml", ".txt"}


def resolve_system_input_files(
    systems_root: Path,
    system_name: str,
    resources_file: str | None,
    jvm_config_file: str | None,
) -> tuple[Path, Path | None]:
    if resources_file:
        resolved_resources = Path(resources_file)
    else:
        resolved_resources = _auto_select_resources_file(systems_root, system_name)

    if jvm_config_file:
        resolved_jvm_config = Path(jvm_config_file)
    elif system_name:
        resolved_jvm_config = _auto_select_jvm_config_file(systems_root, system_name)
    else:
        resolved_jvm_config = None

    return resolved_resources, resolved_jvm_config


def _auto_select_resources_file(systems_root: Path, system_name: str) -> Path:
    system_dir = systems_root / system_name
    if not system_dir.exists():
        raise ValueError(f"System directory not found: {system_dir}")

    candidates = sorted(p for p in system_dir.rglob("*") if p.is_file() and p.suffix.lower() in YAML_SUFFIXES)
    if not candidates:
        raise ValueError(f"No YAML resource files found in: {system_dir}")

    ranked = sorted(candidates, key=_resources_rank, reverse=True)
    return ranked[0]


def _auto_select_jvm_config_file(systems_root: Path, system_name: str) -> Path | None:
    system_dir = systems_root / system_name
    if not system_dir.exists():
        return None

    candidates = sorted(
        p
        for p in system_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in JVM_CONFIG_SUFFIXES
    )
    if not candidates:
        return None

    ranked = sorted(candidates, key=_jvm_config_rank, reverse=True)
    best = ranked[0]
    if _jvm_config_rank(best) <= 0:
        return None
    return best


def _resources_rank(path: Path) -> int:
    name = path.name.lower()
    rank = 0
    for hint in RESOURCE_FILE_NAMES_HINTS:
        if hint in name:
            rank += 10
    if "jvm" in name or "java" in name:
        rank -= 5
    return rank


def _jvm_config_rank(path: Path) -> int:
    name = path.name.lower()
    rank = 0
    for hint in JVM_CONFIG_FILE_NAME_HINTS:
        if hint in name:
            rank += 10
    if "resource" in name:
        rank -= 5
    return rank

