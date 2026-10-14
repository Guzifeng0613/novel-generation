from backend.app.quality import (
    DIMENSION_KEYS,
    DeterministicCheck,
    DimensionResult,
    IssueSeverity,
    QualityIssue,
    build_revision_manifest,
    evaluate_quality,
    has_repeated_blockers,
    merge_review_reports,
)


def test_quality_gate_cannot_pass_without_required_deterministic_checks() -> None:
    dimensions = {key: DimensionResult(key=key, score=95, summary="通过") for key in DIMENSION_KEYS}
    result = evaluate_quality(dimensions, [])
    assert result.passed is False


def test_quality_gate_rejects_unknown_dimension_keys() -> None:
    dimensions = {key: DimensionResult(key=key, score=95, summary="通过") for key in DIMENSION_KEYS}
    dimensions["invented"] = DimensionResult(key="invented", score=100)
    try:
        evaluate_quality(dimensions, [])
    except ValueError as exc:
        assert "unknown review dimension" in str(exc)
    else:
        raise AssertionError("unknown dimension must not be silently accepted")


def test_merge_review_reports_preserves_issue_trace_fields() -> None:
    merged = merge_review_reports([
        {
            "dimension_results": {
                "canon_world_rules": DimensionResult(
                    key="canon_world_rules",
                    score=70,
                    issues=[
                        QualityIssue(
                            issue_id="ISS-1",
                            dimension="canon_world_rules",
                            severity=IssueSeverity.MAJOR,
                            location="雨站",
                            evidence="伤势消失",
                            rule="伤势必须延续",
                            title="伤势连续性断裂",
                            description="角色上一章骨折，本章无治疗恢复",
                            start_offset=5,
                            end_offset=12,
                            canon_fact_ids=["canon-injury"],
                            suspense_ids=["hook-watch"],
                        )
                    ],
                )
            }
        }
    ])

    issue = merged["canon_world_rules"].issues[0]
    assert issue.start_offset == 5
    assert issue.end_offset == 12
    assert issue.canon_fact_ids == ["canon-injury"]
    assert issue.suspense_ids == ["hook-watch"]


def test_failed_deterministic_checks_become_stable_actionable_revision_items() -> None:
    dimensions = {key: DimensionResult(key=key, score=95, summary="通过") for key in DIMENSION_KEYS}
    failed_keys = {
        "title_format",
        "word_count",
        "not_truncated",
        "no_meta_narration",
        "state_delta",
        "required_beats",
        "source_overlap",
    }

    def manifest(message: str) -> list[dict[str, object]]:
        checks = [
            DeterministicCheck(
                key=key,
                passed=False,
                message=message,
                evidence=f"evidence:{key}",
            )
            for key in sorted(failed_keys)
        ]
        return build_revision_manifest(evaluate_quality(dimensions, checks))

    first = manifest("first round")
    second = manifest("changed evidence after revision")
    assert {str(item["issue_id"]) for item in first} == {
        f"DET-{key.replace('_', '-').upper()}" for key in failed_keys
    }
    assert [item["issue_id"] for item in first] == [item["issue_id"] for item in second]
    assert all(item["evidence"] and item["rule"] and item["instruction"] for item in first)


def test_repeated_deterministic_major_can_enter_arbitration_early() -> None:
    dimensions = {key: DimensionResult(key=key, score=95, summary="通过") for key in DIMENSION_KEYS}
    rounds = [
        evaluate_quality(
            dimensions,
            [
                DeterministicCheck(
                    key="word_count",
                    passed=False,
                    message=f"round {round_no}: still too short",
                )
            ],
        )
        for round_no in (1, 2)
    ]
    assert has_repeated_blockers(rounds) is True
