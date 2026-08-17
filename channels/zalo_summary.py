"""Group summary generation shared by controller commands and the daily job."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ai import orchestrator
from channels import zalo_repository
from channels.zalo_text import to_plain_text

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Dưới các ngưỡng này, tóm tắt bằng AI chỉ tạo ra suy diễn thừa.
VERBATIM_MESSAGE_LIMIT = 2
SHORT_SUMMARY_MESSAGE_LIMIT = 5
_CHUNK_CHAR_LIMIT = 18_000
_EMPTY_BODY = "Không tạo được nội dung tổng kết."

_SECTION_TITLES = (
    "MÃ ĐƯỢC BÀN",
    "NHẬN ĐỊNH THỊ TRƯỜNG",
    "HÀNH ĐỘNG ĐƯỢC KHUYẾN NGHỊ",
    "CẢNH BÁO VÀ TIN CẦN THEO DÕI",
    "NGUYÊN VĂN",
)

_GROUNDING = (
    "Chỉ dùng dữ liệu được cung cấp. Không bịa giá, không bịa khuyến nghị, "
    "không bịa người nêu, không suy diễn ý định. Phân biệt rõ nhận định cá nhân "
    "với thông tin đã được xác nhận."
)

_NOISE = (
    "Bỏ hẳn nội dung quảng cáo dịch vụ môi giới, ưu đãi lãi margin, mời mở tài "
    "khoản, mời chuyển sàn, và lời nhắc mang tính cá nhân giữa các thành viên."
)

_STYLE = (
    "Định dạng cho Zalo: PLAIN TEXT. Cấm dùng **, *, _, #, ### và bảng markdown. "
    'Tiêu đề mục viết IN HOA trên dòng riêng, gạch đầu dòng dùng "•". '
    "Không thêm tiêu đề nhóm, không thêm lời dẫn mở đầu, bắt đầu ngay ở mục đầu tiên. "
    'Bỏ hẳn mục nào không có dữ liệu, không ghi "không có thông tin".'
)

_SECTIONS = (
    "CẤU TRÚC BÁO CÁO:\n"
    "MÃ ĐƯỢC BÀN: mỗi mã một dòng gồm mã, quan điểm mua/bán/giữ, vùng giá nếu nguồn "
    "nêu, người nêu. Bỏ qua mã chỉ được nhắc tên mà không kèm quan điểm, vùng giá "
    "hay hành động nào.\n"
    "NHẬN ĐỊNH THỊ TRƯỜNG: chỉ số, thanh khoản, vùng cản hoặc hỗ trợ được nhắc.\n"
    "HÀNH ĐỘNG ĐƯỢC KHUYẾN NGHỊ: chỉ ghi khuyến nghị nêu cho cả nhóm, kèm người nêu. "
    "Hành động cá nhân của thành viên như tự mua, tự chốt, tự all-in hay dùng margin "
    "không phải khuyến nghị, để ở dòng mã tương ứng.\n"
    "CẢNH BÁO VÀ TIN CẦN THEO DÕI: chỉ cảnh báo về mã, thị trường hoặc sự kiện."
)


def _chunk_prompt(index: int, total: int) -> str:
    return (
        "Bạn đang đọc tin nhắn của một room chứng khoán Việt Nam. "
        f"{_GROUNDING}\n"
        "Ghi lại các mã được nhắc kèm quan điểm và vùng giá, nhận định thị trường, "
        "khuyến nghị hành động và cảnh báo. Ghi rõ đâu là khuyến nghị cho cả nhóm, "
        f"đâu là hành động cá nhân. {_NOISE}\n"
        f"Đây là phần {index}/{total}."
    )


def _merge_prompt(alias: str) -> str:
    return (
        f"Hợp nhất các bản tóm tắt của room chứng khoán {alias} thành một báo cáo "
        f"tiếng Việt ngắn gọn, không lặp nội dung.\n{_GROUNDING}\n{_NOISE}\n"
        f"{_STYLE}\n{_SECTIONS}"
    )


def _short_prompt(alias: str) -> str:
    return (
        f"Room chứng khoán {alias} chỉ có vài tin nhắn trong khoảng này. Liệt kê gọn "
        "từng nội dung thực sự có, mỗi ý một dòng, không thêm mục trống, không suy "
        "diễn và không viết phần rủi ro nếu nguồn không nhắc.\n"
        f"{_GROUNDING}\n{_NOISE}\n{_STYLE}"
    )


def _space_sections(text: str) -> str:
    """Chèn dòng trắng trước mỗi tiêu đề mục cho dễ đọc trên Zalo."""
    out: list[str] = []
    for line in text.split("\n"):
        title = line.strip().rstrip(":").upper()
        if title.startswith(_SECTION_TITLES) and out and out[-1].strip():
            out.append("")
        out.append(line)
    return "\n".join(out)


def _render(body: str) -> str:
    return _space_sections(to_plain_text(body))


def resolve_window(spec: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now or datetime.now(VN_TZ)
    value = spec.strip().lower()
    if value == "7d":
        return current - timedelta(days=7), current
    if value == "homnay":
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, current
    if value == "homqua":
        end = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return end - timedelta(days=1), end
    return current - timedelta(hours=24), current


async def _ask(prompt: str) -> str:
    response = await orchestrator.ask(prompt)
    return (getattr(response, "text", None) or "").strip()


def _header(alias: str, start: datetime, end: datetime, count: int) -> str:
    return (
        f"📋 TỔNG KẾT NHÓM {alias.upper()}\n"
        f"⏱ {start.astimezone(VN_TZ).strftime('%H:%M %d/%m')} → "
        f"{end.astimezone(VN_TZ).strftime('%H:%M %d/%m')}\n"
        f"💬 {count} tin nhắn\n\n"
    )


def _transcript(rows) -> list[str]:
    return [
        f"[{sent_at.astimezone(VN_TZ).strftime('%d/%m %H:%M')}] {sender_name or sender_id}: {content}"
        for sender_id, sender_name, content, sent_at in rows
    ]


def _chunks(lines: list[str]) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in lines:
        if current and current_size + len(line) > _CHUNK_CHAR_LIMIT:
            chunks.append("\n".join(current))
            current, current_size = [], 0
        current.append(line)
        current_size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


async def today_discussion(account_id: str, target: str) -> tuple[str, str, list[str]]:
    """Trả về (group_id, alias, các đoạn tin nhắn) - NGUYÊN VĂN tin nhắn từ đầu
    ngày hôm nay (giờ VN) đến hiện tại, KHÔNG qua AI tóm tắt (khác
    summarize_group ở trên). Dùng khi cần đọc lại đúng nội dung đã nhắn thay vì
    bản tóm tắt AI - vd đối chiếu số liệu, xem ai nói gì chính xác."""
    group = await zalo_repository.resolve_group(account_id, target)
    if group is None:
        raise ValueError(f"Không tìm thấy nhóm “{target}”.")
    group_id, alias = group
    start, end = resolve_window("homnay")
    rows = await zalo_repository.get_group_messages(account_id, group_id, start, end, limit=2000)
    if not rows:
        return group_id, alias, [f"📭 Nhóm {alias} chưa có tin nhắn nào hôm nay."]

    lines = _transcript(rows)
    header = (
        f"💬 THẢO LUẬN HÔM NAY — {alias.upper()}\n"
        f"📅 {start.astimezone(VN_TZ).strftime('%d/%m/%Y')} · {len(rows)} tin nhắn\n"
    )
    chunks = _chunks(lines)
    total = len(chunks)
    parts = [
        header + (f"(phần {index}/{total})\n\n" if total > 1 else "\n") + chunk
        for index, chunk in enumerate(chunks, 1)
    ]
    return group_id, alias, parts


async def summarize_group(
    account_id: str,
    target: str,
    start: datetime,
    end: datetime,
) -> tuple[str, str, str]:
    group = await zalo_repository.resolve_group(account_id, target)
    if group is None:
        raise ValueError(f"Không tìm thấy nhóm “{target}”.")
    group_id, alias = group
    rows = await zalo_repository.get_group_messages(account_id, group_id, start, end, limit=2000)
    if not rows:
        return group_id, alias, f"📭 Nhóm {alias} không có tin nhắn trong khoảng đã chọn."

    lines = _transcript(rows)
    header = _header(alias, start, end, len(rows))

    if len(rows) <= VERBATIM_MESSAGE_LIMIT:
        body = "NGUYÊN VĂN (quá ít tin để tổng kết):\n" + "\n".join(lines)
        return group_id, alias, header + _render(body)

    if len(rows) <= SHORT_SUMMARY_MESSAGE_LIMIT:
        short = await _ask(_short_prompt(alias) + "\n\n" + "\n".join(lines))
        return group_id, alias, header + (_render(short) or _EMPTY_BODY)

    chunks = _chunks(lines)
    partials: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        partials.append(await _ask(_chunk_prompt(index, len(chunks)) + "\n\n" + chunk))

    combined = "\n\n--- PHẦN ---\n\n".join(partials)
    final = await _ask(_merge_prompt(alias) + "\n\n" + combined)
    return group_id, alias, header + (_render(final) or _EMPTY_BODY)
