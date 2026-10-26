"""Lệnh Telegram cấp/gỡ quyền kênh phụ: pairing Zoom (/zoompair, /zoomxoa,
/zoomstatus) và quản lý user Zalo (/zalopair, /zaloadmin, /zalohaquyen,
/zalokhoa, /zalomokhoa, /zaloxoa, /zalodanhsach)."""

import html

from telegram import Update
from telegram.ext import ContextTypes

from channels import zalo_onboarding, zalo_users
from core import address, config, database as db
from handlers import common


@common.restricted
async def zoompair_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zoompair <jid> [tên hiển thị] — cấp quyền cho ĐÚNG 1 jid Zoom được
    nói chuyện với bot (thay pairing cũ nếu có)."""
    arg = common.extract_arg(context)
    if not arg:
        await update.message.reply_text(
            "Cách dùng: /zoompair <jid> [tên hiển thị]\n"
            "Lấy jid từ log bot khi có người lạ nhắn Zoom cho bot, hoặc từ "
            "cảnh báo bot tự gửi khi có jid chưa pair nhắn tới."
        )
        return
    parts = arg.split(maxsplit=1)
    jid = parts[0].strip()
    display_name = parts[1].strip() if len(parts) > 1 else ""
    await db.zoom_set_pairing(jid, display_name)
    label = f" ({display_name})" if display_name else ""
    await update.message.reply_text(
        f"✅ Đã cấp quyền Zoom cho jid: {html.escape(jid)}{html.escape(label)}"
    )


@common.restricted
async def zoomxoa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zoomxoa — gỡ pairing Zoom hiện tại (không ai được nhắn cho bot qua Zoom nữa)."""
    pairing = await db.zoom_get_pairing()
    if pairing is None:
        await update.message.reply_text("Hiện chưa pair jid Zoom nào.")
        return
    await db.zoom_clear_pairing()
    await update.message.reply_text(f"🗑️ Đã gỡ pairing Zoom (jid cũ: {html.escape(pairing[0])}).")


@common.restricted
async def zoomstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zoomstatus — xem jid Zoom đang được pair (nếu có)."""
    if not config.ZOOM_ENABLED:
        await update.message.reply_text("⚪ Kênh Zoom đang tắt (ZOOM_ENABLED=false).")
        return
    pairing = await db.zoom_get_pairing()
    if pairing is None:
        await update.message.reply_text(
            "⚪ Kênh Zoom đang bật nhưng chưa pair jid nào.\nDùng /zoompair <jid> để cấp quyền."
        )
        return
    jid, name = pairing
    label = f" ({name})" if name else ""
    await update.message.reply_text(
        f"✅ Zoom đang pair với jid: {html.escape(jid)}{html.escape(label)}"
    )


_ZALO_ROLE_ICON = {"admin": "👑", "user": "👤"}
_ZALO_STATUS_LABEL = {"active": "hoạt động", "suspended": "đã khóa"}
_ADDRESS_CHOICES = "|".join(address.ADDRESS_FORMS)


def _parse_pair_arg(arg: str) -> tuple[str, str, str | None]:
    """ "<id> [tên] [xưng hô]" -> (id, tên, cách xưng hô). Xưng hô là token viết
    thường cuối cùng thuộc core/address.ADDRESS_FORMS ("Mẹ cô" -> tên "Mẹ", gọi
    "cô"); "Tuấn Anh" viết hoa vẫn là tên."""
    external_id, _, rest = arg.strip().partition(" ")
    display_name, form = address.split_trailing_form(rest)
    return external_id.strip(), display_name, form


def _address_note(user: zalo_users.ZaloUser) -> str:
    if not user.address_form:
        return ""
    return (
        f"\nLan Anh sẽ gọi người này là “{user.address_form}”, "
        f"xưng “{address.self_word(user.address_form)}”."
    )


def _welcome_note(result: zalo_onboarding.PairResult) -> str:
    """Báo chủ bot lời chào (lần pair đầu) / câu báo nâng quyền đã đi đâu."""
    if result.welcome == zalo_onboarding.WELCOME_QUEUED:
        sent = "câu báo nâng quyền" if result.promoted else "lời chào kèm vài câu thử"
        return f"\n💌 Đã xếp {sent} gửi tới người này qua Zalo."
    if result.welcome == zalo_onboarding.WELCOME_NO_ACCOUNT:
        return (
            "\n⚠️ Chưa gửi được lời chào: Zalo bot chưa đăng nhập (/zalo). Anh nhắn người "
            "này biết là đã dùng được bot nhé."
        )
    if result.welcome == zalo_onboarding.WELCOME_FAILED:
        return "\n⚠️ Không xếp được lời chào qua Zalo (xem log)."
    return ""


@common.restricted
async def zalopair_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zalopair <id_zalo> [tên] [anh|chị|cô|chú|bạn|em] — cấp quyền THÀNH VIÊN
    (chỉ tính năng bình thường: chat, /prompt, /gia, /dich... KHÔNG có lệnh
    nhóm). Từ xưng hô cuối (viết thường) là cách Lan Anh gọi người này."""
    arg = common.extract_arg(context)
    if not arg:
        await update.message.reply_text(
            f"Cú pháp: /zalopair <id_zalo> [tên hiển thị] [{_ADDRESS_CHOICES}]\n"
            "Lấy id_zalo từ cảnh báo bot tự gửi khi có người lạ nhắn Zalo tới bot.\n"
            "Từ cuối (viết thường) là cách Lan Anh gọi người này, vd: /zalopair 123 Mẹ cô "
            "(bỏ trống = gọi “anh”). Gõ lại lệnh để đổi tên hoặc cách gọi."
        )
        return
    external_id, display_name, form = _parse_pair_arg(arg)
    result = await zalo_onboarding.pair(external_id, display_name, address_form=form)
    user = result.user
    label = f" ({user.display_name})" if user.display_name else ""
    await update.message.reply_text(
        f"✅ Đã cấp quyền THÀNH VIÊN cho Zalo id: {html.escape(user.external_id)}{html.escape(label)}"
        + _address_note(user)
        + _welcome_note(result)
    )


@common.restricted
async def zaloadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zaloadmin <id_zalo> [tên] [xưng hô] — cấp/nâng quyền ADMIN (dùng được
    lệnh nhóm /nhom, /themnhom, /xoanhom, /tongket, /dangnoi và /fb_*). Hỗ trợ
    NHIỀU admin cùng lúc, không giới hạn 1 admin duy nhất."""
    arg = common.extract_arg(context)
    if not arg:
        await update.message.reply_text(
            f"Cú pháp: /zaloadmin <id_zalo> [tên hiển thị] [{_ADDRESS_CHOICES}]"
        )
        return
    external_id, display_name, form = _parse_pair_arg(arg)
    result = await zalo_onboarding.pair(external_id, display_name, admin=True, address_form=form)
    user = result.user
    label = f" ({user.display_name})" if user.display_name else ""
    await update.message.reply_text(
        f"👑 Đã cấp quyền ADMIN cho Zalo id: {html.escape(user.external_id)}{html.escape(label)} "
        f"(dùng được lệnh nhóm và /fb_*)." + _address_note(user) + _welcome_note(result)
    )


@common.restricted
async def zalohaquyen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/zalohaquyen <id_zalo> — hạ 1 admin về thành viên thường (vẫn giữ pairing)."""
    external_id = common.extract_arg(context).strip()
    if not external_id:
        await update.message.reply_text("Cú pháp: /zalohaquyen <id_zalo>")
        return
    found = await zalo_users.demote_to_user(external_id)
    if found:
        await update.message.reply_text(
            f"👤 Đã hạ {html.escape(external_id)} về thành viên thường."
        )
    else:
        await update.message.reply_text(f"Không tìm thấy {html.escape(external_id)} đã pair.")


@common.restricted
async def zalokhoa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    external_id = common.extract_arg(context).strip()
    if not external_id:
        await update.message.reply_text("Cú pháp: /zalokhoa <id_zalo>")
        return
    found = await zalo_users.set_status(external_id, zalo_users.STATUS_SUSPENDED)
    if found:
        await update.message.reply_text(f"🔒 Đã khóa {html.escape(external_id)}.")
    else:
        await update.message.reply_text(f"Không tìm thấy {html.escape(external_id)} đã pair.")


@common.restricted
async def zalomokhoa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    external_id = common.extract_arg(context).strip()
    if not external_id:
        await update.message.reply_text("Cú pháp: /zalomokhoa <id_zalo>")
        return
    found = await zalo_users.set_status(external_id, zalo_users.STATUS_ACTIVE)
    if found:
        await update.message.reply_text(f"🔓 Đã mở khóa {html.escape(external_id)}.")
    else:
        await update.message.reply_text(f"Không tìm thấy {html.escape(external_id)} đã pair.")


@common.restricted
async def zaloxoa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    external_id = common.extract_arg(context).strip()
    if not external_id:
        await update.message.reply_text("Cú pháp: /zaloxoa <id_zalo>")
        return
    found = await zalo_users.remove(external_id)
    if found:
        await update.message.reply_text(
            f"🗑️ Đã xoá pairing {html.escape(external_id)} cùng toàn bộ dữ liệu của người này "
            "(lịch sử chat, trí nhớ, ghi chú, nhắc việc, danh mục)."
        )
    else:
        await update.message.reply_text(f"Không tìm thấy {html.escape(external_id)} đã pair.")


@common.restricted
async def zalodanhsach_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = await zalo_users.list_users()
    if not users:
        await update.message.reply_text("Chưa có Zalo user nào được pair.")
        return
    lines = ["📋 <b>Danh sách Zalo user</b>", ""]
    for user in users:
        icon = _ZALO_ROLE_ICON.get(user.role, "👤")
        status = _ZALO_STATUS_LABEL.get(user.status, user.status)
        ten = html.escape(user.display_name) if user.display_name else "không tên"
        goi = f", gọi “{user.address_form}”" if user.address_form else ""
        lines.append(f"{icon} {html.escape(user.external_id)} ({ten}{goi}) — {status}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
