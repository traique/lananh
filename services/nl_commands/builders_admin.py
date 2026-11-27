"""Lệnh quản trị bot và nhóm Zalo (chủ bot/admin) chạy từ câu tự nhiên: trạng thái
provider, thống kê, lỗi, phiên bản, đánh giá, điểm tin, nhóm, tổng kết (services/
nl_commands)."""

from __future__ import annotations

import re
from typing import Any

from core.text_normalize import fold
from services import tool_registry as reg
from services.nl_commands.registry import Ask, _clean, _cmd, _fixed, _is_switch, _need

# ── Hệ thống (chủ bot/admin) ──
_cmd(
    "cmd_status",
    "status",
    "trạng thái provider",
    "he_thong",
    "Trạng thái provider AI của bot.",
    "args {}.",
    None,
    _fixed("/status"),
    roles=reg.STAFF_ROLES,
    patterns=(r"(?:xem )?(?:trang thai|tinh trang) (?:provider|bot)",),
)


def _build_thongke(args: dict[str, Any]) -> str:
    days = re.sub(r"\D", "", str(args.get("days") or ""))[:3]
    return f"/thongke {days}d" if days else "/thongke"


_cmd(
    "cmd_thongke",
    "thongke",
    "thống kê",
    "he_thong",
    "Thống kê lượt gọi AI theo user/model.",
    '{"days": số ngày, "" = 7}.',
    {"days": reg.integer("số ngày")},
    _build_thongke,
    roles=reg.STAFF_ROLES,
    patterns=(r"(?:xem )?thong ke (?:luot goi|su dung)",),
)
_cmd(
    "cmd_loi",
    "loi",
    "lỗi của bot",
    "he_thong",
    "Lỗi của bot gom theo loại hôm nay.",
    "args {}.",
    None,
    _fixed("/loi"),
    roles=frozenset({reg.ROLE_OWNER}),
    patterns=(r"(?:xem )?loi (?:hom nay|cua bot)", r"hom nay bot co loi gi(?: khong)?"),
)
_cmd(
    "cmd_phienban",
    "phienban",
    "phiên bản",
    "he_thong",
    "Phiên bản bot đang chạy và lịch sử cập nhật.",
    "args {}.",
    None,
    _fixed("/phienban"),
    roles=reg.STAFF_ROLES,
    # _core() đã bỏ "em"/"anh" đầu câu; "bản mới có gì" trùng "bạn mới có gì" nên phải
    # có chữ "phiên bản"/"cập nhật"/"bot".
    patterns=(
        r"(?:xem )?phien ban(?: (?:bot|moi nhat|hien tai|dang chay))?",
        r"(?:xem )?lich su cap nhat(?: bot)?",
        r"phien ban moi co gi(?: moi)?",
        r"bot (?:co gi moi|moi cap nhat (?:gi|nhung gi))(?: khong)?",
        r"moi cap nhat (?:gi|nhung gi)(?: the| vay)?",
    ),
)
_cmd(
    "cmd_danhgia",
    "danhgia",
    "đánh giá câu trả lời",
    "he_thong",
    "Số 👍/👎 câu trả lời theo provider.",
    "args {}.",
    None,
    _fixed("/danhgia"),
    roles=frozenset({reg.ROLE_OWNER}),
)
_cmd(
    "cmd_tintuc",
    "tintuc",
    "điểm tin",
    "he_thong",
    "Điểm tin RSS tổng hợp mới nhất (không theo chủ đề).",
    'args {}. Hỏi tin về 1 chủ đề cụ thể -> "none".',
    None,
    _fixed("/tintuc"),
    roles=reg.STAFF_ROLES,
    patterns=(r"(?:xem |doc )?diem tin(?: moi(?: nhat)?| hom nay| sang nay)?",),
)

# ── Nhóm Zalo (chủ bot/admin) ──
_cmd(
    "cmd_nhom",
    "nhom",
    "danh sách nhóm",
    "nhom",
    "Danh sách nhóm Zalo đang theo dõi.",
    "args {}.",
    None,
    _fixed("/nhom"),
    roles=reg.STAFF_ROLES,
)
_PERIODS = {"24h": "24h", "7d": "7d", "homnay": "homnay", "hom nay": "homnay", "homqua": "homqua",
            "hom qua": "homqua", "tuan": "7d", "7 ngay": "7d"}  # fmt: skip


def _build_tongket(args: dict[str, Any]) -> str | Ask:
    group = _clean(args.get("group"), 60)
    if _is_switch(group.split(" ", 1)[0] if group else ""):
        return Ask("Bật/tắt tổng kết tự động {you} gõ /tongket on|off [tên nhóm] giúp {me} nha.")
    if not group:
        return Ask("{You} muốn {me} tổng kết nhóm nào ạ? (gõ /nhom để xem tên các nhóm)")
    period = _PERIODS.get(fold(_clean(args.get("period"), 10)), "")
    return f"/tongket {group} {period}".strip()


_cmd(
    "cmd_tongket",
    "tongket",
    "tổng kết nhóm",
    "nhom",
    "Tổng kết (AI) thảo luận 1 nhóm Zalo.",
    '{"group": tên nhóm, "period": 24h|7d|homnay|homqua}.',
    {"group": reg.string("tên nhóm"), "period": reg.string("24h|7d|homnay|homqua")},
    _build_tongket,
    roles=reg.STAFF_ROLES,
)
_cmd(
    "cmd_dangnoi",
    "dangnoi",
    "nhóm đang nói gì",
    "nhom",
    "Nguyên văn thảo luận hôm nay của 1 nhóm Zalo.",
    '{"group": tên nhóm}.',
    {"group": reg.string("tên nhóm")},
    _need("group", "{You} muốn xem nhóm nào ạ? (gõ /nhom để xem tên các nhóm)", "/dangnoi", 60),
    roles=reg.STAFF_ROLES,
)
