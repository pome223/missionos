"""
チャネル基底クラス
OpenClaw のチャネルアーキテクチャを参考
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Callable, Awaitable
from dataclasses import dataclass
import asyncio


@dataclass
class ChannelMessage:
    """チャネルメッセージ"""
    content: str
    user_id: str
    channel_id: str
    message_id: Optional[str] = None
    reply_to: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class BaseChannel(ABC):
    """チャネル基底クラス"""

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        self.name = name
        self.config = config or {}
        self.is_running = False
        self.message_handler: Optional[Callable[[ChannelMessage], Awaitable[str]]] = None

    @abstractmethod
    async def start(self):
        """チャネル開始"""
        pass

    @abstractmethod
    async def stop(self):
        """チャネル停止"""
        pass

    @abstractmethod
    async def send_message(self, channel_id: str, message: str, **kwargs) -> bool:
        """メッセージ送信"""
        pass

    def set_message_handler(self, handler: Callable[[ChannelMessage], Awaitable[str]]):
        """メッセージハンドラー設定"""
        self.message_handler = handler

    async def handle_incoming_message(self, message: ChannelMessage) -> Optional[str]:
        """受信メッセージ処理"""
        if self.message_handler:
            try:
                response = await self.message_handler(message)
                return response
            except Exception as e:
                return f"Error processing message: {str(e)}"
        return None

    def get_status(self) -> Dict[str, Any]:
        """ステータス取得"""
        return {
            "name": self.name,
            "running": self.is_running,
            "config": {k: v for k, v in self.config.items() if k not in ["token", "secret"]},
        }
