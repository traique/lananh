from pydantic import BaseModel, Field


class ZaloMessageRequest(BaseModel):
    account_id: str = Field(min_length=1)
    sender_id: str = Field(min_length=1)
    sender_name: str = Field(default="", max_length=500)
    conversation_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=20_000)


class ZaloMessageResponse(BaseModel):
    messages: list[str]
    provider: str | None = None


class ZaloGroupMessageRequest(BaseModel):
    account_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    sender_id: str = Field(min_length=1)
    sender_name: str = Field(default="", max_length=500)
    text: str = Field(min_length=1, max_length=20_000)
    sent_at_ms: int = Field(gt=0)


class ZaloGroupConfig(BaseModel):
    group_id: str
    alias: str


class ZaloOutboxItem(BaseModel):
    id: int
    content: str
