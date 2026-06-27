"""
Telegramチャネル統合
"""

from typing import Optional, Dict, Any
import asyncio

from src.channels.base import BaseChannel, ChannelMessage

# Telegram は遅延インポート
telegram = None
telegram_available = False

try:
    from telegram import Update, Bot
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
    telegram_available = True
except ImportError:
    telegram_available = False


class TelegramChannel(BaseChannel):
    """Telegramチャネル"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__("telegram", config)
        self.bot_token = config.get("bot_token") if config else None
        self.application: Optional[Application] = None

        if not telegram_available:
            raise RuntimeError(
                "python-telegram-bot is not installed. Run: pip install 'boiled-claw[telegram]'"
            )

        if not self.bot_token:
            raise ValueError("Telegram bot_token is required")

    async def start(self):
        """チャネル開始"""
        self.application = Application.builder().token(self.bot_token).build()

        # ハンドラー登録
        self.application.add_handler(CommandHandler("start", self._handle_start))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))

        # Bot開始
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()

        self.is_running = True

    async def stop(self):
        """チャネル停止"""
        if self.application:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()

        self.is_running = False

    async def send_message(self, channel_id: str, message: str, **kwargs) -> bool:
        """メッセージ送信"""
        if not self.application:
            return False

        try:
            await self.application.bot.send_message(
                chat_id=channel_id,
                text=message,
                **kwargs
            )
            return True
        except Exception as e:
            print(f"Failed to send Telegram message: {e}")
            return False

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """startコマンド処理"""
        await update.message.reply_text(
            "🦀 boiled-claw へようこそ！\n"
            "メッセージを送信するとAIエージェントが応答します。"
        )

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """メッセージ処理"""
        if not update.message or not update.message.text:
            return

        # ChannelMessage作成
        channel_message = ChannelMessage(
            content=update.message.text,
            user_id=str(update.message.from_user.id),
            channel_id=str(update.message.chat_id),
            message_id=str(update.message.message_id),
            metadata={
                "username": update.message.from_user.username,
                "first_name": update.message.from_user.first_name,
            }
        )

        # ハンドラー呼び出し
        response = await self.handle_incoming_message(channel_message)

        if response:
            await update.message.reply_text(response)
