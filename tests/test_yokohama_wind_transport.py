"""Exercise the production publisher method without requiring Gazebo bindings."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("response_kind", ["recover", "missing", "wrong_seed"])
def test_seed_confirmation_is_bounded_and_requires_matching_response(response_kind):
    tree = ast.parse((Path(__file__).parents[1] / "scripts/yokohama_sitl_worker.py").read_text())
    method = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "activate_wind"
    )
    clock = SimpleNamespace(value=0.0)

    def sleep(dt):
        clock.value += dt

    def wind():
        return SimpleNamespace(enable_wind=False, linear_velocity=SimpleNamespace(x=0, y=0, z=0))

    ns = {
        "Wind": wind,
        "Empty": lambda: None,
        "time": SimpleNamespace(monotonic=lambda: clock.value, sleep=sleep),
    }
    exec(
        compile(ast.Module(body=[method], type_ignores=[]), "production_activate_wind", "exec"), ns
    )
    requests, published = [], []

    def request(*args):
        requests.append(args)
        sleep(0.5)
        response = wind()
        response.enable_wind = True
        response.linear_velocity.x = 6 if response_kind != "wrong_seed" else 3
        return response_kind != "missing" and len(requests) > 1, response

    fake = SimpleNamespace(
        wind_publisher=SimpleNamespace(
            publish=lambda msg: published.append(
                [msg.linear_velocity.x, msg.linear_velocity.y, msg.linear_velocity.z]
            )
        ),
        node=SimpleNamespace(request=request),
        snapshot=lambda: {"sim_s": clock.value},
    )
    receipt = ns["activate_wind"](fake, [6, 0, 0])
    assert receipt["confirmed"] == (response_kind == "recover")
    assert 2 <= receipt["confirmation_attempts"] <= 5
    assert receipt["end_sim_s"] <= 3.5 + 1e-8
    assert all(v == [6, 0, 0] for v in published)
