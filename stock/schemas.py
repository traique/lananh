"""Schema + lượt gọi có cấu trúc cho các bước debate trong stock/debate.py
(news / debate bull+bear).

Lượt JSON đi qua ``ai.orchestrator.utility_json()`` trước: Groq json_object mode,
Gemini JSON làm dự phòng - JSON hợp lệ ngay từ provider, không tốn lượt của
provider-chain chat (9Router đứng đầu). Không ra được JSON đúng schema thì rơi về
``orchestrator.ask()`` (provider-chain, text thô) -> bóc khối JSON -> validate
pydantic, kèm lỗi cụ thể của lần trước để model tự sửa: tối đa 2 lượt gọi mỗi bước.
Mọi lượt đều nằm trong hạn chót của lượt trả lời (``ai.timeouts.bounded()`` /
provider-chain).

Ràng buộc quan trọng: action và mọi con số giao dịch đều do stock/policy.py chốt.
Các schema ở đây KHÔNG có field action hay giá để LLM trở thành nguồn
entry/stop/target/position size. Vai Research Manager (đánh giá mức thuyết phục
của action hệ thống) gộp vào bước tổng hợp cuối ở stock/analysis.py - không có
output có cấu trúc nào mang action.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TypeVar

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class NewsAnalysis(BaseModel):
    """Tóm tắt tác động tin tức - thay cho việc nhét cả list tin thô vào prompt tổng hợp."""

    relevant: bool = Field(description="Có tin nào thực sự nói về đúng mã này không")
    sentiment_label: str = Field(description="một trong: tích cực / trung lập / tiêu cực")
    key_points: list[str] = Field(default_factory=list, max_length=3)
    impact_note: str = Field(
        default="", description="1 câu: tin này có đáng thay đổi cách nhìn kỹ thuật không, vì sao"
    )


class BullCase(BaseModel):
    """Luận điểm ủng hộ chiều tăng giá, CHỈ dựa trên số liệu đã có trong ctx/decision."""

    thesis: str = Field(description="1-2 câu tóm tắt luận điểm tăng giá")
    points: list[str] = Field(default_factory=list, max_length=4)


class BearCase(BaseModel):
    """Luận điểm rủi ro/giảm giá, phản biện trực tiếp luận điểm phe tăng giá."""

    thesis: str = Field(description="1-2 câu tóm tắt luận điểm rủi ro/giảm giá")
    points: list[str] = Field(default_factory=list, max_length=4)


class DebateCase(BaseModel):
    """Bull + bear trong CÙNG 1 lượt LLM: phe thận trọng phản biện thẳng luận điểm phe
    lạc quan vừa viết trong chính câu trả lời (trước đây 2 lượt tuần tự)."""

    bull: BullCase
    bear: BearCase


_SCHEMA_T = TypeVar("_SCHEMA_T", bound=BaseModel)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _type_skeleton(schema: type[BaseModel]) -> str:
    """Skeleton giá trị đơn giản thay vì dump nguyên JSON-schema properties.

    Bỏ nguyên `{'relevant': {'description': '...', 'type': 'boolean'}, ...}`
    vào prompt khiến model yếu copy ĐÚNG cấu trúc đó làm GIÁ TRỊ trả về (bug
    quan sát trên Render 05/09: mọi bước debate nhận ValidationError kiểu
    `Input should be a valid boolean, input_value={'description': ...}`), đốt
    quota oan 2 lần gọi/bước. Skeleton `{"relevant": true, ...}` khó nhầm hơn
    và token rẻ hơn hẳn. Model lồng nhau (DebateCase.bull) ra object lồng nhau."""
    full_schema = schema.model_json_schema()
    definitions = full_schema.get("$defs", {})

    def placeholder(prop: dict) -> object:
        if "$ref" in prop:
            return placeholder(definitions.get(prop["$ref"].rsplit("/", 1)[-1], {}))
        if "allOf" in prop and len(prop["allOf"]) == 1:
            return placeholder(prop["allOf"][0])
        if "anyOf" in prop:
            for variant in prop["anyOf"]:
                if variant.get("type") != "null":
                    return placeholder(variant)
            return None
        json_type = prop.get("type")
        if json_type == "object" or "properties" in prop:
            return {name: placeholder(sub) for name, sub in prop.get("properties", {}).items()}
        if json_type == "boolean":
            return True
        if json_type == "integer":
            return 1
        if json_type == "number":
            return 0.5
        if json_type == "array":
            items = prop.get("items", {})
            if items.get("type") == "number":
                return [0.5]
            return ["nội dung ý 1"]
        return "nội dung text"

    return json.dumps(placeholder(full_schema), ensure_ascii=False)


def _extract_json(text: str) -> str:
    """LLM hay bọc JSON trong ```json ... ``` hoặc thêm lời dẫn trước/sau -
    lấy khối {...} ngoài cùng đầu tiên thay vì json.loads() thẳng cả text."""
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE)
    match = _JSON_BLOCK_RE.search(stripped)
    return match.group(0) if match else stripped


_sleep = asyncio.sleep  # điểm thay thế cho test


class ChainPacer:
    """Giãn nhịp các lượt gọi provider-chain (``orchestrator.ask`` - 9Router đứng đầu)
    liên tiếp trong 1 lượt phân tích: nghỉ ROUTER9_STEP_DELAY_SEC trước lượt chain thứ
    2 trở đi (không vượt hạn chót còn lại của lượt). Lượt JSON qua utility_json
    (Groq/Gemini) không đụng 9Router nên không tính: đường thường của /phantich (2 lượt
    JSON + 1 tổng hợp) không ngủ giây nào; xấu nhất cả 3 lượt rơi về chain thì nghỉ 2
    lần (trước đây 4 lần x 3s ở mọi lượt)."""

    def __init__(self, delay_sec: float | None = None) -> None:
        if delay_sec is None:
            from core import config

            delay_sec = config.ROUTER9_STEP_DELAY_SEC
        self._delay = float(delay_sec or 0)
        self._chain_calls = 0

    async def before_chain_call(self) -> None:
        from ai.timeouts import remaining_turn_seconds

        delay = self._delay if self._chain_calls else 0.0
        self._chain_calls += 1
        remaining = remaining_turn_seconds()
        if remaining is not None:
            delay = min(delay, max(0.0, remaining))
        if delay > 0:
            await _sleep(delay)


async def ask_structured(
    schema: type[_SCHEMA_T],
    prompt: str,
    *,
    step_name: str,
    pacer: ChainPacer | None = None,
) -> _SCHEMA_T | None:
    """1 bước JSON theo ``schema``: utility_json trước, provider-chain dự phòng.

    Tối đa 2 lượt gọi: (1) utility_json (JSON mode); (2) nếu (1) không trả được
    object hoặc sai schema -> orchestrator.ask() kèm lỗi cụ thể để model tự sửa.
    Lỗi sau đó -> trả None, KHÔNG raise - bước gọi hàm này (mỗi step trong
    stock/debate.py) bỏ qua block đó trong prompt tổng hợp, không làm sập cả
    pipeline phân tích. Hết hạn chót lượt -> None ngay, không gọi thêm.
    """
    from ai import orchestrator
    from ai.timeouts import TurnTimeoutError, bounded

    schema_hint = _type_skeleton(schema)
    full_prompt = (
        f"{prompt}\n\n"
        f"CHỈ trả về DUY NHẤT một object JSON hợp lệ, không thêm lời dẫn, không dùng markdown code block. "
        f"Format JSON chính xác với CÁC GIÁ TRỊ THẬT (không phải mô tả field, không copy chữ 'nội dung'): {schema_hint}"
    )

    last_error: str | None = None
    try:
        data = await bounded(lambda: orchestrator.utility_json(full_prompt))
    except TurnTimeoutError:
        logger.info("Debate step '%s' bỏ qua: hết hạn chót lượt.", step_name)
        return None
    except Exception:
        logger.warning("Debate step '%s' utility_json lỗi", step_name, exc_info=True)
        data = None
    if isinstance(data, dict):
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            last_error = str(exc)
            logger.warning(
                "Debate step '%s' JSON sai schema (utility_json): %s", step_name, last_error
            )

    retry_prompt = full_prompt
    if last_error:
        retry_prompt += (
            f"\n\nLần trước bạn trả JSON KHÔNG hợp lệ, lỗi: {last_error}\n"
            f"LƯU Ý: giá trị của mỗi field phải là NỘI DUNG THẬT (boolean/string/list giá trị), "
            f"TUYỆT ĐỐI không trả lại định nghĩa field kiểu {{'description': ..., 'type': ...}} "
            f"lấy từ mô tả ở trên. Trả lại ĐÚNG format JSON, sửa lỗi trên."
        )
    try:
        if pacer is not None:
            await pacer.before_chain_call()
        response = await orchestrator.ask(retry_prompt)
        return schema.model_validate(json.loads(_extract_json(response.text or "")))
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("Debate step '%s' bỏ qua: JSON lỗi ở cả lượt dự phòng: %s", step_name, exc)
    except TurnTimeoutError:
        logger.info("Debate step '%s' bỏ qua: hết hạn chót lượt.", step_name)
    except Exception:
        logger.exception("Debate step '%s' gọi LLM lỗi", step_name)
    return None
