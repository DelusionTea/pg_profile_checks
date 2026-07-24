from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml
from yaml import YAMLError

from jvmcheck.models import ContainerResources, PodResourcesBudget, ResourceSpec
from jvmcheck.parsers.quantity_parser import parse_cpu_to_millicores, parse_memory_to_mib
from jvmcheck.validation import InputValidationError


JAVA_TOOL_OPTIONS_KEYS = {"JAVA_TOOL_OPTIONS", "JAVA_OPTS", "JAVA_TOOL_OPTS"}
JAVA_TOOL_OPTIONS_INLINE_KEYS = {"javaToolOptions", "java_tool_options", "javaOptions"}


def parse_k8s_or_stand_yaml(text: str, source_path: Path | None = None) -> PodResourcesBudget:
    containers: List[ContainerResources] = []
    pod_memory_limit_mib = None
    pod_memory_request_mib = None
    pod_memory_limit_mib_by_pod: Dict[str, int] = {}
    pod_memory_request_mib_by_pod: Dict[str, int] = {}

    try:
        for doc in yaml.safe_load_all(text):
            if not isinstance(doc, dict):
                continue
            pod_name = _extract_doc_pod_name(doc)
            maybe_containers = _extract_k8s_containers(doc, pod_name=pod_name)
            if maybe_containers:
                containers.extend(maybe_containers)
            else:
                containers.extend(_extract_structured_resource_sections(doc))

            top_resources = doc.get("resources")
            if isinstance(top_resources, dict):
                if _looks_like_resource_bucket(top_resources):
                    containers.append(
                        ContainerResources(
                            name="application",
                            pod_name=pod_name,
                            requests=_parse_resource_spec(top_resources.get("requests") or {}),
                            limits=_parse_resource_spec(top_resources.get("limits") or {}),
                            java_tool_options=[],
                        )
                    )
                pod_memory_limit_mib = _memory_from_bucket(top_resources, "limits")
                pod_memory_request_mib = _memory_from_bucket(top_resources, "requests")
                if pod_name and pod_memory_limit_mib is not None:
                    pod_memory_limit_mib_by_pod[pod_name] = pod_memory_limit_mib
                if pod_name and pod_memory_request_mib is not None:
                    pod_memory_request_mib_by_pod[pod_name] = pod_memory_request_mib
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise InputValidationError(
            "Invalid resources YAML",
            file_path=source_path,
            line=(mark.line + 1) if mark else None,
            column=(mark.column + 1) if mark else None,
            hint="Check indentation and resource quantity format (e.g. 2Gi, 500Mi, 1000m).",
        ) from exc

    return PodResourcesBudget(
        containers=containers,
        pod_memory_limit_mib=pod_memory_limit_mib,
        pod_memory_request_mib=pod_memory_request_mib,
        pod_memory_limit_mib_by_pod=pod_memory_limit_mib_by_pod,
        pod_memory_request_mib_by_pod=pod_memory_request_mib_by_pod,
    )


def _extract_k8s_containers(doc: Dict[str, Any], pod_name: str | None = None) -> List[ContainerResources]:
    template_spec = (
        doc.get("spec", {})
        .get("template", {})
        .get("spec", {})
    )
    pod_spec = doc.get("spec", {})
    container_list = template_spec.get("containers")
    if not isinstance(container_list, list):
        container_list = pod_spec.get("containers")
    if not isinstance(container_list, list):
        return []

    containers: List[ContainerResources] = []
    for item in container_list:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "unnamed-container")
        resources = item.get("resources") or {}
        requests = _parse_resource_spec(resources.get("requests") or {})
        limits = _parse_resource_spec(resources.get("limits") or {})
        jto = _extract_java_options_from_env(item.get("env") or [])
        containers.append(
            ContainerResources(
                name=name,
                pod_name=pod_name,
                requests=requests,
                limits=limits,
                java_tool_options=jto,
            )
        )
    return containers


def _extract_structured_resource_sections(doc: Dict[str, Any]) -> List[ContainerResources]:
    out: List[ContainerResources] = []
    for path, node in _walk_dict(doc):
        if not isinstance(node, dict):
            continue
        resources = node.get("resources")
        if not isinstance(resources, dict):
            continue
        path_parts = path.split(".")
        name = path_parts[-1]
        pod_name = path_parts[0] if len(path_parts) > 1 else None
        requests = _parse_resource_spec(resources.get("requests") or {})
        limits = _parse_resource_spec(resources.get("limits") or {})
        jto = _extract_java_options_from_inline(node)
        out.append(
            ContainerResources(
                name=name,
                pod_name=pod_name,
                requests=requests,
                limits=limits,
                java_tool_options=jto,
            )
        )
    return out


def _extract_doc_pod_name(doc: Dict[str, Any]) -> str | None:
    metadata = doc.get("metadata")
    if isinstance(metadata, dict):
        name = metadata.get("name")
        if name:
            return str(name)
    return None


def _parse_resource_spec(bucket: Dict[str, Any]) -> ResourceSpec:
    return ResourceSpec(
        cpu_millicores=parse_cpu_to_millicores(bucket.get("cpu")),
        memory_mib=parse_memory_to_mib(bucket.get("memory")),
        ephemeral_storage_mib=parse_memory_to_mib(bucket.get("ephemeralStorage")),
    )


def _memory_from_bucket(resources: Dict[str, Any], key: str) -> int | None:
    if key not in resources or not isinstance(resources[key], dict):
        return None
    return parse_memory_to_mib(resources[key].get("memory"))


def _looks_like_resource_bucket(node: Dict[str, Any]) -> bool:
    return any(bucket in node for bucket in ("requests", "limits"))


def _extract_java_options_from_env(env: Iterable[Dict[str, Any]]) -> List[str]:
    for env_entry in env:
        if not isinstance(env_entry, dict):
            continue
        if str(env_entry.get("name")) in JAVA_TOOL_OPTIONS_KEYS:
            return _split_java_options(str(env_entry.get("value", "")))
    return []


def _extract_java_options_from_inline(node: Dict[str, Any]) -> List[str]:
    for key in JAVA_TOOL_OPTIONS_INLINE_KEYS:
        value = node.get(key)
        if value is None:
            continue
        return _split_java_options(str(value))
    return []


def _split_java_options(raw: str) -> List[str]:
    return [part.strip() for part in raw.replace("\n", " ").split(" ") if part.strip()]


def _walk_dict(root: Dict[str, Any], prefix: str = "") -> Iterable[tuple[str, Any]]:
    for key, value in root.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        yield path, value
        if isinstance(value, dict):
            yield from _walk_dict(value, path)

