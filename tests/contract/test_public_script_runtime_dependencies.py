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
