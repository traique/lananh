"""Hỏi tự nhiên thay cho lệnh: người dùng nói tự nhiên, bot tự chạy đúng lệnh.

MỘT cơ chế, tái dùng danh bạ ToolSpec (services/tool_registry.py) và router JSON
của chat (services/tools.py) - không dựng router thứ hai:

1. Đường code (rẻ, không AI) - ``maybe_handle`` chạy trong chuỗi trả lời nhanh
   (Telegram handlers/chat_router._quick_reply, Zalo/Zoom
   channel_chat_service.handle_channel_text):
   - "em làm được gì", "làm sao để dò vé số", "có lệnh nào xem vàng không" -> trả
     lời từ danh bạ lệnh (services/command_catalog.py) theo vai trò;
   - mẫu câu rõ ràng: tử vi/thần số học/kỳ môn có ngày giờ, "ngày tốt tháng này",
     "xem trí nhớ", "bật giọng nói"... -> chạy luôn ToolSpec tương ứng;
   - đang chờ bổ sung tham số ("xem tử vi" -> em hỏi ngày giờ sinh -> người dùng
     trả lời "15/3/1990 9h30 nam") -> chạy tiếp.
2. Đường router (1 lượt LLM, NL_COMMANDS_LLM=1): câu có dấu hiệu là yêu cầu chức
   năng (từ khoá theo NHÓM, ``router_groups``) thì ToolSpec "chạy lệnh" của nhóm
   đó được thêm vào prompt router chat (chỉ nhóm khớp -> giữ trần token); router
   chọn tool -> ``tools.execute`` -> kết quả ``verbatim``: lượt chat gửi NGUYÊN VĂN
   kết quả lệnh, không gọi AI viết lại (lệnh vốn có AI như *_chitiet vẫn gọi AI
   như khi gõ lệnh).

Loại ToolSpec (mọi cái đều đi qua ``run_line`` = đúng dispatcher lệnh /):
- ``cmd_*``: tra cứu/đọc (side_effect=False). Thiếu tham số bắt buộc -> hỏi lại 1
  câu (STATUS_INVALID, nhớ "đang chờ" 10 phút), không đoán.
- ``set_*``: bật/tắt cài đặt (side_effect=True: lượt chỉ đọc/tin thoại, lượt đã
  đọc nội dung web bị chặn như mọi tool ghi). Câu PHẢI nói rõ bật/tắt (code kiểm
  lại trên chữ người dùng, không tin router); câu trả lời mở đầu bằng dòng nhắc
  lại lệnh đã chạy ("↪️ Em làm theo /noi on").
- ``suggest_command``: lệnh quản trị/nguy hiểm/xoá (fb_*, zalo*, /forget,
  /xoadulieu, /reset, /router9, /model...) KHÔNG BAO GIỜ tự chạy - bot chỉ ra đúng
  lệnh cần gõ, tham số điền sẵn.
- ``clarify_command``: router không chắc chức năng nào -> hỏi lại 1 câu ngắn.

Quyền: ToolSpec có ``roles`` như mọi tool; danh bạ lệnh lọc theo vai trò và kênh;
thành viên Zalo không thấy/không gọi được lệnh admin (gợi ý lệnh admin cũng
không lộ - router chọn thì coi như "none"). Dữ liệu luôn theo user_id của lượt.
Một lượt chạy tối đa 1 lệnh (router chọn tối đa 1 tool).
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any

from core import config
from core.envutil import env_on
from core.text_normalize import fold, nfc
from services import command_catalog as catalog
from services import tool_registry as reg

logger = logging.getLogger(__name__)

PENDING_TTL_SEC = 600
_PENDING_MAX = 500
# Tối đa số nhóm lệnh đưa vào 1 prompt router (giữ trần token Groq free).
MAX_GROUPS = 2
_SUGGEST_MAX = 6
_ARG_MAX = 200


def enabled() -> bool:
    """NL_COMMANDS_ENABLED=0: tắt hẳn (chỉ còn lệnh / và các đường tự nhiên cũ)."""
    return env_on("NL_COMMANDS_ENABLED")


def llm_enabled() -> bool:
    """NL_COMMANDS_LLM=0: chỉ đường code, không thêm lệnh vào router LLM."""
    return enabled() and env_on("NL_COMMANDS_LLM")


# ─── Ngữ cảnh lượt ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Turn:
    channel: str = "telegram"
    is_admin: bool = False


_turn: ContextVar[_Turn | None] = ContextVar("nl_command_turn", default=None)


@contextmanager
def turn_scope(channel: str, is_admin: bool) -> Iterator[None]:
    """Kênh + quyền admin Zalo của lượt cho handler ToolSpec "chạy lệnh"."""
    token = _turn.set(_Turn(channel, bool(is_admin)))
    try:
        yield
    finally:
        _turn.reset(token)


def _ctx() -> _Turn:
    return _turn.get() or _Turn()


def role_for(user_id: int, is_admin: bool) -> str:
    if config.is_owner_user_id(user_id):
        return reg.ROLE_OWNER
    return reg.ROLE_ADMIN if is_admin else reg.ROLE_MEMBER


def _forms(user_id: int) -> tuple[str, str]:
    if config.is_owner_user_id(user_id):
        return "anh", "em"
    from services import reply_audience

    return reply_audience.forms()


def _turn_text() -> str:
    from services import tools

    return tools._turn_text.get()


# ─── Đang chờ bổ sung tham số ───────────────────────────────────────────────


@dataclass
class _Pending:
    spec: str
    expires: float
    detail: bool = False


_pending: OrderedDict[int, _Pending] = OrderedDict()


def _set_pending(user_id: int, spec: str, *, detail: bool = False) -> None:
    _pending[user_id] = _Pending(spec, time.monotonic() + PENDING_TTL_SEC, detail)
    _pending.move_to_end(user_id)
    while len(_pending) > _PENDING_MAX:
        _pending.popitem(last=False)


def pending(user_id: int) -> _Pending | None:
    item = _pending.get(user_id)
    if item is None:
        return None
    if item.expires < time.monotonic():
        _pending.pop(user_id, None)
        return None
    return item


def clear_pending(user_id: int) -> None:
    _pending.pop(user_id, None)


def note_turn(user_id: int, outcome: reg.ToolOutcome) -> None:
    """Sau 1 lượt chat: không phải câu bổ sung cho lệnh đang chờ thì bỏ trạng thái chờ."""
    if outcome.status == reg.STATUS_INVALID and outcome.verbatim:
        return
    clear_pending(user_id)


# ─── Chạy 1 dòng lệnh qua đúng dispatcher của lệnh / ───────────────────────


@dataclass(frozen=True)
class CommandRun:
    messages: tuple[str, ...] = ()
    provider: str | None = None
    image_b64: str | None = None
    image_url: str | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(m for m in self.messages if m)


_PORTFOLIO_COMMANDS = frozenset({"/danhmuc", "/lichsucp", "/laicp", "/theodoi"})
_GROUP_COMMANDS = frozenset({"/nhom", "/tongket", "/dangnoi"})


async def _zalo_account_id() -> str | None:
    from channels import zalo_repository, zalo_session

    return (
        await zalo_session.load_account_id() or await zalo_repository.resolve_default_account_id()
    )


async def run_line(user_id: int, line: str, *, channel: str, is_admin: bool) -> CommandRun:
    """Chạy ``line`` (vd "/tuvi 15/3/1990 9h30 nam") y như người dùng gõ lệnh trên
    Zalo/Zoom - cùng hàm service, cùng kiểm quyền; Telegram dùng cùng dispatcher
    (chủ bot = admin)."""
    is_admin = is_admin or config.is_owner_user_id(user_id)
    command = line.split(maxsplit=1)[0].lower()
    if command in _PORTFOLIO_COMMANDS:
        from services import portfolio_service

        text = await portfolio_service.handle_command(user_id, line)
        return CommandRun((text,) if text else ())
    if command in _GROUP_COMMANDS:
        if not is_admin:
            return CommandRun(("Lệnh này chỉ dành cho admin.",))
        from channels import group_commands

        account_id = await _zalo_account_id()
        if not account_id:
            return CommandRun(
                ("Chưa xác định được tài khoản Zalo đang đăng nhập nên chưa xem được nhóm.",)
            )
        result = await group_commands.maybe_handle_group_command(str(account_id), line)
        return CommandRun(tuple(result.messages) if result is not None else ())
    from services import channel_command_service as ccs

    result = await ccs.maybe_handle_command(user_id, line, is_admin, channel)
    if result is None:
        return CommandRun()
    messages, provider = result
    return CommandRun(
        tuple(messages), provider, ccs.take_pending_image(), ccs.take_pending_image_url()
    )


# ─── ToolSpec "chạy lệnh" ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Ask:
    """Thiếu tham số: câu hỏi lại ({you}/{me} thay theo người nhận)."""

    text: str
    detail: bool = False


Builder = Callable[[dict[str, Any]], "str | Ask"]


@dataclass(frozen=True)
class Command:
    spec: reg.ToolSpec
    entry: str  # tên lệnh trong command_catalog (nhãn cho câu hỏi lại, trợ giúp)
    label: str  # tên việc ngắn cho câu hỏi lại ("tử vi", "thần số học")
    # Mẫu câu RÕ RÀNG (đã bỏ dấu, đã bỏ "em ơi"/"nhé"...) chạy thẳng không cần router.
    patterns: tuple[re.Pattern[str], ...] = ()
    pattern_args: dict[str, Any] = field(default_factory=dict)


COMMANDS: dict[str, Command] = {}

# Nhóm lệnh -> từ khoá (bỏ dấu, theo ranh giới từ) để chọn nhóm vào prompt router.
# Viết thận trọng: tin chat phiếm ("vâng hôm nay em bận", "em nói về số liệu") không
# được khớp - test_nl_commands có bộ câu chat thường phải ra rỗng.
_GROUP_RE: dict[str, re.Pattern[str]] = {
    "huyen_hoc": re.compile(r"\b(?:tu vi|la so|than so hoc|so chu dao|ky mon|kymon)\b"),
    "lich": re.compile(
        r"\b(?:ngay tot|ngay xau|ngay dep|(?:ngay|gio) hoang dao|xem ngay|sao han|han nam|"
        r"tam tai|kim lau|hoang oc|ngay le|xuat hanh|dong tho|khai truong|nhap trach|"
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


def _register(command: Command) -> Command:
    COMMANDS[command.spec.name] = command
    reg.register(command.spec)
    return command


def _schema(props: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return reg.object_schema(props or {}, required)


def _clean(value: Any, limit: int = _ARG_MAX) -> str:
    """Tham số router -> 1 dòng an toàn (bỏ xuống dòng, bỏ "/" đầu, cắt độ dài)."""
    text = " ".join(nfc(str(value or "")).split()).lstrip("/")
    return text[:limit].strip()


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "co", "có"}


_DETAIL_RE = re.compile(r"\b(?:luan giai|chi tiet|giai thich|y nghia|phan tich|giai nghia)\b")


def _wants_detail(args: dict[str, Any]) -> bool:
    return _truthy(args.get("detail")) or bool(_DETAIL_RE.search(fold(_turn_text())))


def _make_handler(name: str, build: Builder, *, heavy: Callable[[dict], bool] | None = None):
    async def handler(user_id: int, **args: Any) -> reg.ToolResult:
        built = build(args)
        you, me = _forms(user_id)
        if isinstance(built, Ask):
            _set_pending(user_id, name, detail=built.detail)
            text = built.text.format(you=you, me=me, You=you.capitalize(), Me=me.capitalize())
            return reg.ToolResult(text, status=reg.STATUS_INVALID, extra={"messages": [text]})
        return await _run_command(user_id, built, heavy=bool(heavy and heavy(args)))

    handler.__name__ = f"_nl_{name}"
    return handler


async def _run_command(
    user_id: int, line: str, *, heavy: bool = False, echo: str = ""
) -> reg.ToolResult:
    ctx = _ctx()
    clear_pending(user_id)
    if heavy:
        from services import usage_quota

        if not await usage_quota.heavy_available(
            user_id, channel=ctx.channel, is_admin=ctx.is_admin
        ):
            import messages as bot_messages
            from services import reply_audience

            caps = await usage_quota.caps()
            text = bot_messages.member_quota_notice(
                "heavy", reply_audience.current().address_form, caps.heavy
            )
            return reg.ToolResult(text, extra={"messages": [text], "command": line})
    run = await run_line(user_id, line, channel=ctx.channel, is_admin=ctx.is_admin)
    outputs = [m for m in run.messages if m]
    if echo:
        outputs = [f"{echo}\n{outputs[0]}" if outputs else echo, *outputs[1:]]
    if not outputs and not (run.image_b64 or run.image_url):
        you, me = _forms(user_id)
        outputs = [
            f"{me.capitalize()} chưa chạy được {line} lúc này, {you} gõ thử lệnh giúp {me} nha."
        ]
    logger.info("Lệnh từ câu tự nhiên: %s (user_id=%s)", line.split()[0], user_id)
    return reg.ToolResult(
        "\n\n".join(outputs) or line,
        extra={
            "messages": outputs,
            "image_b64": run.image_b64,
            "image_url": run.image_url,
            "command": line,
        },
    )


def _cmd(
    name: str,
    entry: str,
    label: str,
    group: str,
    description: str,
    hint: str,
    props: dict | None,
    build: Builder,
    *,
    roles: frozenset[str] = reg.ALL_ROLES,
    required: tuple[str, ...] = (),
    heavy: Callable[[dict], bool] | None = None,
    patterns: tuple[str, ...] = (),
    pattern_args: dict | None = None,
) -> Command:
    spec = reg.ToolSpec(
        name=name,
        description=description,
        parameters=_schema(props, required),
        handler=_make_handler(name, build, heavy=heavy),
        roles=roles,
        surfaces=frozenset({reg.COMMAND}),
        router_hint=hint,
        verbatim=True,
        group=group,
    )
    return _register(
        Command(
            spec,
            entry,
            label,
            tuple(re.compile(p) for p in patterns),
            dict(pattern_args or {}),
        )
    )


# ── Huyền học ──
_DATE_RE = re.compile(r"\b(\d{1,2})\s*[/.-]\s*(\d{1,2})n?\s*[/.-]\s*(\d{4})\b")


def _date_text(value: Any) -> str:
    """Ngày sinh "1990-03-15" | "15/3/1990" | câu có ngày -> "15/3/1990"; không có -> ""."""
    raw = _clean(value, 300)
    iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if iso:
        return f"{int(iso.group(3))}/{int(iso.group(2))}/{iso.group(1)}"
    m = _DATE_RE.search(raw)
    return f"{int(m.group(1))}/{int(m.group(2))}/{m.group(3)}" if m else ""


_TUVI_ASK = (
    "{You} cho {me} xin ngày sinh, giờ sinh và giới tính để {me} an lá số nha, "
    "vd: 15/3/1990 9h30 nam (ngày âm thì ghi thêm chữ “âm”)."
)


def _build_tuvi(args: dict[str, Any]) -> str | Ask:
    detail = _wants_detail(args)
    date, time_, gender = _date_text(args.get("date")), _clean(args.get("time"), 30), ""
    gender_raw = fold(_clean(args.get("gender"), 10))
    if gender_raw in {"nam", "male", "trai"}:
        gender = "nam"
    elif gender_raw in {"nu", "female", "gai"}:
        gender = "nữ"
    missing = [
        label
        for label, value in (("ngày sinh", date), ("giờ sinh", time_), ("giới tính", gender))
        if not value
    ]
    if missing:
        return Ask(_TUVI_ASK, detail=detail)
    lunar = "âm " if _truthy(args.get("lunar")) else ""
    command = "/tuvi_chitiet" if detail else "/tuvi"
    return f"{command} {lunar}{date} {time_} {gender}"


_TSH_ASK = "{You} cho {me} xin ngày sinh dương lịch (vd 15/3/1990), có thể kèm họ tên nha."


def _build_tsh(args: dict[str, Any]) -> str | Ask:
    detail = _wants_detail(args)
    date = _date_text(args.get("date"))
    if not date:
        return Ask(_TSH_ASK, detail=detail)
    name = _clean(args.get("name"), 60)
    command = "/tsh_chitiet" if detail else "/tsh"
    return f"{command} {date} {name}".strip()


_KYMON_ASK = (
    "{You} muốn {me} luận giải việc gì trên bàn Kỳ Môn? Vd: “9h sáng mai ký hợp đồng có "
    "thuận không”."
)


def _build_kymon(args: dict[str, Any]) -> str | Ask:
    when = _clean(args.get("when"), 40)
    question = _clean(args.get("question"), 300)
    if _truthy(args.get("detail")) or question:
        if not question:
            return Ask(_KYMON_ASK, detail=True)
        return f"/kymon_chitiet {when} {question}".replace("  ", " ").strip()
    return f"/kymon {when}".strip()


def _ai_detail(args: dict[str, Any]) -> bool:
    return _wants_detail(args)


_cmd(
    "cmd_tuvi",
    "tuvi",
    "tử vi",
    "huyen_hoc",
    "An lá số tử vi (code tính).",
    '{"date": ngày sinh, "time": giờ sinh, "gender": nam|nữ, "lunar": ngày âm?, "detail": '
    "muốn AI luận giải}. Thiếu thì để rỗng (hệ thống hỏi lại), KHÔNG đoán.",
    {
        "date": reg.string("ngày sinh dd/mm/yyyy"),
        "time": reg.string("giờ sinh"),
        "gender": reg.string("nam|nữ"),
        "lunar": reg.boolean("ngày âm"),
        "detail": reg.boolean("AI luận giải"),
    },
    _build_tuvi,
    heavy=_ai_detail,
)
_cmd(
    "cmd_tsh",
    "tsh",
    "thần số học",
    "huyen_hoc",
    "Thần số học theo ngày sinh.",
    '{"date": ngày sinh dương, "name": họ tên nếu có, "detail": muốn AI diễn giải}. Thiếu '
    "ngày sinh để rỗng.",
    {
        "date": reg.string("dd/mm/yyyy"),
        "name": reg.string("họ tên"),
        "detail": reg.boolean("AI diễn giải"),
    },
    _build_tsh,
    heavy=_ai_detail,
)
_cmd(
    "cmd_kymon",
    "kymon",
    "kỳ môn",
    "huyen_hoc",
    "Lập bàn Kỳ Môn Độn Giáp.",
    '{"when": thời điểm nguyên văn ("" = lúc này), "question": việc cần xem nếu muốn luận '
    'giải}. Vd "kỳ môn 9h mai ký hợp đồng có thuận không".',
    {
        "when": reg.string("thời điểm"),
        "question": reg.string("việc cần xem"),
        "detail": reg.boolean("AI luận giải"),
    },
    _build_kymon,
    heavy=lambda args: bool(_clean(args.get("question"))),
)


def _build_lich(args: dict[str, Any]) -> str | Ask:
    query = _clean(args.get("query"), 120) or _clean(_turn_text(), 120)
    return f"/lich {query}".strip()


_cmd(
    "cmd_lich",
    "lich",
    "tra lịch",
    "lich",
    "Tra lịch: ngày tốt/xấu, ngày lễ, tuổi, sao hạn, lịch tháng.",
    '{"query": câu hỏi gọn, vd "ngày tốt tháng 11", "sao hạn 1990 nam năm 2027"}.',
    {"query": reg.string("câu hỏi lịch")},
    _build_lich,
)

# ── Tiện ích ──


def _is_switch(word: str) -> bool:
    """Từ "on"/"off"/"bật"/"tắt": tham số này biến lệnh tra cứu thành lệnh BẬT/TẮT."""
    from core.text_utils import parse_on_off

    return bool(word) and parse_on_off(word) is not None


def _need(field_name: str, ask: str, command: str, limit: int = _ARG_MAX) -> Builder:
    def build(args: dict[str, Any]) -> str | Ask:
        value = _clean(args.get(field_name), limit)
        if _is_switch(value):
            # "/anh on" là bật/tắt tạo ảnh toàn bot - không bao giờ chạy từ câu tự nhiên.
            return Ask(f"Bật/tắt thì {{you}} gõ {command} on|off giúp {{me}} nha.")
        return f"{command} {value}" if value else Ask(ask)

    return build


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


def _fixed(line: str) -> Builder:
    return lambda args: line


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

# ── Cài đặt (bật/tắt): câu phải nói rõ ──
# Kiểm trên chữ CÒN DẤU: bỏ dấu thì "dừng"/"dùng"/"đúng" cùng là "dung", "bỏ"/"bò"/
# "bố" cùng là "bo" - "anh muốn dùng giọng nói" không được thành TẮT giọng nói.
_ON_RE = re.compile(r"\b(?:bật|mở|on|kích hoạt)\b")
_OFF_RE = re.compile(r"\b(?:tắt|ngừng|dừng|off|thôi|khỏi|bỏ)\b")
_ON_PLAIN_RE = re.compile(r"\b(?:bat|on)\b")
_OFF_PLAIN_RE = re.compile(r"\b(?:tat|ngung|off)\b")


def _explicit_mode(text: str) -> str:
    """Trả "on"/"off" theo chữ người dùng (không tin router); không rõ/cả hai -> ""."""
    low = nfc(text or "").lower()
    plain = fold(low) == low
    on_re, off_re = (_ON_PLAIN_RE, _OFF_PLAIN_RE) if plain else (_ON_RE, _OFF_RE)
    on, off = bool(on_re.search(low)), bool(off_re.search(low))
    return "on" if on and not off else "off" if off and not on else ""


_SETTING_ECHO = "↪️ {Me} làm theo lệnh {line}"


def _setting(
    name: str,
    entry: str,
    label: str,
    description: str,
    hint: str,
    props: dict,
    build: Callable[[dict[str, Any], str], str | None],
    *,
    roles: frozenset[str] = reg.ALL_ROLES,
    patterns: tuple[str, ...] = (),
) -> Command:
    async def handler(user_id: int, **args: Any) -> reg.ToolResult:
        you, me = _forms(user_id)
        line = build(args, _explicit_mode(_turn_text()))
        if not line:
            text = f"{you.capitalize()} muốn {me} BẬT hay TẮT {label} ạ?"
            return reg.ToolResult(text, status=reg.STATUS_INVALID, extra={"messages": [text]})
        echo = _SETTING_ECHO.format(Me=me.capitalize(), line=line)
        result = await _run_command(user_id, line, echo=echo)
        return replace(result, confirmation=line)

    handler.__name__ = f"_nl_{name}"
    spec = reg.ToolSpec(
        name=name,
        description=description,
        parameters=_schema(props),
        handler=handler,
        side_effect=True,
        roles=roles,
        surfaces=frozenset({reg.COMMAND}),
        router_hint=hint,
        effect_label=f"cài đặt {label}",
        verbatim=True,
        group="cai_dat",
    )
    return _register(
        Command(spec, entry, label, tuple(re.compile(p) for p in patterns), {"_setting": True})
    )


def _on_off(command: str, *, auto: bool = False):
    def build(args: dict[str, Any], mode: str) -> str | None:
        if auto and fold(_clean(args.get("mode"), 10)) == "auto" and "auto" in fold(_turn_text()):
            return f"{command} auto"
        return f"{command} {mode}" if mode else None

    return build


_MODE = {"mode": reg.string("on|off")}
_setting(
    "set_noi",
    "noi",
    "trả lời kèm giọng nói",
    "Bật/tắt trả lời kèm giọng nói.",
    '{"mode": on|off|auto}. Chỉ khi nói RÕ bật/tắt.',
    {"mode": reg.string("on|off|auto")},
    _on_off("/noi", auto=True),
    patterns=(r"(?:bat|tat|mo|ngung) (?:che do )?(?:tra loi )?(?:bang |kem )?giong noi",),
)


_CHANGE_RE = re.compile(r"\b(?:đổi|chuyển|đặt|sửa|cài)\b")
_CHANGE_PLAIN_RE = re.compile(r"\b(?:doi|chuyen|dat|sua|cai)\b")


def _wants_change(text: str) -> bool:
    """Câu nói rõ đổi/đặt giờ ("đổi bản tin sang 7h") - kiểm trên chữ còn dấu."""
    low = nfc(text or "").lower()
    pattern = _CHANGE_PLAIN_RE if fold(low) == low else _CHANGE_RE
    return bool(pattern.search(low))


def _build_yenlang(args: dict[str, Any], mode: str) -> str | None:
    text = fold(_turn_text())
    span = re.search(r"\b(\d{1,2})(?:h|g|gio)?\s*(?:-|den|toi)\s*(\d{1,2})(?:h|g|gio)?\b", text)
    if span and (mode == "on" or (not mode and _wants_change(_turn_text()))):
        return f"/yenlang {int(span.group(1))}-{int(span.group(2))}"
    return f"/yenlang {mode}" if mode else None


_setting(
    "set_yenlang",
    "yenlang",
    "giờ yên lặng",
    "Bật/tắt/đổi giờ yên lặng (gom thông báo ban đêm).",
    '{"mode": on|off|"22-7"}. Chỉ khi nói RÕ.',
    {"mode": reg.string("on|off|22-7")},
    _build_yenlang,
    patterns=(r"(?:bat|tat|mo|ngung) (?:che do )?gio yen lang(?: .*)?",),
)


def _build_bantin(args: dict[str, Any], mode: str) -> str | None:
    text = fold(_turn_text())
    hour = re.search(r"\b(\d{1,2})\s*(?:h|g|gio)\b", text)
    if hour and (mode == "on" or (not mode and _wants_change(_turn_text()))):
        return f"/bantin {int(hour.group(1))}"
    return f"/bantin {mode}" if mode else None


_setting(
    "set_bantin",
    "bantin",
    "bản tin sáng",
    "Bật/tắt/đổi giờ bản tin sáng cá nhân.",
    '{"mode": on|off|giờ}. Chỉ khi nói RÕ.',
    _MODE,
    _build_bantin,
)
_setting(
    "set_hoithan",
    "hoithan",
    "hỏi thăm chủ động",
    "Bật/tắt em chủ động hỏi thăm việc người dùng kể.",
    '{"mode": on|off}. Chỉ khi nói RÕ.',
    _MODE,
    _on_off("/hoithan"),
    patterns=(r"(?:bat|tat|mo|ngung) (?:che do )?hoi (?:than|tham)(?: chu dong)?",),
)
_setting(
    "set_memory",
    "memory",
    "trí nhớ dài hạn",
    "Bật/tắt trí nhớ dài hạn.",
    '{"mode": on|off}. Chỉ khi nói RÕ bật/tắt TRÍ NHỚ.',
    _MODE,
    _on_off("/memory"),
    patterns=(r"(?:bat|tat|mo|ngung) tri nho(?: dai han)?",),
)
_setting(
    "set_tavily",
    "tavily",
    "tra web Tavily",
    "Bật/tắt tra web Tavily.",
    '{"mode": on|off}. Chỉ khi nói RÕ.',
    _MODE,
    _on_off("/tavily"),
    roles=reg.STAFF_ROLES,
)

# ── Gợi ý lệnh (không bao giờ tự chạy) + hỏi lại ──


async def _suggest_handler(user_id: int, command: str = "", args: str = "", **_ignored):
    ctx = _ctx()
    entry = catalog.get(command)
    role = role_for(user_id, ctx.is_admin)
    if entry is None or not entry.suggest_only or role not in entry.allowed_roles:
        # Không lộ lệnh người hỏi không được dùng: coi như không có tool.
        return reg.ToolResult("", status=reg.STATUS_NONE)
    return reg.ToolResult(*_suggest_text(user_id, entry, _clean(args, 120), ctx.channel))


def _suggest_text(user_id: int, entry: catalog.Entry, args: str, channel: str):
    you, me = _forms(user_id)
    line = f"/{entry.name} {args}".strip()
    where = " (trên Telegram)" if entry.where == "tg" and channel != "telegram" else ""
    text = (
        f"🔐 Việc này {me} không tự làm từ tin nhắn được - {you} tự gõ lệnh để xác nhận "
        f"nha{where}:\n{line}\n({entry.summary})"
    )
    return text, reg.STATUS_OK, "", "", {"messages": [text], "command": line, "suggest": True}


SUGGEST_SPEC = reg.register(
    reg.ToolSpec(
        name="suggest_command",
        description="Chỉ ra lệnh / cần gõ cho việc quản trị/xoá/nguy hiểm (không tự chạy).",
        parameters=_schema({"command": reg.string("tên lệnh"), "args": reg.string("tham số")}),
        handler=_suggest_handler,
        surfaces=frozenset({reg.COMMAND}),
        verbatim=True,
        group="suggest",
    )
)


async def _clarify_handler(user_id: int, tools: Any = None, **_ignored) -> reg.ToolResult:
    you, me = _forms(user_id)
    names = tools if isinstance(tools, list) else []
    labels = [COMMANDS[n].label for n in names if isinstance(n, str) and n in COMMANDS][:3]
    if len(labels) >= 2:
        options = ", ".join(labels[:-1]) + f" hay {labels[-1]}"
        text = f"{me.capitalize()} chưa chắc {you} muốn xem {options}? {you.capitalize()} nói rõ giúp {me} nha."
    else:
        text = (
            f"{me.capitalize()} chưa rõ {you} muốn dùng chức năng nào - {you} nói cụ thể hơn "
            f"giúp {me} nha (hoặc gõ /help để xem gợi ý)."
        )
    return reg.ToolResult(text, status=reg.STATUS_INVALID, extra={"messages": [text]})


CLARIFY_SPEC = reg.register(
    reg.ToolSpec(
        name="clarify_command",
        description="Hỏi lại 1 câu khi rõ là cần 1 chức năng ở trên nhưng không chắc cái nào.",
        parameters=_schema({"tools": {"type": "array", "items": {"type": "string"}}}),
        handler=_clarify_handler,
        surfaces=frozenset({reg.COMMAND}),
        router_hint='args {"tools": [2-3 tên tool ứng viên]}. Chỉ khi thật sự mơ hồ.',
        verbatim=True,
        group="clarify",
    )
)


# ─── Chọn nhóm cho prompt router ────────────────────────────────────────────


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


# ─── Đường code (không AI) ──────────────────────────────────────────────────

_PREFIX_RE = re.compile(
    r"^(?:(?:em|lan anh|bot|ban) oi|oi (?:em|lan anh)|nay em|em|cho (?:anh|chi|em|minh|toi) hoi|"
    r"(?:anh|chi|minh|toi) muon|(?:giup|gium|ho) (?:anh|chi|minh|toi))[\s,]+"
)
_SUFFIX_RE = re.compile(
    r"(?:[\s,]+(?:di|nhe|nha|nhen|voi|em|a|ha|duoc khong|dc khong|giup (?:anh|chi|minh|toi)"
    r"|cho (?:anh|chi|minh|toi)|gium (?:anh|chi|minh|toi)|xem|thu|coi))+$"
)


def _core(text: str) -> str:
    low = fold(text).strip(" ?.!,")
    for _ in range(2):
        low = _PREFIX_RE.sub("", low).strip()
    return _SUFFIX_RE.sub("", low).strip(" ?.!,")


_CAPABILITY_RE = re.compile(
    r"\b(?:lam|giup) (?:duoc )?(?:nhung )?(?:gi|viec gi)\b|\bbiet lam (?:nhung )?gi\b|"
    r"\bco (?:nhung )?(?:tinh nang|chuc nang|lenh) (?:gi|nao)\b|"
    r"\b(?:huong dan|cach) (?:su dung|dung)(?: bot| em)?$|^(?:danh sach|xem) (?:cac )?lenh$"
)
_HOWTO_RE = (
    re.compile(r"^(?:lam (?:sao|the nao)|bang cach nao|cach nao) (?:de )?(?P<x>.+)$"),
    re.compile(r"^(?:co )?lenh (?:nao|gi) (?:de |dung de )?(?P<x>.+?)(?: khong| ko)?$"),
    re.compile(r"^muon (?P<x>.+?) thi (?:lam sao|lam the nao|go gi|go lenh gi|dung lenh gi)$"),
)


def _capability_question(core: str) -> bool:
    if len(core) > 60 or not _CAPABILITY_RE.search(core):
        return False
    # "em làm gì đấy" (hỏi đang làm gì) không phải hỏi tính năng.
    return bool(re.search(r"\b(?:duoc|dc|giup|biet|tinh nang|chuc nang|lenh|su dung|dung)\b", core))


def _howto(core: str, role: str, channel: str, user_id: int) -> str | None:
    if len(core) > 90:
        return None
    for pattern in _HOWTO_RE:
        match = pattern.match(core)
        if not match:
            continue
        found = catalog.search(match.group("x"), role, channel)
        if not found:
            if "lenh" in core:
                you, me = _forms(user_id)
                return (
                    f"{me.capitalize()} chưa có lệnh riêng cho việc này, {you} cứ hỏi thẳng {me} "
                    "bằng lời nha. Gõ /help để xem các việc em làm được."
                )
            return None
        return how_to_text(found, user_id)
    return None


def how_to_text(entries: list[catalog.Entry], user_id: int) -> str:
    you, me = _forms(user_id)
    lines = [f"{you.capitalize()} cứ nhắn tự nhiên cho {me} là được:"]
    for entry in entries:
        ask = f"“{entry.ask}”" if entry.ask and not entry.suggest_only else ""
        if ask:
            lines.append(f"• {ask} — hoặc gõ {entry.usage}: {entry.summary}")
        else:
            lines.append(f"• Gõ {entry.usage}: {entry.summary}")
    return "\n".join(lines)


# Lệnh nguy hiểm nhận ra bằng code: chỉ gợi ý lệnh, không bao giờ tự chạy.
_DANGER_PATTERNS = (
    (re.compile(r"(?:xoa|quen) (?:het|sach|toan bo) (?:tri nho|nhung gi em nho(?: ve .*)?)"), "forget"),
    (re.compile(r"xoa (?:het |sach |toan bo )?du lieu(?: ca nhan)?(?: cua (?:anh|chi|minh|toi))?"), "xoadulieu"),
    (re.compile(r"(?:xoa ngu canh|lam moi (?:cuoc )?(?:tro chuyen|hoi thoai)|reset (?:chat|hoi thoai))"), "reset"),
)  # fmt: skip

_TUVI_TRIGGER = re.compile(r"\b(?:tu vi|la so)\b")
_TSH_TRIGGER = re.compile(r"\bthan so hoc\b")
_KYMON_TRIGGER = re.compile(r"\bky mon\b")
_KNOWLEDGE_RE = re.compile(
    r"\b(?:la gi|nghia la|co dung khong|co tin|tai sao|vi sao|khac gi|nhu the nao|bao nhieu sao)\b"
)
_LICH_TRIGGER = re.compile(
    r"\b(?:ngay tot|ngay xau|ngay dep|ngay hoang dao|sao han|han nam|tam tai|kim lau|hoang oc|"
    r"ngay le|xuat hanh)\b"
)
# "xem tử vi", "lập lá số tử vi cho anh", "luận giải tử vi chi tiết": hỏi ngày giờ sinh.
_ASK_TUVI_RE = re.compile(
    r"(?:xem|lap|an|coi|tra|doc|luan giai|giai)?\s*(?:la so )?(?:tu vi|la so)(?: tu vi)?"
    r"(?: (?:chi tiet|cho (?:anh|chi|em|minh|toi)|giup|gium|ho))*"
)
_ASK_TSH_RE = re.compile(
    r"(?:xem|tinh|tra|coi|luan giai)?\s*than so hoc(?: (?:chi tiet|cho (?:anh|chi|em|minh|toi)|"
    r"giup|gium|ho))*"
)
_TSH_NAME_RE = re.compile(r"\bt[eê]n(?: l[aà])?\s*:?\s*([^\d,.;]{2,60})", re.IGNORECASE)


async def _run_spec(name: str, user_id: int, args: dict[str, Any]) -> CommandRun | None:
    from services import tools

    spec = reg.get(name)
    if spec is None:
        return None
    outcome = await tools.execute(spec, user_id, args)
    messages = outcome_messages(outcome)
    if not messages and not outcome.extra.get("image_b64") and not outcome.extra.get("image_url"):
        return None
    return CommandRun(
        tuple(messages),
        None,
        outcome.extra.get("image_b64"),
        outcome.extra.get("image_url"),
    )


async def _tuvi(user_id: int, text: str, low: str, waiting: _Pending | None) -> CommandRun | None:
    from services import tuvi

    trigger = bool(_TUVI_TRIGGER.search(low))
    follow = not trigger and waiting is not None and waiting.spec == "cmd_tuvi"
    if not (trigger or follow) or (trigger and _KNOWLEDGE_RE.search(low)):
        return None
    has_date = bool(_DATE_RE.search(text))
    if follow and not has_date:
        clear_pending(user_id)  # người dùng nói chuyện khác: thôi chờ
        return None
    detail = bool(_DETAIL_RE.search(low)) or bool(follow and waiting and waiting.detail)
    if not has_date:
        if not _ASK_TUVI_RE.fullmatch(_core(text)):
            return None  # câu khác có chữ "tử vi" mà không có ngày sinh: router/chat lo
        return await _run_spec("cmd_tuvi", user_id, {"detail": detail})
    parsed = tuvi.parse(text)
    if isinstance(parsed, str):
        you, me = _forms(user_id)
        _set_pending(user_id, "cmd_tuvi", detail=detail)
        first = parsed.splitlines()[0]
        return CommandRun(
            (f"{first} {you.capitalize()} ghi thêm giúp {me} nha, vd: 15/3/1990 9h30 nam.",)
        )
    command = "/tuvi_chitiet" if detail else "/tuvi"
    result = await _run_command(user_id, f"{command} {_clean(text, 160)}", heavy=detail)
    return CommandRun(tuple(result.extra.get("messages") or [result.text]))


async def _tsh(user_id: int, text: str, low: str, waiting: _Pending | None) -> CommandRun | None:
    trigger = bool(_TSH_TRIGGER.search(low))
    follow = not trigger and waiting is not None and waiting.spec == "cmd_tsh"
    if not (trigger or follow) or (trigger and _KNOWLEDGE_RE.search(low)):
        return None
    date = _date_text(text)
    if follow and not date:
        clear_pending(user_id)
        return None
    if not date and not _ASK_TSH_RE.fullmatch(_core(text)):
        return None
    name = _TSH_NAME_RE.search(nfc(text))
    args = {
        "date": date,
        "name": name.group(1).strip() if name else "",
        "detail": bool(_DETAIL_RE.search(low)) or bool(follow and waiting and waiting.detail),
    }
    return await _run_spec("cmd_tsh", user_id, args)


async def _kymon_or_lich(user_id: int, text: str, low: str) -> CommandRun | None:
    from services import calendar_query

    if _KNOWLEDGE_RE.search(low):
        return None
    if _KYMON_TRIGGER.search(low):
        core = _core(text)
        if re.fullmatch(
            r"(?:lap |xem )?(?:ban )?ky mon(?: (?:luc nay|bay gio|gio nay|hom nay))?", core
        ):
            return await _run_spec("cmd_kymon", user_id, {})
        return None  # có thời điểm/việc cần xem: router tách tham số
    if _LICH_TRIGGER.search(low) and len(low) <= 100:
        answer = calendar_query.answer(_core(text) or text)
        if not answer.startswith("Em chưa hiểu"):
            return CommandRun((answer,))
    return None


# "xem nhắc việc", "tháng này anh tiêu bao nhiêu": chạy thẳng tool đọc của chat (đúng văn
# bản /nhac, /chitieu) thay vì router AI chọn tool rồi chat AI viết lại (2 lượt AI).
_WHO = r"(?:(?:anh|chi|em|minh|toi|tui) )?"
_REMINDER_LIST_RE = re.compile(
    r"(?:xem |liet ke |kiem tra )?(?:(?:cac|nhung) )?(?:nhac viec|viec can nhac|loi nhac)"
    r"(?: (?:dang cho|sap toi))?(?: cua (?:anh|chi|minh|toi))?|"
    rf"{_WHO}(?:da )?dat (?:nhung |may )?(?:nhac|loi nhac) (?:gi|nao)(?: roi)?|"
    rf"{_WHO}co (?:nhung )?(?:nhac viec|loi nhac|viec can nhac) (?:gi|nao)(?: (?:khong|ko|chua))?"
)
_PERIOD = (
    r"(?:hom nay|hom qua|tuan nay|tuan truoc|tuan roi|thang nay|thang truoc|thang roi|"
    r"thang \d{1,2}(?:/\d{4})?|nam nay)"
)
_EXPENSE_SUMMARY_RE = re.compile(
    rf"{_PERIOD} {_WHO}(?:da )?(?:tieu|chi|xai|chi tieu|tieu xai)(?: het| mat)? "
    r"(?:bao nhieu|het bao nhieu)(?: tien)?(?: (?:roi|vay|the))?|"
    rf"(?:xem |tong )?(?:chi tieu|tong chi)(?: cua (?:anh|chi|minh|toi))? {_PERIOD}"
    r"(?: (?:bao nhieu|het bao nhieu|the nao|ra sao))?(?: (?:roi|vay))?"
)


async def _read_list(user_id: int, core: str, role: str) -> CommandRun | None:
    from services import tools

    if _REMINDER_LIST_RE.fullmatch(core):
        name, args = "list_reminders", {}
    elif _EXPENSE_SUMMARY_RE.fullmatch(core):
        name, args = "expense_summary", {"period": core}
    else:
        return None
    spec = reg.get(name)
    if spec is None or role not in spec.roles:
        return None
    outcome = await tools.execute(spec, user_id, args)
    if outcome.status != reg.STATUS_OK or not outcome.text:
        return None  # lỗi tra: để đường router/chat cũ lo
    return CommandRun((outcome.text,))


async def _structured(user_id: int, text: str, low: str) -> CommandRun | None:
    """Tử vi / thần số học / kỳ môn / tra lịch đọc được bằng code."""
    waiting = pending(user_id)
    for step in (
        lambda: _tuvi(user_id, text, low, waiting),
        lambda: _tsh(user_id, text, low, waiting),
        lambda: _kymon_or_lich(user_id, text, low),
    ):
        result = await step()
        if result is not None:
            return result
    return None


async def maybe_handle(
    user_id: int,
    text: str,
    *,
    channel: str,
    is_admin: bool = False,
    allow_side_effects: bool = True,
) -> CommandRun | None:
    """Đường code của "hỏi tự nhiên thay cho lệnh"; None = đi tiếp đường cũ."""
    text = nfc(text or "").strip()
    if not enabled() or not text or text.startswith("/") or len(text) > 300:
        return None
    role = role_for(user_id, is_admin)
    core = _core(text)
    low = fold(text)
    howto = _howto(core, role, channel, user_id)
    if howto is not None:
        return CommandRun((howto,))
    if _capability_question(core):
        you, _ = _forms(user_id)
        return CommandRun((short_help(role, channel, you),))
    for pattern, name in _DANGER_PATTERNS:
        if pattern.fullmatch(core):
            entry = catalog.get(name)
            if entry is not None and catalog.visible(entry, role, channel):
                return CommandRun((_suggest_text(user_id, entry, "", channel)[0],))
    from services import tools

    with turn_scope(channel, is_admin), tools.turn_text_scope(text):
        structured = await _structured(user_id, text, low) or await _read_list(user_id, core, role)
        if structured is not None:
            return structured
        for command in COMMANDS.values():
            if not command.patterns or role not in command.spec.roles:
                continue
            if command.spec.side_effect and not allow_side_effects:
                continue
            if any(p.fullmatch(core) for p in command.patterns):
                args = dict(command.pattern_args)
                args.pop("_setting", None)
                return await _run_spec(command.spec.name, user_id, args)
    return None


# ─── Trợ giúp ngắn theo vai trò ─────────────────────────────────────────────

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


# Các module khác import để biết có mấy ToolSpec "chạy lệnh" (test, docs).
def command_names() -> list[str]:
    return sorted(COMMANDS)


Runner = Callable[..., Awaitable[CommandRun]]
