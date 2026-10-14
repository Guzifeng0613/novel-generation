"""Versioned contracts shared by the API, orchestrator and model adapters."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "1.0"


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)


class SummaryDimension(StrEnum):
    CORE_SUMMARY = "core_summary"
    STORY_OUTLINE = "story_outline"
    CHARACTERS = "characters"
    CONFLICTS = "conflicts"
    CORE_MYSTERIES = "core_mysteries"
    WORLDBUILDING = "worldbuilding"
    NARRATIVE_POV = "narrative_pov"
    PACING = "pacing"
    FORESHADOWING = "foreshadowing"


SUMMARY_DIMENSIONS = tuple(SummaryDimension)


class ReviewDimension(StrEnum):
    PLOT_ALIGNMENT = "plot_alignment"
    TIMELINE_CAUSALITY = "timeline_causality"
    CONFLICT_PROGRESSION = "conflict_progression"
    CHARACTER_CONSISTENCY = "character_consistency"
    DIALOGUE_POV = "dialogue_pov"
    MYSTERY_FORESHADOWING = "mystery_foreshadowing"
    CANON_WORLD_RULES = "canon_world_rules"
    PACING_SCENE_QUALITY = "pacing_scene_quality"
    ORIGINALITY_FORMAT_REQUIREMENTS = "originality_format_requirements"


REVIEW_DIMENSIONS = tuple(ReviewDimension)


class IssueSeverity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NOTE = "note"


class AssertionKind(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    SPECULATION = "speculation"


class SourceSpanV1(StrictSchema):
    source_artifact_id: UUID | None = None
    source_name: str = ""
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_offsets(self) -> SourceSpanV1:
        if self.end_offset < self.start_offset:
            raise ValueError("end_offset must not precede start_offset")
        return self


class EvidenceV1(StrictSchema):
    evidence_id: str = Field(default_factory=lambda: f"evidence_{uuid4().hex}")
    span: SourceSpanV1
    excerpt: str = ""
    assertion_kind: AssertionKind = AssertionKind.FACT
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class TaskBudgetV1(StrictSchema):
    max_cost_usd: float = Field(default=0.0, ge=0.0)
    max_tokens: int = Field(default=0, ge=0)
    max_calls: int = Field(default=1, ge=1)
    max_wall_time_seconds: int = Field(default=900, ge=1)


class TaskConstraintsV1(StrictSchema):
    output_schema: str | None = None
    output_reserve_tokens: int = Field(default=4_096, ge=0)
    context_window_tokens: int = Field(default=128_000, ge=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    stream: bool = False
    allow_provider_fallback: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_token_reserve(self) -> TaskConstraintsV1:
        if self.output_reserve_tokens >= self.context_window_tokens:
            raise ValueError("output_reserve_tokens must be smaller than the context window")
        return self


class TaskEnvelopeV1(StrictSchema):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    task_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    logical_key: str = Field(min_length=1, max_length=240)
    operation: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=120)
    prompt_version: str = Field(min_length=1, max_length=80)
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    constraints: TaskConstraintsV1 = Field(default_factory=TaskConstraintsV1)
    budget: TaskBudgetV1 = Field(default_factory=TaskBudgetV1)
    depends_on: list[UUID] = Field(default_factory=list)


class DimensionAnalysisV1(StrictSchema):
    summary: str
    facts: list[str] = Field(default_factory=list)
    evidence: list[EvidenceV1] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ChunkAnalysisV1(StrictSchema):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    analysis_id: UUID = Field(default_factory=uuid4)
    chunk_id: str = Field(min_length=1)
    source_span: SourceSpanV1
    sequence_index: int = Field(ge=0)
    dimensions: dict[SummaryDimension, DimensionAnalysisV1]
    state_delta: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_all_dimensions(self) -> ChunkAnalysisV1:
        actual = set(self.dimensions)
        expected = set(SUMMARY_DIMENSIONS)
        if actual != expected:
            missing = sorted(item.value for item in expected - actual)
            extra = sorted(str(item) for item in actual - expected)
            raise ValueError(f"all nine summary dimensions are required; missing={missing}, extra={extra}")
        return self


class StoryFactV1(StrictSchema):
    fact_id: str = Field(default_factory=lambda: f"fact_{uuid4().hex}")
    category: str
    statement: str
    assertion_kind: AssertionKind = AssertionKind.FACT
    evidence: list[EvidenceV1] = Field(default_factory=list)
    immutable_canon: bool = False
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class EntityV1(StrictSchema):
    entity_id: str = Field(default_factory=lambda: f"entity_{uuid4().hex}")
    kind: Literal["character", "location", "faction", "item", "event", "concept", "other"]
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    fact_ids: list[str] = Field(default_factory=list)
    related_entity_ids: list[str] = Field(default_factory=list)
    possible_duplicate_ids: list[str] = Field(default_factory=list)

    @field_validator("aliases")
    @classmethod
    def deduplicate_aliases(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for alias in value:
            key = alias.casefold().strip()
            if key and key not in seen:
                seen.add(key)
                result.append(alias.strip())
        return result


class TimelineEventV1(StrictSchema):
    event_id: str = Field(default_factory=lambda: f"event_{uuid4().hex}")
    order_key: str
    absolute_time: str | None = None
    participants: list[str] = Field(default_factory=list)
    event: str
    cause: str = ""
    effect: str = ""
    turning_point: bool = False
    evidence: list[EvidenceV1] = Field(default_factory=list)


class CharacterStateV1(StrictSchema):
    entity_id: str
    location_id: str | None = None
    alive_status: Literal["alive", "dead", "unknown", "other"] = "unknown"
    physical_state: str = ""
    emotional_state: str = ""
    goals: list[str] = Field(default_factory=list)
    abilities_and_costs: list[str] = Field(default_factory=list)
    knowledge: list[str] = Field(default_factory=list)
    relationships: dict[str, str] = Field(default_factory=dict)
    voice_notes: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)


class ConflictStateV1(StrictSchema):
    conflict_id: str = Field(default_factory=lambda: f"conflict_{uuid4().hex}")
    description: str
    kind: Literal["core", "secondary", "internal", "external"]
    participants: list[str] = Field(default_factory=list)
    stakes: str = ""
    status: Literal["unresolved", "suppressed", "false_resolution", "transformed", "resolved"] = "unresolved"
    fact_ids: list[str] = Field(default_factory=list)


class MysteryStateV1(StrictSchema):
    mystery_id: str = Field(default_factory=lambda: f"mystery_{uuid4().hex}")
    label: str
    subject: str = ""
    known_facts: list[str] = Field(default_factory=list)
    clues: list[str] = Field(default_factory=list)
    knowledge_by_actor: dict[str, str] = Field(default_factory=dict)
    reveal_stage: Literal["setup", "approaching", "partial", "reversal", "revealed", "expanded"] = "setup"
    follow_up: str = ""


class ForeshadowingStateV1(StrictSchema):
    foreshadowing_id: str = Field(default_factory=lambda: f"foreshadow_{uuid4().hex}")
    description: str
    planted_at: str = ""
    status: Literal["planted", "reinforced", "partially_paid", "paid", "abandoned"] = "planted"
    possible_directions: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)


class WorldRuleV1(StrictSchema):
    rule_id: str = Field(default_factory=lambda: f"rule_{uuid4().hex}")
    statement: str
    limitations: list[str] = Field(default_factory=list)
    costs: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)


class LocationStateV1(StrictSchema):
    location_id: str
    name: str
    kind: str = ""
    current_occupants: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)


class NarrativeProfileV1(StrictSchema):
    person: str = ""
    pov_rules: list[str] = Field(default_factory=list)
    information_gap_rules: list[str] = Field(default_factory=list)
    switching_patterns: list[str] = Field(default_factory=list)
    reliability_notes: list[str] = Field(default_factory=list)


class PacingProfileV1(StrictSchema):
    dominant_rhythm: str = ""
    curve: list[str] = Field(default_factory=list)
    action_rules: list[str] = Field(default_factory=list)
    dialogue_rules: list[str] = Field(default_factory=list)
    description_rules: list[str] = Field(default_factory=list)


class StoryStateV1(StrictSchema):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    state_id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    branch_name: str = "main"
    revision: int = Field(default=1, ge=1)
    parent_state_id: UUID | None = None
    facts: list[StoryFactV1] = Field(default_factory=list)
    entities: list[EntityV1] = Field(default_factory=list)
    timeline: list[TimelineEventV1] = Field(default_factory=list)
    characters: list[CharacterStateV1] = Field(default_factory=list)
    conflicts: list[ConflictStateV1] = Field(default_factory=list)
    mysteries: list[MysteryStateV1] = Field(default_factory=list)
    foreshadowing: list[ForeshadowingStateV1] = Field(default_factory=list)
    world_rules: list[WorldRuleV1] = Field(default_factory=list)
    locations: list[LocationStateV1] = Field(default_factory=list)
    narrative_profile: NarrativeProfileV1 = Field(default_factory=NarrativeProfileV1)
    pacing_profile: PacingProfileV1 = Field(default_factory=PacingProfileV1)
    source_artifact_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CanonConflictV1(StrictSchema):
    conflict_id: str = Field(default_factory=lambda: f"canon_conflict_{uuid4().hex}")
    requested_change: str
    conflicting_fact_ids: list[str] = Field(default_factory=list)
    explanation: str
    requires_branch_override: bool = True


class StyleCardV1(StrictSchema):
    pov_strategy: list[str] = Field(default_factory=list)
    rhythm_curve: list[str] = Field(default_factory=list)
    dialogue_rules: list[str] = Field(default_factory=list)
    description_rules: list[str] = Field(default_factory=list)
    originality_rules: list[str] = Field(default_factory=list)


class MysteryPlanItemV1(StrictSchema):
    mystery_id: str
    action: Literal["hold", "hint", "advance", "partial_reveal", "reveal", "expand"]
    chapter_numbers: list[int] = Field(default_factory=list)
    information_gap: str = ""
    prohibited_reveal: str = ""


class ExpectedStateChangeV1(StrictSchema):
    subject_id: str
    field: str
    expected_value: Any = None
    reason: str = ""


class ChapterBriefV1(StrictSchema):
    chapter_id: UUID = Field(default_factory=uuid4)
    number: int = Field(ge=1)
    title: str
    target_chars: int = Field(ge=500)
    pov_characters: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    required_beats: list[str] = Field(default_factory=list)
    prohibited_changes: list[str] = Field(default_factory=list)
    dependency_chapter_ids: list[UUID] = Field(default_factory=list)
    relevant_fact_ids: list[str] = Field(default_factory=list)
    expected_state_changes: list[ExpectedStateChangeV1] = Field(default_factory=list)
    parallel_safe: bool = False


class RunPlanV1(StrictSchema):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    plan_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    branch_name: str = "main"
    start_point: str
    target_chars: int = Field(ge=4_000, le=150_000)
    canon_fact_ids: list[str] = Field(default_factory=list)
    canon_conflicts: list[CanonConflictV1] = Field(default_factory=list)
    style_card: StyleCardV1 = Field(default_factory=StyleCardV1)
    mystery_plan: list[MysteryPlanItemV1] = Field(default_factory=list)
    chapter_briefs: list[ChapterBriefV1] = Field(min_length=1)
    expected_final_state: list[ExpectedStateChangeV1] = Field(default_factory=list)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    model_routes: dict[str, str] = Field(default_factory=dict)
    budget: TaskBudgetV1 = Field(default_factory=TaskBudgetV1)

    @model_validator(mode="after")
    def validate_chapter_plan(self) -> RunPlanV1:
        numbers = [brief.number for brief in self.chapter_briefs]
        if len(numbers) != len(set(numbers)):
            raise ValueError("chapter numbers must be unique")
        if sorted(numbers) != list(range(1, len(numbers) + 1)):
            raise ValueError("chapter numbers must be contiguous and start at 1")
        planned_chars = sum(brief.target_chars for brief in self.chapter_briefs)
        tolerance = max(500, round(self.target_chars * 0.05))
        if abs(planned_chars - self.target_chars) > tolerance:
            raise ValueError("chapter target characters must add up to the run target")
        ids = {brief.chapter_id for brief in self.chapter_briefs}
        dependencies: dict[UUID, set[UUID]] = {}
        for brief in self.chapter_briefs:
            unknown = set(brief.dependency_chapter_ids) - ids
            if unknown:
                raise ValueError(f"chapter {brief.number} depends on unknown chapter ids")
            if brief.chapter_id in brief.dependency_chapter_ids:
                raise ValueError(f"chapter {brief.number} cannot depend on itself")
            dependencies[brief.chapter_id] = set(brief.dependency_chapter_ids)

        visiting: set[UUID] = set()
        visited: set[UUID] = set()

        def visit(chapter_id: UUID) -> None:
            if chapter_id in visiting:
                raise ValueError("chapter dependency graph contains a cycle")
            if chapter_id in visited:
                return
            visiting.add(chapter_id)
            for dependency_id in dependencies[chapter_id]:
                visit(dependency_id)
            visiting.remove(chapter_id)
            visited.add(chapter_id)

        for chapter_id in dependencies:
            visit(chapter_id)
        return self


class StateChangeV1(StrictSchema):
    subject_id: str
    field: str
    before: Any = None
    after: Any = None
    evidence: list[EvidenceV1] = Field(default_factory=list)


class ChapterDeltaV1(StrictSchema):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    delta_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    chapter_id: UUID
    chapter_number: int = Field(ge=1)
    source_artifact_id: UUID
    state_changes: list[StateChangeV1] = Field(default_factory=list)
    timeline_events: list[TimelineEventV1] = Field(default_factory=list)
    new_facts: list[StoryFactV1] = Field(default_factory=list)
    resolved_fact_ids: list[str] = Field(default_factory=list)
    mystery_updates: list[MysteryStateV1] = Field(default_factory=list)
    foreshadowing_updates: list[ForeshadowingStateV1] = Field(default_factory=list)
    end_state_summary: str


class ChapterWorkingStateV1(StrictSchema):
    """Deterministically folded state from chapters already accepted in a run."""

    subject_fields: dict[str, dict[str, Any]] = Field(default_factory=dict)
    timeline_events: list[TimelineEventV1] = Field(default_factory=list)
    active_facts: list[StoryFactV1] = Field(default_factory=list)
    resolved_fact_ids: list[str] = Field(default_factory=list)
    mystery_states: list[MysteryStateV1] = Field(default_factory=list)
    foreshadowing_states: list[ForeshadowingStateV1] = Field(default_factory=list)
    end_state_summaries: list[str] = Field(default_factory=list)


class ChapterStateLedgerV1(StrictSchema):
    """Immutable continuity chain passed to every downstream chapter agent."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    ledger_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    project_id: UUID
    branch_name: str = "main"
    revision: int = Field(default=0, ge=0)
    base_story_state_artifact_id: UUID | None = None
    previous_ledger_artifact_id: UUID | None = None
    chapter_deltas: list[ChapterDeltaV1] = Field(default_factory=list)
    working_state: ChapterWorkingStateV1 = Field(default_factory=ChapterWorkingStateV1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReviewIssueV1(StrictSchema):
    issue_id: str = Field(default_factory=lambda: f"issue_{uuid4().hex}")
    severity: IssueSeverity
    dimension: ReviewDimension
    title: str
    description: str
    location: str = ""
    rule: str = ""
    evidence: str = ""
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    canon_fact_ids: list[str] = Field(default_factory=list)
    suspense_ids: list[str] = Field(default_factory=list)
    revision_instruction: str = ""

    @model_validator(mode="after")
    def validate_offsets(self) -> ReviewIssueV1:
        if self.start_offset is None and self.end_offset is not None:
            raise ValueError("start_offset is required when end_offset is present")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("end_offset must be greater than start_offset")
        return self


class DimensionReviewV1(StrictSchema):
    score: float = Field(ge=0.0, le=100.0)
    weight: float = Field(default=1.0, gt=0.0)
    summary: str = ""
    issues: list[ReviewIssueV1] = Field(default_factory=list)


REQUIRED_DETERMINISTIC_CHECKS = frozenset(
    {
        "word_count",
        "title_format",
        "not_truncated",
        "no_meta_narration",
        "state_delta",
        "required_beats",
        "source_overlap",
    }
)


def default_deterministic_checks() -> dict[str, bool]:
    return {name: False for name in sorted(REQUIRED_DETERMINISTIC_CHECKS)}


class ReviewReportV1(StrictSchema):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    report_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    chapter_id: UUID
    round_no: int = Field(default=1, ge=1)
    source_artifact_id: UUID
    dimension_results: dict[ReviewDimension, DimensionReviewV1]
    deterministic_checks: dict[str, bool] = Field(default_factory=default_deterministic_checks)
    blocker_count: int = Field(default=0, ge=0)
    major_count: int = Field(default=0, ge=0)
    weighted_score: float = Field(default=0.0, ge=0.0, le=100.0)
    passed: bool = False
    summary: str = ""

    @model_validator(mode="after")
    def calculate_gate(self) -> ReviewReportV1:
        actual = set(self.dimension_results)
        expected = set(REVIEW_DIMENSIONS)
        if actual != expected:
            missing = sorted(item.value for item in expected - actual)
            extra = sorted(str(item) for item in actual - expected)
            raise ValueError(f"all nine review dimensions are required; missing={missing}, extra={extra}")

        missing_checks = REQUIRED_DETERMINISTIC_CHECKS - set(self.deterministic_checks)
        if missing_checks:
            raise ValueError(f"missing deterministic checks: {sorted(missing_checks)}")

        issues = [issue for result in self.dimension_results.values() for issue in result.issues]
        blocker_count = sum(issue.severity == IssueSeverity.BLOCKER for issue in issues)
        major_count = sum(issue.severity == IssueSeverity.MAJOR for issue in issues)
        total_weight = sum(result.weight for result in self.dimension_results.values())
        weighted_score = round(
            sum(result.score * result.weight for result in self.dimension_results.values()) / total_weight,
            2,
        )
        passed = (
            blocker_count == 0
            and major_count == 0
            and all(result.score >= 85 for result in self.dimension_results.values())
            and weighted_score >= 90
            and all(self.deterministic_checks.values())
        )
        object.__setattr__(self, "blocker_count", blocker_count)
        object.__setattr__(self, "major_count", major_count)
        object.__setattr__(self, "weighted_score", weighted_score)
        object.__setattr__(self, "passed", passed)
        return self


class ArtifactManifestV1(StrictSchema):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    artifact_id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    run_id: UUID | None = None
    task_id: UUID | None = None
    kind: str = Field(min_length=1, max_length=80)
    version: int = Field(default=1, ge=1)
    relative_path: str
    sha256: str
    media_type: str = "application/octet-stream"
    byte_size: int = Field(ge=0)
    source_artifact_ids: list[UUID] = Field(default_factory=list)
    generation_config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        return normalized

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise ValueError("artifact path must be relative")
        if any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("artifact path contains an unsafe segment")
        return normalized

    @field_validator("generation_config")
    @classmethod
    def reject_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        secret_pattern = re.compile(r"(?:api[_-]?key|authorization|secret|password|token)$", re.I)

        def inspect(item: Any) -> None:
            if isinstance(item, SecretStr):
                raise ValueError("generation_config must not contain SecretStr values")
            if isinstance(item, dict):
                for key, nested in item.items():
                    if secret_pattern.search(str(key)):
                        raise ValueError("generation_config must not contain credentials or secrets")
                    inspect(nested)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    inspect(nested)

        inspect(value)
        return value
