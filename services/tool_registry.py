"""Danh bạ tool dùng chung cho router JSON của chat (services/tools.py) và /agent
(ai/agent_service.py).

``ToolSpec`` mô tả 1 tool ở MỘT chỗ: tên, mô tả tiếng Việt, tham số (JSON
schema), hàm thực thi, có GHI dữ liệu không (``side_effect``), ai được dùng
(``roles``), kết quả có phải nội dung ngoài không tin cậy (``untrusted_output``)
và mặt nào dùng (``surfaces``: chat, agent). Từ 1 nguồn đó sinh ra: đoạn liệt
kê tool trong prompt router JSON, tools chuẩn OpenAI, FunctionDeclaration của
Gemini.

Quy ước handler: ``async def handler(user_id: int, **args) -> str | ToolResult``
- luôn nhận ``user_id`` đầu tiên (tool agent không cần thì bỏ qua) và có
``**_ignored`` để tham số lạ model tự thêm không làm TypeError. Tool ghi dữ
liệu trả ``ToolResult`` để code (không phải model) quyết định câu xác nhận.

Thêm tool mới (vd ``remember_fact`` của gói trí nhớ hội thoại)::

    from services import tool_registry as reg

    async def _remember_fact(user_id: int, fact: str = "", **_ignored) -> reg.ToolResult:
        ...
        return reg.ToolResult(f'Đã nhớ: "{fact}"', confirmation=f'Em nhớ rồi: "{fact}"')

    reg.register(reg.ToolSpec(
        name="remember_fact",
        description="Ghi nhớ lâu dài 1 sự thật về người dùng.",
        parameters=reg.object_schema({"fact": reg.string("điều cần nhớ")}, required=["fact"]),
        handler=_remember_fact,
        side_effect=True,
        effect_label="điều cần nhớ",
        router_hint='args {"fact": điều cần nhớ}. Khi nói "nhớ giúp anh là ...".',
    ))

Mô tả viết NGẮN (1 câu + dạng args + khi nào dùng/1 ví dụ): mỗi lượt router và
mỗi bước /agent gửi lại cả danh bạ, Groq free chỉ 8K token/phút -
test/test_tools.py::test_tool_catalog_budget đặt trần tổng ký tự.

``ToolOutcome`` là kết quả 1 lượt router chat: status none|ok|invalid|failed|
unavailable|blocked - KHÔNG BAO GIỜ trả None im lặng, để lượt chat biết chắc
việc ghi chú/nhắc việc đã lưu hay chưa (không để model tự "xác nhận" bừa).
"""

from __future__ import annotations

import contextvars
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Iterator

CHAT = "chat"
AGENT = "agent"

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ALL_ROLES = frozenset({ROLE_OWNER, ROLE_ADMIN, ROLE_MEMBER})
# Chủ bot + admin Zalo (gồm tài khoản Zalo của chủ bot): kho kiến thức rag/ riêng.
STAFF_ROLES = frozenset({ROLE_OWNER, ROLE_ADMIN})

# ─── Trạng thái 1 lượt router ───────────────────────────────────────────────
STATUS_NONE = "none"  # không cần tool (lọc từ khoá) hoặc router chọn "none"
STATUS_OK = "ok"  # tool chạy xong
STATUS_INVALID = "invalid"  # tool từ chối tham số (thiếu giờ, giờ đã qua...): CHƯA lưu, hỏi lại
STATUS_FAILED = "failed"  # handler lỗi (TypeError, DB, ValueError)
STATUS_UNAVAILABLE = "unavailable"  # router không trả lời được (mọi đường utility đều lỗi)
STATUS_BLOCKED = "blocked"  # tool ghi dữ liệu bị chặn (lượt chỉ đọc, dữ liệu ngoài, quyền)

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ToolResult:
    """Kết quả có cấu trúc của 1 handler (handler trả str = ToolResult(text)).

    confirmation: câu xác nhận CHUẨN do code soạn (vd "Đã đặt nhắc lúc 20:00
    Thứ Bảy 25/10 (giờ VN): gọi mẹ"). confirmation_key: chuỗi câu trả lời phải có
    (nhiều cách viết nối bằng "|", vd "20:00|20h|8h"; nhiều nhóm bắt buộc nối bằng
    "&", vd "8h|08:00&15/11|ngày 15") - thiếu thì lượt chat tự nối confirmation;
    rỗng = không kiểm."""

    text: str
    status: str = STATUS_OK
    confirmation: str = ""
    confirmation_key: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    side_effect: bool = False
    roles: frozenset[str] = ALL_ROLES
    untrusted_output: bool = False
    surfaces: frozenset[str] = frozenset({CHAT})
    # Chỉ dẫn thêm cho router JSON của chat: dạng args, khi nào dùng, ví dụ.
    router_hint: str = ""
    # Tên việc ghi dữ liệu cho câu báo "chưa lưu được ..." ("nhắc việc", "ghi chú"...).
    effect_label: str = ""

    def allowed_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """Chỉ giữ tham số khai báo trong schema (bỏ "user_id" model tự thêm và
        khoá lạ) - handler vẫn có **_ignored làm lưới an toàn thứ hai."""
        props = (self.parameters or {}).get("properties") or {}
        return {k: v for k, v in (args or {}).items() if k in props and k != "user_id"}


@dataclass(frozen=True)
class ToolOutcome:
    status: str = STATUS_NONE
    name: str = ""
    text: str = ""
    side_effect: bool = False
    # Ý định GHI dữ liệu nhận ra bằng regex (độc lập router): "reminder" | "note" | "expense".
    intent: str = ""
    confirmation: str = ""
    confirmation_key: str = ""
    router_ran: bool = False
    effect_label: str = ""
    untrusted: bool = False
    # Câu hỏi tra dữ liệu riêng của người dùng ("nhắc việc", "chi tiêu"...) mà router
    # không chạy được: lượt chat phải nói thật là chưa tra được, không đoán.
    lookup: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def saved(self) -> bool:
        """Tool ghi dữ liệu đã chạy thành công (đã lưu thật)."""
        return self.status == STATUS_OK and self.side_effect

    @property
    def not_saved_hard(self) -> bool:
        """Người dùng muốn lưu mà hệ thống hỏng/không chạy được: phải báo rõ
        bằng câu do code nối thêm (không trông vào model)."""
        if self.status in (STATUS_FAILED, STATUS_UNAVAILABLE):
            return bool(self.intent) or self.side_effect
        return self.status == STATUS_BLOCKED and (self.side_effect or bool(self.intent))

    def grounding(self) -> str:
        """Khối ngữ cảnh chèn vào lượt chat (rỗng = không chèn gì)."""
        label = self.effect_label or intent_label(self.intent)
        if self.status == STATUS_OK:
            head = f"[Kết quả tool '{self.name}' vừa chạy - dùng thông tin này để trả lời tự nhiên]"
            body = self.text
            if self.side_effect and self.confirmation:
                body += (
                    "\n[HỆ THỐNG] Việc này ĐÃ được lưu thật. Khi xác nhận, dùng ĐÚNG thông tin "
                    f"sau, không tự đổi giờ/ngày/số tiền: {self.confirmation}"
                )
            return f"{head}\n{body}"
        if self.status == STATUS_INVALID:
            return (
                f"[Kết quả tool '{self.name}' - CHƯA lưu {label or 'gì cả'}]\n{self.text}\n"
                "[HỆ THỐNG] KHÔNG được nói là đã làm; hỏi lại người dùng đúng điều còn thiếu."
            )
        if self.not_saved_hard:
            reason = f" ({self.text})" if self.text else ""
            return (
                f"[HỆ THỐNG] CHƯA lưu được {label or 'yêu cầu này'}{reason} - KHÔNG được nói "
                "là đã làm; xin lỗi ngắn và mời người dùng thử lại sau ít phút."
            )
        if self.intent and self.router_ran:
            return (
                f"[HỆ THỐNG] Lượt này không lưu {label} nào - không được nói là đã ghi chú/"
                "đặt nhắc/ghi chi tiêu."
            )
        if self.status == STATUS_FAILED and self.name and not self.side_effect:
            return (
                f"[HỆ THỐNG] Không tra được dữ liệu ({self.name}) lúc này - KHÔNG được đoán "
                "hay nói là không có; nói thật là chưa tra được và mời thử lại sau ít phút."
            )
        if self.status in (STATUS_FAILED, STATUS_UNAVAILABLE) and self.lookup:
            return (
                f"[HỆ THỐNG] Hệ thống tra cứu đang lỗi: nếu người dùng hỏi {self.lookup} của "
                "chính họ thì lúc này chưa tra được - KHÔNG được đoán hay nói là không có; "
                "nói thật là chưa tra được và mời thử lại sau ít phút."
            )
        return ""


def intent_label(intent: str) -> str:
    return {"reminder": "nhắc việc", "note": "ghi chú", "expense": "khoản chi"}.get(intent, "")


# ─── Cờ "đã nhiễm nội dung ngoài" của 1 lượt ────────────────────────────────
# Tool có untrusted_output (đọc link, tìm web...) đã chạy thì tool GHI dữ liệu bị
# chặn tới hết lượt: 1 trang độc không xúi được model đặt nhắc/ghi chú/chi tiêu.
# ContextVar giữ 1 đối tượng trạng thái (không phải bool) để task con của
# asyncio.gather đánh dấu thì lượt cha cũng thấy; ngoài taint_scope() thì no-op.


class _TaintState:
    __slots__ = ("tainted",)

    def __init__(self) -> None:
        self.tainted = False


_taint: contextvars.ContextVar[_TaintState | None] = contextvars.ContextVar(
    "tool_taint", default=None
)


@contextmanager
def taint_scope() -> Iterator[None]:
    """Phạm vi 1 lượt (chat hoặc /agent): bắt đầu sạch, kết thúc trả lại như cũ."""
    token = _taint.set(_TaintState())
    try:
        yield
    finally:
        _taint.reset(token)


def mark_tainted() -> None:
    state = _taint.get()
    if state is not None:
        state.tainted = True


def is_tainted() -> bool:
    state = _taint.get()
    return state is not None and state.tainted


# ─── Danh bạ ────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    """Đăng ký (trùng tên thì thay bản cũ - test/gói khác ghi đè được)."""
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def specs(surface: str, role: str | None = None) -> list[ToolSpec]:
    """Tool của 1 mặt (chat/agent), lọc theo vai trò người gọi nếu có."""
    return [
        spec
        for spec in _REGISTRY.values()
        if surface in spec.surfaces and (role is None or role in spec.roles)
    ]


# ─── Dựng schema ────────────────────────────────────────────────────────────


def string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def integer(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


def number(description: str) -> dict[str, Any]:
    return {"type": "number", "description": description}


def boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def object_schema(properties: dict[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": dict(properties)}
    required = list(required)
    if required:
        schema["required"] = required
    return schema


def router_tool_list(items: Iterable[ToolSpec]) -> str:
    """Đoạn liệt kê tool cho prompt router JSON (chat và JSON-router của agent):
    mỗi tool 1 dòng gọn ``- "tên": mô tả. args {...}. khi nào dùng`` (kiểu EasyTool)."""
    lines = []
    for spec in items:
        if spec.router_hint:
            lines.append(f'- "{spec.name}": {spec.description} {spec.router_hint}'.rstrip())
            continue
        props = (spec.parameters or {}).get("properties", {}) or {}
        args_desc = ", ".join(
            f'"{key}": {prop.get("description") or prop.get("type", "any")}'
            for key, prop in props.items()
        )
        lines.append(f'- "{spec.name}": {spec.description} args {{{args_desc}}}')
    return "\n".join(lines)


def catalog_chars(items: Iterable[ToolSpec]) -> int:
    """Tổng ký tự mô tả tool gửi model (dòng router + JSON tools OpenAI) - đo/đặt
    trần để prompt router và /agent vừa TPM free của Groq."""
    specs_list = list(items)
    return len(router_tool_list(specs_list)) + len(
        json.dumps(openai_tools(specs_list), ensure_ascii=False)
    )


def openai_tools(items: Iterable[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in items
    ]


def gemini_declarations(items: Iterable[ToolSpec]):
    from google.genai import types

    declarations = []
    for spec in items:
        # Gemini từ chối schema OBJECT có "properties" rỗng: tool không tham số thì bỏ hẳn.
        if (spec.parameters or {}).get("properties"):
            declarations.append(
                types.FunctionDeclaration(
                    name=spec.name, description=spec.description, parameters=spec.parameters
                )
            )
        else:
            declarations.append(
                types.FunctionDeclaration(name=spec.name, description=spec.description)
            )
    return [types.Tool(function_declarations=declarations)]
