"""
スキルローダー
スキルの動的読み込み
"""

import importlib
import importlib.util
from pathlib import Path
from typing import List, Optional
import yaml

from src.skills.base import BaseSkill, SkillMetadata, get_skill_registry


class MarkdownSkill(BaseSkill):
    """SKILL.md 形式のスキル"""

    def __init__(self, skill_file: Path, metadata: dict, content: str):
        super().__init__()
        self.skill_file = skill_file
        self.metadata = metadata
        self.content = content

    def get_metadata(self) -> SkillMetadata:
        # Markdown skills are instruction-first: execute() returns the SKILL.md
        # body so callers such as skill_spawn can reuse it as an agent prompt.
        # Python skills are separate and can define their own behavior.
        return SkillMetadata(
            name=self.metadata.get("name", self.skill_file.parent.name),
            description=self.metadata.get(
                "description",
                f"Skill loaded from {self.skill_file}",
            ),
            version=self.metadata.get("version", "1.0.0"),
            author=self.metadata.get("author"),
            tags=self.metadata.get("tags", []),
        )

    async def execute(self, **kwargs):
        task = kwargs.get("task", "")
        return {
            "kind": "markdown_skill",
            "skill_file": str(self.skill_file),
            "task": task,
            "content": self.content,
        }


class SkillLoader:
    """スキルローダー"""

    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)
        self.registry = get_skill_registry()

    async def load_skill_from_file(self, file_path: Path) -> Optional[BaseSkill]:
        """ファイルからスキルを読み込む"""
        try:
            # モジュールを動的インポート
            module_name = f"skill_module_{file_path.stem}"
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # BaseSkillを継承したクラスを探す
                for name in dir(module):
                    obj = getattr(module, name)
                    if (
                        isinstance(obj, type)
                        and issubclass(obj, BaseSkill)
                        and obj is not BaseSkill
                    ):
                        # スキルインスタンス化
                        skill = obj()
                        await skill.on_load()
                        self.registry.register(skill)
                        return skill

        except Exception as e:
            print(f"Failed to load skill from {file_path}: {e}")

        return None

    async def load_skill_from_markdown(self, file_path: Path) -> Optional[BaseSkill]:
        """SKILL.md からスキルを読み込む"""
        try:
            text = file_path.read_text(encoding="utf-8")
            metadata = {}
            content = text

            if text.startswith("---"):
                end = text.find("\n---", 3)
                if end != -1:
                    frontmatter = text[3:end].strip()
                    content = text[end + 4:].lstrip("\n")
                    loaded = yaml.safe_load(frontmatter) or {}
                    if isinstance(loaded, dict):
                        metadata = loaded

            skill = MarkdownSkill(
                skill_file=file_path,
                metadata=metadata,
                content=content,
            )
            await skill.on_load()
            self.registry.register(skill)
            return skill
        except Exception as e:
            print(f"Failed to load skill from {file_path}: {e}")
            return None

    async def load_skills_from_directory(self, directory: Optional[Path] = None) -> List[BaseSkill]:
        """ディレクトリから全スキルを読み込む"""
        directory = directory or self.skills_dir

        if not directory.exists():
            return []

        loaded_skills = []

        # 旧形式: skills/*.py
        for file_path in directory.glob("*.py"):
            if file_path.name.startswith("_"):
                continue

            skill = await self.load_skill_from_file(file_path)
            if skill:
                loaded_skills.append(skill)

        # OpenClaw 形式: skills/<name>/SKILL.md
        for skill_dir in directory.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            skill = await self.load_skill_from_markdown(skill_file)
            if skill:
                loaded_skills.append(skill)

        return loaded_skills

    async def unload_skill(self, skill_name: str) -> bool:
        """スキルをアンロード"""
        skill = self.registry.get_skill(skill_name)

        if skill:
            await skill.on_unload()
            del self.registry.skills[skill_name]
            return True

        return False

    async def reload_skill(self, skill_name: str, file_path: Path) -> Optional[BaseSkill]:
        """スキルをリロード"""
        await self.unload_skill(skill_name)
        return await self.load_skill_from_file(file_path)
