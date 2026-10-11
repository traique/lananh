"""/thongke - thống kê lượt gọi AI theo user và theo model.

Dùng chung cho Telegram (handlers/commands.py::thongke_cmd), Zalo/Zoom
(services/channel_command_service.py) và tool xem_thong_ke của /agent.
Số liệu lấy từ core/database.py::usage_by_user/usage_by_model - CÙNG 2 hàm
đang phục vụ trang admin (xem web.py::admin_usage/admin_usage_models), nên
/thongke luôn khớp với trang admin.
"""

import html
from zoneinfo import ZoneInfo

from channels import zalo_users
from core import config, database as db

DEFAULT_HOURS = 24 * 7
_CHANNEL_LABELS = {"telegram": "Telegram", "zoom": "Zoom", "zalo": "Zalo"}


def _fmt_dt_vn(dt) -> str:
    if dt is None:
        return "-"
    return dt.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%H:%M %d/%m")


def parse_thongke_hours(arg: str) -> int:
    """Chấp nhận số giờ (vd "48"), hoặc số ngày kèm hậu tố "d" (vd "3d" = 72
    giờ). Không parse được / để trống -> mặc định DEFAULT_HOURS (7 ngày),
    KHÔNG báo lỗi cú pháp - /thongke không tham số vẫn phải chạy."""
    arg = arg.strip().lower()
    if not arg:
        return DEFAULT_HOURS
    try:
        if arg.endswith("d"):
            return max(1, int(float(arg[:-1]) * 24))
        return max(1, int(float(arg)))
    except ValueError:
        return DEFAULT_HOURS


async def build_thongke_text(since_hours: int = DEFAULT_HOURS, use_html: bool = True) -> str:
    """use_html=True (Telegram, parse_mode=HTML): bọc số liệu bằng <b> và
    html.escape() nhãn. use_html=False (Zalo/Zoom - channels/zalo_text.py chỉ
    hiển thị PLAIN TEXT, không tự dịch thẻ <b>): bỏ thẻ, không escape, vì text
    ở đây không đi qua trình render HTML nào."""
    user_rows = await db.usage_by_user(since_hours)
    model_rows = await db.usage_by_model(since_hours)
    zalo_by_id = {u.internal_user_id: u for u in await zalo_users.list_users()}

    def esc(s: str) -> str:
        return html.escape(s) if use_html else s

    def bold(s: str) -> str:
        return f"<b>{s}</b>" if use_html else s

    period = f"{since_hours // 24} ngày" if since_hours % 24 == 0 else f"{since_hours} giờ"
    lines = [
        f"📊 {bold(f'Thống kê lượt gọi ({period} gần nhất)')}",
        "",
        f"👤 {bold('Theo user')}",
    ]

    if not user_rows:
        lines.append("(chưa có dữ liệu)")
    else:
        for row in user_rows:
            channel, uid = row["channel"], row["telegram_user_id"]
            if channel == "zalo" and uid in zalo_by_id:
                zuser = zalo_by_id[uid]
                label = zuser.display_name or zuser.external_id
            elif uid == config.ALLOWED_USER_ID:
                label = "Chủ bot"
            else:
                label = str(uid)
            channel_label = _CHANNEL_LABELS.get(channel, channel)
            lines.append(
                f"• [{channel_label}] {esc(label)}: {bold(str(row['calls']))} lượt "
                f"(gần nhất {_fmt_dt_vn(row['last_call_at'])})"
            )

    lines.append("")
    lines.append(f"🧠 {bold('Theo model')}")
    if not model_rows:
        lines.append("(chưa có dữ liệu)")
    else:
        for row in model_rows:
            lines.append(
                f"• {esc(row['provider'])}/{esc(row['model'])}: "
                f"{bold(str(row['calls']))} lượt (gần nhất {_fmt_dt_vn(row['last_call_at'])})"
            )

    return "\n".join(lines)
