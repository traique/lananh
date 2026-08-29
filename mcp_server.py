"""MCP server cho lananh (phần "2" trong yêu cầu "biến lananh thành agent"):
phơi 1 vài khả năng SẴN CÓ của bot (tìm giá, xem thống kê, xem trạng thái
provider) qua Model Context Protocol, để Claude Desktop/Claude Code (hoặc bất
kỳ MCP client nào) gọi thẳng được - không cần mở Telegram/Zalo/Zoom.

Học theo cấu trúc mobile-mcp (github.com/mobile-next/mobile-mcp): 1 tool =
1 khả năng rõ ràng, mô tả ngắn cho model biết KHI NÀO nên gọi, và LUÔN tái
dùng logic nghiệp vụ đã có (handlers/commands.py, ai/orchestrator.py) thay vì
viết lại - MCP server này chỉ là 1 lớp mỏng bọc ngoài, không phải bản sao.

CHỈ PHƠI TOOL ĐỌC (read-only, an toàn) - KHÔNG có /reset, /forget, đổi
provider, hay bất kỳ hành động ghi/phá huỷ nào. Chạy server này ở máy cá
nhân (không deploy lên Render cùng web service), dùng chung DATABASE_URL với
bot đang chạy thật (đọc từ .env qua core/config.py) để số liệu khớp nhau.

Chạy thử: `python mcp_server.py` (stdio transport - dùng cho Claude Desktop/
Claude Code cấu hình dạng {"command": "python", "args": ["mcp_server.py"]}).
Cần cài thêm: pip install -r requirements-mcp.txt (KHÔNG gộp vào
requirements.txt vì đó là file dùng để deploy web service lên Render, không
cần biết gì về MCP).
"""
import asyncio
import logging

from mcp.server.fastmcp import FastMCP

from ai import orchestrator
from core import config, database as db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("lananh")

_ready = False
_ready_lock = asyncio.Lock()


async def _ensure_ready() -> None:
    """Khởi tạo pool DB đúng 1 lần cho tiến trình MCP server này - không đi
    qua bot_app.py::_post_init() vì đây là tiến trình RIÊNG, không phải bot
    Telegram đang chạy thật."""
    global _ready
    if _ready:
        return
    async with _ready_lock:
        if _ready:
            return
        await db.init_db()
        await orchestrator.init_provider_state()
        _ready = True


@mcp.tool()
async def tim_gia(ten_san_pham: str) -> str:
    """Tìm giá bán thực tế của 1 sản phẩm tại Việt Nam (tìm web thật qua
    Tavily/Google Search, không bịa số liệu). Dùng khi người dùng hỏi giá 1
    món hàng cụ thể, vd 'iPhone 15 128GB', 'nồi chiên không dầu Philips'."""
    await _ensure_ready()
    from handlers import commands as telegram_commands

    if not ten_san_pham or not ten_san_pham.strip():
        return "Lỗi: thiếu tên sản phẩm."
    text, _used_fallback = await telegram_commands._search_price(ten_san_pham.strip())
    return text or "Không tìm được giá cho sản phẩm này."


@mcp.tool()
async def xem_thong_ke(so_gio: int = 168) -> str:
    """Xem thống kê lượt gọi AI của bot lananh: số lượt theo (kênh, user) và
    theo (provider, model), trong N giờ gần nhất (mặc định 168 = 7 ngày)."""
    await _ensure_ready()
    from handlers import commands as telegram_commands

    so_gio = so_gio if isinstance(so_gio, int) and so_gio > 0 else 168
    return await telegram_commands._build_thongke_text(so_gio, use_html=False)


@mcp.tool()
async def xem_trang_thai_provider() -> str:
    """Xem provider AI nào (router9/groq/openrouter/api1/api2) đang phục vụ
    bot lananh, và provider nào đang cooldown/chết. Dùng khi cần kiểm tra
    nhanh sức khoẻ hạ tầng AI của bot mà không cần mở Telegram gõ /status."""
    await _ensure_ready()
    state = orchestrator.get_provider_state_snapshot()
    lines = [
        f"Provider đang dùng: {state['active_provider']}",
        f"Thứ tự ưu tiên: {' -> '.join(config.PROVIDER_ORDER)}",
        f"9Router: {'bật' if state['router9_enabled'] else 'TẮT thủ công'}"
        + (f", chết từ {state['router9_dead_since']}" if state["router9_dead_since"] else " (đang sống)"),
    ]
    for name in ("groq", "openrouter", "api1", "api2"):
        until = state.get(f"{name}_exhausted_until", 0.0)
        lines.append(f"{name}: {'đang cooldown tới ' + str(until) if until else 'sẵn sàng'}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
