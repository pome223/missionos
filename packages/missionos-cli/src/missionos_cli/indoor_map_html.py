"""Read-only TurtleBot indoor-map HTML view."""

from __future__ import annotations

from typing import Any
import html
import json


TURTLEBOT3_MAP_ICON = "🐢"


def _status_text(value: Any, default: str = "-") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _json_for_html_script(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def _mission_indoor_map_html(model: dict[str, Any]) -> str:
    model_json = _json_for_html_script(model)
    escaped_title = html.escape(f"MissionOS Indoor Map · {model['task_id']}")
    escaped_robot_label = html.escape(_status_text(model.get("robot_label"), "TurtleBot3"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escaped_title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f7fb;
      --ink: #172033;
      --muted: #667085;
      --line: #cbd5e1;
      --room: #ffffff;
      --room-zone: #f8fafc;
      --furniture: #475569;
      --plan: #d97706;
      --observed: #0284c7;
      --recovery: #7c3aed;
      --home: #2563eb;
      --dropoff: #16a34a;
      --obstacle: #dc2626;
      --visual-corroborated: #0d9488;
      --visual-camera-only: #ca8a04;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{ display: grid; gap: 14px; padding: 16px; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: start; }}
    h1 {{ margin: 0; font-size: 1.15rem; letter-spacing: 0; }}
    .muted {{ color: var(--muted); font-size: 0.86rem; line-height: 1.45; }}
    .pill {{
      border: 1px solid var(--line);
      border-radius: 999px;
      background: white;
      padding: 6px 10px;
      white-space: nowrap;
      font-size: 0.75rem;
      font-weight: 700;
    }}
    .pill-stack {{ display: flex; flex-wrap: wrap; gap: 8px; justify-content: end; }}
    .clearance-pill[data-status="verified_clear"] {{
      color: #166534;
      border-color: #86efac;
      background: #f0fdf4;
    }}
    .clearance-pill[data-status="collision_observed"] {{
      color: #b91c1c;
      border-color: #fca5a5;
      background: #fef2f2;
    }}
    .clearance-pill[data-status="unavailable"] {{
      color: #92400e;
      border-color: #fcd34d;
      background: #fffbeb;
    }}
    .map {{
      position: relative;
      min-height: 420px;
      height: min(72vh, 760px);
      border: 1px solid var(--line);
      border-radius: 8px;
      background:
        linear-gradient(90deg, rgba(148, 163, 184, .18) 1px, transparent 1px),
        linear-gradient(rgba(148, 163, 184, .18) 1px, transparent 1px),
        #eef2f7;
      background-size: 32px 32px;
      overflow: hidden;
    }}
    svg {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
    .room {{ fill: var(--room); stroke: #64748b; stroke-width: 2; }}
    .room-zone {{ fill: var(--room-zone); stroke: #cbd5e1; stroke-width: 1.4; }}
    .arena-wall {{ fill: none; stroke: #334155; stroke-width: 3.5; stroke-linejoin: round; }}
    .wall-rect {{ fill: #334155; stroke: none; }}
    .pillar {{ fill: rgba(71, 85, 105, .30); stroke: var(--furniture); stroke-width: 1.7; }}
    .room-label {{ fill: #64748b; font-size: 10px; font-weight: 800; text-anchor: start; }}
    .furniture {{ fill: rgba(71, 85, 105, .16); stroke: var(--furniture); stroke-width: 1.7; rx: 5; }}
    .furniture-label {{ fill: #334155; font-size: 10px; font-weight: 800; text-anchor: middle; paint-order: stroke; stroke: white; stroke-width: 3; }}
    .path-shadow {{ fill: none; stroke: rgba(15, 23, 42, .16); stroke-width: 8; stroke-linecap: round; stroke-linejoin: round; }}
    .planned-path {{ fill: none; stroke: var(--plan); stroke-width: 2.5; stroke-dasharray: 8 7; stroke-linecap: round; stroke-linejoin: round; }}
    .observed-path {{ fill: none; stroke: var(--observed); stroke-width: 4.6; stroke-linecap: round; stroke-linejoin: round; }}
    .live-path {{ fill: none; stroke: #16a34a; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; opacity: .62; }}
    .live-path-ended {{ stroke-dasharray: 7 8; opacity: .28; }}
    .recovery-path {{ fill: none; stroke: var(--recovery); stroke-width: 3.2; stroke-linecap: round; stroke-linejoin: round; }}
    .marker-home {{ fill: var(--home); stroke: white; stroke-width: 2; }}
    .marker-dropoff {{ fill: var(--dropoff); stroke: white; stroke-width: 2; }}
    .marker-current {{ fill: #ef4444; stroke: white; stroke-width: 2; }}
    .marker-turtle {{
      dominant-baseline: central;
      font-size: 24px;
      paint-order: stroke;
      stroke: rgba(255, 255, 255, .95);
      stroke-width: 5px;
      text-anchor: middle;
    }}
    .marker-recovery {{ fill: var(--recovery); stroke: white; stroke-width: 2; }}
    .obstacle {{ fill: rgba(220, 38, 38, .22); stroke: var(--obstacle); stroke-width: 2; }}
    .obstacle-label {{ fill: var(--obstacle); font-size: 10px; font-weight: 800; text-anchor: middle; paint-order: stroke; stroke: white; stroke-width: 3; }}
    .visual-corroborated {{ fill: rgba(13, 148, 136, .30); stroke: var(--visual-corroborated); stroke-width: 2.5; }}
    .visual-label {{ fill: var(--visual-corroborated); font-size: 10px; font-weight: 800; paint-order: stroke; stroke: white; stroke-width: 3; }}
    .label {{ fill: #0f172a; font-size: 12px; font-weight: 800; paint-order: stroke; stroke: white; stroke-width: 4; }}
    .legend {{
      position: absolute;
      top: 8px;
      left: 8px;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: rgba(255, 255, 255, .88);
      padding: 7px;
      font-size: .72rem;
    }}
    .legend span {{ white-space: nowrap; }}
    .evidence-note {{
      margin-top: 5px;
      max-width: 980px;
      color: #475569;
      font-size: .78rem;
      line-height: 1.45;
    }}
    .preview-ended {{ color: #15803d; font-weight: 800; }}
    .facts {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(min(220px, 100%), 1fr));
      gap: 8px;
    }}
    .fact {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      padding: 10px;
      min-width: 0;
    }}
    .fact span {{ display: block; color: var(--muted); font-size: .74rem; }}
    .fact strong {{ display: block; margin-top: 3px; overflow-wrap: anywhere; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>MissionOS Indoor Map</h1>
        <div class="muted">{escaped_robot_label}/Nav2 simulator local-XY evidence. This view is read-only and does not claim physical execution or payload delivery.</div>
        <div class="evidence-note" id="trajectoryTruth">Blue is persisted Nav2-bridge observed trajectory and is the final observation evidence. Purple is persisted recovery evidence. Green, when present, is only a high-rate /odom preview projected onto the map for operator orientation; projection jitter can make it look serpentine. Green is not persisted and is never verifier input.</div>
        <div class="muted" id="liveStatus">Snapshot loaded.</div>
      </div>
      <div class="pill-stack">
        <div class="pill" id="providerPill">Indoor local XY</div>
        <div class="pill clearance-pill" id="clearance3dPill" data-status="unavailable">3D clearance unavailable</div>
      </div>
    </header>
    <section id="map" class="map" aria-label="MissionOS {escaped_robot_label} indoor map"></section>
    <section class="facts" id="facts"></section>
  </main>
  <script id="mission-map-data" type="application/json">{model_json}</script>
  <script>
    let data = JSON.parse(document.getElementById("mission-map-data").textContent);
    const mapEl = document.getElementById("map");
    const factsEl = document.getElementById("facts");
    const liveStatusEl = document.getElementById("liveStatus");
    const clearance3dPillEl = document.getElementById("clearance3dPill");
    const terminalStatuses = new Set((data.live || {{}}).terminal_statuses || []);

    function statusText(value, fallback = "-") {{
      return value === null || value === undefined || value === "" ? fallback : String(value);
    }}

    function firstNumber(...values) {{
      for (const value of values) {{
        if (value === null || value === undefined || value === "") continue;
        const number = Number(value);
        if (Number.isFinite(number)) return number;
      }}
      return null;
    }}

    function points(records) {{
      return (Array.isArray(records) ? records : []).filter((point) =>
        point && Number.isFinite(Number(point.x_m)) && Number.isFinite(Number(point.y_m))
      );
    }}

    function pathD(records, project) {{
      return points(records).map(project).map((point, index) =>
        `${{index ? "L" : "M"}}${{point.x.toFixed(2)}} ${{point.y.toFixed(2)}}`
      ).join(" ");
    }}

    function pathGroupsBySegment(records) {{
      const groups = [];
      let current = [];
      let currentKey = null;
      for (const point of points(records)) {{
        const key = statusText(
          point.segment_ref || point.segment_label || point.segment_index,
          "unsegmented",
        );
        if (current.length && key !== currentKey) {{
          groups.push(current);
          current = [];
        }}
        current.push(point);
        currentKey = key;
      }}
      if (current.length) groups.push(current);
      return groups;
    }}

    function pathMarkup(records, cssClass, project, includeShadow = true) {{
      return pathGroupsBySegment(records).map((group) => {{
        const d = pathD(group, project);
        return d
          ? `${{includeShadow ? `<path class="path-shadow" d="${{d}}"></path>` : ""}}<path class="${{cssClass}}" d="${{d}}"></path>`
          : "";
      }}).join("");
    }}

    function escapeHtml(value) {{
      const element = document.createElement("div");
      element.textContent = String(value);
      return element.innerHTML;
    }}

    function modelFromTaskPayload(payload) {{
      const artifacts = payload && payload.artifacts ? payload.artifacts : (payload.task?.artifacts || {{}});
      const indoor = artifacts.turtlebot3_indoor_map_model
        || artifacts.turtlebot3_home_mission_execution?.turtlebot3_indoor_map_model
        || artifacts.summary?.turtlebot3_indoor_map_model
        || null;
      if (!indoor) return data;
      return {{
        ...data,
        ...indoor,
        task_id: statusText(payload.task_id || payload.task?.task_id, data.task_id),
        task_status: statusText(payload.status || payload.task?.status || payload.task?.task_status, data.task_status),
        generated_at: new Date().toISOString(),
      }};
    }}

    function render() {{
      const width = mapEl.clientWidth || 980;
      const height = mapEl.clientHeight || 560;
      const pad = 46;
      const boundary = data.room_boundary || {{}};
      const allPoints = [
        ...(data.planned_points || []),
        ...(data.observed_points || []),
        ...(data.live_display_points || []),
        ...((data.recovery || {{}}).observed_points || []),
        ...((data.recovery || {{}}).target ? [(data.recovery || {{}}).target] : []),
        ...(data.obstacles || []),
      ].filter((point) => point && Number.isFinite(Number(point.x_m)) && Number.isFinite(Number(point.y_m)));
      const xs = allPoints.map((point) => Number(point.x_m));
      const ys = allPoints.map((point) => Number(point.y_m));
      let minX = firstNumber(boundary.min_x_m, Math.min(...xs), -2.5);
      let maxX = firstNumber(boundary.max_x_m, Math.max(...xs), 1.0);
      let minY = firstNumber(boundary.min_y_m, Math.min(...ys), -1.0);
      let maxY = firstNumber(boundary.max_y_m, Math.max(...ys), 1.0);
      if (Math.abs(maxX - minX) < 0.5) {{ minX -= 0.5; maxX += 0.5; }}
      if (Math.abs(maxY - minY) < 0.5) {{ minY -= 0.5; maxY += 0.5; }}
      const scale = Math.min((width - pad * 2) / (maxX - minX), (height - pad * 2) / (maxY - minY));
      const roomW = (maxX - minX) * scale;
      const roomH = (maxY - minY) * scale;
      const roomX = (width - roomW) / 2;
      const roomY = (height - roomH) / 2;
      const project = (point) => ({{
        x: roomX + (Number(point.x_m) - minX) * scale,
        y: roomY + (maxY - Number(point.y_m)) * scale,
      }});
      const floorPlan = data.floor_plan || {{}};
      const rectFromBounds = (record, cssClass, labelClass = "room-label") => {{
        const minRectX = firstNumber(record.min_x_m);
        const maxRectX = firstNumber(record.max_x_m);
        const minRectY = firstNumber(record.min_y_m);
        const maxRectY = firstNumber(record.max_y_m);
        if (minRectX === null || maxRectX === null || minRectY === null || maxRectY === null) return "";
        const a = project({{ x_m: minRectX, y_m: minRectY }});
        const b = project({{ x_m: maxRectX, y_m: maxRectY }});
        const x = Math.min(a.x, b.x);
        const y = Math.min(a.y, b.y);
        const w = Math.abs(a.x - b.x);
        const h = Math.abs(a.y - b.y);
        const label = statusText(record.label || record.room_id || record.name);
        return `<rect class="${{cssClass}}" x="${{x.toFixed(2)}}" y="${{y.toFixed(2)}}" width="${{w.toFixed(2)}}" height="${{h.toFixed(2)}}"></rect><text class="${{labelClass}}" x="${{(x + 8).toFixed(2)}}" y="${{(y + 15).toFixed(2)}}">${{escapeHtml(label)}}</text>`;
      }};
      const rectFromCenter = (record, cssClass, labelClass = "furniture-label", labelDy = -6) => {{
        if (!record || !Number.isFinite(Number(record.x_m)) || !Number.isFinite(Number(record.y_m))) return "";
        const center = project(record);
        const sizeX = firstNumber(record.size_x_m, 0.3) * scale;
        const sizeY = firstNumber(record.size_y_m, 0.3) * scale;
        const x = center.x - sizeX / 2;
        const y = center.y - sizeY / 2;
        const label = statusText(record.label || record.name || record.kind);
        const labelOffsetX = firstNumber(record.label_offset_x_px, 0);
        const fallbackOffsetY = label.toLowerCase() === "person" ? 34 : null;
        const labelOffsetY = firstNumber(record.label_offset_y_px, fallbackOffsetY);
        const labelX = center.x + labelOffsetX;
        const labelY = labelOffsetY === null ? y + labelDy : center.y + labelOffsetY;
        return `<rect class="${{cssClass}}" x="${{x.toFixed(2)}}" y="${{y.toFixed(2)}}" width="${{sizeX.toFixed(2)}}" height="${{sizeY.toFixed(2)}}"><title>${{escapeHtml(statusText(record.kind || record.name))}}</title></rect><text class="${{labelClass}}" x="${{labelX.toFixed(2)}}" y="${{labelY.toFixed(2)}}">${{escapeHtml(label)}}</text>`;
      }};
      const roomMarkup = (Array.isArray(floorPlan.rooms) ? floorPlan.rooms : [])
        .map((room) => rectFromBounds(room, "room-zone"))
        .join("");
      const wallPolygon = Array.isArray(floorPlan.wall_polygon) ? floorPlan.wall_polygon : [];
      const wallMarkup = wallPolygon.length >= 3
        ? `<polygon class="arena-wall" points="${{wallPolygon.map((point) => {{
            const p = project(point);
            return `${{p.x.toFixed(2)}},${{p.y.toFixed(2)}}`;
          }}).join(" ")}}"><title>turtlebot3_world wall (Nav2 SLAM map)</title></polygon>`
        : "";
      const wallRects = Array.isArray(floorPlan.walls) ? floorPlan.walls : [];
      const wallRectMarkup = wallRects.map((wall) => {{
        if (!Number.isFinite(Number(wall.x_m)) || !Number.isFinite(Number(wall.y_m))) return "";
        const center = project(wall);
        const sizeX = firstNumber(wall.size_x_m, 0.15) * scale;
        const sizeY = firstNumber(wall.size_y_m, 0.15) * scale;
        const deg = -(firstNumber(wall.yaw_rad, 0) * 180 / Math.PI);
        return `<rect class="wall-rect" x="${{(center.x - sizeX / 2).toFixed(2)}}" y="${{(center.y - sizeY / 2).toFixed(2)}}" width="${{sizeX.toFixed(2)}}" height="${{sizeY.toFixed(2)}}" transform="rotate(${{deg.toFixed(2)}} ${{center.x.toFixed(2)}} ${{center.y.toFixed(2)}})"></rect>`;
      }}).join("");
      const pillars = Array.isArray(floorPlan.pillars) ? floorPlan.pillars : [];
      const pillarMarkup = pillars.map((pillar) => {{
        if (!Number.isFinite(Number(pillar.x_m)) || !Number.isFinite(Number(pillar.y_m))) return "";
        const p = project(pillar);
        const radius = firstNumber(pillar.radius_m, 0.15) * scale;
        const label = statusText(pillar.furniture_label || pillar.name);
        return `<circle class="pillar" cx="${{p.x.toFixed(2)}}" cy="${{p.y.toFixed(2)}}" r="${{radius.toFixed(2)}}"><title>${{escapeHtml(statusText(pillar.source))}}</title></circle><text class="furniture-label" x="${{p.x.toFixed(2)}}" y="${{(p.y - radius - 5).toFixed(2)}}">${{escapeHtml(label)}}</text>`;
      }}).join("");
      const pillarPositions = pillars
        .filter((pillar) => Number.isFinite(Number(pillar.x_m)) && Number.isFinite(Number(pillar.y_m)));
      const sitsOnPillar = (item) => pillarPositions.some((pillar) =>
        Math.abs(Number(pillar.x_m) - Number(item.x_m)) < 0.01
        && Math.abs(Number(pillar.y_m) - Number(item.y_m)) < 0.01);
      const furnitureMarkup = (Array.isArray(floorPlan.furniture) ? floorPlan.furniture : [])
        .filter((item) => !sitsOnPillar(item))
        .map((item) => rectFromCenter(item, "furniture"))
        .join("");
      const plannedD = pathD(data.planned_points || [], project);
      const recovery = data.recovery || {{}};
      const clearance3d = data.trajectory_clearance_3d || {{}};
      const clearance3dStatus = statusText(clearance3d.status, "unavailable");
      const clearance3dMinimum = firstNumber(clearance3d.minimum_surface_clearance_m);
      const clearance3dCandidates = Array.isArray(clearance3d.candidate_results)
        ? clearance3d.candidate_results
        : [];
      const clearance3dUnresolved = Array.isArray(clearance3d.unresolved_candidate_refs)
        ? clearance3d.unresolved_candidate_refs
        : [];
      clearance3dPillEl.dataset.status = clearance3dStatus;
      clearance3dPillEl.textContent = `3D ${{clearance3dStatus}} · candidates=${{clearance3dCandidates.length}}/${{clearance3dUnresolved.length}} unresolved · min=${{clearance3dMinimum === null ? "-" : clearance3dMinimum.toFixed(3) + "m"}}`;
      const recoveryPoints = [
        ...(recovery.observed_points || []),
        ...(recovery.target ? [recovery.target] : []),
      ];
      const liveEnded = (data.live_telemetry || {{}}).telemetry_status === "ended";
      const observedMarkup = pathMarkup(data.observed_points || [], "observed-path", project);
      const liveMarkup = pathMarkup(
        data.live_display_points || [],
        liveEnded ? "live-path live-path-ended" : "live-path",
        project,
        false,
      );
      const recoveryMarkup = pathMarkup(recoveryPoints, "recovery-path", project);
      const recoveryLabel = recovery.selected_action === "avoid_obstacle"
        ? "avoid obstacle"
        : recovery.selected_action === "return_home"
          ? "return home"
          : statusText(recovery.selected_action, "recovery");
      const planned = points(data.planned_points || []);
      const home = planned.find((point) => point.role === "home") || planned[0];
      const dropoff = [...planned].reverse().find((point) => point.role === "dropoff") || planned[planned.length - 1];
      const current = (!liveEnded ? points(data.live_display_points || []).at(-1) : null)
        || data.current_pose
        || points(data.observed_points || []).at(-1)
        || points(recovery.observed_points || []).at(-1);
      const obstacleMarkup = points(data.obstacles || []).map((obstacle) => {{
        return rectFromCenter(obstacle, "obstacle", "obstacle-label", -7);
      }}).join("");
      const visualObservations = Array.isArray(data.visual_observations)
        ? data.visual_observations
        : [];
      const corroboratedObservations = visualObservations.filter((observation) => {{
        const projection = observation && observation.map_projection;
        return observation
          && observation.display_status === "camera_lidar_corroborated"
          && projection && projection.status === "projected"
          && Number.isFinite(Number(projection.x_m))
          && Number.isFinite(Number(projection.y_m));
      }});
      const cameraOnlyCount = visualObservations.length - corroboratedObservations.length;
      const visualObservationMarkup = corroboratedObservations.map((observation) => {{
        const projection = observation.map_projection;
        const center = project({{ x_m: Number(projection.x_m), y_m: Number(projection.y_m) }});
        const candidate = statusText(observation.semantic_candidate, "unknown_obstacle");
        const confidence = Number(observation.camera_confidence);
        const confidenceText = Number.isFinite(confidence) ? confidence.toFixed(2) : "-";
        const rangeText = Number.isFinite(Number(projection.range_m))
          ? `${{Number(projection.range_m).toFixed(2)}}m`
          : "-";
        const frameRef = statusText(observation.source_frame_ref).replace("sha256:", "").slice(0, 12);
        const tooltip = [
          `candidate: ${{candidate}} (${{confidenceText}})`,
          `camera+LiDAR corroborated · range ${{rangeText}}`,
          `frame ${{frameRef}} · binding ${{statusText(observation.binding_status)}}`,
          "evidence only — no approval, dispatch, or delivery claim",
        ].join(" · ");
        return `<circle class="visual-corroborated" cx="${{center.x.toFixed(2)}}" cy="${{center.y.toFixed(2)}}" r="9"><title>${{escapeHtml(tooltip)}}</title></circle><text class="visual-label" x="${{(center.x + 12).toFixed(2)}}" y="${{(center.y - 10).toFixed(2)}}">${{escapeHtml(`${{candidate}} ${{confidenceText}}`)}}</text>`;
      }}).join("");
      const marker = (point, cssClass, label, labelDx = 12, labelDy = -10) => {{
        if (!point) return "";
        const p = project(point);
        return `<circle class="${{cssClass}}" cx="${{p.x.toFixed(2)}}" cy="${{p.y.toFixed(2)}}" r="8"></circle><text class="label" x="${{(p.x + labelDx).toFixed(2)}}" y="${{(p.y + labelDy).toFixed(2)}}">${{label}}</text>`;
      }};
      const turtleMarker = (point) => {{
        if (!point) return "";
        const p = project(point);
        return `<text class="marker-turtle" x="${{p.x.toFixed(2)}}" y="${{p.y.toFixed(2)}}">{TURTLEBOT3_MAP_ICON}</text><text class="label" x="${{(p.x + 18).toFixed(2)}}" y="${{(p.y + 16).toFixed(2)}}">TurtleBot3</text>`;
      }};
      mapEl.innerHTML = `
        <svg viewBox="0 0 ${{width}} ${{height}}">
          <rect class="room" x="${{roomX.toFixed(2)}}" y="${{roomY.toFixed(2)}}" width="${{roomW.toFixed(2)}}" height="${{roomH.toFixed(2)}}"></rect>
          ${{roomMarkup}}
          ${{wallMarkup}}
          ${{wallRectMarkup}}
          ${{pillarMarkup}}
          ${{furnitureMarkup}}
          ${{plannedD ? `<path class="path-shadow" d="${{plannedD}}"></path><path class="planned-path" d="${{plannedD}}"></path>` : ""}}
          ${{liveMarkup}}
          ${{recoveryMarkup}}
          ${{observedMarkup}}
          ${{obstacleMarkup}}
          ${{visualObservationMarkup}}
          ${{marker(home, "marker-home", "H home")}}
          ${{marker(dropoff, "marker-dropoff", dropoff && dropoff.room_label ? `D dropoff · ${{escapeHtml(statusText(dropoff.room_label))}}` : "D dropoff", 12, -18)}}
          ${{marker(recovery.target, "marker-recovery", recoveryLabel, 12, 18)}}
          ${{turtleMarker(current)}}
        </svg>
        <div class="legend">
          <span style="color: var(--plan)">plan</span>
          <span style="color: var(--observed); font-weight: 800">blue: persisted observed trajectory (final evidence)</span>
          ${{points(data.live_display_points || []).length ? `<span style="color: #16a34a">${{liveEnded ? "live preview ended — not evidence" : "green: live /odom preview — display-only, not evidence"}}</span>` : ""}}
          <span>{TURTLEBOT3_MAP_ICON} TurtleBot3</span>
          <span style="color: var(--furniture)">furniture</span>
          <span style="color: #334155">wall</span>
          <span style="color: var(--recovery)">purple: persisted recovery evidence</span>
          <span style="color: var(--obstacle)">gray/red: scene blocker (harness-placed)</span>
          <span style="color: var(--visual-corroborated); font-weight: 800">teal: camera+LiDAR corroborated observation</span>
        </div>
      `;
      factsEl.innerHTML = [
        ["task", data.task_id],
        ["status", data.task_status || data.mission_status || "-"],
        ["mission", data.mission_kind || "-"],
        ["frame", data.frame_id || "map"],
        ["planned", `${{points(data.planned_points || []).length}}pts`],
        ["observed", `${{points(data.observed_points || []).length}}pts · source=${{statusText(data.observed_pose_source)}}`],
        ["obstacles", `${{points(data.obstacles || []).length}} · observed=${{statusText((data.obstacles || [])[0]?.observed)}}`],
        ["visual observations", `${{corroboratedObservations.length}} corroborated · ${{cameraOnlyCount}} camera-only (no map position) · evidence only`],
        ["floor plan", statusText((data.floor_plan || {{}}).floor_plan_id)],
        ["furniture", `${{Array.isArray((data.floor_plan || {{}}).furniture) ? data.floor_plan.furniture.length : 0}} · on_sim_pillars=${{Array.isArray((data.floor_plan || {{}}).pillars) && data.floor_plan.pillars.length > 0}}`],
        ["display decimation", `${{statusText((data.display_decimation || {{}}).method)}} · ${{statusText((data.display_decimation || {{}}).raw_point_count)}}→${{statusText((data.display_decimation || {{}}).display_point_count)}}pts`],
        ["2D centerline clearance", `clear=${{statusText((data.obstacles || [])[0]?.trajectory_clearance_observed)}} · intersects=${{statusText((data.obstacles || [])[0]?.trajectory_intersects_obstacle)}}`],
        ["3D swept-volume clearance", `status=${{clearance3dStatus}} · candidates=${{clearance3dCandidates.length}} · unresolved=${{clearance3dUnresolved.length}} · clear=${{statusText(clearance3d.clearance_observed)}} · collision=${{statusText(clearance3d.collision_observed)}} · minimum=${{clearance3dMinimum === null ? "-" : clearance3dMinimum.toFixed(3) + "m"}} · evidence only`],
        ["recovery", `triggered=${{statusText(recovery.triggered)}} · action=${{statusText(recovery.selected_action)}} · completion=${{statusText(recovery.completion_claimed)}}`],
        ["recovery phase", `${{statusText(recovery.runtime_status)}} · segments=${{statusText(recovery.route_segment_completion_count)}}/${{statusText(recovery.route_segment_planned_count)}} · resumed=${{statusText(recovery.route_resumed_after_recovery)}}`],
        ["recovery status", `goal=${{statusText(recovery.goal_status)}} · verification=${{statusText(recovery.verification_status)}} · route=${{statusText(recovery.route_resume_status)}}`],
        ["live /odom preview (not evidence)", `${{statusText((data.live_telemetry || {{}}).telemetry_status)}} · process-local samples=${{points(data.live_display_points || []).length}} · display path=${{statusText((data.live_telemetry || {{}}).display_path_length_m)}}m · captured=${{statusText((data.live_telemetry || {{}}).captured_at)}}`],
        ["motion", `observed=${{statusText((data.motion || {{}}).robot_motion_observed)}} · odom=${{statusText((data.motion || {{}}).odom_delta_m)}}m`],
        ["display alignment", `${{statusText((data.display_alignment || {{}}).method)}} · applied=${{statusText((data.display_alignment || {{}}).applied)}} · dx=${{statusText((data.display_alignment || {{}}).dx_m)}}m · dy=${{statusText((data.display_alignment || {{}}).dy_m)}}m`],
        ["boundary", statusText((data.room_boundary || {{}}).claim_boundary)],
        ["physical", statusText(data.physical_execution_invoked)],
        ["generated", data.generated_at],
      ].map(([key, value]) => `<div class="fact"><span>${{key}}</span><strong><code>${{escapeHtml(value)}}</code></strong></div>`).join("");
    }}

    async function refreshLive() {{
      const live = data.live || {{}};
      if (!live.enabled || !live.task_url) return;
      try {{
        const response = await fetch(live.task_url, {{ cache: "no-store" }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        data = modelFromTaskPayload(await response.json());
        render();
        const previewEnded = (data.live_telemetry || {{}}).telemetry_status === "ended";
        liveStatusEl.textContent = previewEnded
          ? `Live preview ended — not evidence · final status=${{data.task_status || "-"}}`
          : `Live: updated ${{new Date().toLocaleTimeString()}} · status=${{data.task_status || "-"}}`;
        liveStatusEl.classList.toggle("preview-ended", previewEnded);
        if (terminalStatuses.has(data.task_status) && window.__missionIndoorTimer) {{
          window.clearInterval(window.__missionIndoorTimer);
          window.__missionIndoorTimer = null;
        }}
      }} catch (error) {{
        liveStatusEl.textContent = `Live update failed: ${{error.message}}`;
      }}
    }}

    window.addEventListener("resize", render);
    render();
    if (data.live && data.live.enabled && data.live.task_url) {{
      liveStatusEl.textContent = `Live: polling Gateway`;
      window.__missionIndoorTimer = window.setInterval(refreshLive, data.live.poll_interval_ms || 1000);
      refreshLive();
    }} else {{
      const previewEnded = (data.live_telemetry || {{}}).telemetry_status === "ended"
        && points(data.live_display_points || []).length > 0;
      liveStatusEl.textContent = previewEnded
        ? "Live preview ended — not evidence"
        : "Snapshot: persisted blue/purple evidence only; no live preview restored";
      liveStatusEl.classList.toggle("preview-ended", previewEnded);
    }}
  </script>
</body>
</html>
"""
