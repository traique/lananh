"""Lệnh /lich, /tsh, /tsh_chitiet, /tuvi, /tuvi_chitiet dùng chung cho mọi kênh.

/lich, /tsh, /tuvi tính bằng code, KHÔNG gọi AI. Hai lệnh *_chitiet gửi kết
quả đã tính cho AI diễn giải (1 lượt AI, có telemetry như các lệnh AI khác).
Ngày sinh, họ tên KHÔNG lưu vào DB/trí nhớ: telemetry chỉ ghi tên lệnh và độ
dài câu trả lời để đếm lượt AI, không ghi tham số hay nội dung luận giải.
"""

from __future__ import annotations

import datetime as dt
import logging

from ai.timeouts import TurnTimeoutError
from services import calendar_query, numerology, tuvi
from services.telemetry import telemetry

logger = logging.getLogger(__name__)

_AI_ERROR = "❌ AI chưa luận giải được lúc này, xem tạm phần đã tính ở trên nhé."


def lich(argument: str) -> str:
    return calendar_query.answer(argument)


def _parse_tsh(argument: str) -> tuple[dt.date, str] | str:
    """(ngày sinh, họ tên) hoặc câu báo lỗi - cùng quy tắc tên với web/API."""
    parsed = numerology.parse(argument)
    if isinstance(parsed, str):
        return parsed
    birth, name = parsed
    if name and not numerology.has_letters(name):
        return numerology.NAME_ERROR
    return birth, name


def tsh(argument: str) -> str:
    parsed = _parse_tsh(argument)
    if isinstance(parsed, str):
        return parsed
    return numerology.format_reading(numerology.compute(*parsed))


def tuvi_chart(argument: str) -> str:
    chart = tuvi.parse(argument)
    return chart if isinstance(chart, str) else tuvi.format_chart(chart)


async def ai_text(user_id: int, kind: str, channel: str, explain) -> str:
    """1 lượt AI diễn giải có telemetry; lỗi/hết hạn thì trả câu báo lỗi."""
    prompt_id = await telemetry.start(user_id, kind, f"/{kind}", channel=channel)
    try:
        text = await explain()
    except TurnTimeoutError as exc:
        logger.warning("%s hết hạn chót lượt: %s", kind, exc)
        await telemetry.failure(prompt_id, kind, exc)
        return f"⏱️ {exc}"
    except Exception as exc:
        logger.warning("%s lỗi: %r", kind, exc)
        await telemetry.failure(prompt_id, kind, exc)
        return _AI_ERROR
    await telemetry.success(prompt_id, kind, f"({len(text)} ký tự)")
    return text or _AI_ERROR


async def _with_ai(user_id: int, kind: str, channel: str, base: str, explain) -> list[str]:
    return [base, await ai_text(user_id, kind, channel, explain)]


async def tsh_chitiet(user_id: int, argument: str, channel: str) -> list[str]:
    parsed = _parse_tsh(argument)
    if isinstance(parsed, str):
        return [parsed]
    reading = numerology.compute(*parsed)
    return await _with_ai(
        user_id,
        "tsh_chitiet",
        channel,
        numerology.format_reading(reading),
        lambda: numerology.explain_with_ai(reading),
    )


async def tuvi_chitiet(user_id: int, argument: str, channel: str) -> list[str]:
    chart = tuvi.parse(argument)
    if isinstance(chart, str):
        return [chart]
    return await _with_ai(
        user_id,
        "tuvi_chitiet",
        channel,
        tuvi.format_chart(chart),
        lambda: tuvi.explain_with_ai(chart),
    )


async def handle(command: str, argument: str, user_id: int, channel: str) -> list[str] | None:
    """Kết quả cho 1 lệnh huyền học, None nếu không phải lệnh của module này."""
    if command == "/lich":
        return [lich(argument)]
    if command == "/tsh":
        return [tsh(argument)]
    if command in ("/tsh_chitiet", "/ths_chitiet"):
        return await tsh_chitiet(user_id, argument, channel)
    if command == "/tuvi":
        return [tuvi_chart(argument)]
    if command == "/tuvi_chitiet":
        return await tuvi_chitiet(user_id, argument, channel)
    return None
