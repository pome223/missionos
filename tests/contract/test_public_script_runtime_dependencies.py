import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_python_scripts_do_not_call_missing_docker_compose_configuration() -> None:
    compose_configs = [
        path
        for pattern in ("*compose*.yml", "*compose*.yaml")
        for path in ROOT.glob(pattern)
    ]
    compose_callers = []
    for path in (ROOT / "scripts").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if '"docker", "compose"' in source or "docker compose" in source:
            compose_callers.append(path.relative_to(ROOT).as_posix())

    assert not compose_callers or compose_configs, (
        "public scripts invoke Docker Compose without a tracked root compose "
        f"configuration: {compose_callers}"
    )


def test_python_scripts_do_not_import_missing_repository_modules() -> None:
    missing: list[str] = []
    for path in (ROOT / "scripts").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if not module.startswith(("src.", "scripts.", "tests.")):
                    continue
                module_path = ROOT.joinpath(*module.split("."))
                if module_path.with_suffix(".py").is_file() or module_path.is_dir():
                    continue
                missing.append(f"{path.relative_to(ROOT)} -> {module}")

    assert not missing, "public scripts import missing repository modules: " + ", ".join(
        missing
    )
