"""
チャネルレジストリ
チャネルの登録と管理
"""

from typing import Dict, List, Optional, Type
from src.channels.base import BaseChannel


class ChannelRegistry:
    """チャネルレジストリ"""

    def __init__(self):
        self.channels: Dict[str, BaseChannel] = {}
        self.channel_classes: Dict[str, Type[BaseChannel]] = {}

    def register_channel_class(self, name: str, channel_class: Type[BaseChannel]):
        """チャネルクラスを登録"""
        self.channel_classes[name] = channel_class

    def register_channel(self, channel: BaseChannel):
        """チャネルインスタンスを登録"""
        self.channels[channel.name] = channel

    def get_channel(self, name: str) -> Optional[BaseChannel]:
        """チャネル取得"""
        return self.channels.get(name)

    def list_channels(self) -> List[str]:
        """チャネル一覧取得"""
        return list(self.channels.keys())

    def list_available_channel_classes(self) -> List[str]:
        """利用可能なチャネルクラス一覧"""
        return list(self.channel_classes.keys())

    async def start_all_channels(self):
        """全チャネル開始"""
        for channel in self.channels.values():
            if not channel.is_running:
                await channel.start()

    async def stop_all_channels(self):
        """全チャネル停止"""
        for channel in self.channels.values():
            if channel.is_running:
                await channel.stop()

    def get_status(self) -> Dict[str, dict]:
        """全チャネルステータス取得"""
        return {
            name: channel.get_status()
            for name, channel in self.channels.items()
        }


# グローバルレジストリ
_registry: Optional[ChannelRegistry] = None


def get_channel_registry() -> ChannelRegistry:
    """チャネルレジストリインスタンスを取得"""
    global _registry
    if _registry is None:
        _registry = ChannelRegistry()
    return _registry
