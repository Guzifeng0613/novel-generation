"""Chinese novel decoding and boundary-aware splitting utilities."""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass
from pathlib import Path


class TextDecodingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DecodedText:
    text: str
    encoding: str


@dataclass(frozen=True, slots=True)
class ChapterBoundary:
    offset: int
    title: str


@dataclass(frozen=True, slots=True)
class TextChunk:
    index: int
    start_offset: int
    end_offset: int
    text: str
    heading: str | None
    non_whitespace_chars: int
    estimated_tokens: int


_CHINESE_NUMBER = r"〇零一二三四五六七八九十百千万两廿卅\d０-９"
_CHAPTER_TITLE_RE = re.compile(
    rf"(?m)^[ \t]{{0,3}}(?P<title>"
    rf"(?:第[{_CHINESE_NUMBER}]{{1,16}}[章节卷部回集篇幕]|序章|楔子|引子|前言|尾声|终章|后记)"
    rf"(?:[ \t　:：·—-]+[^\r\n]{{0,100}})?"
    rf")[ \t　]*\r?$"
)
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,3}[ \t]+(?P<title>[^\r\n#].*?)[ \t]*#*[ \t]*\r?$")


def _looks_like_utf16(data: bytes) -> str | None:
    if len(data) < 4 or len(data) % 2:
        return None
    even_zeros = data[0::2].count(0) / (len(data) / 2)
    odd_zeros = data[1::2].count(0) / (len(data) / 2)
    if odd_zeros >= 0.25 and odd_zeros > even_zeros * 2:
        return "utf-16-le"
    if even_zeros >= 0.25 and even_zeros > odd_zeros * 2:
        return "utf-16-be"
    return None


def decode_text_bytes(data: bytes) -> DecodedText:
    """Decode UTF-8, UTF-16 or GB18030 without lossy replacement."""

    if data.startswith(b"\xef\xbb\xbf"):
        return DecodedText(data.decode("utf-8-sig"), "utf-8-sig")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return DecodedText(data.decode("utf-16"), "utf-16")

    if utf16_encoding := _looks_like_utf16(data):
        try:
            return DecodedText(data.decode(utf16_encoding), utf16_encoding)
        except UnicodeDecodeError:
            pass

    failures: list[str] = []
    for encoding in ("utf-8", "gb18030"):
        try:
            return DecodedText(data.decode(encoding), encoding)
        except UnicodeDecodeError as exc:
            failures.append(f"{encoding}: byte {exc.start}")
    raise TextDecodingError("cannot decode source as UTF-8, UTF-16 or GB18030; " + "; ".join(failures))


def read_text_file(path: str | Path) -> DecodedText:
    return decode_text_bytes(Path(path).read_bytes())


def count_non_whitespace(text: str) -> int:
    return sum(not char.isspace() for char in text)


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate suitable for chunk budgeting.

    CJK characters are counted one-for-one. Other content is estimated from
    UTF-8 bytes at four bytes per token. Provider adapters still perform their
    final model-specific budget check before sending a request.
    """

    cjk_count = 0
    other_bytes = 0
    for char in text:
        codepoint = ord(char)
        if (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
            or 0x3040 <= codepoint <= 0x30FF
            or 0xAC00 <= codepoint <= 0xD7AF
        ):
            cjk_count += 1
        else:
            other_bytes += len(char.encode("utf-8"))
    return cjk_count + math.ceil(other_bytes / 4)


def find_chapter_boundaries(text: str) -> list[ChapterBoundary]:
    found: dict[int, str] = {}
    for pattern in (_CHAPTER_TITLE_RE, _MARKDOWN_HEADING_RE):
        for match in pattern.finditer(text):
            title = match.group("title").strip().strip("#").strip()
            found.setdefault(match.start(), title)
    return [ChapterBoundary(offset, found[offset]) for offset in sorted(found)]


def _paragraph_boundaries(text: str) -> list[int]:
    return [match.end() for match in re.finditer(r"(?:\r?\n)[ \t　]*(?:\r?\n)+", text)]


def _line_boundaries(text: str) -> list[int]:
    return [match.end() for match in re.finditer(r"\r?\n", text)]


def _offset_after_non_whitespace(text: str, start: int, amount: int) -> int:
    if amount <= 0:
        return start
    count = 0
    for index in range(start, len(text)):
        if not text[index].isspace():
            count += 1
            if count >= amount:
                return index + 1
    return len(text)


def _boundaries_between(boundaries: list[int], lower: int, upper: int) -> list[int]:
    left = bisect.bisect_left(boundaries, lower)
    right = bisect.bisect_right(boundaries, upper)
    return boundaries[left:right]


def _closest_boundary(boundaries: list[int], target: int) -> int:
    return min(boundaries, key=lambda offset: (abs(offset - target), -offset))


def _heading_at(boundaries: list[ChapterBoundary], start: int) -> str | None:
    offsets = [boundary.offset for boundary in boundaries]
    index = bisect.bisect_right(offsets, start) - 1
    return boundaries[index].title if index >= 0 else None


def _make_chunk(index: int, start: int, end: int, text: str, headings: list[ChapterBoundary]) -> TextChunk:
    content = text[start:end]
    return TextChunk(
        index=index,
        start_offset=start,
        end_offset=end,
        text=content,
        heading=_heading_at(headings, start),
        non_whitespace_chars=count_non_whitespace(content),
        estimated_tokens=estimate_tokens(content),
    )


def split_archive_chunks(
    text: str,
    *,
    target_chars: int = 200_000,
    min_chars: int = 150_000,
    max_chars: int = 250_000,
) -> list[TextChunk]:
    """Split archival source parts, preferring chapters then paragraphs/lines."""

    if not (0 < min_chars <= target_chars <= max_chars):
        raise ValueError("expected 0 < min_chars <= target_chars <= max_chars")
    if not text:
        return []

    headings = find_chapter_boundaries(text)
    chapter_offsets = [item.offset for item in headings if item.offset > 0]
    paragraph_offsets = _paragraph_boundaries(text)
    line_offsets = _line_boundaries(text)
    chunks: list[TextChunk] = []
    start = 0

    while start < len(text):
        remaining = count_non_whitespace(text[start:])
        if remaining <= max_chars:
            end = len(text)
        else:
            lower = _offset_after_non_whitespace(text, start, min_chars)
            target = _offset_after_non_whitespace(text, start, target_chars)
            upper = _offset_after_non_whitespace(text, start, max_chars)
            candidates = _boundaries_between(chapter_offsets, lower, upper)
            if not candidates:
                candidates = _boundaries_between(paragraph_offsets, lower, upper)
            if not candidates:
                candidates = _boundaries_between(line_offsets, lower, upper)
            end = _closest_boundary(candidates, target) if candidates else upper
        if end <= start:  # defensive progress guarantee for malformed boundary input
            end = min(len(text), start + 1)
        chunks.append(_make_chunk(len(chunks), start, end, text, headings))
        start = end
    return chunks


def model_input_token_budget(
    *,
    context_window_tokens: int,
    output_reserve_tokens: int,
    prompt_tokens: int,
    safety_margin: float = 0.10,
) -> int:
    if context_window_tokens <= 0:
        raise ValueError("context_window_tokens must be positive")
    if output_reserve_tokens < 0 or prompt_tokens < 0:
        raise ValueError("token reserves cannot be negative")
    if not 0 <= safety_margin < 1:
        raise ValueError("safety_margin must be in [0, 1)")
    safety_tokens = math.ceil(context_window_tokens * safety_margin)
    budget = context_window_tokens - output_reserve_tokens - prompt_tokens - safety_tokens
    if budget <= 0:
        raise ValueError("model context leaves no room for source text")
    return budget


def _max_end_for_tokens(text: str, start: int, token_budget: int) -> int:
    low = start + 1
    high = len(text)
    best = start
    while low <= high:
        middle = (low + high) // 2
        if estimate_tokens(text[start:middle]) <= token_budget:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def split_model_chunks(
    text: str,
    *,
    context_window_tokens: int,
    output_reserve_tokens: int,
    prompt_tokens: int,
    safety_margin: float = 0.10,
) -> list[TextChunk]:
    """Apply a second, API-sized split without dropping source offsets."""

    if not text:
        return []
    budget = model_input_token_budget(
        context_window_tokens=context_window_tokens,
        output_reserve_tokens=output_reserve_tokens,
        prompt_tokens=prompt_tokens,
        safety_margin=safety_margin,
    )
    headings = find_chapter_boundaries(text)
    chapter_offsets = [item.offset for item in headings if item.offset > 0]
    paragraph_offsets = _paragraph_boundaries(text)
    line_offsets = _line_boundaries(text)
    chunks: list[TextChunk] = []
    start = 0

    while start < len(text):
        if estimate_tokens(text[start:]) <= budget:
            end = len(text)
        else:
            raw_end = _max_end_for_tokens(text, start, budget)
            if raw_end <= start:
                raise ValueError("a single source character exceeds the model input token budget")
            minimum = start + max(1, (raw_end - start) // 2)
            candidates = _boundaries_between(chapter_offsets, minimum, raw_end)
            if not candidates:
                candidates = _boundaries_between(paragraph_offsets, minimum, raw_end)
            if not candidates:
                candidates = _boundaries_between(line_offsets, minimum, raw_end)
            end = candidates[-1] if candidates else raw_end
        chunk = _make_chunk(len(chunks), start, end, text, headings)
        if chunk.estimated_tokens > budget:
            raise AssertionError("splitter produced an over-budget chunk")
        chunks.append(chunk)
        start = end
    return chunks
