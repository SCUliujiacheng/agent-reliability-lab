"""Strict OpenAI-compatible chat-completions action adapter."""

import asyncio
import ipaddress
import json
import math
import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from agent_reliability_lab.domain.actions import AgentAction, CallToolAction
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.domain.scenarios import Scenario
from agent_reliability_lab.telemetry.recorder import TraceRecorder

_ACTION_ADAPTER: TypeAdapter[AgentAction] = TypeAdapter(AgentAction)
_DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class ProviderError(RuntimeError):
    """Base error exposed by the provider policy boundary."""


class ProviderProtocolError(ProviderError):
    """Raised when a successful response is not one structured action."""


class ProviderTimeoutError(ProviderError):
    """Raised when the configured provider deadline expires."""


class ProviderTransportError(ProviderError):
    """Raised when no HTTP response can be obtained."""


class ProviderHTTPStatusError(ProviderError):
    """Raised when the provider returns a non-success HTTP status."""


class ProviderResponseTooLargeError(ProviderProtocolError):
    """Raised when a provider response exceeds the configured byte ceiling."""


class _Function(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    arguments: str


class _ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    type: Literal["function"]
    function: _Function


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["assistant"]
    content: str | None = None
    refusal: str | None = None
    tool_calls: tuple[_ToolCall, ...] | None = Field(
        default=None, min_length=1, max_length=1
    )

    @model_validator(mode="after")
    def exactly_one_action_shape(self) -> "_Message":
        has_content = self.content is not None
        has_call = self.tool_calls is not None
        if has_content == has_call:
            raise ValueError("exactly one of content or one function call is required")
        return self


class _Choice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    message: _Message
    finish_reason: Literal["stop", "tool_calls"]
    logprobs: JsonValue | None = None

    @model_validator(mode="after")
    def finish_reason_matches_message(self) -> "_Choice":
        expected = "tool_calls" if self.message.tool_calls is not None else "stop"
        if self.finish_reason != expected:
            raise ValueError("finish_reason does not match message action shape")
        return self


class _Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    prompt_tokens_details: JsonValue | None = None
    completion_tokens_details: JsonValue | None = None


class _CompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    object: Literal["chat.completion"]
    created: int = Field(ge=0)
    model: str = Field(min_length=1)
    choices: tuple[_Choice, ...] = Field(min_length=1, max_length=1)
    usage: _Usage
    system_fingerprint: str | None = None
    service_tier: str | None = None


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    """Connection and credential lookup settings for one provider."""

    base_url: str
    model: str
    api_key_env: str
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 45.0
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("base_url must be HTTP or HTTPS")
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise ValueError("remote provider base_url must use HTTPS")
        if not self.model or not self.api_key_env:
            raise ValueError("model and api_key_env are required")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("provider timeouts must be positive")
        if (
            not math.isfinite(self.total_timeout_seconds)
            or self.total_timeout_seconds <= 0
        ):
            raise ValueError("total_timeout_seconds must be positive and finite")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not 1 <= self.max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ValueError(
                f"max_response_bytes must be between 1 and {_MAX_RESPONSE_BYTES}"
            )


class OpenAICompatiblePolicy:
    """Request and strictly validate one action from a chat-completions API."""

    name = "openai-compatible"

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        recorder: TraceRecorder | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("provide client or transport, not both")
        self._config = config
        self._client = client
        self._transport = transport
        self._recorder = recorder

    async def next_action(self, run: Run, scenario: Scenario) -> AgentAction:
        body = self._request_body(run, scenario)
        api_key = os.environ.get(self._config.api_key_env)
        provider_secrets = {api_key} if api_key else set()
        self._record(
            run,
            "provider.request",
            body,
            extra_secret_values=provider_secrets,
        )
        headers = {
            "content-type": "application/json",
            "accept-encoding": "identity",
        }
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        timeout = httpx.Timeout(
            self._config.read_timeout_seconds,
            connect=self._config.connect_timeout_seconds,
        )
        try:
            async with asyncio.timeout(self._config.total_timeout_seconds):
                response_body = await self._read_response(body, headers, timeout)
        except (TimeoutError, httpx.TimeoutException) as error:
            self._failed(
                run,
                "provider_timeout",
                {"exception_type": type(error).__name__},
                extra_secret_values=provider_secrets,
            )
            raise ProviderTimeoutError("provider request timed out") from error
        except httpx.RequestError as error:
            self._failed(
                run,
                "provider_transport",
                {"exception_type": type(error).__name__},
                extra_secret_values=provider_secrets,
            )
            raise ProviderTransportError("provider transport failed") from error
        except ProviderResponseTooLargeError:
            self._failed(
                run,
                "provider_response_too_large",
                {"limit_bytes": self._config.max_response_bytes},
                extra_secret_values=provider_secrets,
            )
            raise
        except ProviderProtocolError as error:
            self._failed(
                run,
                "provider_protocol",
                {"exception_type": type(error).__name__},
                extra_secret_values=provider_secrets,
            )
            raise
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            self._failed(
                run,
                "provider_http_status",
                {
                    "status_code": status_code,
                    "exception_type": type(error).__name__,
                },
                extra_secret_values=provider_secrets,
            )
            raise ProviderHTTPStatusError(
                f"provider returned HTTP {status_code}"
            ) from error

        try:
            raw_payload = json.loads(response_body)
            envelope = _CompletionResponse.model_validate(raw_payload)
            action = self._parse_action(envelope)
            if _contains_secret(action.model_dump(mode="json"), provider_secrets):
                raise ValueError("provider action contains the active credential")
        except (ValueError, ValidationError, json.JSONDecodeError) as error:
            self._failed(
                run,
                "provider_protocol",
                {"exception_type": type(error).__name__},
                extra_secret_values=provider_secrets,
            )
            raise ProviderProtocolError(
                "provider did not return one valid structured action"
            ) from error
        self._record(
            run,
            "provider.response",
            envelope.model_dump(mode="json"),
            extra_secret_values=provider_secrets,
        )
        return action

    async def _read_response(
        self,
        body: dict[str, JsonValue],
        headers: dict[str, str],
        timeout: httpx.Timeout,
    ) -> bytes:
        if self._client is not None:
            return await self._stream_response(self._client, body, headers, timeout)
        async with httpx.AsyncClient(transport=self._transport) as client:
            return await self._stream_response(client, body, headers, timeout)

    async def _stream_response(
        self,
        client: httpx.AsyncClient,
        body: dict[str, JsonValue],
        headers: dict[str, str],
        timeout: httpx.Timeout,
    ) -> bytes:
        async with client.stream(
            "POST",
            self._url,
            json=body,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            return await _read_bounded_stream(response, self._config.max_response_bytes)

    @property
    def _url(self) -> str:
        return f"{self._config.base_url.rstrip('/')}/chat/completions"

    def _request_body(self, run: Run, scenario: Scenario) -> dict[str, JsonValue]:
        return {
            "model": self._config.model,
            "messages": [
                {
                    "role": "system",
                    "content": "Return exactly one AgentAction JSON object.",
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "scenario_id": scenario.id,
                            "current_step": run.current_step,
                            "context": run.context,
                        },
                        sort_keys=True,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_action",
                    "strict": True,
                    "schema": _ACTION_ADAPTER.json_schema(),
                },
            },
            "tools": _scenario_tools(scenario),
        }

    def _parse_action(self, envelope: _CompletionResponse) -> AgentAction:
        message = envelope.choices[0].message
        try:
            if message.tool_calls is not None:
                function = message.tool_calls[0].function
                candidate: object = {
                    "type": "call_tool",
                    "tool_name": function.name,
                    "arguments": json.loads(function.arguments),
                }
            else:
                candidate = json.loads(message.content or "")
            return _ACTION_ADAPTER.validate_python(candidate)
        except (TypeError, json.JSONDecodeError, ValidationError) as error:
            raise ValueError("invalid structured action") from error

    def _failed(
        self,
        run: Run,
        code: str,
        details: dict[str, JsonValue],
        *,
        extra_secret_values: set[str],
    ) -> None:
        self._record(
            run,
            "provider.failed",
            {"code": code, **details},
            status="error",
            extra_secret_values=extra_secret_values,
        )

    def _record(
        self,
        run: Run,
        event_type: str,
        payload: JsonValue,
        *,
        status: str = "ok",
        extra_secret_values: set[str] | None = None,
    ) -> None:
        if self._recorder is not None:
            self._recorder.record(
                run.trace_id,
                event_type,
                payload,
                parent_span_id=run.trace_id,
                status=status,
                extra_secret_values=extra_secret_values,
            )


def _contains_secret(value: JsonValue, secret_values: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            _contains_secret(str(key), secret_values)
            or _contains_secret(item, secret_values)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret(item, secret_values) for item in value)
    if isinstance(value, str):
        return any(secret and secret in value for secret in secret_values)
    return False


async def _read_bounded_stream(response: httpx.Response, max_bytes: int) -> bytes:
    content_encoding = response.headers.get("content-encoding")
    if (
        content_encoding is not None
        and content_encoding.strip().casefold() != "identity"
    ):
        raise ProviderProtocolError(
            "provider response content encoding must be identity"
        )

    declared_length = response.headers.get("content-length")
    if declared_length is not None:
        try:
            if int(declared_length) > max_bytes:
                raise ProviderResponseTooLargeError(
                    "provider response exceeded configured byte limit"
                )
        except ValueError:
            pass

    content = bytearray()
    async for chunk in response.aiter_bytes():
        if len(content) + len(chunk) > max_bytes:
            raise ProviderResponseTooLargeError(
                "provider response exceeded configured byte limit"
            )
        content.extend(chunk)
    return bytes(content)


def _is_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _scenario_tools(scenario: Scenario) -> list[JsonValue]:
    tools: list[JsonValue] = []
    seen: set[str] = set()
    for action in scenario.actions:
        if not isinstance(action, CallToolAction) or action.tool_name in seen:
            continue
        seen.add(action.tool_name)
        properties: dict[str, JsonValue] = {
            name: {"type": _json_type(value)}
            for name, value in action.arguments.items()
        }
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": action.tool_name,
                    "description": f"Tool available in scenario {scenario.id}",
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": list(action.arguments),
                        "additionalProperties": False,
                    },
                },
            }
        )
    return tools


def _json_type(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


__all__ = [
    "OpenAICompatibleConfig",
    "OpenAICompatiblePolicy",
    "ProviderError",
    "ProviderHTTPStatusError",
    "ProviderProtocolError",
    "ProviderResponseTooLargeError",
    "ProviderTimeoutError",
    "ProviderTransportError",
]
