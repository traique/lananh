"""ChannelResult sống riêng module này (không import gì từ services/handlers/
channels) để tránh vòng import: channel_chat_service -> channel_command_service
-> handlers.commands -> channels.group_commands -> (từng import ngược lại
channel_chat_service chỉ để lấy class này). App thật không crash vì
bot_app.py tình cờ import handlers.commands trước, nhưng import
services.channel_chat_service/channel_command_service trực tiếp (như test)
sẽ crash ImportError do vòng lặp chưa nạp xong.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelResult:
    messages: list[str]
    provider: str | None = None
