from backend.app.splitter import (
    count_non_whitespace,
    decode_text_bytes,
    find_chapter_boundaries,
    model_input_token_budget,
    split_archive_chunks,
    split_model_chunks,
)


def test_decodes_utf8_utf16_and_gb18030() -> None:
    text = "第一章\n旧城醒来"
    assert decode_text_bytes(text.encode("utf-8")).text == text
    assert decode_text_bytes(text.encode("utf-16")).text == text
    decoded_gb = decode_text_bytes(text.encode("gb18030"))
    assert decoded_gb.text == text
    assert decoded_gb.encoding == "gb18030"


def test_finds_chinese_and_markdown_chapter_boundaries() -> None:
    text = "序章\n开端\n\n第一章 风雪\n正文\n\n## 第二章 归人\n正文"
    titles = [item.title for item in find_chapter_boundaries(text)]
    assert titles == ["序章", "第一章 风雪", "第二章 归人"]


def test_archive_split_prefers_boundaries_and_preserves_every_character() -> None:
    chapters = [f"第{i}章 标题{i}\n" + ("山河故人" * 18) + "\n\n" for i in range(1, 7)]
    text = "".join(chapters)
    chunks = split_archive_chunks(text, target_chars=170, min_chars=120, max_chars=210)
    assert len(chunks) >= 2
    assert "".join(item.text for item in chunks) == text
    assert chunks[0].start_offset == 0
    assert chunks[-1].end_offset == len(text)
    assert all(item.non_whitespace_chars <= 210 for item in chunks[:-1])


def test_model_split_respects_computed_budget_and_offsets() -> None:
    text = "\n\n".join("这是一个需要保持因果关系的段落。" * 8 for _ in range(12))
    budget = model_input_token_budget(
        context_window_tokens=240,
        output_reserve_tokens=40,
        prompt_tokens=20,
        safety_margin=0.10,
    )
    chunks = split_model_chunks(
        text,
        context_window_tokens=240,
        output_reserve_tokens=40,
        prompt_tokens=20,
        safety_margin=0.10,
    )
    assert len(chunks) > 1
    assert "".join(item.text for item in chunks) == text
    assert all(item.estimated_tokens <= budget for item in chunks)
    assert sum(count_non_whitespace(item.text) for item in chunks) == count_non_whitespace(text)
