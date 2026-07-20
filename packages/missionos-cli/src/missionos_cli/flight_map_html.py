"""Read-only PX4 and generic mission-map HTML view."""

from __future__ import annotations

from typing import Any
import html
import json

from .indoor_map_html import _mission_indoor_map_html


def _json_for_html_script(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def _mission_map_html(model: dict[str, Any]) -> str:
    if model.get("map_kind") == "indoor_local_xy":
        return _mission_indoor_map_html(model)
    model_json = _json_for_html_script(model)
    escaped_title = html.escape(f"MissionOS 2D Map · {model['task_id']}")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escaped_title}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #07101d;
      --panel: rgba(8, 14, 25, 0.92);
      --line: rgba(148, 163, 184, 0.25);
      --text: #e5eefb;
      --muted: #96a4b8;
	      --green: #22c55e;
	      --blue: #38bdf8;
	      --yellow: #facc15;
	      --orange: #fb923c;
	      --red: #f97373;
	      --cyan: #22d3ee;
	      --gap: #64748b;
	    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .shell {{ display: grid; gap: 14px; padding: 16px; }}
    header {{
      align-items: start;
      display: flex;
      justify-content: space-between;
      gap: 16px;
    }}
    h1 {{ margin: 0; font-size: 1.15rem; letter-spacing: 0; }}
    .muted {{ color: var(--muted); font-size: 0.86rem; line-height: 1.45; }}
    .live-status {{ margin-top: 4px; }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--text);
      background: rgba(15, 23, 42, 0.62);
      padding: 6px 10px;
      white-space: nowrap;
      font-size: 0.75rem;
      font-weight: 700;
    }}
    .map {{
      position: relative;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #dbe4ef;
      height: min(72vh, 760px);
      min-height: 420px;
      overflow: hidden;
    }}
    .tile {{
      position: absolute;
      display: block;
      width: 256px;
      height: 256px;
      max-width: none;
      user-select: none;
    }}
    svg.overlay {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
    }}
	    .path-shadow {{
	      fill: none;
	      stroke: rgba(2, 6, 23, 0.46);
	      stroke-linecap: round;
	      stroke-linejoin: round;
	      stroke-width: 6;
	    }}
	    .planned-path {{
	      fill: none;
	      stroke: var(--yellow);
	      stroke-linecap: round;
	      stroke-linejoin: round;
	      stroke-width: 3;
	      stroke-dasharray: 9 7;
	    }}
	    .observed-path {{
	      fill: none;
	      stroke: var(--blue);
	      stroke-linecap: round;
	      stroke-linejoin: round;
	      stroke-width: 4.5;
	    }}
	    .observed-return-path {{
	      fill: none;
	      stroke: var(--cyan);
	      stroke-linecap: round;
	      stroke-linejoin: round;
	      stroke-width: 4.5;
	      stroke-dasharray: 10 7;
	    }}
	    .telemetry-gap {{
	      fill: none;
	      stroke: var(--gap);
	      stroke-width: 3;
	      stroke-dasharray: 7 7;
	    }}
	    .avoidance-path {{
	      fill: none;
	      stroke: var(--orange);
	      stroke-linecap: round;
	      stroke-linejoin: round;
	      stroke-width: 5;
	    }}
	    .marker-h {{ fill: var(--blue); stroke: white; stroke-width: 2; }}
	    .marker-d {{ fill: var(--green); stroke: white; stroke-width: 2; }}
	    .marker-d-blocked {{ fill: var(--green); stroke: #dc2626; stroke-width: 6; }}
	    .marker-blocked-ring {{ fill: none; stroke: #dc2626; stroke-width: 4; stroke-dasharray: 5 3; }}
	    .marker-current {{ fill: var(--red); stroke: white; stroke-width: 2; }}
	    .marker-avoid {{ fill: var(--orange); stroke: white; stroke-width: 2; }}
	    .marker-recovery-start {{ fill: #f59e0b; stroke: white; stroke-width: 2; }}
	    .marker-rejoin {{ fill: #a78bfa; stroke: white; stroke-width: 2; }}
	    .marker-obstacle {{ fill: #dc2626; stroke: white; stroke-width: 2; }}
	    .obstacle-footprint {{ fill: rgba(220, 38, 38, 0.18); stroke: rgba(127, 29, 29, 0.78); stroke-width: 1.5; }}
	    .label {{
      fill: white;
      font-size: 13px;
      font-weight: 800;
      paint-order: stroke;
      stroke: rgba(2, 6, 23, 0.88);
      stroke-width: 4;
    }}
	    .attribution {{
	      position: absolute;
	      right: 8px;
	      bottom: 8px;
      border-radius: 4px;
      background: rgba(255, 255, 255, 0.88);
      color: #111827;
      font-size: 0.72rem;
	      padding: 5px 7px;
	      text-decoration: none;
	    }}
	    .legend {{
	      position: absolute;
	      left: 8px;
	      top: 8px;
	      display: flex;
	      flex-wrap: wrap;
	      gap: 6px;
	      max-width: calc(100% - 16px);
	      border-radius: 6px;
	      background: rgba(15, 23, 42, 0.82);
	      color: white;
	      font-size: 0.72rem;
	      padding: 7px;
	    }}
	    .legend-item {{ display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }}
	    .legend-swatch {{ width: 20px; height: 3px; border-radius: 999px; background: currentColor; }}
	    .legend-planned {{ color: var(--yellow); }}
	    .legend-observed {{ color: var(--blue); }}
	    .legend-return {{ color: var(--cyan); }}
	    .legend-avoidance {{ color: var(--orange); }}
	    .legend-gap {{ color: var(--gap); }}
	    .legend-gap .legend-swatch {{
	      background: repeating-linear-gradient(90deg, currentColor 0 6px, transparent 6px 10px);
	    }}
	    .legend-obstacle {{ color: var(--red); }}
    .facts {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(220px, 100%), 1fr));
      gap: 8px;
    }}
    .fact {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 10px;
      min-width: 0;
    }}
    .fact span {{ display: block; color: var(--muted); font-size: 0.74rem; }}
    .fact strong {{ display: block; margin-top: 3px; overflow-wrap: anywhere; }}
    .terminal-evidence {{
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--panel);
      padding: 12px;
    }}
    .terminal-evidence[hidden] {{ display: none; }}
    .terminal-evidence h2 {{ margin: 0 0 6px; font-size: 1rem; }}
    .terminal-evidence img {{
      display: block;
      width: 100%;
      height: auto;
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #07101f;
    }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  </style>
</head>
<body>
  <main class="shell">
	    <header>
	      <div>
	        <h1>MissionOS 2D Map</h1>
	        <div class="muted">Follow the numbered markers: departure → Recovery → pass beside obstacle → route rejoin → dropoff → return home. Blue/cyan and orange lines are saved observations; gaps are never drawn as movement. This read-only map does not grant authority or claim delivery.</div>
        <div class="muted live-status" id="liveStatus">Snapshot loaded.</div>
      </div>
      <div class="pill" id="providerPill">provider</div>
    </header>
    <section class="terminal-evidence" id="terminalEvidence" hidden>
      <h2>Terminal E2E Route Evidence / 実行後の航跡証拠</h2>
      <div class="muted">Generated from persisted observations for this task. The source task artifacts—not this image—remain authoritative for approval, dispatch, verification, completion, delivery, and physical-execution claims.</div>
      <img id="terminalEvidenceImage" alt="MissionOS terminal E2E route evidence" />
    </section>
    <section id="map" class="map" aria-label="MissionOS 2D map"></section>
    <section class="facts" id="facts"></section>
  </main>
  <script id="mission-map-data" type="application/json">{model_json}</script>
  <script>
    let data = JSON.parse(document.getElementById("mission-map-data").textContent);
    const TILE_SIZE = 256;
    const mapEl = document.getElementById("map");
    const factsEl = document.getElementById("facts");
    const providerEl = document.getElementById("providerPill");
    const liveStatusEl = document.getElementById("liveStatus");
    const terminalEvidenceEl = document.getElementById("terminalEvidence");
    const terminalEvidenceImageEl = document.getElementById("terminalEvidenceImage");
    providerEl.textContent = data.provider.label;
    const liveConfig = data.live || {{ enabled: false }};
    const terminalStatuses = new Set(liveConfig.terminal_statuses || []);

    function setLiveStatus(message) {{
      liveStatusEl.textContent = message;
    }}

	    function firstNumber(...values) {{
	      for (const value of values) {{
	        if (value === null || value === undefined || value === "") continue;
	        const number = Number(value);
	        if (Number.isFinite(number)) return number;
	      }}
	      return null;
	    }}

	    function firstPresent(...values) {{
	      for (const value of values) {{
	        if (value !== null && value !== undefined && value !== "") return value;
	      }}
	      return null;
	    }}

	    function asBool(value) {{
	      if (typeof value === "boolean") return value;
	      if (typeof value === "number") return value !== 0;
	      if (typeof value === "string") {{
	        const normalized = value.trim().toLowerCase();
	        if (["true", "1", "yes", "y"].includes(normalized)) return true;
	        if (["false", "0", "no", "n"].includes(normalized)) return false;
	      }}
	      return null;
	    }}

        function statusText(value, fallback = "-") {{
          return value === null || value === undefined || value === "" ? fallback : String(value);
        }}

        function fmtMetres(value) {{
          let number = firstNumber(value);
          if (number === null) return "-";
          if (Math.abs(number) < 0.5) number = 0;
          return Math.abs(number) >= 1000
            ? `${{(number / 1000).toFixed(2)}}km`
            : `${{number.toFixed(0)}}m`;
        }}

        function fmtSignedMetres(value) {{
          let number = firstNumber(value);
          if (number === null) return "-";
          if (Math.abs(number) < 0.5) number = 0;
          return `${{number >= 0 ? "+" : ""}}${{fmtMetres(number)}}`;
        }}

        function fmtMps(value) {{
          const number = firstNumber(value);
          return number === null ? "-" : `${{number.toFixed(1)}}m/s`;
        }}

        function fmtDegrees(value) {{
          const number = firstNumber(value);
          return number === null ? "-" : `${{number.toFixed(0)}}deg`;
        }}

        function fmtTemp(value) {{
          const number = firstNumber(value);
          return number === null ? "-" : `${{number.toFixed(1)}}C`;
        }}

        function fmtHpa(value) {{
          const number = firstNumber(value);
          return number === null ? "-" : `${{number.toFixed(0)}}hPa`;
        }}

        function fmtRain(value) {{
          const number = firstNumber(value);
          return number === null ? "-" : `${{number.toFixed(1)}}mm/h`;
        }}

    function taskRecord(payload) {{
      return payload && typeof payload.task === "object" && payload.task !== null
        ? payload.task
        : (payload || {{}});
    }}

    function taskArtifacts(payload) {{
      if (payload && typeof payload.artifacts === "object" && payload.artifacts !== null) {{
        return payload.artifacts;
      }}
      const task = taskRecord(payload);
      return task && typeof task.artifacts === "object" && task.artifacts !== null
        ? task.artifacts
        : {{}};
    }}

    function taskStatus(payload) {{
      const task = taskRecord(payload);
      return statusText(task.status || task.task_status, "");
    }}

        function routeFromArtifacts(artifacts) {{
          const route = artifacts.mission_designer_coordinate_pair_route || {{}};
          const takeoffLat = firstNumber(route.takeoff_latitude, route.takeoff_latitude_deg);
      const takeoffLon = firstNumber(route.takeoff_longitude, route.takeoff_longitude_deg);
      const dropoffLat = firstNumber(route.dropoff_latitude, route.dropoff_latitude_deg);
      const dropoffLon = firstNumber(route.dropoff_longitude, route.dropoff_longitude_deg);
      if ([takeoffLat, takeoffLon, dropoffLat, dropoffLon].some((value) => value === null)) {{
        return null;
      }}
          return {{
            takeoff: {{ lat: takeoffLat, lon: takeoffLon, label: "H" }},
            dropoff: {{ lat: dropoffLat, lon: dropoffLon, label: "D" }},
          }};
        }}

        function terrainProfileSamples(artifacts) {{
          const compilation = artifacts.missionos_auto_mission_compilation || {{}};
          const route = artifacts.mission_designer_coordinate_pair_route || {{}};
          const rawProfile = Array.isArray(compilation.terrain_clearance_profile)
            ? compilation.terrain_clearance_profile
            : Array.isArray(route.terrain_profile)
              ? route.terrain_profile
              : [];
          const plannedRouteM = firstNumber(
            compilation.planned_route_m,
            route.planned_route_m,
            route.derived_route_distance_m,
          );
          const targetClearance = firstNumber(
            compilation.terrain_clearance_target_m,
            route.terrain_clearance_agl_m,
            route.terrain_clearance_target_m,
          );
          let firstTerrain = null;
          const samples = [];
          for (const sample of rawProfile) {{
            if (!sample || typeof sample !== "object") continue;
            const terrain = firstNumber(sample.terrain_elevation_m);
            if (terrain === null) continue;
            if (firstTerrain === null) firstTerrain = terrain;
            const distance = firstNumber(sample.distance_m);
            let fraction = firstNumber(sample.fraction);
            if (fraction === null && distance !== null && plannedRouteM) {{
              fraction = distance / plannedRouteM;
            }}
            if (fraction === null) continue;
            const missionAltitude = firstNumber(sample.mission_altitude_m);
            const sampleTarget = firstNumber(sample.target_clearance_m) ?? targetClearance;
            let targetAmsl = null;
            if (missionAltitude !== null && firstTerrain !== null) {{
              targetAmsl = firstTerrain + missionAltitude;
            }} else if (sampleTarget !== null) {{
              targetAmsl = terrain + sampleTarget;
            }}
            samples.push({{
              fraction: Math.max(0, Math.min(1, fraction)),
              terrain_elevation_m: terrain,
              target_amsl_m: targetAmsl,
            }});
          }}
          samples.sort((a, b) => a.fraction - b.fraction);
          return samples;
        }}

        function telemetryFromArtifacts(artifacts) {{
          const snapshot = artifacts.missionos_auto_mission_runtime_snapshot || {{}};
          const altHome = firstNumber(snapshot.altitude_above_home_m);
          const terrain = firstNumber(snapshot.terrain_elevation_m);
          const agl = firstNumber(snapshot.terrain_clearance_m);
          const aglTarget = firstNumber(snapshot.terrain_clearance_target_m);
          const aglMargin = firstNumber(snapshot.terrain_clearance_margin_m);
          const samples = terrainProfileSamples(artifacts);
          const firstTerrain = samples.length ? samples[0].terrain_elevation_m : null;
          const currentAmsl = terrain !== null && agl !== null
            ? terrain + agl
            : firstTerrain !== null && altHome !== null
              ? firstTerrain + altHome
              : null;
          const destination = [...samples].reverse().find((sample) => sample.target_amsl_m !== null);
          const destinationTargetAmsl = destination ? destination.target_amsl_m : null;
          return {{
            altitude_amsl_m: currentAmsl,
            home_relative_altitude_m: altHome,
            terrain_elevation_amsl_m: terrain,
            agl_m: agl,
            agl_target_m: aglTarget,
            agl_margin_m: aglMargin,
            agl_status: statusText(snapshot.terrain_clearance_status),
            destination_target_amsl_m: destinationTargetAmsl,
            climb_to_destination_m: destinationTargetAmsl !== null && currentAmsl !== null
              ? destinationTargetAmsl - currentAmsl
              : null,
          }};
        }}

        function weatherFromArtifacts(artifacts) {{
          const route = artifacts.mission_designer_coordinate_pair_route || {{}};
          const weather = {{
            wind_speed_mps: firstNumber(route.wind_speed_mps),
            wind_direction_deg: firstNumber(route.wind_direction_deg),
            wind_gust_mps: firstNumber(route.wind_gust_mps),
            wind_variance: statusText(route.wind_variance),
            temperature_c: firstNumber(route.temperature_c),
            pressure_hpa: firstNumber(route.pressure_hpa),
            precipitation_mm_per_hour: firstNumber(route.precipitation_mm_per_hour),
          }};
          return Object.values(weather).some((value) => value !== null && value !== "-")
            ? weather
            : {{}};
        }}

    function localToLatLon(takeoff, northM, eastM) {{
      const lat = takeoff.lat + northM / 111320.0;
      const lonScale = Math.max(1e-9, 111320.0 * Math.cos((takeoff.lat * Math.PI) / 180));
      return {{ lat, lon: takeoff.lon + eastM / lonScale }};
    }}

    function sampleLatLon(sample, takeoff) {{
      const lat = firstNumber(sample.latitude_deg, sample.global_latitude_deg, sample.lat, sample.latitude);
      const lon = firstNumber(sample.longitude_deg, sample.global_longitude_deg, sample.lon, sample.longitude);
      if (lat !== null && lon !== null) {{
        return {{ lat, lon, source: "observed_wgs84" }};
      }}
      const north = firstNumber(sample.local_x_m, sample.x_m, sample.x);
      const east = firstNumber(sample.local_y_m, sample.y_m, sample.y);
      if (north === null || east === null) return null;
      return {{ ...localToLatLon(takeoff, north, east), source: "estimated_from_local_ned" }};
    }}

    function flightSamples(artifacts) {{
      for (const key of [
        "missionos_auto_mission_runtime_replay",
        "auto_mission_runtime_replay",
        "px4_gazebo_mission_designer_sitl_live_flight_run",
        "mission_designer_live_telemetry_snapshot",
      ]) {{
        const candidate = artifacts[key] || {{}};
        for (const samplesKey of ["flight_path_profile", "position_profile", "route_preview_waypoints"]) {{
          const samples = candidate[samplesKey];
          if (Array.isArray(samples) && samples.length) {{
            return samples.filter((sample) => sample && typeof sample === "object");
          }}
        }}
      }}
      return [];
    }}

	    function telemetryPoint(sample, route, index, sourceSuffix = "") {{
	      const latlon = sampleLatLon(sample, route.takeoff);
	      if (!latlon) return null;
	      return {{
	        lat: latlon.lat,
        lon: latlon.lon,
        source: `${{latlon.source}}${{sourceSuffix}}`,
        phase: statusText(sample.phase, `sample_${{index}}`),
        alt_m: firstNumber(sample.relative_alt_m, sample.altitude_above_home_m, sample.local_z_m, sample.z_m, sample.z),
	        elapsed_s: sample.elapsed_s ?? sample.elapsed_seconds ?? sample.sample_time_s ?? sample.sample_index ?? null,
	      }};
	    }}

	    function missionCommandLabel(command) {{
	      const commandId = firstNumber(command);
	      const labels = {{
	        16: "waypoint",
	        19: "dropoff_loiter",
	        21: "land",
	        22: "takeoff",
	      }};
	      return commandId === null ? "-" : (labels[commandId] || `command_${{commandId}}`);
	    }}

	    function plannedPointsFromArtifacts(artifacts, route) {{
	      const compilation = artifacts.missionos_auto_mission_compilation || {{}};
	      const points = [];
	      const items = Array.isArray(compilation.mission_items) ? compilation.mission_items : [];
	      items.forEach((item, index) => {{
	        if (!item || typeof item !== "object") return;
	        const latlon = sampleLatLon(item, route.takeoff);
	        if (!latlon) return;
	        const seq = firstNumber(item.seq);
	        const plannedSource = latlon.source === "observed_wgs84"
	          ? "planned_wgs84"
	          : latlon.source === "estimated_from_local_ned"
	            ? "planned_from_local_ned"
	            : `planned_${{latlon.source}}`;
	        points.push({{
	          lat: latlon.lat,
	          lon: latlon.lon,
	          source: plannedSource,
	          phase: missionCommandLabel(item.command),
	          seq: seq === null ? index : seq,
	          command: firstNumber(item.command),
	          alt_m: firstNumber(item.altitude_m, item.relative_alt_m, item.z_m),
	        }});
	      }});
	      if (points.length >= 2) return points;
	      return [
	        {{ ...route.takeoff, source: "planned_route_takeoff", phase: "takeoff", seq: 0, command: null, alt_m: 0 }},
	        {{ ...route.dropoff, source: "planned_route_dropoff", phase: "dropoff", seq: 1, command: null, alt_m: null }},
	      ];
	    }}

	    function obstaclePosition(record) {{
	      const pose = record && typeof record.pose_readback === "object" ? record.pose_readback : {{}};
	      const x = firstNumber(record.x_m, record.local_x_m, record.x, pose.x);
	      const y = firstNumber(record.y_m, record.local_y_m, record.y, pose.y);
	      return x === null || y === null ? null : {{ x, y }};
	    }}

	    function obstaclesFromArtifacts(artifacts, route) {{
	      const records = [];
	      const seen = new Set();
	      const addObstacle = (sourceRef, record, manifestSpawned) => {{
	        if (!record || typeof record !== "object") return;
	        const pos = obstaclePosition(record);
	        if (!pos) return;
	        const name = statusText(record.name, `obstacle_${{records.length}}`);
	        const key = `${{name}}:${{pos.x.toFixed(2)}}:${{pos.y.toFixed(2)}}`;
	        if (seen.has(key)) return;
	        seen.add(key);
	        const latlon = localToLatLon(route.takeoff, pos.x, pos.y);
	        records.push({{
	          name,
	          kind: statusText(record.kind, "obstacle"),
	          source: statusText(record.source, sourceRef),
	          source_ref: sourceRef,
	          x_m: pos.x,
	          y_m: pos.y,
	          z_m: firstNumber(record.z_m, record.z),
	          size_x_m: firstNumber(record.size_x_m),
	          size_y_m: firstNumber(record.size_y_m),
	          size_z_m: firstNumber(record.size_z_m),
	          spawned: asBool(firstPresent(record.gazebo_obstacle_model_spawned, manifestSpawned)),
	          lat: latlon.lat,
	          lon: latlon.lon,
	        }});
	      }};
	      const addManifest = (sourceRef, manifest, spawned) => {{
	        if (!manifest || typeof manifest !== "object" || !Array.isArray(manifest.obstacles)) return;
	        manifest.obstacles.forEach((obstacle) => addObstacle(sourceRef, obstacle, spawned));
	      }};
	      const probe = artifacts.missionos_auto_mission_probe_observed || {{}};
	      const app = probe.gazebo_obstacle_application || {{}};
	      const snapshot = artifacts.missionos_auto_mission_runtime_snapshot || {{}};
	      const snapshotApp = snapshot.gazebo_obstacle_application || {{}};
	      addManifest("obstacle_manifest", artifacts.obstacle_manifest, artifacts.obstacle_manifest?.gazebo_obstacle_model_spawned);
	      addManifest("probe_observed.obstacle_manifest", probe.obstacle_manifest, probe.obstacle_manifest?.gazebo_obstacle_model_spawned);
	      addManifest("gazebo_obstacle_application.obstacle_manifest", app.obstacle_manifest, firstPresent(app.obstacle_manifest?.gazebo_obstacle_model_spawned, app.gazebo_obstacle_model_spawned));
	      addManifest("runtime_snapshot.obstacle_manifest", snapshot.obstacle_manifest, snapshot.obstacle_manifest?.gazebo_obstacle_model_spawned);
	      addManifest("runtime_snapshot.gazebo_obstacle_application.obstacle_manifest", snapshotApp.obstacle_manifest, firstPresent(snapshotApp.obstacle_manifest?.gazebo_obstacle_model_spawned, snapshotApp.gazebo_obstacle_model_spawned));
	      if (Array.isArray(app.models)) {{
	        app.models.forEach((model) => {{
	          addObstacle(
	            "gazebo_obstacle_application.models",
	            model,
	            firstPresent(model.pose_readback_observed, model.spawn_request_accepted, model.spawn_performed),
	          );
	        }});
	      }}
	      const routeRecord = artifacts.mission_designer_coordinate_pair_route || {{}};
	      if (!records.length && asBool(routeRecord.landing_zone_blocked) === true) {{
	        records.push({{
	          name: "landing_zone_blocked",
	          kind: "landing_zone_risk",
	          source: "mission_designer_coordinate_pair_route",
	          source_ref: "route.landing_zone_blocked",
	          spawned: false,
	          lat: route.dropoff.lat,
	          lon: route.dropoff.lon,
	        }});
	      }}
	      return records;
	    }}

	    function maneuverFromArtifacts(artifacts, route) {{
	      const snapshot = artifacts.missionos_auto_mission_runtime_snapshot || {{}};
	      const probe = artifacts.missionos_auto_mission_probe_observed || {{}};
	      const monitor = probe.monitor || {{}};
	      const operatorRecovery = monitor.operator_recovery || {{}};
	      const command = operatorRecovery.command || {{}};
	      const target = command.target || {{}};
	      const snapshotTarget = snapshot.operator_recovery_target || {{}};
	      const parameters = snapshot.operator_recovery_parameters || {{}};
	      const recoveryPath = statusText(firstPresent(command.recovery_path, snapshot.operator_recovery_path));
	      let action = statusText(firstPresent(command.action, snapshot.operator_recovery_action));
	      if (recoveryPath.includes("avoid_obstacle")) action = "avoid_obstacle";
	      const targetX = firstNumber(
	        target.target_x_m,
	        target.x_m,
	        target.x,
	        snapshotTarget.target_x_m,
	        snapshotTarget.x_m,
	        parameters.target_x_m,
	      );
	      const targetY = firstNumber(
	        target.target_y_m,
	        target.y_m,
	        target.y,
	        snapshotTarget.target_y_m,
	        snapshotTarget.y_m,
	        parameters.target_y_m,
	      );
	      const targetAltitude = firstNumber(
	        command.target_altitude_m,
	        target.target_altitude_m,
	        snapshotTarget.target_altitude_m,
	        parameters.target_altitude_m,
	        Math.abs(firstNumber(target.target_z_m, snapshotTarget.target_z_m) || NaN),
	      );
	      const samples = [];
	      if (Array.isArray(command.maneuver_observation_samples)) {{
	        command.maneuver_observation_samples.forEach((sample, index) => {{
	          if (!sample || typeof sample !== "object") return;
	          const x = firstNumber(sample.local_x_m, sample.x_m, sample.x);
	          const y = firstNumber(sample.local_y_m, sample.y_m, sample.y);
	          if (x === null || y === null) return;
	          const latlon = localToLatLon(route.takeoff, x, y);
	          samples.push({{
	            x_m: x,
	            y_m: y,
	            lat: latlon.lat,
	            lon: latlon.lon,
	            altitude_m: firstNumber(sample.altitude_above_home_m, sample.relative_alt_m, sample.local_z_m, sample.z_m),
	            distance_to_target_m: firstNumber(sample.distance_to_target_m),
	            elapsed_s: sample.elapsed_seconds ?? sample.elapsed_s ?? sample.sample_time_s ?? index,
	            nav_state: sample.nav_state ?? null,
	          }});
	        }});
	      }}
	      let targetPoint = null;
	      if (targetX !== null && targetY !== null) {{
	        const latlon = localToLatLon(route.takeoff, targetX, targetY);
	        targetPoint = {{
	          x_m: targetX,
	          y_m: targetY,
	          lat: latlon.lat,
	          lon: latlon.lon,
	          altitude_m: targetAltitude,
	        }};
	      }}
	      if (!targetPoint && !samples.length) return {{}};
	      return {{
	        action,
	        status: statusText(firstPresent(command.status, snapshot.operator_recovery_assist_status)),
	        recovery_path: recoveryPath,
	        target: targetPoint,
	        samples,
	        target_reached: asBool(firstPresent(command.target_reached, snapshot.operator_recovery_target_reached)),
	        target_distance_m: firstNumber(command.target_distance_m, snapshot.operator_recovery_target_distance_m),
	        resume_auto_status: statusText(firstPresent(command.resume_auto_status, snapshot.operator_recovery_resume_auto_status)),
	        source: Object.keys(command).length ? "operator_recovery_command" : "missionos_auto_mission_runtime_snapshot",
	      }};
	    }}

	    function mapModelFromTaskPayload(payload) {{
	      if (payload && payload.missionos_map_model
	        && typeof payload.missionos_map_model === "object") {{
	        return payload.missionos_map_model;
	      }}
	      const artifacts = taskArtifacts(payload);
	      const route = routeFromArtifacts(artifacts);
	      if (!route) throw new Error("task does not include source route coordinates");
	      const plannedPoints = plannedPointsFromArtifacts(artifacts, route);
	      const observedPoints = [];
	      flightSamples(artifacts).forEach((sample, index) => {{
	        const point = telemetryPoint(sample, route, index);
	        if (point) observedPoints.push(point);
	      }});
	      const snapshot = artifacts.missionos_auto_mission_runtime_snapshot || {{}};
	      if (snapshot && typeof snapshot === "object") {{
	        const latest = telemetryPoint(snapshot, route, observedPoints.length, "_latest_snapshot");
	        if (latest && (!observedPoints.length
	          || Math.abs(observedPoints[observedPoints.length - 1].lat - latest.lat) > 1e-8
	          || Math.abs(observedPoints[observedPoints.length - 1].lon - latest.lon) > 1e-8)) {{
	          observedPoints.push(latest);
	        }}
	      }}
	      const compatibilityPoints = observedPoints.length
	        ? [...observedPoints]
	        : [
	          {{ ...route.takeoff, source: "route_takeoff", phase: "takeoff", alt_m: 0, elapsed_s: null }},
	          {{ ...route.dropoff, source: "route_dropoff", phase: "dropoff", alt_m: null, elapsed_s: null }},
	        ];
	      const task = taskRecord(payload);
	      return {{
	        ...data,
	        task_id: statusText(task.task_id, data.task_id),
	        task_status: taskStatus(payload),
	            generated_at: new Date().toISOString(),
	            route,
	            planned_points: plannedPoints,
	            observed_points: observedPoints,
	            points: compatibilityPoints,
	            latest: observedPoints[observedPoints.length - 1] || null,
	            avoidance: maneuverFromArtifacts(artifacts, route),
	            obstacles: obstaclesFromArtifacts(artifacts, route),
	            telemetry: telemetryFromArtifacts(artifacts),
	            weather: weatherFromArtifacts(artifacts),
	          }};
	        }}

    function mercator(lon, lat, zoom) {{
      const boundedLat = Math.max(-85.05112878, Math.min(85.05112878, lat));
      const sinLat = Math.sin((boundedLat * Math.PI) / 180);
      const worldSize = TILE_SIZE * (2 ** zoom);
      return {{
        x: ((lon + 180) / 360) * worldSize,
        y: (0.5 - Math.log((1 + sinLat) / (1 - sinLat)) / (4 * Math.PI)) * worldSize,
      }};
    }}

        function zoomFor(points, width, height) {{
          const padding = 110;
          for (let zoom = 18; zoom >= 2; zoom -= 1) {{
        const projected = points.map((point) => mercator(point.lon, point.lat, zoom));
        const xs = projected.map((point) => point.x);
        const ys = projected.map((point) => point.y);
        if ((Math.max(...xs) - Math.min(...xs)) <= width - padding
          && (Math.max(...ys) - Math.min(...ys)) <= height - padding) {{
          return zoom;
        }}
      }}
          return 2;
        }}

        function altitudeSummary(telemetry) {{
          const parts = [];
          if (firstNumber(telemetry.altitude_amsl_m) !== null) {{
            parts.push(`alt=${{fmtMetres(telemetry.altitude_amsl_m)}} AMSL`);
          }}
          if (firstNumber(telemetry.home_relative_altitude_m) !== null) {{
            parts.push(`alt(home)=${{fmtSignedMetres(telemetry.home_relative_altitude_m)}}`);
          }}
          if (firstNumber(telemetry.agl_m) !== null || firstNumber(telemetry.agl_target_m) !== null) {{
            let agl = `AGL=${{fmtMetres(telemetry.agl_m)}}`;
            if (firstNumber(telemetry.agl_target_m) !== null) {{
              agl += `/target ${{fmtMetres(telemetry.agl_target_m)}}`;
            }}
            if (firstNumber(telemetry.agl_margin_m) !== null) {{
              agl += ` (margin ${{fmtSignedMetres(telemetry.agl_margin_m)}})`;
            }}
            parts.push(agl);
          }}
          if (firstNumber(telemetry.destination_target_amsl_m) !== null) {{
            let dest = `dest=${{fmtMetres(telemetry.destination_target_amsl_m)}} AMSL`;
            if (firstNumber(telemetry.climb_to_destination_m) !== null) {{
              dest += `/climb ${{fmtSignedMetres(telemetry.climb_to_destination_m)}}`;
            }}
            parts.push(dest);
          }}
          return parts.length ? parts.join(" · ") : "-";
        }}

	        function weatherSummary(weather) {{
	          if (!weather || !Object.keys(weather).length) return "-";
	          return [
	            `wind=${{fmtMps(weather.wind_speed_mps)}}`,
	            `dir=${{fmtDegrees(weather.wind_direction_deg)}}`,
            `gust=${{fmtMps(weather.wind_gust_mps)}}`,
            `temp=${{fmtTemp(weather.temperature_c)}}`,
            `pressure=${{fmtHpa(weather.pressure_hpa)}}`,
	            `rain=${{fmtRain(weather.precipitation_mm_per_hour)}}`,
	          ].join(" · ");
	        }}

	        function validPoints(points) {{
	          return (Array.isArray(points) ? points : [])
	            .filter((point) => point && Number.isFinite(point.lat) && Number.isFinite(point.lon));
	        }}

	        function pathD(points, toOverlay) {{
	          return validPoints(points)
	            .map(toOverlay)
	            .map((point, index) => `${{index ? "L" : "M"}}${{point.x.toFixed(2)}} ${{point.y.toFixed(2)}}`)
	            .join(" ");
	        }}

	        function escapeHtml(value) {{
	          const element = document.createElement("div");
	          element.textContent = String(value);
	          return element.innerHTML;
	        }}

	        function obstacleSummary(obstacles) {{
	          const count = Array.isArray(obstacles) ? obstacles.length : 0;
	          if (!count) return "-";
	          const spawnedValues = obstacles.map((obstacle) => asBool(obstacle.spawned));
	          const spawnedStatus = spawnedValues.some((value) => value === true)
	            ? "spawned"
	            : spawnedValues.every((value) => value === false)
	              ? "not_spawned"
	              : "unknown";
	          return `${{count}} · ${{spawnedStatus}}`;
	        }}

	        function recoveriesSummary(avoidances) {{
	          if (!Array.isArray(avoidances) || !avoidances.length) return "-";
	          const targetReached = avoidances.every((item) => item.target_reached === true);
	          const allResumed = avoidances.every(
	            (item) => statusText(item.resume_auto_status) === "resumed_auto_mission"
	          );
	          const sampleCounts = avoidances.map(
	            (item) => Array.isArray(item.samples) ? item.samples.length : 0
	          ).join("+");
	          return `${{avoidances.length}} observed · targets=${{targetReached ? "all reached" : "mixed"}} · resume=${{allResumed ? "all resumed" : "mixed"}} · samples=${{sampleCounts}}`;
	        }}

	        function recoveryGeometrySummary(avoidances) {{
	          if (!Array.isArray(avoidances) || !avoidances.length) return "-";
	          const values = avoidances.map((item) => statusText(item.geometry_status));
	          return values.every((value) => value === values[0])
	            ? `${{values.length}}× ${{values[0]}}`
	            : values.join(" | ");
	        }}

	        function batterySummary(battery) {{
	          if (!battery || typeof battery !== "object") return "-";
	          const display = firstNumber(battery.display_percent);
	          const reported = firstNumber(battery.reported_percent);
	          const parts = [
	            display === null ? "-" : `${{display.toFixed(1)}}%`,
	            `source=${{statusText(battery.source, "unknown")}}`,
	            `status=${{statusText(battery.status)}}`,
	            `sample=${{statusText(battery.sample_index)}}`,
	            `observed_at=${{statusText(battery.observed_at)}}`,
	          ];
	          if (battery.reset_detected === true) {{
	            const delta = firstNumber(battery.reset_delta_percent);
	            parts.push(`reported=${{reported === null ? "-" : `${{reported.toFixed(1)}}%`}} rejected_reset=${{delta === null ? "-" : `+${{delta.toFixed(1)}}pp`}}`);
	          }}
	          return parts.join(" · ");
	        }}

	    function render() {{
	      mapEl.innerHTML = "";
	      const status = statusText(data.task_status, "-").trim().toLowerCase();
	      const evidenceUrl = statusText(
	        ((data.live || {{}}).evidence_image_url || liveConfig.evidence_image_url),
	        "",
	      );
	      const showTerminalEvidence = terminalStatuses.has(status) && Boolean(evidenceUrl);
	      terminalEvidenceEl.hidden = !showTerminalEvidence;
	      if (showTerminalEvidence && terminalEvidenceImageEl.getAttribute("src") !== evidenceUrl) {{
	        terminalEvidenceImageEl.setAttribute("src", evidenceUrl);
	      }}
	      const width = mapEl.clientWidth || 980;
	      const height = mapEl.clientHeight || 560;
	      const plannedPoints = validPoints(
	        data.planned_points && data.planned_points.length
	          ? data.planned_points
	          : [data.route.takeoff, data.route.dropoff],
	      );
	      const observedSegmentDetails = Array.isArray(data.observed_segment_details)
	        ? data.observed_segment_details.map((detail) => ({{
	            ...detail,
	            points: validPoints((detail || {{}}).points || []),
	          }})).filter((detail) => detail.points.length)
	        : Array.isArray(data.observed_segments)
	          ? data.observed_segments.map((segment, index) => ({{
	              points: validPoints(segment || []),
	              role: index === 0 ? "outbound" : "observed",
	            }})).filter((detail) => detail.points.length)
	          : [{{
	              points: validPoints(data.observed_points || data.points || []),
	              role: "outbound",
	            }}].filter((detail) => detail.points.length);
	      const observedSegments = observedSegmentDetails.map((detail) => detail.points);
	      const observedPoints = observedSegments.flat();
	      const avoidances = (
	        Array.isArray(data.avoidances) && data.avoidances.length
	          ? data.avoidances
	          : data.avoidance ? [data.avoidance] : []
	      );
	      const avoidance = avoidances.length ? avoidances[avoidances.length - 1] : {{}};
	      const avoidancePointSets = avoidances.map((item) => validPoints([
	        ...(item.start && !item.observation_start_gap ? [item.start] : []),
	        ...(Array.isArray(item.samples) ? item.samples : []),
	      ]));
	      const avoidancePoints = avoidancePointSets.flat();
	      const observedGaps = (Array.isArray(data.display_gaps)
	        ? data.display_gaps
	        : Array.isArray(data.observed_gaps) ? data.observed_gaps : [])
	        .map((gap) => ({{
	          ...gap,
	          from: validPoints(gap && gap.from ? [gap.from] : [])[0] || null,
	          to: validPoints(gap && gap.to ? [gap.to] : [])[0] || null,
	        }}))
	        .filter((gap) => gap.from && gap.to);
	      const obstacles = validPoints(data.obstacles || []);
	      const routePoints = validPoints([
	        data.route.takeoff,
	        data.route.dropoff,
	        ...plannedPoints,
	        ...observedPoints,
	        ...avoidancePoints,
	        ...observedGaps.flatMap((gap) => [gap.from, gap.to]),
	        ...obstacles,
	        ...obstacles.flatMap((obstacle) => validPoints(obstacle.footprint || [])),
	      ]);
	      const zoom = zoomFor(routePoints, width, height);
	      const projected = routePoints.map((point) => mercator(point.lon, point.lat, zoom));
	      const xs = projected.map((point) => point.x);
	      const ys = projected.map((point) => point.y);
      const centerX = (Math.min(...xs) + Math.max(...xs)) / 2;
      const centerY = (Math.min(...ys) + Math.max(...ys)) / 2;
      const left = centerX - width / 2;
      const top = centerY - height / 2;
      const tileCount = 2 ** zoom;
      const minTileX = Math.floor(left / TILE_SIZE);
      const maxTileX = Math.floor((left + width) / TILE_SIZE);
      const minTileY = Math.floor(top / TILE_SIZE);
      const maxTileY = Math.floor((top + height) / TILE_SIZE);
      for (let y = minTileY; y <= maxTileY; y += 1) {{
        if (y < 0 || y >= tileCount) continue;
        for (let x = minTileX; x <= maxTileX; x += 1) {{
          const wrappedX = ((x % tileCount) + tileCount) % tileCount;
          const img = document.createElement("img");
          img.className = "tile";
          img.alt = "";
          img.loading = "lazy";
          img.src = data.provider.url_template
            .replace("{{z}}", zoom)
            .replace("{{x}}", wrappedX)
            .replace("{{y}}", y);
          img.style.left = `${{(x * TILE_SIZE - left).toFixed(2)}}px`;
          img.style.top = `${{(y * TILE_SIZE - top).toFixed(2)}}px`;
          mapEl.appendChild(img);
        }}
      }}
	      const toOverlay = (point) => {{
	        const projectedPoint = mercator(point.lon, point.lat, zoom);
	        return {{ x: projectedPoint.x - left, y: projectedPoint.y - top }};
	      }};
	      const plannedD = pathD(plannedPoints, toOverlay);
	      const observedMarkup = observedSegmentDetails.map((detail) => {{
	        const d = pathD(detail.points, toOverlay);
	        const pathClass = detail.role === "return_to_home"
	          ? "observed-return-path"
	          : "observed-path";
	        const arrow = detail.role === "return_to_home" ? "arrow-return" : "arrow-outbound";
	        return d ? `<path class="${{pathClass}}" d="${{d}}" marker-end="url(#${{arrow}})"></path>` : "";
	      }}).join("");
	      const gapMarkup = observedGaps.map((gap) => {{
	        const d = pathD([gap.from, gap.to], toOverlay);
	        if (!d) return "";
	        const from = toOverlay(gap.from);
	        const to = toOverlay(gap.to);
	        const labelX = Math.min(width - 210, Math.max(12, (from.x + to.x) / 2 + 8));
	        const labelY = Math.min(height - 16, Math.max(22, (from.y + to.y) / 2 - 8));
	        return `<path class="telemetry-gap" d="${{d}}"></path><text class="label" x="${{labelX.toFixed(2)}}" y="${{labelY.toFixed(2)}}">not observed</text>`;
	      }}).join("");
	      const avoidanceMarkup = avoidancePointSets.map((points) => {{
	        const d = pathD(points, toOverlay);
	        return d ? `<path class="avoidance-path" d="${{d}}" marker-end="url(#arrow-recovery)"></path>` : "";
	      }}).join("");
	      const home = toOverlay(data.route.takeoff);
	      const dropoff = toOverlay(data.route.dropoff);
	      const latest = data.latest ? toOverlay(data.latest) : null;
	      const terminalMarkerLabel = statusText(data.terminal_marker_label, "current");
	      const terminalAtHome = ["landed at home", "mission ended at home"].includes(terminalMarkerLabel);
	      const recoveryMarkers = avoidances.map((item, index) => {{
	        const startPoint = validPoints(item.start ? [item.start] : [])[0] || null;
	        const targetPoint = validPoints(item.target ? [item.target] : [])[0] || null;
	        const rejoinPoint = validPoints(item.route_rejoin ? [item.route_rejoin] : [])[0] || null;
	        return {{
	          index: index + 1,
	          item,
	          recoveryLabel: avoidances.length === 1 ? "2 Recovery" : `R${{index + 1}} Recovery`,
	          bypassLabel: avoidances.length === 1 ? "3 Bypass" : `R${{index + 1}} Bypass`,
	          oldTargetLabel: avoidances.length === 1 ? "3 Old target" : `R${{index + 1}} Old target`,
	          rejoinLabel: avoidances.length === 1 ? "4 Rejoin" : `R${{index + 1}} Rejoin`,
	          start: startPoint ? toOverlay(startPoint) : null,
	          target: targetPoint ? toOverlay(targetPoint) : null,
	          rejoin: rejoinPoint ? toOverlay(rejoinPoint) : null,
	        }};
	      }});
	      const recoveryMarkerMarkup = recoveryMarkers.map((marker) => `
	        ${{marker.start ? `<circle class="marker-recovery-start" cx="${{marker.start.x.toFixed(2)}}" cy="${{marker.start.y.toFixed(2)}}" r="7"></circle><text class="label" x="${{Math.min(width - 150, marker.start.x + 12).toFixed(2)}}" y="${{Math.min(height - 18, marker.start.y + 22).toFixed(2)}}">${{marker.recoveryLabel}}</text>` : ""}}
	        ${{marker.target ? `<circle class="marker-avoid" cx="${{marker.target.x.toFixed(2)}}" cy="${{marker.target.y.toFixed(2)}}" r="8"></circle><text class="label" x="${{Math.min(width - 160, marker.target.x + 12).toFixed(2)}}" y="${{Math.min(height - 18, marker.target.y + 22).toFixed(2)}}">${{marker.item.target_beyond_obstacle === true ? marker.bypassLabel : marker.oldTargetLabel}}</text>` : ""}}
	        ${{marker.rejoin ? `<circle class="marker-rejoin" cx="${{marker.rejoin.x.toFixed(2)}}" cy="${{marker.rejoin.y.toFixed(2)}}" r="7"></circle><text class="label" x="${{Math.min(width - 150, marker.rejoin.x + 12).toFixed(2)}}" y="${{Math.max(22, marker.rejoin.y - 18).toFixed(2)}}">${{marker.rejoinLabel}}</text>` : ""}}
	      `).join("");
	      const blockedDropoff = obstacles.some((obstacle) => obstacle.coincident_with_dropoff === true);
	      const obstacleMarkup = obstacles.filter((obstacle) => obstacle.coincident_with_dropoff !== true).map((obstacle, obstacleIndex) => {{
	        const point = toOverlay(obstacle);
	        const footprint = validPoints(obstacle.footprint || []);
	        const footprintD = footprint.length >= 3
	          ? pathD([...footprint, footprint[0]], toOverlay)
	          : "";
	        const labelX = Math.min(width - 190, point.x + 13).toFixed(2);
	        const labelY = Math.min(height - 34, Math.max(38, point.y - 34)).toFixed(2);
	        const widthM = firstNumber(obstacle.size_x_m);
	        const depthM = firstNumber(obstacle.size_y_m);
	        const heightM = firstNumber(obstacle.size_z_m);
	        const dimensions = widthM !== null && depthM !== null
	          ? `${{widthM.toFixed(0)}}×${{depthM.toFixed(0)}}m footprint${{heightM !== null ? ` · height ${{heightM.toFixed(0)}}m` : ""}}`
	          : "size unavailable";
	        const overflightClearance = firstNumber(obstacle.return_overflight_min_vertical_clearance_m);
	        const overflight = obstacle.return_overflight_status === "observed_above_obstacle"
	          && overflightClearance !== null
	          ? `return: +${{overflightClearance.toFixed(1)}}m over top`
	          : "";
	        return `
	          ${{footprintD ? `<path class="obstacle-footprint" d="${{footprintD}}"></path>` : ""}}
	          <path class="marker-obstacle" d="M ${{point.x.toFixed(2)}} ${{(point.y - 10).toFixed(2)}} L ${{(point.x + 10).toFixed(2)}} ${{point.y.toFixed(2)}} L ${{point.x.toFixed(2)}} ${{(point.y + 10).toFixed(2)}} L ${{(point.x - 10).toFixed(2)}} ${{point.y.toFixed(2)}} Z">
	            <title>${{escapeHtml(`${{statusText(obstacle.name)}} · ${{dimensions}} · ${{statusText(obstacle.source)}}`)}}</title>
	          </path>
	          <text class="label" x="${{labelX}}" y="${{labelY}}">
	            <tspan x="${{labelX}}">O${{obstacleIndex + 1}} ${{widthM !== null && depthM !== null && heightM !== null ? `${{widthM.toFixed(0)}}×${{depthM.toFixed(0)}}×${{heightM.toFixed(0)}}m` : "obstacle"}}</tspan>
	            ${{overflight ? `<tspan x="${{labelX}}" dy="16">${{overflight}}</tspan>` : ""}}
	          </text>
	        `;
	      }}).join("");
	      const blockedDropoffMarkup = blockedDropoff
	        ? `<circle class="marker-blocked-ring" cx="${{dropoff.x.toFixed(2)}}" cy="${{dropoff.y.toFixed(2)}}" r="16"></circle><path class="marker-obstacle" d="M ${{dropoff.x.toFixed(2)}} ${{(dropoff.y - 11).toFixed(2)}} L ${{(dropoff.x + 11).toFixed(2)}} ${{dropoff.y.toFixed(2)}} L ${{dropoff.x.toFixed(2)}} ${{(dropoff.y + 11).toFixed(2)}} L ${{(dropoff.x - 11).toFixed(2)}} ${{dropoff.y.toFixed(2)}} Z"><title>Obstacle overlaps dropoff</title></path>`
	        : "";
	      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
	      svg.setAttribute("class", "overlay");
	      svg.setAttribute("viewBox", `0 0 ${{width}} ${{height}}`);
	      svg.innerHTML = `
	        <defs>
	          <marker id="arrow-outbound" markerUnits="userSpaceOnUse" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#38bdf8"></path></marker>
	          <marker id="arrow-return" markerUnits="userSpaceOnUse" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#22d3ee"></path></marker>
	          <marker id="arrow-recovery" markerUnits="userSpaceOnUse" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#fb923c"></path></marker>
	        </defs>
	        ${{plannedD ? `<path class="path-shadow" d="${{plannedD}}"></path><path class="planned-path" d="${{plannedD}}"></path>` : ""}}
	        ${{observedMarkup}}
	        ${{gapMarkup}}
	        ${{avoidanceMarkup}}
	        ${{obstacleMarkup}}
	        <circle class="marker-h" cx="${{home.x.toFixed(2)}}" cy="${{home.y.toFixed(2)}}" r="7"></circle>
	        <text class="label" x="${{Math.min(width - 150, home.x + 12).toFixed(2)}}" y="${{Math.max(22, home.y - 10).toFixed(2)}}">1 Start</text>
	        <circle class="${{blockedDropoff ? "marker-d-blocked" : "marker-d"}}" cx="${{dropoff.x.toFixed(2)}}" cy="${{dropoff.y.toFixed(2)}}" r="9"></circle>
	        ${{blockedDropoffMarkup}}
	        <text class="label" x="${{Math.min(width - 150, dropoff.x + 12).toFixed(2)}}" y="${{Math.max(22, dropoff.y - 10).toFixed(2)}}">${{blockedDropoff ? "5 Blocked" : "5 Dropoff"}}</text>
	        ${{recoveryMarkerMarkup}}
	        ${{latest && !terminalAtHome ? `<circle class="marker-current" cx="${{latest.x.toFixed(2)}}" cy="${{latest.y.toFixed(2)}}" r="7"></circle><text class="label" x="${{Math.min(width - 200, latest.x + 12).toFixed(2)}}" y="${{Math.min(height - 18, latest.y + 22).toFixed(2)}}">${{escapeHtml(terminalMarkerLabel)}}</text>` : ""}}
	        ${{terminalAtHome ? `<text class="label" x="${{Math.min(width - 150, home.x + 12).toFixed(2)}}" y="${{Math.min(height - 18, home.y + 24).toFixed(2)}}">6 Home</text>` : ""}}
	      `;
	      mapEl.appendChild(svg);
	      const attribution = document.createElement("a");
      attribution.className = "attribution";
      attribution.href = data.provider.attribution_url;
      attribution.target = "_blank";
	      attribution.rel = "noopener noreferrer";
	      attribution.textContent = data.provider.attribution;
	      mapEl.appendChild(attribution);
	      const legend = document.createElement("div");
	      legend.className = "legend";
	      legend.innerHTML = `
	        <span class="legend-item legend-planned"><span class="legend-swatch"></span>planned route</span>
	        <span class="legend-item legend-observed"><span class="legend-swatch"></span>outbound →</span>
	        <span class="legend-item legend-avoidance"><span class="legend-swatch"></span>Recovery bypass →</span>
	        <span class="legend-item legend-return"><span class="legend-swatch"></span>return home →</span>
	        <span class="legend-item legend-gap"><span class="legend-swatch"></span>not observed</span>
	        <span class="legend-item legend-obstacle"><span class="legend-swatch"></span>collision footprint</span>
	      `;
	      mapEl.appendChild(legend);
	          const telemetry = data.telemetry || {{}};
	          const weather = data.weather || {{}};
	          factsEl.innerHTML = [
	            ["task", data.task_id],
	            ["status", data.task_status || "-"],
            ["altitude", altitudeSummary(telemetry)],
            ["terrain", `terrain=${{fmtMetres(telemetry.terrain_elevation_amsl_m)}} AMSL · AGL status=${{statusText(telemetry.agl_status)}}`],
	            ["weather", weatherSummary(weather)],
	            ["wind", `speed=${{fmtMps(weather.wind_speed_mps)}} · gust=${{fmtMps(weather.wind_gust_mps)}} · dir=${{fmtDegrees(weather.wind_direction_deg)}}`],
	            ["battery", batterySummary(data.battery || {{}})],
	            ["provider", data.provider.label],
	            ["planned", `${{plannedPoints.length}}pts`],
	            ["observed", `${{observedPoints.length}}pts · ${{observedSegments.length}} segment(s)`],
	            ["continuity", `${{observedGaps.length}} unobserved gap(s) · source=${{statusText(data.observed_trace_source)}}`],
	            ["recoveries", recoveriesSummary(avoidances)],
	            ["recovery geometry", recoveryGeometrySummary(avoidances)],
	            ["obstacles", obstacleSummary(data.obstacles || [])],
	            ["latest source", data.latest ? data.latest.source : "-"],
	        ["live", data.live && data.live.enabled ? "polling" : "snapshot"],
	        ["generated", data.generated_at],
      ].map(([key, value]) => `<div class="fact"><span>${{key}}</span><strong><code>${{String(value)}}</code></strong></div>`).join("");
    }}

    async function refreshLive() {{
      if (!liveConfig.enabled || !liveConfig.task_url) return;
      try {{
        const response = await fetch(liveConfig.task_url, {{ cache: "no-store" }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        data = mapModelFromTaskPayload(await response.json());
        render();
        const status = data.task_status || "-";
        setLiveStatus(`Live: updated ${{new Date().toLocaleTimeString()}} · status=${{status}}`);
        if (terminalStatuses.has(status) && window.__missionMapLiveTimer) {{
          window.clearInterval(window.__missionMapLiveTimer);
          window.__missionMapLiveTimer = null;
          setLiveStatus(`Live: terminal status ${{status}} · final update shown`);
        }}
      }} catch (error) {{
        setLiveStatus(`Live update failed: ${{error.message}}`);
      }}
    }}

    window.addEventListener("resize", render);
    render();
    if (liveConfig.enabled && liveConfig.task_url) {{
      setLiveStatus(`Live: polling Gateway every ${{Math.round((liveConfig.poll_interval_ms || 1000) / 100) / 10}}s`);
      window.__missionMapLiveTimer = window.setInterval(
        refreshLive,
        liveConfig.poll_interval_ms || 1000,
      );
      refreshLive();
    }} else {{
      setLiveStatus("Snapshot: no live polling");
    }}
  </script>
</body>
</html>
"""
