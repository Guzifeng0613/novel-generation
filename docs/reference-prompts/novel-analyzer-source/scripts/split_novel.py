#!/usr/bin/env python3
"""Split long Chinese novel plaintext into chapter-aware Markdown parts."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ENCODINGS = ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-8", "gb18030")
FORBIDDEN_FILENAME_CHARS = '<>:"/\\|?*'


CHAPTER_PATTERNS = [
    re.compile(r"^\s*#{1,6}\s+(.{1,120})\s*$"),
    re.compile(
        r"^\s*((?:第\s*[0-9一二三四五六七八九十百千万零〇两]+\s*[章节卷部集篇回幕]"
        r"|[上中下]\s*卷|开\s*篇|序\s*幕|序\s*章|序言|楔子|引子|尾声|后记|番外)"
        r"[^\n]{0,100})\s*$"
    ),
    re.compile(r"^\s*((?:Chapter|CHAPTER)\s+\d+[^\n]{0,100})\s*$"),
]


@dataclass
class Segment:
    title: str
    text: str
    start_offset: int
    end_offset: int

    @property
    def char_count(self) -> int:
        return count_non_whitespace(self.text)


@dataclass
class Chunk:
    index: int
    segments: list[Segment]
    warning: str | None = None

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self.segments)

    @property
    def char_count(self) -> int:
        return count_non_whitespace(self.text)

    @property
    def chapter_start(self) -> str:
        return self.segments[0].title if self.segments else "全文"

    @property
    def chapter_end(self) -> str:
        return self.segments[-1].title if self.segments else "全文"


def read_text(path: Path) -> tuple[str, str]:
    last_error: UnicodeDecodeError | None = None
    for encoding in ENCODINGS:
        try:
            return path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError as error:
            last_error = error
    if last_error:
        raise last_error
    raise ValueError(f"Unable to read {path}")


def count_non_whitespace(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def clean_heading(line: str) -> str:
    stripped = line.strip()
    stripped = re.sub(r"^#{1,6}\s+", "", stripped)
    return re.sub(r"\s+", " ", stripped).strip() or "未命名章节"


def match_heading(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 140:
        return None
    for pattern in CHAPTER_PATTERNS:
        match = pattern.match(stripped)
        if match:
            return clean_heading(match.group(1))
    return None


def detect_chapter_segments(text: str) -> list[Segment]:
    headings: list[tuple[int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        heading = match_heading(line)
        if heading:
            headings.append((offset, heading))
        offset += len(line)

    if len(headings) < 2:
        return []

    if headings[0][0] > 0 and count_non_whitespace(text[: headings[0][0]]) > 0:
        headings.insert(0, (0, "开篇"))

    segments: list[Segment] = []
    for index, (start, title) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        segment_text = text[start:end]
        if count_non_whitespace(segment_text) > 0:
            segments.append(Segment(title=title, text=segment_text, start_offset=start, end_offset=end))
    return segments


def fallback_segments(text: str, max_chars: int) -> list[Segment]:
    segments: list[Segment] = []
    current: list[str] = []
    current_count = 0
    start_offset = 0
    offset = 0
    part_index = 1

    for line in text.splitlines(keepends=True):
        line_count = count_non_whitespace(line)
        if current and current_count + line_count > max_chars:
            segment_text = "".join(current)
            segments.append(
                Segment(
                    title=f"文本段落{part_index:03d}",
                    text=segment_text,
                    start_offset=start_offset,
                    end_offset=offset,
                )
            )
            part_index += 1
            current = []
            current_count = 0
            start_offset = offset
        current.append(line)
        current_count += line_count
        offset += len(line)

    if current:
        segments.append(
            Segment(
                title=f"文本段落{part_index:03d}",
                text="".join(current),
                start_offset=start_offset,
                end_offset=len(text),
            )
        )

    return segments or [Segment(title="全文", text=text, start_offset=0, end_offset=len(text))]


def build_chunks(segments: list[Segment], min_chars: int, target_chars: int, max_chars: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[Segment] = []
    current_count = 0

    def flush(warning: str | None = None) -> None:
        nonlocal current, current_count
        if current:
            chunks.append(Chunk(index=len(chunks) + 1, segments=current, warning=warning))
            current = []
            current_count = 0

    for segment in segments:
        segment_count = segment.char_count
        if segment_count > max_chars:
            if current:
                flush()
            chunks.append(
                Chunk(
                    index=len(chunks) + 1,
                    segments=[segment],
                    warning=f"Single segment exceeds max size: {segment.title}",
                )
            )
            continue

        if current:
            combined = current_count + segment_count
            should_split = (
                current_count >= min_chars
                and (combined > max_chars or current_count >= target_chars)
            )
            if should_split:
                flush()

        current.append(segment)
        current_count += segment_count

    flush()

    if len(chunks) > 1 and chunks[-1].char_count < min_chars:
        previous = chunks[-2]
        last = chunks[-1]
        if previous.char_count + last.char_count <= max_chars:
            previous.segments.extend(last.segments)
            if last.warning:
                previous.warning = last.warning
            chunks.pop()

    for index, chunk in enumerate(chunks, start=1):
        chunk.index = index
    return chunks


def sanitize_filename_part(value: str, fallback: str) -> str:
    cleaned = "".join("_" if char in FORBIDDEN_FILENAME_CHARS else char for char in value)
    cleaned = "".join(char for char in cleaned if ord(char) >= 32)
    cleaned = re.sub(r"\s+", "", cleaned).strip(". ")
    return cleaned or fallback


def infer_title(path: Path, explicit_title: str | None) -> str:
    title = explicit_title or path.stem
    return sanitize_filename_part(title, "未命名小说")


def write_chunk_file(out_dir: Path, title: str, chunk: Chunk, source_path: Path) -> Path:
    filename = f"《{title}》-拆分{chunk.index:03d}-待总结.md"
    output_path = out_dir / filename
    chapter_range = chunk.chapter_start if chunk.chapter_start == chunk.chapter_end else f"{chunk.chapter_start} - {chunk.chapter_end}"
    warning_line = f"- 注意：{chunk.warning}\n" if chunk.warning else ""
    markdown = (
        f"# 《{title}》-拆分{chunk.index:03d}-待总结\n\n"
        f"- 源文件：`{source_path}`\n"
        f"- 拆分编号：{chunk.index:03d}\n"
        f"- 章节范围：{chapter_range}\n"
        f"- 字数估算：{chunk.char_count}\n"
        f"{warning_line}"
        f"- 标签状态：待总结后回填\n\n"
        "---\n\n"
        f"{chunk.text}"
    )
    output_path.write_text(markdown, encoding="utf-8", newline="\n")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split a long Chinese novel into Markdown parts.")
    parser.add_argument("input", type=Path, help="Path to a .txt or .md novel file.")
    parser.add_argument("--out-dir", type=Path, default=None, help="Output directory. Defaults to input parent/输出拆分.")
    parser.add_argument("--title", default=None, help="Override article title used in output filenames.")
    parser.add_argument("--min", dest="min_chars", type=int, default=150000, help="Preferred minimum characters per part.")
    parser.add_argument("--target", dest="target_chars", type=int, default=200000, help="Target characters per part.")
    parser.add_argument("--max", dest="max_chars", type=int, default=250000, help="Preferred maximum characters per part.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve()
    if input_path.suffix.lower() not in {".txt", ".md"}:
        raise SystemExit("Only .txt and .md are supported directly. Extract other formats to text first.")
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")
    if not (0 < args.min_chars <= args.target_chars <= args.max_chars):
        raise SystemExit("--min, --target, and --max must satisfy 0 < min <= target <= max.")

    text, encoding = read_text(input_path)
    title = infer_title(input_path, args.title)
    out_dir = (args.out_dir or input_path.parent / title / "输出拆分").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    segments = detect_chapter_segments(text)
    used_fallback = False
    if not segments:
        segments = fallback_segments(text, args.max_chars)
        used_fallback = True

    chunks = build_chunks(segments, args.min_chars, args.target_chars, args.max_chars)
    files = [write_chunk_file(out_dir, title, chunk, input_path) for chunk in chunks]

    manifest = {
        "source": str(input_path),
        "title": title,
        "encoding": encoding,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counting_rule": "non-whitespace Unicode characters",
        "split_settings": {
            "min_chars": args.min_chars,
            "target_chars": args.target_chars,
            "max_chars": args.max_chars,
        },
        "chapter_detection": {
            "mode": "fallback_paragraphs" if used_fallback else "chapter_headings",
            "segment_count": len(segments),
        },
        "chunks": [
            {
                "index": chunk.index,
                "file": str(path),
                "char_count": chunk.char_count,
                "chapter_start": chunk.chapter_start,
                "chapter_end": chunk.chapter_end,
                "warning": chunk.warning,
            }
            for chunk, path in zip(chunks, files)
        ],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Split complete: {len(chunks)} file(s)")
    print(f"Output directory: {out_dir}")
    print(f"Manifest: {out_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
