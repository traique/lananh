"""Nút bấm dưới tin Telegram ở dạng TRỪU TƯỢNG: các hàng [(nhãn, callback_data)].

channels/ và services/ dựng nút ở đây mà không import kiểu dữ liệu Telegram;
bot_app/handlers đổi sang InlineKeyboardMarkup (handlers/callbacks.markup).
callback_data luôn ngắn (Telegram giới hạn 64 byte), dạng "<verb>:<số>[:<mã>]"
với danh sách verb cố định - ``parse`` từ chối mọi thứ khác (chỉ chủ bot bấm
được, nhưng dữ liệu nút vẫn coi là đầu vào không tin cậy).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Button = tuple[str, str]
Rows = list[list[Button]]

RATE = "rate"  # 👍/👎 câu trả lời chat: rate:<prompt_id>:u|d
REGEN = "regen"  # 🔄 Thử lại / ✂️ Ngắn hơn: regen:<prompt_id>:r|s
FB_OK = "fbok"  # duyệt bài Facebook chờ: fbok:<post_id>
FB_SKIP = "fbno"  # bỏ bài Facebook chờ: fbno:<post_id>
ZALO_PAIR = "zpair"  # cấp quyền người Zalo lạ: zpair:<external_id>:m|a
SNOOZE = "snz"  # hoãn nhắc việc vừa tới: snz:<reminder_id>:10|60|am

_PATTERNS = {
    RATE: re.compile(r"rate:(\d{1,18}):([ud])"),
    REGEN: re.compile(r"regen:(\d{1,18}):([rs])"),
    FB_OK: re.compile(r"fbok:(\d{1,18})()"),
    FB_SKIP: re.compile(r"fbno:(\d{1,18})()"),
    ZALO_PAIR: re.compile(r"zpair:(\d{1,30}):([ma])"),
    SNOOZE: re.compile(r"snz:(\d{1,18}):(10|60|am)"),
}
# Chỉ chạy lại lượt chat (tốn AI, ghi lịch sử) mới phải xếp hàng sau khoá lượt; còn
# lại là ghi DB/gọi API ngắn, chạy ngoài khoá như /fb_*.
LOCKED_VERBS = frozenset({REGEN})
MAX_DATA_BYTES = 64

SNOOZE_CHOICES = {"10": "10p", "60": "1 tiếng", "am": "sáng mai"}


@dataclass(frozen=True)
class Action:
    verb: str
    target: str  # id (chuỗi số)
    option: str = ""


def parse(data: str | None) -> Action | None:
    if not data or len(data.encode()) > MAX_DATA_BYTES:
        return None
    verb = data.split(":", 1)[0]
    pattern = _PATTERNS.get(verb)
    match = pattern.fullmatch(data) if pattern else None
    if match is None:
        return None
    return Action(verb, match.group(1), match.group(2))


def needs_turn_lock(data: str | None) -> bool:
    action = parse(data)
    return action is not None and action.verb in LOCKED_VERBS


def answer_rows(prompt_id: int, *, regenerable: bool) -> Rows:
    rows: Rows = [[("👍", f"{RATE}:{prompt_id}:u"), ("👎", f"{RATE}:{prompt_id}:d")]]
    if regenerable:
        rows.append(
            [("🔄 Thử lại", f"{REGEN}:{prompt_id}:r"), ("✂️ Ngắn hơn", f"{REGEN}:{prompt_id}:s")]
        )
    return rows


def after_rating_rows(prompt_id: int, *, regenerable: bool) -> Rows:
    """Sau khi đánh giá: bỏ hàng 👍/👎, giữ nút làm lại nếu còn là câu mới nhất."""
    return answer_rows(prompt_id, regenerable=True)[1:] if regenerable else []


def facebook_review_rows(post_id: int) -> Rows:
    return [[("✅ Duyệt", f"{FB_OK}:{post_id}"), ("🗑 Bỏ qua", f"{FB_SKIP}:{post_id}")]]


def zalo_pair_rows(external_id: str) -> Rows | None:
    """None nếu id Zalo không phải chuỗi số (giữ nguyên hướng dẫn gõ lệnh)."""
    if not re.fullmatch(r"\d{1,30}", external_id or ""):
        return None
    return [
        [
            ("👤 Cấp thành viên", f"{ZALO_PAIR}:{external_id}:m"),
            ("👑 Cấp admin", f"{ZALO_PAIR}:{external_id}:a"),
        ]
    ]


def reminder_snooze_rows(reminder_id: int) -> Rows:
    return [
        [
            ("+10p", f"{SNOOZE}:{reminder_id}:10"),
            ("+1h", f"{SNOOZE}:{reminder_id}:60"),
            ("Sáng mai", f"{SNOOZE}:{reminder_id}:am"),
        ]
    ]
