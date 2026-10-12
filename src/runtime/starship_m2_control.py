"""Catalog and worker input binding for the separately approved M2 boundary."""

from .starship_replanning import digest

SCENARIOS = {
    "sixdof_m2_" + case: case
    for case in ("normal", "operations_notice", "release_fault", "hold_resume")
}
SCOPE = "local_simulation_deployment_to_observed_return_continuation"


def plan_fields(mode, case, source_sha256):
    from .starship_m2_contract import PROFILE, m2_contract

    authority = m2_contract(mode, case)
    from src.intelligence.starship_m2_broker import require_live_configuration
    require_live_configuration(authority)
    return {
        "backend": "starship_deployment_return_continuation",
        "mission_envelope": authority,
        "release_policy": "observed_deployment_inventory_then_same_plant_bounded_return",
        "model_authority": "bounded_deployment_and_return",
        "jev_authority": "bounded_deployment_and_return",
        "simulation": {
            "case": case,
            "profile": PROFILE,
            "profile_sha256": source_sha256[PROFILE],
            "dt_scale": 1.0,
            "maximum_wall_time_s": 2400,
        },
        "verification_scope": "observed_inventory_same_plant_fresh_return_admission_contact_and_area",
        "limitations": [
            "放出と帰還を一度の承認に束縛します。分離の受付を実際の放出数として扱いません。",
            "Jevは放出を判断し、帰還では数値予測を道具に使います。期限不足・未応答は独立チェックを通る代替動作へ移ります。",
            "回収区域と待機資源は開発用の仮定です。帰還候補が成立しなければ未解決と記録します。実機権限はありません。",
        ],
    }


def validate_worker_input(authority, *, mode, case, run_id, mailbox):
    """Match actual CLI args to one complete canonical authority before work."""
    from pathlib import Path
    import json
    from .starship_m2_contract import m2_contract

    if digest(authority) != digest(m2_contract(mode, case)):
        raise ValueError("m2_worker_authority_args_mismatch")
    if (
        type(run_id) is not str
        or not 1 <= len(run_id) <= 80
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in run_id
        )
    ):
        raise ValueError("m2_worker_run_identity")
    if mode == "live":
        if mailbox is None:
            raise ValueError("m2_live_phase_mailboxes_required")
        root = Path(mailbox)
        if (
            root.is_symlink()
            or not root.is_dir()
            or any(
                (root / phase).is_symlink() or not (root / phase).is_dir()
                for phase in ("deployment", "return")
            )
        ):
            raise ValueError("m2_live_phase_mailboxes_required")
        binding = json.loads((root / "authority.json").read_text())
        if binding != {"run_id": run_id, "authority_sha256": digest(authority)}:
            raise ValueError("m2_mailbox_run_authority_mismatch")
    return authority
