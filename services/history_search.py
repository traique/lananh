"""Tool search_history: tìm lại điều đã trao đổi trong lịch sử chat CỦA CHÍNH
người hỏi ("hôm trước em nói gì về quỹ ETF?", "lần trước anh hỏi em về bảo hiểm
gì nhỉ?").

Lịch sử phiên đưa vào prompt chỉ gồm phiên hiện tại; trí nhớ dài hạn chỉ giữ ý
chính. Tool này tìm nguyên văn trong chat_messages (mỗi người giữ tối đa
CHAT_MESSAGES_RETENTION_LIMIT dòng xoay vòng) theo user_id của lượt - Zalo (id âm)
và Telegram không bao giờ thấy lịch sử của nhau.

Tìm KHÔNG phân biệt dấu: SQL lọc thô bằng translate()+LIKE (core/repositories/
chat_history.search), Python chấm điểm theo số từ khoá khớp nguyên từ rồi lấy
vài lượt khớp nhất. Kết quả là lời cũ (có thể chép từ web/ảnh/tài liệu) nên được
rào core.untrusted và đánh dấu nội dung ngoài (chặn tool ghi dữ liệu cùng lượt).
Không cần bảng hay migration mới.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from core import config, database as db, untrusted
from core.text_normalize import fold
from services import tool_registry as reg

logger = logging.getLogger(__name__)

_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
SCAN_LIMIT = 300
MAX_HITS = 5
MAX_TERMS = 6
ROW_MAX_CHARS = 600
SNIPPET_CHARS = 280
RESULT_MAX_CHARS = 3500
# Từ của chính câu hỏi "hôm trước em nói gì về ..." - không phải nội dung cần tìm.
_QUERY_STOPWORDS = frozenset(
    fold(word)
    for word in """hôm trước lần bữa nói hỏi đã từng nhắc lại tìm chat lịch sử trao đổi
    bảo kể nào nhỉ hả vừa rồi qua tuần tháng ấy đấy nhé""".split()
)


@dataclass(frozen=True)
class HistoryHit:
    row_id: int
    role: str
    content: str
    pair_content: str
    created_at: datetime | None
    score: int


def query_terms(query: str) -> list[str]:
    """Từ khoá đã bỏ dấu (a-z0-9) của câu cần tìm, bỏ hư từ và từ hỏi lịch sử."""
    from services.rag_service import tokenize

    terms = [
        term
        for term in tokenize(query)
        if term.isascii() and term.isalnum() and term not in _QUERY_STOPWORDS
    ]
    return sorted(terms, key=lambda term: (-len(term), term))[:MAX_TERMS]


def _score(text: str, terms: list[str]) -> int:
    folded = fold(text)
    return sum(1 for term in terms if re.search(rf"\b{re.escape(term)}\b", folded))


async def search(user_id: int, query: str) -> tuple[list[str], list[HistoryHit]]:
    """(từ khoá đã dùng, các lượt khớp nhất - điểm cao trước, cùng điểm thì mới trước)."""
    terms = query_terms(query)
    if not terms:
        return terms, []
    rows = await db.search_chat_history(
        user_id, terms, scan_limit=SCAN_LIMIT, max_chars=ROW_MAX_CHARS
    )
    need = 1 if len(terms) == 1 else 2
    best: dict[int, HistoryHit] = {}
    for row in rows:
        content = str(row.get("content") or "")
        pair = str(row.get("pair_content") or "")
        score = _score(f"{content}\n{pair}", terms)
        if score < need:
            continue
        row_id = int(row["id"])
        # 1 lượt (hỏi + đáp) chỉ hiện 1 lần dù cả 2 dòng cùng khớp.
        turn_key = row_id if row.get("role") == "user" else row_id - 1
        hit = HistoryHit(
            row_id, str(row.get("role") or ""), content, pair, row.get("created_at"), score
        )
        if turn_key not in best or best[turn_key].score < score:
            best[turn_key] = hit
    hits = sorted(best.values(), key=lambda h: (-h.score, -h.row_id))[:MAX_HITS]
    return terms, hits


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= SNIPPET_CHARS else text[: SNIPPET_CHARS - 1] + "…"


def _when(stamp: datetime | None) -> str:
    if not isinstance(stamp, datetime):
        return "?"
    return stamp.astimezone(_VN_TZ).strftime("%d/%m/%Y %H:%M")


def format_hits(hits: list[HistoryHit]) -> str:
    lines = []
    for hit in hits:
        question, answer = (
            (hit.content, hit.pair_content)
            if hit.role == "user"
            else (hit.pair_content, hit.content)
        )
        line = f"• {_when(hit.created_at)} - Người dùng: «{_clip(question) or '…'}»"
        if answer:
            line += f"\n  Lan Anh trả lời: «{_clip(answer)}»"
        lines.append(line)
    return "\n".join(lines)


async def _tool_search_history(user_id: int, query: str = "", **_ignored) -> reg.ToolResult:
    query = str(query or "").strip()
    if not query:
        return reg.ToolResult(
            "Chưa rõ cần tìm điều gì trong lịch sử - hỏi lại người dùng từ khoá.",
            status=reg.STATUS_INVALID,
        )
    terms, hits = await search(user_id, query)
    if not hits:
        return reg.ToolResult(
            f"Không tìm thấy lượt trò chuyện cũ nào khớp «{query}» (chỉ lưu khoảng "
            f"{config.CHAT_MESSAGES_RETENTION_LIMIT} tin gần nhất). Nói thật là không thấy, "
            "KHÔNG bịa nội dung cuộc trò chuyện cũ."
        )
    fenced = untrusted.wrap_untrusted(
        format_hits(hits), "lịch sử chat cũ", max_chars=RESULT_MAX_CHARS
    )
    return reg.ToolResult(
        f"Các lượt trò chuyện cũ của CHÍNH người dùng khớp «{query}» (từ khoá: "
        f"{', '.join(terms)}). Trả lời dựa trên đây, nêu ngày nếu hữu ích; không thấy ý "
        f"người dùng hỏi thì nói thật:\n{fenced}"
    )


SPEC = reg.register(
    reg.ToolSpec(
        name="search_history",
        description="Tìm lại điều đã trao đổi trước đây trong lịch sử chat của chính người dùng.",
        parameters=reg.object_schema(
            {"query": reg.string("từ khoá nội dung cần tìm, bỏ từ hỏi")}, ["query"]
        ),
        handler=_tool_search_history,
        untrusted_output=True,
        router_hint=(
            'args: {"query": "từ khoá nội dung cần tìm, vd \\"quỹ ETF\\""}. Dùng khi người dùng '
            'hỏi lại điều ĐÃ nói/hỏi trước đây ("hôm trước em nói gì về quỹ ETF", "lần trước '
            'anh hỏi em về bảo hiểm gì nhỉ", "tìm lại chỗ em bảo về thuế"). Câu hỏi kiến thức '
            "mới (không nhắc chuyện cũ) thì KHÔNG dùng."
        ),
    )
)
