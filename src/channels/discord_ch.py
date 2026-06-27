"""
Discordチャネル統合
"""

from typing import Optional, Dict, Any
import asyncio

from src.channels.base import BaseChannel, ChannelMessage

# Discord は遅延インポート
discord = None
discord_available = False

try:
    import discord
    from discord.ext import commands
    discord_available = True
except ImportError:
    discord_available = False


class DiscordChannel(BaseChannel):
    """Discordチャネル"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__("discord", config)
        self.bot_token = config.get("bot_token") if config else None
        self.bot: Optional[commands.Bot] = None
        self._bot_task: Optional[asyncio.Task] = None

        if not discord_available:
            raise RuntimeError(
                "discord.py is not installed. Run: pip install 'boiled-claw[discord]'"
            )

        if not self.bot_token:
            raise ValueError("Discord bot_token is required")

    async def start(self):
        """チャネル開始"""
        intents = discord.Intents.default()
        intents.message_content = True

        self.bot = commands.Bot(command_prefix="!", intents=intents)

        # イベントハンドラー登録
        @self.bot.event
        async def on_ready():
            print(f"Discord bot logged in as {self.bot.user}")

        @self.bot.event
        async def on_message(message):
            # 自分のメッセージは無視
            if message.author == self.bot.user:
                return

            # コマンドは無視
            if message.content.startswith("!"):
                await self.bot.process_commands(message)
                return

            # ChannelMessage作成
            channel_message = ChannelMessage(
                content=message.content,
                user_id=str(message.author.id),
                channel_id=str(message.channel.id),
                message_id=str(message.id),
                metadata={
                    "username": str(message.author),
                    "guild": str(message.guild.name) if message.guild else None,
                }
            )

            # ハンドラー呼び出し
            response = await self.handle_incoming_message(channel_message)

            if response:
                await message.channel.send(response)

        # Botコマンド
        @self.bot.command()
        async def hello(ctx):
            await ctx.send("🦀 boiled-claw へようこそ！")

        # Bot起動 (非同期タスク)
        self._bot_task = asyncio.create_task(self.bot.start(self.bot_token))
        self.is_running = True

    async def stop(self):
        """チャネル停止"""
        if self.bot:
            await self.bot.close()

        if self._bot_task:
            self._bot_task.cancel()
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass

        self.is_running = False

    async def send_message(self, channel_id: str, message: str, **kwargs) -> bool:
        """メッセージ送信"""
        if not self.bot:
            return False

        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel:
                await channel.send(message, **kwargs)
                return True
            return False
        except Exception as e:
            print(f"Failed to send Discord message: {e}")
            return False
