#!/usr/bin/env python3
"""Minimal OpenAI-compatible stub for checking the qwen_local provider without a model.

Usage:
  python scripts/mock_qwen_server.py --port 8000
  PGPROFILE_LLM_PROVIDER=qwen_local python run_llm.py --output-dir analysis_out --task summary

Options let you reproduce failures on purpose: --fail-times 2 returns HTTP 503 first,
--delay-sec 5 triggers the client timeout.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    delay_sec: float = 0.0
    fail_times: int = 0
    failures_left: int = 0

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"data": [{"id": "qwen2.5-32b-instruct", "object": "model"}]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "body is not JSON"})
            return

        if Handler.failures_left > 0:
            Handler.failures_left -= 1
            self._json(503, {"error": "mock failure, retry expected"})
            return

        if Handler.delay_sec > 0:
            time.sleep(Handler.delay_sec)

        messages = payload.get("messages") or []
        user_text = ""
        for message in messages:
            if message.get("role") == "user":
                user_text = str(message.get("content") or "")
        if not user_text:
            user_text = str(payload.get("prompt") or "")

        trace_id = self.headers.get("X-Trace-Id") or "-"
        answer = "\n".join(
            [
                "h2. Ответ мок-сервера",
                "",
                f"Модель: {payload.get('model')}",
                f"trace_id: {trace_id}",
                f"Получено символов в промпте: {len(user_text)}",
                f"Первая строка задачи: {user_text.splitlines()[0] if user_text else '-'}",
                "",
                "Это заглушка для проверки транспорта, а не анализ отчёта.",
            ]
        )
        self._json(
            200,
            {
                "id": "mock-completion",
                "model": payload.get("model"),
                "choices": [{"message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(user_text) // 4, "completion_tokens": len(answer) // 4},
            },
        )

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[mock-qwen] {self.address_string()} {fmt % args}")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock OpenAI-compatible Qwen endpoint")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--delay-sec", type=float, default=0.0, help="Delay each answer to test client timeouts"
    )
    parser.add_argument(
        "--fail-times",
        type=int,
        default=0,
        help="Return HTTP 503 for the first N requests to test retries",
    )
    args = parser.parse_args()

    Handler.delay_sec = args.delay_sec
    Handler.fail_times = args.fail_times
    Handler.failures_left = args.fail_times

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mock Qwen on http://{args.host}:{args.port}/v1 (Ctrl+C to stop)")
    if args.fail_times:
        print(f"  first {args.fail_times} request(s) will fail with HTTP 503")
    if args.delay_sec:
        print(f"  every answer delayed by {args.delay_sec:g}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
