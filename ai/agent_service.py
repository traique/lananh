"""AI agent thật (phần "1" trong yêu cầu "biến lananh thành agent"): khác
ai/orchestrator.py (pipeline CỐ ĐỊNH - mỗi lệnh gọi đúng 1-2 hàm theo thứ tự
lập trình sẵn), module này để MODEL tự quyết định có cần gọi tool nào, gọi
bao nhiêu lần, theo thứ tự nào, để trả lời 1 câu hỏi - dùng Google GenAI
function calling (`google.genai.types.FunctionDeclaration`).

Thử router9 theo 2 kiểu trước khi rơi xuống api1/api2 (function calling kiểu
Gemini, `functionCall`/`functionResponse` - 2 provider "lưới an toàn" quota
thấp nhất trong provider-chain):
  1. _run_router9(): function-calling chuẩn OpenAI đa bước (xem
     ai/router9_client.py::generate_with_tools) - "agent thật", có thể tự
     lặp gọi nhiều tool.
  2. _run_router9_json_router(): CHỈ chạy khi (1) lỗi - hỏi router9 bằng 1
     lượt JSON-router đơn giản (giống services/tools.py::maybe_run_tool,
     KHÔNG dùng tham số `tools` của API) xem có cần đúng 1 tool không, chạy
     tool đó (nếu có), rồi 1 lượt router9 nữa để tổng hợp câu trả lời tự
     nhiên. KHÔNG đa bước (tối đa 1 tool), nhưng KHÔNG phụ thuộc gateway có
     hỗ trợ đúng function-calling chuẩn OpenAI hay không - chỉ cần model
     theo được chỉ dẫn "trả JSON", hầu như gateway OpenAI-compatible nào
     cũng làm được việc này (đã có tiền lệ trong services/tools.py, chạy ổn
     định). Đây là lưới an toàn TRUNG GIAN, ưu tiên hơn api1/api2.

Vòng lặp CHỦ ĐỘNG giới hạn số bước (MAX_AGENT_STEPS) ở nhánh (1) và
_run_one_provider() (api1/api2) để không đốt quota nhanh - nhánh (2) không
cần giới hạn vì tự thân đã chỉ tối đa 1 tool, không lặp.

LƯU Ý QUAN TRỌNG về nhánh (1) router9 native tool-calling: gateway
OpenAI-compatible bên thứ ba có thể ÂM THẦM bỏ qua tham số `tools` (không
lỗi, chỉ trả lời chữ bình thường) nếu backend thật sự đứng sau không hỗ trợ
function-calling - lúc đó agent sẽ tưởng model "chủ động thấy không cần
tool" và trả thẳng câu trả lời (có thể bịa số liệu thay vì tra tool thật).
Code không tự phát hiện được trường hợp này (tool_calls rỗng do lờ đi và
tool_calls rỗng do model chủ động thấy không cần đều giống hệt nhau ở phía
caller); xem log `"Agent (router9) yêu cầu N tool call(s)..."` bằng traffic
thật để tự kiểm chứng trước khi tin tưởng nhánh (1) ở production. Nhánh (2)
KHÔNG có lỗ hổng này vì không dựa vào `tools`/`tool_calls` của API - chỉ dựa
vào model làm đúng theo prompt JSON, đã có tiền lệ hoạt động ổn ở
services/tools.py.

Chưa thấy log `"Agent (router9) yêu cầu"` xuất hiện dù hỏi câu chắc chắn cần
tool (vd "so sánh giá iPhone 15 và 15 Pro") -> router9 đang lờ `tools` đi ở
nhánh (1), nên tắt riêng nhánh (1) qua `_ROUTER9_NATIVE_TOOLS_ENABLED =
False` bên dưới (nhánh (2) và api1/api2 không bị ảnh hưởng, cứ để mặc định).
Tắt hẳn cả router9 (cả 2 nhánh) qua `_ROUTER9_AGENT_ENABLED = False`.

Cố ý KHÔNG gọi provider_state.mark_router9_dead() khi cả 2 nhánh router9 của
agent đều lỗi: lỗi có thể do router9 không hỗ trợ tools/JSON đúng ý (đặc thù
agent), không phải router9 sập hẳn (ảnh hưởng cả chat chính) - tránh làm
chat chính mất oan router9 chỉ vì agent thất bại. Ngược lại, agent thành
công (ở nhánh nào cũng vậy) VẪN báo alive (tín hiệu tốt, không hại gì) và
tôn trọng router9_enabled/router9_dead_since đã biết (cờ /router9 off, hoặc
router9 đã được đánh dấu chết ở nhánh chat chính) để không lãng phí 1 lượt
gọi chắc chắn sẽ lỗi.

MVP: 2 tool ban đầu (tim_gia, xem_thong_ke), tái dùng thẳng logic đã có ở
handlers/commands.py và core/database.py - KHÔNG viết lại logic tìm giá/thống
kê. Muốn thêm tool mới: viết 1 async function nhận **kwargs trả về str, rồi
thêm vào _TOOLS bên dưới (schema + hàm thực thi) - vòng lặp _run_agent_loop
tự động dùng được, không cần sửa gì khác.
"""
import json
import logging
from typing import Optional

from ai import official_client
from ai.official_client import is_quota_exhausted_error
from ai.timeouts import OFFICIAL_CHAT_TIMEOUT_SEC, with_timeout

logger = logging.getLogger(__name__)

MAX_AGENT_STEPS = 4
_AGENT_PROVIDERS = ("api1", "api2")
# Tắt hẳn cả 2 nhánh router9 (native tools + JSON-router) của /agent, không
# đụng /router9 on|off của chat chính.
_ROUTER9_AGENT_ENABLED = True
# Tắt RIÊNG nhánh native tool-calling (function-calling chuẩn OpenAI) nếu
# xác nhận qua log router9 đang lờ `tools` đi - xem cảnh báo ở docstring
# module. Nhánh JSON-router (_run_router9_json_router) không bị ảnh hưởng
# bởi cờ này, vẫn thử trước khi rơi xuống api1/api2.
_ROUTER9_NATIVE_TOOLS_ENABLED = False

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


async def _tool_xem_gia_co_phieu(ma_co_phieu: str) -> str:
    from stock import analysis as stock_analysis

    if not ma_co_phieu or not ma_co_phieu.strip():
        return "Lỗi: thiếu mã cổ phiếu."
    return await stock_analysis.quick_quote(ma_co_phieu.strip())


async def _tool_doc_link(url: str) -> str:
    from services import web_reader

    if not url or not url.strip():
        return "Lỗi: thiếu URL."
    try:
        return await web_reader.read_url(url.strip())
    except web_reader.WebReaderError as exc:
        return f"Không đọc được link: {exc}"


async def _tool_xem_rss(url: str, so_muc: int = 0) -> str:
    from services import web_reader

    if not url or not url.strip():
        return "Lỗi: thiếu URL feed."
    try:
        return await web_reader.read_rss(url.strip(), so_muc)
    except web_reader.WebReaderError as exc:
        return f"Không đọc được feed: {exc}"


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
    "xem_gia_co_phieu": (
        "Xem giá cổ phiếu Việt Nam hiện tại theo mã (vd 'FPT', 'VNM'), lấy trực tiếp từ DNSE.",
        {
            "type": "object",
            "properties": {
                "ma_co_phieu": {"type": "string", "description": "Mã cổ phiếu, vd 'FPT'"},
            },
            "required": ["ma_co_phieu"],
        },
        _tool_xem_gia_co_phieu,
    ),
    "doc_link": (
        "Đọc nội dung 1 trang web/bài báo cụ thể theo URL người dùng đưa, trả về text sạch để tóm tắt/trả lời.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL đầy đủ (bắt đầu bằng http:// hoặc https://) cần đọc"},
            },
            "required": ["url"],
        },
        _tool_doc_link,
    ),
    "xem_rss": (
        "Đọc các mục mới nhất từ 1 feed RSS/Atom theo URL người dùng đưa.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL feed RSS/Atom"},
                "so_muc": {"type": "integer", "description": "Số mục mới nhất muốn xem, mặc định 8"},
            },
            "required": ["url"],
        },
        _tool_xem_rss,
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


def _build_openai_tools() -> list[dict]:
    """Cùng nội dung mô tả/schema với _build_tool_declarations() ở trên
    (dùng chung _TOOLS làm 1 nguồn duy nhất), chỉ khác khuôn JSON theo chuẩn
    OpenAI function-calling (`{"type": "function", "function": {...}}`) thay
    vì `types.FunctionDeclaration` của Google GenAI SDK."""
    return [
        {"type": "function", "function": {"name": name, "description": desc, "parameters": schema}}
        for name, (desc, schema, _fn) in _TOOLS.items()
    ]


_JSON_ROUTER_INSTRUCTION_TEMPLATE = """Bạn là bộ định tuyến tool (function router) nội bộ cho agent của lananh.
Đọc câu hỏi của người dùng và quyết định có cần gọi 1 trong các tool sau
không. Trả về DUY NHẤT 1 object JSON (không markdown, không code fence,
không thêm chữ nào khác) đúng định dạng:

{{"tool": "ten_tool_hoac_none", "args": {{...}}}}

Danh sách tool khả dụng:
{tool_list}
- "none": câu hỏi KHÔNG cần tool nào (chuyện phiếm, hỏi đáp không cần tra
  cứu dữ liệu thật). args: {{}}.

Quy tắc: CHỈ chọn 1 tool khi câu hỏi có ý định RÕ RÀNG khớp mô tả trên. Nếu
câu hỏi cần NHIỀU tool hoặc nhiều bước (vd so sánh 2 sản phẩm), vẫn chỉ được
chọn ĐÚNG 1 tool phù hợp nhất cho bước đầu tiên (nhánh này không hỗ trợ đa
bước) - hoặc chọn "none" nếu không có tool nào giải quyết được 1 phần rõ
ràng của câu hỏi.

Câu hỏi của người dùng: {question}"""


def _build_tool_list_text() -> str:
    """Liệt kê _TOOLS thành text cho prompt JSON-router - tự sinh từ cùng 1
    nguồn _TOOLS như _build_tool_declarations()/_build_openai_tools(), thêm
    tool mới vào _TOOLS là nhánh JSON-router tự biết, không cần sửa gì ở đây."""
    lines = []
    for name, (desc, schema, _fn) in _TOOLS.items():
        props = (schema or {}).get("properties", {}) or {}
        args_desc = ", ".join(
            f'"{key}": {prop.get("description") or prop.get("type", "any")}' for key, prop in props.items()
        )
        lines.append(f'- "{name}": {desc} args: {{{args_desc}}}')
    return "\n".join(lines)


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


async def _run_router9(question: str) -> str:
    """Agent loop qua router9 (function-calling chuẩn OpenAI). Cấu trúc
    tương tự _run_one_provider() (cùng MAX_AGENT_STEPS, cùng _call_tool()),
    chỉ khác định dạng messages/tool_calls theo chuẩn OpenAI thay vì
    Content/Part của Google GenAI SDK."""
    from ai import router9_client

    conversation: list[dict] = [
        {"role": "system", "content": _SYSTEM_INSTRUCTION},
        {"role": "user", "content": question},
    ]
    tools = _build_openai_tools()

    for step in range(MAX_AGENT_STEPS):
        result = await router9_client.generate_with_tools(conversation, tools)

        if not result.tool_calls:
            return result.text or "Xin lỗi, em chưa tìm được câu trả lời."

        logger.info("Agent (router9) yêu cầu %d tool call(s) ở bước %d.", len(result.tool_calls), step + 1)
        conversation.append(
            {
                "role": "assistant",
                "content": result.text or None,
                "tool_calls": [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)},
                    }
                    for call in result.tool_calls
                ],
            }
        )
        for call in result.tool_calls:
            logger.info("Agent (router9) gọi tool '%s' với args=%s", call["name"], call["arguments"])
            result_text = await _call_tool(call["name"], call["arguments"])
            conversation.append({"role": "tool", "tool_call_id": call["id"], "content": result_text})

    return "Xin lỗi, câu hỏi này cần nhiều bước quá, em dừng lại để không lặp vô hạn - anh hỏi cụ thể/ngắn hơn giúp em nhé."


async def _run_router9_json_router(question: str) -> Optional[str]:
    """Lưới an toàn TRUNG GIAN (Hướng A) - chỉ gọi khi _run_router9() (native
    tool-calling) lỗi. KHÔNG dùng tham số `tools` của API - chỉ hỏi router9
    bằng 1 prompt yêu cầu trả JSON (cùng kiểu services/tools.py::maybe_run_tool),
    nên không phụ thuộc gateway có hỗ trợ đúng function-calling chuẩn OpenAI
    hay không. Tối đa 1 tool (không đa bước). Trả về None nếu không xác định
    được tool hợp lệ hoặc lỗi bất kỳ bước nào - caller (ask_agent) coi None
    là "nhánh này không giải quyết được", rơi tiếp xuống api1/api2."""
    from ai import router9_client

    router_prompt = _JSON_ROUTER_INSTRUCTION_TEMPLATE.format(tool_list=_build_tool_list_text(), question=question)
    try:
        router_response = await router9_client.generate(router_prompt, temperature=0.1)
    except Exception:
        logger.warning("Agent (router9 JSON-router): lỗi khi hỏi định tuyến, bỏ qua nhánh này.", exc_info=True)
        return None

    raw = (router_response.text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Agent (router9 JSON-router): model không trả JSON hợp lệ, bỏ qua nhánh này. raw=%r", raw[:200])
        return None

    tool_name = str(data.get("tool", "none")).strip()

    if tool_name == "none" or tool_name not in _TOOLS:
        # Không cần tool - hỏi router9 trả lời trực tiếp bằng văn phong tự
        # nhiên (KHÔNG dùng lại text ép JSON ở trên).
        try:
            reply = await router9_client.generate(question, system_instruction=_SYSTEM_INSTRUCTION)
            return (reply.text or "").strip() or None
        except Exception:
            logger.warning("Agent (router9 JSON-router): lỗi khi trả lời trực tiếp, bỏ qua nhánh này.", exc_info=True)
            return None

    args = data.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    logger.info("Agent (router9 JSON-router) gọi tool '%s' với args=%s", tool_name, args)
    tool_result = await _call_tool(tool_name, args)

    synth_prompt = (
        f"Câu hỏi của người dùng: {question}\n\n"
        f"Kết quả tool '{tool_name}' vừa chạy để trả lời câu hỏi trên:\n{tool_result}\n\n"
        "Dựa DUY NHẤT vào kết quả tool ở trên, trả lời người dùng bằng văn phong tự nhiên, ngắn gọn, tiếng Việt. "
        "Không tự bịa thêm thông tin ngoài kết quả tool."
    )
    try:
        final = await router9_client.generate(synth_prompt, system_instruction=_SYSTEM_INSTRUCTION)
        return (final.text or "").strip() or tool_result
    except Exception:
        logger.warning(
            "Agent (router9 JSON-router): lỗi khi tổng hợp câu trả lời cuối, trả thẳng kết quả tool thô.",
            exc_info=True,
        )
        return tool_result


async def ask_agent(question: str) -> tuple[str, str]:
    """Chạy vòng lặp agent cho 1 câu hỏi, trả về (câu trả lời, provider đã
    dùng). Thử router9 theo thứ tự 2 nhánh (native tool-calling rồi
    JSON-router - xem docstring module), lỗi cả 2 thì rơi xuống api1/api2:
    api1 trước, hết quota (429) thì rớt xuống api2 - giống mọi nhánh
    api1/api2 khác trong ai/official_client.py."""
    if _ROUTER9_AGENT_ENABLED:
        from ai.provider_state import provider_state

        await provider_state.ensure_loaded()
        if provider_state.router9_enabled and provider_state.router9_dead_since is None:
            if _ROUTER9_NATIVE_TOOLS_ENABLED:
                try:
                    text = await _run_router9(question)
                    await provider_state.mark_router9_alive()
                    return text, "router9"
                except Exception as exc:
                    # Cố ý KHÔNG mark_router9_dead() - xem docstring module.
                    logger.warning("Agent: router9 (native tools) lỗi (%s), thử JSON-router.", exc, exc_info=True)

            try:
                text = await _run_router9_json_router(question)
                if text:
                    await provider_state.mark_router9_alive()
                    return text, "router9"
                logger.warning("Agent: router9 (JSON-router) không xác định được tool/trả lời, chuyển sang api1/api2.")
            except Exception as exc:
                logger.warning("Agent: router9 (JSON-router) lỗi ngoài dự kiến (%s), chuyển sang api1/api2.", exc, exc_info=True)

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
    raise RuntimeError(f"Cả router9, api1 và api2 đều lỗi cho agent: {last_exc}")
