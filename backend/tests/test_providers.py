from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.app.providers import (
    AgentRole,
    AuthenticationProviderError,
    DeepSeekChatProvider,
    IndeterminateProviderError,
    LLMRequest,
    OpenAIResponsesProvider,
    RetryableProviderError,
    TruncatedOutputError,
)


def test_provider_base_urls_can_be_configured_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_OPENAI_BASE_URL", "https://openai-compatible.example/v1/")
    monkeypatch.setenv("NOVEL_DEEPSEEK_BASE_URL", "https://deepseek-compatible.example/v1/")

    assert OpenAIResponsesProvider(api_key="test").base_url == "https://openai-compatible.example/v1"
    assert DeepSeekChatProvider(api_key="test").base_url == "https://deepseek-compatible.example/v1"


def test_explicit_provider_base_url_wins_over_environment(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_DEEPSEEK_BASE_URL", "https://environment.example/v1")

    provider = DeepSeekChatProvider(api_key="test", base_url="https://explicit.example/v1/")

    assert provider.base_url == "https://explicit.example/v1"


def _request() -> LLMRequest:
    return LLMRequest(role=AgentRole.WRITER, system_prompt="system", user_prompt="user")


def _sse(*events: tuple[str | None, object]) -> bytes:
    lines: list[str] = []
    for event_name, data in events:
        if event_name:
            lines.append(f"event: {event_name}")
        value = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        lines.extend((f"data: {value}", ""))
    return ("\n".join(lines) + "\n").encode()


class _BrokenSSEStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield _sse(
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "已收到的正文"},
            )
        )
        raise httpx.ReadError("stream disconnected")


def test_openai_incomplete_nonempty_text_is_not_success() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "resp_1",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output_text": "只有前半段。",
                    "usage": {"input_tokens": 5, "output_tokens": 10},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIResponsesProvider(api_key="test", client=client)
            with pytest.raises(TruncatedOutputError, match="未完成"):
                await provider.complete("test-model", _request())

    asyncio.run(scenario())


def test_deepseek_length_finish_is_not_success() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "chat_1",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "未完正文"},
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 10},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekChatProvider(api_key="test", client=client)
            with pytest.raises(TruncatedOutputError, match="截断"):
                await provider.complete("test-model", _request())

    asyncio.run(scenario())


def test_retry_after_429_then_success_and_401_is_not_retried() -> None:
    async def scenario() -> None:
        calls = 0

        async def transient_handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(
                    429,
                    request=request,
                    headers={"retry-after": "0"},
                    json={"error": {"message": "rate limited"}},
                )
            return httpx.Response(
                200,
                request=request,
                json={
                    "id": "resp_2",
                    "status": "completed",
                    "output_text": "完成。",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(transient_handler)) as client:
            provider = OpenAIResponsesProvider(api_key="test", client=client, max_attempts=3)
            response = await provider.complete("test-model", _request())
            assert response.attempts == 2
            assert calls == 2

        auth_calls = 0

        async def auth_handler(request: httpx.Request) -> httpx.Response:
            nonlocal auth_calls
            auth_calls += 1
            return httpx.Response(
                401,
                request=request,
                json={"error": {"message": "invalid key"}},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(auth_handler)) as client:
            provider = OpenAIResponsesProvider(api_key="bad", client=client, max_attempts=3)
            with pytest.raises(AuthenticationProviderError):
                await provider.complete("test-model", _request())
            assert auth_calls == 1

    asyncio.run(scenario())


def test_openai_responses_sse_yields_text_and_terminal_telemetry() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["stream"] is True
            return httpx.Response(
                200,
                request=request,
                headers={"x-request-id": "req_stream_openai"},
                content=_sse(
                    (
                        "response.output_text.delta",
                        {"type": "response.output_text.delta", "delta": "第一段"},
                    ),
                    (
                        "response.output_text.delta",
                        {"type": "response.output_text.delta", "delta": "，第二段。"},
                    ),
                    (
                        "response.completed",
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp_stream",
                                "status": "completed",
                                "output": [
                                    {
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "第一段，第二段。",
                                            }
                                        ]
                                    }
                                ],
                                "usage": {
                                    "input_tokens": 12,
                                    "output_tokens": 8,
                                    "total_tokens": 20,
                                },
                            },
                        },
                    ),
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIResponsesProvider(api_key="test", client=client)
            events = [event async for event in provider.stream_events("test-model", _request())]

        assert [event.delta for event in events if event.delta] == ["第一段", "，第二段。"]
        response = next(event.response for event in events if event.response is not None)
        assert response.text == "第一段，第二段。"
        assert response.request_id == "req_stream_openai"
        assert response.finish_status == "completed"
        assert response.usage.total_tokens == 20

    asyncio.run(scenario())


def test_deepseek_chat_sse_yields_text_finish_and_usage() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            assert payload["stream"] is True
            assert payload["stream_options"] == {"include_usage": True}
            return httpx.Response(
                200,
                request=request,
                content=_sse(
                    (
                        None,
                        {
                            "id": "chat_stream",
                            "choices": [{"delta": {"content": "雨落"}, "finish_reason": None}],
                        },
                    ),
                    (
                        None,
                        {
                            "id": "chat_stream",
                            "choices": [{"delta": {"content": "长街。"}, "finish_reason": None}],
                        },
                    ),
                    (
                        None,
                        {
                            "id": "chat_stream",
                            "choices": [{"delta": {}, "finish_reason": "stop"}],
                            "usage": {
                                "prompt_tokens": 7,
                                "completion_tokens": 5,
                                "total_tokens": 12,
                            },
                        },
                    ),
                    (None, "[DONE]"),
                ),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DeepSeekChatProvider(api_key="test", client=client)
            events = [event async for event in provider.stream_events("test-model", _request())]

        assert [event.delta for event in events if event.delta] == ["雨落", "长街。"]
        response = next(event.response for event in events if event.response is not None)
        assert response.text == "雨落长街。"
        assert response.request_id == "chat_stream"
        assert response.finish_status == "stop"
        assert response.usage.total_tokens == 12

    asyncio.run(scenario())


def test_transport_uncertainty_is_never_replayed() -> None:
    async def scenario() -> None:
        calls = 0

        async def timeout_handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("timed out after POST", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
            provider = OpenAIResponsesProvider(api_key="test", client=client, max_attempts=3)
            with pytest.raises(IndeterminateProviderError):
                await provider.complete("test-model", _request())
        assert calls == 1

    asyncio.run(scenario())


def test_sse_interruption_after_delta_is_indeterminate_and_not_replayed() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, request=request, stream=_BrokenSSEStream())

        parts: list[str] = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIResponsesProvider(api_key="test", client=client, max_attempts=3)
            with pytest.raises(IndeterminateProviderError):
                async for part in provider.stream_text("test-model", _request()):
                    parts.append(part)
        assert parts == ["已收到的正文"]
        assert calls == 1

    asyncio.run(scenario())


def test_http_transient_retry_envelope_is_at_most_three_calls() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                503,
                request=request,
                headers={"retry-after": "0"},
                json={"error": {"message": "temporarily unavailable"}},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIResponsesProvider(api_key="test", client=client, max_attempts=3)
            with pytest.raises(RetryableProviderError):
                await provider.complete("test-model", _request())
        assert calls == 3

    asyncio.run(scenario())
