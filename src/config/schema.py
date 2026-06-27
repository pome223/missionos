"""
設定スキーマ定義
Pydantic modelsを使った型定義
"""

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class ChannelConfig(BaseModel):
    """チャネル設定"""
    name: str = Field(..., description="Channel name")
    enabled: bool = Field(default=True, description="Is channel enabled")
    config: Dict[str, Any] = Field(default_factory=dict, description="Channel-specific config")


class ModelConfig(BaseModel):
    """モデル設定"""
    name: str = Field(..., description="Model name (e.g., gemini-3.1-flash-lite-preview)")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="Temperature")
    max_tokens: Optional[int] = Field(default=None, description="Max output tokens")
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Top-p sampling")
    top_k: Optional[int] = Field(default=None, ge=1, description="Top-k sampling")


class SessionConfig(BaseModel):
    """セッション設定"""
    timeout_seconds: int = Field(default=3600, description="Session timeout")
    max_history: int = Field(default=100, description="Max message history")
    enable_memory: bool = Field(default=True, description="Enable memory storage")


class SecurityConfig(BaseModel):
    """セキュリティ設定"""
    enable_audit: bool = Field(default=True, description="Enable audit logging")
    allowed_commands: Optional[List[str]] = Field(default=None, description="Allowed shell commands")
    blocked_commands: List[str] = Field(
        default_factory=lambda: [
            "rm -rf",
            "sudo rm",
            "mkfs",
            "dd if=",
            "> /dev/",
            "chmod 777",
        ],
        description="Blocked shell commands"
    )


class AppConfig(BaseModel):
    """アプリケーション全体の設定"""
    channels: List[ChannelConfig] = Field(default_factory=list, description="Channel configs")
    model: ModelConfig = Field(default_factory=lambda: ModelConfig(name="gemini-3.1-flash-lite-preview"))
    session: SessionConfig = Field(default_factory=SessionConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
