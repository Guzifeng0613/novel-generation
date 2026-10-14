"""Deterministic, atomic run deliverables."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class ChapterExport:
    chapter_id: str
    title: str
    text: str
    sequence: int


@dataclass(slots=True)
class ExportResult:
    root: Path
    files: dict[str, Path]
    sha256: dict[str, str]


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: _plain(item) for key, item in vars(value).items() if not key.startswith("_")}
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, content: bytes, *, immutable: bool = True) -> Path:
    """Write a file atomically and reject attempts to mutate an artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == content:
            return path
        if immutable:
            raise FileExistsError(f"不可变产物已存在且内容不同：{path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def atomic_write_text(path: Path, content: str, *, immutable: bool = True) -> Path:
    return atomic_write_bytes(path, content.encode("utf-8"), immutable=immutable)


def render_plan_markdown(plan: Any) -> str:
    data = _plain(plan)
    lines = ["# 本轮章纲", ""]
    start = data.get("start_point", "") if isinstance(data, Mapping) else ""
    target = data.get("target_chars", "") if isinstance(data, Mapping) else ""
    lines.extend((f"- 续写起点：{start}", f"- 目标字符数：{target}", ""))
    conflicts = data.get("canon_conflicts", []) if isinstance(data, Mapping) else []
    if conflicts:
        lines.extend(("## Canon 冲突", ""))
        lines.extend(f"- {item}" for item in conflicts)
        lines.append("")
    lines.extend(
        (
            "## 分章计划",
            "",
            "| 章序 | 标题 | 目标字符 | POV | 地点 | 核心事件 | 章末钩子 |",
            "|---:|---|---:|---|---|---|---|",
        )
    )
    briefs = data.get("chapter_briefs", []) if isinstance(data, Mapping) else []
    for index, item in enumerate(briefs, 1):
        item = _plain(item)
        lines.append(
            "| {seq} | {title} | {chars} | {pov} | {location} | {event} | {hook} |".format(
                seq=item.get("sequence", item.get("chapter_no", index)),
                title=_md_cell(item.get("title", "")),
                chars=item.get("target_chars", ""),
                pov=_md_cell(item.get("pov", "")),
                location=_md_cell(item.get("location", "")),
                event=_md_cell(item.get("core_event", item.get("summary", ""))),
                hook=_md_cell(item.get("ending_hook", item.get("chapter_hook", ""))),
            )
        )
    style = data.get("style_card") if isinstance(data, Mapping) else None
    mystery = data.get("mystery_plan") if isinstance(data, Mapping) else None
    if style:
        lines.extend(("", "## 风格卡", "", _render_unknown_markdown(style)))
    if mystery:
        lines.extend(("", "## 悬念计划", "", _render_unknown_markdown(mystery)))
    return "\n".join(lines).rstrip() + "\n"


def _md_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", "<br>")


def _render_unknown_markdown(value: Any) -> str:
    value = _plain(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(f"- **{key}**：{_inline(item)}" for key, item in value.items())
    if isinstance(value, Sequence):
        return "\n".join(f"- {_inline(item)}" for item in value)
    return str(value)


def _inline(value: Any) -> str:
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(_plain(value), ensure_ascii=False, separators=(",", ":"))
    return str(value)


def render_qa_markdown(qa: Any) -> str:
    data = _plain(qa)
    lines = ["# 本轮质量检查报告", ""]
    reports = data if isinstance(data, list) else data.get("chapters", [data])
    for index, report in enumerate(reports, 1):
        report = _plain(report)
        chapter_id = report.get("chapter_id", f"chapter-{index}")
        passed = bool(report.get("passed"))
        lines.extend(
            (
                f"## {chapter_id} — {'通过' if passed else '未通过'}",
                "",
                f"- 加权分：{report.get('weighted_score', 0)}",
                f"- Blocker：{report.get('blocker_count', 0)}",
                f"- Major：{report.get('major_count', 0)}",
                "",
                "| 维度 | 分数 | 摘要 |",
                "|---|---:|---|",
            )
        )
        dimensions = report.get("dimensions", report.get("dimension_results", {}))
        if isinstance(dimensions, list):
            dimensions = {str(item.get("key", item.get("dimension", "unknown"))): item for item in dimensions}
        for key, dimension in dimensions.items():
            dimension = _plain(dimension)
            summary = _md_cell(dimension.get("summary", ""))
            lines.append(f"| {_md_cell(key)} | {dimension.get('score', 0)} | {summary} |")
        issues = report.get("issues") or [
            issue for dimension in dimensions.values() for issue in _plain(dimension).get("issues", [])
        ]
        if issues:
            lines.extend(("", "### 问题", ""))
            for issue in issues:
                issue = _plain(issue)
                lines.append(
                    "- `{id}` [{severity}] {location}：{instruction}".format(
                        id=issue.get("issue_id", "unknown"),
                        severity=issue.get("severity", "unknown"),
                        location=issue.get("location", ""),
                        instruction=issue.get("instruction", ""),
                    )
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _zip_files(destination: Path, members: Iterable[tuple[Path, str]]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for source, archive_name in sorted(members, key=lambda item: item[1]):
                safe_name = archive_name.replace("\\", "/").lstrip("/")
                if ".." in Path(safe_name).parts:
                    raise ValueError(f"ZIP 路径越界：{archive_name}")
                info = zipfile.ZipInfo(safe_name)
                # Fixed timestamp makes retries byte-for-byte deterministic.
                info.date_time = (2020, 1, 1, 0, 0, 0)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, source.read_bytes())
        content = temporary.read_bytes()
        atomic_write_bytes(destination, content, immutable=True)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def build_delivery_bundle(
    output_dir: Path | str,
    *,
    book_title: str,
    plan: Any,
    chapters: Sequence[ChapterExport],
    mystery_ledger: Any,
    qa_report: Any,
    memory_snapshot: Any,
    recovery_files: Sequence[tuple[Path | str, str]] = (),
    recovery_metadata: Mapping[str, Any] | None = None,
) -> ExportResult:
    """Create all required final artifacts after the caller's global gate passes."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    ordered = sorted(chapters, key=lambda item: item.sequence)
    markdown_parts = [f"# {book_title}（续写）", ""]
    text_parts = [f"{book_title}（续写）", ""]
    chapter_members: list[tuple[Path, str]] = []
    chapter_dir = root / "章节"
    for chapter in ordered:
        text = chapter.text.strip() + "\n"
        if not text.lstrip().startswith(("#", "第")):
            text = f"# {chapter.title}\n\n{text}"
        markdown_parts.append(text.rstrip())
        markdown_parts.append("")
        plain = re_sub_markdown_heading(text)
        text_parts.append(plain.rstrip())
        text_parts.append("")
        chapter_path = atomic_write_text(chapter_dir / f"第{chapter.sequence:03d}章.md", text)
        chapter_members.append((chapter_path, f"章节/{chapter_path.name}"))

    files: dict[str, Path] = {
        "combined_markdown": atomic_write_text(
            root / "本轮正文-合并版.md", "\n".join(markdown_parts).rstrip() + "\n"
        ),
        "combined_text": atomic_write_text(
            root / "本轮正文-合并版.txt", "\n".join(text_parts).rstrip() + "\n"
        ),
        "plan": atomic_write_text(root / "本轮章纲.md", render_plan_markdown(plan)),
        "mystery_ledger": atomic_write_text(
            root / "本轮悬念台账.md",
            "# 本轮悬念台账\n\n" + _render_unknown_markdown(mystery_ledger) + "\n",
        ),
        "qa": atomic_write_text(root / "本轮质量检查报告.md", render_qa_markdown(qa_report)),
        "memory": atomic_write_text(
            root / "续写记忆快照.md",
            "# 续写记忆快照\n\n" + _render_unknown_markdown(memory_snapshot) + "\n",
        ),
    }
    machine_manifest = {
        "schema_version": "1.0",
        "kind": "recoverable_project",
        "format": "novel-orchestrator-recovery",
        "book_title": book_title,
        "plan": _plain(plan),
        "chapters": [
            {
                "chapter_id": chapter.chapter_id,
                "sequence": chapter.sequence,
                "title": chapter.title,
                "sha256": hashlib.sha256(chapter.text.encode("utf-8")).hexdigest(),
            }
            for chapter in ordered
        ],
        "mystery_ledger": _plain(mystery_ledger),
        "qa_report": _plain(qa_report),
        "memory_snapshot": _plain(memory_snapshot),
        "recovery": _plain(recovery_metadata or {}),
        "recovery_capabilities": {
            "restores": [
                "project_metadata",
                "active_branch_story_state",
                "source_artifacts",
                "delivery_artifacts",
            ],
            "omits": ["run_history", "task_history", "model_call_history"],
        },
        "artifact_index": [
            {
                "archive_path": path.name,
                "sha256": sha256_file(path),
                "byte_size": path.stat().st_size,
            }
            for path in files.values()
        ]
        + [
            {
                "archive_path": archive_name,
                "sha256": sha256_file(path),
                "byte_size": path.stat().st_size,
            }
            for path, archive_name in chapter_members
        ]
        + [
            {
                "archive_path": archive_name.replace("\\", "/").lstrip("/"),
                "sha256": sha256_file(Path(path)),
                "byte_size": Path(path).stat().st_size,
            }
            for path, archive_name in recovery_files
            if Path(path).is_file()
        ],
    }
    files["manifest"] = atomic_write_text(
        root / "recovery-manifest.json",
        json.dumps(machine_manifest, ensure_ascii=False, indent=2) + "\n",
    )
    reading_members = [
        (files["combined_markdown"], files["combined_markdown"].name),
        (files["combined_text"], files["combined_text"].name),
        (files["plan"], files["plan"].name),
        (files["mystery_ledger"], files["mystery_ledger"].name),
        (files["qa"], files["qa"].name),
        (files["memory"], files["memory"].name),
        *chapter_members,
    ]
    files["reading_zip"] = _zip_files(root / "阅读包.zip", reading_members)
    recovery_members = [*reading_members, (files["manifest"], files["manifest"].name)]
    for source, archive_name in recovery_files:
        source_path = Path(source)
        if source_path.is_file():
            recovery_members.append((source_path, archive_name))
    files["recovery_zip"] = _zip_files(root / "可恢复项目包.zip", recovery_members)
    return ExportResult(
        root=root,
        files=files,
        sha256={name: sha256_file(path) for name, path in files.items()},
    )


def re_sub_markdown_heading(text: str) -> str:
    return "\n".join(re_sub_heading_line(line) for line in text.splitlines()) + "\n"


def re_sub_heading_line(line: str) -> str:
    index = 0
    while index < len(line) and line[index] == "#":
        index += 1
    if index and index < len(line) and line[index].isspace():
        return line[index:].lstrip()
    return line
