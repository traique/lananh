"""Lệnh Telegram chạy trên tài khoản Zalo đang đăng nhập: quản lý/xem lại
nhóm Zalo (/nhom, /themnhom, /xoanhom, /tongket, /dangnoi) và luồng
Zalo → Facebook Page (/fb_*). Cùng dispatcher với Zalo/Zoom
(channels.group_commands, channels.facebook_commands); handler ở đây chỉ suy ra
account_id rồi chuyển tiếp."""

from telegram import Update
from telegram.ext import ContextTypes

from channels import facebook_commands, group_commands, zalo_repository, zalo_session
from handlers import common


async def _zalo_account_id() -> str | None:
    """Ưu tiên session Zalo đang đăng nhập; fallback resolver cũ để tương thích
    session cũ (cùng thứ tự với web.py cho Zoom)."""
    return (
        await zalo_session.load_account_id() or await zalo_repository.resolve_default_account_id()
    )


async def _group_command(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    """Dùng chung cho /nhom, /themnhom, /xoanhom, /tongket, /dangnoi trên
    Telegram - cùng 1 hàm channels.group_commands.maybe_handle_group_command()
    mà Zalo/Zoom đang dùng, chỉ khác cách lấy account_id (Telegram không có
    sẵn account_id Zalo trong update, phải tự suy ra qua
    zalo_repository.resolve_default_account_id())."""
    account_id = await _zalo_account_id()
    if account_id is None:
        await update.message.reply_text(
            "Chưa theo dõi nhóm Zalo nào. Dùng /nhom để xem hướng dẫn lấy group ID, "
            "sau đó /themnhom <group_id> <tên-gợi-nhớ>."
        )
        return
    argument = common.extract_arg(context)
    text = f"{command} {argument}".strip()
    result = await group_commands.maybe_handle_group_command(account_id, text)
    if result is None:
        await update.message.reply_text("Lệnh chưa được hỗ trợ.")
        return
    for message_text in result.messages:
        await common.reply_long_text(update.message, message_text)


async def _facebook_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, command: str
) -> None:
    """Dùng cùng facebook_commands với Zalo/Zoom; account_id lấy từ session Zalo
    để luồng Facebook không phụ thuộc việc đã cấu hình nhóm /tongket hay chưa."""
    account_id = await _zalo_account_id()
    if account_id is None or not str(account_id).strip():
        await update.message.reply_text(
            "Chưa xác định được tài khoản Zalo đang đăng nhập. Hãy đăng nhập Zalo bot trước rồi thử lại."
        )
        return
    argument = common.extract_arg(context)
    text = f"{command} {argument}".strip()
    result = await facebook_commands.maybe_handle_facebook_command(str(account_id), text)
    if result is None:
        await update.message.reply_text("Lệnh Facebook chưa được hỗ trợ.")
        return
    for message_text in result.messages:
        await common.reply_long_text(update.message, message_text)


# Bảng lệnh -> handler dùng chung; tạo module attribute ``<tên>_cmd`` để
# bot_app đăng ký như trước (thay cho 20 wrapper giống hệt nhau).
_GROUP_COMMANDS = ("nhom", "themnhom", "xoanhom", "tongket", "dangnoi")
_FACEBOOK_COMMANDS = (
    "fb_nhom",
    "fb_pages",
    "fb_market",
    "fb_themnhom",
    "fb_xoanhom",
    "fb_xem",
    "fb_sua",
    "fb_link",
    "fb_ok",
    "fb_boqua",
    "fb_check",
    "fb_reconcile",
    "fb_reset",
    "fb_boloc",
    "fb_loclai",
)


def _make_handler(command: str, runner):
    @common.restricted
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await runner(update, context, f"/{command}")

    handler.__name__ = f"{command}_cmd"
    handler.__qualname__ = handler.__name__
    handler.__doc__ = f"Telegram {command} -> chuyển tiếp dispatcher dùng chung với Zalo/Zoom."
    return handler


COMMAND_HANDLERS = {
    **{name: _make_handler(name, _group_command) for name in _GROUP_COMMANDS},
    **{name: _make_handler(name, _facebook_command) for name in _FACEBOOK_COMMANDS},
}
globals().update({f"{name}_cmd": fn for name, fn in COMMAND_HANDLERS.items()})
