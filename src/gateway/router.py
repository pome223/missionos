"""
メッセージルーティング
チャネルとエージェント間のメッセージルーティング
"""

from typing import Optional, Dict, Any, Callable, Awaitable
from enum import Enum
import asyncio


class MessageType(Enum):
    """メッセージタイプ"""
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    AUDIO = "audio"
    VIDEO = "video"
    COMMAND = "command"
    SYSTEM = "system"


class Message:
    """メッセージモデル"""

    def __init__(
        self,
        content: str,
        message_type: MessageType = MessageType.TEXT,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        channel: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.content = content
        self.message_type = message_type
        self.user_id = user_id
        self.session_id = session_id
        self.channel = channel
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        """辞書形式に変換"""
        return {
            "content": self.content,
            "type": self.message_type.value,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "channel": self.channel,
            "metadata": self.metadata,
        }


class MessageRouter:
    """メッセージルーター"""

    def __init__(self):
        self.handlers: Dict[str, Callable[[Message], Awaitable[Any]]] = {}
        self.channel_routes: Dict[str, str] = {}  # channel -> handler

    def register_handler(
        self,
        name: str,
        handler: Callable[[Message], Awaitable[Any]]
    ):
        """ハンドラー登録"""
        self.handlers[name] = handler

    def register_channel(self, channel: str, handler_name: str):
        """チャネルルート登録"""
        self.channel_routes[channel] = handler_name

    async def route(self, message: Message) -> Any:
        """メッセージをルーティング"""
        # チャネル指定がある場合
        if message.channel and message.channel in self.channel_routes:
            handler_name = self.channel_routes[message.channel]
            if handler_name in self.handlers:
                return await self.handlers[handler_name](message)

        # デフォルトハンドラー
        if "default" in self.handlers:
            return await self.handlers["default"](message)

        raise ValueError(f"No handler found for message: {message.to_dict()}")

    async def broadcast(self, message: Message, exclude_channels: Optional[list] = None):
        """全チャネルにブロードキャスト"""
        exclude_channels = exclude_channels or []
        tasks = []

        for channel, handler_name in self.channel_routes.items():
            if channel not in exclude_channels and handler_name in self.handlers:
                # メッセージコピーしてチャネル設定
                msg = Message(
                    content=message.content,
                    message_type=message.message_type,
                    user_id=message.user_id,
                    session_id=message.session_id,
                    channel=channel,
                    metadata=message.metadata,
                )
                tasks.append(self.handlers[handler_name](msg))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
