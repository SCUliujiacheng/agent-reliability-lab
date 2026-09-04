"""OpenAI-compatible structured-action policy tests."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.actions import CallToolAction
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.domain.scenarios import Scenario
from agent_reliability_lab.providers.openai_compatible import (
    OpenAICompatibleConfig,
    OpenAICompatiblePolicy,
    ProviderError,
    ProviderProtocolError,
    ProviderTimeoutError,
)
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder


def config() -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig(
        base_url="https://provider.example/v1",
        model="reliable-model",
        api_key_env="TEST_PROVIDER_KEY",
        connect_timeout_seconds=0.25,
        read_timeout_seconds=1.5,
    )


def scenario() -> Scenario:
    return Scenario(
        id="provider-scenario",
        version=1,
        actions=(CallToolAction(tool_name="get_deployment"),),
        expected_outcome="done",
    )


def json_transport(payload: object, *, status_code: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _: httpx.Response(status_code, json=payload))


def full_response(message: dict[str, object]) -> dict[str, object]:
    return {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "created": 1788500000,
        "model": "reliable-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", **message},
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 12,
            "total_tokens": 32,
        },
    }


@pytest.mark.asyncio
async def test_provider_rejects_plain_text_instead_of_a_structured_action() -> None:
    transport = json_transport(full_response({"content": "just do it"}))
    async with httpx.AsyncClient(transport=transport) as client:
        policy = OpenAICompatiblePolicy(config(), client=client)
        with pytest.raises(ProviderProtocolError, match="structured action"):
            await policy.next_action(Run.new("scenario", "resilient"), scenario())


@pytest.mark.asyncio
async def test_provider_parses_complete_response_into_discriminated_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "top-secret")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json=full_response(
                {
                    "content": json.dumps(
                        {
                            "type": "call_tool",
                            "tool_name": "get_deployment",
                            "arguments": {},
                        }
                    )
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        action = await OpenAICompatiblePolicy(config(), client=client).next_action(
            Run.new("scenario", "resilient"), scenario()
        )

    assert action == CallToolAction(tool_name="get_deployment")
    assert captured[0].url == "https://provider.example/v1/chat/completions"
    assert captured[0].headers["authorization"] == "Bearer top-secret"
    request_body = json.loads(captured[0].content)
    assert request_body["model"] == "reliable-model"
    assert request_body["response_format"]["type"] == "json_schema"
    assert request_body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_deployment",
                "description": "Tool available in scenario provider-scenario",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_provider_accepts_known_optional_chat_completion_fields() -> None:
    response = full_response(
        {"content": '{"type":"finish","summary":"done","outcome":"done"}'}
    )
    response["system_fingerprint"] = None
    response["service_tier"] = "default"
    response["choices"][0]["logprobs"] = None
    response["choices"][0]["message"]["refusal"] = None
    response["usage"]["prompt_tokens_details"] = {"cached_tokens": 0}
    response["usage"]["completion_tokens_details"] = {"reasoning_tokens": 0}

    async with httpx.AsyncClient(transport=json_transport(response)) as client:
        action = await OpenAICompatiblePolicy(config(), client=client).next_action(
            Run.new("scenario", "resilient"), scenario()
        )

    assert action.outcome == "done"


@pytest.mark.asyncio
async def test_provider_parses_standard_function_tool_call() -> None:
    response = full_response(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "get_deployment",
                        "arguments": "{}",
                    },
                }
            ],
        }
    )
    async with httpx.AsyncClient(transport=json_transport(response)) as client:
        action = await OpenAICompatiblePolicy(config(), client=client).next_action(
            Run.new("scenario", "resilient"), scenario()
        )

    assert action == CallToolAction(tool_name="get_deployment")


@pytest.mark.asyncio
async def test_provider_accepts_response_at_exact_configured_byte_limit() -> None:
    """Changing the inclusive byte ceiling into an off-by-one rejection must fail."""
    payload = full_response(
        {"content": '{"type":"finish","summary":"done","outcome":"done"}'}
    )
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    bounded = replace(config(), max_response_bytes=len(encoded))
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=encoded))

    async with httpx.AsyncClient(transport=transport) as client:
        action = await OpenAICompatiblePolicy(bounded, client=client).next_action(
            Run.new("scenario", "resilient"), scenario()
        )

    assert action.outcome == "done"


@pytest.mark.asyncio
async def test_provider_stops_streaming_after_response_limit_and_traces_stable_error(
    tmp_path: Path,
) -> None:
    """Buffering or consuming an oversized provider body must fail this test."""

    class CountingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.chunks_read = 0

        async def __aiter__(self):  # type: ignore[no-untyped-def]
            for chunk in (b"12345", b"6", b"must-not-be-read"):
                self.chunks_read += 1
                yield chunk

    body = CountingStream()
    transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
    bounded = replace(config(), max_response_bytes=5)
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=tmp_path, database_path=tmp_path / "oversized.db")
    )
    store.create_schema()
    run = store.save_run(Run.new("scenario", "resilient"))

    async with httpx.AsyncClient(transport=transport) as client:
        policy = OpenAICompatiblePolicy(
            bounded, client=client, recorder=TraceRecorder(store)
        )
        with pytest.raises(ProviderError, match="configured byte limit") as caught:
            await policy.next_action(run, scenario())

    assert body.chunks_read == 2
    assert type(caught.value).__name__ == "ProviderResponseTooLargeError"
    failed = [
        event
        for event in store.list_events(run.trace_id)
        if event.event_type == "provider.failed"
    ]
    assert failed[0].payload == {
        "code": "provider_response_too_large",
        "limit_bytes": 5,
    }


@pytest.mark.parametrize("limit", [0, 16 * 1024 * 1024 + 1, True])
def test_provider_rejects_invalid_response_byte_limits(limit: object) -> None:
    """Invalid byte ceilings must fail before any provider request is possible."""
    with pytest.raises(ValueError, match="max_response_bytes"):
        replace(config(), max_response_bytes=limit)


def test_provider_allows_loopback_http_but_rejects_remote_plaintext() -> None:
    """Bearer credentials must never be configured for a remote plaintext hop."""
    for base_url in (
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000/v1",
    ):
        assert replace(config(), base_url=base_url).base_url == base_url

    with pytest.raises(ValueError, match="HTTPS"):
        replace(config(), base_url="http://provider.example/v1")


@pytest.mark.asyncio
async def test_provider_total_deadline_covers_streaming_body() -> None:
    """Read inactivity timeouts alone must not permit an endless response stream."""

    class NeverEndingStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield b"{"
            await asyncio.Event().wait()

    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, stream=NeverEndingStream())
    )
    bounded = replace(config(), total_timeout_seconds=0.01)

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderTimeoutError, match="timed out"):
            await OpenAICompatiblePolicy(bounded, client=client).next_action(
                Run.new("scenario", "resilient"), scenario()
            )


@pytest.mark.parametrize("deadline", [0, float("inf")])
def test_provider_rejects_invalid_total_deadline(deadline: float) -> None:
    """A non-positive or unbounded total deadline would disable the safety cap."""
    with pytest.raises(ValueError, match="total_timeout_seconds"):
        replace(config(), total_timeout_seconds=deadline)


@pytest.mark.asyncio
async def test_provider_timeout_is_typed_and_uses_explicit_timeout_values() -> None:
    observed: list[dict[str, object]] = []

    class TimeoutClient:
        async def post(self, *args: object, **kwargs: object) -> object:
            observed.append(kwargs)
            raise httpx.ReadTimeout("late")

    policy = OpenAICompatiblePolicy(config(), client=TimeoutClient())
    with pytest.raises(ProviderTimeoutError, match="timed out"):
        await policy.next_action(Run.new("scenario", "resilient"), scenario())

    timeout = observed[0]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 0.25
    assert timeout.read == 1.5


@pytest.mark.asyncio
async def test_provider_traces_redact_credentials_and_secret_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "top-secret")
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=tmp_path, database_path=tmp_path / "provider.db"),
        secret_values={"top-secret"},
    )
    store.create_schema()
    run = store.save_run(
        Run.new("scenario", "resilient").model_copy(
            update={"context": {"note": "contains top-secret"}}
        )
    )
    response = full_response(
        {
            "content": json.dumps(
                {"type": "finish", "summary": "top-secret", "outcome": "done"}
            )
        }
    )
    async with httpx.AsyncClient(transport=json_transport(response)) as client:
        policy = OpenAICompatiblePolicy(
            config(), client=client, recorder=TraceRecorder(store, {"top-secret"})
        )
        await policy.next_action(run, scenario())

    serialized = json.dumps(
        [event.model_dump(mode="json") for event in store.list_events(run.trace_id)]
    )
    assert "top-secret" not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.asyncio
async def test_provider_automatically_redacts_its_api_key_from_success_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider reflecting its own credential must never persist that value."""
    api_key = "provider-reflected-success-secret"
    monkeypatch.setenv("TEST_PROVIDER_KEY", api_key)
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=tmp_path, database_path=tmp_path / "reflected-success.db")
    )
    store.create_schema()
    run = store.save_run(
        Run.new("scenario", "resilient").model_copy(
            update={"context": {"note": f"provider echoed {api_key}"}}
        )
    )
    response = full_response(
        {
            "content": json.dumps(
                {"type": "finish", "summary": api_key, "outcome": "done"}
            )
        }
    )

    async with httpx.AsyncClient(transport=json_transport(response)) as client:
        action = await OpenAICompatiblePolicy(
            config(), client=client, recorder=TraceRecorder(store)
        ).next_action(run, scenario())

    assert action.outcome == "done"
    serialized = json.dumps(
        [event.model_dump(mode="json") for event in store.list_events(run.trace_id)]
    )
    assert api_key not in serialized
    assert "[REDACTED]" in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: {**value, "unexpected": True},
        lambda value: {**value, "choices": value["choices"] * 2},
        lambda value: {
            **value,
            "choices": [
                {
                    **value["choices"][0],
                    "message": {
                        **value["choices"][0]["message"],
                        "role": "user",
                    },
                }
            ],
        },
        lambda value: {
            **value,
            "choices": [
                {
                    **value["choices"][0],
                    "message": {
                        "role": "assistant",
                        "content": '{"type":"finish","summary":"x","outcome":"x"}',
                        "tool_calls": [
                            {
                                "id": "call",
                                "type": "function",
                                "function": {
                                    "name": "get_deployment",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                }
            ],
        },
        lambda value: {
            **value,
            "choices": [
                {
                    **value["choices"][0],
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call",
                                "type": "not_function",
                                "function": {
                                    "name": "get_deployment",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                }
            ],
        },
        lambda value: {
            **value,
            "choices": [{**value["choices"][0], "finish_reason": "tool_calls"}],
        },
    ],
)
async def test_provider_rejects_malformed_response_envelopes(mutate: object) -> None:
    """Permissive envelope parsing must not accept ambiguous provider output."""
    base = full_response({"content": '{"type":"finish","summary":"x","outcome":"x"}'})
    payload = mutate(base)
    async with httpx.AsyncClient(transport=json_transport(payload)) as client:
        with pytest.raises(ProviderProtocolError):
            await OpenAICompatiblePolicy(config(), client=client).next_action(
                Run.new("scenario", "resilient"), scenario()
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_error_name", "expected_code"),
    [
        ("transport", "ProviderTransportError", "provider_transport"),
        ("status", "ProviderHTTPStatusError", "provider_http_status"),
        ("protocol", "ProviderProtocolError", "provider_protocol"),
    ],
)
async def test_provider_failure_categories_are_traced_and_redacted(
    tmp_path: Path,
    kind: str,
    expected_error_name: str,
    expected_code: str,
) -> None:
    """Collapsing failures or persisting their secrets would defeat diagnosis."""
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=tmp_path, database_path=tmp_path / f"{kind}.db"),
        secret_values={"failure-secret"},
    )
    store.create_schema()
    run = store.save_run(Run.new("scenario", "resilient"))
    if kind == "transport":
        transport = httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(
                httpx.ConnectError("failure-secret", request=request)
            )
        )
    elif kind == "status":
        transport = json_transport(
            {"error": {"message": "failure-secret"}}, status_code=503
        )
    else:
        transport = json_transport(full_response({"content": "failure-secret"}))

    async with httpx.AsyncClient(transport=transport) as client:
        policy = OpenAICompatiblePolicy(
            config(),
            client=client,
            recorder=TraceRecorder(store, {"failure-secret"}),
        )
        with pytest.raises(ProviderError) as caught:
            await policy.next_action(run, scenario())

    assert type(caught.value).__name__ == expected_error_name

    failed = [
        event
        for event in store.list_events(run.trace_id)
        if event.event_type == "provider.failed"
    ]
    assert len(failed) == 1
    assert failed[0].payload["code"] == expected_code
    assert "failure-secret" not in json.dumps(failed[0].payload)


@pytest.mark.asyncio
async def test_provider_failure_never_persists_environment_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default tracing must be safe even when an error body reflects credentials."""
    api_key = "environment-only-provider-key"
    monkeypatch.setenv("TEST_PROVIDER_KEY", api_key)
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=tmp_path, database_path=tmp_path / "default-recorder.db")
    )
    store.create_schema()
    run = store.save_run(Run.new("scenario", "resilient"))
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            503,
            text=f"authorization: Bearer {api_key}",
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        policy = OpenAICompatiblePolicy(
            config(), client=client, recorder=TraceRecorder(store)
        )
        with pytest.raises(ProviderError):
            await policy.next_action(run, scenario())

    events = store.list_events(run.trace_id)
    serialized = json.dumps(
        [event.model_dump(mode="json") for event in events], sort_keys=True
    )
    assert api_key not in serialized
    assert "authorization" not in serialized.lower()
    assert "bearer" not in serialized.lower()
    failed = [event for event in events if event.event_type == "provider.failed"]
    assert failed[0].payload == {
        "code": "provider_http_status",
        "status_code": 503,
        "exception_type": "HTTPStatusError",
    }
