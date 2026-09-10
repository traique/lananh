"""Dọn file .md OCR từ PDF thành kho kiến thức sạch cho /rag (lệnh /ragxuly).

File gốc của người dùng là PDF → OCR sang markdown nên thường: không heading,
dòng bị ngắt cứng giữa chừng, dính số trang / header-footer lặp, ký tự OCR sai.

Quy trình của /ragxuly <tên-file>:
1. Preclean DIỀN KIỆN (không AI): gộp dòng ngắt cứng, bỏ số trang đứng một
   mình, nén khoảng trắng — các biến đổi tất-định, không thể sai.
2. AI dọn (một lượt): tách heading + đoạn, sửa lỗi OCR, GIỮ NGUYÊN nội dung.
   Gọi qua orchestrator.ask() (provider chain + retry như mọi lệnh khác).
3. Backup bản gốc vào rag/_goc/<tên>.md rồi GHI ĐÈ file chính thức.
   Lần sau /rag tìm thấy ngay vì file mới có heading rõ ràng.

Nội dung gốc nằm trong prompt chỉ có vai trò dữ liệu — không có lệnh nào
trong file được xử lý như chỉ dẫn cho AI (chống prompt injection từ PDF).
"""

import logging
import re
import unicodedata
from pathlib import Path

from services.rag_service import RAG_DIR

logger = logging.getLogger(__name__)

# Bản gốc chưa dọn nằm ở rag/_goc/ (rag_service đã chủ động bỏ qua _goc/).
BACKUP_DIR = RAG_DIR / "_goc"

MAX_SOURCE_CHARS = 30000  # trần 1 lượt dọn (~75k token tiếng Việt), đủ dài
PAGE_NUMBER_RE = re.compile(r"(?m)^\s*[-–—|]*\s*(?:trang\s*)?\d{1,4}\s*[-–—|]*\s*$")
MULTIBLANK_RE = re.compile(r"\n{3,}")
SOFT_BREAK_RE = re.compile(r"(?<!\n)\n(?!\n)")  # \n đơn, giữ \n\n (ranh giới đoạn)


def resolve_path(name: str) -> Path:
    """Giải pháp tên file người dùng nhập → đường dẫn an toàn trong rag/.

    Chấp nhận "ghichu", "ghichu.md", "sub/ghichu.md". Chặn path traversal
    (.., / tuyệt đối, ký tự điều khiển) - mọi đường dẫn hợp lệ đều phải nằm
    trong RAG_DIR và là file .md tồn tại.
    """
    name = name.strip().strip('"').strip("'")
    if not name:
        raise ValueError("Tên file trống.")
    candidate = (RAG_DIR / name).resolve()
    root = RAG_DIR.resolve()
    if root not in candidate.parents:
        raise ValueError("Chỉ được dọn file bên trong thư mục rag/.")
    if candidate.suffix.casefold() != ".md":
        # Người dùng gõ "ghichu" thiếu đuôi -> thử "ghichu.md".
        with_md = candidate.with_name(candidate.name + ".md")
        if with_md.is_file() and root in with_md.parents:
            candidate = with_md
        else:
            raise ValueError("Chỉ hỗ trợ file .md (đuôi khác thì đổi tên trước).")
    if not candidate.is_file():
        raise FileNotFoundError(
            f"Không thấy file {candidate.relative_to(root).as_posix()} trong rag/."
        )
    return candidate


def preclean(text: str) -> str:
    """Làm sạch DIỀN KIỆN trước khi đưa cho AI: gộp dòng ngắt OCR, bỏ số trang.

    Chỉ làm các biến đổi tất-định; AI sau đó lo phần "thông minh" (heading,
    tách đoạn, sửa chữ). Kết quả luôn khác rỗng nếu input khác rỗng.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = PAGE_NUMBER_RE.sub("", text)
    # Dòng ngắt giữa chừng trong 1 đoạn (OCR cắt theo dòng PDF) → thay bằng
    # dấu cách; giữ \n\n là ranh giới đoạn thật.
    text = SOFT_BREAK_RE.sub(" ", text)
    text = MULTIBLANK_RE.sub("\n\n", text)
    # Nén khoảng trắng thừa do gộp dòng sinh ra (giữ \n đã xử lý xong).
    text = re.sub(r"[^\S\n]{2,}", " ", text)
    return text.strip()


_CLEAN_INSTRUCTION = """Nhiệm vụ: dọn một file markdown DO OCR TỪ PDF tạo ra, biến nó thành tài liệu kiến thức sạch để hệ thống tra cứu theo heading (RAG chunking theo heading).

QUY TẮC BẮT BUỘC:
1. GIỮ NGUYÊN nội dung, ý, số liệu, tên riêng, bảng biểu - KHÔNG bổ sung kiến thức ngoài, KHÔNG tóm gọn, KHÔNG bình luận, KHÔNG bỏ câu nào.
2. Sửa lỗi OCR rõ ràng (ký tự sai, chữ dính nhau, dấu tiếng Việt lỗi) bằng ngữ cảnh. Không chắc thì giữ nguyên.
3. Tạo cấu trúc heading markdown (#, ##, ###) theo chủ đề thực tế của tài liệu. Heading phải nêu đúng từ khóa chủ đề. KHÔNG đặt heading bao trùm cả file trừ khi nó thật sự là chủ đề chính.
4. Tách lại đoạn hợp lý: mỗi ý/mỗi gạch đầu dòng một đoạn, ngăn cách bằng dòng trống.
5. Bỏ rác OCR còn sót: số trang, header/footer lặp, ký tự vô nghĩa (‰, ¶, chuỗi ký tự lạ) - TRỪ khi nó là số liệu thực (bảng số liệu, tỉ lệ...).
6. Giữ nguyên ngôn ngữ của tài liệu (tiếng Việt thì giữ tiếng Việt).
7. Output CHỈ có nội dung markdown đã dọn - không lời dẫn, không wrap trong code fence.

TÀI LIỆU CẦN DÓN:
<<<
{source}
>>>"""


def _clean_via_ai(precleaned: str) -> str:
    """Gọi AI dọn 1 lượt qua orchestrator (provider chain + retry sẵn có).

    Hàm đồng bộ - caller chạy trong thread riêng để không chặn event loop.
    """
    import asyncio

    from ai import orchestrator

    prompt = _CLEAN_INSTRUCTION.replace("{source}", precleaned)

    async def _run():
        response = await orchestrator.ask(prompt)
        return (getattr(response, "text", "") or "").strip()

    return asyncio.run(_run())


def _validate_cleaned(original: str, cleaned: str) -> str | None:
    """Kiểm bảo vệ: bản dọn phải đủ gần bản gốc về độ dài và từ khóa.

    Trả về lý do từ chối (hoặc None nếu đạt). Chống AI tóm gọn quá mức,
    bịa thêm tràn lan, hoặc trả lời lệch (vd trả lời bằng lời dẫn).
    """
    if not cleaned:
        return "AI trả về rỗng"
    if len(cleaned) < 0.5 * len(original):
        return f"Bản dọn ngắn bất thường ({len(cleaned)}/{len(original)} ký tự, có thể bị tóm gọn)"
    if len(cleaned) > 1.6 * len(original) + 500:
        return f"Bản dọn dài bất thường ({len(cleaned)}/{len(original)} ký tự, có thể bịa thêm)"
    # Độ phủ từ khóa: >=80% token của bản preclean phải xuất hiện trong bản dọn.
    from services.rag_service import tokenize

    src_tokens = tokenize(original)
    if src_tokens:
        out_tokens = tokenize(cleaned)
        missing = src_tokens - out_tokens
        coverage = 1 - len(missing) / len(src_tokens)
        if coverage < 0.8:
            return f"Bản dọn thiếu ~{len(missing)} từ khóa của bản gốc (độ phủ {coverage:.0%})"
    return None


def _backup_and_write(path: Path, original: str, cleaned: str) -> Path:
    """Sao lưu bản gốc vào rag/_goc/ (không ghi đè backup có sẵn) rồi ghi đè file."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / path.name
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
    path.write_text(cleaned, encoding="utf-8")
    return backup


def clean_file(name: str) -> str:
    """Flow chính /ragxuly: preclean → AI dọn → validate → backup → ghi đè.

    Trả về thông báo sẵn sàng gửi người dùng. Raise ValueError/FileNotFoundError
    cho lỗi input; exception khác (AI lỗi, validate trượt...) đã được bọc
    thành thông báo thân thiện - caller không cần xử lý thêm.
    """
    try:
        path = resolve_path(name)
    except (ValueError, FileNotFoundError) as exc:
        return f"⚠️ {exc}"

    try:
        original = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        logger.warning("Không đọc được file: %s", path, exc_info=True)
        return "⚠️ Không đọc được file (lỗi mã hóa hoặc file hệ thống). Ghi file bằng UTF-8 nhé."

    if not original.strip():
        return "⚠️ File rỗng, không có gì để dọn."

    if len(original) > MAX_SOURCE_CHARS:
        return (
            f"⚠️ File quá dài ({len(original):,} ký tự, trần {MAX_SOURCE_CHARS:,}). "
            "Anh tách nhỏ thành vài file rồi dọn từng file giúp em."
        )

    precleaned = preclean(original)
    # AI dọn chạy trong thread riêng: _clean_via_ai dùng asyncio.run() bên
    # trong (orchestrator.ask cần event loop riêng), không được chặn loop chính.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_clean_via_ai, precleaned)
        try:
            cleaned = future.result(timeout=180)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return "⚠️ AI dọn file quá 3 phút không xong. Thử lại sau, hoặc file dài quá thì tách nhỏ giúp em."
        except Exception as exc:
            logger.warning("AI dọn /ragxuly lỗi: %s", exc)
            return f"⚠️ AI không dọn được file lúc này ({exc}). Preclean vẫn đáng giá: anh xem lại sau vài phút."

    reason = _validate_cleaned(precleaned, cleaned)
    if reason is not None:
        logger.warning("Từ chối bản dọn của AI: %s", reason)
        return (
            f"⚠️ Bản dọn của AI không đạt kiểm định ({reason}) nên em CHƯA ghi đè file. "
            "Thử lại được nhé - hoặc nếu muốn, em gửi preclean (đã bỏ số trang, gộp dòng) để anh tự xem."
        )

    backup = _backup_and_write(path, original, cleaned)
    return (
        "✅ Đã dọn xong!\n"
        f"• File: {path.relative_to(RAG_DIR.parent).as_posix()}\n"
        f"• Bản gốc: backup tại {backup.as_posix()}\n"
        f"• Kích thước: {len(original):,} → {len(cleaned):,} ký tự\n"
        "Giờ /rag sẽ tra được chuẩn hơn vì file đã có heading rõ ràng. "
        "Anh xem lại file nhé - AI có thể sai sót chỗ nào đó."
    )
