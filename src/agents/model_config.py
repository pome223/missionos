"""
モデル設定管理
Gemini の既定モデル設定を管理する
"""

import os
import re
from typing import Optional, Dict, Any
from dataclasses import dataclass

from src.config.settings import get_settings


@dataclass
class GeminiModelConfig:
    """Gemini モデル設定"""

    name: str
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None

    def to_generation_config(self) -> Dict[str, Any]:
        """ADK の generation_config 形式に変換"""
        config = {
            "temperature": self.temperature,
        }
        if self.max_tokens:
            config["max_output_tokens"] = self.max_tokens
        if self.top_p:
            config["top_p"] = self.top_p
        if self.top_k:
            config["top_k"] = self.top_k
        return config


# デフォルトモデル設定
_DEFAULT_MODEL_NAME = get_settings().agent_model

DEFAULT_MODEL = GeminiModelConfig(
    name=_DEFAULT_MODEL_NAME,
    temperature=0.7,
)

# 高精度モデル設定
PRECISE_MODEL = GeminiModelConfig(
    name=_DEFAULT_MODEL_NAME,
    temperature=0.2,
    top_k=20,
)

# 創造的モデル設定
CREATIVE_MODEL = GeminiModelConfig(
    name=_DEFAULT_MODEL_NAME,
    temperature=1.2,
)


def get_model_config(name: str = "default") -> GeminiModelConfig:
    """モデル設定を取得"""
    configs = {
        "default": DEFAULT_MODEL,
        "precise": PRECISE_MODEL,
        "creative": CREATIVE_MODEL,
    }
    return configs.get(name, DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# ローカル LLM バックエンド切替（Gemini / Ollama / MLX）
#
# env で切り替える。既定は "ollama" なので未設定ならローカル優先。
#   MISSIONOS_LLM_BACKEND      gemini | ollama | mlx | off
#   MISSIONOS_LOCAL_MODEL      ローカルモデル ID（LiteLlm 形式）
#   MISSIONOS_LOCAL_API_BASE   ローカル推論サーバの base URL
#   MISSIONOS_OLLAMA_MODEL     Ollama モデルタグ（例: gemma4:26b）
#   MISSIONOS_OLLAMA_BASE_URL  Ollama base URL
#
# 旧名 BOILED_CLAW_* も互換として読む。
#
# ローカルは ADK の LiteLlm ラッパー経由で呼ぶ（要 `google-adk[extensions]`）。
# Ollama 例:  ollama serve + `ollama pull gemma4:26b`
# MLX 例:     `mlx_lm.server` の OpenAI 互換エンドポイント
# ---------------------------------------------------------------------------

_LOCAL_BACKENDS = {"ollama", "mlx"}
_OFF_BACKENDS = {"off", "none", "disabled", "deterministic"}
_DEFAULT_LOCAL_MODELS = {
    # Gemma 4 26B MoE（active 3.8B）を既定に。実際の Ollama タグに合わせて上書き可。
    "ollama": "ollama_chat/gemma4:26b",
    "mlx": "openai/mlx-community/gemma-4-26b",
}
_DEFAULT_LOCAL_API_BASES = {
    "ollama": "http://localhost:11434",
    "mlx": "http://localhost:8080/v1",
}


def _agent_env_suffix(agent_name: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", agent_name or "").strip("_").upper()


def _agent_env(agent_name: str | None, key: str) -> str:
    suffix = _agent_env_suffix(agent_name)
    if not suffix:
        return ""
    return os.environ.get(f"MISSIONOS_AGENT_{suffix}_{key}", "").strip()


def _llm_backend(agent_name: str | None = None) -> str:
    backend = (
        _agent_env(agent_name, "LLM_BACKEND")
        or os.environ.get("MISSIONOS_LLM_BACKEND")
        or os.environ.get("BOILED_CLAW_LLM_BACKEND")
        or "off"
    ).strip().lower()
    if backend in {"google", "google_adk"}:
        return "gemini"
    return backend


def google_llm_backend_enabled(agent_name: str | None = None) -> bool:
    """Return True when MissionOS should use the Google/Gemini ADK backend."""
    return _llm_backend(agent_name) == "gemini"


def local_llm_backend_enabled(agent_name: str | None = None) -> bool:
    """Return True when MissionOS should route ADK calls through a local backend."""
    return _llm_backend(agent_name) in _LOCAL_BACKENDS


def llm_backend_disabled(agent_name: str | None = None) -> bool:
    """Return True when MissionOS should avoid LLM-backed ADK calls."""
    return _llm_backend(agent_name) in _OFF_BACKENDS


def _local_model_for_backend(backend: str, agent_name: str | None = None) -> str:
    model = (
        _agent_env(agent_name, "LOCAL_MODEL")
        or (
            _agent_env(agent_name, "OLLAMA_MODEL")
            if backend == "ollama"
            else ""
        )
        or _agent_env(agent_name, "MODEL")
        or os.environ.get("MISSIONOS_LOCAL_MODEL")
        or (
            os.environ.get("MISSIONOS_OLLAMA_MODEL")
            if backend == "ollama"
            else None
        )
        or os.environ.get("BOILED_CLAW_LOCAL_MODEL")
        or _DEFAULT_LOCAL_MODELS[backend]
    ).strip()
    if backend == "ollama" and "/" not in model:
        return f"ollama_chat/{model}"
    return model


def _local_api_base_for_backend(backend: str, agent_name: str | None = None) -> str:
    return (
        _agent_env(agent_name, "LOCAL_API_BASE")
        or (
            _agent_env(agent_name, "OLLAMA_BASE_URL")
            if backend == "ollama"
            else ""
        )
        or os.environ.get("MISSIONOS_LOCAL_API_BASE")
        or (
            os.environ.get("MISSIONOS_OLLAMA_BASE_URL")
            if backend == "ollama"
            else None
        )
        or os.environ.get("BOILED_CLAW_LOCAL_API_BASE")
        or _DEFAULT_LOCAL_API_BASES[backend]
    ).strip()


def _agent_model_override(agent_name: str | None) -> str:
    return _agent_env(agent_name, "MODEL_ID") or _agent_env(agent_name, "MODEL")


def agent_model_label(
    model_id: Optional[str] = None,
    *,
    agent_name: str | None = None,
) -> str:
    """表示・evidence 用のモデル識別子文字列を返す（オブジェクトではなく str）。"""
    backend = _llm_backend(agent_name)
    if backend in _LOCAL_BACKENDS:
        return _local_model_for_backend(backend, agent_name)
    return (_agent_model_override(agent_name) or model_id or DEFAULT_MODEL.name).strip()


def llm_provider_label(agent_name: str | None = None) -> str:
    """Return the provider label recorded in invocation evidence."""
    backend = _llm_backend(agent_name)
    if backend in _LOCAL_BACKENDS:
        return f"google_adk_litellm_{backend}"
    if backend in _OFF_BACKENDS:
        return "disabled"
    return "google_adk_gemini"


def resolve_agent_model(
    model_id: Optional[str] = None,
    *,
    agent_name: str | None = None,
) -> Any:
    """ADK Agent の `model=` に渡す値を解決する。

    - backend=gemini（既定）: モデル名の文字列をそのまま返す（従来挙動）。
    - backend=ollama/mlx: `LiteLlm` インスタンスを返す（要 google-adk[extensions]）。

    ローカル時に Gemini モデル名 (`model_id`) は無視され、env のローカルモデルを使う。
    """
    backend = _llm_backend(agent_name)
    if backend not in _LOCAL_BACKENDS:
        return _agent_model_override(agent_name) or model_id or DEFAULT_MODEL.name

    local_model = _local_model_for_backend(backend, agent_name)
    api_base = _local_api_base_for_backend(backend, agent_name)

    try:
        from google.adk.models.lite_llm import LiteLlm
    except ImportError as exc:  # pragma: no cover - 環境依存
        raise RuntimeError(
            "ローカル LLM バックエンド (MISSIONOS_LLM_BACKEND="
            f"{backend}) には google-adk[extensions] が必要です。"
            "`python -m pip install -e '.[local-llm]'` を実行してください。"
        ) from exc

    return LiteLlm(model=local_model, api_base=api_base)
