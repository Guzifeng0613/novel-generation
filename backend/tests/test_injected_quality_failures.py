from __future__ import annotations

import pytest

from backend.app.quality import (
    DIMENSION_KEYS,
    DeterministicCheck,
    DimensionResult,
    IssueSeverity,
    QualityIssue,
    build_revision_manifest,
    evaluate_quality,
)
from backend.app.schemas import REQUIRED_DETERMINISTIC_CHECKS


@pytest.mark.parametrize(
    ("case_id", "dimension", "evidence"),
    [
        ("DEAD-REVIVAL", "character_consistency", "已确认死亡的角色无解释重新行动"),
        ("LOCATION-JUMP", "timeline_causality", "同一时刻从旧站无路程跳到城北"),
        ("INJURY-VANISH", "canon_world_rules", "上一章骨折在本章无治疗直接消失"),
        ("POV-OVERREACH", "dialogue_pov", "限知视角直接知道他人未说出口的计划"),
        ("VOICE-DRIFT", "dialogue_pov", "角色口吻与既有 voice notes 相反"),
        ("MYSTERY-EARLY", "mystery_foreshadowing", "章纲禁止揭示的真相被提前公布"),
        ("ABILITY-COST", "canon_world_rules", "能力使用跳过既定代价"),
    ],
)
def test_injected_continuity_failures_are_located_and_block_the_gate(
    case_id: str,
    dimension: str,
    evidence: str,
) -> None:
    dimensions = {key: DimensionResult(key=key, score=95, summary="通过") for key in DIMENSION_KEYS}
    dimensions[dimension] = DimensionResult(
        key=dimension,
        score=70,
        summary="发现植入问题",
        issues=[
            QualityIssue(
                issue_id=case_id,
                dimension=dimension,
                severity=IssueSeverity.BLOCKER,
                location="正文中段",
                evidence=evidence,
                rule="不得违反已批准 Canon、状态或知情边界",
                instruction="只修复该定位问题并保持其他剧情不变",
            )
        ],
    )
    checks = [
        DeterministicCheck(key=key, passed=True, message="通过") for key in REQUIRED_DETERMINISTIC_CHECKS
    ]

    gate = evaluate_quality(dimensions, checks)
    manifest = build_revision_manifest(gate)

    assert gate.passed is False
    assert gate.blocker_count == 1
    issue = next(item for item in manifest if item["issue_id"] == case_id)
    assert issue["dimension"] == dimension
    assert issue["evidence"] == evidence
    assert issue["instruction"]
