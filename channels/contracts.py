from typing import Literal

from pydantic import BaseModel, Field, field_validator

from core.text_normalize import nfc

# Trần tin được trích dẫn (ZaloMessageRequest.quote_text) - khớp
# services/chat_context.QUOTE_MAX_CHARS.
QUOTE_TEXT_MAX_CHARS = 600


class ZaloMessageRequest(BaseModel):
    account_id: str = Field(min_length=1)
    sender_id: str = Field(min_length=1)
    sender_name: str = Field(default="", max_length=500)
    conversation_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=20_000)
    # Tin người gửi đang trả lời (zca-js data.quote.msg). Tuỳ chọn, JSON key
    # "quote_text", tối đa QUOTE_TEXT_MAX_CHARS ký tự - dài hơn thì CẮT (không
    # từ chối cả tin nhắn). Gateway cũ không gửi trường này -> None.
    quote_text: str | None = None

    # nfc(): text từ Zalo (đặc biệt qua một số input method/iOS) có thể ở
    # dạng NFD - chuẩn hoá ngay tại model, để MỌI endpoint nhận request này
    # (receive, group_message) tự động được bảo vệ mà không cần sửa từng nơi.
    @field_validator("text", "sender_name")
    @classmethod
    def _normalize_nfc(cls, value: str) -> str:
        return nfc(value)

    @field_validator("quote_text", mode="before")
    @classmethod
    def _clip_quote(cls, value: object) -> str | None:
        if value is None:
            return None
        text = nfc(str(value)).strip()[:QUOTE_TEXT_MAX_CHARS]
        return text or None


# Giá trị TextStyle của zca-js 2.1.2 (dist/apis/sendMessage.d.ts) mà bridge dùng:
# đậm, nghiêng, gạch chân, gạch ngang, đỏ, cam, vàng, xanh lá, chữ nhỏ, chữ to.
ZaloTextStyleName = Literal[
    "b", "i", "u", "s", "c_db342e", "c_f27806", "c_f7b503", "c_15a85f", "f_13", "f_18"
]


class ZaloTextStyle(BaseModel):
    """1 phần tử ``styles`` của zca-js sendMessage({msg, styles}). start/len tính
    theo đơn vị UTF-16 (như String.length của JavaScript), trong chính tin đó."""

    start: int = Field(ge=0)
    len: int = Field(ge=1)
    st: ZaloTextStyleName


class ZaloMessageResponse(BaseModel):
    messages: list[str]
    provider: str | None = None
    image_b64: str | None = None
    # Tuỳ chọn: styles[i] là định dạng của messages[i] (channels/zalo_text.py);
    # None/thiếu = gửi chữ thuần. Gateway chưa hỗ trợ thì bỏ qua trường này - chữ
    # trong messages đã là bản thuần đầy đủ. Bản cache idempotency cũ không có
    # trường này vẫn đọc được.
    styles: list[list[ZaloTextStyle]] | None = None


# Câu báo gateway xin dựng sẵn (POST /internal/zalo/notice, channels/zalo_media.py)
# - khớp NoticeKind trong zalo-gateway/src/bridge.ts.
ZaloNoticeKind = Literal[
    "image",
    "voice_failed",
    "voice_too_large",
    "file_failed",
    "file_too_large",
    "file_unsupported",
    "video",
    "location",
]


class ZaloNoticeRequest(BaseModel):
    account_id: str = Field(default="zalo-bot", max_length=200)
    sender_id: str = Field(min_length=1, max_length=200)
    kind: ZaloNoticeKind


class ZaloNoticeResponse(BaseModel):
    # None = người lạ chưa pair / bị khoá: gateway im lặng.
    text: str | None = None


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


class ZaloFacebookMedia(BaseModel):
    mime_type: str = Field(pattern=r"^image/(jpeg|png|webp)$")
    data_b64: str = Field(min_length=1)


class ZaloFacebookPostRequest(BaseModel):
    account_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    sender_id: str = Field(min_length=1)
    sender_name: str = Field(default="", max_length=500)
    message_ids: list[str] = Field(min_length=1, max_length=50)
    text: str = Field(default="", max_length=20_000)
    media: list[ZaloFacebookMedia] = Field(default_factory=list, max_length=10)

    @field_validator("text", "sender_name")
    @classmethod
    def _normalize_facebook_nfc(cls, value: str) -> str:
        return nfc(value)


class ZaloOutboxItem(BaseModel):
    id: int
    content: str
