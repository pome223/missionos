"""Record real MuJoCo renders beside telemetry and a clearly labelled map."""

from pathlib import Path

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from simulators.go2_delivery.mujoco_backend import OBSTACLES


class DeliveryVideo:
    def __init__(self, physics, output: Path):
        self.physics, self.output = physics, output
        self.renderer = physics.mj.Renderer(physics.model, height=640, width=960)
        self.camera = physics.mj.MjvCamera()
        self.camera.distance = 3.6
        self.camera.azimuth = 135
        self.camera.elevation = -32
        self.writer = imageio.get_writer(
            str(output / "delivery.mp4"), fps=30, codec="libx264", quality=8, macro_block_size=16
        )
        self.frames = 0
        self.last_phase = None
        self.fonts = {size: ImageFont.load_default(size=size) for size in (14, 17, 20, 26, 32)}

    def frame(self, state, client):
        self.camera.lookat[:] = [*state["ground_truth_xy"], 0.25]
        self.renderer.update_scene(self.physics.data, camera=self.camera)
        rendered = self.renderer.render()
        canvas = Image.new("RGB", (1280, 800), (14, 23, 36))
        canvas.paste(Image.fromarray(rendered), (0, 88))
        draw = ImageDraw.Draw(canvas)

        def text(xy, s, size=17, fill="#bdc9da"):
            return draw.text(xy, s, font=self.fonts[size], fill=fill)

        text((24, 16), "MissionOS / Go2 delivery", 32, "#f2f7ff")
        text(
            (26, 55), "Actual MuJoCo physics render  |  Learned walking policy  |  Playback 3x", 17
        )
        text((984, 105), "MISSION CONTROL", 20, "#f1f5ff")
        text((984, 144), client.phase, 20, "#66e0b0" if client.phase == "Completed" else "#90c5ff")
        text((984, 184), f"Simulation: {state['sim_time_s']:.1f} s")
        text((984, 212), f"Speed: {state['measured_speed_mps']:.2f} m/s")
        text((984, 240), f"Body height: {state['height_m']:.2f} m")
        text((984, 268), f"Policy calls: {state['policy_inference_calls']:,}")
        text((984, 312), "Known office map", 20, "#f2f7ff")

        # This inset is telemetry, not a camera observation or localization test.
        def tx(p):
            return (1120 + p[0] * 32, 466 - p[1] * 32)

        draw.rectangle((983, 358, 1257, 574), fill="#e3e8ed")
        for x, y, sx, sy in OBSTACLES:
            a, b = tx((x - sx, y + sy)), tx((x + sx, y - sy))
            draw.rectangle((*a, *b), fill="#7b8798")
        for x, y, sx, sy in client.planner.obstacles[len(OBSTACLES) :]:
            a, b = tx((x - sx, y + sy)), tx((x + sx, y - sy))
            draw.rectangle((*a, *b), fill="#f47738")
        track = state.get("moving_obstacle")
        if track:
            x, y = tx(track["xy"])
            draw.ellipse((x - 8, y - 8, x + 8, y + 8), fill="#ef4929", outline="white")
            vx, vy = track["velocity_xy_mps"]
            draw.line((x, y, x + vx * 100, y - vy * 100), fill="#ef4929", width=3)
        if len(client.route) > 1:
            draw.line([tx(p) for p in client.route], fill="#509bf5", width=3)
        if len(client.visited) > 1:
            draw.line([tx(p) for p in client.visited], fill="#298e7b", width=2)
        for p, color, label in (
            ((-2.5, 0), "#2e88bd", "Reception"),
            ((2.5, 0), "#26925d", "Room A"),
        ):
            x, y = tx(p)
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=color)
            text((x - 30, y + 12), label, 14, "#253446")
        x, y = tx(state["ground_truth_xy"])
        draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill="#162b44", outline="white", width=2)
        text((984, 596), "Blue: current route", 14)
        text((984, 619), "Green: measured trajectory", 14)
        text((984, 641), "Orange: moving obstacle" if track else "", 14, "#ff9367")
        text((984, 658), "Pose from simulator", 14, "#ffce8c")
        text((984, 681), "Receipt is simulated", 14, "#ffce8c")
        text(
            (24, 735),
            "Reception  >  Meeting room A  >  Recipient confirmation  >  Return",
            20,
            "#e7eef9",
        )
        text(
            (24, 771),
            "Simulation only. No physical payload handling or SLAM. Supervision: "
            + (
                "host Agent enabled"
                if client.metadata.get("supervision_mode") == "agent"
                else "fixed rules"
            ),
            14,
        )
        self.writer.append_data(np.asarray(canvas))
        if self.frames % 5 == 0:
            temporary = self.output / "live.tmp.jpg"
            canvas.save(temporary, quality=85)
            temporary.replace(self.output / "live.jpg")
        if self.last_phase != client.phase:
            safe = client.phase.lower().replace(" ", "-").replace(":", "")
            canvas.save(self.output / f"{self.frames:05d}-{safe}.png")
            self.last_phase = client.phase
        self.last_frame = canvas
        self.frames += 1

    def close(self):
        self.writer.close()
        self.renderer.close()
        if self.frames:
            self.last_frame.save(self.output / "preview.png")
            temporary = self.output / "live.tmp.jpg"
            self.last_frame.save(temporary, quality=85)
            temporary.replace(self.output / "live.jpg")
