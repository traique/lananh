"""Schema cho các bước debate (news / bull / bear) trong stock/debate.py.

Đây KHÔNG phải structured output "xịn" kiểu function-calling/response_schema
native của từng provider - `ai/orchestrator.ask()` chạy qua nhiều provider
khác nhau (9Router, Groq, OpenRouter, Gemini) và chỉ trả về text thô, không
có 1 chuẩn structured-output chung cho cả chain. Nên cách làm ở đây là:
LLM được yêu cầu trả JSON thuần trong prompt -> parse -> validate bằng
pydantic -> nếu lỗi thì retry đúng 1 lần kèm thông báo lỗi để model tự sửa.

Ràng buộc quan trọng - GIỮ NGUYÊN nguyên tắc "không được đổi SỐ" của
stock/policy.py, dù đã cho phép FinalDecision.action khác action hệ thống
theo yêu cầu người dùng: mọi schema ở đây (trừ field `action`/`confidence`
định tính của FinalDecision) CHỈ có field str/list[str], KHÔNG có field giá
nào. Nếu LLM nhét số vào trong text thì đó cũng chỉ là câu chữ hiển thị lại,
không phải nguồn số liệu được code nào khác tin dùng - entry/stop/target/
position size vẫn CHỈ tồn tại khi đã qua đủ gate định lượng của policy.py;
nếu FinalDecision.action khác action hệ thống, KHÔNG có vùng giá nào cho
action mới đó cả (xem stock/analysis.py phần ghép trade_plan).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

_MAX_POINT_LEN = 160  # 1 luận điểm quá dài là dấu hiệu LLM đang viết cả đoạn văn thay vì gạch đầu dòng

_ACTIONS = Literal["BUY", "HOLD", "WATCH", "SELL", "NO_TRADE"]


class NewsAnalysis(BaseModel):
    """Tóm tắt tác động tin tức - thay cho việc nhét cả list tin thô vào prompt tổng hợp."""

    relevant: bool = Field(description="Có tin nào thực sự nói về đúng mã này không")
    sentiment_label: str = Field(description="một trong: tích cực / trung lập / tiêu cực")
    key_points: list[str] = Field(default_factory=list, max_length=3)
    impact_note: str = Field(default="", description="1 câu: tin này có đáng thay đổi cách nhìn kỹ thuật không, vì sao")


class BullCase(BaseModel):
    """Luận điểm ủng hộ chiều tăng giá, CHỈ dựa trên số liệu đã có trong ctx/decision."""

    thesis: str = Field(description="1-2 câu tóm tắt luận điểm tăng giá")
    points: list[str] = Field(default_factory=list, max_length=4)


class BearCase(BaseModel):
    """Luận điểm rủi ro/giảm giá, được thấy bull_case để phản biện trực tiếp."""

    thesis: str = Field(description="1-2 câu tóm tắt luận điểm rủi ro/giảm giá")
    points: list[str] = Field(default_factory=list, max_length=4)


class FinalDecision(BaseModel):
    """Quyết định CUỐI CÙNG sau khi 'Manager' nghe hết news/bull/bear + quyết định gốc của code.

    Đây là bước DUY NHẤT trong debate pipeline được phép ra action khác với
    stock.policy.Decision.action (theo yêu cầu: để AI tự cân nhắc, không bắt
    buộc đồng ý với code). Nhưng dứt khoát KHÔNG có field giá/entry/stop/
    target/tỷ trọng nào ở đây - những con số đó chỉ tồn tại khi đã qua đủ 4
    gate định lượng của policy.py (RR, data quality, regime, setup). Nếu
    action ở đây khác action của code, coi như "nhận định định tính, CHƯA
    qua gate định lượng" - render layer (xem stock/analysis.py) phải tự nói
    rõ điều này, KHÔNG được suy ra 1 vùng giá nào cho action mới.
    """

    action: _ACTIONS = Field(description="BUY / HOLD / WATCH / SELL / NO_TRADE")
    confidence: float = Field(ge=0.0, le=1.0, description="0.0 đến 1.0, mức tự tin của Manager vào action này")
    reasoning: str = Field(description="2-3 câu giải thích vì sao chọn action này, đặc biệt PHẢI giải thích rõ nếu khác action hệ thống")


_SCHEMA_T = TypeVar("_SCHEMA_T", bound=BaseModel)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> str:
    """LLM hay bọc JSON trong ```json ... ``` hoặc thêm lời dẫn trước/sau -
    lấy khối {...} ngoài cùng đầu tiên thay vì json.loads() thẳng cả text."""
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE)
    match = _JSON_BLOCK_RE.search(stripped)
    return match.group(0) if match else stripped


async def ask_structured(
    schema: type[_SCHEMA_T],
    prompt: str,
    *,
    step_name: str,
) -> _SCHEMA_T | None:
    """Gọi orchestrator.ask() với prompt yêu cầu JSON, validate theo `schema`.

    Retry đúng 1 lần khi parse/validate lỗi (gửi kèm lỗi cụ thể để model tự
    sửa). Lỗi sau 2 lần -> trả None, KHÔNG raise - bước gọi hàm này (mỗi step
    trong stock/debate.py) phải tự quyết định fallback (bỏ qua block đó
    trong prompt tổng hợp), không được làm sập cả pipeline phân tích.
    """
    from ai import orchestrator

    schema_hint = json.dumps(schema.model_json_schema().get("properties", {}), ensure_ascii=False)
    full_prompt = (
        f"{prompt}\n\n"
        f"CHỈ trả về DUY NHẤT một object JSON hợp lệ, không thêm lời dẫn, không dùng markdown code block. "
        f"Các field bắt buộc và mô tả: {schema_hint}"
    )

    last_error: str | None = None
    for attempt in range(2):
        current_prompt = full_prompt
        if last_error:
            current_prompt += (
                f"\n\nLần trước bạn trả JSON KHÔNG hợp lệ, lỗi: {last_error}\n"
                f"Trả lại ĐÚNG format JSON, sửa lỗi trên."
            )
        try:
            response = await orchestrator.ask(current_prompt)
            raw = _extract_json(response.text or "")
            data = json.loads(raw)
            return schema.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = str(exc)
            logger.warning("Debate step '%s' JSON lỗi lần %d: %s", step_name, attempt + 1, last_error)
        except Exception:
            logger.exception("Debate step '%s' gọi LLM lỗi", step_name)
            return None
    logger.warning("Debate step '%s' bỏ qua sau 2 lần JSON lỗi liên tiếp.", step_name)
    return None
