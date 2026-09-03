"""Strict OpenAI-compatible chat-completions action adapter."""

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

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


class AsyncPostClient(Protocol):
    async def post(self, *args: object, **kwargs: object) -> Any:
        """Send one HTTP request."""
        ...


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
            self._record(
                run,
                "provider.failed",
                {"code": "provider_timeout"},
                status="error",
            )
            raise ProviderTimeoutError("provider request timed out") from error
        except httpx.HTTPError as error:
            raise ProviderError("provider request failed") from error

        try:
            response.raise_for_status()
            payload = cast(JsonValue, response.json())
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderProtocolError(
                "provider returned an invalid response"
            ) from error
        self._record(run, "provider.response", payload)
        return self._parse_action(payload)

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

    def _parse_action(self, payload: JsonValue) -> AgentAction:
        try:
            choices = cast(dict[str, Any], payload)["choices"]
            message = choices[0]["message"]
            tool_calls = message.get("tool_calls")
            if tool_calls:
                if len(tool_calls) != 1:
                    raise ProviderProtocolError(
                        "provider must return exactly one structured action"
                    )
                function = tool_calls[0]["function"]
                arguments = json.loads(function["arguments"])
                candidate: object = {
                    "type": "call_tool",
                    "tool_name": function["name"],
                    "arguments": arguments,
                }
            else:
                content = message.get("content")
                if not isinstance(content, str):
                    raise ProviderProtocolError(
                        "provider did not return a structured action"
                    )
                candidate = json.loads(content)
            return _ACTION_ADAPTER.validate_python(candidate)
        except ProviderProtocolError:
            raise
        except (
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
            ValidationError,
        ) as error:
            raise ProviderProtocolError(
                "provider did not return a valid structured action"
            ) from error

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
    """Expose bounded function schemas for tools present in the frozen scenario."""
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
    "ProviderProtocolError",
    "ProviderTimeoutError",
]
