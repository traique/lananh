"""Câu do CODE viết, giọng Lan Anh, cho tin thoại, ảnh hỏi đáp, tài liệu và tin
nhắn loại chưa hỗ trợ (services/voice_service.py, vision_service.py,
document_service.py, handlers/unsupported.py, câu báo của gateway Zalo -
channels/zalo_media.py POST /notice).

Cùng quy ước với messages.friendly_error: ``{ai}``/``{Ai}`` là Lan Anh tự xưng,
``{tail}`` là " <cách gọi người nhận>" hoặc rỗng khi thành viên Zalo chưa được
đặt cách gọi (câu không gọi trống "anh"). Chủ bot luôn được gọi "anh" và được
xem thêm chi tiết kỹ thuật (``detail``); người khác không bao giờ thấy tên
provider hay nội dung exception.
"""

from __future__ import annotations

from core import address, config
from services import reply_audience


def forms(*, owner: bool, address_form: str | None) -> tuple[str, str]:
    """(cách gọi người nhận - rỗng nếu chưa đặt, Lan Anh tự xưng)."""
    if owner:
        return "anh", "em"
    form = address.normalize(address_form)
    return (form, address.self_word(form)) if form else ("", "em")


def say(
    template: str,
    *,
    owner: bool = False,
    address_form: str | None = None,
    detail: str = "",
    **fields: object,
) -> str:
    """Điền ``template`` theo người nhận; ``detail`` chỉ nối thêm cho chủ bot."""
    you, me = forms(owner=owner, address_form=address_form)
    text = template.format(ai=me, Ai=me.capitalize(), tail=f" {you}" if you else "", **fields)
    if owner and detail:
        text += f"\n🔎 {detail}"
    return text


async def say_to(template: str, user_id: int, *, detail: str = "", **fields: object) -> str:
    """``say`` cho người nhận của lượt hiện tại (services/reply_audience.py)."""
    return say(
        template,
        owner=await reply_audience.is_owner(user_id),
        address_form=reply_audience.current().address_form,
        detail=detail,
        **fields,
    )


def persona_forms(user_id: int) -> tuple[str, str, bool]:
    """(cách gọi, tự xưng, là chủ bot Telegram/Zoom) cho persona rút gọn của chỉ
    dẫn ảnh/tài liệu: thành viên Zalo theo cách gọi riêng, chưa đặt thì "anh" như
    persona mặc định (services/channel_chat_service._forms)."""
    owner = config.is_owner_user_id(user_id)
    form = None if owner else address.normalize(reply_audience.current().address_form)
    you = form or "anh"
    return you, address.self_word(you), owner


# ─── Tin nhắn loại chưa hỗ trợ (handlers/unsupported.py) ─────────────────────
_SUPPORTED = (
    "{Ai} hiểu được: tin nhắn chữ, ảnh (kèm câu hỏi để {ai} xem ảnh, không kèm thì {ai} "
    "viết prompt), tin thoại, file PDF/TXT/Word và vị trí (để {ai} báo thời tiết chỗ đó) "
    "nha{tail}."
)
_UNSUPPORTED = {
    "sticker": "😆 Sticker dễ thương ghê! Có gì cần {ai} giúp thì nhắn chữ hoặc gửi tin thoại nha{tail}.",
    "video": "🎬 {Ai} chưa xem được video hay ảnh động. " + _SUPPORTED,
    "document": "📄 {Ai} mới đọc được file PDF, TXT/MD, Word (.docx) và ảnh thôi. " + _SUPPORTED,
    "contact": "👤 {Ai} chưa lưu được danh bạ gửi qua chat. " + _SUPPORTED,
    "other": "{Ai} chưa xử lý được loại tin này. " + _SUPPORTED,
}


def unsupported_text(kind: str, *, owner: bool = False, address_form: str | None = None) -> str:
    """Câu giọng Lan Anh cho tin nhắn loại chưa hỗ trợ (sticker, video, file lạ...)."""
    return say(
        _UNSUPPORTED.get(kind, _UNSUPPORTED["other"]), owner=owner, address_form=address_form
    )


# ─── Câu báo của gateway Zalo (channels/zalo_media.py POST /notice) ──────────
# Gateway gửi các câu này mà không qua lượt AI: "đã nhận ảnh" TRƯỚC khi xem ảnh,
# tin thoại/file không tải được hoặc quá lớn, loại tin Zalo chưa hỗ trợ. Ngắn,
# 1 câu - khác _UNSUPPORTED của Telegram (Zalo chưa đọc được vị trí).
_ZALO_NOTICES = {
    "image": "🖼️ {Ai} nhận được ảnh rồi, đợi {ai} xem xíu nha{tail}…",
    "voice_failed": "🎙️ {Ai} chưa nghe được tin thoại này, gõ giúp {ai} nha{tail}.",
    "voice_too_large": (
        "🎙️ Tin thoại này dài quá, {ai} chưa nghe hết được. Nhắn ngắn hơn hoặc gõ giúp "
        "{ai} nha{tail}."
    ),
    "file_failed": "📎 {Ai} chưa tải được file này từ Zalo, gửi lại giúp {ai} nha{tail}.",
    "file_too_large": "📎 File này lớn quá, {ai} chỉ đọc được file tới {mb} MB thôi nha{tail}.",
    "file_unsupported": "📎 {Ai} mới đọc được file PDF, Word (.docx) và TXT/MD thôi nha{tail}.",
    "video": (
        "🎬 {Ai} chưa xem được video nha{tail}. Gửi ảnh chụp màn hình hoặc kể {ai} nghe "
        "bằng chữ nhé."
    ),
    "location": (
        '📍 {Ai} chưa đọc được vị trí gửi qua Zalo nha{tail}. Nhắn tên nơi (vd "thời tiết '
        'Đà Lạt") là {ai} xem giúp liền.'
    ),
}
ZALO_NOTICE_KINDS = frozenset(_ZALO_NOTICES)


def zalo_notice_text(
    kind: str, *, owner: bool = False, address_form: str | None = None, mb: int = 5
) -> str:
    """Câu báo ngắn gateway Zalo gửi không qua AI, theo cách gọi riêng của người nhận."""
    return say(_ZALO_NOTICES[kind], owner=owner, address_form=address_form, mb=mb)
