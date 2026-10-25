"""Các chuỗi thông báo gửi cho người dùng (Telegram) dùng chung nhiều nơi.

Gom về 1 chỗ để dễ sửa văn phong / dịch đa ngôn ngữ sau này, thay vì rải rác
literal string trong logic nghiệp vụ.
"""

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
STOCK_FETCH_ERROR = (
    "Em không lấy được dữ liệu giá cho mã {symbol} lúc này, anh thử lại sau ít phút nhé."
)
STOCK_ANALYZE_FAILED = "❌ Lỗi khi phân tích {symbol}, bỏ qua mã này."
STOCK_QUOTE_FAILED = "❌ Lỗi khi lấy giá {symbol}, bỏ qua mã này."
# Dùng khi tin nhắn rõ ràng đang hỏi giá nhưng hệ thống không nhận ra mã nào.
# Thà nói thẳng là không tra được, còn hơn để Gemini trả lời không có dữ liệu
# thật và bịa ra một con số nghe hợp lý.
STOCK_SYMBOL_UNRESOLVED = (
    "Anh ơi em chưa tra ra mã cổ phiếu nào trong câu này nên không dám đọc giá đại đâu ạ. "
    'Anh gõ lại mã in HOA giúp em nha (vd: GVR), hoặc thêm chữ "cổ phiếu" phía trước.'
)

# ─── Chat & lệnh chung ───────────────────────────────────────────────────────
INVALID_COMMAND = "Lệnh không hợp lệ. Gõ /help để xem danh sách lệnh."
CHAT_GENERIC_ERROR = "Gemini không phản hồi gì, thử lại nhé."
PHOTO_TIMEOUT_ERROR = "❌ Tải ảnh từ Telegram bị timeout. Anh gửi lại ảnh hoặc thử ảnh nhỏ hơn nhé."

# --- runtime ---
WEBHOOK_DEAD_ALERT = (
    "⚠️ Sự kiện {channel}/{event_id} lỗi {attempts} lần liên tiếp ({error}) - "
    "đã ngừng thử lại (DEAD). Xem log server nếu cần xử lý tay."
)
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
