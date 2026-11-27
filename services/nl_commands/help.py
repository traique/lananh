"""Trợ giúp ngắn theo vai trò: /help, /help <chủ đề>, câu "em làm được gì"
(services/nl_commands)."""

from __future__ import annotations

from core.text_normalize import fold
from services import command_catalog as catalog
from services import tool_registry as reg

_EXAMPLES_COMMON = (
    "“giá vàng hôm nay”, “dò vé 123456 đài Tây Ninh”, “mai Đà Lạt mưa không”",
    "“nhắc {you} 8h tối mai gọi mẹ”, “cafe 45k”, “tháng này {you} tiêu bao nhiêu”",
    "“xem tử vi 15/3/1990 9h30 nam”, “ngày tốt tháng này”, “dịch giúp: お世話になります”",
    "“phân tích FPT”, “xem danh mục”, “hôm nay có gì cần {you} chú ý”",
)
_EXAMPLES_STAFF = "“provider đang chạy cái nào”, “tóm tắt nhóm Gia đình hôm nay”"


def short_help(role: str, channel: str, you: str = "anh") -> str:
    lines = [f"📖 {you.capitalize()} cứ nhắn tự nhiên, không cần nhớ lệnh. Ví dụ:"]
    lines += [f"• {line.format(you=you)}" for line in _EXAMPLES_COMMON]
    if role != reg.ROLE_MEMBER:
        lines.append(f"• {_EXAMPLES_STAFF}")
    lines.append("• “em làm được gì?”, “làm sao để đặt nhắc lặp?”")
    topics = " · ".join(f"{t.icon} {t.word or t.key}" for t in catalog.topics_for(role, channel))
    lines += [
        "",
        "Xem chi tiết 1 chủ đề: /help <chủ đề>, vd /help vàng, /help nhắc việc"
        + (", /help admin" if role != reg.ROLE_MEMBER else ""),
        f"Chủ đề: {topics}",
        "Bản đầy đủ mọi lệnh: /help all · Dừng câu đang trả lời: /huy",
    ]
    return "\n".join(lines)


def topic_help(topic: catalog.Topic, role: str, channel: str, you: str = "anh") -> str:
    lines = [f"{topic.icon} {topic.title}"]
    for entry in catalog.entries_for(role, channel, topic.key):
        ask = f" · nói: “{entry.ask}”" if entry.ask and not entry.suggest_only else ""
        lines.append(f"• {entry.usage} — {entry.summary}{ask}")
    lines.append("")
    lines.append(
        f"{you.capitalize()} cứ nhắn tự nhiên như các câu trong ngoặc; việc có 🔐 (xoá, quản trị) "
        "em chỉ chỉ ra lệnh, không tự làm. /help all: bản đầy đủ."
    )
    return "\n".join(lines)


def help_reply(argument: str, role: str, channel: str, you: str = "anh") -> str | None:
    """Câu /help <tham số>: "" -> ngắn; chủ đề -> chi tiết; "all" -> None (bản đầy đủ
    do kênh tự gửi); tham số lạ -> tìm theo từ khoá, không thấy thì bản ngắn."""
    arg = fold(argument or "").strip(" ?.!")
    if arg in {"all", "het", "tat ca", "day du", "full", "a"}:
        return None
    if not arg:
        return short_help(role, channel, you)
    topic = catalog.find_topic(arg, role, channel)
    if topic is not None:
        return topic_help(topic, role, channel, you)
    found = catalog.search(arg, role, channel)
    if found:
        return "\n".join(
            f"• {e.usage} — {e.summary}"
            + (f" · nói: “{e.ask}”" if e.ask and not e.suggest_only else "")
            for e in found
        )
    return short_help(role, channel, you)
