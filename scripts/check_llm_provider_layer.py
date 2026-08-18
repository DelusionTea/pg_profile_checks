#!/usr/bin/env python3
"""Checks for the LLM provider layer and prompt bundle assembly.

Runs entirely offline: HTTP behaviour is exercised through an injected transport, so the
retry policy, normalized errors and payload shape are verified without a Qwen instance.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pgprofile_llm import (  # noqa: E402 - path setup must come first
    ChatCompletionsProvider,
    DryRunProvider,
    LLMAuthError,
    LLMConfigError,
    LLMRequest,
    LLMResponseError,
    LLMTimeoutError,
    LLMTransportError,
    QwenGatewayProvider,
    QwenLocalProvider,
    _TransportFailure,
    _TransportTimeout,
    build_provider,
    describe_providers,
    load_llm_config,
    probe_llm_connection,
)
from pgprofile_llm_tasks import (  # noqa: E402
    ALLOWED_OVERRIDES,
    LLMBundleError,
    build_prompt_bundle,
    write_llm_artifacts,
)

CASE_DIR = ROOT / "analysis_out_test" / "case_matrix" / "nt_runs_3nt_one_symptom"

results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> None:
    results.append((bool(condition), label))


def _chat_body(text: str) -> bytes:
    payload = {
        "model": "qwen-test",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    return json.dumps(payload).encode("utf-8")


def _provider(transport: Any, **kwargs: Any) -> ChatCompletionsProvider:
    options: dict[str, Any] = {
        "name": "test",
        "base_url": "http://127.0.0.1:9/v1",
        "model": "qwen-test",
        "retry_backoff_sec": 0.0,
        "transport": transport,
    }
    options.update(kwargs)
    return ChatCompletionsProvider(**options)


def check_happy_path() -> None:
    seen: dict[str, Any] = {}

    def transport(url, headers, body, timeout):
        seen["url"] = url
        seen["headers"] = headers
        seen["body"] = json.loads(body.decode("utf-8"))
        seen["timeout"] = timeout
        return 200, _chat_body("готовый ответ")

    provider = _provider(transport, timeout_sec=42.0)
    response = provider.generate(LLMRequest(prompt="данные", system="роль", task="summary"))

    check(response.text == "готовый ответ", "chat provider returns the answer text")
    check(response.attempts == 1, "successful call does not retry")
    check(bool(response.trace_id), "response carries a trace id")
    check(response.finish_reason == "stop", "finish_reason is propagated")
    check(response.usage.get("prompt_tokens") == 10, "usage is propagated")
    check(seen["url"] == "http://127.0.0.1:9/v1/chat/completions", "endpoint url is composed")
    check(seen["timeout"] == 42.0, "configured timeout reaches the transport")
    check(seen["headers"].get("X-Trace-Id") == response.trace_id, "trace id is sent as a header")
    messages = seen["body"].get("messages") or []
    check(
        len(messages) == 2 and messages[0]["role"] == "system" and messages[1]["role"] == "user",
        "chat payload carries system and user messages",
    )
    check(seen["body"].get("stream") is False, "streaming is disabled")


def check_prompt_style_and_response_path() -> None:
    seen: dict[str, Any] = {}

    def transport(url, headers, body, timeout):
        seen["body"] = json.loads(body.decode("utf-8"))
        return 200, json.dumps({"result": {"text": "ответ шлюза"}}).encode("utf-8")

    provider = _provider(transport, request_style="prompt", response_path="result.text")
    response = provider.generate(LLMRequest(prompt="данные", system="роль"))

    check("prompt" in seen["body"], "prompt style sends a flat prompt field")
    check("messages" not in seen["body"], "prompt style does not send messages")
    check(response.text == "ответ шлюза", "custom response_path is honoured")


def check_retries() -> None:
    calls: list[int] = []

    def flaky(url, headers, body, timeout):
        calls.append(1)
        if len(calls) < 3:
            return 503, b"service unavailable"
        return 200, _chat_body("ответ после ретраев")

    provider = _provider(flaky, max_retries=2)
    response = provider.generate(LLMRequest(prompt="данные"))
    check(response.attempts == 3, "retryable status is retried up to max_retries")
    check(response.text == "ответ после ретраев", "retry eventually returns the answer")

    def always_500(url, headers, body, timeout):
        return 500, b"boom"

    provider = _provider(always_500, max_retries=1)
    try:
        provider.generate(LLMRequest(prompt="данные"))
        check(False, "exhausted retries raise LLMTransportError")
    except LLMTransportError as exc:
        check("HTTP 500" in str(exc), "exhausted retries raise LLMTransportError")
        check(bool(exc.trace_id), "transport error carries a trace id")

    def bad_request(url, headers, body, timeout):
        return 400, b"bad request"

    attempts: list[int] = []

    def counting_bad_request(url, headers, body, timeout):
        attempts.append(1)
        return bad_request(url, headers, body, timeout)

    provider = _provider(counting_bad_request, max_retries=3)
    try:
        provider.generate(LLMRequest(prompt="данные"))
        check(False, "non-retryable 4xx fails immediately")
    except LLMTransportError:
        check(len(attempts) == 1, "non-retryable 4xx fails immediately")


def check_error_normalization() -> None:
    def unauthorized(url, headers, body, timeout):
        return 401, b"no token"

    try:
        _provider(unauthorized, max_retries=3).generate(LLMRequest(prompt="x"))
        check(False, "401 raises LLMAuthError without retrying")
    except LLMAuthError:
        check(True, "401 raises LLMAuthError without retrying")

    def timing_out(url, headers, body, timeout):
        raise _TransportTimeout("too slow")

    try:
        _provider(timing_out, max_retries=0).generate(LLMRequest(prompt="x"))
        check(False, "transport timeout maps to LLMTimeoutError")
    except LLMTimeoutError:
        check(True, "transport timeout maps to LLMTimeoutError")

    def refused(url, headers, body, timeout):
        raise _TransportFailure("connection refused")

    try:
        _provider(refused, max_retries=0).generate(LLMRequest(prompt="x"))
        check(False, "connection failure maps to LLMTransportError")
    except LLMTransportError:
        check(True, "connection failure maps to LLMTransportError")

    def garbage(url, headers, body, timeout):
        return 200, b"<html>not json</html>"

    try:
        _provider(garbage).generate(LLMRequest(prompt="x"))
        check(False, "non-JSON body maps to LLMResponseError")
    except LLMResponseError:
        check(True, "non-JSON body maps to LLMResponseError")

    def empty_text(url, headers, body, timeout):
        return 200, json.dumps({"choices": [{"message": {"content": "  "}}]}).encode("utf-8")

    try:
        _provider(empty_text).generate(LLMRequest(prompt="x"))
        check(False, "empty answer maps to LLMResponseError")
    except LLMResponseError:
        check(True, "empty answer maps to LLMResponseError")


def check_factory() -> None:
    config = load_llm_config()
    provider = build_provider(config, provider_name="dry_run")
    check(isinstance(provider, DryRunProvider), "factory builds the dry-run provider")

    provider = build_provider(config, provider_name="qwen_local")
    check(isinstance(provider, QwenLocalProvider), "factory builds the local provider")
    check(
        provider.describe().get("authenticated") is False,
        "local provider needs no credentials",
    )

    os.environ["PGPROFILE_LLM_TOKEN"] = "check-token"
    try:
        gateway = build_provider(config, provider_name="qwen_gateway")
    finally:
        os.environ.pop("PGPROFILE_LLM_TOKEN", None)
    check(isinstance(gateway, QwenGatewayProvider), "factory builds the gateway provider")
    check(
        gateway.describe().get("authenticated") is True,
        "gateway provider picks the token up from the environment",
    )

    try:
        build_provider(
            {
                "default_provider": "g",
                "providers": {"g": {"type": "qwen_gateway", "base_url": "https://x/v1", "model": "m"}},
            }
        )
        check(False, "gateway config without token_env is rejected")
    except LLMConfigError as exc:
        check("auth.token_env" in str(exc), "gateway config without token_env is rejected")

    try:
        build_provider(config, provider_name="does_not_exist")
        check(False, "unknown provider raises LLMConfigError")
    except LLMConfigError as exc:
        check("available" in str(exc), "unknown provider raises LLMConfigError with a hint")

    try:
        # Token env is intentionally not set in this process.
        build_provider(config, provider_name="qwen_gateway")
        check(False, "gateway without a token raises LLMConfigError")
    except LLMConfigError as exc:
        check("PGPROFILE_LLM_TOKEN" in str(exc), "gateway without a token names the env variable")

    described = {row["provider"]: row for row in describe_providers(config)}
    check(
        described.get("dry_run", {}).get("is_default") is True,
        "describe_providers marks the default provider",
    )
    check(
        described.get("qwen_gateway", {}).get("token_present") is False,
        "describe_providers reports a missing token without raising",
    )

    try:
        build_provider({"default_provider": "x", "providers": {"x": {"type": "nope"}}})
        check(False, "unsupported provider type raises LLMConfigError")
    except LLMConfigError:
        check(True, "unsupported provider type raises LLMConfigError")


def check_bundle() -> None:
    bundle = build_prompt_bundle(CASE_DIR, task="summary")
    check(bundle.sources[0].endswith("brief.md"), "bundle starts from the brief artifact")
    check(
        any("influence_summary" in source for source in bundle.sources),
        "bundle includes the influence summary",
    )
    check("# DATA:" in bundle.prompt, "bundle has a DATA section")
    check(bool(bundle.trace_id), "bundle has a trace id")
    check("PROBABLE" in bundle.system, "system rules explain PROBABLE handling")
    check(
        (bundle.metadata.get("policy") or {}).get("name") == "none",
        "default policy recorded on the bundle is none",
    )
    check(
        (bundle.metadata.get("policy") or {}).get("unchanged") is True,
        "default policy leaves the payload unchanged",
    )

    tuning = build_prompt_bundle(CASE_DIR, task="tuning")
    check(
        len(tuning.sources) >= len(bundle.sources),
        "tuning preset collects at least as many sources as summary",
    )

    with_extra = build_prompt_bundle(
        CASE_DIR, task="summary", overrides={"extra_instructions": "Смотри только на WAL"}
    )
    check("Смотри только на WAL" in with_extra.prompt, "manual override reaches the prompt")

    try:
        build_prompt_bundle(CASE_DIR, task="summary", overrides={"model": "other"})
        check(False, "unknown override is rejected")
    except LLMBundleError as exc:
        check("unsupported overrides" in str(exc), "unknown override is rejected")

    try:
        build_prompt_bundle(CASE_DIR, task="summary", overrides={"temperature": 5})
        check(False, "out-of-range temperature is rejected")
    except LLMBundleError:
        check(True, "out-of-range temperature is rejected")

    try:
        build_prompt_bundle(CASE_DIR, task="unknown_task")
        check(False, "unknown task is rejected")
    except LLMBundleError:
        check(True, "unknown task is rejected")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            build_prompt_bundle(Path(tmp), task="summary")
            check(False, "directory without a brief is rejected")
        except LLMBundleError as exc:
            check("no brief artifact" in str(exc), "directory without a brief is rejected")

    trimmed = build_prompt_bundle(CASE_DIR, task="detailed_rca", max_chars=4000)
    check(trimmed.char_count() <= 4000, "bundle respects the character budget")
    check(
        bool(trimmed.metadata.get("trimmed_sections")),
        "trimming is recorded in bundle metadata",
    )
    check(
        "блок сокращён" in trimmed.prompt,
        "trimmed sections are marked inside the prompt",
    )
    check(ALLOWED_OVERRIDES == {"extra_instructions", "temperature", "max_tokens"},
          "override allowlist stays narrow")


def check_real_transport() -> None:
    """Exercise the urllib transport itself: the injected transport bypasses it."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if self.path.endswith("/slow"):
                time.sleep(1.5)
            body = _chat_body("ответ локального сервера")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        provider = ChatCompletionsProvider(
            name="local_http",
            base_url=f"http://{host}:{port}/v1",
            model="qwen-test",
            timeout_sec=5.0,
            max_retries=0,
        )
        response = provider.generate(LLMRequest(prompt="данные"))
        check(
            response.text == "ответ локального сервера",
            "urllib transport talks to a real HTTP endpoint",
        )

        slow = ChatCompletionsProvider(
            name="local_http_slow",
            base_url=f"http://{host}:{port}/v1",
            model="qwen-test",
            path="/slow",
            timeout_sec=0.3,
            max_retries=0,
        )
        try:
            slow.generate(LLMRequest(prompt="данные"))
            check(False, "urllib transport turns a real timeout into LLMTimeoutError")
        except LLMTimeoutError:
            check(True, "urllib transport turns a real timeout into LLMTimeoutError")
    finally:
        server.shutdown()
        server.server_close()


def check_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        bundle = build_prompt_bundle(CASE_DIR, task="summary")
        provider = DryRunProvider()
        response = provider.generate(bundle.to_request())
        written = write_llm_artifacts(
            out, bundle, response=response, provider_info=provider.describe()
        )
        names = sorted(path.name for path in written)
        check(
            names == ["llm_request_summary.json", "llm_response_summary.json", "llm_summary.md"],
            "successful run writes request, response and answer",
        )
        request_payload = json.loads((out / "llm_request_summary.json").read_text(encoding="utf-8"))
        check(
            request_payload.get("trace_id") == bundle.trace_id,
            "request artifact keeps the bundle trace id",
        )
        check(
            (request_payload.get("policy") or {}).get("name") == "none",
            "request artifact records policy none",
        )
        response_payload = json.loads(
            (out / "llm_response_summary.json").read_text(encoding="utf-8")
        )
        check(response_payload.get("status") == "success", "response artifact records success")
        check(
            response_payload.get("trace_id") == response.trace_id,
            "response artifact keeps the provider trace id",
        )

        failure = LLMTimeoutError("timed out", provider="qwen_local", trace_id=bundle.trace_id)
        write_llm_artifacts(out, bundle, error=failure, provider_info=provider.describe())
        failed = json.loads((out / "llm_response_summary.json").read_text(encoding="utf-8"))
        check(failed.get("status") == "failed", "failed run records the failure")
        check(failed.get("error") == "LLMTimeoutError", "failed run records the error type")


def check_cli_preflight() -> None:
    """`run_llm.py --check-connection` must separate setup problems from prompt problems."""
    import contextlib
    import io as _io

    import run_llm

    out = _io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run_llm.main(["--check-connection"])
    text = out.getvalue()
    check(code == 0, "preflight succeeds against the dry-run provider")
    check("CONNECTION_OK" in text, "preflight prints CONNECTION_OK on success")
    check("trace_id" in text, "preflight prints a trace id")

    err = _io.StringIO()
    os.environ["PGPROFILE_LLM_BASE_URL"] = "http://127.0.0.1:1/v1"
    try:
        with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(err):
            code = run_llm.main(["--check-connection", "--provider", "qwen_local"])
    finally:
        os.environ.pop("PGPROFILE_LLM_BASE_URL", None)
    check(code == 1, "preflight fails when the endpoint is unreachable")
    check(
        "CONNECTION_FAILED" in err.getvalue(),
        "preflight reports the failure on stderr",
    )


def check_ui_live_probe() -> None:
    """UI must not treat dry_run as a live Qwen connection."""
    config = load_llm_config()
    skipped = probe_llm_connection(config, live_only=True)
    check(skipped.available is False, "UI probe does not mark dry_run as available")
    check(skipped.skipped is True, "UI probe skips dry_run instead of calling a model")
    forced_skip = probe_llm_connection(config, skip=True, live_only=True)
    check(forced_skip.skipped is True, "explicit skip leaves Qwen unavailable")
    check(forced_skip.available is False, "explicit skip is not available")


def main() -> int:
    if not CASE_DIR.is_dir():
        print(f"Missing analysis fixture: {CASE_DIR}")
        print("Run scripts/check_report_cases.py first to produce it.")
        return 1

    check_happy_path()
    check_prompt_style_and_response_path()
    check_retries()
    check_error_normalization()
    check_factory()
    check_real_transport()
    check_bundle()
    check_artifacts()
    check_cli_preflight()
    check_ui_live_probe()

    for ok, label in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")

    failed = [label for ok, label in results if not ok]
    if failed:
        print(f"\nLLM_PROVIDER_CHECKS_FAILED ({len(failed)} of {len(results)})")
        return 1
    print(f"\nLLM_PROVIDER_CHECKS_PASSED ({len(results)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
