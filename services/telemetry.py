"""Ghi lại lịch sử prompt/result (bảng prompts/results, xem core/database.py)
cho mọi lệnh AI - dùng chung 1 TelemetryService thay vì gọi core.database
rải rác trong từng handler, để boilerplate "tạo prompt -> ghi kết quả/lỗi"
chỉ nằm ở 1 chỗ.

Không dùng decorator bọc quanh toàn bộ handler vì mỗi lệnh cần luồng khác
nhau (early-return khi rỗng, format tin nhắn trả lời khác nhau) - decorator
sẽ phải nhận quá nhiều tham số tuỳ biến để còn tiết kiệm được code. Thay vào
đó, TelemetryService chỉ gói 3 thao tác DB lặp lại (start/success/failure),
để handler vẫn tự quyết định luồng của mình.
"""
import logging

from core import database as db

logger = logging.getLogger(__name__)


class TelemetryService:
    async def start(self, telegram_user_id: int, action_type: str, prompt_text: str) -> int:
        return await db.save_prompt(telegram_user_id, action_type, prompt_text)

    async def success(self, prompt_id: int, action_type: str, content_text: str) -> None:
        await db.save_result(prompt_id, action_type, content_text=content_text)

    async def failure(self, prompt_id: int, action_type: str, error: Exception) -> None:
        """Chỉ lưu TÊN loại lỗi (vd "ValueError"), KHÔNG lưu str(error) - có
        thể chứa dữ liệu nhạy cảm (token, nội dung tin nhắn user...) lọt vào
        exception message, không nên lưu thẳng vào DB lịch sử."""
        try:
            await db.save_result(prompt_id, action_type, content_text=f"error: {type(error).__name__}")
        except Exception:
            logger.exception("Không ghi được lỗi vào DB cho prompt_id=%s", prompt_id)


telemetry = TelemetryService()
