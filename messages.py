"""Các chuỗi thông báo gửi cho người dùng (Telegram) dùng chung nhiều nơi.

Gom về 1 chỗ để dễ sửa văn phong / dịch đa ngôn ngữ sau này, thay vì rải rác
literal string trong logic nghiệp vụ.
"""

from core import address

# ─── Provider-chain (router9 <-> api1/api2) ─────────────────────────────────
ROUTER9_DEAD_ALERT = (
    "⚠️ 9Router vừa lỗi, đã chuyển hẳn sang Google AI Studio API. "
    "Em sẽ tự thử lại 9Router định kỳ, hoặc anh gõ /userouter9 để ép thử lại ngay."
)
ROUTER9_ALIVE_ALERT = "✅ 9Router đã hoạt động trở lại, em quay về dùng 9Router nhé."

# services/monitor_service.py - cảnh báo khi TOÀN BỘ provider trong
# PROVIDER_ORDER cùng lúc không dùng được (khác ROUTER9_DEAD_ALERT ở trên -
# cái đó chỉ báo router9 chết, còn lananh vẫn trả lời bình thường qua
# api1/api2; cảnh báo này báo tình huống nặng hơn: KHÔNG provider nào trả
# lời được, mọi lệnh AI sẽ lỗi cho tới khi có provider hồi phục).
ALL_PROVIDERS_DOWN_ALERT = (
    "🚨 TOÀN BỘ provider trong PROVIDER_ORDER hiện đều không dùng được "
    "(9Router chết/tắt + các provider còn lại đều đang cooldown hết quota). "
    "Mọi lệnh AI sẽ báo lỗi cho tới khi có provider hồi phục - gõ /status để xem chi tiết."
)
ALL_PROVIDERS_RECOVERED_ALERT = (
    "✅ Đã có ít nhất 1 provider dùng lại được, bot hoạt động bình thường trở lại."
)

# ─── Zalo Team Chat (nhiều tài khoản, phân quyền admin/user) ───────────────
ZALO_LOCKED_REPLY = "Tài khoản Zalo đang bị tạm khóa."

# ─── Zoom Team Chat ──────────────────────────────────────────────────────────
ZOOM_UNPAIRED_ALERT = (
    "🔔 Zoom jid={jid} vừa nhắn cho bot nhưng chưa được cấp quyền.\n"
    "Dùng /zoompair {jid} để cấp quyền."
)

# ─── Phân tích cổ phiếu ──────────────────────────────────────────────────────
# Câu trung tính (không "anh"/"em"): stock/analysis.py dùng chung cho chủ bot
# Telegram lẫn thành viên Zalo - tầng stock/ không biết người nhận là ai.
STOCK_FETCH_ERROR = "Chưa lấy được dữ liệu giá mã {symbol} lúc này, thử lại sau ít phút nha."
STOCK_ANALYZE_FAILED = "❌ Lỗi khi phân tích {symbol}, bỏ qua mã này."
STOCK_QUOTE_FAILED = "❌ Lỗi khi lấy giá {symbol}, bỏ qua mã này."
# Dùng khi tin nhắn rõ ràng đang hỏi giá nhưng hệ thống không nhận ra mã nào.
# Thà nói thẳng là không tra được, còn hơn để Gemini trả lời không có dữ liệu
# thật và bịa ra một con số nghe hợp lý.
STOCK_SYMBOL_UNRESOLVED = (
    "Anh ơi em chưa tra ra mã cổ phiếu nào trong câu này nên không dám đọc giá đại đâu ạ. "
    'Anh gõ lại mã in HOA giúp em nha (vd: GVR), hoặc thêm chữ "cổ phiếu" phía trước.'
)


def stock_symbol_unresolved(address_form: str | None = None, *, owner: bool = False) -> str:
    """STOCK_SYMBOL_UNRESOLVED theo người nhận: chủ bot giữ câu gọi "anh"; thành
    viên Zalo theo cách gọi riêng (core/address.py), chưa đặt thì câu trung tính."""
    form = "anh" if owner else address.normalize(address_form)
    if form == "anh":
        return STOCK_SYMBOL_UNRESOLVED
    if not form:
        return (
            "Em chưa tra ra mã cổ phiếu nào trong câu này nên không dám đọc giá đại đâu ạ. "
            'Gõ lại mã in HOA giúp em nha (vd: GVR), hoặc thêm chữ "cổ phiếu" phía trước.'
        )
    me = address.self_word(form)
    return (
        f"{form.capitalize()} ơi {me} chưa tra ra mã cổ phiếu nào trong câu này nên không dám "
        f"đọc giá đại đâu ạ. {form.capitalize()} gõ lại mã in HOA giúp {me} nha (vd: GVR), "
        'hoặc thêm chữ "cổ phiếu" phía trước.'
    )


# ─── Chat & lệnh chung ───────────────────────────────────────────────────────
INVALID_COMMAND = "Lệnh không hợp lệ. Gõ /help để xem danh sách lệnh."
PHOTO_TIMEOUT_ERROR = "❌ Tải ảnh từ Telegram bị timeout. Anh gửi lại ảnh hoặc thử ảnh nhỏ hơn nhé."

# --- runtime ---
WEBHOOK_DEAD_ALERT = (
    "⚠️ Sự kiện {channel}/{event_id} lỗi {attempts} lần liên tiếp ({error}) - "
    "đã ngừng thử lại (DEAD). Xem log server nếu cần xử lý tay."
)
# Thông điệp của TurnTimeoutError (ai/timeouts.py) - dùng cho log và làm chi tiết
# kỹ thuật cho chủ bot; người dùng nhận câu friendly_error("timeout").
CHAT_TURN_TIMEOUT_ERROR = "Hết {seconds}s chờ AI trả lời (mọi provider đều chậm/lỗi)."
# core/untrusted.py - dữ liệu ngoài (web/RSS/nhóm Zalo) có câu giống prompt
# injection. Chỉ báo (giãn cách 1 giờ), không chặn gì.
UNTRUSTED_INJECTION_ALERT = (
    "🛡️ Dữ liệu ngoài ({source}) có câu nghi là prompt injection: “{phrase}”. "
    "Em đã rào nó như dữ liệu tham khảo, không làm theo - anh để ý nếu câu trả lời lạ."
)
# core/webhook_inbox.py - lỗi chắc chắn lặp lại nếu chạy lại (lỗi lập trình,
# Telegram từ chối tin, hết hạn chót lượt): không thử lại kẻo gửi trùng muộn.
WEBHOOK_NO_RETRY_ALERT = (
    "⚠️ Sự kiện {channel}/{event_id} lỗi {error} - lỗi kiểu này chạy lại vẫn hỏng nên em "
    "KHÔNG thử lại (tránh tốn quota và gửi trùng). Xem log server nếu cần xử lý tay."
)
MARKET_PAGE_UNCERTAIN_ALERT = (
    "⚠️ Page chứng khoán: lượt đăng {job} ({key}) bị gián đoạn giữa chừng nên chưa rõ "
    "bài đã lên Facebook hay chưa. Bot KHÔNG tự đăng lại - anh kiểm tra Page, "
    "nếu chưa có bài thì đăng tay bằng /fb_market {job} dang."
)

# --- zalo gateway ---
ZALO_OUTBOX_FAILED_ALERT = (
    "⚠️ Tin Zalo #{item_id} gửi tới {recipient} lỗi {attempts} lần liên tiếp ({error}) - "
    "đã ngừng thử lại để không chặn các tin sau."
)

# ─── Câu báo lỗi giọng Lan Anh ───────────────────────────────────────────────
# Mỗi câu nêu 1 lý do ngắn (AI đang bận / chậm / ảnh không đọc được) và việc nên
# làm tiếp. KHÔNG bao giờ có tên provider (lỗi có thể từ 9Router, Groq,
# OpenRouter... chứ không riêng Gemini) hay nội dung exception - thân response
# upstream có thể lọt vào thông điệp lỗi. Chỉ chủ bot (Telegram/Zoom, Zalo
# controller) được thêm chi tiết kỹ thuật + gợi ý /status để khỏi mò log Render.
# {ai}/{Ai}: Lan Anh tự xưng; {tail}: " <cách gọi người nhận>" hoặc rỗng khi
# thành viên chưa được đặt cách gọi (câu không gọi trống "anh").
_ERROR_TEMPLATES = {
    "chat": (
        "😥 Bên AI đang bận nên {ai} chưa trả lời được tin vừa rồi. "
        "Gửi lại giúp {ai} sau khoảng 1 phút nha{tail}."
    ),
    "timeout": (
        "⏱️ AI trả lời chậm quá nên {ai} đành dừng lượt này. "
        "Gửi lại giúp {ai} sau ít phút nha{tail}."
    ),
    "empty": "{Ai} chưa nghĩ ra câu trả lời cho tin này. Hỏi lại theo cách khác giúp {ai} nha{tail}.",
    "image": (
        "😥 {Ai} chưa đọc được ảnh này (AI đang bận hoặc ảnh không xem được). "
        "Thử lại sau ít phút hoặc gửi ảnh khác giúp {ai} nha{tail}."
    ),
    "image_empty": "{Ai} xem ảnh rồi mà chưa viết ra được prompt. Thử ảnh khác giúp {ai} nha{tail}.",
    "image_download": (
        "📥 {Ai} chưa tải được ảnh về (mạng chập chờn hoặc ảnh lỗi). "
        "Gửi lại ảnh giúp {ai} nha{tail}."
    ),
    "prompt": (
        "😥 Bên AI đang bận nên {ai} chưa viết được prompt. Thử lại giúp {ai} sau ít phút nha{tail}."
    ),
    "prompt_empty": (
        "{Ai} chưa viết ra được prompt cho mô tả này. Tả kỹ hơn một chút giúp {ai} nha{tail}."
    ),
    "price": "😥 {Ai} chưa tìm được giá lúc này. Thử lại giúp {ai} sau ít phút nha{tail}.",
    "unexpected": (
        "😥 Có trục trặc ngoài ý muốn nên {ai} chưa làm xong việc vừa rồi. "
        "Thử lại giúp {ai} sau ít phút nha{tail}."
    ),
}
# Lỗi phía provider: chủ bot được nhắc xem /status.
_OWNER_STATUS_HINT_KINDS = frozenset({"chat", "timeout", "image", "prompt"})
OWNER_STATUS_HINT = "Anh gõ /status xem provider nào đang sống nhé."


def friendly_error(
    kind: str, *, owner: bool = False, address_form: str | None = None, detail: str = ""
) -> str:
    """Câu báo lỗi giọng Lan Anh cho người dùng.

    owner=True: gọi "anh", thêm chi tiết kỹ thuật ``detail`` (nếu có) và gợi ý
    /status. Người khác: theo cách gọi riêng ``address_form`` (core/address.py)
    hoặc câu trung tính; ``detail`` bị bỏ qua - không lộ exception/provider."""
    template = _ERROR_TEMPLATES.get(kind, _ERROR_TEMPLATES["unexpected"])
    if owner:
        you, me = "anh", "em"
    else:
        form = address.normalize(address_form)
        you, me = (form, address.self_word(form)) if form else ("", "em")
    text = template.format(ai=me, Ai=me.capitalize(), tail=f" {you}" if you else "")
    if not owner:
        return text
    if detail:
        text += f"\n🔎 {detail}"
    if kind in _OWNER_STATUS_HINT_KINDS:
        text += f"\n{OWNER_STATUS_HINT}"
    return text


def exception_detail(exc: BaseException, limit: int = 300) -> str:
    """ "<Loại lỗi>: <thông điệp>" rút gọn - chi tiết cho friendly_error(owner=True)."""
    detail = f"{type(exc).__name__}: {exc}"
    return detail if len(detail) <= limit else detail[:limit] + "…"


# Model trả về rỗng (chủ bot - Telegram/Zoom).
CHAT_GENERIC_ERROR = friendly_error("empty", owner=True)
