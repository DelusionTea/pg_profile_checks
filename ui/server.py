#!/usr/bin/env python3
"""Standalone stdlib HTTP UI for pg_profile_checks.

Usage (Python 3.10+ — тот же интерпретатор, что и для CLI):
  .venv/bin/python ui/server.py
  python3 ui/server.py --port 8090 --host 127.0.0.1
"""

import sys

# До любых конструкций 3.10+ / __future__ — иначе на Python 2/3.6 сообщение непонятное:
# "future feature annotations is not defined"
if sys.version_info < (3, 10):
    sys.stderr.write(
        "pg_profile UI requires Python 3.10+ (found %s).\n"
        "Use the same interpreter as for analyze_pgprofile.py, for example:\n"
        "  .venv/bin/python ui/server.py\n"
        "  python3 ui/server.py\n"
        % ".".join(str(x) for x in sys.version_info[:3])
    )
    raise SystemExit(2)

import argparse
import json
import mimetypes
import shutil
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# Defaults (overridden by CLI in main)
SESSION_TTL_SECONDS = 24 * 3600
CLEANUP_INTERVAL_SECONDS = 3600
LLM_CONFIG_PATH: Path | None = None
LLM_PROVIDER_OVERRIDE: str | None = None
LLM_STATUS: dict[str, Any] = {
    "available": False,
    "skipped": True,
    "failed": False,
    "provider": "",
    "provider_type": "",
    "reason": "Qwen probe has not run yet",
    "model": "",
    "url": "",
    "latency_ms": None,
    "trace_id": "",
    "answer_preview": "",
}

# Ensure project root is on sys.path when run as `python ui/server.py`
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pgprofile_llm import describe_providers, load_llm_config, probe_llm_connection  # noqa: E402
from pgprofile_llm_policy import describe_policy  # noqa: E402
from pgprofile_llm_tasks import list_tasks  # noqa: E402
from ui.analysis_runner import (  # noqa: E402
    AnalyzeRequest,
    ReportMeta,
    build_zip,
    list_symptoms,
    list_thresholds,
    run_analysis,
    suggest_label,
    suggest_scenario,
)
from ui.models import JvmTreeAnswers  # noqa: E402
from ui.jvm_runner import (  # noqa: E402
    JvmAnalyzeRequest,
    create_jvm_system,
    list_jvm_containers,
    list_jvm_playbook,
    list_jvm_problems,
    list_jvm_systems,
    load_jvm_last_input,
    run_jvm_analysis,
)
from ui.llm_runner import (  # noqa: E402
    LLMJobConflict,
    LLMJobError,
    job_status,
    read_answer,
    start_llm_job,
)

WEB_ROOT = Path(__file__).resolve().parent / "web"
SESSIONS_ROOT = Path(tempfile.gettempdir()) / "pgprofile_ui_sessions"


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, list[str]], list[tuple[str, str, bytes]]]:
    """Parse multipart/form-data without cgi (removed in Python 3.13+).

    Returns (fields, files) where files are (field_name, filename, data).
    """
    if "multipart/form-data" not in content_type:
        raise ValueError("expected multipart/form-data")
    # Build a minimal MIME message for email parser
    raw = b"Content-Type: " + content_type.encode("utf-8") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    import email
    from email import policy

    msg = email.message_from_bytes(raw, policy=policy.default)
    fields: dict[str, list[str]] = {}
    files: list[tuple[str, str, bytes]] = []
    if not msg.is_multipart():
        raise ValueError("multipart body expected")
    for part in msg.iter_parts():
        disp = part.get_content_disposition()
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename or disp == "attachment":
            files.append((name, filename or "upload.html", payload))
        else:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                text = payload.decode("latin-1", errors="replace")
            fields.setdefault(name, []).append(text)
    return fields, files


def _quality_view(output_dir: Path) -> dict[str, Any]:
    json_path = output_dir / "quality_report.json"
    md_path = output_dir / "quality_report.md"
    report: dict[str, Any] = {}
    if json_path.is_file():
        try:
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                report = loaded
        except json.JSONDecodeError:
            report = {}
    text = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""
    return {"quality_text": text, "quality": report}


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _text_file_response(handler: BaseHTTPRequestHandler, path: Path, download_name: str | None = None) -> None:
    if not path or not path.is_file():
        _json_response(handler, 404, {"error": "file not found"})
        return
    data = path.read_bytes()
    ctype = "text/plain; charset=utf-8"
    if path.suffix == ".wiki":
        ctype = "text/plain; charset=utf-8"
    elif path.suffix == ".md":
        ctype = "text/markdown; charset=utf-8"
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    if download_name:
        handler.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
    handler.end_headers()
    handler.wfile.write(data)


def _bytes_response(
    handler: BaseHTTPRequestHandler,
    data: bytes,
    content_type: str,
    download_name: str | None = None,
) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    if download_name:
        handler.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
    handler.end_headers()
    handler.wfile.write(data)


def _session_dir(session_id: str) -> Path:
    if not re_session(session_id):
        raise ValueError("invalid session id")
    return SESSIONS_ROOT / session_id


def re_session(session_id: str) -> bool:
    try:
        uuid.UUID(session_id)
        return True
    except ValueError:
        return False


def _session_mtime(path: Path) -> float:
    """Newest mtime among session dir / meta.json / out (best-effort)."""
    latest = path.stat().st_mtime
    for candidate in (path / "meta.json", path / "out"):
        try:
            latest = max(latest, candidate.stat().st_mtime)
        except OSError:
            pass
    return latest


def cleanup_old_sessions(
    max_age_seconds: float | None = None,
    *,
    sessions_root: Path | None = None,
) -> int:
    """Delete session dirs older than TTL. Returns number of removed sessions.

    TTL <= 0 disables cleanup. Safe to call concurrently with analyzes: only
    UUID-named directories are considered, and only if older than TTL.
    """
    ttl = SESSION_TTL_SECONDS if max_age_seconds is None else max_age_seconds
    if ttl <= 0:
        return 0
    root = sessions_root or SESSIONS_ROOT
    if not root.is_dir():
        return 0
    cutoff = time.time() - ttl
    removed = 0
    for child in root.iterdir():
        if not child.is_dir() or not re_session(child.name):
            continue
        try:
            if _session_mtime(child) >= cutoff:
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        except OSError as exc:
            sys.stderr.write(f"cleanup: skip {child.name}: {exc}\n")
    if removed:
        sys.stderr.write(
            f"cleanup: removed {removed} session(s) older than {ttl / 3600:.1f}h "
            f"from {root}\n"
        )
    return removed


def _start_cleanup_thread(interval_seconds: float) -> None:
    if interval_seconds <= 0 or SESSION_TTL_SECONDS <= 0:
        return

    def loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                cleanup_old_sessions()
            except Exception as exc:  # noqa: BLE001 — background best-effort
                sys.stderr.write(f"cleanup: error: {exc}\n")

    thread = threading.Thread(target=loop, name="session-cleanup", daemon=True)
    thread.start()


class Handler(BaseHTTPRequestHandler):
    server_version = "PgProfileUI/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path in ("/thresholds", "/thresholds.html"):
            self._serve_static("thresholds.html")
            return
        if path.startswith("/css/") or path.startswith("/js/") or path.startswith("/img/"):
            self._serve_static(path.lstrip("/"))
            return
        if path == "/api/symptoms":
            _json_response(self, 200, {"symptoms": list_symptoms()})
            return
        if path == "/api/thresholds":
            try:
                _json_response(self, 200, list_thresholds())
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return
        if path == "/api/suggest":
            qs = parse_qs(parsed.query)
            # lightweight suggest for scenario from query counts — used optionally
            _json_response(self, 200, {"ok": True, "hint": qs})
            return
        if path == "/api/jvm/systems":
            _json_response(self, 200, {"systems": list_jvm_systems()})
            return
        if path == "/api/jvm/problems":
            _json_response(self, 200, {"problems": list_jvm_problems()})
            return
        if path == "/api/jvm/playbook":
            try:
                _json_response(self, 200, {"playbook": list_jvm_playbook()})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return
        if path == "/api/jvm/containers":
            qs = parse_qs(parsed.query)
            system_name = str((qs.get("system") or [""])[0]).strip()
            _json_response(
                self,
                200,
                {"containers": list_jvm_containers(system_name)},
            )
            return
        if path == "/api/jvm/last-input":
            qs = parse_qs(parsed.query)
            system_name = str((qs.get("system") or [""])[0]).strip()
            container_name = str((qs.get("container") or [""])[0]).strip()
            pod_name = str((qs.get("pod") or [""])[0]).strip() or None
            values = load_jvm_last_input(system_name, container_name, pod_name=pod_name)
            if not values:
                _json_response(self, 404, {"error": "last input not found"})
                return
            _json_response(self, 200, {"values": values})
            return
        if path == "/api/llm/status":
            _json_response(self, 200, dict(LLM_STATUS))
            return
        if path == "/api/llm/providers":
            try:
                rows = describe_providers(load_llm_config(LLM_CONFIG_PATH))
                if LLM_PROVIDER_OVERRIDE:
                    for row in rows:
                        row["is_default"] = row.get("provider") == LLM_PROVIDER_OVERRIDE
                _json_response(self, 200, {"providers": rows})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return
        if path == "/api/llm/tasks":
            _json_response(self, 200, {"tasks": list_tasks()})
            return
        if path == "/api/llm/policy":
            try:
                _json_response(self, 200, {"policy": describe_policy()})
            except Exception as exc:
                _json_response(self, 500, {"error": str(exc)})
            return

        # /api/sessions/{id}/wiki|prompt|brief|zip|meta|llm|quality
        # /api/sessions/{id}/llm/answer
        parts = path.strip("/").split("/")
        if len(parts) in {4, 5} and parts[0] == "api" and parts[1] == "sessions":
            session_id, kind = parts[2], parts[3]
            extra = parts[4] if len(parts) == 5 else ""
            try:
                sdir = _session_dir(session_id)
            except ValueError:
                _json_response(self, 400, {"error": "invalid session id"})
                return
            out = sdir / "out"
            meta_path = sdir / "meta.json"
            if kind == "meta":
                if extra:
                    _json_response(self, 404, {"error": "not found"})
                    return
                if not meta_path.is_file():
                    _json_response(self, 404, {"error": "session not found"})
                    return
                _json_response(self, 200, json.loads(meta_path.read_text(encoding="utf-8")))
                return
            if kind == "llm":
                if not out.is_dir():
                    _json_response(self, 404, {"error": "session output not found"})
                    return
                if extra == "answer":
                    answer = read_answer(out)
                    if answer is None:
                        _json_response(self, 404, {"error": "LLM answer not ready"})
                        return
                    _json_response(self, 200, answer)
                    return
                if extra:
                    _json_response(self, 404, {"error": "not found"})
                    return
                _json_response(self, 200, job_status(out))
                return
            if kind == "quality":
                if extra:
                    _json_response(self, 404, {"error": "not found"})
                    return
                if not out.is_dir():
                    _json_response(self, 404, {"error": "session output not found"})
                    return
                _json_response(self, 200, _quality_view(out))
                return
            if extra:
                _json_response(self, 404, {"error": "not found"})
                return
            if not out.is_dir():
                _json_response(self, 404, {"error": "session output not found"})
                return
            meta = {}
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if kind == "wiki":
                rel = meta.get("wiki")
                _text_file_response(self, out / rel if rel else Path(), download_name=rel)
                return
            if kind == "prompt":
                rel = meta.get("prompt")
                _text_file_response(self, out / rel if rel else Path(), download_name=rel)
                return
            if kind == "brief":
                rel = meta.get("brief")
                _text_file_response(self, out / rel if rel else Path(), download_name=rel)
                return
            if kind == "zip":
                data = build_zip(out)
                _bytes_response(
                    self,
                    data,
                    "application/zip",
                    download_name=f"pgprofile_{session_id[:8]}.zip",
                )
                return

        _json_response(self, 404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if (
            len(parts) == 4
            and parts[0] == "api"
            and parts[1] == "sessions"
            and parts[3] == "llm"
        ):
            self._handle_llm_start(parts[2])
            return
        if parsed.path == "/api/jvm/systems":
            self._handle_create_jvm_system()
            return
        if parsed.path != "/api/analyze":
            _json_response(self, 404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            fields, files = _parse_multipart(ctype, body)

            meta_raw = fields.get("meta", ["{}"])[0]
            meta = json.loads(meta_raw)
            mode = str(meta.get("mode") or "pg_profile")
            scenario = str(meta.get("scenario") or "health")
            symptoms = list(meta.get("symptoms") or [])
            reports_meta = meta.get("reports") or []
            if mode != "jvm" and not files:
                _json_response(self, 400, {"error": "нет загруженных файлов"})
                return
            if mode == "jvm":
                session_id = str(uuid.uuid4())
                sdir = SESSIONS_ROOT / session_id
                uploads = sdir / "uploads"
                out = sdir / "out"
                uploads.mkdir(parents=True, exist_ok=True)
                jvm_blobs = [(fname, data) for name, fname, data in files if name in ("jvm_file", "file")]
                upload_paths: list[Path] = []
                for i, (fname, data) in enumerate(jvm_blobs):
                    safe_name = Path(fname).name
                    dest = uploads / f"{i:02d}_{safe_name}"
                    dest.write_bytes(data)
                    upload_paths.append(dest)
                tree_raw = meta.get("tree") if isinstance(meta.get("tree"), dict) else {}
                req = JvmAnalyzeRequest(
                    system_name=str(meta.get("system_name") or "").strip(),
                    pod_name=(str(meta.get("pod_name") or "").strip() or None),
                    container_name=(str(meta.get("container_name") or "").strip() or None),
                    selected_problems=[],
                    tree=_parse_jvm_tree(tree_raw),
                    threshold_profile=str(meta.get("threshold_profile") or "normal"),
                    jdk_version=_opt_int(meta.get("jdk_version")),
                    spring_boot_version=(str(meta.get("spring_boot_version") or "").strip() or None),
                    confluence_title=(str(meta.get("confluence_title") or "").strip() or None),
                    heap_used_mib=_opt_int(meta.get("heap_used_mib")),
                    heap_committed_mib=_opt_int(meta.get("heap_committed_mib")),
                    old_gen_used_mib=_opt_int(meta.get("old_gen_used_mib")),
                    old_gen_capacity_mib=_opt_int(meta.get("old_gen_capacity_mib")),
                    gc_pause_p95_ms=_opt_float(meta.get("gc_pause_p95_ms")),
                    gc_pause_p99_ms=_opt_float(meta.get("gc_pause_p99_ms")),
                    gc_time_ratio_percent=_opt_float(meta.get("gc_time_ratio_percent")),
                    container_memory_usage_percent=_opt_float(meta.get("container_memory_usage_percent")),
                    heap_used_percent=_opt_float(meta.get("heap_used_percent")),
                    old_gen_used_percent=_opt_float(meta.get("old_gen_used_percent")),
                    new_gen_used_mib=_opt_int(meta.get("new_gen_used_mib")),
                    new_gen_capacity_mib=_opt_int(meta.get("new_gen_capacity_mib")),
                    new_gen_used_percent=_opt_float(meta.get("new_gen_used_percent")),
                    container_memory_working_set_mib=_opt_int(meta.get("container_memory_working_set_mib")),
                )
                if not req.system_name:
                    _json_response(self, 400, {"error": "выберите систему АС (system_name)"})
                    return
                if not req.container_name:
                    _json_response(self, 400, {"error": "выберите контейнер"})
                    return
                result = run_jvm_analysis(req, upload_paths, out)
                if result.error:
                    _json_response(self, 400, {"error": result.error, "exit_code": result.exit_code})
                    return
                meta_out = {
                    "session_id": session_id,
                    "mode": "jvm",
                    "scenario": "jvm",
                    "exit_code": result.exit_code,
                    "wiki": result.wiki_path.name if result.wiki_path else None,
                    "prompt": result.prompt_path.name if result.prompt_path else None,
                    "brief": result.brief_path.name if result.brief_path else None,
                    "summary": result.summary,
                }
                (sdir / "meta.json").write_text(
                    json.dumps(meta_out, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                wiki_text = result.wiki_path.read_text(encoding="utf-8") if result.wiki_path else ""
                prompt_text = result.prompt_path.read_text(encoding="utf-8") if result.prompt_path else ""
                brief_text = result.brief_path.read_text(encoding="utf-8") if result.brief_path else ""
                quality_view = _quality_view(result.output_dir)
                summary = result.summary or {}
                findings_ui = summary.pop("findings_ui", None) or []
                meta_out["summary"] = summary
                _json_response(
                    self,
                    200,
                    {
                        **meta_out,
                        "wiki_text": wiki_text,
                        "prompt_text": prompt_text,
                        "brief_text": brief_text,
                        "quality_text": quality_view.get("quality_text") or "",
                        "findings_ui": findings_ui,
                    },
                )
                return
            if not reports_meta:
                # auto-build from files as NT
                reports_meta = [
                    {
                        "filename": fname,
                        "env": "NT",
                        "label": suggest_label(fname, "NT", i),
                        "order": i,
                    }
                    for i, (_, fname, _) in enumerate(files)
                    if _.startswith("file") or True
                ]

            # Match files field "file" (multiple) with reports by order
            file_blobs = [(fname, data) for name, fname, data in files if name == "file"]
            if not file_blobs:
                file_blobs = [(fname, data) for _, fname, data in files]

            if len(file_blobs) != len(reports_meta):
                _json_response(
                    self,
                    400,
                    {
                        "error": (
                            f"число файлов ({len(file_blobs)}) не совпадает "
                            f"с meta.reports ({len(reports_meta)})"
                        )
                    },
                )
                return

            session_id = str(uuid.uuid4())
            sdir = SESSIONS_ROOT / session_id
            uploads = sdir / "uploads"
            out = sdir / "out"
            uploads.mkdir(parents=True, exist_ok=True)

            upload_paths: list[Path] = []
            report_objs: list[ReportMeta] = []
            for i, ((fname, data), rm) in enumerate(zip(file_blobs, reports_meta)):
                env = str(rm.get("env") or "NT").upper()
                if env not in ("NT", "PROD"):
                    env = "NT"
                label = str(rm.get("label") or suggest_label(fname, env, i))
                order = int(rm.get("order", i))
                safe_name = Path(fname).name
                if not safe_name.lower().endswith(".html"):
                    safe_name += ".html"
                dest = uploads / f"{i:02d}_{safe_name}"
                dest.write_bytes(data)
                upload_paths.append(dest)
                report_objs.append(
                    ReportMeta(filename=safe_name, env=env, label=label, order=order)
                )

            if not scenario or scenario == "auto":
                scenario = suggest_scenario(report_objs, symptoms)

            req = AnalyzeRequest(
                scenario=scenario,
                reports=report_objs,
                symptoms=symptoms,
                query_hex=meta.get("query_hex") or None,
                query_id=meta.get("query_id") or None,
                query_text=meta.get("query_text") or None,
                confluence_title=meta.get("confluence_title") or None,
            )
            result = run_analysis(req, upload_paths, out)
            # Opportunistic cleanup of other old sessions (cheap if nothing expired).
            try:
                cleanup_old_sessions()
            except Exception:
                pass
            if result.error:
                _json_response(self, 400, {"error": result.error, "exit_code": result.exit_code})
                # keep session for debugging
                return

            meta_out = {
                "session_id": session_id,
                "scenario": scenario,
                "exit_code": result.exit_code,
                "wiki": result.wiki_path.name if result.wiki_path else None,
                "prompt": result.prompt_path.name if result.prompt_path else None,
                "brief": result.brief_path.name if result.brief_path else None,
                "summary": result.summary,
            }
            (sdir / "meta.json").write_text(
                json.dumps(meta_out, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            wiki_text = result.wiki_path.read_text(encoding="utf-8") if result.wiki_path else ""
            prompt_text = (
                result.prompt_path.read_text(encoding="utf-8") if result.prompt_path else ""
            )
            brief_text = result.brief_path.read_text(encoding="utf-8") if result.brief_path else ""
            quality_view = _quality_view(result.output_dir)
            summary = result.summary or {}
            findings_ui = summary.pop("findings_ui", None) or []
            meta_out["summary"] = summary
            _json_response(
                self,
                200,
                {
                    **meta_out,
                    "wiki_text": wiki_text,
                    "prompt_text": prompt_text,
                    "brief_text": brief_text,
                    "quality_text": quality_view.get("quality_text") or "",
                    "findings_ui": findings_ui,
                },
            )
        except Exception as exc:
            traceback.print_exc()
            _json_response(self, 500, {"error": str(exc)})

    def _handle_create_jvm_system(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            fields, files = _parse_multipart(ctype, body)
            name = str((fields.get("system_name") or [""])[0]).strip()
            blobs = [
                (fname, data)
                for field, fname, data in files
                if field in ("jvm_file", "file")
            ]
            if not blobs:
                _json_response(self, 400, {"error": "нет файлов resources / jvm-config"})
                return
            tmp = Path(tempfile.mkdtemp(prefix="jvm-new-system-"))
            upload_paths: list[Path] = []
            try:
                for i, (fname, data) in enumerate(blobs):
                    dest = tmp / f"{i:02d}_{Path(fname).name}"
                    dest.write_bytes(data)
                    upload_paths.append(dest)
                created = create_jvm_system(name, upload_paths)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            _json_response(self, 200, created)
        except ValueError as exc:
            _json_response(self, 400, {"error": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            _json_response(self, 500, {"error": str(exc)})

    def _handle_llm_start(self, session_id: str) -> None:
        try:
            sdir = _session_dir(session_id)
        except ValueError:
            _json_response(self, 400, {"error": "invalid session id"})
            return
        out = sdir / "out"
        if not out.is_dir() or not (sdir / "meta.json").is_file():
            _json_response(self, 404, {"error": "session not found"})
            return
        try:
            payload = _read_json_body(self)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            _json_response(self, 400, {"error": f"invalid JSON: {exc}"})
            return
        task = str(payload.get("task") or "summary").strip()
        provider_name = str(payload.get("provider") or "").strip() or LLM_PROVIDER_OVERRIDE
        extra = str(payload.get("extra_instructions") or "")
        try:
            job = start_llm_job(
                out,
                task=task,
                provider_name=provider_name,
                extra_instructions=extra,
            )
        except LLMJobConflict as exc:
            _json_response(self, 409, {"error": str(exc), **job_status(out)})
            return
        except LLMJobError as exc:
            _json_response(self, 400, {"error": str(exc)})
            return
        except Exception as exc:
            traceback.print_exc()
            _json_response(self, 500, {"error": str(exc)})
            return
        _json_response(self, 202, job)

    def _serve_static(self, rel: str) -> None:
        # prevent path traversal
        target = (WEB_ROOT / rel).resolve()
        if not str(target).startswith(str(WEB_ROOT.resolve())):
            _json_response(self, 403, {"error": "forbidden"})
            return
        if not target.is_file():
            _json_response(self, 404, {"error": "not found"})
            return
        ctype, _ = mimetypes.guess_type(str(target))
        if ctype is None:
            ctype = "application/octet-stream"
        if target.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        if target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        if target.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw.strip():
        return {}
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON object expected")
    return payload


def _parse_jvm_tree(raw: dict[str, Any]) -> JvmTreeAnswers:
    def _opt_choice(value: Any) -> str | None:
        if value in (None, ""):
            return None
        return str(value).strip() or None

    return JvmTreeAnswers(
        pods_per_shoulder=_opt_int(raw.get("pods_per_shoulder")),
        restart_kind=_opt_choice(raw.get("restart_kind")),
        memory_cause_closed=_opt_choice(raw.get("memory_cause_closed")),
        heap_growing=_opt_choice(raw.get("heap_growing")),
        heap_growth_percent=_opt_float(raw.get("heap_growth_percent")),
        heap_growth_hours=_opt_float(raw.get("heap_growth_hours")),
        growth_of=_opt_choice(raw.get("growth_of")),
        gc_ran_in_window=_opt_choice(raw.get("gc_ran_in_window")),
        heap_used_before_gc_mib=_opt_int(raw.get("heap_used_before_gc_mib")),
        heap_used_after_gc_mib=_opt_int(raw.get("heap_used_after_gc_mib")),
        oldgen_returned_after_gc=_opt_choice(raw.get("oldgen_returned_after_gc")),
        cpu_throttled=_opt_choice(raw.get("cpu_throttled")),
        cpu_pct_limits_shoulder_1=_opt_float(raw.get("cpu_pct_limits_shoulder_1")),
        cpu_pct_limits_shoulder_2=_opt_float(raw.get("cpu_pct_limits_shoulder_2")),
        user_latency_grew=_opt_choice(raw.get("user_latency_grew")),
        user_latency_p95_ms=_opt_float(raw.get("user_latency_p95_ms")),
        pauses_coincide_throttle=_opt_choice(raw.get("pauses_coincide_throttle")),
        post_gc_floor_rising=_opt_choice(raw.get("post_gc_floor_rising")),
        gc_cpu_spike_sla=_opt_choice(raw.get("gc_cpu_spike_sla")),
    )


def _opt_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _opt_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def main(argv: list[str] | None = None) -> int:
    global SESSION_TTL_SECONDS, CLEANUP_INTERVAL_SECONDS
    global LLM_CONFIG_PATH, LLM_PROVIDER_OVERRIDE, LLM_STATUS

    parser = argparse.ArgumentParser(description="pg_profile_checks standalone UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--session-ttl-hours",
        type=float,
        default=24.0,
        help="Delete sessions older than this many hours (default: 24; 0 = disable)",
    )
    parser.add_argument(
        "--cleanup-interval-hours",
        type=float,
        default=1.0,
        help="How often to scan for expired sessions while server runs (default: 1; 0 = only at start/analyze)",
    )
    parser.add_argument(
        "--llm-config",
        type=Path,
        default=None,
        help="llm_providers.yaml (default: repo llm_providers.yaml)",
    )
    parser.add_argument(
        "--llm-provider",
        default=None,
        help="Override default_provider from yaml for this process",
    )
    parser.add_argument(
        "--skip-llm-probe",
        action="store_true",
        help="Do not contact Qwen at startup (hides Headless Qwen and the Quality tab)",
    )
    args = parser.parse_args(argv)

    if not WEB_ROOT.is_dir():
        print(f"error: web root missing: {WEB_ROOT}", file=sys.stderr)
        return 2

    SESSION_TTL_SECONDS = max(0.0, float(args.session_ttl_hours)) * 3600.0
    CLEANUP_INTERVAL_SECONDS = max(0.0, float(args.cleanup_interval_hours)) * 3600.0

    SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)
    removed = cleanup_old_sessions()
    if SESSION_TTL_SECONDS <= 0:
        print("session cleanup: disabled (--session-ttl-hours 0)")
    else:
        print(
            f"session cleanup: TTL {args.session_ttl_hours:g}h "
            f"(removed {removed} on startup)"
        )
        if CLEANUP_INTERVAL_SECONDS > 0:
            _start_cleanup_thread(CLEANUP_INTERVAL_SECONDS)
            print(f"session cleanup: every {args.cleanup_interval_hours:g}h")

    LLM_CONFIG_PATH = args.llm_config
    LLM_PROVIDER_OVERRIDE = (args.llm_provider or "").strip() or None
    try:
        llm_config = load_llm_config(LLM_CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001 - startup must still serve PG analysis
        LLM_STATUS = {
            "available": False,
            "skipped": False,
            "failed": True,
            "provider": LLM_PROVIDER_OVERRIDE or "",
            "provider_type": "",
            "reason": str(exc),
            "model": "",
            "url": "",
            "latency_ms": None,
            "trace_id": "",
            "answer_preview": "",
        }
        print(f"llm: Qwen hidden ({exc})")
    else:
        ui_cfg = llm_config.get("ui") if isinstance(llm_config.get("ui"), dict) else {}
        probe_timeout = ui_cfg.get("probe_timeout_sec", 5)
        try:
            probe_timeout = float(probe_timeout)
        except (TypeError, ValueError):
            probe_timeout = 5.0
        skip_probe = bool(args.skip_llm_probe) or ui_cfg.get("probe_on_startup") is False
        status = probe_llm_connection(
            llm_config,
            provider_name=LLM_PROVIDER_OVERRIDE,
            skip=skip_probe,
            live_only=True,
            timeout_sec=probe_timeout,
        )
        LLM_STATUS = status.to_dict()
        if status.available:
            print(
                f"llm: Qwen ready ({status.provider} {status.url or status.model}, "
                f"{status.latency_ms} ms)"
            )
        else:
            print(f"llm: Qwen hidden ({status.reason})")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"pg_profile UI: http://{args.host}:{args.port}/")
    print(f"sessions: {SESSIONS_ROOT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutdown")
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
