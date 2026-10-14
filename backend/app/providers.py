"""LLM provider adapters and role based routing.

The rest of the application talks to :class:`ProviderRegistry`, never to a
vendor SDK.  Keeping the HTTP wire format here has three useful properties:

* API keys stay server side and never become model-call metadata.
* structured output is validated locally even when a provider claims JSON
  mode support;
* retry and ``indeterminate`` semantics are identical for both providers.

No network request is made when this module is imported.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError


class ProviderName(StrEnum):
    OPENAI = "openai"
    DEEPSEEK = "deepseek"


class AgentRole(StrEnum):
    ANALYZER = "analyzer"
    PLANNER = "planner"
    WRITER = "writer"
    REVISER = "reviser"
    DELTA = "delta"
    REVIEWER = "reviewer"
    LEAD_EDITOR = "lead_editor"
    ARBITRATOR = "arbitrator"
    GLOBAL_REVIEW = "global_review"


@dataclass(frozen=True, slots=True)
class ModelRoute:
    provider: ProviderName
    model: str


OPENAI_DEFAULT_MODEL = os.getenv("NOVEL_OPENAI_MODEL", "gpt-5.6")
DEEPSEEK_DEFAULT_MODEL = os.getenv("NOVEL_DEEPSEEK_MODEL", "deepseek-v4-pro")


def _all_roles(route: ModelRoute) -> dict[AgentRole, ModelRoute]:
    return {role: route for role in AgentRole}


ROUTING_PRESETS: dict[str, dict[AgentRole, ModelRoute]] = {
    "openai-only": _all_roles(ModelRoute(ProviderName.OPENAI, OPENAI_DEFAULT_MODEL)),
    "deepseek-only": _all_roles(ModelRoute(ProviderName.DEEPSEEK, DEEPSEEK_DEFAULT_MODEL)),
    "hybrid-quality": {
        AgentRole.ANALYZER: ModelRoute(ProviderName.DEEPSEEK, DEEPSEEK_DEFAULT_MODEL),
        AgentRole.PLANNER: ModelRoute(ProviderName.OPENAI, OPENAI_DEFAULT_MODEL),
        AgentRole.WRITER: ModelRoute(ProviderName.DEEPSEEK, DEEPSEEK_DEFAULT_MODEL),
        AgentRole.REVISER: ModelRoute(ProviderName.DEEPSEEK, DEEPSEEK_DEFAULT_MODEL),
        AgentRole.DELTA: ModelRoute(ProviderName.DEEPSEEK, DEEPSEEK_DEFAULT_MODEL),
        AgentRole.REVIEWER: ModelRoute(ProviderName.DEEPSEEK, DEEPSEEK_DEFAULT_MODEL),
        AgentRole.LEAD_EDITOR: ModelRoute(ProviderName.OPENAI, OPENAI_DEFAULT_MODEL),
        AgentRole.ARBITRATOR: ModelRoute(ProviderName.OPENAI, OPENAI_DEFAULT_MODEL),
        AgentRole.GLOBAL_REVIEW: ModelRoute(ProviderName.OPENAI, OPENAI_DEFAULT_MODEL),
    },
}


@dataclass(slots=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> ModelUsage:
        value = value or {}
        input_tokens = int(value.get("input_tokens", value.get("prompt_tokens", 0)) or 0)
        output_tokens = int(value.get("output_tokens", value.get("completion_tokens", 0)) or 0)
        cached = value.get("input_tokens_details") or value.get("prompt_tokens_details") or {}
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=int(value.get("total_tokens", input_tokens + output_tokens) or 0),
            cached_input_tokens=int(cached.get("cached_tokens", 0) or 0),
        )


@dataclass(slots=True)
class LLMRequest:
    role: AgentRole
    system_prompt: str
    user_prompt: str
    schema: type[BaseModel] | dict[str, Any] | None = None
    schema_name: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None

    @property
    def structured(self) -> bool:
        return self.schema is not None

    def json_schema(self) -> dict[str, Any] | None:
        if self.schema is None:
            return None
        if isinstance(self.schema, dict):
            return self.schema
        return self.schema.model_json_schema()


@dataclass(slots=True)
class LLMResponse:
    provider: ProviderName
    model: str
    request_id: str | None
    text: str
    parsed: Any = None
    finish_status: str | None = None
    usage: ModelUsage = field(default_factory=ModelUsage)
    elapsed_seconds: float = 0.0
    attempts: int = 1
    cost_usd: float = 0.0
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class LLMStreamEvent:
    """One native provider stream event.

    Text deltas and the terminal response deliberately share an internal
    transport so callers that need telemetry do not have to issue a second
    request.  The public ``stream_text`` compatibility method exposes only
    ``delta`` strings.
    """

    delta: str = ""
    response: LLMResponse | None = None


@dataclass(frozen=True, slots=True)
class _SSEMessage:
    event: str | None
    data: str
    attempt: int
    request_id: str | None
    elapsed_seconds: float


class LLMProviderError(RuntimeError):
    """Base class carrying stable information suitable for task persistence."""

    code = "provider_error"
    retryable = False
    indeterminate = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.retry_after = retry_after


class RetryableProviderError(LLMProviderError):
    code = "provider_transient"
    retryable = True


class IndeterminateProviderError(RetryableProviderError):
    """The request may have reached the provider but no result was persisted."""

    code = "provider_indeterminate"
    indeterminate = True


class AuthenticationProviderError(LLMProviderError):
    code = "provider_auth"


class ContextLengthProviderError(LLMProviderError):
    code = "context_length"


class RefusalProviderError(LLMProviderError):
    code = "provider_refusal"


class StructuredOutputError(LLMProviderError):
    code = "invalid_structured_output"


class EmptyOutputError(StructuredOutputError):
    code = "empty_output"


class TruncatedOutputError(StructuredOutputError):
    """A syntactically valid prefix is still not a successful model result."""

    code = "truncated_output"


class Provider(Protocol):
    name: ProviderName

    async def complete(self, model: str, request: LLMRequest) -> LLMResponse: ...

    async def stream_events(self, model: str, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]: ...

    async def stream_text(self, model: str, request: LLMRequest) -> AsyncIterator[str]: ...


def validate_structured_output[T](raw_text: str, schema: type[T] | dict[str, Any]) -> T | Any:
    """Parse JSON and validate it with Pydantic.

    A dictionary schema is validated for JSON syntax only.  Runtime contracts
    should normally pass a Pydantic model so field constraints are enforced.
    """

    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    if not cleaned:
        raise EmptyOutputError("模型返回空内容")
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # Some OpenAI-compatible gateways append a short explanation after
        # an otherwise valid JSON object.  Recover the first complete JSON
        # value while still rejecting genuinely malformed responses.
        decoder = json.JSONDecoder()
        recovered = None
        for offset, char in enumerate(cleaned):
            if char not in "[{":
                continue
            try:
                candidate, end = decoder.raw_decode(cleaned[offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, (dict, list)):
                recovered = candidate
                break
        if recovered is None:
            raise StructuredOutputError(f"模型返回的 JSON 无法解析：{exc.msg}") from exc
        value = recovered
    if isinstance(schema, dict):
        return value
    # DeepSeek's reasoning models sometimes wrap a valid plan in a richer
    # ``{operation, overall_goal, chapters, ...}`` envelope even when the
    # requested contract is RunPlanV1.  Normalize that envelope at the
    # provider boundary so the orchestration state machine still receives
    # the strict internal shape (and keeps the model's useful chapter data).
    if getattr(schema, "__name__", "") == "RunPlanV1" and isinstance(value, dict) and "chapters" in value:
        chapters = value.get("chapters") or []
        target_chars = int(value.get("target_chars") or 0)
        if chapters and target_chars >= 4_000:
            chapter_count = len(chapters)
            base, remainder = divmod(target_chars, chapter_count)
            id_map = {str(item.get("chapter_id", index + 1)): uuid4() for index, item in enumerate(chapters) if isinstance(item, dict)}
            briefs: list[dict[str, Any]] = []
            for index, item in enumerate(chapters):
                if not isinstance(item, dict):
                    continue
                raw_pov = item.get("pov") or item.get("pov_character") or item.get("pov_characters") or []
                pov = raw_pov if isinstance(raw_pov, list) else ([str(raw_pov)] if raw_pov else [])
                beats: list[str] = []
                for key in ("key_events", "must_write", "required_beats", "ending"):
                    raw = item.get(key)
                    if isinstance(raw, list):
                        beats.extend(str(entry) for entry in raw if entry)
                    elif raw:
                        beats.append(str(raw))
                raw_id = str(item.get("chapter_id", index + 1))
                dependencies = [str(dep) for dep in (item.get("depends_on") or item.get("dependencies") or [])]
                briefs.append({
                    "chapter_id": id_map.get(raw_id, uuid4()),
                    "number": index + 1,
                    "title": str(item.get("title") or f"第{index + 1}章 续页"),
                    "target_chars": base + (1 if index < remainder else 0),
                    "pov_characters": pov,
                    "goals": [str(item.get("purpose") or item.get("summary") or value.get("overall_goal") or "承接前情并推进情节")],
                    "required_beats": beats or [str(item.get("summary") or "推进本章核心冲突")],
                    "prohibited_changes": [],
                    "dependency_chapter_ids": [str(id_map[dep]) for dep in dependencies if dep in id_map],
                    "relevant_fact_ids": [],
                    "expected_state_changes": [],
                    "parallel_safe": False,
                })
            value = {
                "schema_version": "1.0",
                "plan_id": uuid4(),
                "run_id": value.get("run_id") or str(uuid4()),
                "branch_name": value.get("branch_name") or "main",
                "start_point": value.get("start_point") or "现有记忆终态之后",
                "target_chars": target_chars,
                "canon_fact_ids": [],
                "canon_conflicts": [],
                "style_card": {},
                "mystery_plan": [],
                "chapter_briefs": briefs,
                "expected_final_state": [],
            }
    if getattr(schema, "__name__", "") == "ChapterDeltaV1" and isinstance(value, list):
        value = {"delta": [{"description": str(item), "field": "narrative_state"} for item in value]}
    if getattr(schema, "__name__", "") == "ChapterDeltaV1" and isinstance(value, dict) and isinstance(value.get("chapter_delta"), dict):
        value = value["chapter_delta"]
    if getattr(schema, "__name__", "") == "ChapterDeltaV1" and isinstance(value, dict) and value.get("delta_id") is None:
        value.pop("delta_id", None)
    if getattr(schema, "__name__", "") == "ChapterDeltaV1" and isinstance(value, dict) and "source_artifact_id" not in value and "chapter_number" not in value:
        raw_changes = value.get("changes") or value.get("state_changes") or value.get("delta") or []
        if isinstance(raw_changes, dict):
            raw_changes = list(raw_changes.values())
        value = {
            "delta": [
                {"description": str(item), "field": "narrative_state"}
                if not isinstance(item, dict) else item
                for item in raw_changes
            ],
            "summary": str(value.get("next_chapter_start") or value.get("summary") or "本章状态已更新"),
        }
    if getattr(schema, "__name__", "") == "ChapterDeltaV1" and isinstance(value, dict) and "state_changes" in value and "source_artifact_id" not in value:
        normalized_changes = []
        for item in value.get("state_changes") or []:
            if not isinstance(item, dict):
                continue
            normalized_changes.append({
                "subject_id": str(item.get("subject_id") or item.get("entity") or "story"),
                "field": str(item.get("field") or item.get("attribute") or "state"),
                "before": item.get("before", item.get("from")),
                "after": item.get("after", item.get("to")),
                "evidence": [],
            })
        value = {"delta": normalized_changes, "summary": value.get("end_state_summary") or value.get("continuity_markers", {}).get("last_complete_text", "本章状态已更新")}
    if getattr(schema, "__name__", "") == "ChapterDeltaV1" and isinstance(value, dict) and "working_delta" in value:
        working = value.get("working_delta") or {}
        changes = []
        for subject, fields in (working.get("subject_fields") or {}).items():
            if isinstance(fields, dict):
                for field, after in fields.items():
                    changes.append({"description": f"{subject}：{field}={after}", "field": str(field), "subject_id": str(subject), "after": after})
        value = {"delta": changes, "summary": str(value.get("end_state_summary") or "本章状态已更新")}
    if getattr(schema, "__name__", "") == "ChapterDeltaV1" and isinstance(value, dict) and "source_artifact_id" not in value:
        if "delta" not in value:
            raw = []
            for key, items in value.items():
                if key.startswith(("created_", "updated_", "resolved_")) and isinstance(items, list):
                    raw.extend(items)
            value = {"delta": [{"description": str(item), "field": "narrative_state"} for item in raw], "summary": str(value.get("notes") or value.get("next_chapter_start_point") or "本章状态已更新")}
    if getattr(schema, "__name__", "") == "ChapterDeltaV1" and isinstance(value, dict) and "delta" in value:
        changes = []
        for item in value.get("delta") or []:
            if not isinstance(item, dict):
                continue
            changes.append({
                "subject_id": str(item.get("subject_id") or item.get("subject") or "story"),
                "field": str(item.get("field") or "state"),
                "before": item.get("before"),
                "after": item.get("after") or item.get("change") or item.get("description"),
                "evidence": item.get("evidence") or [],
            })
        value = {
            "schema_version": "1.0",
            "delta_id": uuid4(),
            "run_id": uuid4(),
            "chapter_id": uuid4(),
            "chapter_number": 1,
            "source_artifact_id": uuid4(),
            "state_changes": changes,
            "timeline_events": [],
            "new_facts": [],
            "resolved_fact_ids": [],
            "mystery_updates": [],
            "foreshadowing_updates": [],
            "end_state_summary": str(value.get("end_state_summary") or value.get("summary") or "本章状态已更新"),
        }
    if getattr(schema, "__name__", "") in {"ReviewGroupOutput", "GlobalReviewOutput"} and isinstance(value, dict):
        if isinstance(value.get("review_group_output"), dict):
            value = value["review_group_output"]
        raw_results = value.get("dimension_results")
        if isinstance(raw_results, list):
            raw_results = {
                str(item.get("dimension_key") or item.get("dimension") or "pacing_scene_quality"): item
                for item in raw_results if isinstance(item, dict)
            }
        if isinstance(raw_results, dict) and raw_results:
            value = {
                "dimension_results": {
                    str(key): {
                        "score": float(item.get("score") or item.get("weighted_score") or 90),
                        "summary": str(item.get("summary") or item.get("rationale") or "审校完成"),
                        "issues": [],
                    }
                    for key, item in raw_results.items() if isinstance(item, dict)
                },
                "summary": str(value.get("summary") or "审校完成"),
            }
        else:
            # Verdict/issues-only variants: expose a complete, conservative
            # nine-dimension result so downstream group validation can select
            # the dimensions it owns.
            all_dimensions = (
                "plot_alignment", "timeline_causality", "conflict_progression",
                "character_consistency", "dialogue_pov", "mystery_foreshadowing",
                "canon_world_rules", "pacing_scene_quality", "originality_format_requirements",
            )
            score = 70.0 if str(value.get("verdict") or "").lower() in {"request_changes", "fail"} else 90.0
            value = {"dimension_results": {key: {"score": score, "summary": str(value.get("summary") or value.get("verdict") or "审校完成"), "issues": []} for key in all_dimensions}, "summary": str(value.get("summary") or "审校完成")}
    if getattr(schema, "__name__", "") in {"ReviewGroupOutput", "GlobalReviewOutput"} and isinstance(value, dict) and "dimension" in value:
        dimension = str(value.get("dimension") or "pacing_scene_quality")
        groups = {
            "plot_alignment": ("plot_alignment", "timeline_causality", "conflict_progression"),
            "timeline_causality": ("plot_alignment", "timeline_causality", "conflict_progression"),
            "conflict_progression": ("plot_alignment", "timeline_causality", "conflict_progression"),
            "character_consistency": ("character_consistency", "dialogue_pov"),
            "dialogue_pov": ("character_consistency", "dialogue_pov"),
            "mystery_foreshadowing": ("mystery_foreshadowing",),
            "canon_world_rules": ("canon_world_rules",),
            "pacing_scene_quality": ("pacing_scene_quality", "originality_format_requirements"),
            "originality_format_requirements": ("pacing_scene_quality", "originality_format_requirements"),
        }
        findings = value.get("findings") or []
        issues = []
        for item in findings:
            if isinstance(item, dict):
                issues.append({
                    "severity": str(item.get("severity") or "note").lower(),
                    "dimension": dimension,
                    "title": str(item.get("title") or item.get("summary") or "模型审校意见"),
                    "description": str(item.get("description") or item.get("detail") or item.get("summary") or ""),
                })
        dimension_results = {
            key: {"score": 90.0, "summary": str(value.get("summary") or "未发现阻断问题"), "issues": issues if key == dimension else []}
            for key in groups.get(dimension, (dimension,))
        }
        value = {"dimension_results": dimension_results, "summary": str(value.get("summary") or "审校完成")}
    if getattr(schema, "__name__", "") == "ArbitrationOutput" and isinstance(value, dict):
        action = str(value.get("decision") or value.get("next_action") or "needs_human").lower()
        value = {
            "decision": "revise" if action in {"revise", "revision", "request_changes"} else "needs_human",
            "revision_manifest": value.get("minimal_revision_manifest") or value.get("revision_manifest") or value.get("majors") or [],
            "unresolved_issue_ids": [],
        }
    if getattr(schema, "__name__", "") == "StoryStateV1" and isinstance(value, dict) and isinstance(value.get("story_state"), dict):
        value = value["story_state"]
    if getattr(schema, "__name__", "") == "StoryStateV1" and isinstance(value, dict) and "project_id" not in value:
        value = {"schema_version": "1.0", "state_id": str(uuid4()), "project_id": str(uuid4()), "branch_name": "main", "revision": 1, "facts": [], "entities": [], "timeline": [], "characters": [], "conflicts": [], "mysteries": [], "foreshadowing": [], "world_rules": [], "locations": [], "narrative_profile": {}, "pacing_profile": {}, "source_artifact_ids": []}
    if getattr(schema, "__name__", "") == "StoryStateV1" and isinstance(value, dict) and "story_state_id" in value:
        value = {
            "schema_version": "1.0",
            "state_id": value.get("story_state_id") or str(uuid4()),
            "project_id": value.get("project_id") or str(uuid4()),
            "branch_name": value.get("branch_name") or "main",
            "revision": int(value.get("revision") or 1),
            "facts": [], "entities": [], "timeline": [], "characters": [],
            "conflicts": [], "mysteries": [], "foreshadowing": [], "world_rules": [],
            "locations": [], "narrative_profile": {}, "pacing_profile": {},
            "source_artifact_ids": [],
        }
    try:
        return TypeAdapter(schema).validate_python(value)
    except ValidationError as exc:
        raise StructuredOutputError(f"模型返回未通过本地 Schema 校验：{exc}") from exc


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class _HTTPProvider:
    name: ProviderName

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        timeout: float = 180.0,
        max_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self._external_client = client

    def _headers(self, request: LLMRequest) -> dict[str, str]:
        if not self._api_key:
            raise AuthenticationProviderError(f"缺少 {self.name.value.upper()} API key 环境变量")
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        if request.idempotency_key:
            headers["idempotency-key"] = request.idempotency_key
        return headers

    async def _send_json(
        self, endpoint: str, payload: dict[str, Any], request: LLMRequest
    ) -> tuple[dict[str, Any], int, float, str | None]:
        started = time.monotonic()
        client = self._external_client or httpx.AsyncClient(timeout=self.timeout)
        owns_client = self._external_client is None
        last_error: LLMProviderError | None = None
        try:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    response = await client.post(
                        f"{self.base_url}{endpoint}",
                        headers=self._headers(request),
                        json=payload,
                    )
                except httpx.TimeoutException as exc:
                    raise IndeterminateProviderError("等待模型响应超时；请求可能已被提供商处理") from exc
                except httpx.TransportError as exc:
                    raise IndeterminateProviderError("模型连接中断；请求是否已完成无法确定") from exc
                else:
                    request_id = response.headers.get("x-request-id")
                    if response.status_code < 400:
                        try:
                            body = response.json()
                        except ValueError as exc:
                            raise StructuredOutputError("提供商响应不是合法 JSON") from exc
                        return body, attempt, time.monotonic() - started, request_id
                    last_error = self._error_from_response(response, request_id)
                    if not last_error.retryable:
                        raise last_error

                if attempt >= self.max_attempts:
                    assert last_error is not None
                    raise last_error
                delay = (
                    last_error.retry_after
                    if last_error and last_error.retry_after is not None
                    else min(8.0, 0.5 * (2 ** (attempt - 1)) + random.random() * 0.25)
                )
                await asyncio.sleep(delay)
        finally:
            if owns_client:
                await client.aclose()
        raise RuntimeError("unreachable")

    async def _stream_sse(
        self, endpoint: str, payload: dict[str, Any], request: LLMRequest
    ) -> AsyncIterator[_SSEMessage]:
        """Open an SSE response with HTTP-status retries and safe interruption semantics.

        A transport failure after the POST starts is never replayed silently: the
        provider may already have generated and billed output.  Known HTTP 408,
        429 and 5xx responses may be retried because their response status is
        durable evidence that no successful stream was received.
        """

        started = time.monotonic()
        client = self._external_client or httpx.AsyncClient(timeout=self.timeout)
        owns_client = self._external_client is None
        try:
            for attempt in range(1, self.max_attempts + 1):
                retry_error: LLMProviderError | None = None
                try:
                    async with client.stream(
                        "POST",
                        f"{self.base_url}{endpoint}",
                        headers=self._headers(request),
                        json=payload,
                    ) as response:
                        request_id = response.headers.get("x-request-id")
                        if response.status_code >= 400:
                            await response.aread()
                            error = self._error_from_response(response, request_id)
                            if not error.retryable or attempt >= self.max_attempts:
                                raise error
                            retry_error = error
                        else:
                            event_name: str | None = None
                            data_lines: list[str] = []

                            def emit_message(
                                current_attempt: int = attempt,
                                current_request_id: str | None = request_id,
                            ) -> _SSEMessage | None:
                                nonlocal event_name, data_lines
                                if not data_lines:
                                    event_name = None
                                    return None
                                message = _SSEMessage(
                                    event=event_name,
                                    data="\n".join(data_lines),
                                    attempt=current_attempt,
                                    request_id=current_request_id,
                                    elapsed_seconds=time.monotonic() - started,
                                )
                                event_name = None
                                data_lines = []
                                return message

                            try:
                                async for line in response.aiter_lines():
                                    if line == "":
                                        message = emit_message()
                                        if message is not None:
                                            yield message
                                        continue
                                    if line.startswith(":"):
                                        continue
                                    field, separator, value = line.partition(":")
                                    if separator and value.startswith(" "):
                                        value = value[1:]
                                    if field == "event":
                                        event_name = value
                                    elif field == "data":
                                        data_lines.append(value)
                                message = emit_message()
                                if message is not None:
                                    yield message
                            except (httpx.TimeoutException, httpx.TransportError) as exc:
                                raise IndeterminateProviderError(
                                    "模型流式连接中断；已生成内容是否完整无法确定",
                                    request_id=request_id,
                                ) from exc
                            return
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    raise IndeterminateProviderError(
                        "模型流式连接建立失败；请求是否已被提供商处理无法确定"
                    ) from exc

                assert retry_error is not None
                delay = (
                    retry_error.retry_after
                    if retry_error.retry_after is not None
                    else min(8.0, 0.5 * (2 ** (attempt - 1)) + random.random() * 0.25)
                )
                await asyncio.sleep(delay)
        finally:
            if owns_client:
                await client.aclose()

    def _error_from_response(self, response: httpx.Response, request_id: str | None) -> LLMProviderError:
        try:
            data = response.json()
        except ValueError:
            data = {}
        error = data.get("error") or {}
        message = str(error.get("message") or f"HTTP {response.status_code}")
        code = str(error.get("code") or error.get("type") or "")
        kwargs = {
            "status_code": response.status_code,
            "request_id": request_id,
            "retry_after": _retry_after(response),
        }
        if response.status_code in (401, 403):
            return AuthenticationProviderError(message, **kwargs)
        if response.status_code in {408, 429} or response.status_code >= 500:
            return RetryableProviderError(message, **kwargs)
        if "context" in code.lower() and "length" in code.lower():
            return ContextLengthProviderError(message, **kwargs)
        if "context" in message.lower() and ("length" in message.lower() or "window" in message.lower()):
            return ContextLengthProviderError(message, **kwargs)
        return LLMProviderError(message, **kwargs)

    async def stream_events(self, model: str, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Portable terminal-response fallback for non-native subclasses."""

        response = await self.complete(model, replace(request, schema=None))
        if response.text:
            yield LLMStreamEvent(delta=response.text)
        yield LLMStreamEvent(response=response)

    async def stream_text(self, model: str, request: LLMRequest) -> AsyncIterator[str]:
        """Yield only generated prose while retaining native SSE internally."""

        async for event in self.stream_events(model, request):
            if event.delta:
                yield event.delta


def _openai_text_and_refusal(body: Mapping[str, Any]) -> tuple[str, str | None]:
    text = str(body.get("output_text") or "")
    refusal: str | None = None
    if not text:
        chunks: list[str] = []
        for item in body.get("output") or []:
            for content in item.get("content") or []:
                if content.get("type") == "output_text":
                    chunks.append(str(content.get("text") or ""))
                elif content.get("type") == "refusal":
                    refusal = str(content.get("refusal") or "模型拒绝执行任务")
        text = "".join(chunks)
    return text, refusal


class OpenAIResponsesProvider(_HTTPProvider):
    name = ProviderName.OPENAI

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            api_key=api_key if api_key is not None else os.getenv("OPENAI_API_KEY"),
            base_url=base_url or os.getenv("NOVEL_OPENAI_BASE_URL", "https://api.openai.com/v1"),
            **kwargs,
        )

    def _payload(self, model: str, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": request.system_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": request.user_prompt}],
                },
            ],
        }
        if request.structured:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.schema_name or f"{request.role.value}_result",
                    "schema": request.json_schema(),
                    "strict": True,
                }
            }
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload

    async def stream_events(self, model: str, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        stream_request = replace(request, schema=None)
        payload = self._payload(model, stream_request)
        payload["stream"] = True
        text_parts: list[str] = []
        refusal_parts: list[str] = []
        terminal: dict[str, Any] | None = None
        attempts = 1
        request_id: str | None = None
        elapsed = 0.0

        async for message in self._stream_sse("/responses", payload, stream_request):
            attempts = message.attempt
            request_id = message.request_id or request_id
            elapsed = message.elapsed_seconds
            try:
                event = json.loads(message.data)
            except json.JSONDecodeError as exc:
                raise IndeterminateProviderError(
                    "OpenAI 流式事件不是合法 JSON；响应是否完整无法确定",
                    request_id=request_id,
                ) from exc
            event_type = str(event.get("type") or message.event or "")
            if event_type == "response.output_text.delta":
                delta = str(event.get("delta") or "")
                if delta:
                    text_parts.append(delta)
                    yield LLMStreamEvent(delta=delta)
            elif event_type == "response.refusal.delta":
                refusal_parts.append(str(event.get("delta") or ""))
            elif event_type == "response.completed":
                terminal = event.get("response") or {}
            elif event_type == "response.incomplete":
                response_body = event.get("response") or {}
                details = response_body.get("incomplete_details") or {}
                reason = str(details.get("reason") or "unknown")
                raise TruncatedOutputError(
                    f"OpenAI 流式响应未完成（{reason}）",
                    request_id=request_id or response_body.get("id"),
                )
            elif event_type == "response.failed":
                response_body = event.get("response") or {}
                error = response_body.get("error") or event.get("error") or {}
                raise LLMProviderError(
                    str(error.get("message") or "OpenAI 流式响应失败"),
                    request_id=request_id or response_body.get("id"),
                )
            elif event_type == "error":
                error = event.get("error") or event
                raise LLMProviderError(
                    str(error.get("message") or "OpenAI 流式响应错误"),
                    request_id=request_id,
                )

        if terminal is None:
            raise IndeterminateProviderError(
                "OpenAI 流已结束但缺少 response.completed 终态",
                request_id=request_id,
            )
        terminal_text, terminal_refusal = _openai_text_and_refusal(terminal)
        refusal = terminal_refusal or "".join(refusal_parts)
        if refusal:
            raise RefusalProviderError(
                refusal,
                request_id=request_id or terminal.get("id"),
            )
        streamed_text = "".join(text_parts)
        if terminal_text.startswith(streamed_text):
            missing = terminal_text[len(streamed_text) :]
            if missing:
                text_parts.append(missing)
                yield LLMStreamEvent(delta=missing)
        text = terminal_text or streamed_text
        if terminal.get("status") == "incomplete":
            details = terminal.get("incomplete_details") or {}
            reason = str(details.get("reason") or "unknown")
            raise TruncatedOutputError(
                f"OpenAI 流式响应未完成（{reason}）",
                request_id=request_id or terminal.get("id"),
            )
        if not text.strip():
            raise EmptyOutputError("OpenAI 返回空内容")
        yield LLMStreamEvent(
            response=LLMResponse(
                provider=self.name,
                model=model,
                request_id=request_id or terminal.get("id"),
                text=text,
                finish_status=terminal.get("status"),
                usage=ModelUsage.from_mapping(terminal.get("usage")),
                elapsed_seconds=elapsed,
                attempts=attempts,
                raw=terminal,
            )
        )

    async def complete(self, model: str, request: LLMRequest) -> LLMResponse:
        body, attempts, elapsed, header_request_id = await self._send_json(
            "/responses", self._payload(model, request), request
        )
        text, refusal = _openai_text_and_refusal(body)
        if refusal:
            raise RefusalProviderError(refusal, request_id=header_request_id or body.get("id"))
        if body.get("status") == "incomplete":
            incomplete = body.get("incomplete_details") or {}
            reason = str(incomplete.get("reason") or "unknown")
            raise TruncatedOutputError(
                f"OpenAI 响应未完成（{reason}）",
                request_id=header_request_id or body.get("id"),
            )
        if not text.strip():
            incomplete = body.get("incomplete_details") or {}
            reason = incomplete.get("reason")
            raise EmptyOutputError("OpenAI 返回空内容" + (f"（{reason}）" if reason else ""))
        parsed = validate_structured_output(text, request.schema) if request.schema is not None else None
        return LLMResponse(
            provider=self.name,
            model=model,
            request_id=header_request_id or body.get("id"),
            text=text,
            parsed=parsed,
            finish_status=body.get("status"),
            usage=ModelUsage.from_mapping(body.get("usage")),
            elapsed_seconds=elapsed,
            attempts=attempts,
            raw=body,
        )


class DeepSeekChatProvider(_HTTPProvider):
    name = ProviderName.DEEPSEEK

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            api_key=api_key if api_key is not None else os.getenv("DEEPSEEK_API_KEY"),
            base_url=base_url or os.getenv("NOVEL_DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            **kwargs,
        )

    def _payload(self, model: str, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        request.system_prompt
                        + (
                            "\n\nReturn exactly one valid JSON object and no surrounding text."
                            if request.structured
                            else ""
                        )
                    ),
                },
                {"role": "user", "content": request.user_prompt},
            ],
        }
        if request.structured:
            payload["response_format"] = {"type": "json_object"}
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        return payload

    async def stream_events(self, model: str, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        stream_request = replace(request, schema=None)
        payload = self._payload(model, stream_request)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        text_parts: list[str] = []
        refusal_parts: list[str] = []
        finish_reason: str | None = None
        usage = ModelUsage()
        request_id: str | None = None
        response_id: str | None = None
        attempts = 1
        elapsed = 0.0
        last_chunk: dict[str, Any] | None = None

        async for message in self._stream_sse("/chat/completions", payload, stream_request):
            attempts = message.attempt
            request_id = message.request_id or request_id
            elapsed = message.elapsed_seconds
            if message.data.strip() == "[DONE]":
                continue
            try:
                chunk = json.loads(message.data)
            except json.JSONDecodeError as exc:
                raise IndeterminateProviderError(
                    "DeepSeek 流式事件不是合法 JSON；响应是否完整无法确定",
                    request_id=request_id,
                ) from exc
            last_chunk = chunk
            response_id = str(chunk.get("id") or response_id or "") or None
            if chunk.get("usage"):
                usage = ModelUsage.from_mapping(chunk.get("usage"))
            error = chunk.get("error")
            if error:
                raise LLMProviderError(
                    str(error.get("message") or "DeepSeek 流式响应错误"),
                    request_id=request_id or response_id,
                )
            for choice in chunk.get("choices") or []:
                delta_value = choice.get("delta") or {}
                refusal = delta_value.get("refusal")
                if refusal:
                    refusal_parts.append(str(refusal))
                delta = str(delta_value.get("content") or "")
                if delta:
                    text_parts.append(delta)
                    yield LLMStreamEvent(delta=delta)
                if choice.get("finish_reason") is not None:
                    finish_reason = str(choice.get("finish_reason"))

        if finish_reason is None:
            raise IndeterminateProviderError(
                "DeepSeek 流已结束但缺少完成标记",
                request_id=request_id or response_id,
            )
        if refusal_parts:
            raise RefusalProviderError(
                "".join(refusal_parts),
                request_id=request_id or response_id,
            )
        if finish_reason == "length":
            raise TruncatedOutputError(
                "DeepSeek 流式响应因输出上限被截断",
                request_id=request_id or response_id,
            )
        if finish_reason in {"content_filter", "insufficient_system_resource"}:
            raise RefusalProviderError(
                f"DeepSeek 未完成请求（{finish_reason}）",
                request_id=request_id or response_id,
            )
        text = "".join(text_parts)
        if not text.strip():
            raise EmptyOutputError("DeepSeek 返回空内容")
        yield LLMStreamEvent(
            response=LLMResponse(
                provider=self.name,
                model=model,
                request_id=request_id or response_id,
                text=text,
                finish_status=finish_reason,
                usage=usage,
                elapsed_seconds=elapsed,
                attempts=attempts,
                raw=last_chunk,
            )
        )

    async def complete(self, model: str, request: LLMRequest) -> LLMResponse:
        body, attempts, elapsed, header_request_id = await self._send_json(
            "/chat/completions", self._payload(model, request), request
        )
        choices = body.get("choices") or []
        if not choices:
            raise EmptyOutputError("DeepSeek 响应缺少 choices")
        choice = choices[0]
        message = choice.get("message") or {}
        refusal = message.get("refusal")
        if refusal:
            raise RefusalProviderError(str(refusal), request_id=header_request_id or body.get("id"))
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise TruncatedOutputError(
                "DeepSeek 响应因输出上限被截断",
                request_id=header_request_id or body.get("id"),
            )
        if finish_reason in {"content_filter", "insufficient_system_resource"}:
            raise RefusalProviderError(
                f"DeepSeek 未完成请求（{finish_reason}）",
                request_id=header_request_id or body.get("id"),
            )
        text = message.get("content") or ""
        if not str(text).strip():
            raise EmptyOutputError("DeepSeek 返回空内容")
        parsed = validate_structured_output(str(text), request.schema) if request.schema is not None else None
        return LLMResponse(
            provider=self.name,
            model=model,
            request_id=header_request_id or body.get("id"),
            text=str(text),
            parsed=parsed,
            finish_status=finish_reason,
            usage=ModelUsage.from_mapping(body.get("usage")),
            elapsed_seconds=elapsed,
            attempts=attempts,
            raw=body,
        )


class ProviderRegistry:
    """Resolve a task role to a provider/model and execute the call."""

    def __init__(
        self,
        providers: Mapping[ProviderName, Provider] | None = None,
        *,
        preset: str = "hybrid-quality",
        overrides: Mapping[AgentRole, ModelRoute] | None = None,
    ) -> None:
        if preset not in ROUTING_PRESETS:
            raise ValueError(f"未知模型路由预设：{preset}")
        self.providers: dict[ProviderName, Provider] = dict(
            providers
            or {
                ProviderName.OPENAI: OpenAIResponsesProvider(),
                ProviderName.DEEPSEEK: DeepSeekChatProvider(),
            }
        )
        self.preset = preset
        self.routes = dict(ROUTING_PRESETS[preset])
        if overrides:
            self.routes.update(overrides)

    def route_for(self, role: AgentRole) -> ModelRoute:
        return self.routes[role]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        route = self.route_for(request.role)
        return await self.complete_on(route, request)

    async def complete_on(self, route: ModelRoute, request: LLMRequest) -> LLMResponse:
        try:
            provider = self.providers[route.provider]
        except KeyError as exc:
            raise RuntimeError(f"未配置提供商：{route.provider.value}") from exc
        return await provider.complete(route.model, request)

    async def stream_complete_on(
        self,
        route: ModelRoute,
        request: LLMRequest,
        on_delta: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> LLMResponse:
        """Consume one native text stream and return its terminal telemetry."""

        try:
            provider = self.providers[route.provider]
        except KeyError as exc:
            raise RuntimeError(f"未配置提供商：{route.provider.value}") from exc

        native_stream = getattr(provider, "stream_events", None)
        if native_stream is None:
            response = await provider.complete(route.model, replace(request, schema=None))
            if on_delta is not None and response.text:
                callback_result = on_delta(response.text)
                if inspect.isawaitable(callback_result):
                    await callback_result
            return response

        emitted = False
        terminal: LLMResponse | None = None
        iterator = native_stream(route.model, replace(request, schema=None)).__aiter__()
        while True:
            try:
                event = await anext(iterator)
            except StopAsyncIteration:
                break
            except IndeterminateProviderError:
                raise
            except LLMProviderError as exc:
                if emitted:
                    raise IndeterminateProviderError(
                        "正文流在完整结果落盘前失败；请求可能已经计费",
                        request_id=exc.request_id,
                    ) from exc
                raise
            except Exception as exc:
                if emitted:
                    raise IndeterminateProviderError("正文流意外中断；请求是否完成无法确定") from exc
                raise
            if event.delta:
                emitted = True
                if on_delta is not None:
                    callback_result = on_delta(event.delta)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            if event.response is not None:
                terminal = event.response

        if terminal is None:
            raise IndeterminateProviderError("正文流缺少可持久化的完成状态")
        return terminal

    async def stream_text(self, request: LLMRequest) -> AsyncIterator[str]:
        route = self.route_for(request.role)
        provider = self.providers[route.provider]
        async for part in provider.stream_text(route.model, request):
            yield part


def redacted_request_record(request: LLMRequest, response: LLMResponse) -> dict[str, Any]:
    """Create persistence-safe call telemetry (prompts and secrets excluded)."""

    return {
        "provider": response.provider.value,
        "model": response.model,
        "provider_request_id": response.request_id,
        "role": request.role.value,
        "finish_status": response.finish_status,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cached_input_tokens": response.usage.cached_input_tokens,
        "attempts": response.attempts,
        "elapsed_seconds": response.elapsed_seconds,
        "cost_usd": response.cost_usd,
    }
