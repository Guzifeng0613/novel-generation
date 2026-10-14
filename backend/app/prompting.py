"""Versioned prompt loading and task-envelope rendering."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from .providers import AgentRole, LLMRequest

PROMPT_FILES: dict[AgentRole, str] = {
    AgentRole.ANALYZER: "summarizer_analyzer.md",
    AgentRole.PLANNER: "continuator_planner.md",
    AgentRole.WRITER: "continuator_writer.md",
    AgentRole.REVISER: "continuator_writer.md",
    AgentRole.DELTA: "continuator_writer.md",
    AgentRole.REVIEWER: "continuator_reviewer.md",
    AgentRole.LEAD_EDITOR: "continuator_lead_editor.md",
    AgentRole.ARBITRATOR: "continuator_lead_editor.md",
    AgentRole.GLOBAL_REVIEW: "continuator_lead_editor.md",
}

OPERATION_ROLES: dict[str, AgentRole] = {
    "analyze_chunk": AgentRole.ANALYZER,
    "merge_story_state": AgentRole.ANALYZER,
    "build_plan": AgentRole.PLANNER,
    "revise_plan": AgentRole.PLANNER,
    "write_chapter": AgentRole.WRITER,
    "revise_chapter": AgentRole.REVISER,
    "extract_delta": AgentRole.DELTA,
    "review_dimension_group": AgentRole.REVIEWER,
    "consolidate_reviews": AgentRole.LEAD_EDITOR,
    "arbitrate_chapter": AgentRole.ARBITRATOR,
    "global_review": AgentRole.GLOBAL_REVIEW,
    "build_memory": AgentRole.LEAD_EDITOR,
}

OPERATION_PROMPT_FILES: dict[str, str] = {
    "analyze_chunk": "summarizer_analyzer.md",
    "merge_story_state": "summarizer_analyzer.md",
    "build_plan": "continuator_planner.md",
    "revise_plan": "continuator_planner.md",
    "write_chapter": "continuator_writer.md",
    "revise_chapter": "continuator_writer.md",
    "extract_delta": "continuator_writer.md",
    "review_dimension_group": "continuator_reviewer.md",
    "consolidate_reviews": "continuator_lead_editor.md",
    "arbitrate_chapter": "continuator_lead_editor.md",
    "global_review": "continuator_lead_editor.md",
    "build_memory": "continuator_lead_editor.md",
}


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    version: str
    body: str
    sha256: str
    metadata: Mapping[str, str]

    def render(self, **variables: Any) -> str:
        serialized = {key: _serialize_prompt_value(value) for key, value in variables.items()}
        return Template(self.body).safe_substitute(serialized)


def _serialize_prompt_value(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return "null"
    return str(value)


def _prompt_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _prompt_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_prompt_jsonable(item) for item in value]
    return value


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((index for index in range(1, len(lines)) if lines[index] == "---"), None)
    if end is None:
        return {}, text
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, "\n".join(lines[end + 1 :]).strip()


class PromptStore:
    """Read immutable prompt templates from ``backend/prompts``."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        frozen_templates: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.root = Path(root) if root else Path(__file__).resolve().parents[1] / "prompts"
        self._cache: dict[str, PromptTemplate] = {}
        self._frozen_templates = {
            str(filename): dict(value) for filename, value in (frozen_templates or {}).items()
        }

    @classmethod
    def from_snapshot(cls, value: Mapping[str, Any]) -> PromptStore:
        templates = value.get("templates")
        if not isinstance(templates, Mapping):
            raise ValueError("prompt snapshot has no templates mapping")
        return cls(frozen_templates=templates)

    def load(self, filename: str) -> PromptTemplate:
        if filename in self._cache:
            return self._cache[filename]
        frozen = self._frozen_templates.get(filename)
        if frozen is not None:
            metadata_value = frozen.get("metadata") or {}
            if not isinstance(metadata_value, Mapping):
                raise ValueError(f"frozen prompt metadata is invalid: {filename}")
            metadata = {str(key): str(value) for key, value in metadata_value.items()}
            body = str(frozen.get("body") or "")
            digest = str(frozen.get("sha256") or "")
            if not body or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"frozen prompt snapshot is invalid: {filename}")
            result = PromptTemplate(
                name=str(frozen.get("name") or Path(filename).stem),
                version=str(frozen.get("version") or "0"),
                body=body,
                sha256=digest,
                metadata=metadata,
            )
        else:
            path = self.root / filename
            text = path.read_text(encoding="utf-8")
            metadata, body = _parse_front_matter(text)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            result = PromptTemplate(
                name=metadata.get("name", path.stem),
                version=metadata.get("version", "0"),
                body=body,
                sha256=digest,
                metadata=metadata,
            )
        self._cache[filename] = result
        return result

    def for_role(self, role: AgentRole) -> PromptTemplate:
        return self.load(PROMPT_FILES[role])

    def for_operation(self, operation: str, role: AgentRole) -> PromptTemplate:
        return self.load(OPERATION_PROMPT_FILES.get(operation, PROMPT_FILES[role]))

    def _manifest_filenames(self) -> list[str]:
        return sorted(set(PROMPT_FILES.values()) | set(OPERATION_PROMPT_FILES.values()))

    def version_manifest(self) -> dict[str, dict[str, str]]:
        return {
            filename: {
                "name": template.name,
                "version": template.version,
                "sha256": template.sha256,
            }
            for filename in self._manifest_filenames()
            for template in [self.load(filename)]
        }

    def snapshot_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "templates": {
                filename: {
                    "name": template.name,
                    "version": template.version,
                    "sha256": template.sha256,
                    "metadata": dict(template.metadata),
                    "body": template.body,
                }
                for filename in self._manifest_filenames()
                for template in [self.load(filename)]
            },
        }

    def build_request(
        self,
        *,
        operation: str,
        context: Mapping[str, Any] | BaseModel,
        schema: type[BaseModel] | dict[str, Any] | None = None,
        role: AgentRole | None = None,
        max_output_tokens: int | None = None,
        idempotency_key: str | None = None,
    ) -> LLMRequest:
        resolved_role = role or OPERATION_ROLES.get(operation)
        if resolved_role is None:
            raise ValueError(f"未知提示词操作：{operation}")
        template = self.for_operation(operation, resolved_role)
        if isinstance(context, BaseModel):
            payload: Any = context.model_dump(mode="json")
        else:
            payload = _prompt_jsonable(context)
        system_prompt = template.render(
            operation=operation,
            schema_name=(schema.__name__ if isinstance(schema, type) else "JSON"),
        )
        user_prompt = json.dumps(
            {
                "operation": operation,
                "prompt_version": template.version,
                "input": payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return LLMRequest(
            role=resolved_role,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
            schema_name=(schema.__name__ if isinstance(schema, type) else f"{operation}_result"),
            max_output_tokens=max_output_tokens,
            metadata={
                "operation": operation,
                "prompt_name": template.name,
                "prompt_version": template.version,
                "prompt_sha256": template.sha256,
            },
            idempotency_key=idempotency_key,
        )
