"""Resident four-second NWM hold predictor for opt-in simulator experiments.

The base bundle supplies pinned dependencies, not qualification for this new
horizon. The separate adaptation manifest identifies the experimental weights.
No scenario, obstacle pose, speed, or future image is accepted by select().
"""

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from nwm_assets import CHECKPOINT_SHA256, VAE_SHA256, VAE_CONFIG_SHA256, digest
from nwm_assets import UPSTREAM_REVISION, encode_action


def exposure(image):
    import numpy as np

    rgb = np.asarray(image.convert("RGB").resize((224, 224))).astype(float) / 255
    crop = rgb[56:190, 78:146]
    return float(
        (
            (crop[:, :, 0] > 0.2)
            & (crop[:, :, 0] > 1.5 * crop[:, :, 1])
            & (crop[:, :, 0] > 1.5 * crop[:, :, 2])
        ).mean()
    )


class HoldPredictor:
    def __init__(self, bundle, adaptation):
        started = time.monotonic()
        import torch
        import numpy as np
        from diffusers import AutoencoderKL

        b = json.loads(bundle.read_text())
        upstream = (bundle.parent / b["upstream_directory"]).resolve()
        vae_path = (bundle.parent / b["vae_directory"]).resolve()
        m = json.loads(adaptation.read_text())
        checkpoint = (adaptation.parent / m["checkpoint_file"]).resolve()
        if (
            m["status"] != "complete"
            or m["base_sha256"] != CHECKPOINT_SHA256
            or checkpoint.parent != adaptation.resolve().parent
            or digest(checkpoint) != m["checkpoint_sha256"]
        ):
            raise ValueError("invalid hold adaptation provenance")
        if (
            digest(vae_path / "diffusion_pytorch_model.safetensors") != VAE_SHA256
            or digest(vae_path / "config.json") != VAE_CONFIG_SHA256
        ):
            raise ValueError("unexpected VAE")
        if (
            subprocess.check_output(
                ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
            ).strip()
            != UPSTREAM_REVISION
            or subprocess.check_output(
                ["git", "-C", str(upstream), "status", "--porcelain"], text=True
            ).strip()
        ):
            raise ValueError("upstream must match clean pinned revision")
        if not torch.backends.mps.is_available():
            raise RuntimeError("MPS required")
        sys.path.insert(0, str(upstream))
        cwd = Path.cwd()
        try:
            os.chdir(upstream)
            import misc
            from models import CDiT_models
            from diffusion import create_diffusion, gaussian_diffusion
        finally:
            os.chdir(cwd)
        self.transform = misc.transform
        self.model = CDiT_models["CDiT-S/2"](context_size=4, input_size=28, in_channels=4)
        self.model.load_state_dict(
            torch.load(checkpoint, map_location="cpu", weights_only=True)["ema"], strict=True
        )
        self.model = self.model.to("mps").eval()
        self.vae = (
            AutoencoderKL.from_pretrained(vae_path, local_files_only=True, use_safetensors=True)
            .to("mps")
            .eval()
        )
        self.diffusion = create_diffusion("ddim25")
        original = gaussian_diffusion._extract_into_tensor

        def extract(array, t, shape):
            return original(array.astype(np.float32), t, shape)

        gaussian_diffusion._extract_into_tensor = extract
        self.provenance = dict(
            checkpoint_sha256=m["checkpoint_sha256"],
            adaptation_manifest_sha256=digest(adaptation),
            upstream_revision=UPSTREAM_REVISION,
            device="mps",
            sampler="DDIM25",
            horizon_s=4,
            cloud_resources_created=False,
        )
        # Compile/cache device kernels before simulation starts, with synthetic inputs.
        from PIL import Image

        self.predict([Image.new("RGB", (320, 240))] * 4)
        self.provenance["load_and_warmup_wall_s"] = time.monotonic() - started

    def predict(self, images, seed=0):
        import torch
        from PIL import Image

        with torch.inference_mode():
            torch.manual_seed(seed)
            frames = torch.stack([self.transform(im.convert("RGB")) for im in images]).to("mps")
            context = self.vae.encode(frames).latent_dist.sample().mul_(0.18215).unsqueeze(0)
            noise = torch.randn(1, 4, 28, 28, device="mps")
            kwargs = dict(
                x_cond=context,
                y=torch.tensor([encode_action(0, 0, 0)], dtype=torch.float32, device="mps"),
                rel_t=torch.tensor([0.125], device="mps"),
            )
            samples = self.diffusion.ddim_sample_loop(
                self.model.forward,
                noise.shape,
                noise,
                clip_denoised=False,
                model_kwargs=kwargs,
                progress=False,
                device="mps",
            )
            pixels = self.vae.decode(samples / 0.18215).sample.clamp(-1, 1)
            if not torch.isfinite(pixels).all().item():
                raise ValueError("nonfinite prediction")
            array = ((pixels[0].cpu() + 1) * 127.5).byte().permute(1, 2, 0).numpy()
            torch.mps.synchronize()
            return Image.fromarray(array)

    def select(self, request, output):
        from PIL import Image

        started = time.monotonic()
        if set(request) != {"policy", "context", "timestamps"} or request["policy"] not in (
            "nwm",
            "image_only",
        ):
            raise ValueError("only camera history, timestamps and policy are accepted")
        ts = request["timestamps"]
        if (
            len(request["context"]) != 4
            or len(ts) != 4
            or not all(math.isfinite(t) for t in ts)
            or any(abs(b - a - 0.25) > 0.05 for a, b in zip(ts, ts[1:]))
        ):
            raise ValueError("four aligned 4 Hz frames required")
        images = [Image.open(p).convert("RGB") for p in request["context"]]
        current = exposure(images[-1])
        predicted = None
        if request["policy"] == "nwm":
            image = self.predict(images)
            image.save(output / "hold-prediction.png")
            predicted = exposure(image)
        candidate = (
            "direct"
            if current <= 0.06
            else "wait"
            if predicted is not None and predicted <= 0.06
            else "detour"
        )
        return dict(
            candidate=candidate,
            source=request["policy"] + "_four_second_hold_passability",
            current_exposure=current,
            predicted_exposure=predicted,
            threshold=0.06,
            learned_wam_invoked=request["policy"] == "nwm",
            hypothetical_action=[0, 0, 0],
            prediction_horizon_s=4,
            wait_time_origin="last_context_timestamp",
            latency_wall_s=time.monotonic() - started,
            safety_probability=None,
            provenance=self.provenance if predicted is not None else None,
        )
