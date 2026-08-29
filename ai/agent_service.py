"""AI agent thật (phần "1" trong yêu cầu "biến lananh thành agent"): khác
ai/orchestrator.py (pipeline CỐ ĐỊNH - mỗi lệnh gọi đúng 1-2 hàm theo thứ tự
lập trình sẵn), module này để MODEL tự quyết định có cần gọi tool nào, gọi
bao nhiêu lần, theo thứ tự nào, để trả lời 1 câu hỏi - dùng Google GenAI
function calling (`google.genai.types.FunctionDeclaration`).

Dùng riêng api1/api2 (KHÔNG qua router9/groq/openrouter) vì function calling
kiểu Gemini cần đúng định dạng response `functionCall`/`functionResponse` -
router9/groq là proxy nên không đảm bảo tương thích. Đây cũng là 2 provider
"lưới an toàn" (quota thấp nhất) trong provider-chain, nên vòng lặp agent
CHỦ ĐỘNG giới hạn số bước (MAX_AGENT_STEPS) để không đốt quota nhanh.

MVP: 2 tool ban đầu (tim_gia, xem_thong_ke), tái dùng thẳng logic đã có ở
handlers/commands.py và core/database.py - KHÔNG viết lại logic tìm giá/thống
kê. Muốn thêm tool mới: viết 1 async function nhận **kwargs trả về str, rồi
thêm vào _TOOLS bên dưới (schema + hàm thực thi) - vòng lặp _run_agent_loop
tự động dùng được, không cần sửa gì khác.
"""
import json
import logging

from ai import official_client
from ai.official_client import is_quota_exhausted_error
from ai.timeouts import OFFICIAL_CHAT_TIMEOUT_SEC, with_timeout

logger = logging.getLogger(__name__)

MAX_AGENT_STEPS = 4
_AGENT_PROVIDERS = ("api1", "api2")

_SYSTEM_INSTRUCTION = (
    "Bạn là trợ lý của lananh, 1 bot Telegram/Zalo/Zoom cá nhân. Bạn có các "
    "tool bên dưới để tra cứu dữ liệu THẬT trước khi trả lời - KHÔNG tự bịa "
    "số liệu. Nếu câu hỏi cần nhiều bước (vd so sánh 2 sản phẩm), hãy gọi "
    "tool nhiều lần rồi mới tổng hợp câu trả lời cuối. Trả lời ngắn gọn, "
    "tiếng Việt, không cần nhắc lại đã dùng tool nào."
)


async def _tool_tim_gia(ten_san_pham: str) -> str:
    # Import trễ (lazy) để tránh vòng import: handlers/commands.py cũng import
    # nhiều module ai.* ở mức module-level.
    from handlers import commands as telegram_commands

    if not ten_san_pham or not ten_san_pham.strip():
        return "Lỗi: thiếu tên sản phẩm."
    text, _used_fallback = await telegram_commands._search_price(ten_san_pham.strip())
    return text or "Không tìm được giá cho sản phẩm này."


async def _tool_xem_thong_ke(so_gio: int = 168) -> str:
    from handlers import commands as telegram_commands

    so_gio = so_gio if isinstance(so_gio, int) and so_gio > 0 else 168
    return await telegram_commands._build_thongke_text(so_gio, use_html=False)


# name -> (mô tả cho model, JSON schema tham số kiểu OpenAPI, hàm thực thi)
_TOOLS: dict[str, tuple[str, dict, callable]] = {
    "tim_gia": (
        "Tìm giá bán thực tế của 1 sản phẩm tại Việt Nam (tìm web thật, không bịa).",
        {
            "type": "object",
            "properties": {
                "ten_san_pham": {"type": "string", "description": "Tên sản phẩm cần tìm giá, vd 'iPhone 15 128GB'"},
            },
            "required": ["ten_san_pham"],
        },
        _tool_tim_gia,
    ),
    "xem_thong_ke": (
        "Xem thống kê lượt gọi AI của bot (theo user/kênh và theo model) trong N giờ gần nhất.",
        {
            "type": "object",
            "properties": {
                "so_gio": {"type": "integer", "description": "Số giờ gần nhất muốn xem, mặc định 168 (7 ngày)"},
            },
        },
        _tool_xem_thong_ke,
    ),
}


def _build_tool_declarations():
    from google.genai import types

    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name=name, description=desc, parameters=schema)
                for name, (desc, schema, _fn) in _TOOLS.items()
            ]
        )
    ]


async def _call_tool(name: str, args: dict) -> str:
    entry = _TOOLS.get(name)
    if entry is None:
        return f"Lỗi: không có tool tên '{name}'."
    _desc, _schema, fn = entry
    try:
        return await fn(**(args or {}))
    except Exception as exc:
        logger.warning("Tool '%s' lỗi với args=%r", name, args, exc_info=True)
        # Trả lỗi VÀO lại cho model (không raise) - để model tự quyết định thử
        # cách khác hoặc báo người dùng, thay vì cả agent crash vì 1 tool lỗi.
        return f"Lỗi khi chạy tool '{name}': {exc}"


async def _run_one_provider(idx: int, question: str) -> str:
    from google.genai import types

    client, model = await official_client.get_client_and_model(idx)
    contents: list = [
        types.Content(role="user", parts=[types.Part.from_text(text=question)]),
    ]
    tools = _build_tool_declarations()
    gen_config = types.GenerateContentConfig(system_instruction=_SYSTEM_INSTRUCTION, tools=tools)

    for step in range(MAX_AGENT_STEPS):
        response = await with_timeout(
            client.aio.models.generate_content(model=model, contents=contents, config=gen_config),
            OFFICIAL_CHAT_TIMEOUT_SEC,
            f"agent api{idx} step {step + 1}",
        )
        candidate = response.candidates[0] if response.candidates else None
        parts = candidate.content.parts if candidate and candidate.content else []
        function_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

        if not function_calls:
            return (response.text or "").strip() or "Xin lỗi, em chưa tìm được câu trả lời."

        # Model có thể yêu cầu nhiều tool cùng lúc trong 1 bước - chạy tuần tự
        # (đơn giản, đủ dùng cho quy mô 1 user; không cần asyncio.gather).
        contents.append(candidate.content)
        response_parts = []
        for call in function_calls:
            logger.info("Agent gọi tool '%s' với args=%s", call.name, dict(call.args or {}))
            result_text = await _call_tool(call.name, dict(call.args or {}))
            response_parts.append(
                types.Part.from_function_response(name=call.name, response={"result": result_text})
            )
        contents.append(types.Content(role="user", parts=response_parts))

    return "Xin lỗi, câu hỏi này cần nhiều bước quá, em dừng lại để không lặp vô hạn - anh hỏi cụ thể/ngắn hơn giúp em nhé."


async def ask_agent(question: str) -> tuple[str, str]:
    """Chạy vòng lặp agent cho 1 câu hỏi, trả về (câu trả lời, provider đã
    dùng). Thử api1 trước, hết quota (429) thì rớt xuống api2 - giống mọi
    nhánh api1/api2 khác trong ai/official_client.py."""
    last_exc: Exception | None = None
    for idx, name in ((1, "api1"), (2, "api2")):
        try:
            text = await _run_one_provider(idx, question)
            return text, name
        except Exception as exc:
            last_exc = exc
            if is_quota_exhausted_error(exc):
                logger.warning("Agent: %s hết quota, thử provider kế tiếp.", name)
                continue
            logger.exception("Agent: lỗi không phải quota ở %s.", name)
            continue
    raise RuntimeError(f"Cả api1 và api2 đều lỗi cho agent: {last_exc}")
