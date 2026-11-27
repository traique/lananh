"""Hỏi tự nhiên thay cho lệnh: người dùng nói tự nhiên, bot tự chạy đúng lệnh.

MỘT cơ chế, tái dùng danh bạ ToolSpec (services/tool_registry.py) và router JSON
của chat (services/tools.py) - không dựng router thứ hai:

1. Đường code (rẻ, không AI) - ``maybe_handle`` là 1 cổng trong chuỗi trả lời nhanh
   dùng chung mọi kênh (services/quick_reply.py):
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

Bố cục gói (tách từ 1 file 1.700 dòng, thứ tự import trong ``__init__`` = thứ tự đăng ký
ToolSpec như cũ - router liệt kê tool theo thứ tự này, mẫu câu khớp đầu tiên thắng):

- ``turn``: ngữ cảnh lượt (kênh, quyền), trạng thái "đang chờ bổ sung tham số".
- ``runner``: ``run_line`` - chạy 1 dòng lệnh / qua đúng dispatcher của kênh chat.
- ``registry``: khai báo ToolSpec "chạy lệnh" (``Command``, ``_cmd``), ``_run_command``.
- ``builders_huyenhoc``, ``builders_personal``, ``builders_admin``, ``builders_settings``:
  các lệnh theo lĩnh vực (huyền học + lịch; tiện ích, cá nhân, sổ sách; quản trị + nhóm
  Zalo; bật/tắt cài đặt).
- ``suggest``: ``suggest_command``/``clarify_command``; ``router``: chọn nhóm cho prompt
  router, gom kết quả cho lượt chat; ``codepath``: ``maybe_handle``; ``help``: trợ giúp.

Tên được test thay bằng monkeypatch (``services.nl_commands.run_line``) tra qua gói lúc
gọi nên vẫn có hiệu lực.
"""

from __future__ import annotations

# Thứ tự import dưới đây là thứ tự đăng ký ToolSpec - không đảo.
from services.nl_commands.turn import (
    PENDING_TTL_SEC,
    clear_pending,
    enabled,
    llm_enabled,
    note_turn,
    pending,
    role_for,
    turn_scope,
)
from services.nl_commands.turn import _pending as _pending
from services.nl_commands.runner import CommandRun, Runner, run_line
from services.nl_commands.registry import COMMANDS, Ask, Builder, Command, command_names
from services.nl_commands import (  # noqa: F401 - import để đăng ký ToolSpec, đúng thứ tự
    builders_huyenhoc,
    builders_personal,
    builders_admin,
    builders_settings,
)
from services.nl_commands.suggest import CLARIFY_SPEC, SUGGEST_SPEC
from services.nl_commands.router import (
    MAX_GROUPS,
    ROUTER_RULE,
    outcome_messages,
    records_itself,
    router_groups,
    router_specs,
)
from services.nl_commands.codepath import how_to_text, maybe_handle
from services.nl_commands.codepath import _GENDER_WORD_RE as _GENDER_WORD_RE
from services.nl_commands.codepath import _core as _core
from services.nl_commands.help import help_reply, short_help, topic_help

__all__ = [
    "CLARIFY_SPEC",
    "COMMANDS",
    "MAX_GROUPS",
    "PENDING_TTL_SEC",
    "ROUTER_RULE",
    "SUGGEST_SPEC",
    "Ask",
    "Builder",
    "Command",
    "CommandRun",
    "Runner",
    "clear_pending",
    "command_names",
    "enabled",
    "help_reply",
    "how_to_text",
    "llm_enabled",
    "maybe_handle",
    "note_turn",
    "outcome_messages",
    "pending",
    "records_itself",
    "role_for",
    "router_groups",
    "router_specs",
    "run_line",
    "short_help",
    "topic_help",
    "turn_scope",
]
