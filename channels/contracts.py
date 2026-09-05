from pydantic import BaseModel, Field, field_validator

from core.text_normalize import nfc


class ZaloMessageRequest(BaseModel):
    account_id: str = Field(min_length=1)
    sender_id: str = Field(min_length=1)
    sender_name: str = Field(default="", max_length=500)
    conversation_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=20_000)

    # nfc(): text từ Zalo (đặc biệt qua một số input method/iOS) có thể ở
    # dạng NFD - chuẩn hoá ngay tại model, để MỌI endpoint nhận request này
    # (receive, group_message) tự động được bảo vệ mà không cần sửa từng nơi.
    @field_validator("text", "sender_name")
    @classmethod
    def _normalize_nfc(cls, value: str) -> str:
        return nfc(value)


class ZaloMessageResponse(BaseModel):
    messages: list[str]
    provider: str | None = None
    image_b64: str | None = None


class ZaloGroupMessageRequest(BaseModel):
    account_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    sender_id: str = Field(min_length=1)
    sender_name: str = Field(default="", max_length=500)
    text: str = Field(min_length=1, max_length=20_000)
    sent_at_ms: int = Field(gt=0)

    @field_validator("text", "sender_name")
    @classmethod
    def _normalize_nfc(cls, value: str) -> str:
        return nfc(value)


class ZaloGroupConfig(BaseModel):
    group_id: str
    alias: str


class ZaloOutboxItem(BaseModel):
    id: int
    content: str
