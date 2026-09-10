import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services import rag_clean_service, rag_service


@pytest.fixture(autouse=True)
def rag_dir(tmp_path, monkeypatch):
    """Trỏ cả RAG_DIR (rag_service) lẫn BACKUP_DIR sang tmp_path."""
    monkeypatch.setattr(rag_service, "RAG_DIR", tmp_path)
    monkeypatch.setattr(rag_clean_service, "RAG_DIR", tmp_path)
    monkeypatch.setattr(rag_clean_service, "BACKUP_DIR", tmp_path / "_goc")
    return tmp_path


def _write(name: str, text: str) -> None:
    path = rag_service.RAG_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_preclean_merges_soft_breaks_and_drops_page_numbers():
    raw = (
        "Chiến lược đầu tư dài hạn\n"
        "dựa trên nền tảng vững\n"
        "12\n"
        "\n"
        "Quản trị rủi ro là trên hết.\n"
        "--- 13 ---\n"
    )
    cleaned = rag_clean_service.preclean(raw)
    assert "dài hạn dựa trên nền tảng vững" in cleaned  # dòng ngắt đã gộp
    assert "\n12\n" not in cleaned and "13" not in cleaned  # số trang đã bỏ
    assert "Quản trị rủi ro" in cleaned


def test_preclean_keeps_paragraph_breaks():
    cleaned = rag_clean_service.preclean("Đoạn một.\n\nĐoạn hai.")
    assert cleaned == "Đoạn một.\n\nĐoạn hai."


def test_resolve_path_accepts_various_forms():
    _write("ghichu.md", "nội dung")
    assert rag_clean_service.resolve_path("ghichu").name == "ghichu.md"
    assert rag_clean_service.resolve_path("ghichu.md").suffix == ".md"


def test_resolve_path_rejects_traversal_and_missing():
    with pytest.raises(ValueError):
        rag_clean_service.resolve_path("../secret.md")
    with pytest.raises(ValueError):
        rag_clean_service.resolve_path("x.txt")
    with pytest.raises(FileNotFoundError):
        rag_clean_service.resolve_path("khong-ton-tai.md")


def test_validate_rejects_summarized_or_hallucinated_output():
    original = "Câu quan trọng về DCA. " * 100  # ~2500 ký tự
    # Tóm gọn quá mức -> từ chối
    assert rag_clean_service._validate_cleaned(original, "DCA là mua dần.") is not None
    # Bịa thêm tràn lan -> từ chối
    hallucinated = original + "Câu bịa hoàn toàn mới " * 300
    assert rag_clean_service._validate_cleaned(original, hallucinated) is not None
    # Bản dọn tốt (giữ nguyên + heading) -> đạt
    good = "# Đầu tư\n\n## DCA\n" + original
    assert rag_clean_service._validate_cleaned(original, good) is None


def test_backup_and_write_preserves_original(rag_dir):
    _write("sach.md", "BẢN GỐC OCR BẬN")
    path = rag_clean_service.resolve_path("sach.md")
    backup = rag_clean_service._backup_and_write(path, "BẢN GỐC OCR BẬN", "# Sách\nNội dung sạch.")
    assert backup.read_text(encoding="utf-8") == "BẢN GỐC OCR BẬN"
    assert path.read_text(encoding="utf-8").startswith("# Sách")


def test_cleaned_file_is_searchable_but_backup_is_not(rag_dir):
    _write("sach.md", "goc ocr ban khong co heading")
    path = rag_clean_service.resolve_path("sach.md")
    rag_clean_service._backup_and_write(path, "goc ocr ban", "# Đầu tư\n\n## Chiến lược DCA\nVào tiền khi giá giảm.")
    # Kho /rag thấy nội dung đã dọn...
    assert rag_service.search("chien luoc dca")
    # ...nhưnh bỏ qua thư mục backup
    assert not rag_service.search("goc ocr ban")


def test_clean_file_rejects_empty_and_oversize(rag_dir):
    _write("rong.md", "   \n  ")
    assert "rỗng" in rag_clean_service.clean_file("rong.md")
    _write("dai.md", "x" * (rag_clean_service.MAX_SOURCE_CHARS + 100))
    assert "quá dài" in rag_clean_service.clean_file("dai.md")


def test_clean_file_rejects_missing_file():
    reply = rag_clean_service.clean_file("khong-co.md")
    assert "Không thấy file" in reply


def test_clean_file_happy_path_with_fake_ai(rag_dir, monkeypatch):
    raw = "Ghi chú đầu tư\ndòng ngắt cứng\n7\n"
    _write("ghichu.md", raw)

    def fake_ai(precleaned: str) -> str:
        assert "dòng ngắt cứng" in precleaned  # AI nhận bản preclean
        return "# Đầu tư\n\n## Ghi chú\nGhi chú đầu tư dòng ngắt cứng."

    monkeypatch.setattr(rag_clean_service, "_clean_via_ai", fake_ai)
    reply = rag_clean_service.clean_file("ghichu.md")
    assert reply.startswith("✅")
    path = rag_service.RAG_DIR / "ghichu.md"
    assert path.read_text(encoding="utf-8").startswith("# Đầu tư")
    backup = rag_service.RAG_DIR / "_goc" / "ghichu.md"
    assert backup.read_text(encoding="utf-8") == raw
    # File đã có heading -> /rag tra được ngay
    assert rag_service.search("ghi chu dau tu")


def test_clean_file_ai_failure_keeps_original(rag_dir, monkeypatch):
    raw = "Nội dung gốc quan trọng."
    _write("quantrong.md", raw)

    def broken_ai(precleaned):
        raise RuntimeError("provider sập")

    monkeypatch.setattr(rag_clean_service, "_clean_via_ai", broken_ai)
    reply = rag_clean_service.clean_file("quantrong.md")
    assert "⚠️" in reply
    # File gốc KHÔNG bị ghi đè khi AI lỗi
    assert (rag_service.RAG_DIR / "quantrong.md").read_text(encoding="utf-8") == raw


def test_clean_file_rejects_suspicious_ai_output(rag_dir, monkeypatch):
    raw = "Chiến lược DCA rất quan trọng đối với danh mục dài hạn. " * 40
    _write("tongquan.md", raw)

    monkeypatch.setattr(rag_clean_service, "_clean_via_ai", lambda p: "DCA là mua dần.")  # tóm gọn quá mức

    reply = rag_clean_service.clean_file("tongquan.md")
    assert "CHƯA ghi đè" in reply
    assert (rag_service.RAG_DIR / "tongquan.md").read_text(encoding="utf-8") == raw
