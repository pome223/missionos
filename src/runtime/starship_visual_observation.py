"""Bounded synthetic camera pixels and arrival-aware, content-addressed evidence.

The renderer is deterministic NumPy triangle projection with a depth buffer;
Pillow only encodes PNG. Cameras and opaque surfaces are fixed geometry, not
diagnosis overlays. This synthetic internal bay is not Starship CAD/optics.
World provenance stays private; provider frames carry no truth state or labels.
"""
from collections import deque
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import stat
import time

import numpy as np
from PIL import Image, __version__ as PILLOW_VERSION

from .starship_deployment_world import canonical, digest

FRAME_SCHEMA = "missionos.starship_visual_frame.v1"
EVIDENCE_SCHEMA = "missionos.starship_visual_evidence.v1"
MAX_FRAME_BYTES = 256 * 1024
MAX_REQUEST_FRAMES = 6
FRAME_KEYS = {"schema", "frame_id", "run_id", "vehicle_id", "camera_id", "camera_config_sha256",
              "capture_clock_id", "capture_sim_time_s", "available_sim_time_s", "source_sequence",
              "image_sha256", "image_bytes", "mime_type", "width", "height",
              "renderer_source_sha256", "capture_status"}
DEFAULT_CONFIG = {"fps": 2., "latency_s": .5, "history_s": 30., "max_age_s": 5.,
                  "ring_bytes": 64 * 1024 * 1024, "store_bytes": 128 * 1024 * 1024,
                  "maximum_captures": 10000, "maximum_pinned_frames": 512}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _identifier(value):
    return type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) is not None


def _hash(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def default_cameras(*, width=640, height=360):
    """Two fixed internal views, measured relative to the physical attachment.

    The open bay, supports and exit aperture are declared toy geometry. Retained
    target geometry stays at its physical attachment; neither camera moves it.
    lens_covered inserts an opaque near-camera plane into the same depth buffer.
    """
    return [{"camera_id": name, "width": width, "height": height, "horizontal_fov_deg": 75.,
             "near_m": .05, "far_m": 120., "eye_offset_m": [-2., side*4., 2.8],
             "look_offset_m": [3., 0., 0.], "up_body": [0., 0., 1.], "lens_covered": False}
            for name, side in (("deployment_port_primary", -1), ("deployment_port_secondary", 1))]


def validate_camera(camera):
    keys = {"camera_id", "width", "height", "horizontal_fov_deg", "near_m", "far_m",
            "eye_offset_m", "look_offset_m", "up_body", "lens_covered"}
    if type(camera) is not dict or set(camera) != keys or not _identifier(camera["camera_id"]):
        raise ValueError("invalid_visual_camera")
    if (any(type(camera[k]) is not int or not 64 <= camera[k] <= 1024 for k in ("width", "height"))
            or camera["width"]*camera["height"] > 640*640
            or not _number(camera["horizontal_fov_deg"]) or not 10 <= camera["horizontal_fov_deg"] <= 120
            or not _number(camera["near_m"]) or not _number(camera["far_m"])
            or not 0 < camera["near_m"] < camera["far_m"] <= 1000
            or type(camera["lens_covered"]) is not bool):
        raise ValueError("invalid_visual_camera_limits")
    for key in ("eye_offset_m", "look_offset_m", "up_body"):
        if (type(camera[key]) not in (list, tuple) or len(camera[key]) != 3
                or any(not _number(v) or abs(v) > 1000 for v in camera[key])):
            raise ValueError("invalid_visual_camera_vector")
    direction = np.asarray(camera["look_offset_m"])-np.asarray(camera["eye_offset_m"])
    if np.linalg.norm(direction) < 1e-6 or np.linalg.norm(np.cross(direction, camera["up_body"])) < 1e-6:
        raise ValueError("degenerate_visual_camera")
    return deepcopy(camera)


def _box(center, dimensions, color, rotation=None):
    vertices = np.array([[x, y, z] for x in (-.5, .5) for y in (-.5, .5) for z in (-.5, .5)])
    vertices *= dimensions
    if rotation is not None:
        vertices = vertices @ np.asarray(rotation).T
    vertices += center
    faces = ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1),
             (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3))
    result = []
    for i, (a, b, c, d) in enumerate(faces):
        shade = np.clip(np.asarray(color)*(0.62+0.07*i), 0, 255).astype(np.uint8)
        result.extend(((vertices[[a, b, c]], shade), (vertices[[a, c, d]], shade)))
    return result


def _clip_z(vertices, plane, keep_above):
    """Clip in camera space before projection; opaque geometry cannot see through."""
    output = []
    for a, b in zip(vertices, [*vertices[1:], vertices[0]]):
        inside_a = a[2] >= plane if keep_above else a[2] <= plane
        inside_b = b[2] >= plane if keep_above else b[2] <= plane
        if inside_a:
            output.append(a)
        if inside_a != inside_b:
            output.append(a+(b-a)*((plane-a[2])/(b[2]-a[2])))
    return output


def render_camera(scene, camera):
    """Return actual PNG pixels. Scene is private; no labels/text are rendered."""
    camera = validate_camera(camera)
    anchor = np.asarray(scene["attachment_body_m"], dtype=float)
    eye = anchor+camera["eye_offset_m"]
    forward = np.asarray(camera["look_offset_m"])-np.asarray(camera["eye_offset_m"])
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, camera["up_body"])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    basis = np.stack((right, up, forward))
    triangles = _box(np.asarray(scene["payload_center_body_m"]), scene["payload_dimensions_m"],
                     (175, 180, 188), np.asarray(scene["payload_rotation_columns"]).T)
    # Fixed internal bay floor, two thin supports, and a frame around the exit.
    # No opaque hull is drawn and then bypassed: this is explicitly an open bay.
    for offset, size, color in [
        ([1., 0., -.65], [12., 7., .15], (65, 74, 85)),
        ([0., -1.8, -.3], [6., .12, .5], (110, 120, 128)),
        ([0., 1.8, -.3], [6., .12, .5], (110, 120, 128)),
        ([4.5, -2.2, .3], [.15, .2, 2.2], (100, 110, 120)),
        ([4.5, 2.2, .3], [.15, .2, 2.2], (100, 110, 120)),
        ([4.5, 0., 1.4], [.15, 4.6, .2], (100, 110, 120)),
    ]:
        triangles.extend(_box(anchor+offset, size, color))
    width, height = camera["width"], camera["height"]
    pixels = np.full((height, width, 3), (8, 12, 20), dtype=np.uint8)
    depth = np.full((height, width), np.inf)
    focal = width/(2*math.tan(math.radians(camera["horizontal_fov_deg"])/2))
    camera_triangles = [((vertices-eye)@basis.T, color) for vertices, color in triangles]
    if camera["lens_covered"]:
        # A physical opaque lens cover in camera coordinates, not a result icon.
        distance = camera["near_m"]*2
        half = distance*math.tan(math.radians(camera["horizontal_fov_deg"])/2)*2
        cover = np.array([[-half, -half, distance], [half, -half, distance],
                          [half, half, distance], [-half, half, distance]])
        camera_triangles.extend(((cover[[0, 1, 2]], (22, 23, 25)), (cover[[0, 2, 3]], (22, 23, 25))))
    for vertices, color in camera_triangles:
        polygon = _clip_z(list(vertices), camera["near_m"], True)
        if len(polygon) < 3:
            continue
        polygon = _clip_z(polygon, camera["far_m"], False)
        for k in range(1, len(polygon)-1):
            xyz = np.array([polygon[0], polygon[k], polygon[k+1]])
            projected = np.column_stack((width/2+focal*xyz[:, 0]/xyz[:, 2],
                                         height/2-focal*xyz[:, 1]/xyz[:, 2]))
            x0, y0 = np.maximum(np.floor(projected.min(axis=0)).astype(int), 0)
            x1, y1 = np.minimum(np.ceil(projected.max(axis=0)).astype(int), (width-1, height-1))
            if x0 > x1 or y0 > y1:
                continue
            a, b, c = projected
            denominator = (b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
            if abs(denominator) < 1e-12:
                continue
            xx, yy = np.meshgrid(np.arange(x0, x1+1)+.5, np.arange(y0, y1+1)+.5)
            u = ((b[1]-c[1])*(xx-c[0])+(c[0]-b[0])*(yy-c[1]))/denominator
            v = ((c[1]-a[1])*(xx-c[0])+(a[0]-c[0])*(yy-c[1]))/denominator
            w = 1-u-v
            inside = (u >= -1e-10) & (v >= -1e-10) & (w >= -1e-10)
            inverse = u/xyz[0, 2]+v/xyz[1, 2]+w/xyz[2, 2]
            z = np.divide(1., inverse, out=np.full_like(inverse, np.inf), where=inverse > 0)
            region = depth[y0:y1+1, x0:x1+1]
            visible = inside & (z < region)
            region[visible] = z[visible]
            pixels[y0:y1+1, x0:x1+1][visible] = color
    output = BytesIO()
    Image.fromarray(pixels).save(output, format="PNG", compress_level=6)
    wire = output.getvalue()
    if len(wire) > MAX_FRAME_BYTES:
        raise ValueError("visual_frame_byte_budget_exceeded")
    return wire


def validate_frame(frame, *, now=None, run_id=None, vehicle_id=None, max_age_s=None):
    if type(frame) is not dict or set(frame) != FRAME_KEYS or frame.get("schema") != FRAME_SCHEMA:
        raise ValueError("invalid_visual_frame_schema")
    if any(not _identifier(frame[k]) for k in ("frame_id", "run_id", "vehicle_id", "camera_id", "capture_clock_id")):
        raise ValueError("invalid_visual_frame_identity")
    if any(not _hash(frame[k]) for k in ("camera_config_sha256", "image_sha256", "renderer_source_sha256")):
        raise ValueError("invalid_visual_frame_hash")
    if (not _number(frame["capture_sim_time_s"]) or not _number(frame["available_sim_time_s"])
            or not 0 <= frame["capture_sim_time_s"] <= frame["available_sim_time_s"]
            or type(frame["source_sequence"]) is not int or frame["source_sequence"] < 1
            or type(frame["image_bytes"]) is not int or not 1 <= frame["image_bytes"] <= MAX_FRAME_BYTES
            or frame["capture_status"] != "available" or frame["mime_type"] != "image/png"
            or any(type(frame[k]) is not int or not 64 <= frame[k] <= 1024 for k in ("width", "height"))
            or frame["width"]*frame["height"] > 640*640):
        raise ValueError("invalid_visual_frame_fields")
    if (run_id is not None and frame["run_id"] != run_id
            or vehicle_id is not None and frame["vehicle_id"] != vehicle_id):
        raise ValueError("visual_frame_scope_mismatch")
    if now is not None:
        if not _number(now) or frame["available_sim_time_s"] > now:
            raise ValueError("visual_frame_not_arrived")
        if max_age_s is not None and (not _number(max_age_s) or max_age_s < 0
                                     or now-frame["capture_sim_time_s"] > max_age_s):
            raise ValueError("visual_frame_stale")
    return deepcopy(frame)


def _check_pixels(data, frame):
    if (type(data) is not bytes or len(data) != frame["image_bytes"] or len(data) > MAX_FRAME_BYTES
            or sha256(data).hexdigest() != frame["image_sha256"]):
        raise ValueError("visual_image_hash_or_size_mismatch")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != "PNG" or image.size != (frame["width"], frame["height"]):
                raise ValueError("visual_image_format_or_dimensions_mismatch")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
    except (OSError, SyntaxError) as error:
        raise ValueError("visual_image_decode_failed") from error


class FrameStore:
    """A run-owned store. Readers resolve only registered digests, never paths."""

    def __init__(self, root, *, max_bytes=128*1024*1024, maximum_pinned_frames=512):
        self.root = Path(root).absolute()
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise ValueError("visual_store_symlink")
        if type(max_bytes) is not int or max_bytes <= 0 or type(maximum_pinned_frames) is not int or maximum_pinned_frames < 1:
            raise ValueError("invalid_visual_storage_budget")
        self.max_bytes, self.maximum_pinned_frames = max_bytes, maximum_pinned_frames
        self.root.mkdir(parents=True, exist_ok=True)
        self.frames = self.root/"frames"
        self.private = self.root/"private-provenance"
        self.frames.mkdir(exist_ok=True)
        self.private.mkdir(exist_ok=True)
        self._identity = (self.root.stat().st_dev, self.root.stat().st_ino)
        self._records, self._pinned, self._provenance = {}, set(), {}
        manifest = self.root/"manifest.json"
        if manifest.exists():
            wire = self._read_file(manifest, 2*1024*1024)
            saved = json.loads(wire)
            if (type(saved) is not dict or set(saved) != {"schema", "frames"}
                    or saved["schema"] != "missionos.starship_frame_store.v1"
                    or type(saved["frames"]) is not list or len(saved["frames"]) > maximum_pinned_frames):
                raise ValueError("invalid_visual_store_manifest")
            for frame in saved["frames"]:
                validate_frame(frame)
                if frame["frame_id"] in self._records:
                    raise ValueError("duplicate_visual_store_frame")
                self._records[frame["frame_id"]] = frame
                self._pinned.add(frame["frame_id"])
        self._assert_root()
        self._sizes = {}
        for path in (*self.frames.iterdir(), *self.private.iterdir()):
            if path.is_symlink() or not path.is_file():
                raise ValueError("unexpected_visual_store_entry")
            self._sizes[str(path)] = path.stat().st_size
        if manifest.exists():
            self._sizes[str(manifest)] = manifest.stat().st_size
        if sum(self._sizes.values()) > self.max_bytes:
            raise ValueError("visual_storage_budget_exceeded")

    def _assert_root(self):
        if (any(path.is_symlink() for path in (self.root, *self.root.parents, self.frames, self.private))
                or (self.root.stat().st_dev, self.root.stat().st_ino) != self._identity):
            raise ValueError("visual_store_identity_changed")

    def _read_file(self, path, maximum):
        self._assert_root()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise ValueError("invalid_visual_store_file")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                wire = source.read(maximum+1)
            if len(wire) > maximum:
                raise ValueError("visual_store_file_too_large")
            return wire
        finally:
            os.close(descriptor)

    def _write_new(self, path, wire):
        self._assert_root()
        if path.exists() or path.is_symlink():
            if self._read_file(path, len(wire)) != wire:
                raise ValueError("visual_content_address_collision")
            return
        if sum(self._sizes.values())+len(wire) > self.max_bytes:
            raise ValueError("visual_storage_budget_exceeded")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(wire)
        self._sizes[str(path)] = len(wire)

    def put(self, frame, data, *, provenance=None):
        frame = validate_frame(frame)
        _check_pixels(data, frame)
        identity = frame["frame_id"]
        if identity in self._records and self._records[identity] != frame:
            raise ValueError("visual_frame_identity_reused")
        self._write_new(self.frames/(frame["image_sha256"]+".png"), data)
        self._records[identity] = frame
        if provenance is not None:
            self._provenance[identity] = deepcopy(provenance)

    def read(self, frame):
        frame = validate_frame(frame)
        if self._records.get(frame["frame_id"]) != frame:
            raise ValueError("unregistered_or_modified_visual_frame")
        wire = self._read_file(self.frames/(frame["image_sha256"]+".png"), MAX_FRAME_BYTES)
        _check_pixels(wire, frame)
        return wire

    def pin(self, frame):
        self.read(frame)
        identity = frame["frame_id"]
        if identity in self._pinned:
            return
        if len(self._pinned) >= self.maximum_pinned_frames:
            raise ValueError("visual_pinned_frame_budget_exceeded")
        if identity in self._provenance:
            self._write_new(self.private/(identity+".json"), canonical(self._provenance[identity]))
        new_pinned = self._pinned | {identity}
        saved = {"schema": "missionos.starship_frame_store.v1",
                 "frames": [self._records[key] for key in sorted(new_pinned)]}
        wire = canonical(saved)
        if len(wire) > 2*1024*1024:
            raise ValueError("visual_manifest_budget_exceeded")
        temporary = self.root/("manifest-"+sha256(wire).hexdigest()+".tmp")
        self._write_new(temporary, wire)
        previous = self.root/"manifest.json"
        if previous.is_symlink():
            raise ValueError("visual_manifest_symlink")
        os.replace(temporary, previous)
        self._sizes.pop(str(temporary), None)
        self._sizes[str(previous)] = len(wire)
        self._pinned = new_pinned

    def discard(self, frame):
        identity = frame["frame_id"]
        if identity in self._pinned:
            return
        if self._records.get(identity) != frame:
            raise ValueError("unregistered_visual_discard")
        del self._records[identity]
        self._provenance.pop(identity, None)
        if not any(row["image_sha256"] == frame["image_sha256"] for row in self._records.values()):
            self._assert_root()
            path = self.frames/(frame["image_sha256"]+".png")
            if path.is_symlink():
                raise ValueError("visual_store_symlink")
            path.unlink()
            self._sizes.pop(str(path), None)


class VisualObservationCollector:
    """Capture current snapshots, then expose only arrived and fresh frames."""

    def __init__(self, world, output_dir, *, run_id, vehicle_id="ship", cameras=None, config=None):
        if not _identifier(run_id) or not _identifier(vehicle_id):
            raise ValueError("invalid_visual_run_identity")
        self.world, self.run_id, self.vehicle_id = world, run_id, vehicle_id
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        if set(self.config) != set(DEFAULT_CONFIG):
            raise ValueError("unknown_visual_collection_setting")
        for key in ("fps", "latency_s", "history_s", "max_age_s"):
            if not _number(self.config[key]) or self.config[key] < 0:
                raise ValueError("invalid_visual_collection_setting")
        if (not 0 < self.config["fps"] <= 10 or not 0 < self.config["max_age_s"] <= self.config["history_s"] <= 120
                or self.config["latency_s"] > 120
                or any(type(self.config[k]) is not int or self.config[k] <= 0
                       for k in ("ring_bytes", "store_bytes", "maximum_captures", "maximum_pinned_frames"))):
            raise ValueError("invalid_visual_collection_limits")
        self.cameras = [validate_camera(c) for c in (default_cameras() if cameras is None else cameras)]
        if (len(self.cameras) != 2 or {c["camera_id"] for c in self.cameras}
                != {"deployment_port_primary", "deployment_port_secondary"}):
            raise ValueError("unregistered_visual_camera_set")
        self.store = FrameStore(output_dir, max_bytes=self.config["store_bytes"],
                                maximum_pinned_frames=self.config["maximum_pinned_frames"])
        self._ring, self._sequence, self._last_now, self._next_due = deque(), 0, None, 0.
        self._renderer_hash = sha256(Path(__file__).read_bytes()).hexdigest()
        self._requested = set()
        self._dropout_after_s = None
        self.capture_receipts = deque(maxlen=256)

    def set_dropout(self, now):
        """Latch a registered synthetic link outage; keep already received frames."""
        self._clock(now)
        if self._dropout_after_s is None:
            self._dropout_after_s = now
        return {"capture_status": "missing", "dropout_after_sim_s": self._dropout_after_s,
                "run_id": self.run_id, "physical_execution": False}

    def _clock(self, now):
        if (not _number(now) or now < 0 or self.world.time_s != now
                or self._last_now is not None and now < self._last_now):
            raise ValueError("noncausal_visual_capture_clock")
        self._last_now = now

    def _trim(self, now, additional=0):
        while self._ring and (now-self._ring[0]["capture_sim_time_s"] > self.config["history_s"]
                              or sum(f["image_bytes"] for f in self._ring)+additional > self.config["ring_bytes"]):
            self.store.discard(self._ring.popleft())
        if additional > self.config["ring_bytes"]:
            raise ValueError("visual_ring_budget_exceeded")

    def capture(self, now):
        self._clock(now)
        self._trim(now)
        periodic = now+1e-9 >= self._next_due
        selected = [c for c in self.cameras if periodic or c["camera_id"] in self._requested]
        if not selected:
            return []
        if self._sequence+len(selected) > self.config["maximum_captures"]:
            raise ValueError("visual_capture_budget_exceeded")
        schedule_lateness = max(0., now-self._next_due) if self._sequence else 0.
        if periodic:
            self._next_due = now+1/self.config["fps"]
        if self._dropout_after_s is not None and now >= self._dropout_after_s:
            for camera in selected:
                self._sequence += 1
                self.capture_receipts.append({"camera_id": camera["camera_id"], "capture_sim_time_s": now,
                                              "source_sequence": self._sequence, "capture_status": "missing"})
                self._requested.discard(camera["camera_id"])
            return []
        capture_started = time.monotonic()
        scene = self.world.snapshot()
        captured = []
        for camera in selected:
            self._sequence += 1
            started = time.monotonic()
            wire = render_camera(scene, camera)
            finished = time.monotonic()
            render_wall_s = finished-started
            capture_elapsed = finished-capture_started
            # One wall second maps to one simulation second for this capture
            # debt. A slow renderer cannot backdate availability. The caller
            # continues the physical clock and owns overall mission pacing.
            available = now+max(self.config["latency_s"], capture_elapsed)
            identity = digest({"run_id": self.run_id, "vehicle_id": self.vehicle_id,
                               "camera_id": camera["camera_id"], "sequence": self._sequence})
            frame = {"schema": FRAME_SCHEMA, "frame_id": identity, "run_id": self.run_id,
                     "vehicle_id": self.vehicle_id, "camera_id": camera["camera_id"],
                     "camera_config_sha256": digest(camera), "capture_clock_id": "simulation",
                     "capture_sim_time_s": now, "available_sim_time_s": available,
                     "source_sequence": self._sequence, "image_sha256": sha256(wire).hexdigest(),
                     "image_bytes": len(wire), "mime_type": "image/png", "width": camera["width"],
                     "height": camera["height"], "renderer_source_sha256": self._renderer_hash,
                     "capture_status": "available"}
            self._trim(now, len(wire))
            provenance = {"world": scene, "camera": camera, "collection_config": self.config,
                          "renderer": "numpy_triangle_perspective_zbuffer_v1", "pillow": PILLOW_VERSION,
                          "numpy": np.__version__, "render_wall_s": render_wall_s,
                          "capture_elapsed_wall_s": capture_elapsed,
                          "clock_mapping": "capture_sim_plus_max_registered_latency_render_wall_1to1",
                          "scope": "synthetic_camera_not_real_starship_sensor"}
            self.store.put(frame, wire, provenance=provenance)
            self._ring.append(frame)
            self.capture_receipts.append({"frame_id": identity, "render_wall_s": provenance["render_wall_s"],
                                          "capture_elapsed_wall_s": capture_elapsed,
                                          "schedule_lateness_sim_s": schedule_lateness,
                                          "available_sim_time_s": available})
            captured.append(deepcopy(frame))
            self._requested.discard(camera["camera_id"])
        return captured

    def request_observation(self, choice, now):
        self._clock(now)
        choices = {"secondary_camera": "deployment_port_secondary", "alternate_camera": "deployment_port_secondary",
                   "newer_primary_frames": "deployment_port_primary", "next_frame_primary": "deployment_port_primary"}
        if choice not in choices:
            raise ValueError("unregistered_visual_observation_choice")
        camera = choices[choice]
        self._requested.add(camera)
        return {"schema": "missionos.starship_visual_observation_request.v1", "run_id": self.run_id,
                "camera_id": camera, "requested_at_sim_s": now, "after_capture_s": now,
                "status": "pending_new_exposure", "physical_execution": False}

    def evidence(self, now, *, camera_id=None, maximum_frames=6, pin=True, after_capture_s=None):
        if (not _number(now) or now < 0 or self._last_now is not None and now < self._last_now
                or type(maximum_frames) is not int or not 1 <= maximum_frames <= MAX_REQUEST_FRAMES
                or camera_id is not None and camera_id not in {c["camera_id"] for c in self.cameras}
                or after_capture_s is not None and not _number(after_capture_s)):
            raise ValueError("invalid_visual_evidence_selection")
        self._trim(now)
        selected = [f for f in self._ring if f["available_sim_time_s"] <= now
                    and now-f["capture_sim_time_s"] <= self.config["max_age_s"]
                    and (camera_id is None or f["camera_id"] == camera_id)
                    and (after_capture_s is None or f["capture_sim_time_s"] > after_capture_s)][-maximum_frames:]
        for frame in selected:
            validate_frame(frame, now=now, run_id=self.run_id, vehicle_id=self.vehicle_id,
                           max_age_s=self.config["max_age_s"])
            if pin:
                self.store.pin(frame)
        result = {"schema": EVIDENCE_SCHEMA, "run_id": self.run_id, "vehicle_id": self.vehicle_id,
                  "snapshot_time_s": now, "frames": deepcopy(selected)}
        result["manifest_sha256"] = digest(result)
        return result


def validate_evidence(evidence, *, now=None, run_id=None, vehicle_id=None, max_age_s=5.):
    keys = {"schema", "run_id", "vehicle_id", "snapshot_time_s", "frames", "manifest_sha256"}
    if type(evidence) is not dict or set(evidence) != keys or evidence.get("schema") != EVIDENCE_SCHEMA:
        raise ValueError("invalid_visual_evidence_schema")
    if (not _number(evidence["snapshot_time_s"]) or evidence["snapshot_time_s"] < 0
            or not _identifier(evidence["run_id"]) or not _identifier(evidence["vehicle_id"])
            or now is not None and not _number(now)
            or now is not None and evidence["snapshot_time_s"] > now
            or type(evidence["frames"]) is not list or len(evidence["frames"]) > MAX_REQUEST_FRAMES
            or evidence["manifest_sha256"] != digest({k: v for k, v in evidence.items() if k != "manifest_sha256"})):
        raise ValueError("invalid_visual_evidence_manifest")
    if ((run_id is not None and evidence["run_id"] != run_id)
            or (vehicle_id is not None and evidence["vehicle_id"] != vehicle_id)):
        raise ValueError("visual_evidence_scope_mismatch")
    previous, identities = None, set()
    for frame in evidence["frames"]:
        validate_frame(frame, now=evidence["snapshot_time_s"],
                       run_id=evidence["run_id"], vehicle_id=evidence["vehicle_id"], max_age_s=max_age_s)
        if now is not None:
            validate_frame(frame, now=now, max_age_s=max_age_s)
        order = (frame["capture_sim_time_s"], frame["source_sequence"])
        if previous is not None and order <= previous or frame["frame_id"] in identities:
            raise ValueError("visual_evidence_order_or_duplicate")
        identities.add(frame["frame_id"])
        previous = order
    return deepcopy(evidence)
