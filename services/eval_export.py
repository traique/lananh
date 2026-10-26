"""/xuateval [Nd]: xuất câu trả lời bị 👎 thành case đánh giá ỨNG VIÊN cho golden set.

- Chỉ lấy dòng answer_feedback (migrations/021) kênh Telegram của CHÍNH chủ bot
  (ALLOWED_USER_ID) - không bao giờ lấy dữ liệu thành viên Zalo hay kênh khác.
- Kết quả là 1 file YAML cùng định dạng test/evals/*.yaml (xem test/evals/harness.py),
  gửi riêng cho chủ bot trên Telegram. Mỗi case có câu trả lời bị chê làm
  ``examples.bad`` và lý do/provider/model trong ``notes``; check chỉ có ``no_cjk``
  nên test tier A sẽ ĐỎ cho tới khi chủ bot thêm check bắt được lỗi đó - cố ý.
- Repo PUBLIC: trước khi chép case vào test/evals/ phải bỏ thông tin cá nhân (tên,
  số tài khoản, danh mục...) khỏi câu hỏi/câu trả lời.

RAM: tối đa MAX_CASES dòng, mỗi đoạn chữ cắt TEXT_MAX ký tự.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

from core import config
from core import database as db

logger = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
DEFAULT_DAYS = 30
MAX_DAYS = 180
MAX_CASES = 50
TEXT_MAX = 1500

_HEADER = """\
# Case đánh giá ỨNG VIÊN xuất từ các câu trả lời bị 👎 trên Telegram (/xuateval).
# Trước khi chép vào test/evals/*.yaml:
#   1. Bỏ thông tin cá nhân khỏi prompt/examples (repo public).
#   2. Đổi category cho đúng nhóm, thêm check tất định bắt được lỗi đã chê
#      (address, must_include, numbers, allowed_actions...) - test tier A yêu cầu
#      mọi examples.bad phải trượt ít nhất 1 check.
#   3. Thêm examples.good là câu trả lời mong muốn.
"""


def parse_days(argument: str) -> int:
    """Số ngày từ "30", "30d", "7 ngày" (kẹp 1..MAX_DAYS); trống/sai -> DEFAULT_DAYS."""
    digits = "".join(ch for ch in (argument or "").strip().split(" ", 1)[0] if ch.isdigit())
    if not digits:
        return DEFAULT_DAYS
    return max(1, min(MAX_DAYS, int(digits)))


def _case(row: dict) -> dict:
    created = row.get("created_at")
    when = created.astimezone(VN_TZ).strftime("%d/%m/%Y") if created else ""
    note = "; ".join(
        part
        for part in (
            f"👎 lúc {when}" if when else "👎",
            f"lý do: {row['reason']}" if row.get("reason") else "",
            f"provider: {row.get('provider') or '?'}/{row.get('model') or '?'}",
        )
        if part
    )
    case = {
        "id": f"fb-{int(row['id'])}",
        "category": "persona",
        "kind": "chat",
        "user": "owner",
        "channel": "telegram",
        "prompt": str(row.get("prompt_text") or "")[:TEXT_MAX],
        "checks": {"no_cjk": True},
        "examples": {"bad": [str(row.get("answer_text") or "")[:TEXT_MAX]]},
        "notes": note,
    }
    if row.get("reason"):
        case["judge"] = f"Câu trả lời đã khắc phục điều người dùng chê: {row['reason']}"
    return case


async def candidate_cases(days: int = DEFAULT_DAYS) -> list[dict]:
    """Case ứng viên từ 👎 Telegram của chủ bot trong ``days`` ngày (mới nhất trước)."""
    owner = config.ALLOWED_USER_ID
    if not owner:
        return []
    rows = await db.answer_feedback_owner_down(owner, days, MAX_CASES)
    return [_case(row) for row in rows if str(row.get("prompt_text") or "").strip()]


def to_yaml(cases: list[dict]) -> str:
    body = yaml.safe_dump(cases, allow_unicode=True, sort_keys=False, width=100)
    return f"{_HEADER}\n{body}"


async def export(argument: str = "") -> tuple[str, bytes, int] | None:
    """(tên file, nội dung YAML, số case) hoặc None khi chưa có 👎 nào."""
    days = parse_days(argument)
    cases = await candidate_cases(days)
    if not cases:
        return None
    name = f"eval-candidates-{datetime.now(VN_TZ):%Y%m%d}.yaml"
    return name, to_yaml(cases).encode("utf-8"), len(cases)
