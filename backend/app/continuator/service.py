"""Public service functions for the continuator module."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

CONTINUATOR_STAGES = (
    "continuator_memory",
    "continuator_canon",
    "continuator_style",
    "continuator_suspense",
    "continuator_outline",
    "continuator_writing",
    "continuator_review",
    "continuator_memory_update",
    "continuator_delivery",
)


async def create_continuation_run(
    runtime: Any,
    *,
    project_id: UUID,
    target_chars: int,
    branch_name: str,
    provider_preset: str,
    user_requirements: str,
    budget: Mapping[str, Any],
) -> Any:
    """Create a continuation run through the shared runtime."""

    return await runtime.create_continuation_run(
        project_id=project_id,
        target_chars=target_chars,
        branch_name=branch_name,
        provider_preset=provider_preset,
        user_requirements=user_requirements,
        budget={
            **dict(budget),
            "module": "continuator",
        },
    )
