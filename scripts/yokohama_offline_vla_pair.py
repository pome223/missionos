"""Pure offline image-pair boundary; no model loading, network or dispatch.

Real inference needs a separately qualified, pinned service. These helpers do
not enable the operational visual contract or its two-second response lease.
"""

import hashlib
import io
import math
import re

from PIL import Image

PROMPT = "<image>\nFly straight ahead toward the approved nearby goal. Maintain height.\nAction: "
GENERATION = {
    "do_sample": False,
    "max_new_tokens": 20,
    "generation_action_mask": None,
}


def model_payload(images):
    """Only identical neutral instructions and the two RGB byte strings cross."""
    if set(images) != {"rgb", "down"} or any(type(v) is not bytes for v in images.values()):
        raise ValueError("Only dual-view image bytes are allowed")
    if sum(len(v) for v in images.values()) > 1024**2:
        raise ValueError("Offline image payload exceeds transfer bound")
    for raw in images.values():
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.mode != "RGB" or image.size != (640, 360):
                raise ValueError("Unexpected dual-view RGB PNG")
            image.verify()
    return {"prompt": PROMPT, "images": dict(images)}


def review_pair(records):
    """Evaluation labels/pose/goal remain outside the inference payload."""
    if len(records) != 2 or [r.get("label") for r in records] != ["open", "blocked"]:
        raise ValueError("Exactly one fixed open/blocked pair is required")
    first = records[0]
    for record in records:
        if set(record) != {"label", "images", "camera_pose", "goal"}:
            raise ValueError("Unexpected evaluation metadata or expected answer")
        pose, goal = record["camera_pose"], record["goal"]
        if (
            len(pose) != 6
            or len(goal) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in (*pose, *goal))
            or not math.isclose(math.dist(pose[:2], goal[:2]), 8, abs_tol=1e-6)
            or pose != first["camera_pose"]
            or goal != first["goal"]
        ):
            raise ValueError("Pair must retain the same camera and eight-metre goal")
    payloads = [model_payload(r["images"]) for r in records]
    if payloads[0]["images"] == payloads[1]["images"]:
        raise ValueError("Pair images must actually differ")
    return payloads


def analyze_raw(text):
    """Keep exact text; describe one optional EOS without repairing an answer."""
    if type(text) is not str or len(text) > 4096:
        raise ValueError("Bounded raw text required")
    result = {"raw_text": text, "dispatch_authorized": False}
    match = re.fullmatch(
        r"(0|[1-9][0-9]?) (47|48|49|50|51) (45|46|47|48|49|50|51|52|53)(</s>)?", text
    )
    if match:
        bins = list(map(int, match.groups()[:3]))
        result["EOS_marker"] = match.group(4)
        if bins == [0, 49, 49]:
            return {**result, "recognized": True, "kind": "hold", "bins": bins}
        if 20 <= bins[0] <= 58:
            return {**result, "recognized": True, "kind": "translation", "bins": bins}
    return {**result, "recognized": False, "reason": "unsupported_exact_raw_text"}


def compare(records, predict):
    """At most two calls; exceptions end the attempt without a retry."""
    payloads = review_pair(records)
    outcomes = []
    for record, payload in zip(records, payloads, strict=True):
        hashes = {k: hashlib.sha256(v).hexdigest() for k, v in payload["images"].items()}
        response = predict(payload)
        if payload != model_payload(record["images"]):
            raise ValueError("Inference callback altered the fixed input")
        outcomes.append(
            {
                "evaluation_label": record["label"],
                "input_images_sha256": hashes,
                "raw_response": response,
                "analysis": analyze_raw(response["generated_text"]),
            }
        )
    return {
        "outcomes": outcomes,
        "raw_text_differs": outcomes[0]["analysis"]["raw_text"]
        != outcomes[1]["analysis"]["raw_text"],
        "inference_proven_by_this_helper": False,
        "dispatch_authorized": False,
        "operational_response_lease_qualified": False,
        "avoidance_performance_qualified": False,
    }
