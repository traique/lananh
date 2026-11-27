"""Lệnh tiện ích, cá nhân (xem) và sổ sách chạy từ câu tự nhiên: tìm giá, dịch, prompt,
tạo ảnh, trí nhớ, lịch sử chat, việc nền, tác vụ định kỳ, bản tin, ăn uống, danh sách theo
dõi, sổ giao dịch, lãi/lỗ đã chốt, khoản chi (services/nl_commands)."""

from __future__ import annotations

import re
from typing import Any

from core.text_normalize import fold, nfc
from services import tool_registry as reg
from services.nl_commands.registry import Ask, _clean, _cmd, _fixed, _need

# ── Tiện ích ──

_cmd(
    "cmd_gia",
    "gia",
    "tìm giá",
    "tien_ich",
    "Tìm và so sánh giá 1 sản phẩm trên các shop.",
    '{"product": tên sản phẩm}. Vd "tìm giá iPhone 16 Pro".',
    {"product": reg.string("sản phẩm")},
    _need("product", "{You} muốn {me} tìm giá sản phẩm nào ạ?", "/gia", 120),
)


def _build_dich(args: dict[str, Any]) -> str | Ask:
    text = nfc(str(args.get("text") or "")).strip()[:1500]
    if not text:
        return Ask("{You} gửi {me} câu cần dịch nha (Nhật ↔ Việt).")
    direction = fold(_clean(args.get("direction"), 10))
    prefix = {"ja>vi": "ja>vi ", "vi>ja": "vi>ja "}.get(direction, "")
    return f"/dich {prefix}{text}"


_cmd(
    "cmd_dich",
    "dich",
    "dịch",
    "tien_ich",
    "Dịch Nhật-Việt.",
    '{"text": nguyên văn câu cần dịch, "direction": ja>vi|vi>ja|""}.',
    {"text": reg.string("câu cần dịch"), "direction": reg.string("ja>vi|vi>ja")},
    _build_dich,
)
_cmd(
    "cmd_prompt",
    "prompt",
    "viết prompt",
    "tien_ich",
    "Viết prompt tạo ảnh từ mô tả.",
    '{"description": mô tả}.',
    {"description": reg.string("mô tả")},
    _need("description", "{You} mô tả ảnh muốn tạo prompt giúp {me} nha.", "/prompt"),
)
_cmd(
    "cmd_anh",
    "anh",
    "tạo ảnh",
    "tien_ich",
    "Tạo ảnh thật từ mô tả.",
    '{"description": mô tả ảnh}. Chỉ khi bảo TẠO/VẼ ảnh.',
    {"description": reg.string("mô tả")},
    _need("description", "{You} muốn {me} tạo ảnh gì ạ?", "/anh"),
    heavy=lambda args: True,
)

# ── Cá nhân (xem) ──

_cmd(
    "cmd_memory",
    "memory",
    "trí nhớ",
    "ca_nhan",
    "Xem trí nhớ dài hạn em lưu về người dùng.",
    "args {}.",
    None,
    _fixed("/memory"),
    patterns=(
        r"(?:xem )?tri nho(?: cua em| ve (?:anh|chi|minh|toi))?",
        r"(?:em )?(?:dang )?nho (?:nhung )?gi ve (?:anh|chi|minh|toi)",
    ),
)
_cmd(
    "cmd_history",
    "history",
    "lịch sử chat",
    "ca_nhan",
    "Liệt kê 10 lượt chat gần nhất.",
    "args {}. Tìm lại 1 chuyện cụ thể thì dùng search_history.",
    None,
    _fixed("/history"),
    patterns=(r"(?:xem )?lich su (?:chat|tro chuyen|hoi thoai)(?: gan day| gan nhat)?",),
)


def _build_viec(args: dict[str, Any]) -> str:
    job = re.sub(r"\D", "", str(args.get("job_id") or ""))[:6]
    return f"/viec xem {job}" if job else "/viec"


_cmd(
    "cmd_viec",
    "viec",
    "việc nền",
    "ca_nhan",
    "Xem việc nền /viec (hoặc kết quả 1 việc).",
    '{"job_id": số việc nếu hỏi 1 việc}. Không giao việc mới.',
    {"job_id": reg.integer("số việc")},
    _build_viec,
    patterns=(
        r"(?:xem )?(?:cac )?viec nen(?: cua (?:anh|chi|minh|toi))?(?: (?:sao|toi dau) roi)?",
    ),
)
_cmd(
    "cmd_lichtacvu",
    "lichtacvu",
    "tác vụ định kỳ",
    "ca_nhan",
    "Xem các tác vụ định kỳ.",
    "args {}. Không đặt tác vụ mới.",
    None,
    _fixed("/lichtacvu"),
    roles=reg.STAFF_ROLES,
    patterns=(r"(?:xem )?(?:cac )?tac vu dinh ky(?: cua (?:anh|chi|minh|toi))?",),
)
_cmd(
    "cmd_bantin",
    "bantin",
    "bản tin sáng",
    "ca_nhan",
    "Xem thử bản tin sáng cá nhân.",
    "args {}.",
    None,
    _fixed("/bantin xem"),
    patterns=(r"xem (?:thu )?ban tin(?: sang)?(?: cua (?:anh|chi|minh|toi))?",),
)


def _build_an(args: dict[str, Any]) -> str:
    return (
        "/an tuan" if fold(_clean(args.get("period"), 20)) in {"tuan", "week", "7 ngay"} else "/an"
    )


_cmd(
    "cmd_an",
    "an",
    "ăn uống",
    "so_sach",
    "Xem nhật ký ăn uống, kcal hôm nay/7 ngày.",
    '{"period": "hôm nay"|"tuan"}. Không ghi món mới.',
    {"period": reg.string("hôm nay|tuan")},
    _build_an,
)
_cmd(
    "cmd_theodoi",
    "theodoi",
    "danh sách theo dõi",
    "so_sach",
    "Xem danh sách mã theo dõi và ngưỡng giá.",
    "args {}. Không thêm/bỏ mã.",
    None,
    _fixed("/theodoi"),
    patterns=(r"(?:xem )?danh sach (?:ma )?theo doi(?: cua (?:anh|chi|minh|toi))?",),
)


def _build_lichsucp(args: dict[str, Any]) -> str:
    symbol = re.sub(r"[^A-Za-z0-9]", "", str(args.get("symbol") or ""))[:6].upper()
    return f"/lichsucp {symbol}".strip()


_cmd(
    "cmd_lichsucp",
    "lichsucp",
    "sổ giao dịch",
    "so_sach",
    "Sổ giao dịch cổ phiếu đã ghi.",
    '{"symbol": mã nếu hỏi 1 mã}.',
    {"symbol": reg.string("mã cổ phiếu")},
    _build_lichsucp,
)


def _build_laicp(args: dict[str, Any]) -> str:
    month = re.sub(r"[^0-9/]", "", str(args.get("month") or ""))[:7]
    return f"/laicp {month}".strip()


_cmd(
    "cmd_laicp",
    "laicp",
    "lãi lỗ đã chốt",
    "so_sach",
    "Lãi/lỗ đã chốt theo tháng.",
    '{"month": số tháng hoặc MM/YYYY, "" = tháng này}.',
    {"month": reg.string("tháng")},
    _build_laicp,
)
_cmd(
    "cmd_chitieu_ds",
    "chitieu",
    "khoản chi gần đây",
    "so_sach",
    "Liệt kê các khoản chi gần nhất (có số # để xoá).",
    "args {}. Tổng theo kỳ thì dùng expense_summary.",
    None,
    _fixed("/chitieu ds"),
    patterns=(r"(?:xem |liet ke )?(?:cac )?khoan chi gan (?:day|nhat)",),
)
