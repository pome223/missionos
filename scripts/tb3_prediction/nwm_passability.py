"""NWM CDiT task head predicting a future obstacle mask under a four-second hold.

This changes the diffusion objective to supervised future-state prediction. It
is neither stock NWM image generation nor a calibrated collision probability.
The frozen VAE and adapted CDiT are identified separately in the manifest.
"""

import json
import time

from nwm_hold_predict import HoldPredictor, exposure
from nwm_assets import digest, CHECKPOINT_SHA256
from nwm_assets import encode_action


def mask_tensor(image):
    import torch
    import numpy as np
    import torch.nn.functional as F

    rgb = np.asarray(image.convert("RGB").resize((224, 224))).astype(float) / 255
    red = (
        (rgb[:, :, 0] > 0.2)
        & (rgb[:, :, 0] > 1.5 * rgb[:, :, 1])
        & (rgb[:, :, 0] > 1.5 * rgb[:, :, 2])
    )
    return F.adaptive_avg_pool2d(torch.tensor(red.astype("float32"))[None, None], (28, 28))[0]


def mask_score(mask):
    import torch.nn.functional as F

    return float(
        F.interpolate(mask[None], size=(224, 224), mode="bilinear", align_corners=False)[
            0, 0, 56:190, 78:146
        ]
        .mean()
        .item()
    )


def forward(model, contexts, motion_scale=1.0):
    import torch

    n = len(contexts)
    return model(
        contexts[:, -1],
        torch.zeros(n, device="mps"),
        y=torch.tensor([encode_action(0, 0, 0)] * n, dtype=torch.float32, device="mps"),
        x_cond=contexts[:, -1:] + (contexts - contexts[:, -1:]) * motion_scale,
        rel_t=torch.full((n,), 0.125, device="mps"),
    )[:, :1]


class PassabilityPredictor(HoldPredictor):
    def __init__(self, bundle, manifest):
        import torch

        m = json.loads(manifest.read_text())
        if (
            m.get("head_kind") != "NWM_CDiT_future_obstacle_mask_hold_4s"
            or m["status"] != "complete"
            or m["base_sha256"] != CHECKPOINT_SHA256
        ):
            raise ValueError("incompatible task head manifest")
        parent = (manifest.parent / m["initial_adaptation_manifest"]).resolve()
        if digest(parent) != m["initial_adaptation_manifest_sha256"]:
            raise ValueError("initial adaptation changed")
        self.motion_scale = m.get("input_motion_scale", 1.0)
        self.context_period = m.get("context_period_s", 0.25)
        super().__init__(bundle, parent)
        checkpoint = (manifest.parent / m["checkpoint_file"]).resolve()
        if (
            checkpoint.parent != manifest.resolve().parent
            or digest(checkpoint) != m["checkpoint_sha256"]
        ):
            raise ValueError("task checkpoint changed")
        self.model.load_state_dict(
            torch.load(checkpoint, map_location="cpu", weights_only=True)["ema"], strict=True
        )
        self.model.eval().requires_grad_(False)
        self.provenance.update(
            checkpoint_sha256=m["checkpoint_sha256"],
            head_kind=m["head_kind"],
            input_motion_scale=self.motion_scale,
            context_period_s=self.context_period,
            task_manifest_sha256=digest(manifest),
            sampler="single deterministic CDiT forward; supervised future mask",
        )
        from PIL import Image

        self.predict_mask([Image.new("RGB", (320, 240))] * 4)

    def predict_mask(self, images):
        import torch

        with torch.inference_mode():
            frames = torch.stack([self.transform(im.convert("RGB")) for im in images]).to("mps")
            contexts = self.vae.encode(frames).latent_dist.mode().mul_(0.18215)[None]
            mask = forward(self.model, contexts, self.motion_scale).sigmoid()[0]
            if not torch.isfinite(mask).all().item():
                raise ValueError("nonfinite future obstacle mask")
            torch.mps.synchronize()
            return mask.cpu()

    def select(self, request, output):
        # Reuse strict online input validation without invoking image generation.
        import math
        import numpy as np
        from PIL import Image

        started = time.monotonic()
        if set(request) != {"policy", "context", "timestamps"} or request["policy"] not in (
            "nwm",
            "image_only",
            "image_history",
        ):
            raise ValueError("only camera history, timestamps and policy accepted")
        ts = request["timestamps"]
        if (
            len(request["context"]) != 4
            or len(ts) != 4
            or not all(math.isfinite(t) for t in ts)
            or any(abs(b - a - self.context_period) > 0.05 for a, b in zip(ts, ts[1:]))
        ):
            raise ValueError(f"four aligned {1 / self.context_period:g} Hz frames required")
        images = [Image.open(p).convert("RGB") for p in request["context"]]
        current = exposure(images[-1])
        score = None
        if request["policy"] == "nwm":
            mask = self.predict_mask(images)
            score = mask_score(mask)
            Image.fromarray((mask[0].numpy() * 255).astype(np.uint8)).resize((224, 224)).save(
                output / "future-obstacle-mask.png"
            )
        from camera_history_policy import (
            assessment,
            history_decision,
            learned_decision,
            selection_basis,
        )

        assessed = assessment(images, ts)
        motion_guard = assessed["motion"]
        candidate = (
            learned_decision(current, score, assessed)
            if score is not None
            else history_decision(current, assessed)
            if request["policy"] == "image_history"
            else "direct"
            if current <= 0.06
            else "detour"
        )
        return dict(
            candidate=candidate,
            selection_basis=(
                "current_image_only"
                if request["policy"] == "image_only"
                else selection_basis(candidate, assessed, learned=score is not None)
            ),
            image_motion_consistency=motion_guard,
            approaching_obstacle=assessed["incoming"],
            source=(
                "NWM_future_obstacle_mask"
                if score is not None
                else "image_history_projection"
                if request["policy"] == "image_history"
                else "current_image_only"
            ),
            current_exposure=current,
            predicted_exposure=score,
            threshold=0.06,
            forecast_exposure_threshold=0.2,
            post_wait_observation_required=True,
            learned_wam_invoked=score is not None,
            hypothetical_action=[0, 0, 0],
            prediction_horizon_s=4,
            wait_time_origin="last_context_timestamp",
            latency_wall_s=time.monotonic() - started,
            safety_probability=None,
            provenance=self.provenance if score is not None else None,
        )
