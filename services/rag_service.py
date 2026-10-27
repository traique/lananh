"""Tra cứu kiến thức cục bộ theo file .md trong thư mục rag/ (lệnh /rag và tra
tự động trong chat của chủ bot/admin).

Thiết kế cố ý ĐƠN GIẢN - không embedding, không bảng DB mới:
- Chia chunk theo heading markdown (#, ##, ###...), mỗi chunk là 1 "kiến thức"
  mang nhãn (tên file, chuỗi heading).
- Chấm điểm BM25 (k1=1.2, b=0.75, IDF kiểu Lucene log(1+(N-df+0.5)/(df+0.5)) -
  không âm với từ xuất hiện ở hơn nửa số chunk) trên token ĐÃ BỎ DẤU: âm tiết
  đơn + cặp âm tiết liền nhau ("dat_nen", "co_phieu") để "bán đất" không còn
  khớp mọi chunk có "bán" hay "đất". Stopword tiếng Việt mở rộng (anh, em, có,
  không, gì, mình, giúp...). Heading được nhân đôi trọng số.
- Điểm chuẩn hoá = BM25 / tổng IDF các token câu hỏi (so được giữa các câu
  hỏi); phải đạt ngưỡng tối thiểu VÀ trùng >= 2 token nội dung mới tính là khớp
  - câu không liên quan nhận gợi ý từ khoá thay vì trích dẫn lạc đề.
- Chỉ mục cache trong RAM theo (đường dẫn, mtime, kích thước) của các file,
  có trần số chunk; đọc lại đĩa chỉ khi file đổi.
- ``tokenize()`` (tập âm tiết đơn) giữ nguyên cho /ragxuly so độ phủ từ khoá
  và trí nhớ hội thoại - KHÔNG thêm cặp âm tiết vào đó.
- AI diễn giải: các chunk khớp nhất được nhét vào làm grounding cho
  orchestrator.chat() (giữ persona Lan Anh + lịch sử hội thoại) với chỉ dẫn
  CHỈ trả lời dựa trên tài liệu. Không khớp chunk nào thì không gọi AI.
"""

import asyncio
import logging
import math
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Thư mục kiến thức nằm ở root repo (cwd khi chạy bot: main.py / web.py).
RAG_DIR = Path("rag")

# Giới hạn độ lớn để prompt không phình vô hạn.
MAX_CHUNK_CHARS = 2000  # trần cứng 1 mẩu (đoạn đơn lẻ khổng lồ bị cắt)
CHUNK_TARGET_CHARS = 1200  # cỡ mong muốn khi chia mẩu theo đoạn trống
MAX_MATCHES = 4  # số chunk đưa vào grounding
MAX_GROUNDING_CHARS = 6000  # trần tổng chiều dài grounding
SUGGESTION_LIMIT = 8  # số từ khóa gợi ý khi không tìm thấy


def strip_vn_diacritics(text: str) -> str:
    """Bỏ dấu tiếng Việt, giữ chữ cái gốc: "đất nền" -> "dat nen"."""
    decomposed = unicodedata.normalize("NFD", text)
    # đ (U+0111) không tổ hợp được ở NFD nên thay tay trước khi lọc dấu.
    no_marks = "".join(
        ch for ch in decomposed.replace("đ", "d").replace("Đ", "D") if not unicodedata.combining(ch)
    )
    return unicodedata.normalize("NFC", no_marks).casefold()


# Từ quá ngắn/quá phổ biến (tiếng Việt lẫn tiếng Anh) không đáng để chấm điểm.
# So sánh với token ĐÃ BỎ DẤU nên danh sách phải qua strip_vn_diacritics - vì
# vậy KHÔNG thêm hư từ trùng dạng bỏ dấu với từ nội dung ("nên"/"nền",
# "mà"/"mã", "đầu"/"đâu", "vâng"/"vàng", "nhà"/"nha", "nợ"/"nó"...).
_STOPWORDS = frozenset(
    strip_vn_diacritics(w)
    for w in """và với của các cho trong khi thì là được bị sẽ đã cũng
    như về tới đến từ trên dưới ngoài vào ra một hai ba này đó kia những
    cái con chú điều chỗ lúc nào sao vậy nhỉ nhé
    anh em mình tôi tớ chúng ta bọn có không gì giúp dùm ạ à ơi dạ ừ thế đi
    rất hơn hay hoặc nếu vì để nhiêu xem biết thể
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

    file: str  # đường dẫn tương đối, hiện cho người dùng
    heading: str  # chuỗi heading (vd "Đầu tư > Chiến lược DCA")
    content: str  # nội dung chunk (đã strip, cắt theo MAX_CHUNK_CHARS)
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
        p
        for p in RAG_DIR.rglob("*.md")
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
            chunks.append(
                Chunk(
                    file=rel,
                    heading=label,
                    content=part,
                    tokens=frozenset(tokenize(f"{label} {part}")),
                    heading_tokens=frozenset(tokenize(last_heading)),
                )
            )

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


# ─── BM25 ────────────────────────────────────────────────────────────────────

BM25_K1 = 1.2
BM25_B = 0.75
# Ngưỡng cho /rag: điểm chuẩn hoá tối thiểu + số token nội dung trùng.
MIN_NORMALIZED_SCORE = 0.2
MIN_MATCHED_TERMS = 2
# Trần chỉ mục trong RAM. Đo 27/10 (kho thật 100 chunk / 82 nghìn ký tự): chunk +
# Counter BM25 tốn ~44 KB/chunk, ~55 byte/ký tự -> trần cũ 2000 chunk có thể lên
# 90-200 MB (tài liệu /doc lưu vào rag/ tới 20 MB). Trần mới: 600 chunk VÀ 400
# nghìn ký tự nội dung (~22 MB tệ nhất); phần vượt không được tra, có log cảnh báo.
MAX_INDEX_CHUNKS = 600
MAX_INDEX_CHARS = 400_000


def _syllables(text: str) -> list[tuple[str, str]]:
    """[(bỏ dấu, còn dấu chữ thường)] từng âm tiết, đã bỏ ký tự không phải chữ/số."""
    out = []
    for raw in unicodedata.normalize("NFC", text or "").lower().split():
        accented = "".join(ch for ch in raw if ch.isalnum())
        if accented:
            out.append((strip_vn_diacritics(accented), accented))
    return out


def index_terms(text: str, *, query: bool = False) -> list[str]:
    """Token BM25 (CÓ lặp để tính tần suất): âm tiết đơn không phải stopword +
    cặp 2 âm tiết NỘI DUNG liền nhau ("dat_nen", "vong_lap"). Chỉ mục giữ cả
    dạng bỏ dấu lẫn dạng còn dấu (tiền tố "~"); câu hỏi gõ CÓ dấu thì so dạng
    còn dấu để "bán đất" không khớp "bạn đặt". KHÁC tokenize() (tập âm tiết
    đơn bỏ dấu, dùng cho /ragxuly và trí nhớ)."""
    pairs = _syllables(text)
    content = [len(f) >= 2 and f not in _STOPWORDS for f, _ in pairs]
    terms: list[str] = []
    for (folded, accented), ok in zip(pairs, content, strict=True):
        if not ok:
            continue
        if query:
            terms.append(f"~{accented}" if accented != folded else folded)
        else:
            terms.append(folded)
            if accented != folded:
                terms.append(f"~{accented}")
    for i in range(len(pairs) - 1):
        if not (content[i] and content[i + 1]):
            continue
        (f1, a1), (f2, a2) = pairs[i], pairs[i + 1]
        both_accented = a1 != f1 and a2 != f2
        if query:
            terms.append(f"~{a1}_{a2}" if both_accented else f"{f1}_{f2}")
        else:
            terms.append(f"{f1}_{f2}")
            if a1 != f1 or a2 != f2:
                terms.append(f"~{a1}_{a2}")
    return terms


@dataclass
class _Index:
    chunks: list[Chunk]
    doc_terms: list[Counter]
    doc_len: list[int]
    df: Counter
    avgdl: float

    def idf(self, term: str) -> float:
        n = len(self.chunks)
        df = self.df.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))


_index_cache: tuple[tuple, _Index] | None = None


def _files_signature() -> tuple:
    sig = []
    for path in _iter_markdown_files():
        try:
            stat = path.stat()
        except OSError:
            continue
        sig.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(sig)


def _cap_chunks(chunks: list[Chunk]) -> list[Chunk]:
    kept: list[Chunk] = []
    chars = 0
    for chunk in chunks:
        chars += len(chunk.content) + len(chunk.heading)
        if len(kept) >= MAX_INDEX_CHUNKS or chars > MAX_INDEX_CHARS:
            logger.warning(
                "Kho rag/ vượt trần chỉ mục (%d chunk / %d ký tự): chỉ tra %d/%d chunk đầu.",
                MAX_INDEX_CHUNKS,
                MAX_INDEX_CHARS,
                len(kept),
                len(chunks),
            )
            break
        kept.append(chunk)
    return kept


def _build_index(chunks: list[Chunk]) -> _Index:
    chunks = _cap_chunks(chunks)
    doc_terms: list[Counter] = []
    df: Counter = Counter()
    for chunk in chunks:
        terms = Counter(index_terms(f"{chunk.heading} {chunk.heading} {chunk.content}"))
        doc_terms.append(terms)
        df.update(terms.keys())
    doc_len = [sum(c.values()) for c in doc_terms]
    avgdl = (sum(doc_len) / len(doc_len)) if doc_len else 1.0
    return _Index(chunks, doc_terms, doc_len, df, avgdl or 1.0)


def _get_index() -> _Index:
    """Chỉ mục BM25, đọc lại đĩa chỉ khi danh sách file/mtime/kích thước đổi."""
    global _index_cache
    signature = _files_signature()
    if _index_cache is not None and _index_cache[0] == signature:
        return _index_cache[1]
    index = _build_index(_load_chunks())
    _index_cache = (signature, index)
    return index


def clear_index_cache() -> None:
    global _index_cache
    _index_cache = None


def _bm25(index: _Index, query_terms: set[str]) -> list[tuple[float, int, int]]:
    """[(điểm chuẩn hoá, số âm tiết nội dung trùng, vị trí chunk)] chưa lọc."""
    if not index.chunks or not query_terms:
        return []
    idf = {term: index.idf(term) for term in query_terms}
    total_idf = sum(idf.values()) or 1.0
    results = []
    for pos, terms in enumerate(index.doc_terms):
        dl = index.doc_len[pos] or 1
        score = 0.0
        matched_unigrams = 0
        for term in query_terms:
            tf = terms.get(term, 0)
            if not tf:
                continue
            if "_" not in term:
                matched_unigrams += 1
            denom = tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / index.avgdl)
            score += idf[term] * tf * (BM25_K1 + 1) / denom
        if score > 0:
            results.append((score / ((BM25_K1 + 1) * total_idf), matched_unigrams, pos))
    results.sort(key=lambda r: r[0], reverse=True)
    return results


def search(
    query: str,
    top_n: int = MAX_MATCHES,
    *,
    min_score: float = MIN_NORMALIZED_SCORE,
    min_terms: int = MIN_MATCHED_TERMS,
    strict_terms: bool = False,
) -> list[Match]:
    """Các chunk khớp nhất (BM25 trên token bỏ dấu, không cần API). Câu hỏi chỉ
    có 1 token nội dung thì chỉ cần trùng 1 (trừ khi strict_terms)."""
    query_terms = set(index_terms(query, query=True))
    content_terms = {t for t in query_terms if "_" not in t}
    if not content_terms:
        return []
    need = min_terms if strict_terms else min(min_terms, len(content_terms))
    index = _get_index()
    matches = []
    for score, matched, pos in _bm25(index, query_terms):
        if score < min_score or matched < need:
            continue
        matches.append(Match(chunk=index.chunks[pos], score=score))
        if len(matches) >= top_n:
            break
    return matches


# Tra tự động trong chat thường (services/chat_context.kb_grounding): ngưỡng cao
# hơn /rag nhiều (đã hiệu chỉnh trên rag/c1.md: câu đúng chủ đề ~0,7-0,8, câu
# đời thường/cổ phiếu < 0,25) và bắt buộc trùng >= 2 token nội dung.
AUTO_MIN_SCORE = 0.5
AUTO_MIN_TERMS = 2
AUTO_MAX_CHARS = 1500


def auto_lookup(query: str) -> Match | None:
    """Chunk khớp RẤT rõ với câu chat (hoặc None) - không gọi AI."""
    matches = search(
        query, top_n=1, min_score=AUTO_MIN_SCORE, min_terms=AUTO_MIN_TERMS, strict_terms=True
    )
    return matches[0] if matches else None


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
    chunks = _get_index().chunks
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
    '- Không nhắc tới chữ "trích dẫn" hay quy trình - cứ trả lời trực tiếp.'
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


async def _ask_llm(
    user_id: int,
    query: str,
    matches: list[Match],
    *,
    channel: str = "telegram",
    is_admin: bool = False,
    address_form: str | None = None,
) -> str:
    from ai import orchestrator  # tránh import vòng khi ai.* import services.*

    grounding, prompt = build_prompt(query, matches)
    # search_mode="off": /rag CHỈ trả lời từ tài liệu riêng của chủ bot - nhánh
    # Gemini không được bật Google Search (trước đây bật ngầm vì không truyền
    # web_grounded, trộn kết quả web vào câu trả lời "chỉ từ tài liệu").
    # channel/is_admin/address_form: cùng biến thể persona + cách gọi như lượt
    # chat thường của người hỏi (admin Zalo không bị coi là thành viên + "anh").
    response = await orchestrator.chat(
        user_id,
        prompt,
        grounding=grounding,
        memory_context="",
        search_mode="off",
        channel=channel,
        is_admin=is_admin,
        address_form=address_form,
    )
    answer = (getattr(response, "text", "") or "").strip()
    if not answer:
        raise ValueError("LLM trả về rỗng")
    return answer


async def ask(
    user_id: int,
    query: str,
    *,
    channel: str = "telegram",
    is_admin: bool = False,
    address_form: str | None = None,
) -> str:
    """Flow chính của /rag: tìm chunk -> AI diễn giải (hoặc trả trích dẫn).

    Trả về text sẵn sàng gửi người dùng (đã kèm nguồn / gợi ý khi trượt).
    Telemetry do handler ở trên lo (kèm channel); ở đây chỉ log fallback.
    channel/is_admin/address_form chuyển xuống orchestrator.chat (Telegram -
    chủ bot - giữ mặc định: biến thể chủ bot chọn theo user_id).
    """
    matches = await asyncio.to_thread(search, query)
    if not matches:
        # Đọc chỉ mục (có thể phải đọc đĩa) ngoài event loop.
        return await asyncio.to_thread(_no_match_reply, query)

    try:
        answer = await _ask_llm(
            user_id,
            query,
            matches,
            channel=channel,
            is_admin=is_admin,
            address_form=address_form,
        )
    except Exception as exc:
        logger.warning("AI diễn giải /rag lỗi, fallback trả trích dẫn: %s", exc)
        return (
            f"⚠️ AI diễn giải lỗi nên em gửi nguyên trích dẫn nhé:\n\n{_format_citations(matches)}"
        )
    # Lưu câu hỏi GỐC (không phải khung build_prompt) + câu trả lời vào lịch sử:
    # "giải thích ý 2" ở lượt sau mới có ngữ cảnh.
    from services import conversation_log

    await conversation_log.record_exchange(
        user_id, f"/rag {query}", answer, conversation_log.KIND_RAG
    )
    return answer
