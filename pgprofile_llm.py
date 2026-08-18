"""LLM provider layer: one interface for headless Qwen, local or behind a gateway.

The rest of the codebase only sees `LLMRequest` -> `LLMResponse` and the normalized
`LLMError` family, so swapping a local instance for the bank gateway is a config change.
Only the standard library is used: the target environment cannot install HTTP clients.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


DEFAULT_LLM_CONFIG = Path(__file__).resolve().parent / "llm_providers.yaml"

DRY_RUN_PROVIDER = "dry_run"

# Env overrides win over the YAML file: tokens must never be committed to it.
ENV_PROVIDER = "PGPROFILE_LLM_PROVIDER"
ENV_BASE_URL = "PGPROFILE_LLM_BASE_URL"
ENV_MODEL = "PGPROFILE_LLM_MODEL"
ENV_TIMEOUT = "PGPROFILE_LLM_TIMEOUT"

RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """Base class for every provider failure, so callers never see urllib internals."""

    def __init__(self, message: str, *, provider: str = "", trace_id: str = "") -> None:
        super().__init__(message)
        self.provider = provider
        self.trace_id = trace_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": str(self),
            "provider": self.provider,
            "trace_id": self.trace_id,
        }


class LLMConfigError(LLMError):
    """Provider cannot be built: unknown name, missing endpoint or missing token."""


class LLMAuthError(LLMError):
    """Endpoint rejected the credentials (401/403)."""


class LLMTimeoutError(LLMError):
    """Endpoint did not answer within the configured timeout."""


class LLMTransportError(LLMError):
    """Network or HTTP-level failure after retries were exhausted."""


class LLMResponseError(LLMError):
    """Endpoint answered, but the payload is not usable."""


@dataclass
class LLMRequest:
    prompt: str
    system: str = ""
    task: str = "summary"
    temperature: float | None = None
    max_tokens: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def char_count(self) -> int:
        return len(self.system) + len(self.prompt)


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    trace_id: str
    latency_ms: int
    attempts: int
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "trace_id": self.trace_id,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "usage": self.usage,
            "finish_reason": self.finish_reason,
            "text": self.text,
        }


# A transport returns (status, body); injecting one keeps retry/error handling testable.
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def _urllib_transport(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:  # HTTP status is the useful part here
        return int(exc.code), exc.read()
    except TimeoutError as exc:
        raise _TransportTimeout(str(exc) or "request timed out") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            raise _TransportTimeout(str(reason) or "request timed out") from exc
        raise _TransportFailure(str(reason)) from exc


class _TransportTimeout(Exception):
    """Internal marker so transports can signal a timeout without importing LLMError."""


class _TransportFailure(Exception):
    """Internal marker for connection-level failures."""


class LLMProvider:
    """Interface every provider implements."""

    name: str = ""
    model: str = ""

    def generate(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model}


class DryRunProvider(LLMProvider):
    """Answers locally without any network call.

    Exists so the whole pipeline (bundle -> request -> artifacts) can be exercised on a
    laptop and in CI where no Qwen instance is reachable.
    """

    def __init__(self, *, name: str = DRY_RUN_PROVIDER, model: str = "dry-run") -> None:
        self.name = name
        self.model = model

    def generate(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        payload = {
            "verdict": "need-validation",
            "summary": (
                f"dry-run: модель не вызывалась. Задача {request.task}, "
                f"промпт {request.char_count()} символов."
            ),
            "claims": [],
            "recommendations": [],
            "risks": ["ответ сформирован провайдером dry-run без модели"],
            "missing_data": ["реальный ответ Qwen"],
        }
        return LLMResponse(
            text=json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            provider=self.name,
            model=self.model,
            trace_id=str(request.metadata.get("trace_id") or new_trace_id()),
            latency_ms=int((time.monotonic() - started) * 1000),
            attempts=1,
            usage={"prompt_chars": request.char_count()},
            finish_reason="dry_run",
        )


class ChatCompletionsProvider(LLMProvider):
    """OpenAI-compatible chat provider: covers vLLM, llama.cpp and most internal gateways.

    `request_style` and `response_path` exist because internal gateways keep the chat
    contract but rename the envelope; both are configuration, not new code.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        model: str,
        path: str = "/chat/completions",
        timeout_sec: float = 120.0,
        max_retries: int = 2,
        retry_backoff_sec: float = 0.5,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        headers: dict[str, str] | None = None,
        auth: dict[str, Any] | None = None,
        request_style: str = "chat",
        response_path: str = "choices.0.message.content",
        transport: Transport | None = None,
    ) -> None:
        if not base_url:
            raise LLMConfigError(f"provider {name}: base_url is required", provider=name)
        if not model:
            raise LLMConfigError(f"provider {name}: model is required", provider=name)
        if request_style not in {"chat", "prompt"}:
            raise LLMConfigError(
                f"provider {name}: request_style must be chat or prompt", provider=name
            )

        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.path = path if path.startswith("/") else f"/{path}"
        self.timeout_sec = float(timeout_sec)
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_sec = float(retry_backoff_sec)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.extra_headers = dict(headers or {})
        self.request_style = request_style
        self.response_path = response_path
        self._transport = transport or _urllib_transport
        self._auth_header, self._auth_value = self._resolve_auth(auth or {})

    def _resolve_auth(self, auth: dict[str, Any]) -> tuple[str, str]:
        token_env = str(auth.get("token_env") or "").strip()
        if not token_env:
            return "", ""
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise LLMConfigError(
                f"provider {self.name}: token is expected in env {token_env}, but it is empty",
                provider=self.name,
            )
        header = str(auth.get("header") or "Authorization")
        scheme = str(auth.get("scheme") or "Bearer").strip()
        return header, f"{scheme} {token}".strip()

    @property
    def url(self) -> str:
        return f"{self.base_url}{self.path}"

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model,
            "url": self.url,
            "timeout_sec": self.timeout_sec,
            "max_retries": self.max_retries,
            "authenticated": bool(self._auth_value),
        }

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        temperature = self.temperature if request.temperature is None else request.temperature
        max_tokens = self.max_tokens if request.max_tokens is None else request.max_tokens
        if self.request_style == "prompt":
            text = f"{request.system}\n\n{request.prompt}" if request.system else request.prompt
            return {
                "model": self.model,
                "prompt": text,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        return {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self.extra_headers)
        if self._auth_header and self._auth_value:
            headers[self._auth_header] = self._auth_value
        return headers

    def generate(self, request: LLMRequest) -> LLMResponse:
        trace_id = str(request.metadata.get("trace_id") or new_trace_id())
        body = json.dumps(self._payload(request), ensure_ascii=False).encode("utf-8")
        headers = self._headers()
        headers["X-Trace-Id"] = trace_id

        started = time.monotonic()
        attempts = 0
        last_error: LLMError | None = None

        while attempts <= self.max_retries:
            attempts += 1
            try:
                status, raw = self._transport(self.url, headers, body, self.timeout_sec)
            except _TransportTimeout as exc:
                last_error = LLMTimeoutError(
                    f"provider {self.name}: timeout after {self.timeout_sec:g}s ({exc})",
                    provider=self.name,
                    trace_id=trace_id,
                )
            except _TransportFailure as exc:
                last_error = LLMTransportError(
                    f"provider {self.name}: connection failed ({exc})",
                    provider=self.name,
                    trace_id=trace_id,
                )
            else:
                if status in {401, 403}:
                    # Credentials will not fix themselves; retrying only hides the cause.
                    raise LLMAuthError(
                        f"provider {self.name}: endpoint rejected credentials (HTTP {status})",
                        provider=self.name,
                        trace_id=trace_id,
                    )
                if status in RETRYABLE_STATUSES:
                    last_error = LLMTransportError(
                        f"provider {self.name}: HTTP {status} from {self.url}"
                        f" ({_short_body(raw)})",
                        provider=self.name,
                        trace_id=trace_id,
                    )
                elif status >= 400:
                    raise LLMTransportError(
                        f"provider {self.name}: HTTP {status} from {self.url}"
                        f" ({_short_body(raw)})",
                        provider=self.name,
                        trace_id=trace_id,
                    )
                else:
                    return self._parse_response(
                        raw,
                        trace_id=trace_id,
                        attempts=attempts,
                        latency_ms=int((time.monotonic() - started) * 1000),
                    )

            if attempts <= self.max_retries:
                time.sleep(self.retry_backoff_sec * (2 ** (attempts - 1)))

        raise last_error or LLMTransportError(
            f"provider {self.name}: request failed", provider=self.name, trace_id=trace_id
        )

    def _parse_response(
        self, raw: bytes, *, trace_id: str, attempts: int, latency_ms: int
    ) -> LLMResponse:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMResponseError(
                f"provider {self.name}: response is not JSON ({exc})",
                provider=self.name,
                trace_id=trace_id,
            ) from exc

        text = _dig(payload, self.response_path)
        if not isinstance(text, str) or not text.strip():
            raise LLMResponseError(
                f"provider {self.name}: no text at response_path {self.response_path!r}",
                provider=self.name,
                trace_id=trace_id,
            )

        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        finish_reason = _dig(payload, "choices.0.finish_reason")
        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(payload.get("model") or self.model),
            trace_id=trace_id,
            latency_ms=latency_ms,
            attempts=attempts,
            usage=dict(usage or {}),
            finish_reason=str(finish_reason or ""),
        )


class QwenLocalProvider(ChatCompletionsProvider):
    """Headless Qwen on the same host (vLLM, llama.cpp, LM Studio): no credentials needed."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("base_url", "http://127.0.0.1:8000/v1")
        super().__init__(**kwargs)


class QwenGatewayProvider(ChatCompletionsProvider):
    """Qwen behind the internal gateway: a token env variable is mandatory."""

    def __init__(self, **kwargs: Any) -> None:
        auth = kwargs.get("auth") or {}
        if not str(auth.get("token_env") or "").strip():
            raise LLMConfigError(
                f"provider {kwargs.get('name')}: gateway requires auth.token_env"
                " pointing at an env variable with the token",
                provider=str(kwargs.get("name") or ""),
            )
        super().__init__(**kwargs)


_PROVIDER_TYPES: dict[str, type[ChatCompletionsProvider]] = {
    "qwen_local": QwenLocalProvider,
    "qwen_gateway": QwenGatewayProvider,
}


def _short_body(raw: bytes, limit: int = 200) -> str:
    try:
        text = raw.decode("utf-8", errors="replace").strip().replace("\n", " ")
    except Exception:  # noqa: BLE001 - diagnostics must never mask the original failure
        return "<unreadable body>"
    return text[:limit] if text else "<empty body>"


def _dig(payload: Any, dotted_path: str) -> Any:
    """Read `choices.0.message.content`-style paths; gateways nest the text differently."""
    current = payload
    for part in dotted_path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def new_trace_id() -> str:
    return uuid.uuid4().hex


def load_llm_config(path: Path | None = None) -> dict[str, Any]:
    """Load provider config; a missing file means dry-run only, not a crash."""
    config_path = path or DEFAULT_LLM_CONFIG
    if not config_path.exists():
        if path is not None:
            raise LLMConfigError(f"LLM config not found: {config_path}")
        return {"default_provider": DRY_RUN_PROVIDER, "providers": {DRY_RUN_PROVIDER: {"type": DRY_RUN_PROVIDER}}}
    with config_path.open(encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise LLMConfigError(f"invalid LLM config: {config_path}")
    providers = config.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise LLMConfigError(f"LLM config has no providers: {config_path}")
    return config


def resolve_provider_name(config: dict[str, Any], requested: str | None = None) -> str:
    name = (requested or os.environ.get(ENV_PROVIDER) or config.get("default_provider") or "").strip()
    if not name:
        raise LLMConfigError("no provider requested and no default_provider in config")
    providers = config.get("providers") or {}
    if name not in providers:
        available = ", ".join(sorted(providers)) or "<none>"
        raise LLMConfigError(f"unknown provider {name!r}; available: {available}")
    return name


def build_provider(
    config: dict[str, Any] | None = None,
    *,
    provider_name: str | None = None,
    transport: Transport | None = None,
) -> LLMProvider:
    """Pick and construct a provider from config plus environment overrides."""
    config = config if config is not None else load_llm_config()
    name = resolve_provider_name(config, provider_name)
    settings = dict((config.get("providers") or {}).get(name) or {})
    provider_type = str(settings.pop("type", "") or "").strip()
    if not provider_type:
        raise LLMConfigError(f"provider {name}: type is required", provider=name)

    if provider_type == DRY_RUN_PROVIDER:
        return DryRunProvider(name=name, model=str(settings.get("model") or "dry-run"))

    provider_class = _PROVIDER_TYPES.get(provider_type)
    if provider_class is None:
        raise LLMConfigError(
            f"provider {name}: unsupported type {provider_type!r}"
            f" (expected {', '.join(sorted(_PROVIDER_TYPES))} or {DRY_RUN_PROVIDER})",
            provider=name,
        )

    base_url = os.environ.get(ENV_BASE_URL) or settings.get("base_url") or ""
    model = os.environ.get(ENV_MODEL) or settings.get("model") or ""
    timeout_sec = os.environ.get(ENV_TIMEOUT) or settings.get("timeout_sec") or 120
    try:
        timeout_sec = float(timeout_sec)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(f"provider {name}: timeout must be numeric", provider=name) from exc

    return provider_class(
        name=name,
        base_url=str(base_url),
        model=str(model),
        path=str(settings.get("path") or "/chat/completions"),
        timeout_sec=timeout_sec,
        max_retries=int(settings.get("max_retries", 2)),
        retry_backoff_sec=float(settings.get("retry_backoff_sec", 0.5)),
        temperature=float(settings.get("temperature", 0.2)),
        max_tokens=int(settings.get("max_tokens", 2048)),
        headers=settings.get("headers") or {},
        auth=settings.get("auth") or {},
        request_style=str(settings.get("request_style") or "chat"),
        response_path=str(settings.get("response_path") or "choices.0.message.content"),
        transport=transport,
    )


@dataclass
class LLMConnectionStatus:
    """Result of a startup/CLI probe: live Qwen vs skipped vs failed."""

    available: bool
    skipped: bool
    failed: bool
    provider: str
    provider_type: str
    reason: str
    model: str = ""
    url: str = ""
    latency_ms: int | None = None
    trace_id: str = ""
    answer_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "skipped": self.skipped,
            "failed": self.failed,
            "provider": self.provider,
            "provider_type": self.provider_type,
            "reason": self.reason,
            "model": self.model,
            "url": self.url,
            "latency_ms": self.latency_ms,
            "trace_id": self.trace_id,
            "answer_preview": self.answer_preview,
        }


def _provider_type_of(config: dict[str, Any], name: str) -> str:
    settings = (config.get("providers") or {}).get(name) or {}
    return str(settings.get("type") or "")


def probe_llm_connection(
    config: dict[str, Any] | None = None,
    *,
    provider_name: str | None = None,
    skip: bool = False,
    live_only: bool = False,
    timeout_sec: float | None = None,
) -> LLMConnectionStatus:
    """One round-trip (or a deliberate skip) so UI/CLI can decide if Qwen is usable.

    live_only=True treats dry_run as skipped: the UI must not show Headless Qwen.
    CLI --check-connection keeps live_only=False so dry_run still prints CONNECTION_OK.
    """
    config = config if config is not None else load_llm_config()
    try:
        name = resolve_provider_name(config, provider_name)
    except LLMError as exc:
        return LLMConnectionStatus(
            available=False,
            skipped=False,
            failed=True,
            provider=str(provider_name or ""),
            provider_type="",
            reason=str(exc),
        )
    ptype = _provider_type_of(config, name)
    if skip:
        return LLMConnectionStatus(
            available=False,
            skipped=True,
            failed=False,
            provider=name,
            provider_type=ptype,
            reason="подключение к Qwen пропущено",
        )
    if live_only and ptype == DRY_RUN_PROVIDER:
        return LLMConnectionStatus(
            available=False,
            skipped=True,
            failed=False,
            provider=name,
            provider_type=ptype,
            reason="default_provider=dry_run — живой Qwen в конфиге не выбран",
        )
    try:
        provider = build_provider(config, provider_name=name)
    except LLMError as exc:
        return LLMConnectionStatus(
            available=False,
            skipped=False,
            failed=True,
            provider=name,
            provider_type=ptype,
            reason=str(exc),
        )
    if timeout_sec is not None and hasattr(provider, "timeout_sec"):
        provider.timeout_sec = min(float(provider.timeout_sec), float(timeout_sec))
        if hasattr(provider, "max_retries"):
            provider.max_retries = 0
    info = provider.describe() if hasattr(provider, "describe") else {}
    request = LLMRequest(
        prompt="Ответь одним словом: ok",
        system="Ты проверочный эндпоинт. Отвечай максимально коротко.",
        task="summary",
        max_tokens=16,
    )
    try:
        response = provider.generate(request)
    except LLMError as exc:
        return LLMConnectionStatus(
            available=False,
            skipped=False,
            failed=True,
            provider=name,
            provider_type=ptype,
            reason=f"{type(exc).__name__}: {exc}",
            model=str(info.get("model") or getattr(provider, "model", "") or ""),
            url=str(info.get("url") or ""),
            trace_id=str(getattr(exc, "trace_id", "") or ""),
        )
    preview = response.text.strip().splitlines()[0] if response.text.strip() else ""
    return LLMConnectionStatus(
        available=True,
        skipped=False,
        failed=False,
        provider=name,
        provider_type=ptype,
        reason="CONNECTION_OK",
        model=response.model,
        url=str(info.get("url") or ""),
        latency_ms=response.latency_ms,
        trace_id=response.trace_id,
        answer_preview=preview[:120],
    )


def describe_providers(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """List configured providers for UI/CLI, without building them (no token needed)."""
    config = config if config is not None else load_llm_config()
    default_name = str(config.get("default_provider") or "")
    rows: list[dict[str, Any]] = []
    for name, settings in sorted((config.get("providers") or {}).items()):
        settings = settings or {}
        auth = settings.get("auth") or {}
        token_env = str(auth.get("token_env") or "")
        rows.append(
            {
                "provider": name,
                "type": str(settings.get("type") or ""),
                "model": str(os.environ.get(ENV_MODEL) or settings.get("model") or ""),
                "base_url": str(os.environ.get(ENV_BASE_URL) or settings.get("base_url") or ""),
                "is_default": name == default_name,
                "token_env": token_env,
                "token_present": bool(token_env and os.environ.get(token_env, "").strip()),
            }
        )
    return rows
