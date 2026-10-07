"""CG follows a common recorded clock without inventing flight events."""

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
REPLAY = ROOT / "src/runtime/assets/starship_cg_replay.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required for the browser adapter")

BOOTSTRAP = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
const {interpolate, frameFor} = globalThis.MissionOSStarshipReplay;
const radius = 6378137 + 275000;
const rate = Math.sqrt(3.986004418e14 / radius ** 3);
const omega = 7.292115e-5;
const sample = t => [t, radius*Math.cos(rate*t), radius*Math.sin(rate*t), 0,
    -radius*rate*Math.sin(rate*t), radius*rate*Math.cos(rate*t), 0];
const distance = (a,b) => Math.hypot(...a.map((value,i)=>value-b[i]));
const satId = 'sim-starlink-v3-01';
const run = {
  profile: {launch_latitude_deg: 0, launch_longitude_deg: 0},
  cg_replay: {
    tracks: {ship: [sample(0),sample(60),sample(120),sample(180)],
             [satId]: [sample(15),sample(75),sample(135),sample(180)]},
    commands: {ship: [[0,180,'orbital_coast',0,0,0,0,0,0,0,0]]}
  },
  events: [{event:'hot_stage_separation',body:'ship',time_s:0},
           {event:'satellite_released',body:'ship',time_s:15,satellite_id:satId}],
  release_receipts: [{time_s:15,satellite_id:satId}]
};
"""


def assert_javascript(source: str) -> None:
    result = subprocess.run(
        [NODE, "-e", BOOTSTRAP + "\n" + source, str(REPLAY)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_hermite_preserves_endpoints_and_tracks_analytic_circle():
    assert_javascript(r"""
const track = [sample(0),sample(60)];
for (const t of [0,5,15,30,45,55,60]) {
  const state = interpolate(track,t);
  assert.ok(distance(state.r,sample(t).slice(1,4)) < 1,
            `60-second orbital bracket exceeded 1 metre at ${t}`);
  assert.ok(distance(state.v,sample(t).slice(4,7)) < .05,
            `interpolated velocity exceeded .05 m/s at ${t}`);
}
assert.ok(distance(interpolate(track,0).r,track[0].slice(1,4)) < 1e-8);
assert.ok(distance(interpolate(track,60).r,track[1].slice(1,4)) < 1e-8);
""")


def test_asynchronous_recordings_share_one_clock_for_relative_positions():
    assert_javascript(r"""
const frame = frameFor(run,'ship',60);
const other = frame.separatedBodies.find(body => body.id === satId);
assert.ok(other, 'already-released satellite must appear');
assert.ok(Math.hypot(...other.relative_m) < 2,
          'co-orbital bodies were separated by their different sampling times');
assert.equal(frame.time_s,60);
""")


def test_no_satellite_appearance_or_release_count_before_release():
    assert_javascript(r"""
const before = frameFor(run,'ship',14.999);
assert.ok(!before.separatedBodies.some(body => body.id === satId));
assert.equal(before.releasedCount,0);
const after = frameFor(run,'ship',15);
assert.equal(after.releasedCount,1);
assert.ok(after.separatedBodies.some(body => body.id === satId));
assert.equal(frameFor(run,'ship',16).releasedCount,1,
             'event and receipt must not double-count one release');
""")


def test_contact_extinguishes_flames_and_holds_position_on_rotating_earth():
    assert_javascript(r"""
const ground = t => [t,6378137*Math.cos(omega*t),6378137*Math.sin(omega*t),0,
    -6378137*omega*Math.sin(omega*t),6378137*omega*Math.cos(omega*t),0];
const contactRun = {
  profile:run.profile,
  cg_replay:{tracks:{ship:[ground(0),ground(10)]},commands:{
    ship:[[0,20,'terminal_flip_burn',3,1928000,0,0,1,0,0,.26]]}},
  events:[{event:'hot_stage_separation',body:'ship',time_s:0},
          {event:'surface_contact',body:'ship',time_s:10}],
  release_receipts:[]
};
const contact = frameFor(contactRun,'ship',10);
const later = frameFor(contactRun,'ship',1000);
for (const frame of [contact,later]) {
  assert.equal(frame.afterContact,true);
  assert.equal(frame.applied_thrust_n,0);
  assert.equal(frame.engine_count,0);
}
assert.ok(distance(contact.position_ecef_m,later.position_ecef_m) < 1e-6,
          'a held surface contact must not drift because the display clock advances');
assert.equal(later.time_s,1000,'render clock must not silently rewind to final sample');
""")


def test_control_intervals_and_staging_switch_at_recorded_boundary():
    assert_javascript(r"""
const stagedRun = JSON.parse(JSON.stringify(run));
stagedRun.events = [{event:'hot_stage_separation',body:'ship',time_s:10}];
stagedRun.release_receipts = [];
stagedRun.cg_replay.tracks = {ship:[sample(0),sample(60)]};
stagedRun.cg_replay.commands.ship = [
  [0,10,'stack_ascent',32,78000000,0,0,1,0,0,1],
  [10,20,'ship_ascent',6,15000000,0,0,1,0,0,1],
  [20,60,'suborbital_coast',0,0,0,0,0,0,0,0]
];
const before = frameFor(stagedRun,'ship',9.999);
const at = frameFor(stagedRun,'ship',10);
const coast = frameFor(stagedRun,'ship',20);
assert.equal(before.stacked,true);
assert.equal(before.engine_count,32);
assert.equal(at.stacked,false);
assert.equal(at.engine_count,6);
assert.equal(coast.engine_count,0);
assert.equal(coast.applied_thrust_n,0);
""")


def test_local_frame_uses_right_handed_east_up_minus_north_axes():
    assert_javascript(r"""
const axisRun = JSON.parse(JSON.stringify(run));
axisRun.cg_replay.commands.ship = [[0,180,'ship_ascent',1,1000,0,0,0,0,1,1]];
const frame = frameFor(axisRun,'ship',0);
assert.ok(distance(frame.thrust_direction_local,[0,0,-1]) < 1e-10,
          'ECI north at the zero epoch equator must point along local -Z');
""")


def test_replay_is_deterministic_and_does_not_mutate_saved_evidence():
    assert_javascript(r"""
const evidence = JSON.stringify(run);
const first = JSON.stringify(frameFor(run,'ship',35));
frameFor(run,'ship',150);
assert.equal(JSON.stringify(frameFor(run,'ship',35)),first);
assert.equal(JSON.stringify(run),evidence);
assert.equal(interpolate([sample(15),sample(75)],14),null,
             'a trajectory must not be extrapolated backwards before release');
""")


def test_export_retains_close_release_records_and_bounds_later_orbit_gaps():
    from copy import deepcopy

    from src.runtime.starship_3d_report import _cg_export

    def sample(t):
        return {"time_s": t, "x_m": 6_653_137., "y_m": 7700. * t, "z_m": 0.,
                "vx_mps": 0., "vy_mps": 7700., "vz_mps": 0.}

    raw = {"traces": {"ship": [], "booster": [],
                      "satellites": {"satellite": [sample(t) for t in range(0, 1001, 10)]}}}
    original = deepcopy(raw)
    export = _cg_export(raw)
    times = [s[0] for s in export["tracks"]["satellite"]]
    assert times[:13] == list(range(0, 121, 10))
    assert max(b - a for a, b in zip(times, times[1:])) <= 60
    assert times[-1] == 1000
    assert raw == original


def test_export_stops_visual_thrust_at_fuel_depletion_inside_record():
    from src.runtime.starship_3d_report import _cg_export

    state = {"time_s": 0., "r": [6_378_137., 0., 0.], "v": [0., 0., 0.], "propellant_kg": .25}
    record = {"before": state, "after": {**state, "time_s": 1., "propellant_kg": 0.},
              "phase": "terminal_flip_burn", "engine_count": 3, "alpha_deg": 0.,
              "vehicle": {"max_thrust_n": 9806.65, "isp_s": 1000.},
              "control": {"throttle": 1., "bank_rad": 0., "thrust_direction_eci": [1., 0., 0.]}}
    commands = _cg_export({"step_records": {"ship": [record]}, "traces": {}})["commands"]["ship"]
    assert commands[0][:2] == pytest.approx([0., .25])
    assert commands[0][3:5] == [3, 9806.65]
    assert commands[1][:2] == pytest.approx([.25, 1.])
    assert commands[1][3:5] == [0, 0]
