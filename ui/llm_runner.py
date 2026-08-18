"""Background LLM jobs for a UI analysis session.

Statuses are persisted in the session output directory so the browser can poll
without holding an HTTP connection for the whole generation.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pgprofile_llm import LLMError, LLMProvider, build_provider, load_llm_config
from pgprofile_llm_tasks import (
    TASK_PRESETS,
    build_prompt_bundle,
    write_llm_artifacts,
)
from pgprofile_llm_validate import record_llm_quality

JOB_FILENAME = "llm_job.json"
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAIL = "fail"
ACTIVE_STATUSES = frozenset({STATUS_QUEUED, STATUS_RUNNING})

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


class LLMJobConflict(RuntimeError):
    """A job is already queued or running for this session."""


class LLMJobError(ValueError):
    """The request cannot be started (unknown task, missing session output)."""


def job_path(output_dir: Path) -> Path:
    return output_dir / JOB_FILENAME


def _lock_for(output_dir: Path) -> threading.Lock:
    key = str(output_dir.resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_job(output_dir: Path) -> dict[str, Any] | None:
    path = job_path(output_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def write_job(output_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = job_path(output_dir)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _public_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop prompt bodies; the UI only needs status, error and artifact names."""
    return {
        "status": payload.get("status") or STATUS_QUEUED,
        "task": payload.get("task") or "",
        "provider": payload.get("provider") or "",
        "model": payload.get("model") or "",
        "trace_id": payload.get("trace_id") or "",
        "error": payload.get("error"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "latency_ms": payload.get("latency_ms"),
        "attempts": payload.get("attempts"),
        "char_count": payload.get("char_count"),
        "sources": list(payload.get("sources") or []),
        "policy": payload.get("policy"),
        "answer_file": payload.get("answer_file"),
        "request_file": payload.get("request_file"),
        "response_file": payload.get("response_file"),
        "quality_file": payload.get("quality_file"),
        "publishable": payload.get("publishable"),
        "quality_score": payload.get("quality_score"),
        "quality_verdict": payload.get("quality_verdict"),
        "quality_reasons": list(payload.get("quality_reasons") or []),
    }


def job_status(output_dir: Path) -> dict[str, Any]:
    payload = read_job(output_dir)
    if payload is None:
        return {"status": "idle"}
    return _public_job(payload)


def read_answer(output_dir: Path) -> dict[str, Any] | None:
    payload = read_job(output_dir)
    if not payload or payload.get("status") != STATUS_SUCCESS:
        return None
    rel = str(payload.get("answer_file") or "")
    path = output_dir / rel if rel else None
    if path is None or not path.is_file():
        return None
    return {
        "task": payload.get("task") or "",
        "trace_id": payload.get("trace_id") or "",
        "provider": payload.get("provider") or "",
        "model": payload.get("model") or "",
        "text": path.read_text(encoding="utf-8"),
    }


def execute_llm_job(
    output_dir: Path,
    *,
    task: str,
    provider_name: str | None = None,
    extra_instructions: str = "",
    config: dict[str, Any] | None = None,
    provider: LLMProvider | None = None,
) -> dict[str, Any]:
    """Run bundle → provider → artifacts and return the public job snapshot."""
    if task not in TASK_PRESETS:
        raise LLMJobError(f"unknown task {task!r}; available: {', '.join(sorted(TASK_PRESETS))}")
    if not output_dir.is_dir():
        raise LLMJobError("analysis output directory not found")

    started = time.monotonic()
    started_at = _now_iso()
    resolved_provider = provider_name or ""
    job: dict[str, Any] = {
        "status": STATUS_RUNNING,
        "task": task,
        "provider": resolved_provider,
        "started_at": started_at,
        "extra_instructions": extra_instructions,
    }
    write_job(output_dir, job)

    bundle = None
    provider_info: dict[str, Any] = {}
    try:
        bundle = build_prompt_bundle(
            output_dir,
            task=task,
            overrides={"extra_instructions": extra_instructions} if extra_instructions else {},
        )
        job["trace_id"] = bundle.trace_id
        job["char_count"] = bundle.char_count()
        job["sources"] = list(bundle.sources)
        job["policy"] = dict(bundle.metadata.get("policy") or {})
        write_job(output_dir, job)

        active = provider or build_provider(config, provider_name=provider_name)
        provider_info = active.describe()
        job["provider"] = str(provider_info.get("provider") or active.name)
        job["model"] = str(provider_info.get("model") or active.model)
        write_job(output_dir, job)

        response = active.generate(bundle.to_request())
        written = write_llm_artifacts(
            output_dir, bundle, response=response, provider_info=provider_info
        )
        names = {path.name: path.name for path in written}
        dry_run = response.finish_reason == "dry_run" or str(
            provider_info.get("provider") or active.name
        ) == "dry_run"
        quality = record_llm_quality(
            output_dir,
            response.text,
            task=task,
            dry_run=dry_run,
        )
        job.update(
            {
                "status": STATUS_SUCCESS,
                "finished_at": _now_iso(),
                "latency_ms": response.latency_ms,
                "attempts": response.attempts,
                "model": response.model,
                "trace_id": response.trace_id or bundle.trace_id,
                "request_file": names.get(f"llm_request_{task}.json"),
                "response_file": names.get(f"llm_response_{task}.json"),
                "answer_file": names.get(f"llm_{task}.md"),
                "quality_file": quality.get("quality_file"),
                "publishable": bool(quality.get("publishable")),
                "quality_score": quality.get("score"),
                "quality_verdict": quality.get("verdict"),
                "quality_reasons": list(quality.get("reasons") or [])[:8],
            }
        )
        return write_job(output_dir, job)
    except LLMError as exc:
        if bundle is not None:
            write_llm_artifacts(output_dir, bundle, error=exc, provider_info=provider_info)
        job.update(
            {
                "status": STATUS_FAIL,
                "finished_at": _now_iso(),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": exc.as_dict(),
                "trace_id": exc.trace_id or job.get("trace_id") or "",
                "request_file": f"llm_request_{task}.json" if bundle is not None else None,
                "response_file": f"llm_response_{task}.json" if bundle is not None else None,
            }
        )
        return write_job(output_dir, job)


def start_llm_job(
    output_dir: Path,
    *,
    task: str,
    provider_name: str | None = None,
    extra_instructions: str = "",
    config: dict[str, Any] | None = None,
    provider: LLMProvider | None = None,
    wait: bool = False,
) -> dict[str, Any]:
    """Queue a job. Raises LLMJobConflict if one is already in flight."""
    if task not in TASK_PRESETS:
        raise LLMJobError(f"unknown task {task!r}; available: {', '.join(sorted(TASK_PRESETS))}")
    if not output_dir.is_dir():
        raise LLMJobError("analysis output directory not found")

    lock = _lock_for(output_dir)
    with lock:
        current = read_job(output_dir)
        if current and current.get("status") in ACTIVE_STATUSES:
            raise LLMJobConflict("LLM job is already running for this session")
        queued = write_job(
            output_dir,
            {
                "status": STATUS_QUEUED,
                "task": task,
                "provider": provider_name or "",
                "started_at": _now_iso(),
                "extra_instructions": extra_instructions,
            },
        )

    loaded_config = config if config is not None else load_llm_config()

    def _run() -> None:
        execute_llm_job(
            output_dir,
            task=task,
            provider_name=provider_name,
            extra_instructions=extra_instructions,
            config=loaded_config,
            provider=provider,
        )

    thread = threading.Thread(target=_run, name=f"llm-job-{output_dir.name}", daemon=True)
    thread.start()
    if wait:
        thread.join()
        return _public_job(read_job(output_dir) or queued)
    return _public_job(queued)
