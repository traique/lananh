"""Nhánh cổ phiếu chạy trong MỘT hạn chót chung của lượt trả lời.

Telegram (handlers/chat_router.py) và Zalo/Zoom
(services/channel_chat_service.handle_channel_text) bọc nhánh cổ phiếu và lượt
chat theo sau trong 1 ``ai.timeouts.turn_deadline()``. Các hàm phân tích
(stock.analysis.analyze_symbol/analyze_persona) nuốt MỌI exception - kể cả
TurnTimeoutError - nên caller không bắt được lỗi hết giờ. Thay vào đó caller hỏi
``turn_expired()`` sau vòng lặp các mã để báo 1 câu rõ ràng:

- Phân tích đầy đủ: bản số liệu rule-based (nguồn số chuẩn từ stock/policy.py)
  vẫn gửi cho MỌI mã - hết hạn chót thì ``bounded()`` từ chối ngay, các mã sau
  chỉ chạy phần dữ liệu (không tốn lượt AI). Mã nào không có phần diễn giải AI
  được gom vào MỘT câu báo, thay cho dòng "⏳ ... sẽ gửi tiếp ngay sau đây" rồi
  im lặng.
- Góc nhìn persona: các mã lỗi được gom thành 1 dòng thay vì lặp N câu lỗi.
"""

from __future__ import annotations

import messages
from ai.timeouts import remaining_turn_seconds

# Biên nhỏ coi như đã hết hạn (khớp ai.timeouts._DEADLINE_EPSILON_SEC).
_EXPIRED_EPSILON_SEC = 0.05


def turn_expired() -> bool:
    """True nếu lượt hiện tại có hạn chót và đã hết (hoặc sắp hết)."""
    remaining = remaining_turn_seconds()
    return remaining is not None and remaining <= _EXPIRED_EPSILON_SEC


def _join(symbols: list[str]) -> str:
    return ", ".join(symbols)


def commentary_skipped_note(symbols: list[str]) -> str:
    """Câu báo cho các mã đã gửi bản số liệu nhưng không có phần diễn giải AI."""
    if turn_expired():
        return (
            f"⏱️ Phần bình luận AI cho {_join(symbols)} quá giờ nên em dừng ở bản số liệu, "
            "số liệu ở trên vẫn đúng nha anh."
        )
    return (
        f"ℹ️ Phần diễn giải AI cho {_join(symbols)} chưa tạo được lúc này, "
        "số liệu ở trên vẫn đúng nha anh."
    )


def is_persona_failure(text: str, symbol: str) -> bool:
    return text == messages.STOCK_ANALYZE_FAILED.format(symbol=symbol)


def persona_failure_note(symbols: list[str]) -> str:
    """1 dòng gom các mã persona lỗi/hết giờ."""
    if turn_expired():
        return f"⏱️ Góc nhìn cho {_join(symbols)} quá giờ, em chưa kịp diễn giải nha anh."
    return messages.STOCK_ANALYZE_FAILED.format(symbol=_join(symbols))


def split_persona_results(symbols: list[str], results: list[str]) -> tuple[list[str], list[str]]:
    """(kết quả thành công theo thứ tự, các mã lỗi) - caller gom mã lỗi thành
    1 dòng ``persona_failure_note`` thay vì lặp câu lỗi cho từng mã."""
    kept: list[str] = []
    failed: list[str] = []
    for symbol, text in zip(symbols, results, strict=False):
        if not text:
            continue
        if is_persona_failure(text, symbol):
            failed.append(symbol)
        else:
            kept.append(text)
    return kept, failed
