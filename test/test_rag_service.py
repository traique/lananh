import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import rag_service


@pytest.fixture
def rag_dir(tmp_path, monkeypatch):
    """Trỏ RAG_DIR sang tmp_path để không đụng kho thật trong repo."""
    monkeypatch.setattr(rag_service, "RAG_DIR", tmp_path)
    return tmp_path


def _write(rag_dir: Path, name: str, text: str) -> None:
    path = rag_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_strip_vn_diacritics():
    assert rag_service.strip_vn_diacritics("Đất nền hồng hạnh") == "dat nen hong hanh"
    assert rag_service.strip_vn_diacritics("HOA HỒNG") == "hoa hong"
    # Chữ thường của "đ" sau casefold thành "đ" (không phải "d") - đã thay tay.
    assert rag_service.strip_vn_diacritics("Đường đua") == "duong dua"


def test_tokenize_drops_stopwords_and_short_tokens():
    tokens = rag_service.tokenize("Chiến lược DCA vào cổ phiếu FPT")
    assert "dca" in tokens and "fpt" in tokens and "chien" in tokens
    assert "vao" not in tokens  # stopword
    assert "a" not in tokens    # quá ngắn


def test_split_chunks_by_heading(rag_dir):
    _write(rag_dir, "dautu.md", """# Đầu tư

## Chiến lược DCA
Vào tiền 30% khi VNINDEX dưới MA200.

## Quản trị rủi ro
Không all-in, tối đa 10% vốn cho một mã.
""")
    chunks = rag_service._load_chunks()
    headings = {c.heading for c in chunks}
    assert headings == {"Đầu tư > Chiến lược DCA", "Đầu tư > Quản trị rủi ro"}


def test_split_chunks_long_section_is_split_by_paragraph(rag_dir):
    # Mục quá dài phải được chia thành nhiều mẩu theo đoạn trống,
    # KHÔNG cắt bỏ như trước đây (mất phần đuôi).
    paras = "\n\n".join(f"Đoạn {i}: " + "nội dung chuyên sâu " * 40 for i in range(1, 8))
    _write(rag_dir, "dai.md", f"# Mục\n\n{paras}")
    chunks = rag_service._load_chunks()
    assert len(chunks) > 1
    assert all(len(c.content) <= rag_service.MAX_CHUNK_CHARS for c in chunks)
    # Không mất từ nào so với bản gốc (chia, không cắt)
    original_tokens = rag_service.tokenize(paras)
    merged_tokens: set[str] = set()
    for c in chunks:
        merged_tokens.update(rag_service.tokenize(c.content))
    assert merged_tokens >= original_tokens
    # Nhãn phần đánh số để người dùng biết mẩu đang ở đâu trong mục
    assert chunks[0].heading.startswith("Mục (phần 1/")


def test_split_chunks_raw_file_without_headings(rag_dir):
    # File .md THÔ (không heading nào) vẫn được chia mẩu và tìm được cả đuôi.
    tail_note = "GHI CHÉ CUỐI: giữ vị thế tối đa 10 phần trăm cho một mã."
    body = "Ghi chú về đầu tư cá nhân. " * 250 + "\n\n" + tail_note
    _write(rag_dir, "thon.md", body)
    chunks = rag_service._load_chunks()
    assert len(chunks) > 1
    matches = rag_service.search("giu vi the toi da")
    assert matches, "Nội dung ở cuối file thô phải tìm thấy"
    assert "10 phần trăm" in matches[0].chunk.content
    # Không heading -> nhãn chỉ còn đánh số phần
    assert matches[0].chunk.heading.startswith("(phần ")


def test_search_matches_without_diacritics(rag_dir):
    _write(rag_dir, "dautu.md", """# Đầu tư

## Chiến lược DCA
Vào tiền 30% khi VNINDEX dưới MA200.

## Nguyên tắc cắt lỗ
Cắt lỗ khi giá giảm 7% so với giá vào.
""")
    # Hỏi không dấu vẫn khớp heading có dấu.
    matches = rag_service.search("chiến lược dca vào tiền")
    assert matches, "Phải tìm thấy chunk Chiến lược DCA"
    top = matches[0].chunk
    assert top.heading == "Đầu tư > Chiến lược DCA"
    assert "MA200" in top.content
    # Heading khớp phải được thưởng điểm, đứng trước chunk thân bài.
    assert all(m.chunk.heading != "Đầu tư > Nguyên tắc cắt lỗ" for m in matches[:1])


def test_search_no_hit_returns_empty(rag_dir):
    _write(rag_dir, "dautu.md", "# Đầu tư\n\n## DCA\nVào tiền đều đặn.\n")
    assert rag_service.search("mon an sang nay") == []


def test_search_empty_dir(rag_dir):
    assert rag_service.search("bất cứ gì") == []


def test_readme_file_is_ignored(rag_dir):
    _write(rag_dir, "README.md", "# Ghi chú riêng\nTừ khóa bí mật xyzzy.")
    assert rag_service.search("Từ khóa bí mật xyzzy") == []


def test_no_match_reply_hints_existing_keywords(rag_dir):
    _write(rag_dir, "dautu.md", "# Đầu tư\n\n## Chiến lược DCA\nVào tiền đều.\n")
    reply = rag_service._no_match_reply("công thức nấu phở")
    assert "không tìm thấy" in reply
    assert "chien" in reply or "dca" in reply  # gợi ý từ heading có sẵn


def test_no_match_reply_empty_dir(rag_dir):
    reply = rag_service._no_match_reply("câu hỏi")
    assert "chưa có kiến thức" in reply


def test_build_prompt_contains_citations_and_directive(rag_dir):
    _write(rag_dir, "dautu.md", "# Đầu tư\n\n## DCA\nVào tiền đều đặn hàng tháng.\n")
    (match,) = rag_service.search("dca")
    grounding, prompt = rag_service.build_prompt("DCA là gì?", [match])
    assert "【dautu.md › Đầu tư > DCA】" in grounding
    assert "CHỈ dựa trên" in grounding
    assert "DCA là gì?" in prompt


@pytest.mark.asyncio
async def test_ask_falls_back_to_citations_when_llm_fails(rag_dir, monkeypatch):
    _write(rag_dir, "dautu.md", "# Đầu tư\n\n## DCA\nVào tiền đều đặn hàng tháng.\n")

    async def fail_chat(*args, **kwargs):
        raise RuntimeError("provider sập")

    import ai.orchestrator as orchestrator
    monkeypatch.setattr(orchestrator, "chat", fail_chat)

    reply = await rag_service.ask(1, "dca")
    assert "trích dẫn" in reply
    assert "【dautu.md › Đầu tư > DCA】" in reply


@pytest.mark.asyncio
async def test_ask_no_match_does_not_call_llm(rag_dir, monkeypatch):
    _write(rag_dir, "dautu.md", "# Đầu tư\n\n## DCA\nVào tiền đều đặn hàng tháng.\n")
    called = False

    async def fail_chat(*args, **kwargs):
        nonlocal called
        called = True
        raise RuntimeError("không được gọi tới")

    import ai.orchestrator as orchestrator
    monkeypatch.setattr(orchestrator, "chat", fail_chat)

    reply = await rag_service.ask(1, "mon an sang nay")
    assert "không tìm thấy" in reply
    assert not called
