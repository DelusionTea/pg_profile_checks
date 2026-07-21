from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

import yaml
from yaml import YAMLError

from jvmcheck.validation import InputValidationError


SECTION_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*:\s*(.*)$")
JAVA_KEYS = {"javaToolOptions", "java_tool_options", "javaOptions", "JAVA_TOOL_OPTIONS", "JAVA_OPTS", "JAVA_TOOL_OPTS"}


def parse_custom_jvm_options(text: str) -> Dict[str, List[str]]:
    """
    Parse non-uniform config blocks like resources/jvmconf.txt.

    Returns mapping: section_name -> list of JVM/Java opts.
    """
    result: Dict[str, List[str]] = {}
    current_section: str | None = None
    buffer: List[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        match = SECTION_RE.match(line)
        if match and not line.lstrip().startswith("-"):
            if current_section:
                result[current_section] = _extract_opts(" ".join(buffer))
            current_section = match.group(1)
            remainder = match.group(2).strip()
            buffer = [remainder] if remainder else []
            continue
        if current_section:
            buffer.append(line.strip().strip("'\""))

    if current_section:
        result[current_section] = _extract_opts(" ".join(buffer))

    return result


def parse_jvm_options_file(path: Path) -> Dict[str, List[str]]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in {".yaml", ".yml"}:
        try:
            return _parse_yaml_jvm_options(text)
        except YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            raise InputValidationError(
                "Invalid JVM config YAML",
                file_path=path,
                line=(mark.line + 1) if mark else None,
                column=(mark.column + 1) if mark else None,
                hint="Expected valid YAML with javaToolOptions / JAVA_TOOL_OPTIONS fields.",
            ) from exc
    return parse_custom_jvm_options(text)


def _extract_opts(raw: str) -> List[str]:
    cleaned = raw.replace("javaToolOptions:", " ")
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    return [token for token in tokens if token.startswith("-")]


def _parse_yaml_jvm_options(text: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for doc in yaml.safe_load_all(text):
        if not isinstance(doc, dict):
            continue
        for path, node in _walk_dict(doc):
            if not isinstance(node, dict):
                continue
            for key in JAVA_KEYS:
                if key not in node:
                    continue
                opts = _extract_opts(str(node.get(key, "")))
                if not opts:
                    continue
                section_name = path.split(".")[-1]
                out[section_name] = opts
    return out


def _walk_dict(root: Dict[str, object], prefix: str = ""):
    for key, value in root.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        yield path, value
        if isinstance(value, dict):
            yield from _walk_dict(value, path)

