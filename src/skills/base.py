"""
スキル基底クラス
OpenClaw のスキルシステムを参考
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from dataclasses import dataclass


@dataclass
class SkillMetadata:
    """スキルメタデータ"""
    name: str
    description: str
    version: str = "1.0.0"
    author: Optional[str] = None
    tags: List[str] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


class BaseSkill(ABC):
    """スキル基底クラス"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.enabled = True

    @abstractmethod
    def get_metadata(self) -> SkillMetadata:
        """スキルメタデータを取得"""
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """スキル実行"""
        pass

    def enable(self):
        """スキルを有効化"""
        self.enabled = True

    def disable(self):
        """スキルを無効化"""
        self.enabled = False

    async def validate_input(self, **kwargs) -> tuple[bool, Optional[str]]:
        """入力検証"""
        return True, None

    async def on_load(self):
        """スキル読み込み時のフック"""
        pass

    async def on_unload(self):
        """スキルアンロード時のフック"""
        pass


class SkillRegistry:
    """スキルレジストリ"""

    def __init__(self):
        self.skills: Dict[str, BaseSkill] = {}

    def register(self, skill: BaseSkill):
        """スキル登録"""
        metadata = skill.get_metadata()
        self.skills[metadata.name] = skill

    def get_skill(self, name: str) -> Optional[BaseSkill]:
        """スキル取得"""
        return self.skills.get(name)

    def list_skills(self) -> List[SkillMetadata]:
        """スキル一覧取得"""
        return [skill.get_metadata() for skill in self.skills.values()]

    def get_enabled_skills(self) -> List[BaseSkill]:
        """有効なスキル一覧"""
        return [skill for skill in self.skills.values() if skill.enabled]


# グローバルレジストリ
_skill_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    """スキルレジストリインスタンスを取得"""
    global _skill_registry
    if _skill_registry is None:
        _skill_registry = SkillRegistry()
    return _skill_registry
