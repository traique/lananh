"""Câu do CODE viết, giọng Lan Anh, cho tin thoại, ảnh hỏi đáp, tài liệu và tin
nhắn loại chưa hỗ trợ (services/voice_service.py, vision_service.py,
document_service.py, handlers/unsupported.py).

Cùng quy ước với messages.friendly_error: ``{ai}``/``{Ai}`` là Lan Anh tự xưng,
``{tail}`` là " <cách gọi người nhận>" hoặc rỗng khi thành viên Zalo chưa được
đặt cách gọi (câu không gọi trống "anh"). Chủ bot luôn được gọi "anh" và được
xem thêm chi tiết kỹ thuật (``detail``); người khác không bao giờ thấy tên
provider hay nội dung exception.
"""

from __future__ import annotations

from core import address
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
