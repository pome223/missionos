/* Offline, procedural Starship V3-inspired display geometry.
 * Public design anchors: 52 m Ship, 72 m Super Heavy, 9 m diameter,
 * 3 + 3 Ship engines, 33 booster engines, four flaps and three grid fins.
 * Dimensions beyond those anchors, surface details, hinge geometry, satellite
 * design and launch-site geometry are illustrative, not engineering CAD.
 * Geometry is in metres, right-handed, +Y up, vehicle base at Y = 0.
 * The windward heat shield faces -Z. This model supplies no flight physics.
 */
(function (global) {
  "use strict";

  const TAU = Math.PI * 2;
  const STEEL = [0.63, 0.69, 0.72];
  const DARK_STEEL = [0.24, 0.29, 0.32];
  const SHIELD = [0.035, 0.043, 0.05];
  const ENGINE = [0.18, 0.23, 0.26];

  function geometry() { return {positions: [], normals: []}; }
  function unit(v) {
    const d = Math.hypot(v[0], v[1], v[2]) || 1;
    return v.map(x => x / d);
  }
  function triangle(g, a, b, c, na, nb, nc) {
    const u = b.map((x, i) => x - a[i]);
    const v = c.map((x, i) => x - a[i]);
    const cross = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
    const n = na || unit(cross);
    nb = nb || n;
    nc = nc || n;
    if (cross.reduce((s, x, i) => s + x * n[i], 0) < 0) {
      [b, c] = [c, b];
      [nb, nc] = [nc, nb];
    }
    g.positions.push(...a, ...b, ...c);
    g.normals.push(...n, ...nb, ...nc);
  }
  function quad(g, a, b, c, d, n) {
    triangle(g, a, b, c, n);
    triangle(g, a, c, d, n);
  }
  function box(width = 1, height = 1, depth = 1) {
    const g = geometry(), x = width / 2, y = height / 2, z = depth / 2;
    quad(g, [x,-y,-z], [x,y,-z], [x,y,z], [x,-y,z], [1,0,0]);
    quad(g, [-x,-y,z], [-x,y,z], [-x,y,-z], [-x,-y,-z], [-1,0,0]);
    quad(g, [-x,y,-z], [-x,y,z], [x,y,z], [x,y,-z], [0,1,0]);
    quad(g, [-x,-y,z], [-x,-y,-z], [x,-y,-z], [x,-y,z], [0,-1,0]);
    quad(g, [-x,-y,z], [x,-y,z], [x,y,z], [-x,y,z], [0,0,1]);
    quad(g, [x,-y,-z], [-x,-y,-z], [-x,y,-z], [x,y,-z], [0,0,-1]);
    return g;
  }
  function revolve(profile, segments = 64, start = 0, end = TAU) {
    const g = geometry();
    for (let j = 0; j < profile.length - 1; j++) {
      const [y0, r0] = profile[j], [y1, r1] = profile[j + 1];
      const slope = (r1 - r0) / Math.max(1e-9, y1 - y0);
      for (let i = 0; i < segments; i++) {
        const a = start + (end - start) * i / segments;
        const b = start + (end - start) * (i + 1) / segments;
        const va = [r0 * Math.cos(a), y0, r0 * Math.sin(a)];
        const vb = [r0 * Math.cos(b), y0, r0 * Math.sin(b)];
        const vc = [r1 * Math.cos(b), y1, r1 * Math.sin(b)];
        const vd = [r1 * Math.cos(a), y1, r1 * Math.sin(a)];
        const na = unit([Math.cos(a), -slope, Math.sin(a)]);
        const nb = unit([Math.cos(b), -slope, Math.sin(b)]);
        if (r0 > 0) triangle(g, va, vb, vd, na, nb, na);
        if (r1 > 0) triangle(g, vb, vc, vd, nb, nb, na);
      }
    }
    return g;
  }
  function disk(radius, y = 0, segments = 64, upward = true) {
    const g = geometry(), n = [0, upward ? 1 : -1, 0];
    for (let i = 0; i < segments; i++) {
      const a = TAU * i / segments, b = TAU * (i + 1) / segments;
      triangle(g, [0,y,0], [radius*Math.cos(a),y,radius*Math.sin(a)], [radius*Math.cos(b),y,radius*Math.sin(b)], n);
    }
    return g;
  }
  function cylinder(radius = 1, height = 1, segments = 48, caps = true) {
    const g = revolve([[0,radius], [height,radius]], segments);
    if (caps) { append(g, disk(radius, 0, segments, false)); append(g, disk(radius, height, segments, true)); }
    return g;
  }
  function cone(radius = 1, height = 1, segments = 48) {
    return revolve([[0,radius], [height,0]], segments);
  }
  function sphere(radius = 1, segments = 48, rings = 24) {
    const profile = [];
    for (let j = 0; j <= rings; j++) {
      const a = -Math.PI / 2 + Math.PI * j / rings;
      profile.push([radius * Math.sin(a), j === 0 || j === rings ? 0 : radius * Math.cos(a)]);
    }
    return revolve(profile, segments);
  }
  function rotate(p, r) {
    let [x,y,z] = p;
    let s = Math.sin(r[0]), c = Math.cos(r[0]); [y,z] = [y*c-z*s, y*s+z*c];
    s = Math.sin(r[1]); c = Math.cos(r[1]); [x,z] = [x*c+z*s, -x*s+z*c];
    s = Math.sin(r[2]); c = Math.cos(r[2]); [x,y] = [x*c-y*s, x*s+y*c];
    return [x,y,z];
  }
  function append(target, source, position = [0,0,0], rotation = [0,0,0]) {
    for (let i = 0; i < source.positions.length; i += 3) {
      const p = rotate(source.positions.slice(i, i + 3), rotation);
      const n = rotate(source.normals.slice(i, i + 3), rotation);
      target.positions.push(p[0]+position[0], p[1]+position[1], p[2]+position[2]);
      target.normals.push(...n);
    }
    return target;
  }
  function polygon(points, thickness = 0.2) {
    // All flap polygons are convex. End faces are triangulated about their centre.
    const g = geometry(), z = thickness / 2;
    const centre = points.reduce((a, p) => [a[0]+p[0]/points.length, a[1]+p[1]/points.length], [0,0]);
    const winding = Math.sign(points.reduce((sum, a, i) => {
      const b = points[(i + 1) % points.length];
      return sum + a[0]*b[1] - b[0]*a[1];
    }, 0)) || 1;
    for (let i = 0; i < points.length; i++) {
      const a = points[i], b = points[(i + 1) % points.length];
      triangle(g, [...centre,z], [...a,z], [...b,z], [0,0,1]);
      triangle(g, [...centre,-z], [...b,-z], [...a,-z], [0,0,-1]);
      const n = unit([winding*(b[1]-a[1]), winding*(a[0]-b[0]), 0]);
      quad(g, [...a,-z], [...b,-z], [...b,z], [...a,z], n);
    }
    return g;
  }
  function part(g, color, position = [0,0,0], rotation = [0,0,0], tag = "", emissive = 0) {
    return {geometry:g, color, position, rotation, scale:[1,1,1], emissive, tag};
  }
  function rod(a, b, radius, color, tag = "") {
    const v = b.map((x,i) => x-a[i]), length = Math.hypot(...v);
    // Rotate local +Y onto the line using X then Y rotations.
    const rotation = [Math.acos(Math.max(-1, Math.min(1, v[1]/length))), Math.atan2(v[0], v[2]), 0];
    return part(cylinder(radius, length, 8), color, a, rotation, tag);
  }
  function mergeParts(parts, tag) {
    const grouped = new Map(), result = [];
    for (const p of parts) {
      if (p.tag && p.tag.startsWith("engine_")) { result.push(p); continue; }
      const key = p.color.join(",") + ":" + p.emissive;
      if (!grouped.has(key)) grouped.set(key, part(geometry(), p.color, [0,0,0], [0,0,0], tag, p.emissive));
      append(grouped.get(key).geometry, p.geometry, p.position, p.rotation);
    }
    return result.concat([...grouped.values()]);
  }

  function engineBell(radius, height, x, z, tag, index) {
    const profile = [[0,radius], [height*.12,radius*.98], [height*.35,radius*.78], [height*.63,radius*.49], [height*.82,radius*.32], [height,radius*.34]];
    const outer = revolve(profile, 32);
    const inner = revolve(profile.map(([y,r]) => [y+.015, Math.max(.02,r-.065)]), 32);
    // The bell is hollow; inward-facing normals make its dark interior visible.
    for (let i = 0; i < inner.normals.length; i++) inner.normals[i] *= -1;
    for (let i = 0; i < inner.positions.length; i += 9) {
      for (let j = 0; j < 3; j++) {
        [inner.positions[i+3+j],inner.positions[i+6+j]] = [inner.positions[i+6+j],inner.positions[i+3+j]];
        [inner.normals[i+3+j],inner.normals[i+6+j]] = [inner.normals[i+6+j],inner.normals[i+3+j]];
      }
    }
    return [part(outer, ENGINE, [x,.1,z], [0,0,0], tag), part(inner, [0.025,.032,.037], [x,.1,z]), part(revolve([[0,radius+.028],[.055,radius+.028]], 32), [.39,.45,.48], [x,.1,z])]
      .map(p => ({...p, actuator:{kind:"engine", index}}));
  }

  function heatTiles(yStart, yEnd, radius) {
    // Deliberately simplified hexagonal pattern: not a map of real vehicle tiles.
    const groups = [geometry(), geometry(), geometry()];
    const tile = .43, rowStep = tile*1.5, arcStep = tile*Math.sqrt(3);
    for (let row = 0, y = yStart; y < yEnd; row++, y += rowStep) {
      for (let col = 0, arc = arcStep/2 + (row%2)*arcStep/2; arc < Math.PI*radius-arcStep/2; col++, arc += arcStep) {
        const theta = Math.PI + arc/radius;
        const centre = [(radius+.012)*Math.cos(theta), y, (radius+.012)*Math.sin(theta)];
        const nc = [Math.cos(theta),0,Math.sin(theta)];
        for (let k = 0; k < 6; k++) {
          const angleA = Math.PI/6 + TAU*k/6, angleB = Math.PI/6 + TAU*(k+1)/6;
          const vertex = a => {
            const t = theta + .95*tile*Math.cos(a)/radius;
            return {p:[(radius+.014)*Math.cos(t), y+.95*tile*Math.sin(a), (radius+.014)*Math.sin(t)], n:[Math.cos(t),0,Math.sin(t)]};
          };
          const a = vertex(angleA), b = vertex(angleB);
          triangle(groups[(row*7+col*11)%3], centre, a.p, b.p, nc, a.n, b.n);
        }
      }
    }
    return groups.map((g,i) => part(g, [.050+i*.006,.058+i*.006,.064+i*.006], [0,0,0], [0,0,0], "illustrative_heat_tiles"));
  }

  function makeShip(engineAnchors) {
    const p = [], radius = 4.5;
    p.push(part(revolve([[2.5,radius],[40,radius]], 80, 0, Math.PI), STEEL));
    p.push(part(revolve([[2.5,radius],[40,radius]], 80, Math.PI, TAU), SHIELD));
    const nose = [];
    for (let i = 0; i <= 28; i++) {
      const a = (Math.PI/2)*i/28;
      nose.push([40+12*Math.sin(a), i===28 ? 0 : radius*Math.cos(a)]);
    }
    p.push(part(revolve(nose, 64, 0, Math.PI), STEEL, [0,0,0], [0,0,0], "nose_stainless"));
    p.push(part(revolve(nose, 64, Math.PI, TAU), SHIELD, [0,0,0], [0,0,0], "nose_heatshield"));
    p.push(...heatTiles(3.1, 39.6, radius));
    for (let y = 3.2; y < 40; y += 1.85) {
      p.push(part(revolve([[y,4.514],[y+.045,4.514]], 80, 0, Math.PI), [.37,.43,.46]));
    }
    p.push(part(revolve([[2.2,4.42],[2.5,4.5]], 80), DARK_STEEL));
    p.push(part(disk(4.4, 2.3, 64, false), [.10,.13,.15]));
    for (let i = 0; i < 3; i++) {
      const a = TAU*i/3;
      const x = 1.12*Math.cos(a), z = 1.12*Math.sin(a), tag = "engine_sl_"+i;
      p.push(...engineBell(.73, 2.05, x, z, tag, i));
      engineAnchors.push({position:[x,.1,z],kind:"sl",tag});
      const av = a+Math.PI/3, xv = 2.85*Math.cos(av), zv = 2.85*Math.sin(av), vtag = "engine_rvac_"+i;
      p.push(...engineBell(1.13, 2.5, xv, zv, vtag, i+3));
      engineAnchors.push({position:[xv,.1,zv],kind:"rvac",tag:vtag});
    }
    for (const side of [-1,1]) {
      const aft = [[4.05,5.2],[9.6,4.4],[10.1,11.1],[4.1,15.5]].map(([x,y]) => [side*x,y]);
      const fore = [[3.9,33.3],[7.5,35.6],[6.4,40.5],[3.55,42.7]].map(([x,y]) => [side*x,y]);
      // Hinge locations are display proxies. The recorded physics angle drives
      // both faces about the same axis; no independent flap animation is used.
      const aftActuator={kind:"flap",name:"flap_aft_"+side,index:side<0?3:4,pivot:[side*4.13,10,-.6],axis:[0,side,0]};
      const foreActuator={kind:"flap",name:"flap_forward_"+side,index:side<0?5:6,pivot:[side*3.92,38,-.68],axis:[0,side,0]};
      p.push({...part(polygon(aft,.26), DARK_STEEL, [0,0,-.6], [0,0,0], "flap_aft_"+side),actuator:aftActuator});
      p.push({...part(polygon(aft,.025), SHIELD, [0,0,-.746]),actuator:aftActuator});
      p.push({...part(polygon(fore,.22), DARK_STEEL, [0,0,-.68], [0,0,0], "flap_forward_"+side),actuator:foreActuator});
      p.push({...part(polygon(fore,.025), SHIELD, [0,0,-.806]),actuator:foreActuator});
      p.push(rod([side*4.13,5.5,-.47],[side*4.13,14.8,-.47],.13, [.42,.47,.49]));
      p.push(rod([side*3.92,34,-.57],[side*3.92,40.7,-.57],.1, [.42,.47,.49]));
    }
    // Rectangular side aperture is a visual reference to the dispenser, not CAD.
    p.push(part(box(2.75,.78,.08), [.025,.035,.04], [0,34.1,4.49], [0,0,0], "payload_door"));
    p.push(part(box(2.95,.10,.14), [.43,.48,.50], [0,34.55,4.51]));
    p.push(part(box(2.95,.10,.14), [.43,.48,.50], [0,33.65,4.51]));
    // Small cold-gas/RCS-like dark openings, illustrative positions only.
    for (const y of [6.2,37.6]) for (const x of [-2.7,2.7]) {
      p.push(part(box(.3,.34,.11), [.05,.07,.09], [x,y,3.62], [0,x>0?.57:-.57,0]));
    }
    return p;
  }

  function makeBooster(engineAnchors) {
    const p = [];
    p.push(part(revolve([[3.0,4.5],[69.1,4.5]], 96), [.59,.65,.69]));
    for (let y = 3.5, row = 0; y < 69; y += 1.9, row++) {
      p.push(part(revolve([[y,4.512],[y+.045,4.512]], 96), row%3 ? [.36,.42,.45] : [.44,.49,.52]));
    }
    p.push(part(disk(4.4,3.05,64,false), [.105,.13,.15]));
    p.push(part(revolve([[2.65,4.38],[3.2,4.5]], 64), [.27,.33,.36]));
    const rings = [{count:3,r:1.0,start:0},{count:10,r:2.33,start:.08},{count:20,r:3.76,start:0}];
    let index = 0;
    for (const ring of rings) for (let i = 0; i < ring.count; i++) {
      const a = TAU*i/ring.count+ring.start, x = ring.r*Math.cos(a), z = ring.r*Math.sin(a), tag = "engine_sl_"+index++;
      p.push(...engineBell(.51,2.55,x,z,tag,index-1));
      engineAnchors.push({position:[x,.1,z],kind:"sl",tag});
    }
    // Integrated hot-stage ring: open vertical slots, not a jettisoned ring.
    p.push(part(revolve([[69.1,4.5],[69.45,4.5]], 96), DARK_STEEL));
    p.push(part(revolve([[71.55,4.5],[72,4.5]], 96), STEEL));
    p.push(part(revolve([[69.3,4.14],[71.75,4.14]], 96), [.055,.068,.074]));
    for (let i = 0; i < 48; i++) {
      const a = TAU*i/48;
      p.push(part(box(.13,2.15,.19), [.49,.55,.58], [4.45*Math.cos(a),70.5,4.45*Math.sin(a)], [0,-a,0]));
    }
    // Three simplified deployed lattice fins, 120 degrees apart.
    const lattice = [];
    for (const x of [-1.9,1.9]) lattice.push(part(box(.16,.28,4.2), DARK_STEEL, [x,0,0]));
    for (const z of [-2.1,2.1]) lattice.push(part(box(3.8,.28,.16), DARK_STEEL, [0,0,z]));
    for (let x = -1.45; x <= 1.46; x += .48) lattice.push(part(box(.055,.23,4.05), [.34,.40,.43], [x,0,0]));
    for (let z = -1.65; z <= 1.66; z += .47) lattice.push(part(box(3.65,.23,.055), [.34,.40,.43], [0,0,z]));
    const finGeometry = geometry();
    for (const q of lattice) append(finGeometry,q.geometry,q.position,q.rotation);
    for (let i = 0; i < 3; i++) {
      // Physics body (x,y,z) maps to display (x,z,-y).
      const a = -TAU*i/3;
      p.push({...part(finGeometry, [.34,.4,.43], [6.35*Math.cos(a),65,6.35*Math.sin(a)], [0,-a,0], "grid_fin_"+i),
        actuator:{kind:"flap",name:"grid_fin_"+i,index:3+i,pivot:[4.45*Math.cos(a),65,4.45*Math.sin(a)],axis:[Math.cos(a),0,Math.sin(a)]}});
      p.push(part(box(1.25,.7,1), DARK_STEEL, [4.45*Math.cos(a),65,4.45*Math.sin(a)], [0,-a,0]));
    }
    // Longitudinal external raceways, approximate location and section.
    for (const a of [.2,Math.PI+.2]) {
      p.push(part(box(.22,47,.18), [.46,.53,.56], [4.5*Math.cos(a),30,4.5*Math.sin(a)], [0,-a,0]));
    }
    return p;
  }

  function makeSatellite() {
    const p = [];
    p.push(part(box(3.6,.45,2.8), [.72,.73,.69], [0,.25,0], [0,0,0], "satellite_bus_assumed"));
    p.push(part(box(3.42,.04,2.63), [.23,.28,.32], [0,-.005,0]));
    p.push(part(box(3.45,.10,2.6), [.83,.85,.84], [0,.53,0]));
    for (const side of [-1,1]) {
      p.push(part(box(11.8,.045,3.3), [.025,.08,.18], [side*7.9,.3,0], [0,0,0], "solar_array_assumed"));
      for (let i = 0; i <= 16; i++) p.push(part(box(.026,.052,3.32), [.15,.28,.39], [side*7.9-5.9+i*11.8/16,.31,0]));
      for (const z of [-1.65,-.82,0,.82,1.65]) p.push(part(box(11.82,.052,.024), [.15,.28,.39], [side*7.9,.31,z]));
      p.push(part(box(.36,.1,1.1), [.65,.67,.65], [side*1.94,.3,0]));
    }
    p.push(part(cylinder(.18,.12,20), [.22,.24,.26], [0,.61,.8]));
    return mergeParts(p,"satellite_illustrative");
  }

  function makeTower(withStaticArms = true) {
    const p = [], size = 12, height = 142;
    const column = [.30,.34,.36], braces = [.39,.43,.44];
    for (const x of [-size/2,size/2]) for (const z of [-size/2,size/2]) {
      p.push(part(box(.9,height,.9), column, [x,height/2,z]));
    }
    for (let y = 4; y < height; y += 14) {
      p.push(part(box(size,.5,size), [.22,.27,.29], [0,y,0]));
      for (const z of [-size/2,size/2]) {
        p.push(rod([-size/2,y,z],[size/2,Math.min(height,y+14),z],.22,braces));
        p.push(rod([size/2,y,z],[-size/2,Math.min(height,y+14),z],.22,braces));
      }
      for (const x of [-size/2,size/2]) {
        p.push(rod([x,y,-size/2],[x,Math.min(height,y+14),size/2],.22,braces));
      }
    }
    p.push(part(box(13,2,13), [.44,.47,.48], [0,141,0]));
    // Catch arms are static visual site context; never a simulated catch receipt.
    for (const z of withStaticArms?[-6,6]:[]) {
      p.push(part(box(23,1.5,1.2), [.48,.51,.52], [15,80,z], [0,0,0], "static_catch_arm"));
      p.push(part(box(23,.3,.25), [.20,.23,.24], [15,81,z]));
    }
    if(withStaticArms)p.push(part(box(15,1.15,1.35), [.52,.55,.54], [12,114,0], [0,0,0], "static_ship_arm"));
    p.push(part(box(7,7,4), [.56,.59,.59], [0,88,7]));
    return mergeParts(p,"tower_illustrative");
  }

  function makePad() {
    const p = [];
    p.push(part(cylinder(11,1.2,64), [.23,.26,.27], [0,0,0]));
    p.push(part(revolve([[10.4,6.8],[12.1,6.8]],64), [.44,.47,.47]));
    p.push(part(revolve([[10.4,5],[12.1,5]],64), [.11,.14,.16]));
    for (let i = 0; i < 6; i++) {
      const a = TAU*i/6;
      p.push(rod([8.4*Math.cos(a),1.2,8.4*Math.sin(a)],[5.8*Math.cos(a),11.2,5.8*Math.sin(a)],.7,[.40,.44,.45]));
    }
    // The viewer positions the vehicle above this illustrative mount.
    p.push(part(box(60,.22,48), [.18,.22,.23], [0,-.13,0]));
    for (const z of [-14,14]) p.push(part(box(42,.025,.12), [.67,.57,.27], [0,.005,z]));
    for (const x of [-21,21]) p.push(part(box(.12,.025,28), [.67,.57,.27], [x,.005,0]));
    return mergeParts(p,"launch_mount_illustrative");
  }

  function build() {
    const engineAnchors = {ship:[],booster:[]};
    return {
      ship:makeShip(engineAnchors.ship),
      booster:makeBooster(engineAnchors.booster),
      satellite:makeSatellite(),
      tower:makeTower(),
      catchTower:makeTower(false),
      pad:makePad(),
      metadata:{
        shipHeightM:52, boosterHeightM:72, shipRadiusM:4.5, boosterRadiusM:4.5,
        stackHeightM:124, launchMountHeightM:12.1, engineAnchors,
        windwardDirection:[0,0,-1], vehicleUpDirection:[0,1,0],
        geometryClassification:"public_dimension_anchored_illustrative_mesh",
        engineeringCad:false, satelliteGeometry:"assumed_deployed_configuration",
        movingFlapGeometry:"recorded_actuator_angles_when_available", hingeGeometry:"illustrative_not_engineering_cad", launchSiteGeometry:"illustrative_not_georeferenced"
      }
    };
  }

  global.MissionOSStarshipModels = {build,geometry:{box,cylinder,cone,sphere,revolve,disk,append},version:"1.0.0"};
})(typeof window !== "undefined" ? window : globalThis);
