from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.schemas import (
    REVIEW_DIMENSIONS,
    SUMMARY_DIMENSIONS,
    ArtifactManifestV1,
    ChapterBriefV1,
    ChunkAnalysisV1,
    DimensionAnalysisV1,
    DimensionReviewV1,
    IssueSeverity,
    ReviewIssueV1,
    ReviewReportV1,
    RunPlanV1,
    SourceSpanV1,
    SummaryDimension,
    default_deterministic_checks,
)


def test_chunk_analysis_requires_all_nine_dimensions() -> None:
    dimensions = {item: DimensionAnalysisV1(summary=item.value) for item in SUMMARY_DIMENSIONS}
    model = ChunkAnalysisV1(
        chunk_id="chunk-1",
        source_span=SourceSpanV1(source_name="book.txt", start_offset=0, end_offset=100),
        sequence_index=0,
        dimensions=dimensions,
    )
    assert len(model.dimensions) == 9

    dimensions.pop(SummaryDimension.PACING)
    with pytest.raises(ValidationError, match="all nine summary dimensions"):
        ChunkAnalysisV1(
            chunk_id="chunk-1",
            source_span=SourceSpanV1(source_name="book.txt", start_offset=0, end_offset=100),
            sequence_index=0,
            dimensions=dimensions,
        )


def test_review_gate_is_computed_from_scores_issues_and_checks() -> None:
    reviews = {item: DimensionReviewV1(score=92) for item in REVIEW_DIMENSIONS}
    checks = {key: True for key in default_deterministic_checks()}
    report = ReviewReportV1(
        run_id=uuid4(),
        chapter_id=uuid4(),
        source_artifact_id=uuid4(),
        dimension_results=reviews,
        deterministic_checks=checks,
    )
    assert report.passed is True
    assert report.weighted_score == 92

    reviews[REVIEW_DIMENSIONS[0]] = DimensionReviewV1(
        score=95,
        issues=[
            ReviewIssueV1(
                severity=IssueSeverity.MAJOR,
                dimension=REVIEW_DIMENSIONS[0],
                title="因果断裂",
                description="角色突然抵达另一地点",
            )
        ],
    )
    failed = ReviewReportV1(
        run_id=uuid4(),
        chapter_id=uuid4(),
        source_artifact_id=uuid4(),
        dimension_results=reviews,
        deterministic_checks=checks,
    )
    assert failed.major_count == 1
    assert failed.passed is False


def test_review_issue_location_offsets_and_references_are_optional_but_validated() -> None:
    issue = ReviewIssueV1(
        severity=IssueSeverity.MAJOR,
        dimension=REVIEW_DIMENSIONS[0],
        title="地点跳变",
        description="角色无过渡抵达另一地点",
        location="雨站段落",
        start_offset=4,
        end_offset=9,
        canon_fact_ids=["canon-1"],
        suspense_ids=["hook-1"],
    )

    assert issue.start_offset == 4
    assert issue.end_offset == 9
    assert issue.canon_fact_ids == ["canon-1"]

    with pytest.raises(ValidationError, match="greater than start_offset"):
        ReviewIssueV1(
            severity=IssueSeverity.MINOR,
            dimension=REVIEW_DIMENSIONS[0],
            title="空范围",
            description="偏移不能为空",
            start_offset=4,
            end_offset=4,
        )


def test_run_plan_validates_range_and_dependencies() -> None:
    first = ChapterBriefV1(number=1, title="来信", target_chars=2_000)
    second = ChapterBriefV1(
        number=2,
        title="赴约",
        target_chars=2_000,
        dependency_chapter_ids=[first.chapter_id],
    )
    plan = RunPlanV1(
        run_id=uuid4(),
        start_point="上一卷尾声之后",
        target_chars=4_000,
        chapter_briefs=[first, second],
    )
    assert len(plan.chapter_briefs) == 2

    with pytest.raises(ValidationError):
        RunPlanV1(
            run_id=uuid4(),
            start_point="起点",
            target_chars=3_999,
            chapter_briefs=[first],
        )


def test_artifact_manifest_rejects_unsafe_paths_and_secret_keys() -> None:
    common = dict(
        project_id=uuid4(),
        kind="chapter",
        sha256="a" * 64,
        byte_size=3,
    )
    with pytest.raises(ValidationError, match="relative"):
        ArtifactManifestV1(relative_path="C:/secret.txt", **common)
    with pytest.raises(ValidationError, match="credentials"):
        ArtifactManifestV1(
            relative_path="objects/aa/file.txt", generation_config={"api_key": "leak"}, **common
        )
    with pytest.raises(ValidationError, match="credentials"):
        ArtifactManifestV1(
            relative_path="objects/aa/file.txt",
            generation_config={"request": {"headers": {"authorization": "Bearer leak"}}},
            **common,
        )
