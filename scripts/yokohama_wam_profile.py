"""Frozen, opt-in city adapter profile. No model or cloud execution on import."""

if __package__:
    from .ship_anwm import APPEARANCE_POLICY, MOTION_ADAPTER_SHA256, MOTION_CONTRACT
else:
    from ship_anwm import APPEARANCE_POLICY, MOTION_ADAPTER_SHA256, MOTION_CONTRACT

ADAPTER_NAMES = {
    "final_layer." + n
    for n in (
        "fuse_supervised.weight",
        "fuse_supervised.bias",
        "linear.weight",
        "linear.bias",
        "attn.mha.in_proj_weight",
        "attn.mha.in_proj_bias",
        "attn.mha.out_proj.weight",
        "attn.mha.out_proj.bias",
    )
} | {
    "blocks.27." + n
    for n in (
        "attn.qkv.weight",
        "attn.qkv.bias",
        "attn.proj.weight",
        "attn.proj.bias",
        "cttn.in_proj_weight",
        "cttn.in_proj_bias",
        "cttn.bias_k",
        "cttn.bias_v",
        "cttn.out_proj.weight",
        "cttn.out_proj.bias",
        "adaLN_modulation.1.weight",
        "adaLN_modulation.1.bias",
        "mlp.fc1.weight",
        "mlp.fc1.bias",
        "mlp.fc2.weight",
        "mlp.fc2.bias",
    )
}


def validate_service_profile(identity, profile, *, appearance_sha256, profile_sha256):
    if profile == "motion-v4":
        if (
            identity.get("adapter_sha256") != MOTION_ADAPTER_SHA256
            or identity.get("model_time_index") != 1
            or identity.get("appearance_policy") != APPEARANCE_POLICY
            or identity.get("appearance_sha256") != appearance_sha256
            or identity.get("profile_sha256") != profile_sha256
            or identity.get("candidate_contracts") != [MOTION_CONTRACT]
        ):
            raise ValueError("Motion WAM service differs from frozen adapter/input profile")
    elif profile != "legacy" or identity.get("adapter_sha256") is not None:
        raise ValueError("Adapted WAM requires explicit motion-v4 profile")
