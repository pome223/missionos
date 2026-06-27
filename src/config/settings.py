"""
設定管理 - Pydantic Settings
環境変数から設定を読み込む
"""

from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """アプリケーション設定"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Google AI / ADK
    google_api_key: Optional[str] = Field(default="", description="Google AI API Key")
    google_genai_use_vertexai: bool = Field(default=False, description="Use Vertex AI")

    # Agent settings
    agent_name: str = Field(default="boiled-claw", description="Agent name")
    agent_model: str = Field(default="gemini-3.1-flash-lite-preview", description="Default model")

    # Gateway settings
    gateway_host: str = Field(default="127.0.0.1", description="Gateway host")
    gateway_port: int = Field(default=18789, description="Gateway port")
    gateway_ws_path: str = Field(default="/ws", description="WebSocket path")

    # Channels
    telegram_bot_token: Optional[str] = Field(default=None, description="Telegram bot token")
    discord_bot_token: Optional[str] = Field(default=None, description="Discord bot token")
    slack_bot_token: Optional[str] = Field(default=None, description="Slack bot token")
    slack_app_token: Optional[str] = Field(default=None, description="Slack app token")

    # Memory settings
    memory_db_path: Path = Field(default=Path("data/memory.db"), description="Memory DB path")
    memory_vector_dim: int = Field(default=768, description="Vector dimension")
    memory_embedding_model: str = Field(
        default="gemini-embedding-001",
        description="Embedding model for memory vectors",
    )
    self_improvement_canary_root: Path = Field(
        default=Path("data/canaries"),
        description="Root directory for offline self-improvement worktrees",
    )
    self_improvement_benchmark_timeout_seconds: int = Field(
        default=900,
        description="Default timeout for canary benchmark commands",
    )
    computer_trajectory_db_path: Path = Field(
        default=Path("data/computer_trajectories.db"),
        description="Browser-first computer-use trajectory DB path",
    )
    task_store_db_path: Path = Field(
        default=Path("data/tasks.db"),
        description="SQLite DB path for persistent workflow task objects",
    )

    # Subagent settings
    subagent_max_concurrent: int = Field(default=8, description="Max concurrent subagent runs globally")
    subagent_max_per_session: int = Field(default=5, description="Max concurrent subagent runs per requester session")
    subagent_max_spawn_depth: int = Field(default=2, description="Max subagent nesting depth (root agent=0, first subagent=1)")

    # Security settings
    audit_log_path: Path = Field(default=Path("data/audit.log"), description="Audit log path")
    shell_enabled: bool = Field(default=True, description="Enable shell execution")
    gateway_api_key: Optional[str] = Field(default=None, description="API key for gateway auth (empty = auth disabled)")
    gateway_auth_user_header: Optional[str] = Field(
        default=None,
        description=(
            "Trusted header containing the authenticated user id when gateway auth "
            "is enabled. If unset, all requests authenticated by the shared API key "
            "share a single derived principal."
        ),
    )
    file_workspace_paths: str = Field(
        default="",
        description="Comma-separated allowed file paths. Empty = blocklist only (no whitelist).",
    )

    # Redis settings (for future session store)
    redis_url: Optional[str] = Field(default=None, description="Redis URL")
    redis_session_namespace: str = Field(
        default="boiled-claw:sessions",
        description="Key namespace for Redis-backed ADK sessions",
    )

    # Browser settings
    browser_headless: bool = Field(default=True, description="Headless browser mode")
    browser_timeout: int = Field(default=30000, description="Browser timeout (ms)")
    browser_allow_loopback: bool = Field(
        default=False,
        description="Allow browser automation to access localhost / loopback URLs",
    )

    # Host / Desktop Bridge settings
    bridge_allow_remote_bind: bool = Field(
        default=False,
        description="Allow bridge services to bind to non-loopback addresses",
    )

    # Host Bridge settings
    host_bridge_enabled: bool = Field(default=False, description="Enable Host Bridge execution")
    host_bridge_url: Optional[str] = Field(
        default=None,
        description="Host Bridge MCP SSE endpoint URL",
    )
    host_bridge_timeout_seconds: int = Field(
        default=5,
        description="HTTP timeout for Host Bridge MCP connection",
    )
    host_bridge_sse_read_timeout_seconds: int = Field(
        default=300,
        description="SSE read timeout for Host Bridge MCP connection",
    )

    # Current Tab extension bridge settings
    current_tab_bridge_enabled: bool = Field(
        default=False,
        description="Enable the Chrome extension relay for current-tab control",
    )
    current_tab_bridge_host: str = Field(
        default="127.0.0.1",
        description="Bind host for the current-tab extension relay WebSocket server",
    )
    current_tab_bridge_port: int = Field(
        default=8768,
        description="Bind port for the current-tab extension relay WebSocket server",
    )
    current_tab_bridge_token: Optional[str] = Field(
        default=None,
        description="Optional shared token required by the Chrome Current Tab relay",
    )

    # Desktop Bridge settings
    desktop_bridge_enabled: bool = Field(
        default=False,
        description="Enable Desktop Bridge execution",
    )
    desktop_bridge_url: Optional[str] = Field(
        default=None,
        description="Desktop Bridge MCP SSE endpoint URL",
    )
    desktop_bridge_timeout_seconds: int = Field(
        default=5,
        description="HTTP timeout for Desktop Bridge MCP connection",
    )
    desktop_bridge_sse_read_timeout_seconds: int = Field(
        default=300,
        description="SSE read timeout for Desktop Bridge MCP connection",
    )

    # Physical AI adapter settings
    physical_ai_isaac_sim_url: Optional[str] = Field(
        default=None,
        description="Adapter endpoint for Isaac Sim simulation validation",
    )
    physical_ai_isaac_sim_status_url: Optional[str] = Field(
        default=None,
        description="Optional adapter endpoint for Isaac Sim validation status refresh",
    )
    physical_ai_osmo_url: Optional[str] = Field(
        default=None,
        description="Adapter endpoint for OSMO workflow orchestration",
    )
    physical_ai_osmo_status_url: Optional[str] = Field(
        default=None,
        description="Optional adapter endpoint for OSMO validation status refresh",
    )
    physical_ai_ros2_bridge_url: Optional[str] = Field(
        default=None,
        description="Adapter endpoint for ROS2 action dispatch",
    )
    physical_ai_timeout_seconds: int = Field(
        default=20,
        description="HTTP timeout for physical AI adapter calls",
    )
    physical_ai_validation_db_path: Path = Field(
        default=Path("data/physical_ai_validation.db"),
        description="SQLite DB path for persisted physical AI validation runs",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # ディレクトリ作成
        self.memory_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.self_improvement_canary_root.mkdir(parents=True, exist_ok=True)
        self.computer_trajectory_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.task_store_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.physical_ai_validation_db_path.parent.mkdir(parents=True, exist_ok=True)


# グローバルインスタンス
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """設定インスタンスを取得"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Clear the cached settings instance."""
    global _settings
    _settings = None
