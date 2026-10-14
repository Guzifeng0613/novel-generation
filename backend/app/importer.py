"""Read-only inspection and normalization for supported novel imports."""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from .schemas import (
    AssertionKind,
    CharacterStateV1,
    ConflictStateV1,
    EntityV1,
    EvidenceV1,
    ForeshadowingStateV1,
    LocationStateV1,
    MysteryStateV1,
    NarrativeProfileV1,
    PacingProfileV1,
    SourceSpanV1,
    StoryFactV1,
    StoryStateV1,
    SummaryDimension,
    TimelineEventV1,
    WorldRuleV1,
)
from .splitter import DecodedText, decode_text_bytes, read_text_file


class ImportValidationError(ValueError):
    pass


class ImportKind(StrEnum):
    RAW_TEXT = "raw_text"
    SUMMARY_BUNDLE = "summary_bundle"
    PROJECT_BUNDLE = "project_bundle"


DIMENSION_LABELS: dict[SummaryDimension, str] = {
    SummaryDimension.CORE_SUMMARY: "核心摘要",
    SummaryDimension.STORY_OUTLINE: "故事大纲",
    SummaryDimension.CHARACTERS: "角色",
    SummaryDimension.CONFLICTS: "冲突",
    SummaryDimension.CORE_MYSTERIES: "核心悬念",
    SummaryDimension.WORLDBUILDING: "背景/世界观",
    SummaryDimension.NARRATIVE_POV: "叙事视角",
    SummaryDimension.PACING: "节奏控制",
    SummaryDimension.FORESHADOWING: "伏笔追踪",
}


_TITLE_ALIASES: dict[str, SummaryDimension] = {
    "核心摘要": SummaryDimension.CORE_SUMMARY,
    "摘要": SummaryDimension.CORE_SUMMARY,
    "故事大纲": SummaryDimension.STORY_OUTLINE,
    "大纲": SummaryDimension.STORY_OUTLINE,
    "时间线大纲": SummaryDimension.STORY_OUTLINE,
    "角色": SummaryDimension.CHARACTERS,
    "角色卡片": SummaryDimension.CHARACTERS,
    "人物": SummaryDimension.CHARACTERS,
    "人物角色": SummaryDimension.CHARACTERS,
    "冲突": SummaryDimension.CONFLICTS,
    "冲突引擎": SummaryDimension.CONFLICTS,
    "核心悬念": SummaryDimension.CORE_MYSTERIES,
    "悬念": SummaryDimension.CORE_MYSTERIES,
    "悬念台账": SummaryDimension.CORE_MYSTERIES,
    "背景世界观": SummaryDimension.WORLDBUILDING,
    "世界观背景": SummaryDimension.WORLDBUILDING,
    "世界观": SummaryDimension.WORLDBUILDING,
    "背景": SummaryDimension.WORLDBUILDING,
    "叙事视角": SummaryDimension.NARRATIVE_POV,
    "视角": SummaryDimension.NARRATIVE_POV,
    "节奏控制": SummaryDimension.PACING,
    "节奏": SummaryDimension.PACING,
    "节奏分析": SummaryDimension.PACING,
    "伏笔追踪": SummaryDimension.FORESHADOWING,
    "伏笔": SummaryDimension.FORESHADOWING,
    "伏笔台账": SummaryDimension.FORESHADOWING,
}

_NUMBER_PREFIX_RE = re.compile(
    r"^(?:"
    r"[（(]?\s*(?:[一二三四五六七八九十百]+|\d+|[０-９]+)\s*[、.．:：)）-]\s*"
    r"|第\s*(?:[一二三四五六七八九十百]+|\d+|[０-９]+)\s*(?:部分|章|节)\s*[、.．:：-]?\s*"
    r")"
)
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(?P<title>[^\r\n]+?)[ \t]*#*[ \t]*\r?$")


@dataclass(frozen=True, slots=True)
class ImportDocument:
    source_name: str
    text: str
    encoding: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class SummarySection:
    dimension: SummaryDimension
    source_name: str
    title: str
    content: str


@dataclass(slots=True)
class ImportInspection:
    kind: ImportKind
    title: str
    documents: list[ImportDocument] = field(default_factory=list)
    summaries: dict[SummaryDimension, list[SummarySection]] = field(default_factory=dict)
    project_manifest: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def dimensions_found(self) -> list[SummaryDimension]:
        return [dimension for dimension in SummaryDimension if self.summaries.get(dimension)]


def _clean_dimension_title(title: str) -> str:
    value = title.strip().strip("`*_ ")
    value = re.sub(r"^#{1,6}\s*", "", value)
    value = _NUMBER_PREFIX_RE.sub("", value).strip()
    value = re.sub(r"[（(](?:完全版|精简版|完整版|简版)[)）]$", "", value).strip()
    value = re.sub(r"[-—_:：]\s*(?:完全版|精简版|完整版|简版)$", "", value).strip()
    value = re.sub(r"(?:综合)?分析$", "", value).strip()
    value = re.sub(r"[\s/／、·_\-—]+", "", value)
    return value


def normalize_dimension_title(title: str) -> SummaryDimension | None:
    """Map numbered and unnumbered legacy headings to one stable key."""

    return _TITLE_ALIASES.get(_clean_dimension_title(title))


def dimension_from_filename(filename: str) -> SummaryDimension | None:
    stem = Path(filename).stem
    cleaned = re.sub(r"^《[^》]+》[-_—\s]*", "", stem)
    cleaned = re.sub(r"[-_—\s]*(?:完全版|精简版|完整版|简版|总结\d+.*)$", "", cleaned)
    direct = normalize_dimension_title(cleaned)
    if direct:
        return direct
    compact = _clean_dimension_title(cleaned)
    # Longest aliases first avoids classifying 核心悬念 as a generic 悬念.
    for alias, dimension in sorted(_TITLE_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if alias in compact:
            return dimension
    return None


def extract_nine_dimensions(markdown: str) -> dict[SummaryDimension, str]:
    """Extract recognized sections while retaining unrecognized subheadings."""

    recognized: list[tuple[int, int, SummaryDimension]] = []
    for match in _MARKDOWN_HEADING_RE.finditer(markdown):
        if dimension := normalize_dimension_title(match.group("title")):
            recognized.append((match.start(), match.end(), dimension))

    result: dict[SummaryDimension, str] = {}
    for index, (_start, body_start, dimension) in enumerate(recognized):
        body_end = recognized[index + 1][0] if index + 1 < len(recognized) else len(markdown)
        content = markdown[body_start:body_end].strip()
        if dimension in result and content:
            result[dimension] = f"{result[dimension]}\n\n{content}".strip()
        else:
            result[dimension] = content
    return result


_TABLE_HEADER_ALIASES: dict[SummaryDimension, dict[str, str]] = {
    SummaryDimension.STORY_OUTLINE: {
        "时间": "时间/顺序",
        "时间线": "时间/顺序",
        "顺序": "时间/顺序",
        "人物": "人物/势力",
        "角色": "人物/势力",
        "势力": "人物/势力",
        "高潮": "高潮/转折",
        "转折": "高潮/转折",
    },
    SummaryDimension.CONFLICTS: {
        "冲突表述": "冲突",
        "参与人物": "参与方",
        "人物": "参与方",
        "状态": "当前状态",
        "升级": "升级方式",
    },
    SummaryDimension.CORE_MYSTERIES: {
        "悬念标签": "悬念",
        "悬念对象": "对象",
        "阶段": "揭示阶段",
        "追踪": "后续追踪",
    },
    SummaryDimension.WORLDBUILDING: {
        "名称": "地点",
        "地点名": "地点",
        "人物": "相关人物",
        "作用": "剧情作用",
    },
    SummaryDimension.FORESHADOWING: {
        "伏笔内容": "伏笔",
        "位置": "埋设位置",
        "状态": "当前状态",
        "发展": "可能发展方向",
    },
}


def normalize_table_header(header: str, dimension: SummaryDimension) -> str:
    cleaned = re.sub(r"[`*_\s]", "", header)
    return _TABLE_HEADER_ALIASES.get(dimension, {}).get(cleaned, cleaned)


def parse_markdown_tables(markdown: str, dimension: SummaryDimension) -> list[list[dict[str, str]]]:
    """Parse simple legacy Markdown tables with normalized column labels."""

    lines = markdown.splitlines()
    tables: list[list[dict[str, str]]] = []
    index = 0
    while index + 1 < len(lines):
        header_line = lines[index].strip()
        separator_line = lines[index + 1].strip()
        if "|" not in header_line or not re.fullmatch(
            r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?", separator_line
        ):
            index += 1
            continue
        headers = [
            normalize_table_header(cell.strip(), dimension) for cell in header_line.strip("|").split("|")
        ]
        rows: list[dict[str, str]] = []
        index += 2
        while index < len(lines) and "|" in lines[index]:
            cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            rows.append(dict(zip(headers, cells[: len(headers)], strict=True)))
            index += 1
        tables.append(rows)
    return tables


_CHARACTER_FIELD_LABELS = {
    "主要角色",
    "配角",
    "次要人物",
    "角色",
    "人物",
    "姓名",
    "名称",
    "别名",
    "身份",
    "性格",
    "标签",
    "备注",
    "对应拆分",
    "处理范围",
    "前情上下文",
    "角色定位",
    "角色标签",
    "层级",
    "成长历程",
    "思想发展",
    "能力变化",
    "标志性特征",
    "核心动机",
    "主要冲突",
    "说话习惯",
    "当前状态",
    "地点",
    "目标",
    "知识",
    "能力",
    "情绪",
    "关系",
    "角色分析",
    "人物分析",
}
_VALUE_SPLIT_RE = re.compile(r"\s*(?:、|，|,|；|;|/|／)\s*")


def _uuid_or_none(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except ValueError:
        return None


def _section_evidence(
    inspection: ImportInspection,
    section: SummarySection,
    source_artifact_ids: Mapping[str, UUID | str],
    *,
    assertion_kind: AssertionKind = AssertionKind.INFERENCE,
    confidence: float = 0.72,
    excerpt: str | None = None,
) -> EvidenceV1:
    document = next(
        (item for item in inspection.documents if item.source_name == section.source_name),
        None,
    )
    content = section.content.strip()
    start_offset = document.text.find(content) if document and content else -1
    if start_offset < 0:
        start_offset = 0
        end_offset = len(document.text) if document else len(content)
    else:
        end_offset = start_offset + len(content)
    evidence_excerpt = (excerpt if excerpt is not None else content).strip()
    if len(evidence_excerpt) > 500:
        evidence_excerpt = evidence_excerpt[:497].rstrip() + "…"
    return EvidenceV1(
        span=SourceSpanV1(
            source_artifact_id=_uuid_or_none(source_artifact_ids.get(section.source_name)),
            source_name=section.source_name,
            start_offset=start_offset,
            end_offset=end_offset,
        ),
        excerpt=evidence_excerpt,
        assertion_kind=assertion_kind,
        confidence=confidence,
    )


def _row_value(row: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value and value not in {"-", "—", "无", "暂无", "不详"}:
            return value
    return ""


def _value_list(value: str) -> list[str]:
    return [item for item in _VALUE_SPLIT_RE.split(value.strip()) if item]


def _dimension_archive(
    records: list[tuple[SummarySection, EvidenceV1, StoryFactV1]],
    dimension: SummaryDimension,
) -> str:
    return "\n\n".join(
        f"[来源：{section.source_name}]\n{section.content.strip()}"
        for section, _evidence, _fact in records
        if section.dimension == dimension and section.content.strip()
    )


def _dimension_fact_ids(
    records: list[tuple[SummarySection, EvidenceV1, StoryFactV1]],
    dimension: SummaryDimension,
) -> list[str]:
    return list(
        dict.fromkeys(fact.fact_id for section, _evidence, fact in records if section.dimension == dimension)
    )


def _dimension_rows(
    records: list[tuple[SummarySection, EvidenceV1, StoryFactV1]],
    dimension: SummaryDimension,
) -> Iterable[tuple[dict[str, str], SummarySection, EvidenceV1, StoryFactV1]]:
    for section, evidence, fact in records:
        if section.dimension != dimension:
            continue
        for table in parse_markdown_tables(section.content, dimension):
            for row in table:
                yield row, section, evidence, fact


def _clean_character_name(value: str) -> tuple[str, list[str]] | None:
    raw = re.sub(r"^[\s>*+\-\d.、]+", "", value.strip())
    raw = raw.strip("`*_ #")
    raw = re.sub(r"^(?:主要角色|配角|次要人物)(?:\s*[：:]\s*|\s+)", "", raw)
    canonical = re.sub(r"\s*[（(][^()（）]{0,40}[)）]\s*$", "", raw).strip()
    if (
        not canonical
        or len(canonical) > 40
        or canonical in _CHARACTER_FIELD_LABELS
        or canonical.endswith(("分析", "列表", "档案", "概览"))
        or re.search(r"[|：:，,。；;！？!?]", canonical)
        or canonical.isdigit()
    ):
        return None
    aliases = [raw] if raw != canonical else []
    return canonical, aliases


def _character_candidates(
    records: list[tuple[SummarySection, EvidenceV1, StoryFactV1]],
) -> Iterable[tuple[str, list[str], str, StoryFactV1]]:
    for row, _section, _evidence, fact in _dimension_rows(records, SummaryDimension.CHARACTERS):
        raw_name = _row_value(row, "角色", "角色名", "姓名", "人物", "名称")
        cleaned = _clean_character_name(raw_name)
        if cleaned:
            description = "；".join(f"{key}：{value}" for key, value in row.items() if value)
            yield cleaned[0], cleaned[1], description, fact

    for section, _evidence, fact in records:
        if section.dimension != SummaryDimension.CHARACTERS:
            continue
        candidates: list[tuple[str, str]] = []
        for match in re.finditer(r"(?m)^[ \t]{0,3}#{2,6}[ \t]+([^\r\n#]+)", section.content):
            candidates.append((match.group(1), ""))
        for line in section.content.splitlines():
            match = re.match(
                r"^\s*(?:[-*+]\s*)?(?:\*\*)?([^：:\r\n]{1,40}?)(?:\*\*)?\s*[：:]\s*(.+)$",
                line,
            )
            if match:
                candidates.append((match.group(1), match.group(2).strip()))
        for raw_name, description in candidates:
            cleaned = _clean_character_name(raw_name)
            if cleaned:
                yield cleaned[0], cleaned[1], description, fact


def _conflict_kind(value: str) -> str:
    if "内" in value or "心理" in value:
        return "internal"
    if "外" in value:
        return "external"
    if "次" in value or "支线" in value:
        return "secondary"
    return "core"


def _conflict_status(value: str) -> str:
    if "解决" in value and not any(token in value for token in ("未", "假", "表面")):
        return "resolved"
    if "假" in value or "表面" in value:
        return "false_resolution"
    if "转" in value:
        return "transformed"
    if "压" in value or "抑制" in value:
        return "suppressed"
    return "unresolved"


def _mystery_stage(value: str) -> str:
    if "反转" in value:
        return "reversal"
    if "部分" in value:
        return "partial"
    if "接近" in value or "临近" in value:
        return "approaching"
    if "扩" in value:
        return "expanded"
    if "揭" in value and "未" not in value:
        return "revealed"
    return "setup"


def _foreshadow_status(value: str) -> str:
    if ("完全" in value or "已" in value) and ("回收" in value or "兑现" in value):
        return "paid"
    if "部分" in value and ("回收" in value or "兑现" in value):
        return "partially_paid"
    if "强化" in value or "再次" in value:
        return "reinforced"
    if "废弃" in value or "取消" in value:
        return "abandoned"
    return "planted"


def build_story_state_from_summary(
    inspection: ImportInspection,
    *,
    project_id: UUID,
    source_artifact_ids: Mapping[str, UUID | str],
    branch_name: str = "main",
) -> StoryStateV1 | None:
    """Conservatively adapt a complete nine-dimension summary into StoryStateV1.

    Imported summaries are secondary sources. Their prose is retained verbatim
    as high-level inference facts with source spans; only explicit table cells
    and explicit character labels are promoted into finer structures. Content
    that cannot be safely split remains a clearly labelled archive entry.
    """

    if inspection.kind != ImportKind.SUMMARY_BUNDLE:
        return None
    if any(
        not inspection.summaries.get(dimension)
        or not any(section.content.strip() for section in inspection.summaries[dimension])
        for dimension in SummaryDimension
    ):
        return None

    facts: list[StoryFactV1] = []
    records: list[tuple[SummarySection, EvidenceV1, StoryFactV1]] = []
    fact_by_content: dict[tuple[SummaryDimension, str], StoryFactV1] = {}
    for dimension in SummaryDimension:
        for section in inspection.summaries[dimension]:
            content = section.content.strip()
            if not content:
                continue
            evidence = _section_evidence(inspection, section, source_artifact_ids)
            key = (dimension, content)
            fact = fact_by_content.get(key)
            if fact is None:
                fact = StoryFactV1(
                    category=f"imported_summary.{dimension.value}",
                    statement=content,
                    assertion_kind=AssertionKind.INFERENCE,
                    evidence=[evidence],
                    immutable_canon=False,
                    confidence=0.72,
                )
                fact_by_content[key] = fact
                facts.append(fact)
            elif not any(item.span.source_name == section.source_name for item in fact.evidence):
                fact.evidence.append(evidence)
            records.append((section, evidence, fact))

    timeline: list[TimelineEventV1] = []
    seen_events: set[tuple[str, str]] = set()
    for row, _section, evidence, _fact in _dimension_rows(records, SummaryDimension.STORY_OUTLINE):
        event = _row_value(row, "事件", "事件概述", "大纲")
        if not event:
            continue
        order_key = _row_value(row, "时间/顺序", "时间线", "顺序", "阶段") or "summary-order"
        event_key = (order_key, event)
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        event_type = _row_value(row, "类型", "高潮/转折")
        timeline.append(
            TimelineEventV1(
                order_key=order_key,
                absolute_time=_row_value(row, "绝对时间") or None,
                participants=_value_list(_row_value(row, "人物/势力", "参与人物", "参与方")),
                event=event,
                cause=_row_value(row, "起因", "原因"),
                effect=_row_value(row, "影响", "结果"),
                turning_point=any(token in event_type for token in ("高潮", "转折", "反转")),
                evidence=[evidence],
            )
        )
    if not timeline:
        timeline.append(
            TimelineEventV1(
                order_key="imported-summary-archive",
                event="导入总结的故事大纲档案（未拆分为细粒度事件）：\n"
                + _dimension_archive(records, SummaryDimension.STORY_OUTLINE),
                evidence=[
                    evidence
                    for section, evidence, _fact in records
                    if section.dimension == SummaryDimension.STORY_OUTLINE
                ],
            )
        )

    entities: list[EntityV1] = []
    characters: list[CharacterStateV1] = []
    character_index: dict[str, EntityV1] = {}
    character_fact_ids: dict[str, list[str]] = {}
    for name, aliases, description, fact in _character_candidates(records):
        key = name.casefold()
        entity = character_index.get(key)
        if entity is None:
            entity = EntityV1(
                kind="character",
                canonical_name=name,
                aliases=aliases,
                description=description,
                fact_ids=[fact.fact_id],
            )
            character_index[key] = entity
            character_fact_ids[key] = [fact.fact_id]
            entities.append(entity)
        else:
            entity.aliases = list(dict.fromkeys([*entity.aliases, *aliases]))
            if description and description not in entity.description:
                entity.description = "；".join(item for item in (entity.description, description) if item)
            if fact.fact_id not in entity.fact_ids:
                entity.fact_ids.append(fact.fact_id)
                character_fact_ids[key].append(fact.fact_id)
    for key, entity in character_index.items():
        characters.append(CharacterStateV1(entity_id=entity.entity_id, fact_ids=character_fact_ids[key]))
    if not character_index:
        entities.append(
            EntityV1(
                kind="other",
                canonical_name="导入角色档案",
                description=_dimension_archive(records, SummaryDimension.CHARACTERS),
                fact_ids=_dimension_fact_ids(records, SummaryDimension.CHARACTERS),
            )
        )

    conflicts: list[ConflictStateV1] = []
    for row, _section, _evidence, fact in _dimension_rows(records, SummaryDimension.CONFLICTS):
        description = _row_value(row, "冲突", "冲突表述", "描述")
        if not description:
            continue
        conflicts.append(
            ConflictStateV1(
                description=description,
                kind=_conflict_kind(_row_value(row, "类型", "层级")),
                participants=_value_list(_row_value(row, "参与方", "人物/势力")),
                stakes=_row_value(row, "代价", "赌注", "影响"),
                status=_conflict_status(_row_value(row, "当前状态", "状态")),
                fact_ids=[fact.fact_id],
            )
        )
    if not conflicts:
        conflicts.append(
            ConflictStateV1(
                description="导入总结的冲突档案（未拆分为细粒度冲突）：\n"
                + _dimension_archive(records, SummaryDimension.CONFLICTS),
                kind="core",
                fact_ids=_dimension_fact_ids(records, SummaryDimension.CONFLICTS),
            )
        )

    mysteries: list[MysteryStateV1] = []
    for row, _section, _evidence, _fact in _dimension_rows(records, SummaryDimension.CORE_MYSTERIES):
        label = _row_value(row, "悬念", "悬念标签")
        if not label:
            continue
        mysteries.append(
            MysteryStateV1(
                label=label,
                subject=_row_value(row, "对象", "悬念对象"),
                known_facts=[value] if (value := _row_value(row, "已知信息")) else [],
                clues=[value] if (value := _row_value(row, "新增线索", "线索")) else [],
                reveal_stage=_mystery_stage(_row_value(row, "揭示阶段", "阶段")),
                follow_up=_row_value(row, "后续追踪", "追踪"),
            )
        )
    if not mysteries:
        mysteries.append(
            MysteryStateV1(
                label="导入总结的核心悬念档案（未细分）",
                subject=_dimension_archive(records, SummaryDimension.CORE_MYSTERIES),
            )
        )

    foreshadowing: list[ForeshadowingStateV1] = []
    for row, section, _evidence, fact in _dimension_rows(records, SummaryDimension.FORESHADOWING):
        description = _row_value(row, "伏笔", "伏笔内容")
        if not description:
            continue
        direction = _row_value(row, "可能发展方向", "发展")
        fact_ids = [fact.fact_id]
        if direction:
            speculation = StoryFactV1(
                category="imported_summary.foreshadowing_possible_direction",
                statement=f"{description}｜可能发展方向：{direction}",
                assertion_kind=AssertionKind.SPECULATION,
                evidence=[
                    _section_evidence(
                        inspection,
                        section,
                        source_artifact_ids,
                        assertion_kind=AssertionKind.SPECULATION,
                        confidence=0.5,
                        excerpt=direction,
                    )
                ],
                immutable_canon=False,
                confidence=0.5,
            )
            facts.append(speculation)
            fact_ids.append(speculation.fact_id)
        foreshadowing.append(
            ForeshadowingStateV1(
                description=description,
                planted_at=_row_value(row, "埋设位置", "位置"),
                status=_foreshadow_status(_row_value(row, "当前状态", "状态")),
                possible_directions=[direction] if direction else [],
                fact_ids=fact_ids,
            )
        )
    if not foreshadowing:
        foreshadowing.append(
            ForeshadowingStateV1(
                description="导入总结的伏笔档案（未细分）：\n"
                + _dimension_archive(records, SummaryDimension.FORESHADOWING),
                fact_ids=_dimension_fact_ids(records, SummaryDimension.FORESHADOWING),
            )
        )

    locations: list[LocationStateV1] = []
    seen_locations: set[str] = set()
    for row, _section, _evidence, fact in _dimension_rows(records, SummaryDimension.WORLDBUILDING):
        name = _row_value(row, "地点", "名称", "地点名")
        if not name or name.casefold() in seen_locations:
            continue
        seen_locations.add(name.casefold())
        location_entity = EntityV1(
            kind="location",
            canonical_name=name,
            description="；".join(f"{key}：{value}" for key, value in row.items() if value),
            fact_ids=[fact.fact_id],
        )
        entities.append(location_entity)
        constraint = _row_value(row, "限制", "约束")
        locations.append(
            LocationStateV1(
                location_id=location_entity.entity_id,
                name=name,
                kind=_row_value(row, "类型"),
                constraints=[constraint] if constraint else [],
                fact_ids=[fact.fact_id],
            )
        )

    world_rules = [
        WorldRuleV1(
            statement="导入总结的背景/世界观档案（未拆分为细粒度规则）：\n"
            + _dimension_archive(records, SummaryDimension.WORLDBUILDING),
            fact_ids=_dimension_fact_ids(records, SummaryDimension.WORLDBUILDING),
        )
    ]
    narrative_archive = _dimension_archive(records, SummaryDimension.NARRATIVE_POV)
    person_match = re.search(r"(?:第一|第二|第三)人称(?:全知|限知|有限)?", narrative_archive)
    narrative_profile = NarrativeProfileV1(
        person=person_match.group(0) if person_match else "",
        pov_rules=[narrative_archive],
    )
    pacing_profile = PacingProfileV1(dominant_rhythm=_dimension_archive(records, SummaryDimension.PACING))

    state_source_ids: list[UUID] = []
    for document in inspection.documents:
        artifact_id = _uuid_or_none(source_artifact_ids.get(document.source_name))
        if artifact_id and artifact_id not in state_source_ids:
            state_source_ids.append(artifact_id)
    return StoryStateV1(
        project_id=project_id,
        branch_name=branch_name,
        revision=1,
        facts=facts,
        entities=entities,
        timeline=timeline,
        characters=characters,
        conflicts=conflicts,
        mysteries=mysteries,
        foreshadowing=foreshadowing,
        world_rules=world_rules,
        locations=locations,
        narrative_profile=narrative_profile,
        pacing_profile=pacing_profile,
        source_artifact_ids=state_source_ids,
    )


def _safe_zip_name(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ImportValidationError(f"unsafe ZIP member path: {name}")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ImportValidationError(f"unsafe ZIP member path: {name}")
    return path


def _zip_documents(
    path: Path,
    *,
    max_total_bytes: int,
    max_members: int,
) -> tuple[list[ImportDocument], dict[str, Any] | None]:
    documents: list[ImportDocument] = []
    project_manifest: dict[str, Any] | None = None
    total_bytes = 0
    with zipfile.ZipFile(path) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) > max_members:
            raise ImportValidationError(f"ZIP contains more than {max_members} files")
        for member in members:
            safe_name = _safe_zip_name(member.filename)
            total_bytes += member.file_size
            if total_bytes > max_total_bytes:
                raise ImportValidationError("ZIP uncompressed size exceeds import limit")
            if member.compress_size and member.file_size / member.compress_size > 1_000:
                raise ImportValidationError(f"suspicious ZIP compression ratio: {member.filename}")
            suffix = safe_name.suffix.lower()
            if suffix not in {".txt", ".md", ".json"}:
                continue
            data = archive.read(member)
            if suffix == ".json" and safe_name.name.lower() in {
                "project-manifest.json",
                "export-manifest.json",
                "recovery-manifest.json",
            }:
                try:
                    parsed = json.loads(data.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ImportValidationError(f"invalid project manifest: {safe_name}") from exc
                if not isinstance(parsed, dict):
                    raise ImportValidationError("project manifest must be a JSON object")
                project_manifest = parsed
                continue
            if suffix in {".txt", ".md"}:
                decoded = decode_text_bytes(data)
                documents.append(
                    ImportDocument(safe_name.as_posix(), decoded.text, decoded.encoding, len(data))
                )
    if project_manifest and project_manifest.get("format") == "novel-orchestrator-recovery":
        _validate_recovery_manifest(project_manifest)
    return documents, project_manifest


def _validate_recovery_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != "1.0":
        raise ImportValidationError("unsupported recovery manifest schema version")
    recovery = manifest.get("recovery")
    if not isinstance(recovery, dict):
        raise ImportValidationError("recovery manifest is missing recovery metadata")
    project = recovery.get("project")
    branch = recovery.get("branch")
    if not isinstance(project, dict) or not str(project.get("name") or "").strip():
        raise ImportValidationError("recovery manifest is missing project metadata")
    if not isinstance(branch, dict) or not str(branch.get("name") or "").strip():
        raise ImportValidationError("recovery manifest is missing branch metadata")
    try:
        StoryStateV1.model_validate(branch.get("story_state"))
    except Exception as exc:
        raise ImportValidationError("recovery branch story_state is invalid") from exc
    sources = recovery.get("source_artifacts") or []
    if not isinstance(sources, list):
        raise ImportValidationError("recovery source_artifacts must be a list")
    for item in sources:
        if not isinstance(item, dict):
            raise ImportValidationError("recovery source artifact entry must be an object")
        _safe_zip_name(str(item.get("archive_path") or ""))


def _directory_documents(
    path: Path,
    *,
    max_total_bytes: int,
    max_members: int,
) -> tuple[list[ImportDocument], dict[str, Any] | None]:
    candidates = sorted(item for item in path.rglob("*") if item.is_file())
    if len(candidates) > max_members:
        raise ImportValidationError(f"directory contains more than {max_members} files")
    documents: list[ImportDocument] = []
    project_manifest: dict[str, Any] | None = None
    total_bytes = 0
    for candidate in candidates:
        total_bytes += candidate.stat().st_size
        if total_bytes > max_total_bytes:
            raise ImportValidationError("directory size exceeds import limit")
        relative_name = candidate.relative_to(path).as_posix()
        if candidate.suffix.lower() == ".json" and candidate.name.lower() in {
            "project-manifest.json",
            "export-manifest.json",
            "recovery-manifest.json",
        }:
            try:
                parsed = json.loads(candidate.read_text(encoding="utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ImportValidationError(f"invalid project manifest: {relative_name}") from exc
            if not isinstance(parsed, dict):
                raise ImportValidationError("project manifest must be a JSON object")
            project_manifest = parsed
        elif candidate.suffix.lower() in {".txt", ".md"}:
            decoded = read_text_file(candidate)
            documents.append(
                ImportDocument(relative_name, decoded.text, decoded.encoding, candidate.stat().st_size)
            )
    return documents, project_manifest


def _single_document(path: Path, max_total_bytes: int) -> ImportDocument:
    byte_size = path.stat().st_size
    if byte_size > max_total_bytes:
        raise ImportValidationError("source file exceeds import limit")
    decoded: DecodedText = read_text_file(path)
    return ImportDocument(path.name, decoded.text, decoded.encoding, byte_size)


def _collect_summaries(documents: Iterable[ImportDocument]) -> dict[SummaryDimension, list[SummarySection]]:
    summaries: dict[SummaryDimension, list[SummarySection]] = {}
    for document in documents:
        extracted = extract_nine_dimensions(document.text)
        if extracted:
            for dimension, content in extracted.items():
                summaries.setdefault(dimension, []).append(
                    SummarySection(dimension, document.source_name, DIMENSION_LABELS[dimension], content)
                )
        elif dimension := dimension_from_filename(document.source_name):
            summaries.setdefault(dimension, []).append(
                SummarySection(
                    dimension, document.source_name, DIMENSION_LABELS[dimension], document.text.strip()
                )
            )
    return summaries


def _guess_title(path: Path, documents: list[ImportDocument]) -> str:
    for document in documents:
        match = re.search(r"《([^》]+)》", document.source_name)
        if match:
            return match.group(1).strip()
        match = re.search(r"(?m)^#\s+《?([^\r\n》]+)》?", document.text)
        if match:
            return match.group(1).strip()
    return path.stem if path.is_file() else path.name


def inspect_import(
    source: str | Path,
    *,
    max_total_bytes: int = 512 * 1024 * 1024,
    max_members: int = 5_000,
) -> ImportInspection:
    """Inspect an import without extracting or writing any source files."""

    path = Path(source)
    if not path.exists():
        raise ImportValidationError(f"import source does not exist: {path}")
    project_manifest: dict[str, Any] | None = None
    if path.is_dir():
        documents, project_manifest = _directory_documents(
            path, max_total_bytes=max_total_bytes, max_members=max_members
        )
    elif path.suffix.lower() == ".zip":
        documents, project_manifest = _zip_documents(
            path, max_total_bytes=max_total_bytes, max_members=max_members
        )
    elif path.suffix.lower() in {".txt", ".md"}:
        documents = [_single_document(path, max_total_bytes)]
    else:
        raise ImportValidationError("supported imports are .txt, .md, .zip or a summary directory")

    if project_manifest is not None:
        kind = ImportKind.PROJECT_BUNDLE
    else:
        summaries = _collect_summaries(documents)
        # A batch file containing all nine headings is a summary bundle; a
        # single prose Markdown file with an incidental heading remains raw.
        summary_file_count = sum(dimension_from_filename(item.source_name) is not None for item in documents)
        kind = (
            ImportKind.SUMMARY_BUNDLE
            if len(summaries) >= 5 or summary_file_count >= 5
            else ImportKind.RAW_TEXT
        )
    summaries = _collect_summaries(documents) if kind != ImportKind.PROJECT_BUNDLE else {}
    warnings: list[str] = []
    if kind == ImportKind.SUMMARY_BUNDLE:
        missing = [
            DIMENSION_LABELS[item]
            for item in SummaryDimension
            if not summaries.get(item) or not any(section.content.strip() for section in summaries[item])
        ]
        if missing:
            warnings.append("总结包缺少维度：" + "、".join(missing))
    if not documents and project_manifest is None:
        raise ImportValidationError("import contains no readable .txt or .md documents")
    title = _guess_title(path, documents)
    if project_manifest and project_manifest.get("format") == "novel-orchestrator-recovery":
        title = str(
            ((project_manifest.get("recovery") or {}).get("project") or {}).get("name")
            or project_manifest.get("book_title")
            or title
        )
    return ImportInspection(
        kind=kind,
        title=title,
        documents=documents,
        summaries=summaries,
        project_manifest=project_manifest,
        warnings=warnings,
    )
