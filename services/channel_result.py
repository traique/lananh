"""ChannelResult sống riêng module này (không import gì từ services/handlers/
channels) để tránh vòng import: channels.group_commands/facebook_commands cần
class này nhưng không được import ngược services.channel_chat_service (module
đó kéo theo channel_command_service và cả chuỗi service phía sau).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelResult:
    messages: list[str]
    provider: str | None = None
    image_b64: str | None = None
    image_url: str | None = None
