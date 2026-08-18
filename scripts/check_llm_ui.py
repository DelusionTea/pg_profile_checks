#!/usr/bin/env python3
"""Offline checks for one-click LLM jobs in the UI.

Covers the job runner (queued/running/success/fail, conflict, artifacts) and the
HTTP endpoints the browser uses, with dry_run so no Qwen instance is required.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pgprofile_llm import DryRunProvider, LLMRequest, LLMResponse  # noqa: E402
from ui.llm_runner import (  # noqa: E402
    LLMJobConflict,
    LLMJobError,
    job_status,
    read_answer,
    start_llm_job,
)

results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    results.append((bool(condition), label))


def _brief_dir() -> Path:
    out = Path(tempfile.mkdtemp(prefix="llm_ui_out_"))
    (out / "brief.md").write_text(
        "h2. Краткий вердикт\n\nГипотеза подтверждена частично. WAL снизился, CPU без изменений.\n",
        encoding="utf-8",
    )
    return out


def check_dry_run_job() -> None:
    out = _brief_dir()
    job = start_llm_job(out, task="summary", provider_name="dry_run", wait=True)
    check(job["status"] == "success", "dry-run job reaches success")
    check(job.get("publishable") is False, "dry-run job is not publishable")
    check(job.get("quality_verdict") in {"warning", "fail"}, "dry-run records a quality verdict")
    check((out / "llm_quality_summary.json").is_file(), "writes llm_quality_summary.json")
    check(job["provider"] == "dry_run", "dry-run job records provider name")
    check(bool(job["trace_id"]), "dry-run job has trace_id")
    check((job.get("policy") or {}).get("name") == "none", "dry-run job records policy none")
    check((out / "llm_request_summary.json").is_file(), "writes llm_request_summary.json")
    check((out / "llm_response_summary.json").is_file(), "writes llm_response_summary.json")
    check((out / "llm_summary.md").is_file(), "writes llm_summary.md")
    answer = read_answer(out)
    check(answer is not None and "dry-run" in (answer or {}).get("text", ""), "answer text is readable")
    payload = json.loads((out / "llm_response_summary.json").read_text(encoding="utf-8"))
    check(payload.get("status") == "success", "response artifact status=success")
    check(payload.get("trace_id") == job["trace_id"], "response artifact shares trace_id")


def check_unknown_task() -> None:
    out = _brief_dir()
    try:
        start_llm_job(out, task="not-a-task", wait=True)
        check(False, "unknown task is rejected")
    except LLMJobError as exc:
        check("unknown task" in str(exc), "unknown task raises LLMJobError")


def check_missing_brief_fails() -> None:
    out = Path(tempfile.mkdtemp(prefix="llm_ui_empty_"))
    job = start_llm_job(out, task="summary", provider_name="dry_run", wait=True)
    check(job["status"] == "fail", "missing brief → fail, not an HTTP 500")
    check(job.get("error", {}).get("error") == "LLMBundleError", "fail payload is LLMBundleError")


class _BlockingProvider(DryRunProvider):
    def __init__(self, gate: threading.Event) -> None:
        super().__init__()
        self.gate = gate

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.gate.wait(timeout=5)
        return super().generate(request)


def check_conflict() -> None:
    out = _brief_dir()
    gate = threading.Event()
    first = start_llm_job(out, task="summary", provider=_BlockingProvider(gate), wait=False)
    check(first["status"] in {"queued", "running"}, "first job starts in-flight")
    try:
        start_llm_job(out, task="summary", provider_name="dry_run", wait=False)
        check(False, "second job is rejected while first runs")
    except LLMJobConflict:
        check(True, "second job raises LLMJobConflict")
    finally:
        gate.set()
    deadline = time.time() + 3
    while time.time() < deadline and job_status(out).get("status") in {"queued", "running"}:
        time.sleep(0.05)
    check(job_status(out).get("status") == "success", "blocked job completes after release")
    again = start_llm_job(out, task="summary", provider_name="dry_run", wait=True)
    check(again["status"] == "success", "rerun is allowed after success")


def _http_json(url: str, payload: dict | None = None, method: str = "GET") -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
            return int(response.status), body
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"error": raw}
        return int(exc.code), body


def check_http_endpoints() -> None:
    import ui.server as server

    sessions = Path(tempfile.mkdtemp(prefix="llm_ui_sessions_"))
    server.SESSIONS_ROOT = sessions
    session_id = str(uuid.uuid4())
    sdir = sessions / session_id
    out = sdir / "out"
    out.mkdir(parents=True)
    (out / "brief.md").write_text("h2. Вердикт\nNEED-VALIDATION\n", encoding="utf-8")
    (sdir / "meta.json").write_text(
        json.dumps({"session_id": session_id, "scenario": "health"}) + "\n",
        encoding="utf-8",
    )

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        code, providers = _http_json(base + "/api/llm/providers")
        names = [row["provider"] for row in providers.get("providers") or []]
        check(code == 200 and "dry_run" in names, "GET /api/llm/providers lists dry_run")

        code, tasks = _http_json(base + "/api/llm/tasks")
        task_names = [row["task"] for row in tasks.get("tasks") or []]
        check(code == 200 and {"summary", "tuning", "detailed_rca"} <= set(task_names),
            "GET /api/llm/tasks lists presets",
        )

        code, policy = _http_json(base + "/api/llm/policy")
        check(
            code == 200 and (policy.get("policy") or {}).get("name") == "none",
            "GET /api/llm/policy reports none",
        )

        code, llm_status = _http_json(base + "/api/llm/status")
        check(code == 200, "GET /api/llm/status is 200")
        check(llm_status.get("available") is False, "Handler without startup probe does not claim Qwen is up")

        code, started = _http_json(
            base + f"/api/sessions/{session_id}/llm",
            {"task": "summary", "provider": "dry_run"},
            method="POST",
        )
        check(code == 202 and started.get("status") in {"queued", "running", "success"}, "POST starts LLM job")

        status = {}
        deadline = time.time() + 5
        while time.time() < deadline:
            code, status = _http_json(base + f"/api/sessions/{session_id}/llm")
            if code == 200 and status.get("status") in {"success", "fail"}:
                break
            time.sleep(0.05)
        check(code == 200 and status.get("status") == "success", "GET status reaches success")
        check(bool(status.get("trace_id")), "GET status includes trace_id")
        check((status.get("policy") or {}).get("name") == "none", "GET status includes policy none")

        code, answer = _http_json(base + f"/api/sessions/{session_id}/llm/answer")
        check(code == 200 and "dry-run" in (answer.get("text") or ""), "GET answer returns model text")

        code, bad = _http_json(
            base + f"/api/sessions/{session_id}/llm",
            {"task": "nope"},
            method="POST",
        )
        check(code == 400, "POST unknown task → 400")

        missing = str(uuid.uuid4())
        code, _ = _http_json(base + f"/api/sessions/{missing}/llm", {"task": "summary"}, method="POST")
        check(code == 404, "POST unknown session → 404")
    except URLError as exc:
        check(False, f"HTTP endpoints reachable ({exc})")
    finally:
        httpd.shutdown()
        httpd.server_close()


def check_payload_hint() -> None:
    html = (ROOT / "ui" / "web" / "index.html").read_text(encoding="utf-8")
    check("SQL из findings" in html, "UI states default policy does not mask SQL")
    check("bank_redact" in html, "UI mentions bank_redact is not the yaml default")
    check('class="qwen-unavailable"' in html or "qwen-unavailable" in html, "body starts Qwen-hidden until probe")
    check("js-qwen-ui" in html, "Headless Qwen and Quality tab are marked js-qwen-ui")


def main() -> int:
    check_payload_hint()
    check_dry_run_job()
    check_unknown_task()
    check_missing_brief_fails()
    check_conflict()
    check_http_endpoints()
    failed = [label for ok, label in results if not ok]
    for ok, label in results:
        print(f"{'PASS' if ok else 'FAIL'}: {label}")
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
