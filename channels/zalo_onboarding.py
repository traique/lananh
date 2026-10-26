"""Cấp quyền Zalo kèm lời chào cho người vừa được pair.

Trước đây /zalopair, /zaloadmin chỉ trả lời chủ bot trên Telegram: người được
cấp quyền không biết mình đã dùng được bot, còn tin đầu tiên của họ (lúc chưa
pair) đã bị bỏ qua âm thầm (channels/router.py) và không bao giờ được trả lời.

Giờ lần pair ĐẦU TIÊN xếp 1 lời chào giọng Lan Anh vào outbox Zalo (gateway gửi
dạng tin riêng tới external_id) với vài câu thử chạy được thật và lời nhắn gửi
lại câu vừa rồi - không giữ/phát lại tin cũ trong RAM (Render free ngủ/khởi
động lại là mất, và cần 1 lượt chat chạy nền ngoài request). Pair lại/đổi tên
không chào lại; nâng thành viên lên admin chỉ báo 1 câu ngắn. Tắt bằng
ZALO_WELCOME_ON_PAIR=0.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from channels import zalo_repository, zalo_session, zalo_users
from core import address
from core.envutil import env_on

logger = logging.getLogger(__name__)

# Trạng thái lời chào báo lại cho chủ bot.
WELCOME_QUEUED = "queued"
WELCOME_NONE = "none"  # pair lại / đổi tên: không gửi gì
WELCOME_DISABLED = "disabled"  # ZALO_WELCOME_ON_PAIR=0
WELCOME_NO_ACCOUNT = "no_account"  # Zalo bot chưa đăng nhập
WELCOME_FAILED = "failed"

# Câu thử nhanh: chỉ tính năng chạy được thật với thành viên (thời tiết
# Open-Meteo, nhắc việc qua Zalo, giá vàng tra web, giá cổ phiếu DNSE). KHÔNG
# quảng cáo "gửi ảnh rồi hỏi": ảnh Zalo chỉ được viết thành prompt tạo ảnh.
_EXAMPLES = (
    "mai Hà Nội mưa không",
    "nhắc tôi 8h tối gọi mẹ",
    "giá vàng hôm nay",
    "giá FPT hôm nay",
)
_ADMIN_FEATURES = "lệnh nhóm Zalo (/nhom, /tongket) và đăng Facebook (/fb_*)"


@dataclass(frozen=True)
class PairResult:
    user: zalo_users.ZaloUser
    created: bool  # lần pair đầu tiên của external_id này
    promoted: bool  # thành viên có sẵn vừa được nâng lên admin
    welcome: str  # WELCOME_*


def _words(user: zalo_users.ZaloUser) -> tuple[str, str]:
    """(cách gọi người dùng, Lan Anh tự xưng); chưa đặt cách gọi -> ("", "em")."""
    if user.address_form:
        return user.address_form, address.self_word(user.address_form)
    return "", "em"


def welcome_text(user: zalo_users.ZaloUser) -> str:
    """Lời chào lần đầu (chữ thuần cho Zalo). Chưa có cách gọi riêng thì viết
    câu không gọi trống "anh" - tránh gọi nhầm mẹ, chị, đồng nghiệp nữ là anh."""
    you, me = _words(user)
    Me = me.capitalize()
    if you:
        lines = [
            f"🌸 {Me} chào {you} ạ! {Me} là Lan Anh, trợ lý AI trên Zalo này - từ giờ "
            f"{you} cứ nhắn thẳng cho {me} là được nha.",
        ]
    else:
        lines = [
            "🌸 Xin chào! Em là Lan Anh, trợ lý AI trên Zalo này - từ giờ cứ nhắn thẳng "
            "cho em là được nha.",
        ]
    lines.append("Vài câu thử nhanh:")
    lines.extend(f"• {example}" for example in _EXAMPLES)
    if user.is_admin:
        owner_of = f"{you.capitalize()} có" if you else "Tài khoản này có"
        lines.append(f"🔑 {owner_of} quyền quản trị: dùng thêm được {_ADMIN_FEATURES}.")
    tail = f" {you}" if you else ""
    lines.append(
        f"Gõ /help để xem hết các lệnh. Nếu lúc nãy đã nhắn mà chưa thấy {me} trả lời "
        f"thì gửi lại câu đó giúp {me} nha{tail}."
    )
    return "\n".join(lines)


def promoted_text(user: zalo_users.ZaloUser) -> str:
    you, me = _words(user)
    subject = f"{me.capitalize()} báo {you} nè: tài khoản" if you else "Tài khoản này"
    return (
        f"🔑 {subject} vừa được nâng quyền quản trị, dùng thêm được {_ADMIN_FEATURES}. "
        "Gõ /help để xem đủ lệnh nha."
    )


async def _enqueue(external_id: str, text: str) -> str:
    """Xếp 1 tin riêng vào outbox của tài khoản Zalo bot ĐANG đăng nhập (account
    id gateway dùng để lấy outbox). Chưa đăng nhập thì bỏ qua - tin xếp vào
    account đoán mò có thể không bao giờ được gửi."""
    try:
        account_id = await zalo_session.load_account_id()
    except Exception:
        logger.warning("Không đọc được session Zalo để gửi lời chào.", exc_info=True)
        return WELCOME_FAILED
    if not account_id:
        return WELCOME_NO_ACCOUNT
    try:
        await zalo_repository.enqueue_outbox(account_id, external_id, text)
    except Exception:
        logger.warning("Không xếp được lời chào Zalo cho %s.", external_id, exc_info=True)
        return WELCOME_FAILED
    return WELCOME_QUEUED


async def pair(
    external_id: str,
    display_name: str = "",
    *,
    admin: bool = False,
    address_form: str | None = None,
) -> PairResult:
    """Pair (zalo_users.pair / pair_as_admin) rồi chào nếu là lần đầu. Lỗi xếp
    lời chào không làm hỏng việc cấp quyền."""
    existing = await zalo_users.resolve(external_id)
    if admin:
        user = await zalo_users.pair_as_admin(external_id, display_name, address_form=address_form)
    else:
        user = await zalo_users.pair(external_id, display_name, address_form=address_form)
    created = existing is None
    promoted = existing is not None and not existing.is_admin and user.is_admin
    if not created and not promoted:
        return PairResult(user, created, promoted, WELCOME_NONE)
    if not env_on("ZALO_WELCOME_ON_PAIR"):
        return PairResult(user, created, promoted, WELCOME_DISABLED)
    text = welcome_text(user) if created else promoted_text(user)
    return PairResult(user, created, promoted, await _enqueue(user.external_id, text))
