"""Strict OpenAI-compatible chat-completions action adapter."""

import json
import os
from dataclasses import dataclass
from typing import Any, Literal, Protocol

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


class AsyncPostClient(Protocol):
    async def post(self, *args: object, **kwargs: object) -> Any:
        """Send one HTTP request."""
        ...


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

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be HTTP or HTTPS")
        if not self.model or not self.api_key_env:
            raise ValueError("model and api_key_env are required")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("provider timeouts must be positive")


class OpenAICompatiblePolicy:
    """Request and strictly validate one action from a chat-completions API."""

    name = "openai-compatible"

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        client: AsyncPostClient | None = None,
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
        self._record(run, "provider.request", body)
        headers = {"content-type": "application/json"}
        api_key = os.environ.get(self._config.api_key_env)
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        timeout = httpx.Timeout(
            self._config.read_timeout_seconds,
            connect=self._config.connect_timeout_seconds,
        )
        try:
            if self._client is not None:
                response = await self._client.post(
                    self._url, json=body, headers=headers, timeout=timeout
                )
            else:
                async with httpx.AsyncClient(transport=self._transport) as client:
                    response = await client.post(
                        self._url, json=body, headers=headers, timeout=timeout
                    )
        except httpx.TimeoutException as error:
            self._failed(
                run,
                "provider_timeout",
                {"exception_type": type(error).__name__},
            )
            raise ProviderTimeoutError("provider request timed out") from error
        except httpx.RequestError as error:
            self._failed(
                run,
                "provider_transport",
                {"exception_type": type(error).__name__},
            )
            raise ProviderTransportError("provider transport failed") from error

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            self._failed(
                run,
                "provider_http_status",
                {
                    "status_code": response.status_code,
                    "exception_type": type(error).__name__,
                },
            )
            raise ProviderHTTPStatusError(
                f"provider returned HTTP {response.status_code}"
            ) from error

        try:
            raw_payload = response.json()
            envelope = _CompletionResponse.model_validate(raw_payload)
            action = self._parse_action(envelope)
        except (ValueError, ValidationError, json.JSONDecodeError) as error:
            self._failed(
                run,
                "provider_protocol",
                {"exception_type": type(error).__name__},
            )
            raise ProviderProtocolError(
                "provider did not return one valid structured action"
            ) from error
        self._record(run, "provider.response", envelope.model_dump(mode="json"))
        return action

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

    def _failed(self, run: Run, code: str, details: dict[str, JsonValue]) -> None:
        self._record(run, "provider.failed", {"code": code, **details}, status="error")

    def _record(
        self,
        run: Run,
        event_type: str,
        payload: JsonValue,
        *,
        status: str = "ok",
    ) -> None:
        if self._recorder is not None:
            self._recorder.record(
                run.trace_id,
                event_type,
                payload,
                parent_span_id=run.trace_id,
                status=status,
            )


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
    "ProviderTimeoutError",
    "ProviderTransportError",
]
