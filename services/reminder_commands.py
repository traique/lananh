"""Lệnh /nhac và /ngaynho dùng chung mọi kênh, đọc bằng code (không gọi AI):

- /nhac — danh sách nhắc việc đang chờ (đánh số), kèm nhắc lặp.
- /nhac <thời gian> <nội dung> — đặt nhắc, vd /nhac 8h tối mai gọi mẹ,
  /nhac 7h sáng mỗi ngày uống thuốc, /nhac mùng 1 và rằm 6h thắp hương.
- /nhac huy <số|từ khoá> — huỷ (nhắc lặp: huỷ cả chuỗi).
- /nhac hoan <số> <30p|1 tiếng|sáng mai> — dời 1 lần nhắc đang chờ.
- /ngaynho — danh sách ngày nhớ; /ngaynho <tên> <ngày> [âm] [trước N ngày] [lúc 7h]
  — thêm (giỗ, sinh nhật, kỷ niệm); /ngaynho xoa <số|tên> — xoá.

Handler Telegram (handlers/reminder_commands.py) và channel_command_service
(Zalo/Zoom) chỉ chuyển lệnh vào đây.
"""

from __future__ import annotations

import datetime as dt
import re

from core.text_normalize import fold
from services import ngay_nho, reminder_service, reply_audience, vn_time_parse

NHAC_USAGE = (
    "⏰ /nhac — xem nhắc việc đang chờ\n"
    "/nhac 8h tối mai gọi mẹ · /nhac 30 phút nữa tắt bếp · /nhac thứ 6 tuần sau 9h nộp báo cáo\n"
    "/nhac 7h sáng mỗi ngày uống thuốc · /nhac mùng 1 và rằm 6h thắp hương\n"
    "/nhac huy <số> — huỷ · /nhac hoan <số> 30p — dời\n"
    'Vừa nhận tin nhắc thì trả lời "hoãn 10p", "hoãn sáng mai" hoặc "xong".'
)
NGAYNHO_USAGE = (
    "🗓 /ngaynho — xem ngày nhớ\n"
    "/ngaynho giỗ ông nội 12/3 âm trước 3 ngày · /ngaynho sinh nhật vợ 20/11/1990\n"
    "/ngaynho kỷ niệm ngày cưới 5/6/2015 lúc 7h · /ngaynho xoa <số>\n"
    "Âm lịch: tháng thiếu không có ngày 30 thì nhắc ngày 29; năm có tháng nhuận vẫn theo "
    'tháng thường (thêm "theo tháng nhuận" nếu nhà mình làm theo tháng nhuận).'
)
_CANCEL_WORDS = {"huy", "xoa", "bo", "cancel", "del"}
_SNOOZE_WORDS = {"hoan", "doi", "snooze"}
_LIST_WORDS = {"", "xem", "list", "ds", "danh sach"}
_HELP_WORDS = {"help", "huong dan", "?"}


def _split(argument: str) -> tuple[str, str]:
    head, _, rest = argument.strip().partition(" ")
    return fold(head), rest.strip()


async def nhac(user_id: int, argument: str) -> str:
    argument = " ".join(argument.split())
    head, rest = _split(argument)
    if fold(argument) in _LIST_WORDS:
        return reminder_service.format_items(await reminder_service.pending_items(user_id))
    if fold(argument) in _HELP_WORDS:
        return NHAC_USAGE
    if head in _CANCEL_WORDS:
        if not rest:
            return "Huỷ nhắc nào? Ví dụ /nhac huy 2 (số trong danh sách /nhac)."
        return (await reminder_service.cancel(user_id, rest))[0]
    if head in _SNOOZE_WORDS:
        target, _, when = rest.partition(" ")
        if not target:
            return "Ví dụ: /nhac hoan 2 30p, /nhac hoan 2 sáng mai."
        return (await reminder_service.snooze_pending(user_id, target, when))[0]
    now = dt.datetime.now(vn_time_parse.VN_TZ)
    extracted = vn_time_parse.extract_reminder(argument, now)
    if extracted is None:
        return "Em chưa thấy thời điểm nhắc trong lệnh.\n\n" + NHAC_USAGE
    message, when = extracted
    if when.error or when.due_at is None:
        return f"Chưa đặt nhắc: {when.error or 'chưa rõ thời điểm'}\n\n{NHAC_USAGE}"
    try:
        created = await reminder_service.create(
            user_id, message or "Tới giờ hẹn rồi", when.due_at, when.recurrence
        )
    except reminder_service.ReminderError as exc:
        return f"❌ {exc}"
    note = " (giờ mặc định của buổi - đổi bằng /nhac hoan)" if when.approx_time else ""
    return f"✅ {created.confirmation(now)}{note}"


async def ngaynho(user_id: int, argument: str) -> str:
    argument = " ".join(argument.split())
    head, rest = _split(argument)
    you = reply_audience.current().address_form or "anh"
    if fold(argument) in _LIST_WORDS:
        today = dt.datetime.now(vn_time_parse.VN_TZ).date()
        return ngay_nho.format_items(await ngay_nho.items(user_id, today), today, you=you)
    if fold(argument) in _HELP_WORDS:
        return NGAYNHO_USAGE
    if head in _CANCEL_WORDS and rest and (rest.isdigit() or not re.search(r"\d+[/-]\d+", rest)):
        return await ngay_nho.remove(user_id, rest)
    try:
        spec = ngay_nho.parse(argument)
        text, _ = await ngay_nho.add(user_id, spec)
    except ngay_nho.NgayNhoError as exc:
        return f"❌ {exc}\n\n{NGAYNHO_USAGE}"
    return f"✅ {text}"


async def handle(command: str, argument: str, user_id: int, channel: str) -> list[str] | None:
    """Kết quả /nhac, /ngaynho; None nếu không phải lệnh của module này."""
    if command in {"/nhac", "/nhacviec"}:
        return [await nhac(user_id, argument)]
    if command in {"/ngaynho", "/ngay_nho"}:
        return [await ngaynho(user_id, argument)]
    return None
