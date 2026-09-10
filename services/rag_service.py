"""Tra cứu kiến thức cục bộ theo file .md trong thư mục rag/ (lệnh /rag).

Thiết kế cố ý ĐƠN GIẢN - không embedding, không bảng DB mới:
- Mỗi lần query quét lại toàn bộ rag/**/*.md (kho cá nhân vài chục file,
  đọc đĩa vẫn nhanh hơn plumbing sync embedding + pgvector).
- Chia chunk theo heading markdown (#, ##, ###...), mỗi chunk là 1 "kiến thức"
  mang nhãn (tên file, chuỗi heading).
- Chấm điểm bằng trùng từ khóa ĐÃ BỎ DẤU (Unicode NFD + bỏ combining marks),
  nên "hoa hong" vẫn khớp "hồng hạnh", "dat nen" khớp "đất nền". Có thưởng
  nhỏ nếu từ khóa xuất hiện ngay trong heading.
- AI diễn giải: các chunk khớp nhất được nhét vào làm grounding cho
  orchestrator.chat() (giữ persona Lan Anh + lịch sử hội thoại) với chỉ dẫn
  CHỈ trả lời dựa trên tài liệu. Không khớp chunk nào thì không gọi AI.
"""

import asyncio
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Thư mục kiến thức nằm ở root repo (cwd khi chạy bot: main.py / web.py).
RAG_DIR = Path("rag")

# Giới hạn độ lớn để prompt không phình vô hạn.
MAX_CHUNK_CHARS = 2000      # trần cứng 1 mẩu (đoạn đơn lẻ khổng lồ bị cắt)
CHUNK_TARGET_CHARS = 1200   # cỡ mong muốn khi chia mẩu theo đoạn trống
MAX_MATCHES = 4             # số chunk đưa vào grounding
MAX_GROUNDING_CHARS = 6000  # trần tổng chiều dài grounding
SUGGESTION_LIMIT = 8        # số từ khóa gợi ý khi không tìm thấy

def strip_vn_diacritics(text: str) -> str:
    """Bỏ dấu tiếng Việt, giữ chữ cái gốc: "đất nền" -> "dat nen"."""
    decomposed = unicodedata.normalize("NFD", text)
    # đ (U+0111) không tổ hợp được ở NFD nên thay tay trước khi lọc dấu.
    no_marks = "".join(
        ch for ch in decomposed.replace("đ", "d").replace("Đ", "D")
        if not unicodedata.combining(ch)
    )
    return unicodedata.normalize("NFC", no_marks).casefold()


# Từ quá ngắn/quá phổ biến (tiếng Việt lẫn tiếng Anh) không đáng để chấm điểm.
# So sánh với token ĐÃ BỎ DẤU nên danh sách phải qua strip_vn_diacritics.
_STOPWORDS = frozenset(
    strip_vn_diacritics(w)
    for w in """và với của các cho trong khi thì là được bị sẽ đã cũng
    như về tới đến từ trên dưới ngoài vào ra một hai ba này đó kia những
    cái con chú điều chỗ lúc nào sao vậy nhỉ nhé
    a an and are as at be but by for from has have how i in is it its
    of on or that the to was what when where which who why will with""".split()
)


def tokenize(text: str) -> set[str]:
    """Tách thành tập token không dấu, bỏ token rỗng/quá ngắn/stopword."""
    cleaned = strip_vn_diacritics(text)
    tokens: set[str] = set()
    for raw in cleaned.split():
        token = "".join(ch for ch in raw if ch.isalnum())
        if len(token) >= 2 and token not in _STOPWORDS:
            tokens.add(token)
    return tokens


@dataclass
class Chunk:
    """Một mẩu kiến thức: nội dung dưới 1 heading trong 1 file .md."""

    file: str          # đường dẫn tương đối, hiện cho người dùng
    heading: str       # chuỗi heading (vd "Đầu tư > Chiến lược DCA")
    content: str       # nội dung chunk (đã strip, cắt theo MAX_CHUNK_CHARS)
    tokens: frozenset  # token của heading + nội dung, đã bỏ dấu
    heading_tokens: frozenset  # riêng token heading để tính điểm thưởng

    @property
    def display_name(self) -> str:
        return f"{self.file} › {self.heading}" if self.heading else self.file


@dataclass
class Match:
    chunk: Chunk
    score: float


def _iter_markdown_files() -> list[Path]:
    if not RAG_DIR.exists():
        return []
    # Bỏ qua file/Thư mục bắt đầu bằng "_" hoặc "." (vd rag/_goc/ là backup
    # bản gốc chưa dọn của /ragxuly) - chỉ nội dung "chính thức" mới vào kho.
    return sorted(
        p for p in RAG_DIR.rglob("*.md")
        if p.is_file()
        and p.name.casefold() != "readme.md"
        and not any(part.startswith(("_", ".")) for part in p.relative_to(RAG_DIR).parts)
    )


def _split_body(content: str) -> list[str]:
    """Chia 1 khối thân bài dài thành nhiều mẩu ~CHUNK_TARGET_CHARS.

    File .md thô (không heading) hoặc mục quá dài không bị mất nội dung:
    cắt tại ranh giới đoạn trống (blank line), chỉ cắt cứng giữa đoạn khi
    1 đoạn đơn lẻ dài hơn trần (chọn chỗ ngắt gần dấu cách nhất).
    """
    if len(content) <= CHUNK_TARGET_CHARS:
        return [content]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for para in content.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        while len(para) > CHUNK_TARGET_CHARS:
            if current:
                parts.append("\n\n".join(current))
                current, size = [], 0
            # Cắt cứng đoạn khổng lồ: ưu tiên ngắt sau dấu cách gần trần nhất.
            cut = para.rfind(" ", 0, CHUNK_TARGET_CHARS)
            if cut < CHUNK_TARGET_CHARS // 2:
                cut = CHUNK_TARGET_CHARS
            parts.append(para[:cut].rstrip())
            para = para[cut:].lstrip()
        if size and size + len(para) > CHUNK_TARGET_CHARS:
            parts.append("\n\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para)
    if current:
        parts.append("\n\n".join(current))
    return parts


def _split_chunks(file: Path, text: str) -> list[Chunk]:
    """Chia 1 file .md thành chunks theo heading markdown (bất kỳ cấp nào).

    File thô không heading nào cũng được: toàn bộ thân bài coi như 1 "mục"
    duy nhất, rồi vẫn được chia nhỏ theo đoạn trống qua _split_body() nên
    không có nội dung nào bị bỏ. Chunk đầu file (intro trước heading) cũng
    giữ nếu có nội dung thật.
    """
    rel = file.relative_to(RAG_DIR).as_posix()
    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []  # (level, title), level từ 1
    body_lines: list[str] = []
    last_heading = ""  # heading đơn giản nhất của chunk hiện tại (sau cùng)

    def _flush():
        nonlocal body_lines
        content = "\n".join(body_lines).strip()
        body_lines = []
        if not content:
            return
        parts = _split_body(content)
        for i, part in enumerate(parts, 1):
            if len(parts) == 1:
                label = last_heading
            elif last_heading:
                label = f"{last_heading} (phần {i}/{len(parts)})"
            else:
                label = f"(phần {i}/{len(parts)})"
            chunks.append(Chunk(
                file=rel,
                heading=label,
                content=part,
                tokens=frozenset(tokenize(f"{label} {part}")),
                heading_tokens=frozenset(tokenize(last_heading)),
            ))

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped.lstrip("#").strip()
            if not title:
                continue  # dòng "###" trống - không phải heading thật
            _flush()
            # Giữ heading đúng thứ bậc: cấp con đè cấp cha cùng vị trí.
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            last_heading = " > ".join(t for _, t in heading_stack)
            continue
        body_lines.append(line)
    _flush()
    return chunks


def _load_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    for file in _iter_markdown_files():
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            logger.warning("Không đọc được file kiến thức: %s", file, exc_info=True)
            continue
        chunks.extend(_split_chunks(file, text))
    return chunks


def search(query: str, top_n: int = MAX_MATCHES) -> list[Match]:
    """Tìm các chunk khớp nhất với câu hỏi (không cần dấu, không cần API)."""
    query_tokens = tokenize(query)
    if not query_tokens:
        return []
    scored: list[Match] = []
    for chunk in _load_chunks():
        if not chunk.tokens:
            continue
        overlap = query_tokens & chunk.tokens
        if not overlap:
            continue
        score = len(overlap) / len(query_tokens)
        # Thưởng nhẹ khi từ khóa nằm ngay trong heading - đây là "chủ đề"
        # của chunk, đáng tin hơn việc từ khóa lọt lẻo trong thân bài.
        score += 0.5 * len(query_tokens & chunk.heading_tokens) / len(query_tokens)
        scored.append(Match(chunk=chunk, score=score))
    scored.sort(key=lambda m: m.score, reverse=True)
    return scored[:top_n]


def _format_citation(match: Match) -> str:
    return f"【{match.chunk.display_name}】\n{match.chunk.content}"


def _format_citations(matches: list[Match]) -> str:
    return "\n\n---\n\n".join(_format_citation(m) for m in matches)


def _suggestions(chunks: list[Chunk]) -> list[str]:
    """Gom vài từ khóa tiêu biểu (ưu tiên token heading) để gợi ý khi trượt."""
    heading_counts: dict[str, int] = {}
    for chunk in chunks:
        for token in chunk.heading_tokens:
            heading_counts[token] = heading_counts.get(token, 0) + 1
    ranked = sorted(heading_counts.items(), key=lambda kv: kv[1], reverse=True)
    suggestions = [token for token, _ in ranked if len(token) >= 4]
    if not suggestions:
        all_tokens: set[str] = set()
        for chunk in chunks:
            all_tokens.update(chunk.tokens)
        suggestions = sorted(all_tokens, key=len, reverse=True)
    return suggestions[:SUGGESTION_LIMIT]


def _no_match_reply(query: str) -> str:
    chunks = _load_chunks()
    if not chunks:
        return (
            "📁 Em chưa có kiến thức nào trong thư mục rag/ cả.\n"
            "Anh chép các file .md vào rag/ rồi thử lại nhé."
        )
    suggestions = _suggestions(chunks)
    hint = ", ".join(suggestions) if suggestions else "(kho đang trống từ khóa)"
    return (
        f"Em không tìm thấy nội dung nào khớp với “{query}” trong kho kiến thức rag/.\n"
        f"Anh thử từ khóa khác xem, ví dụ: {hint}"
    )


_GROUNDING_DIRECTIVE = (
    "Bạn được cung cấp các TRÍCH DẪN từ kho kiến thức cá nhân của người dùng "
    "(mỗi trích dẫn có nhãn 【tên file › heading】 cho biết nguồn).\n"
    "NHIỆM VỤ: trả lời câu hỏi của người dùng CHỈ dựa trên các trích dẫn này.\n"
    "- Nếu trích dẫn chứa đủ thông tin: trả lời tự nhiên, giọng thân mật, "
    "và nêu nguồn dạng (file › heading) ở phần tương ứng.\n"
    "- Nếu trích dẫn chỉ chứa một phần: trả lời phần có dữ liệu, nói rõ "
    "phần còn lại không có trong tài liệu.\n"
    "- TUYỆT ĐỐI không bịa thông tin ngoài tài liệu, không dùng kiến thức "
    "riêng của bạn để bổ sung, không tra web.\n"
    "- Trích dẫn có thể bị cắt giữa chừng (dấu …) - đừng suy diễn phần bị cắt.\n"
    "- Không nhắc tới chữ \"trích dẫn\" hay quy trình - cứ trả lời trực tiếp."
)


def build_prompt(query: str, matches: list[Match]) -> tuple[str, str]:
    """Dựng (grounding, prompt) cho orchestrator.chat()."""
    grounding = (
        f"{_GROUNDING_DIRECTIVE}\n\n=== TRÍCH DẪN TỪ KHO KIẾN THỨC rag/ ===\n"
        f"{_format_citations(matches)}"
    )
    prompt = (
        f"Dựa trên kho kiến thức của anh, trả lời: {query}\n"
        "(Chỉ dùng tài liệu đã cung cấp ở trên, có ghi nguồn.)"
    )
    return grounding, prompt


async def _ask_llm(user_id: int, query: str, matches: list[Match]) -> str:
    from ai import orchestrator  # tránh import vòng khi ai.* import services.*

    grounding, prompt = build_prompt(query, matches)
    response = await orchestrator.chat(
        user_id, prompt, grounding=grounding, memory_context=""
    )
    answer = (getattr(response, "text", "") or "").strip()
    if not answer:
        raise ValueError("LLM trả về rỗng")
    return answer


async def ask(user_id: int, query: str) -> str:
    """Flow chính của /rag: tìm chunk -> AI diễn giải (hoặc trả trích dẫn).

    Trả về text sẵn sàng gửi người dùng (đã kèm nguồn / gợi ý khi trượt).
    Telemetry do handler ở trên lo (kèm channel); ở đây chỉ log fallback.
    """
    matches = await asyncio.to_thread(search, query)
    if not matches:
        return _no_match_reply(query)

    try:
        answer = await _ask_llm(user_id, query, matches)
    except Exception as exc:
        logger.warning("AI diễn giải /rag lỗi, fallback trả trích dẫn: %s", exc)
        return (
            "⚠️ AI diễn giải lỗi nên em gửi nguyên trích dẫn nhé:\n\n"
            f"{_format_citations(matches)}"
        )
    return answer
