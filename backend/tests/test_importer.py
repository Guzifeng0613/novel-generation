import zipfile
from uuid import uuid4

import pytest

from backend.app.importer import (
    ImportKind,
    ImportValidationError,
    build_story_state_from_summary,
    extract_nine_dimensions,
    inspect_import,
    normalize_dimension_title,
    parse_markdown_tables,
)
from backend.app.schemas import AssertionKind, SummaryDimension

NINE_SECTION_DOCUMENT = """# 《无名城》-总结001

## 一、核心摘要
主角抵达无名城。

## 故事大纲
| 时间线 | 事件 | 影响 |
|---|---|---|
| 夜晚 | 抵达 | 被监视 |

## 3. 角色分析
林岚：主角。

## 四、冲突
生存与真相。

## 五、核心悬念分析
城门为何只进不出。

## 6、背景／世界观分析
城外终年落雪。

## 七、叙事视角
第三人称限知。

## 八、节奏控制
先慢后快。

## 9、伏笔追踪
坏掉的怀表。
"""


def test_legacy_headings_with_or_without_numbers_are_normalized() -> None:
    assert normalize_dimension_title("一、核心摘要") == SummaryDimension.CORE_SUMMARY
    assert normalize_dimension_title("## 2. 故事大纲") == SummaryDimension.STORY_OUTLINE
    assert normalize_dimension_title("背景/世界观分析") == SummaryDimension.WORLDBUILDING
    assert normalize_dimension_title("九、伏笔追踪") == SummaryDimension.FORESHADOWING
    sections = extract_nine_dimensions(NINE_SECTION_DOCUMENT)
    assert set(sections) == set(SummaryDimension)
    assert sections[SummaryDimension.CHARACTERS] == "林岚：主角。"


def test_legacy_table_headers_are_normalized() -> None:
    tables = parse_markdown_tables(
        "| 时间线 | 人物 | 事件 | 高潮 |\n|---|---|---|---|\n| 深夜 | 林岚 | 入城 | 是 |",
        SummaryDimension.STORY_OUTLINE,
    )
    assert tables == [[{"时间/顺序": "深夜", "人物/势力": "林岚", "事件": "入城", "高潮/转折": "是"}]]


def test_inspects_full_summary_file_as_bundle(tmp_path) -> None:
    source = tmp_path / "summary.md"
    source.write_text(NINE_SECTION_DOCUMENT, encoding="utf-8")
    inspection = inspect_import(source)
    assert inspection.kind == ImportKind.SUMMARY_BUNDLE
    assert inspection.title == "无名城"
    assert len(inspection.dimensions_found) == 9


def test_complete_summary_bundle_builds_conservative_story_state(tmp_path) -> None:
    source = tmp_path / "summary.md"
    structured = NINE_SECTION_DOCUMENT.replace(
        "坏掉的怀表。",
        "| 伏笔 | 埋设位置 | 当前状态 | 可能发展方向 |\n"
        "|---|---|---|---|\n"
        "| 坏掉的怀表 | 入城时 | 已埋设 | 可能指向失踪的守门人 |",
    )
    source.write_text(structured, encoding="utf-8")
    inspection = inspect_import(source)
    project_id = uuid4()
    source_artifact_id = uuid4()

    state = build_story_state_from_summary(
        inspection,
        project_id=project_id,
        source_artifact_ids={inspection.documents[0].source_name: source_artifact_id},
    )

    assert state is not None
    assert state.project_id == project_id
    assert state.branch_name == "main"
    assert state.source_artifact_ids == [source_artifact_id]
    assert {
        fact.category.removeprefix("imported_summary.")
        for fact in state.facts
        if fact.category.startswith("imported_summary.")
    } >= {dimension.value for dimension in SummaryDimension}
    assert all(fact.immutable_canon is False for fact in state.facts)
    assert {fact.assertion_kind for fact in state.facts} >= {
        AssertionKind.INFERENCE,
        AssertionKind.SPECULATION,
    }
    assert all(fact.evidence for fact in state.facts)
    first_span = state.facts[0].evidence[0].span
    assert first_span.source_artifact_id == source_artifact_id
    assert first_span.source_name == "summary.md"
    assert first_span.end_offset > first_span.start_offset
    assert any(item.event == "抵达" for item in state.timeline)
    assert any(item.canonical_name == "林岚" and item.kind == "character" for item in state.entities)
    assert state.characters
    assert state.conflicts and "未拆分" in state.conflicts[0].description
    assert state.mysteries and "核心悬念档案" in state.mysteries[0].label
    assert state.foreshadowing[0].possible_directions == ["可能指向失踪的守门人"]
    assert state.world_rules and "未拆分为细粒度规则" in state.world_rules[0].statement
    assert state.narrative_profile.person == "第三人称限知"
    assert "先慢后快" in state.pacing_profile.dominant_rhythm


def test_incomplete_summary_bundle_does_not_claim_complete_story_state(tmp_path) -> None:
    source = tmp_path / "summary.md"
    source.write_text(NINE_SECTION_DOCUMENT.replace("## 9、伏笔追踪\n坏掉的怀表。", ""), encoding="utf-8")
    inspection = inspect_import(source)

    state = build_story_state_from_summary(
        inspection,
        project_id=uuid4(),
        source_artifact_ids={inspection.documents[0].source_name: uuid4()},
    )

    assert inspection.kind == ImportKind.SUMMARY_BUNDLE
    assert inspection.warnings == ["总结包缺少维度：伏笔追踪"]
    assert state is None


def test_detects_recoverable_project_zip_without_extracting(tmp_path) -> None:
    archive_path = tmp_path / "project.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("project-manifest.json", '{"schema_version":"1.0","project_id":"p1"}')
        archive.writestr("sources/book.txt", "正文")
    inspection = inspect_import(archive_path)
    assert inspection.kind == ImportKind.PROJECT_BUNDLE
    assert inspection.project_manifest == {"schema_version": "1.0", "project_id": "p1"}


def test_rejects_zip_path_traversal(tmp_path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside.txt", "bad")
    with pytest.raises(ImportValidationError, match="unsafe ZIP"):
        inspect_import(archive_path)
