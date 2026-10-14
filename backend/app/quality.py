"""Deterministic chapter checks and nine-dimension quality gating."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum, StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .schemas import REQUIRED_DETERMINISTIC_CHECKS

DIMENSION_KEYS: tuple[str, ...] = (
    "plot_alignment",
    "timeline_causality",
    "conflict_progression",
    "character_consistency",
    "dialogue_pov",
    "mystery_foreshadowing",
    "canon_world_rules",
    "pacing_scene_quality",
    "originality_format_requirements",
)

DIMENSION_WEIGHTS: dict[str, float] = {
    "plot_alignment": 0.14,
    "timeline_causality": 0.11,
    "conflict_progression": 0.10,
    "character_consistency": 0.14,
    "dialogue_pov": 0.10,
    "mystery_foreshadowing": 0.11,
    "canon_world_rules": 0.14,
    "pacing_scene_quality": 0.08,
    "originality_format_requirements": 0.08,
}

REVIEW_GROUPS: tuple[tuple[str, ...], ...] = (
    ("plot_alignment", "timeline_causality", "conflict_progression"),
    ("character_consistency", "dialogue_pov"),
    ("mystery_foreshadowing",),
    ("canon_world_rules",),
    ("pacing_scene_quality", "originality_format_requirements"),
)


class IssueSeverity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NOTE = "note"


class QualityIssue(BaseModel):
    model_config = ConfigDict(extra="allow")

    issue_id: str
    dimension: str
    severity: IssueSeverity
    location: str = ""
    evidence: str = ""
    rule: str = ""
    instruction: str = ""
    title: str = ""
    description: str = ""
    start_offset: int | None = None
    end_offset: int | None = None
    canon_fact_ids: list[str] = Field(default_factory=list)
    suspense_ids: list[str] = Field(default_factory=list)
    source_issue_ids: list[str] = Field(default_factory=list)


class DimensionResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    key: str
    score: float = Field(ge=0, le=100)
    summary: str = ""
    issues: list[QualityIssue] = Field(default_factory=list)


class DeterministicCheck(BaseModel):
    key: str
    passed: bool
    message: str
    evidence: str | None = None


class QualityGateResult(BaseModel):
    dimensions: dict[str, DimensionResult]
    deterministic_checks: list[DeterministicCheck]
    issues: list[QualityIssue]
    blocker_count: int
    major_count: int
    weighted_score: float
    missing_dimensions: list[str]
    passed: bool


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    if hasattr(value, "__dict__"):
        return vars(value)
    raise TypeError(f"无法转换审校结果：{type(value)!r}")


def _enum_text(value: Any) -> str:
    return str(value.value) if isinstance(value, Enum) else str(value)


def _stable_issue_id(dimension: str, location: str, rule: str, evidence: str) -> str:
    source = "|".join((dimension, location, rule, evidence)).encode("utf-8")
    return f"ISS-{hashlib.sha256(source).hexdigest()[:12].upper()}"


def _normalize_issue(raw: Any, dimension: str) -> QualityIssue:
    data = dict(_as_mapping(raw))
    severity = _enum_text(data.get("severity", "minor")).lower()
    if severity not in {item.value for item in IssueSeverity}:
        severity = "minor"
    location = str(data.get("location", data.get("text_location", "")) or "")
    evidence = str(data.get("evidence", "") or "")
    rule = str(data.get("rule", data.get("violated_rule", "")) or "")
    issue_id = str(data.get("issue_id") or data.get("id") or "")
    if not issue_id:
        issue_id = _stable_issue_id(dimension, location, rule, evidence)

    def optional_offset(*keys: str) -> int | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, int) and value >= 0:
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None

    def id_list(*keys: str) -> list[str]:
        values: list[str] = []
        for key in keys:
            raw_value = data.get(key)
            if isinstance(raw_value, str):
                raw_items = [raw_value]
            elif isinstance(raw_value, list):
                raw_items = raw_value
            else:
                raw_items = []
            for item in raw_items:
                text = str(item).strip()
                if text and text not in values:
                    values.append(text)
        return values

    return QualityIssue(
        issue_id=issue_id,
        dimension=dimension,
        severity=IssueSeverity(severity),
        location=location,
        evidence=evidence,
        rule=rule,
        title=str(data.get("title", "") or ""),
        description=str(data.get("description", data.get("message", "")) or ""),
        start_offset=optional_offset("start_offset", "startOffset"),
        end_offset=optional_offset("end_offset", "endOffset"),
        canon_fact_ids=id_list("canon_fact_ids", "canonFactIds"),
        suspense_ids=id_list("suspense_ids", "suspenseIds", "foreshadowing_ids", "mystery_ids"),
        instruction=str(data.get("instruction", data.get("revision_instruction", "")) or ""),
        source_issue_ids=[issue_id],
    )


def _extract_dimensions(report: Any) -> list[DimensionResult]:
    data = _as_mapping(report)
    raw_dimensions = data.get("dimension_results", data.get("dimensions", {}))
    items: Iterable[tuple[str | None, Any]]
    if isinstance(raw_dimensions, Mapping):
        items = raw_dimensions.items()
    else:
        items = ((None, item) for item in (raw_dimensions or []))
    results: list[DimensionResult] = []
    for mapping_key, raw in items:
        dimension = dict(_as_mapping(raw))
        key = _enum_text(dimension.get("key", dimension.get("dimension", mapping_key or "")))
        if key not in DIMENSION_KEYS:
            continue
        issues = [_normalize_issue(issue, key) for issue in dimension.get("issues", [])]
        results.append(
            DimensionResult(
                key=key,
                score=float(dimension.get("score", 0)),
                summary=str(dimension.get("summary", "") or ""),
                issues=issues,
            )
        )
    return results


def _issue_fingerprint(issue: QualityIssue) -> str:
    text = "|".join(
        (
            issue.dimension,
            re.sub(r"\s+", "", issue.location.lower()),
            re.sub(r"\s+", "", issue.rule.lower()),
            re.sub(r"\s+", "", issue.evidence.lower())[:160],
        )
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def merge_review_reports(reports: Sequence[Any]) -> dict[str, DimensionResult]:
    """Merge five independent groups without allowing missing keys to vanish."""

    candidates: dict[str, list[DimensionResult]] = {key: [] for key in DIMENSION_KEYS}
    for report in reports:
        for result in _extract_dimensions(report):
            candidates[result.key].append(result)

    merged: dict[str, DimensionResult] = {}
    severity_rank = {
        IssueSeverity.NOTE: 0,
        IssueSeverity.MINOR: 1,
        IssueSeverity.MAJOR: 2,
        IssueSeverity.BLOCKER: 3,
    }
    for key, values in candidates.items():
        if not values:
            continue
        by_fingerprint: dict[str, QualityIssue] = {}
        for value in values:
            for issue in value.issues:
                fingerprint = _issue_fingerprint(issue)
                previous = by_fingerprint.get(fingerprint)
                if previous is None:
                    by_fingerprint[fingerprint] = issue.model_copy(deep=True)
                    continue
                previous.source_issue_ids = sorted(set(previous.source_issue_ids + issue.source_issue_ids))
                if severity_rank[issue.severity] > severity_rank[previous.severity]:
                    previous.severity = issue.severity
                if len(issue.instruction) > len(previous.instruction):
                    previous.instruction = issue.instruction
                if not previous.title and issue.title:
                    previous.title = issue.title
                if not previous.description and issue.description:
                    previous.description = issue.description
                if previous.start_offset is None and issue.start_offset is not None:
                    previous.start_offset = issue.start_offset
                    previous.end_offset = issue.end_offset
                previous.canon_fact_ids = sorted(set(previous.canon_fact_ids + issue.canon_fact_ids))
                previous.suspense_ids = sorted(set(previous.suspense_ids + issue.suspense_ids))
        # Conservatively keep the lowest score when duplicate reviewers inspect
        # a dimension; the lead editor can only raise it with explicit evidence.
        merged[key] = DimensionResult(
            key=key,
            score=min(value.score for value in values),
            summary="；".join(value.summary for value in values if value.summary),
            issues=list(by_fingerprint.values()),
        )
    return merged


_META_PATTERNS = (
    re.compile(r"作为(?:一个)?(?:AI|人工智能|语言模型)", re.IGNORECASE),
    re.compile(r"以下是(?:为你|根据要求)?(?:续写|创作|正文|章节)"),
    re.compile(r"(?:写作|创作|修改)说明[:：]"),
    re.compile(r"我无法(?:继续|完成|创作)"),
)


def chinese_character_count(text: str) -> int:
    """Count non-whitespace Unicode characters, matching project targets."""

    return sum(1 for char in text if not char.isspace())


def _normalized_for_overlap(text: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFKC", text)
        if not char.isspace() and not unicodedata.category(char).startswith("P")
    )


def find_long_exact_overlap(generated: str, sources: Sequence[str], *, window: int = 80) -> str | None:
    """Return an exact normalized long span shared with a source, if any."""

    generated_norm = _normalized_for_overlap(generated)
    if len(generated_norm) < window:
        return None
    # Stepping reduces memory while retaining multiple detection offsets.
    spans = {
        generated_norm[index : index + window]
        for index in range(0, len(generated_norm) - window + 1, max(1, window // 4))
    }
    for source in sources:
        source_norm = _normalized_for_overlap(source)
        for span in spans:
            if span in source_norm:
                return span
    return None


def run_deterministic_checks(
    chapter_text: str,
    *,
    target_chars: int,
    required_beats: Sequence[str] = (),
    source_texts: Sequence[str] = (),
    delta: Any | None = None,
    char_tolerance: float = 0.35,
) -> list[DeterministicCheck]:
    stripped = chapter_text.strip()
    first_line = next((line.strip() for line in stripped.splitlines() if line.strip()), "")
    title_pattern = r"^(?:#{1,3}\s*)?(?:第[0-9一二三四五六七八九十百千零〇两]+章\b|[^。！？]{1,40}$)"
    has_title = bool(re.match(title_pattern, first_line))
    count = chinese_character_count(chapter_text)
    lower = max(1, math.floor(target_chars * (1 - char_tolerance)))
    upper = math.ceil(target_chars * (1 + char_tolerance))
    ending_ok = bool(stripped) and stripped[-1] in "。！？!?…’”」』）》】"
    meta_hits = [pattern.pattern for pattern in _META_PATTERNS if pattern.search(chapter_text)]
    missing_beats = [beat for beat in required_beats if beat and beat not in chapter_text]
    overlap = find_long_exact_overlap(chapter_text, source_texts)
    checks = [
        DeterministicCheck(
            key="title_format",
            passed=has_title,
            message="章节标题存在" if has_title else "缺少可识别的章节标题",
            evidence=first_line or None,
        ),
        DeterministicCheck(
            key="word_count",
            passed=lower <= count <= upper,
            message=f"非空白字符数 {count}，允许范围 {lower}–{upper}",
        ),
        DeterministicCheck(
            key="not_truncated",
            passed=ending_ok,
            message="结尾完整" if ending_ok else "末尾疑似截断或缺少结束标点",
        ),
        DeterministicCheck(
            key="no_meta_narration",
            passed=not meta_hits,
            message="无模型元叙述" if not meta_hits else "发现模型/写作说明式元叙述",
            evidence="; ".join(meta_hits) or None,
        ),
        DeterministicCheck(
            key="state_delta",
            passed=delta is not None,
            message="状态 delta 已生成" if delta is not None else "缺少章节状态 delta",
        ),
        DeterministicCheck(
            key="required_beats",
            passed=not missing_beats,
            message="必写情节均有文本命中" if not missing_beats else "存在未命中的必写情节",
            evidence="；".join(missing_beats) or None,
        ),
        DeterministicCheck(
            key="source_overlap",
            passed=overlap is None,
            message="未发现原文长片段精确复用" if overlap is None else "发现原文长片段精确复用",
            evidence=overlap,
        ),
    ]
    return checks


def evaluate_quality(
    dimensions: Mapping[str, DimensionResult | Mapping[str, Any]],
    deterministic_checks: Sequence[DeterministicCheck | Mapping[str, Any]],
) -> QualityGateResult:
    normalized: dict[str, DimensionResult] = {}
    for key, value in dimensions.items():
        if key not in DIMENSION_WEIGHTS:
            raise ValueError(f"unknown review dimension: {key}")
        if isinstance(value, DimensionResult):
            normalized[key] = value
        else:
            payload = dict(value)
            payload.setdefault("key", key)
            payload["issues"] = [_normalize_issue(issue, key) for issue in payload.get("issues", [])]
            normalized[key] = DimensionResult.model_validate(payload)
    checks = [
        check if isinstance(check, DeterministicCheck) else DeterministicCheck.model_validate(check)
        for check in deterministic_checks
    ]
    check_keys = {check.key for check in checks}
    has_required_checks = REQUIRED_DETERMINISTIC_CHECKS <= check_keys
    missing = [key for key in DIMENSION_KEYS if key not in normalized]
    issues = [issue for result in normalized.values() for issue in result.issues]
    counts = Counter(issue.severity.value for issue in issues)
    weighted_score = round(sum(normalized[key].score * DIMENSION_WEIGHTS[key] for key in normalized), 2)
    passed = (
        not missing
        and counts[IssueSeverity.BLOCKER.value] == 0
        and counts[IssueSeverity.MAJOR.value] == 0
        and all(normalized[key].score >= 85 for key in DIMENSION_KEYS if key in normalized)
        and weighted_score >= 90
        and has_required_checks
        and all(check.passed for check in checks)
    )
    return QualityGateResult(
        dimensions=normalized,
        deterministic_checks=checks,
        issues=issues,
        blocker_count=counts[IssueSeverity.BLOCKER.value],
        major_count=counts[IssueSeverity.MAJOR.value],
        weighted_score=weighted_score,
        missing_dimensions=missing,
        passed=passed,
    )


_DETERMINISTIC_REVISION_RULES: dict[str, tuple[str, IssueSeverity, str, str, str]] = {
    "title_format": (
        "originality_format_requirements",
        IssueSeverity.MAJOR,
        "标题行",
        "每章必须以可识别的章节标题开头",
        "补充或修正首行章节标题，保留章号与章纲标题含义。",
    ),
    "word_count": (
        "originality_format_requirements",
        IssueSeverity.MAJOR,
        "全文",
        "章节非空白字符数必须处于目标字数允许范围",
        "扩写或删减正文至证据所示允许范围；不得用重复段落或元叙述凑字数。",
    ),
    "not_truncated": (
        "pacing_scene_quality",
        IssueSeverity.MAJOR,
        "章末",
        "正文必须完整收束，不能在句中或输出中途截断",
        "补全被截断的句子、场景与章末钩子，并以完整结束标点收尾。",
    ),
    "no_meta_narration": (
        "originality_format_requirements",
        IssueSeverity.MAJOR,
        "全文",
        "正文不得包含 AI 身份、写作说明或交付说明式元叙述",
        "删除证据所指元叙述，只保留小说标题与正文，并自然衔接相邻段落。",
    ),
    "state_delta": (
        "canon_world_rules",
        IssueSeverity.BLOCKER,
        "章末状态",
        "每章必须产生可验证的章节状态 delta",
        "明确正文中的人物、地点、伤势、知情范围与悬念终态，以便重新提取状态 delta。",
    ),
    "required_beats": (
        "plot_alignment",
        IssueSeverity.MAJOR,
        "全文",
        "章纲中的必写情节必须在正文中明确落地",
        "补写证据列出的必写情节，并把它们嵌入现有因果链，避免清单式硬塞。",
    ),
    "source_overlap": (
        "originality_format_requirements",
        IssueSeverity.BLOCKER,
        "全文",
        "续写不得长片段逐字复用项目源稿",
        "重写与源稿重叠的片段，保留必要事实但更换表达、动作组织与场景细节。",
    ),
}


def _deterministic_revision_item(check: DeterministicCheck) -> dict[str, Any]:
    rule = _DETERMINISTIC_REVISION_RULES.get(check.key)
    if rule is None:
        dimension = "originality_format_requirements"
        severity = IssueSeverity.MAJOR
        location = "全文"
        violated_rule = f"本地确定性检查 {check.key} 必须通过"
        instruction = f"按证据修复确定性检查 {check.key}，不得改动无关剧情。"
    else:
        dimension, severity, location, violated_rule, instruction = rule
    issue_id = f"DET-{re.sub(r'[^A-Za-z0-9]+', '-', check.key).strip('-').upper()}"
    evidence = check.message
    if check.evidence:
        evidence = f"{evidence}；证据：{check.evidence}"
    return {
        "issue_id": issue_id,
        "source_issue_ids": [issue_id],
        "dimension": dimension,
        "severity": severity.value,
        "location": location,
        "evidence": evidence,
        "rule": violated_rule,
        "instruction": instruction,
    }


def build_revision_manifest(result: QualityGateResult) -> list[dict[str, Any]]:
    """Return the only issues a revision agent is authorized to change."""

    ordered = sorted(
        result.issues,
        key=lambda issue: (
            {
                IssueSeverity.BLOCKER: 0,
                IssueSeverity.MAJOR: 1,
                IssueSeverity.MINOR: 2,
                IssueSeverity.NOTE: 3,
            }[issue.severity],
            issue.location,
            issue.issue_id,
        ),
    )
    review_items = [
        {
            "issue_id": issue.issue_id,
            "source_issue_ids": issue.source_issue_ids,
            "dimension": issue.dimension,
            "severity": issue.severity.value,
            "location": issue.location,
            "evidence": issue.evidence,
            "rule": issue.rule,
            "instruction": issue.instruction,
        }
        for issue in ordered
        if issue.severity in {IssueSeverity.BLOCKER, IssueSeverity.MAJOR, IssueSeverity.MINOR}
    ]
    deterministic_items = [
        _deterministic_revision_item(check)
        for check in sorted(result.deterministic_checks, key=lambda item: item.key)
        if not check.passed
    ]
    severity_order = {
        IssueSeverity.BLOCKER.value: 0,
        IssueSeverity.MAJOR.value: 1,
        IssueSeverity.MINOR.value: 2,
        IssueSeverity.NOTE.value: 3,
    }
    return sorted(
        [*review_items, *deterministic_items],
        key=lambda item: (
            severity_order.get(str(item["severity"]), 4),
            str(item["location"]),
            str(item["issue_id"]),
        ),
    )


def has_repeated_blockers(history: Sequence[QualityGateResult]) -> bool:
    """Detect the same blocker/major fingerprint in two consecutive rounds."""

    if len(history) < 2:
        return False
    current = {
        _issue_fingerprint(issue)
        for issue in history[-1].issues
        if issue.severity in {IssueSeverity.BLOCKER, IssueSeverity.MAJOR}
    }
    previous = {
        _issue_fingerprint(issue)
        for issue in history[-2].issues
        if issue.severity in {IssueSeverity.BLOCKER, IssueSeverity.MAJOR}
    }
    for target, result in ((current, history[-1]), (previous, history[-2])):
        for check in result.deterministic_checks:
            if check.passed:
                continue
            item = _deterministic_revision_item(check)
            if item["severity"] in {IssueSeverity.BLOCKER.value, IssueSeverity.MAJOR.value}:
                target.add(f"deterministic:{check.key}")
    return bool(current & previous)
