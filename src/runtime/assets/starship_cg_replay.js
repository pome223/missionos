/* Recorded-state adapter for the offline CG. Interpolation is display only. */
(function (root) {
  'use strict';
  const OMEGA = 7.292115e-5, A = 6378137, F = 1 / 298.257223563;
  const E2 = F * (2 - F);
  const dot = (a, b) => a.reduce((s, x, i) => s + x * b[i], 0);
  const sub = (a, b) => a.map((x, i) => x - b[i]);
  const unit = a => { const n = Math.hypot(...a); return n > 1e-12 ? a.map(x => x / n) : [0, 1, 0]; };
  const rotate = (v, t) => {
    const c = Math.cos(-OMEGA * t), s = Math.sin(-OMEGA * t);
    return [c * v[0] - s * v[1], s * v[0] + c * v[1], v[2]];
  };

  function interpolate(track, t) {
    if (!track || !track.length || !Number.isFinite(t) || t < track[0][0]) return null;
    let lo = 0, hi = track.length - 1;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (track[mid][0] <= t) lo = mid; else hi = mid - 1;
    }
    const a = track[lo], b = track[lo + 1];
    if (!b || t === a[0]) return {
      time_s: a[0], r: a.slice(1, 4), v: a.slice(4, 7),
      bracket_s: [a[0], a[0]], held: t > a[0],
    };
    const h = b[0] - a[0], u = (t - a[0]) / h, u2 = u * u, u3 = u2 * u;
    const r = [], v = [];
    for (let k = 0; k < 3; k++) {
      r.push((2 * u3 - 3 * u2 + 1) * a[k + 1] + (u3 - 2 * u2 + u) * h * a[k + 4]
        + (-2 * u3 + 3 * u2) * b[k + 1] + (u3 - u2) * h * b[k + 4]);
      v.push((6 * u2 - 6 * u) / h * a[k + 1] + (3 * u2 - 4 * u + 1) * a[k + 4]
        + (-6 * u2 + 6 * u) / h * b[k + 1] + (3 * u2 - 2 * u) * b[k + 4]);
    }
    return { time_s: t, r, v, bracket_s: [a[0], b[0]], held: false };
  }

  function geodetic(r) {
    const xy = Math.hypot(r[0], r[1]), lon = Math.atan2(r[1], r[0]);
    let lat = Math.atan2(r[2], xy * (1 - E2));
    for (let i = 0; i < 8; i++) {
      const n = A / Math.sqrt(1 - E2 * Math.sin(lat) ** 2);
      lat = Math.atan2(r[2] + E2 * n * Math.sin(lat), xy);
    }
    const sl = Math.sin(lat), cl = Math.cos(lat), so = Math.sin(lon), co = Math.cos(lon);
    const altitude = xy * cl + r[2] * sl - A * Math.sqrt(1 - E2 * sl * sl);
    return { altitude, lat, lon, east: [-so, co, 0], up: [cl * co, cl * so, sl],
      south: [sl * co, sl * so, -cl] };
  }

  function commandAt(commands, t) {
    let lo = 0, hi = commands.length - 1, result = null;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (commands[mid][0] <= t) { result = commands[mid]; lo = mid + 1; } else hi = mid - 1;
    }
    // A step's command applies to [before, after), not the interval after it.
    return result && t < result[1] ? result : null;
  }

  function contactEvent(run, id) {
    return (run.events || []).find(e =>
      (id === 'booster' ? e.event === 'booster_surface_contact' : id === 'ship' && e.event === 'surface_contact')
      && (!e.body || e.body === id));
  }

  function frameFor(run, bodyId, time) {
    const data = run.cg_replay;
    if (!data || !data.tracks || !data.tracks[bodyId]) return null;
    const events = run.events || [];
    function stateFor(id) {
      const track = data.tracks[id];
      if (!track || !track.length) return null;
      // Release appearance is gated by the exact receipt as well as track start.
      const receipt = (run.release_receipts || []).find(r => r.satellite_id === id);
      if (receipt && time < receipt.time_s) return null;
      const contact = contactEvent(run, id);
      const query = contact && time >= contact.time_s ? contact.time_s : time;
      const s = interpolate(track, query);
      if (!s) return null;
      const ended = time > track.at(-1)[0], afterContact = !!contact && time >= contact.time_s;
      // An ended trajectory is a held endpoint in the Earth-fixed frame; no
      // inertial propagation, Earth spin or contact dynamics are invented.
      return { ...s, ecef: rotate(s.r, s.time_s), ended, afterContact };
    }
    const focus = stateFor(bodyId);
    if (!focus) return null;
    const basis = geodetic(focus.ecef);
    const local = vector => [dot(vector, basis.east), dot(vector, basis.up), dot(vector, basis.south)];
    const separation = events.find(e => e.event === 'hot_stage_separation');
    const stacked = bodyId === 'ship' && (!separation || time < separation.time_s);
    const command = commandAt(data.commands?.[bodyId] || [], time);
    const afterContact = focus.afterContact;
    const engines = command && !afterContact && !focus.ended ? command[3] : 0;
    const thrust = command && !afterContact && !focus.ended ? command[4] : 0;
    const phase = command?.[2] || (afterContact ? 'surface_contact' :
      bodyId === 'ship' ? (stacked ? 'stack_ascent' : 'coast') :
      bodyId === 'booster' ? 'booster_coast' : 'passive_satellite_coast');
    const thrustDirection = command ? local(rotate(command.slice(7, 10), time)) : [0, 0, 0];
    const velocityEcef = sub(rotate(focus.v, focus.time_s), [-OMEGA * focus.ecef[1], OMEGA * focus.ecef[0], 0]);
    const localVelocity = local(velocityEcef);
    const alpha = command?.[5] || 0, bank = command?.[6] || 0;
    let attitude;
    if (thrust > 0) attitude = unit(thrustDirection);
    else if (afterContact) attitude = [0, 1, 0];
    else if (phase === 'belly_flop' || phase === 'terminal_flip_burn') attitude = unit([localVelocity[0] || 1, 0, localVelocity[2]]);
    else {
      const forward = unit(localVelocity), up = [0, 1, 0];
      const projection = sub(up, forward.map(x => x * dot(up, forward)));
      const normal = Math.hypot(...projection) > 1e-9 ? unit(projection) : [1, 0, 0];
      const a = alpha * Math.PI / 180;
      attitude = unit(forward.map((v, i) => v * Math.cos(a) + normal[i] * Math.sin(a)));
    }
    const separatedBodies = [];
    for (const id of Object.keys(data.tracks)) {
      if (id === bodyId || (id === 'booster' && separation && time < separation.time_s)) continue;
      const s = stateFor(id);
      if (!s) continue;
      const c = commandAt(data.commands?.[id] || [], time);
      separatedBodies.push({ id, relative_m: local(sub(s.ecef, focus.ecef)),
        afterContact: s.afterContact, held: s.ended,
        phase: c?.[2] || (id === 'booster' ? 'booster_coast' : 'passive_satellite_coast'),
        applied_thrust_n: c && !s.afterContact && !s.ended ? c[4] : 0,
        engine_count: c && !s.afterContact && !s.ended ? c[3] : 0,
        attitude_direction_local: c && c[4] > 0 ? unit(local(rotate(c.slice(7, 10), time))) : [0, 1, 0],
        sample_time_s: s.time_s, bracket_s: s.bracket_s });
    }
    const launchTrack = data.tracks.ship || [], launch = launchTrack.length ? rotate(launchTrack[0].slice(1, 4), launchTrack[0][0]) : null;
    const releases = run.release_receipts || events.filter(e => e.event === 'satellite_released');
    const uniqueReleased = new Set(releases.filter(r => r.time_s <= time).map(r => r.satellite_id));
    const heatTrack = data.heat?.[bodyId] || [];
    let heat = 0;
    for (let i = 0; i < heatTrack.length && heatTrack[i][0] <= time; i++) heat = heatTrack[i][1];
    return { time_s: time, sample_time_s: focus.time_s, bracket_s: focus.bracket_s,
      bodyId, phase, altitude_m: basis.altitude, stacked, afterContact,
      recordEnded: focus.ended, engine_count: thrust > 0 ? engines : 0,
      throttle: command && thrust > 0 ? command[10] : 0, applied_thrust_n: thrust,
      alpha_deg: alpha, bank_deg: bank,
      heat_rate_w_m2: afterContact || focus.ended ? 0 : heat,
      thrust_direction_local: thrustDirection, attitude_direction_local: attitude,
      local_velocity_mps: localVelocity, separatedBodies,
      launch_relative_m: launch ? local(sub(launch, focus.ecef)) : null,
      releasedCount: uniqueReleased.size, position_ecef_m: focus.ecef, position_eci_m: focus.r,
      latitude_deg: basis.lat * 180 / Math.PI, longitude_deg: basis.lon * 180 / Math.PI,
      interpolation: 'recorded-position/velocity Hermite; display only',
      attitude_source: 'display orientation from command/velocity and prescribed alpha; not 6DOF' };
  }

  root.MissionOSStarshipReplay = { interpolate, frameFor };
})(globalThis);
