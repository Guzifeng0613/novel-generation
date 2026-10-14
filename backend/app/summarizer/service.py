"""Public service functions for the summarizer module."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

SUMMARIZER_STAGES = (
    "summarizer_split",
    "summarizer_extract",
    "summarizer_entity_index",
    "summarizer_merge",
    "summarizer_compact",
    "summarizer_complete",
)


async def create_summary_run(
    runtime: Any,
    *,
    project_id: UUID,
    branch_name: str,
    provider_preset: str,
    budget: Mapping[str, Any],
    splitter_enabled: bool,
) -> Any:
    """Create a summarizer run through the shared runtime.

    The runtime still provides scheduling, persistence, budgets and provider
    calls; this module fixes the summarizer-facing contract so API/UI code does
    not call continuation concepts by accident.
    """

    return await runtime.create_analysis_run(
        project_id=project_id,
        branch_name=branch_name,
        provider_preset=provider_preset,
        budget={
            **dict(budget),
            "module": "summarizer",
            "splitter_enabled": splitter_enabled,
        },
        splitter_enabled=splitter_enabled,
    )
