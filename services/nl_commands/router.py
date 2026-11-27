"""Đường router (1 lượt LLM): chọn nhóm lệnh đưa vào prompt router chat, và gom kết quả
tool "chạy lệnh" thành tin gửi nguyên văn cho lượt chat (services/nl_commands)."""

from __future__ import annotations

import re
from dataclasses import replace

from core.text_normalize import fold
from services import command_catalog as catalog
from services import tool_registry as reg
from services.nl_commands.registry import COMMANDS
from services.nl_commands.suggest import CLARIFY_SPEC, SUGGEST_SPEC
from services.nl_commands.turn import _ctx, llm_enabled, pending

# Tối đa số nhóm lệnh đưa vào 1 prompt router (giữ trần token Groq free).
MAX_GROUPS = 2
_SUGGEST_MAX = 6

# Nhóm lệnh -> từ khoá (bỏ dấu, theo ranh giới từ) để chọn nhóm vào prompt router.
# Viết thận trọng: tin chat phiếm ("vâng hôm nay em bận", "em nói về số liệu") không
# được khớp - test_nl_commands có bộ câu chat thường phải ra rỗng.
_GROUP_RE: dict[str, re.Pattern[str]] = {
    "huyen_hoc": re.compile(
        r"\b(?:tu vi|la so|than so hoc|so chu dao|ky mon|kymon|bat tu|tu tru)\b"
    ),
    # Nhóm riêng để câu tử vi không kéo thêm 2 tool phong thủy vào prompt router.
    "phong_thuy": re.compile(r"\b(?:lo ban|cung phi|bat trach|huong nha)\b"),
    "lich": re.compile(
        r"\b(?:ngay tot|ngay xau|ngay dep|(?:ngay|gio) hoang dao|xem ngay|sao han|han nam|"
        r"tam tai|kim lau|hoang oc|ngay le|le hoi|xuat hanh|dong tho|khai truong|nhap trach|"
        r"xem tuoi|hop tuoi|tuoi (?:gi|con gi)|lich thang)\b"
    ),
    "tien_ich": re.compile(
        r"\b(?:tim gia|so sanh gia|so gia|gia re nhat|dich (?:giup|ho|gium|sang|cau|doan|"
        r"tieng|cho)|tieng nhat|viet prompt|prompt|tao (?:anh|hinh)|ve (?:anh|hinh|giup|cho))\b"
    ),
    "ca_nhan": re.compile(
        r"\b(?:tri nho|nho (?:nhung )?gi ve|lich su (?:chat|tro chuyen|hoi thoai)|viec nen|"
        r"tac vu dinh ky|ban tin sang|xem (?:thu )?ban tin)\b"
    ),
    "so_sach": re.compile(
        r"\b(?:an uong|calo|kcal|danh sach theo doi|watchlist|dang theo doi|so giao dich|"
        r"lich su giao dich|lai lo da chot|chot lai|khoan chi gan|cac khoan chi)\b"
    ),
    "he_thong": re.compile(
        r"\b(?:provider|trang thai bot|tinh trang bot|thong ke|luot goi|loi hom nay|"
        r"bot (?:co )?loi|diem tin|tin tuc moi|ban tin moi|luot like|danh gia cau tra loi)\b"
    ),
    "nhom": re.compile(
        r"\bnhom\b.*\b(?:tong ket|tom tat|noi gi|ban gi|thao luan|danh sach)\b|"
        r"\b(?:tong ket|tom tat|danh sach) (?:cac )?nhom\b|\bnhom zalo\b"
    ),
    "cai_dat": re.compile(
        r"\b(?:bat|tat|mo|ngung|on|off)\b.*\b(?:giong noi|tin thoai|doc to|"
        r"yen lang|ban tin|hoi than|hoi tham|tri nho|tavily|tra web)\b|"
        r"\b(?:giong noi|yen lang|ban tin|hoi than|hoi tham|tri nho|tavily)\b.*"
        r"\b(?:bat|tat|mo|ngung|on|off)\b"
    ),
}
# Nhóm chỉ chủ bot/admin mới có tool (thành viên không cần tốn lượt router).
_STAFF_GROUPS = frozenset({"he_thong", "nhom"})


def _suggest_entries(text: str, role: str, channel: str) -> list[catalog.Entry]:
    # Lệnh chỉ có trên Telegram đã giới hạn chủ bot qua roles: chủ bot nhắn từ Zoom vẫn
    # được chỉ lệnh (kèm "trên Telegram").
    found = [
        e
        for e in catalog.search(text, role, "telegram", limit=12)
        if e.suggest_only and not e.has_nl
    ]
    return found[:_SUGGEST_MAX]


def router_groups(user_id: int, text: str, role: str, channel: str = "") -> list[str]:
    """Nhóm lệnh khớp câu (tối đa MAX_GROUPS, nhóm đang chờ bổ sung đứng đầu); rỗng =
    không thêm lệnh nào vào prompt router."""
    if not llm_enabled() or not text or text.lstrip().startswith("/"):
        return []
    low = fold(text)
    groups: list[str] = []
    waiting = pending(user_id)
    if waiting is not None and waiting.spec in COMMANDS:
        groups.append(COMMANDS[waiting.spec].spec.group)
    for group, pattern in _GROUP_RE.items():
        if group in _STAFF_GROUPS and role == reg.ROLE_MEMBER:
            continue
        if group not in groups and pattern.search(low):
            groups.append(group)
    if len(groups) < MAX_GROUPS and _suggest_entries(text, role, channel or _ctx().channel):
        groups.append("suggest")
    return groups[:MAX_GROUPS]


def router_specs(user_id: int, text: str, role: str, channel: str = "") -> list[reg.ToolSpec]:
    """ToolSpec "chạy lệnh" đưa thêm vào prompt router cho câu này (theo nhóm + vai trò)."""
    groups = router_groups(user_id, text, role, channel)
    if not groups:
        return []
    chosen = [
        spec
        for spec in reg.specs(reg.COMMAND, role)
        if spec.group in groups and spec.group not in ("suggest", "clarify")
    ]
    if "suggest" in groups:
        entries = _suggest_entries(text, role, channel or _ctx().channel)
        listed = ", ".join(e.usage.split(" · ")[0] for e in entries)
        chosen.append(
            replace(
                SUGGEST_SPEC,
                router_hint=(
                    f'args {{"command": 1 trong: {listed}; "args": tham số người dùng nói}}. '
                    "Khi muốn làm đúng việc đó."
                ),
            )
        )
    if chosen:
        chosen.append(CLARIFY_SPEC)
    return chosen


ROUTER_RULE = (
    '- Tool "cmd_*"/"set_*": chạy 1 chức năng của bot khi người dùng muốn dùng NGAY; hỏi '
    'kiến thức chung ("tử vi là gì", "kỳ môn có đúng không") -> "none". "set_*" chỉ khi '
    "nói rõ bật/tắt."
)


# ─── Kết quả cho lượt chat ──────────────────────────────────────────────────

# Lệnh tự lưu lịch sử của mình (conversation_log.run_and_record): lượt chat không lưu lại.
_SELF_RECORDING = frozenset({"/dich"})


def records_itself(line: str) -> bool:
    return bool(line) and line.split(maxsplit=1)[0].lower() in _SELF_RECORDING


def outcome_messages(outcome: reg.ToolOutcome) -> list[str]:
    """Các tin gửi nguyên văn của 1 outcome tool "chạy lệnh" (rỗng = không phải)."""
    if not outcome.verbatim:
        return []
    if outcome.status in (reg.STATUS_OK, reg.STATUS_INVALID):
        messages = [m for m in (outcome.extra.get("messages") or []) if m]
        return messages or ([outcome.text] if outcome.text else [])
    if outcome.status == reg.STATUS_BLOCKED:
        entry = catalog.get(outcome.name.removeprefix("set_"))
        line = f"/{entry.name}" if entry else "lệnh"
        return [f"⚠️ Lượt này em không tự đổi cài đặt ({outcome.text}) - gõ {line} giúp em nha."]
    if outcome.status == reg.STATUS_FAILED:
        return [
            "❌ Em chưa chạy được chức năng này lúc này, thử lại sau ít phút hoặc gõ /help nha."
        ]
    return []
