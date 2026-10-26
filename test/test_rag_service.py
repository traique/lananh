import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import rag_service


@pytest.fixture
def rag_dir(tmp_path, monkeypatch):
    """Trỏ RAG_DIR sang tmp_path để không đụng kho thật trong repo."""
    monkeypatch.setattr(rag_service, "RAG_DIR", tmp_path)
    rag_service.clear_index_cache()
    yield tmp_path
    rag_service.clear_index_cache()


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
    assert "a" not in tokens  # quá ngắn


def test_split_chunks_by_heading(rag_dir):
    _write(
        rag_dir,
        "dautu.md",
        """# Đầu tư

## Chiến lược DCA
Vào tiền 30% khi VNINDEX dưới MA200.

## Quản trị rủi ro
Không all-in, tối đa 10% vốn cho một mã.
""",
    )
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
    _write(
        rag_dir,
        "dautu.md",
        """# Đầu tư

## Chiến lược DCA
Vào tiền 30% khi VNINDEX dưới MA200.

## Nguyên tắc cắt lỗ
Cắt lỗ khi giá giảm 7% so với giá vào.
""",
    )
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


@pytest.mark.asyncio
async def test_ask_luu_cau_hoi_goc_va_cau_tra_loi_vao_lich_su(rag_dir, monkeypatch):
    _write(rag_dir, "dautu.md", "# Đầu tư\n\n## DCA\nVào tiền đều đặn hàng tháng.\n")
    saved = []

    async def fake_chat(*args, **kwargs):
        return type("R", (), {"text": "DCA là vào tiền đều."})()

    async def fake_record(user_id, user_text, reply, kind="chat", **kwargs):
        saved.append((user_id, user_text, reply, kind))
        return True

    import ai.orchestrator as orchestrator
    from services import conversation_log

    monkeypatch.setattr(orchestrator, "chat", fake_chat)
    monkeypatch.setattr(conversation_log, "record_exchange", fake_record)
    assert await rag_service.ask(1, "dca là gì") == "DCA là vào tiền đều."
    # Câu hỏi GỐC, không phải khung build_prompt.
    assert saved == [(1, "/rag dca là gì", "DCA là vào tiền đều.", "rag")]


# ─── BM25 + cặp âm tiết + stopword mở rộng ──────────────────────────────────

_AGENT_DOC = """# AI Agent

## Mở đầu
Bạn đặt câu hỏi và nó trả lời. Bạn có thể giúp mình không? Có cách nào để bạn đặt
lịch không? Bạn đặt vé, bạn đặt câu hỏi, bạn có cách làm của bạn.

## Vòng lặp ReAct
Vòng lặp ReAct gồm suy nghĩ, hành động, quan sát; vòng lặp lặp lại tới khi xong.

## Guardrails
Guardrails và an ninh cho agent: chặn công cụ nguy hiểm.
"""


def test_cau_khong_lien_quan_khong_con_khop_toi_da(rag_dir):
    """Trước đây "có cách nào giúp mình bán đất không" đạt điểm tối đa 1,0 trên
    các chunk về AI agent chỉ vì "có/không/bán/mình" xuất hiện khắp nơi."""
    _write(rag_dir, "c1.md", _AGENT_DOC)
    assert rag_service.search("có cách nào giúp mình bán đất không") == []
    assert rag_service.auto_lookup("có cách nào giúp mình bán đất không") is None
    # Câu đúng chủ đề vẫn ra đúng chunk đầu tiên, cả /rag lẫn tra tự động.
    (top, *_) = rag_service.search("vòng lặp ReAct là gì")
    assert top.chunk.heading == "AI Agent > Vòng lặp ReAct"
    assert rag_service.auto_lookup("vòng lặp ReAct").chunk is top.chunk


def test_token_chi_muc_co_cap_am_tiet_va_dang_con_dau():
    terms = rag_service.index_terms("Đất nền Long An")
    assert {"dat", "nen", "dat_nen", "~đất_nền"} <= set(terms)
    query = rag_service.index_terms("bán đất", query=True)
    assert query == ["~bán", "~đất", "~bán_đất"]  # gõ có dấu: so dạng còn dấu
    assert rag_service.index_terms("ban dat", query=True) == ["ban", "dat", "ban_dat"]
    # Stopword mở rộng: lời xưng hô/hư từ không còn là token nội dung.
    assert rag_service.index_terms("anh ơi em có không gì giúp mình", query=True) == []


def test_tokenize_cho_ragxuly_van_chi_am_tiet_don():
    assert rag_service.tokenize("đất nền") == {"dat", "nen"}


def test_chi_muc_cache_doc_lai_khi_file_doi(rag_dir, monkeypatch):
    _write(rag_dir, "a.md", "# A\n\n## DCA\nVào tiền đều đặn.\n")
    loads = []
    real_load = rag_service._load_chunks

    def counting_load():
        loads.append(1)
        return real_load()

    monkeypatch.setattr(rag_service, "_load_chunks", counting_load)
    rag_service.search("dca")
    rag_service.search("dca vào tiền")
    assert len(loads) == 1  # không đọc lại đĩa mỗi câu hỏi
    _write(rag_dir, "a.md", "# A\n\n## DCA\nVào tiền đều đặn hàng tháng, kỷ luật.\n")
    rag_service.search("dca")
    assert len(loads) == 2


@pytest.mark.asyncio
async def test_rag_cua_admin_zalo_dung_persona_va_cach_xung_ho_rieng(rag_dir, monkeypatch):
    """/rag trên Zalo (chỉ admin) trả lời bằng biến thể persona admin + cách gọi
    chủ bot đặt, như lượt chat thường - không rơi về persona thành viên + "anh"."""
    from services import channel_command_service, conversation_log, reply_audience

    _write(rag_dir, "dautu.md", "# Đầu tư\n\n## DCA\nVào tiền đều đặn hàng tháng.\n")
    seen = {}

    async def fake_chat(user_id, prompt, **kwargs):
        seen.update(kwargs)
        return type("R", (), {"text": "DCA là vào tiền đều."})()

    async def fake_record(*args, **kwargs):
        return True

    import ai.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "chat", fake_chat)
    monkeypatch.setattr(conversation_log, "record_exchange", fake_record)
    with reply_audience.use(reply_audience.ReplyAudience(address_form="chị")):
        result = await channel_command_service.maybe_handle_command(
            -7, "/rag dca", is_admin=True, channel="zalo"
        )
    assert result is not None and result[0] == ["DCA là vào tiền đều."]
    assert seen["channel"] == "zalo" and seen["is_admin"] is True
    assert seen["address_form"] == "chị" and seen["search_mode"] == "off"
    # Telegram (chủ bot) giữ mặc định.
    seen.clear()
    await rag_service.ask(1, "dca")
    assert seen.get("channel", "telegram") == "telegram" and not seen.get("is_admin")
