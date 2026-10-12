/* Offline WebGL presentation of recorded MissionOS simulation states.
 * World axes: east, up, minus north. Geometry and effects are illustrative;
 * this renderer never advances the mission or writes a physics state.
 */
(function () {
  "use strict";

  const EARTH_RADIUS = 6378137;
  const TAU = Math.PI * 2;
  // Presentation cutoff only: finite actuator decay can leave nonzero recorded
  // throttle/thrust long after shutdown. Keep those exact values in diagnostics;
  // flame visibility does not define ignition, physical force or engine health.
  const FLAME_VISIBILITY_MIN_THROTTLE = 1e-6;
  const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
  const number = (x, fallback = 0) => Number.isFinite(Number(x)) ? Number(x) : fallback;
  const length = (v) => Math.hypot(v[0], v[1], v[2]);
  const normalize = (v) => {
    const n = length(v);
    return n > 1e-12 ? v.map((x) => x / n) : [0, 1, 0];
  };
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const subtract = (a, b) => a.map((x, i) => x - b[i]);
  const dot = (a, b) => a.reduce((sum, x, i) => sum + x * b[i], 0);
  const identity = () => [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];

  function multiply(a, b) {
    const out = new Array(16).fill(0);
    for (let column = 0; column < 4; column += 1) {
      for (let row = 0; row < 4; row += 1) {
        for (let k = 0; k < 4; k += 1) out[column * 4 + row] += a[k * 4 + row] * b[column * 4 + k];
      }
    }
    return out;
  }

  function translation(v) {
    const m = identity();
    m[12] = number(v[0]); m[13] = number(v[1]); m[14] = number(v[2]);
    return m;
  }

  function scale(v) {
    return [v[0], 0, 0, 0, 0, v[1], 0, 0, 0, 0, v[2], 0, 0, 0, 0, 1];
  }

  function rotation(axis, angle) {
    const [x, y, z] = normalize(axis);
    const c = Math.cos(angle), s = Math.sin(angle), t = 1 - c;
    return [t*x*x+c, t*x*y+s*z, t*x*z-s*y, 0,
      t*x*y-s*z, t*y*y+c, t*y*z+s*x, 0,
      t*x*z+s*y, t*y*z-s*x, t*z*z+c, 0, 0, 0, 0, 1];
  }

  function euler(v) {
    return multiply(multiply(rotation([1, 0, 0], v[0]), rotation([0, 1, 0], v[1])), rotation([0, 0, 1], v[2]));
  }

  function alongY(direction) {
    const y = normalize(direction);
    const helper = Math.abs(y[2]) < 0.9 ? [0, 0, 1] : [1, 0, 0];
    const x = normalize(cross(y, helper));
    const z = cross(x, y);
    return [x[0], x[1], x[2], 0, y[0], y[1], y[2], 0, z[0], z[1], z[2], 0, 0, 0, 0, 1];
  }

  function perspective(fov, aspect, near, far) {
    const f = 1 / Math.tan(fov / 2), nf = 1 / (near - far);
    return [f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0];
  }

  function transformPoint(matrix, point) {
    return [0,1,2].map((row) => matrix[row]*point[0]+matrix[4+row]*point[1]+matrix[8+row]*point[2]+matrix[12+row]);
  }

  function lookAt(eye, target, up=[0,1,0]) {
    const z = normalize(subtract(eye, target));
    const x = normalize(cross(Math.abs(dot(z,up)) > 0.995 ? [0, 0, 1] : up, z));
    const y = cross(z, x);
    return [x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0, -dot(x, eye), -dot(y, eye), -dot(z, eye), 1];
  }

  function normalMatrix(m) {
    // Inverse transpose of the upper-left 3x3; all parts may scale independently.
    const a=m[0], b=m[4], c=m[8], d=m[1], e=m[5], f=m[9], g=m[2], h=m[6], i=m[10];
    const det = a*(e*i-f*h)-b*(d*i-f*g)+c*(d*h-e*g);
    if (Math.abs(det) < 1e-20) return [1,0,0,0,1,0,0,0,1];
    return [(e*i-f*h)/det, (c*h-b*i)/det, (b*f-c*e)/det,
      (f*g-d*i)/det, (a*i-c*g)/det, (c*d-a*f)/det,
      (d*h-e*g)/det, (b*g-a*h)/det, (a*e-b*d)/det];
  }

  function sphere(segments = 64, rings = 32) {
    const positions = [], normals = [];
    const vertex = (u, v) => [Math.sin(v) * Math.cos(u), Math.cos(v), Math.sin(v) * Math.sin(u)];
    for (let j = 0; j < rings; j += 1) {
      for (let i = 0; i < segments; i += 1) {
        const a=vertex(TAU*i/segments,Math.PI*j/rings), b=vertex(TAU*(i+1)/segments,Math.PI*j/rings);
        const c=vertex(TAU*(i+1)/segments,Math.PI*(j+1)/rings), d=vertex(TAU*i/segments,Math.PI*(j+1)/rings);
        for (const p of [a,b,c,a,c,d]) { positions.push(...p); normals.push(...p); }
      }
    }
    return {positions, normals};
  }

  function plumeCone(segments = 24) {
    const positions = [], normals = [];
    for (let i = 0; i < segments; i += 1) {
      const a=TAU*i/segments, b=TAU*(i+1)/segments;
      const pa=[Math.cos(a),0,Math.sin(a)], pb=[Math.cos(b),0,Math.sin(b)];
      for (const p of [pa, [0,-1,0], pb]) positions.push(...p);
      for (const p of [a,(a+b)/2,b]) normals.push(...normalize([Math.cos(p),-0.2,Math.sin(p)]));
    }
    return {positions, normals};
  }

  const MESH_VERTEX = `
    attribute vec3 aPosition;
    attribute vec3 aNormal;
    uniform mat4 uProjection, uView, uModel;
    uniform mat3 uNormal;
    varying vec3 vNormal, vWorld;
    void main() {
      vec4 world = uModel * vec4(aPosition, 1.0);
      vWorld = world.xyz;
      vNormal = normalize(uNormal * aNormal);
      gl_Position = uProjection * uView * world;
    }`;
  const MESH_FRAGMENT = `
    precision highp float;
    varying vec3 vNormal, vWorld;
    uniform vec3 uColor, uEye;
    uniform float uEmissive, uAlpha, uMetal, uEarth;
    float hash21(vec2 p) { return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453); }
    float noise21(vec2 p) {
      vec2 i=floor(p), f=fract(p), u=f*f*(3.0-2.0*f);
      return mix(mix(hash21(i),hash21(i+vec2(1.0,0.0)),u.x),mix(hash21(i+vec2(0.0,1.0)),hash21(i+vec2(1.0,1.0)),u.x),u.y);
    }
    float fbm(vec2 p) {
      float sum=0.0, weight=0.5;
      for(int i=0;i<4;i++) {sum+=weight*noise21(p);p=p*2.03+vec2(2.1,9.7);weight*=0.5;}
      return sum;
    }
    void main() {
      vec3 n = normalize(vNormal);
      if (!gl_FrontFacing) n = -n;
      vec3 sun = normalize(vec3(-0.60, 0.75, 0.28));
      float lambert = max(dot(n, sun), 0.0);
      vec3 eye = normalize(uEye-vWorld);
      vec3 halfway = normalize(sun+eye);
      float specular = pow(max(dot(n, halfway), 0.0), 44.0) * uMetal;
      vec3 base = uColor;
      if (uEarth > 1.5) {
        float rim = pow(1.0-clamp(dot(n,eye),0.0,1.0),4.0);
        gl_FragColor = vec4(0.12,0.42,0.92,rim*0.25);
        return;
      }
      if (uEarth > 0.5) {
        // Procedural visual texture only; no satellite map or terrain data.
        vec2 map=n.xz+vec2(n.y*0.13,n.y*0.19);
        float land=fbm(map*19.0);
        base=mix(vec3(0.022,0.10,0.23),vec3(0.12,0.21,0.16),smoothstep(0.49,0.55,land));
        float cloudField=fbm(map*150.0+vec2(sin(map.y*33.0),cos(map.x*29.0))*2.0);
        float fineCloud=fbm(map*4300.0+vec2(cloudField*3.0,cloudField));
        float broadCloud=fbm(map*900.0+vec2(cloudField*3.0,cloudField));
        float cloudDetail=mix(fineCloud,broadCloud,smoothstep(80000.0,400000.0,length(uEye-vWorld)));
        float cloud=smoothstep(0.47,0.67,cloudDetail)*smoothstep(0.28,0.64,cloudField);
        base=mix(base,vec3(0.80,0.86,0.90),cloud*0.92);
      }
      float ambient = mix(0.26,0.46,clamp(n.y*0.5+0.5,0.0,1.0));
      vec3 color = base*(ambient+0.72*lambert)+vec3(0.84,0.9,1.0)*specular;
      if (uEarth > 0.5) color=mix(color,vec3(0.19,0.39,0.62),pow(1.0-max(dot(n,eye),0.0),5.0)*0.42);
      color = mix(color,base*1.35,clamp(uEmissive,0.0,1.0));
      gl_FragColor = vec4(color,uAlpha);
    }`;
  const STAR_VERTEX = `
    attribute vec3 aPosition;
    uniform mat4 uProjection, uView;
    uniform vec3 uEye;
    uniform float uPixelRatio;
    void main() {
      gl_Position = uProjection*uView*vec4(aPosition*20000000.0+uEye,1.0);
      gl_PointSize = uPixelRatio * (1.0+0.4*abs(aPosition.y));
    }`;
  const STAR_FRAGMENT = `
    precision mediump float;
    uniform float uOpacity;
    void main() {
      float radial = length(gl_PointCoord-vec2(0.5));
      gl_FragColor = vec4(0.67,0.76,0.91,uOpacity*(1.0-smoothstep(0.1,0.5,radial)));
    }`;

  function createProgram(gl, vertexSource, fragmentSource) {
    const shaders = [];
    for (const [kind, source] of [[gl.VERTEX_SHADER,vertexSource],[gl.FRAGMENT_SHADER,fragmentSource]]) {
      const shader = gl.createShader(kind);
      gl.shaderSource(shader,source); gl.compileShader(shader);
      if (!gl.getShaderParameter(shader,gl.COMPILE_STATUS)) {
        const message = gl.getShaderInfoLog(shader);
        gl.deleteShader(shader); shaders.forEach((item) => gl.deleteShader(item));
        throw new Error("WebGL shader: " + message);
      }
      shaders.push(shader);
    }
    const program = gl.createProgram();
    shaders.forEach((shader) => gl.attachShader(program,shader)); gl.linkProgram(program);
    shaders.forEach((shader) => gl.deleteShader(shader));
    if (!gl.getProgramParameter(program,gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(program); gl.deleteProgram(program);
      throw new Error("WebGL program: " + message);
    }
    return program;
  }

  function fallback(canvas, reason) {
    const note = document.createElement("div");
    note.className = "cg-unavailable";
    note.setAttribute("role", "status");
    note.textContent = "この環境では WebGL 3DCG を表示できません。軌跡・数値の再生は引き続き利用できます。";
    note.style.cssText = "padding:22px;color:#c6d6e6;background:#091321;border:1px solid #25435e;border-radius:12px;font-size:13px;line-height:1.7";
    canvas.insertAdjacentElement("afterend",note);
    canvas.hidden = true;
    return {available:false,render(){},setCamera(){},reset(){},dispose(){note.remove();},getDiagnostics(){return {available:false,reason};}};
  }

  window.createMissionOSStarshipCG = function createMissionOSStarshipCG(canvas) {
    let gl;
    try { gl=canvas.getContext("webgl",{alpha:false,antialias:true,preserveDrawingBuffer:false}); } catch (error) { return fallback(canvas,String(error)); }
    if (!gl) return fallback(canvas,"WebGL context unavailable");
    if (!window.MissionOSStarshipModels) return fallback(canvas,"Model library unavailable");
    let meshProgram, starProgram;
    try { meshProgram=createProgram(gl,MESH_VERTEX,MESH_FRAGMENT); starProgram=createProgram(gl,STAR_VERTEX,STAR_FRAGMENT); }
    catch (error) { return fallback(canvas,String(error)); }

    const modelSet = window.MissionOSStarshipModels.build();
    const metadata = modelSet.metadata || {};
    const shipHeight = number(metadata.shipHeightM,52), boosterHeight=number(metadata.boosterHeightM,72);
    const buffers = new WeakMap(), assembledParts = new WeakMap(), articulatedParts = new WeakMap(), resources = [], listeners = [];
    const globe=sphere(96,48), cone=plumeCone(), glowSphere=sphere(20,10);
    const catchBox=window.MissionOSStarshipModels.geometry.box();
    const ground={positions:[-1,0,-1, 1,0,1, 1,0,-1, -1,0,-1, -1,0,1, 1,0,1],normals:[0,1,0,0,1,0,0,1,0,0,1,0,0,1,0,0,1,0]};
    let disposed=false, contextLost=false, lastFrame=null;
    let mode="orbit", yaw=0.88, pitch=0.23, zoom=1, drag=null;
    let drawing=0, plumeVisible=false, flameCount=0, frameCount=0, lastBody="", lastStacked=null;
    let integratedOrientationAccepted=false, actualEngineInputsAccepted=false, actualFlapInputsAccepted=false;
    let engineActuation=[], flapActuation=[], actuatorInputErrors=[];
    let catchGeometryAccepted=false,catchGeometryDiagnostics=null;
    let eye=[130,50,200], projection=identity(), view=identity(), cameraFov=36*Math.PI/180;
    const locations={};
    for (const name of ["uProjection","uView","uModel","uNormal","uColor","uEye","uEmissive","uAlpha","uMetal","uEarth"]) locations[name]=gl.getUniformLocation(meshProgram,name);
    const positionLocation=gl.getAttribLocation(meshProgram,"aPosition"), normalLocation=gl.getAttribLocation(meshProgram,"aNormal");
    const starLocations={};
    for (const name of ["uProjection","uView","uEye","uPixelRatio","uOpacity"]) starLocations[name]=gl.getUniformLocation(starProgram,name);
    const starPosition=gl.getAttribLocation(starProgram,"aPosition");
    const starBuffer=gl.createBuffer(); resources.push(starBuffer);
    const stars=[];
    for (let i=0;i<520;i+=1) {
      const z=1-2*(i+0.5)/520, longitude=i*2.399963229728653, radial=Math.sqrt(1-z*z);
      stars.push(radial*Math.cos(longitude),z,radial*Math.sin(longitude));
    }
    gl.bindBuffer(gl.ARRAY_BUFFER,starBuffer); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(stars),gl.STATIC_DRAW);

    function geometryBuffers(geometry) {
      if (buffers.has(geometry)) return buffers.get(geometry);
      const result={count:geometry.positions.length/3,position:gl.createBuffer(),normal:gl.createBuffer()};
      for (const [buffer, values] of [[result.position,geometry.positions],[result.normal,geometry.normals]]) {
        gl.bindBuffer(gl.ARRAY_BUFFER,buffer); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array(values),gl.STATIC_DRAW); resources.push(buffer);
      }
      buffers.set(geometry,result); return result;
    }

    function draw(geometry, matrix, color, options={}) {
      const data=geometryBuffers(geometry);
      gl.bindBuffer(gl.ARRAY_BUFFER,data.position); gl.enableVertexAttribArray(positionLocation); gl.vertexAttribPointer(positionLocation,3,gl.FLOAT,false,0,0);
      gl.bindBuffer(gl.ARRAY_BUFFER,data.normal); gl.enableVertexAttribArray(normalLocation); gl.vertexAttribPointer(normalLocation,3,gl.FLOAT,false,0,0);
      gl.uniformMatrix4fv(locations.uModel,false,matrix); gl.uniformMatrix3fv(locations.uNormal,false,normalMatrix(matrix));
      gl.uniform3fv(locations.uColor,color); gl.uniform1f(locations.uEmissive,number(options.emissive));
      gl.uniform1f(locations.uAlpha,number(options.alpha,1)); gl.uniform1f(locations.uMetal,number(options.metal,0.18));
      gl.uniform1f(locations.uEarth,number(options.earth));
      gl.drawArrays(gl.TRIANGLES,0,data.count); drawing+=1;
    }

    function assemble(partsList, articulated=false) {
      const cache=articulated?articulatedParts:assembledParts;
      if (cache.has(partsList)) return cache.get(partsList);
      const groups=new Map();
      for (const part of partsList || []) {
        if (articulated&&part.actuator) continue;
        if (!part.geometry || !part.geometry.positions || !part.geometry.normals) continue;
        const position=part.position||[0,0,0], angles=part.rotation||[0,0,0], dimensions=part.scale||[1,1,1];
        const matrix=multiply(translation(position),multiply(euler(angles),scale(dimensions)));
        const normals=normalMatrix(matrix), color=part.color||[0.5,0.6,0.7];
        const options={emissive:number(part.emissive),metal:part.tag&&/tile|heat|panel|window|dark/i.test(part.tag)?0.04:0.42};
        const key=JSON.stringify([color,options]);
        if (!groups.has(key)) groups.set(key,{geometry:{positions:[],normals:[]},color,options});
        const group=groups.get(key), source=part.geometry;
        for(let i=0;i<source.positions.length;i+=3) {
          const x=source.positions[i],y=source.positions[i+1],z=source.positions[i+2];
          group.geometry.positions.push(matrix[0]*x+matrix[4]*y+matrix[8]*z+matrix[12],matrix[1]*x+matrix[5]*y+matrix[9]*z+matrix[13],matrix[2]*x+matrix[6]*y+matrix[10]*z+matrix[14]);
          const nx=source.normals[i],ny=source.normals[i+1],nz=source.normals[i+2];
          group.geometry.normals.push(...normalize([normals[0]*nx+normals[3]*ny+normals[6]*nz,normals[1]*nx+normals[4]*ny+normals[7]*nz,normals[2]*nx+normals[5]*ny+normals[8]*nz]));
        }
      }
      const result=Array.from(groups.values()); cache.set(partsList,result); return result;
    }

    function parts(partsList, parent, actuation=null, prefix="") {
      if(!partsList)return;
      for(const group of assemble(partsList,Boolean(actuation))) draw(group.geometry,parent,group.color,group.options);
      if(!actuation)return;
      const recordedFlaps=new Set();
      for(const part of partsList) {
        const binding=part.actuator;
        if(!binding)continue;
        let local=multiply(translation(part.position||[0,0,0]),multiply(euler(part.rotation||[0,0,0]),scale(part.scale||[1,1,1])));
        if(binding.kind==="engine"&&!prefix&&actuation.engines) {
          const engine=actuation.engines[binding.index];
          if(engine)local=multiply(translation(engine.anchorCG),alongY(engine.thrustDirectionCG));
        } else if(binding.kind==="flap"&&actuation.flaps.has(prefix+binding.name)) {
          const angle=actuation.flaps.get(prefix+binding.name);
          const hinge=multiply(translation(binding.pivot),multiply(rotation(binding.axis,angle),translation(binding.pivot.map(x=>-x))));
          local=multiply(hinge,local);
          if(!recordedFlaps.has(binding.name)) {
            flapActuation.push({name:prefix+binding.name,angle_rad:angle,pivotCG:binding.pivot,axisCG:binding.axis,hingeMatrixCG:hinge});
            recordedFlaps.add(binding.name);
          }
        }
        draw(part.geometry,multiply(parent,local),part.color,{emissive:number(part.emissive),metal:part.tag&&/tile|heat|panel|window|dark/i.test(part.tag)?0.04:0.42});
      }
    }

    function validOrientation(matrix) {
      if(!Array.isArray(matrix)||matrix.length!==16||!matrix.every(Number.isFinite))return false;
      const axes=[matrix.slice(0,3),matrix.slice(4,7),matrix.slice(8,11)];
      return [3,7,11,12,13,14].every(i=>Math.abs(matrix[i])<1e-6)&&Math.abs(matrix[15]-1)<1e-6&&
        axes.every(a=>Math.abs(dot(a,a)-1)<1e-4)&&Math.abs(dot(axes[0],axes[1]))<1e-4&&
        Math.abs(dot(axes[0],axes[2]))<1e-4&&Math.abs(dot(axes[1],axes[2]))<1e-4&&dot(cross(axes[0],axes[1]),axes[2])>0.9999;
    }

    function readActuation(frame,kind) {
      const requested=frame.attitude_source==="integrated_quaternion"||frame.engine_states!==undefined||frame.flap_angles_rad!==undefined;
      if(!requested||kind==="satellite")return null;
      const result={engines:null,flaps:new Map()};
      const count=kind==="booster"?33:6, states=frame.engine_states, anchors=frame.main_engine_anchors, thrusts=frame.main_engine_thrust_n;
      const vector=v=>Array.isArray(v)&&v.length===3&&v.every(Number.isFinite);
      const validStates=Array.isArray(states)&&states.length>=count&&states.slice(0,count).every(e=>e&&typeof e.available==="boolean"&&
        Number.isFinite(e.throttle)&&e.throttle>=0&&e.throttle<=1&&Number.isFinite(e.gimbal_x_rad)&&Number.isFinite(e.gimbal_y_rad));
      if(validStates&&Array.isArray(anchors)&&anchors.length===count&&anchors.every(vector)&&
         Array.isArray(thrusts)&&thrusts.length===count&&thrusts.every(x=>Number.isFinite(x)&&x>=0)) {
        result.engines=states.slice(0,count).map((e,i)=>{
          const gx=e.gimbal_x_rad,gy=e.gimbal_y_rad;
          // Ry(gy) Rx(gx) body +Z, then body (x,y,z) -> CG (x,z,-y).
          const thrustDirectionCG=[Math.sin(gy)*Math.cos(gx),Math.cos(gy)*Math.cos(gx),Math.sin(gx)];
          return {index:i,available:e.available,throttle:e.throttle,gimbal_x_rad:gx,gimbal_y_rad:gy,
            thrust_n:thrusts[i],anchorCG:[anchors[i][0],anchors[i][2],-anchors[i][1]],thrustDirectionCG,
            recordedNonzeroThrust:thrusts[i]>0,
            lit:!frame.afterContact&&e.available&&e.throttle>=FLAME_VISIBILITY_MIN_THROTTLE&&thrusts[i]>0};
        });
        engineActuation=result.engines;actualEngineInputsAccepted=true;
      } else actuatorInputErrors.push("main_engine_state_anchor_or_thrust_mismatch");
      const angles=frame.flap_angles_rad, names=frame.aero_panel_names;
      if(Array.isArray(angles)&&Array.isArray(names)&&angles.length===names.length&&angles.every(Number.isFinite)&&
         names.every(n=>typeof n==="string")&&new Set(names).size===names.length) {
        names.forEach((name,i)=>result.flaps.set(name,angles[i]));
        const required=kind==="booster"?["grid_fin_0","grid_fin_1","grid_fin_2"]:["flap_aft_-1","flap_aft_1","flap_forward_-1","flap_forward_1"];
        actualFlapInputsAccepted=required.every(name=>result.flaps.has(name));
        if(!actualFlapInputsAccepted)actuatorInputErrors.push("required_flap_names_missing");
      } else actuatorInputErrors.push("flap_angles_or_panel_names_mismatch");
      return result;
    }

    function bodyKind(id) { return id==="booster"?"booster":id==="ship"?"ship":"satellite"; }
    function bodyHeight(kind) { return kind==="booster"?boosterHeight:kind==="ship"?shipHeight:3; }
    function hiddenByPlanet(position, center) {
      const ray=subtract(position,eye), origin=subtract(eye,center), a=dot(ray,ray);
      if(a<1)return false;
      const b=2*dot(origin,ray), c=dot(origin,origin)-EARTH_RADIUS*EARTH_RADIUS;
      const discriminant=b*b-4*a*c;
      if(discriminant<0)return false;
      const entry=(-b-Math.sqrt(discriminant))/(2*a);
      return entry>0&&entry<1;
    }
    function attitude(frame) {
      // A 6DOF replay supplies all three axes from its integrated quaternion.
      // The legacy display-axis path remains only for older point-mass reports.
      const matrix=frame.attitude_matrix_local;
      if(validOrientation(matrix))return matrix;
      const direction=frame.attitude_direction_local||frame.body_axis_local||[0,1,0];
      const valid=Array.isArray(direction)&&direction.length===3&&direction.every(Number.isFinite)&&length(direction)>0.01;
      return multiply(alongY(valid?direction:[0,1,0]),rotation([0,1,0],number(frame.bank_deg)*Math.PI/180));
    }

    function engineAnchors(kind,count) {
      const anchors=metadata.engineAnchors&&metadata.engineAnchors[kind];
      if (Array.isArray(anchors)&&anchors.length) return anchors.slice(0,count).map((anchor)=>anchor.position||anchor);
      const ring=count<=3?1.4:kind==="booster"?3:2.7;
      return Array.from({length:count},(_,i)=>[Math.sin(TAU*i/count)*ring,0,Math.cos(TAU*i/count)*ring]);
    }

    function flames(kind, baseMatrix, frame, actuation) {
      let active;
      if(actuation) {
        // A missing/invalid recorded engine vector must not invent ignitions
        // from aggregate engine_count. The display-only visibility cutoff
        // leaves the recorded per-engine throttle and thrust untouched.
        active=(actuation.engines||[]).filter(e=>e.lit);
      } else {
        const count=clamp(Math.floor(number(frame.engine_count)),0,kind==="booster"?33:6);
        const throttle=clamp(number(frame.throttle),0,1);
        if(frame.afterContact||count===0||number(frame.applied_thrust_n)<=0||throttle<FLAME_VISIBILITY_MIN_THROTTLE)return;
        active=engineAnchors(kind,count).map((anchor,i)=>({index:i,anchorCG:anchor,thrustDirectionCG:[0,1,0],throttle}));
      }
      if(!active.length)return;
      const vacuum=clamp((number(frame.altitude_m)-25000)/75000,0,1);
      gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA,gl.ONE); gl.depthMask(false);
      for(const engine of active) {
        const root=multiply(baseMatrix,multiply(translation(engine.anchorCG),alongY(engine.thrustDirectionCG)));
        const plumeLength=(kind==="booster"?41:30)*Math.sqrt(engine.throttle);
        // Deterministic at the shared replay time; no independent animation clock.
        const shimmer=0.96+0.04*Math.sin(number(frame.time_s)*27+engine.index*1.7);
        const width=(kind==="booster"?0.9:1.15)*(1+vacuum*0.55);
        draw(cone,multiply(root,scale([width,plumeLength*shimmer,width])),[0.23,0.42,1.0],{emissive:1,alpha:0.24,metal:0});
        draw(cone,multiply(root,scale([width*0.64,plumeLength*0.67*shimmer,width*0.64])),[0.7,0.68,1.0],{emissive:1,alpha:0.42,metal:0});
        draw(cone,multiply(root,scale([width*0.35,plumeLength*0.30,width*0.35])),[1.0,0.87,0.65],{emissive:1,alpha:0.8,metal:0});
        flameCount+=1;
      }
      gl.depthMask(true); gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA); gl.disable(gl.BLEND);
      plumeVisible=active.length>0;
    }

    function drawStars(altitude,pixelRatio) {
      if (altitude<35000) return;
      gl.useProgram(starProgram); gl.disable(gl.DEPTH_TEST); gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);
      gl.bindBuffer(gl.ARRAY_BUFFER,starBuffer); gl.enableVertexAttribArray(starPosition); gl.vertexAttribPointer(starPosition,3,gl.FLOAT,false,0,0);
      gl.uniformMatrix4fv(starLocations.uProjection,false,projection); gl.uniformMatrix4fv(starLocations.uView,false,view);
      gl.uniform3fv(starLocations.uEye,eye); gl.uniform1f(starLocations.uPixelRatio,pixelRatio); gl.uniform1f(starLocations.uOpacity,clamp((altitude-35000)/65000,0,0.85));
      gl.drawArrays(gl.POINTS,0,stars.length/3); gl.disable(gl.BLEND); gl.enable(gl.DEPTH_TEST);
    }

    function camera(height, frame, aspect) {
      const satellite=bodyKind(frame.bodyId)==="satellite";
      cameraFov=36*Math.PI/180;
      if (mode==="onboard"&&!satellite) {
        // Illustrative hull-side camera, using the supplied attitude. Camera
        // mount and optics are not flight telemetry. Keep it fixed to the hull
        // as the recorded centre of mass moves during propellant consumption.
        const body=attitude(frame), comHeight=number(frame.com_height_m,height/2);
        const mount=height-comHeight-Math.min(height*0.17,8.5);
        const targetY=frame.stacked?mount-36:8-comHeight;
        const localEye=[6.8,mount,3.6];
        const localTarget=[9+Math.sin(yaw-0.88)*22,targetY,5+(pitch-0.23)*24];
        eye=transformPoint(body,localEye);
        const target=transformPoint(body,localTarget);
        // A modest fixed roll makes a broadcast-like horizon without changing
        // the recorded position, orientation, or relative body geometry.
        const forward=normalize(subtract(target,eye));
        const right=normalize(cross(forward,Math.abs(forward[1])>0.995?[0,0,1]:[0,1,0]));
        const upright=normalize(cross(right,forward));
        const roll=0.50;
        const up=upright.map((value,i)=>value*Math.cos(roll)+right[i]*Math.sin(roll));
        cameraFov=clamp(70*Math.PI/180*zoom,35*Math.PI/180,105*Math.PI/180);
        projection=perspective(cameraFov,aspect,0.12,80000000);
        view=lookAt(eye,target,up);
        return;
      }
      const fit=height/(2*Math.tan(36*Math.PI/360))*1.3/Math.min(1,aspect);
      let distance=Math.max(satellite?24:100,fit)*zoom;
      let localYaw=yaw, localPitch=pitch;
      if (mode==="ground") { localYaw=-0.66+yaw-0.88; localPitch=clamp(-0.10+pitch-0.23,-0.6,1.1); distance*=1.14; }
      if (mode==="chase") { localYaw=2.45+yaw-0.88; localPitch=clamp(0.48+pitch-0.23,-1.3,1.3); }
      if (mode==="payload") { localYaw=0.8+yaw-0.88; localPitch=clamp(0.32+pitch-0.23,-1.3,1.3); distance*=satellite?1:0.9; }
      const target=[0,mode==="payload"&&!satellite?height*0.21:0,0];
      eye=[Math.sin(localYaw)*Math.cos(localPitch)*distance,Math.sin(localPitch)*distance+target[1],Math.cos(localYaw)*Math.cos(localPitch)*distance];
      // Wide far range includes the actual Earth-scale visual sphere and satellites.
      projection=perspective(cameraFov,aspect,Math.max(0.15,distance/1200),80000000);
      view=lookAt(eye,target);
    }

    function catchMechanism(record) {
      if(!record)return;
      const config=record.configuration||{},matrix=record.site_matrix_local;
      const keys=['support_height_m','arm_half_width_m','arm_half_length_m'];
      if(!Array.isArray(matrix)||matrix.length!==16||!matrix.every(Number.isFinite)||
         !Number.isFinite(record.arm_half_span_m)||!keys.every(k=>Number.isFinite(config[k])&&config[k]>0)||
         !Array.isArray(record.pins)||!record.pins.every(p=>Array.isArray(p.position_local_m)&&p.position_local_m.length===3&&p.position_local_m.every(Number.isFinite)&&Number.isFinite(p.normal_force_n)))return;
      const supportY=config.support_height_m,depth=config.arm_half_length_m;
      // Tower silhouette and links are visual context. Only the highlighted
      // pad footprints, arm span and point positions come from the catch model.
      parts(modelSet.catchTower,multiply(matrix,translation([0,0,-20])));
      draw(catchBox,multiply(matrix,multiply(translation([0,supportY-1.5,-17]),scale([record.arm_half_span_m*2+3,2,2]))),[.32,.38,.43],{metal:.5});
      for(let index=0;index<2;index+=1){
        if(record.missing_support&&index===1)continue;
        const sign=index===0?-1:1,x=sign*record.arm_half_span_m;
        const loaded=record.pins[index]?.normal_force_n>0;
        const color=loaded?[.46,.75,.57]:record.authorized?[.68,.59,.38]:[.59,.28,.24];
        draw(catchBox,multiply(matrix,multiply(translation([x,supportY-.6,(-17+depth)/2]),scale([config.arm_half_width_m*2,1.2,17+depth]))),[.43,.48,.52],{metal:.65});
        draw(catchBox,multiply(matrix,multiply(translation([x,supportY-.02,0]),scale([config.arm_half_width_m*2,.04,depth*2]))),color,{metal:.25});
      }
      for(const pin of record.pins){
        const color=pin.normal_force_n>0?[.55,.93,.68]:[.97,.66,.32];
        draw(glowSphere,multiply(translation(pin.position_local_m),scale([.45,.45,.45])),color,{emissive:.35,metal:.2});
      }
      catchGeometryAccepted=true;
      catchGeometryDiagnostics={source:'recorded_support_frames',frame_time_s:record.frame_time_s,
        actuator_sample_time_s:record.actuator_sample_time_s,arm_half_span_m:record.arm_half_span_m,
        arm_rate_mps:record.arm_rate_mps,authorized:record.authorized,missing_support:record.missing_support,
        support_points_local_m:record.pins.map(p=>p.position_local_m),support_force_n:record.pins.map(p=>p.normal_force_n),
        settle_elapsed_s:record.settle_elapsed_s,physicalHardwareGeometryValidated:false,visualCaptureStateOverride:false};
    }

    function render(frame) {
      if (disposed||contextLost) return;
      integratedOrientationAccepted=false;actualEngineInputsAccepted=false;actualFlapInputsAccepted=false;
      engineActuation=[];flapActuation=[];actuatorInputErrors=[];
      catchGeometryAccepted=false;catchGeometryDiagnostics=null;
      if (!frame) {
        lastFrame=null;lastBody="";lastStacked=null;drawing=0;plumeVisible=false;flameCount=0;
        gl.clearColor(0.003,0.006,0.016,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);return;
      }
      lastFrame=frame;
      const rect=canvas.getBoundingClientRect(), pixelRatio=Math.min(window.devicePixelRatio||1,2);
      const width=Math.max(1,Math.round((rect.width||canvas.clientWidth||900)*pixelRatio));
      const height=Math.max(1,Math.round((rect.height||canvas.clientHeight||560)*pixelRatio));
      if (canvas.width!==width||canvas.height!==height) {canvas.width=width;canvas.height=height;}
      gl.viewport(0,0,width,height);
      const altitude=Math.max(0,number(frame.altitude_m)), kind=bodyKind(frame.bodyId||"ship");
      const stacked=Boolean(frame.stacked)&&kind!=="satellite";
      const craftHeight=stacked?shipHeight+boosterHeight:bodyHeight(kind);
      const comHeight=number(frame.com_height_m,craftHeight/2);
      const altitudeOffset=frame.attitude_source==="integrated_quaternion"?0:craftHeight/2;
      lastBody=frame.bodyId||"ship"; lastStacked=stacked;
      drawing=0; plumeVisible=false; flameCount=0; frameCount+=1;
      camera(frame.catch_geometry?Math.max(142,craftHeight):craftHeight,frame,width/height);
      const sky=clamp(altitude/85000,0,1);
      gl.clearColor(0.025*(1-sky)+0.003,0.075*(1-sky)+0.006,0.145*(1-sky)+0.016,1);
      gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT); gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL); gl.disable(gl.CULL_FACE);
      drawStars(altitude,pixelRatio);
      gl.useProgram(meshProgram);
      gl.uniformMatrix4fv(locations.uProjection,false,projection); gl.uniformMatrix4fv(locations.uView,false,view); gl.uniform3fv(locations.uEye,eye);

      if (altitude>15000) {
        const planetCenter=[0,-EARTH_RADIUS-altitude-altitudeOffset,0];
        // Render planetary distances separately: a centimetre-scale near plane
        // otherwise collapses the Earth's front and back into the same depth bin.
        const planetProjection=perspective(cameraFov,width/height,1000,80000000);
        gl.uniformMatrix4fv(locations.uProjection,false,planetProjection);
        draw(globe,multiply(translation(planetCenter),scale([EARTH_RADIUS,EARTH_RADIUS,EARTH_RADIUS])),[0.025,0.11,0.24],{earth:1,metal:0});
        if(altitude>120000) {
          gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);gl.depthMask(false);
          const atmosphereRadius=EARTH_RADIUS+24000;
          draw(globe,multiply(translation(planetCenter),scale([atmosphereRadius,atmosphereRadius,atmosphereRadius])),[0.12,0.42,0.92],{earth:2,metal:0});
          gl.depthMask(true);gl.disable(gl.BLEND);
        }
        gl.clear(gl.DEPTH_BUFFER_BIT);
        gl.uniformMatrix4fv(locations.uProjection,false,projection);
      } else {
        // A schematic local sea/terrain surface; launch pad placement uses recorded coordinates.
        const floor=-altitude-altitudeOffset-(stacked&&frame.attitude_source!=="integrated_quaternion"?number(metadata.launchMountHeightM,12.1):0);
        draw(ground,multiply(translation([0,floor,0]),scale([180000,1,180000])),[0.025,0.09,0.12],{metal:0.13});
      }

      const launch=frame.launch_relative_m;
      if (!frame.catch_geometry&&Array.isArray(launch)&&launch.length===3&&launch.every(Number.isFinite)&&length(launch)<22000) {
        const base=[launch[0],launch[1]-craftHeight/2-number(metadata.launchMountHeightM,12.1),launch[2]];
        parts(modelSet.pad,translation(base));
        parts(modelSet.tower,translation([base[0]+30,base[1],base[2]-13]));
      }

      catchMechanism(frame.catch_geometry);

      const craftAttitude=attitude(frame);
      integratedOrientationAccepted=frame.attitude_source==="integrated_quaternion"&&validOrientation(frame.attitude_matrix_local);
      const actuation=readActuation(frame,stacked?"booster":kind);
      let primaryBase;
      if (stacked) {
        const boosterBase=multiply(craftAttitude,translation([0,-comHeight,0]));
        const shipBase=multiply(craftAttitude,translation([0,boosterHeight-comHeight,0]));
        parts(modelSet.booster,boosterBase,actuation); parts(modelSet.ship,shipBase,actuation,"upper_"); primaryBase=boosterBase;
      } else {
        primaryBase=multiply(craftAttitude,translation([0,-comHeight,0]));
        parts(modelSet[kind],primaryBase,actuation);
      }

      for (const body of frame.separatedBodies||[]) {
        const relative=body.relative_m;
        if (!Array.isArray(relative)||relative.length!==3||!relative.every(Number.isFinite)||length(relative)>2000000||body.id===frame.bodyId) continue;
        if(altitude>15000&&hiddenByPlanet(relative,[0,-EARTH_RADIUS-altitude-craftHeight/2,0]))continue;
        const otherKind=bodyKind(body.id), otherHeight=bodyHeight(otherKind);
        // Positions retain their recorded metre scale. Unresolved distant bodies are not enlarged.
        const base=multiply(translation(relative),multiply(attitude(body),translation([0,-otherHeight/2,0])));
        parts(modelSet[otherKind],base);
      }

      flames(stacked?"booster":kind,primaryBase,frame,actuation);

      const heat=number(frame.heat_rate_w_m2);
      if (kind==="ship"&&!stacked&&!frame.afterContact&&altitude>20000&&heat>40000) {
        // Qualitative glow, driven only by the recorded proxy. It is not a TPS temperature.
        const heatStrength=clamp(Math.log(1+heat/40000)/9,0,0.28);
        gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA,gl.ONE); gl.depthMask(false);
        const heated=multiply(craftAttitude,multiply(translation([0,-3,-2]),scale([5,shipHeight*0.49,5])));
        draw(glowSphere,heated,[1,0.23,0.045],{emissive:1,alpha:heatStrength,metal:0});
        gl.depthMask(true); gl.disable(gl.BLEND);
      }
      gl.flush();
    }

    function redraw() { if(lastFrame) render(lastFrame); }
    function listen(target,name,callback,options) { target.addEventListener(name,callback,options); listeners.push([target,name,callback,options]); }
    canvas.style.touchAction="none";
    canvas.style.cursor="grab";
    if (!canvas.hasAttribute("tabindex")) canvas.tabIndex=0;
    if (!canvas.hasAttribute("aria-label")) canvas.setAttribute("aria-label","記録された飛行状態の 3DCG。ドラッグで回転、ホイールで拡大。矢印キーでも視点を回転できます。");
    listen(canvas,"pointerdown",(event)=>{
      if (event.button!==0) return;
      drag={id:event.pointerId,x:event.clientX,y:event.clientY};
      canvas.setPointerCapture(event.pointerId); canvas.style.cursor="grabbing"; canvas.focus({preventScroll:true});
    });
    listen(canvas,"pointermove",(event)=>{
      if (!drag||event.pointerId!==drag.id) return;
      yaw+=(event.clientX-drag.x)*0.007; pitch=clamp(pitch+(event.clientY-drag.y)*0.005,-1.35,1.35);
      drag.x=event.clientX;drag.y=event.clientY;redraw();
    });
    const release=(event)=>{if(drag&&event.pointerId===drag.id){drag=null;canvas.style.cursor="grab";}};
    listen(canvas,"pointerup",release); listen(canvas,"pointercancel",release); listen(canvas,"lostpointercapture",release);
    listen(canvas,"wheel",(event)=>{event.preventDefault();zoom=clamp(zoom*Math.exp(clamp(event.deltaY,-100,100)*0.0018),0.22,14);redraw();},{passive:false});
    listen(canvas,"keydown",(event)=>{
      let used=true;
      if(event.key==="ArrowLeft")yaw-=0.08;
      else if(event.key==="ArrowRight")yaw+=0.08;
      else if(event.key==="ArrowUp")pitch=clamp(pitch-0.06,-1.35,1.35);
      else if(event.key==="ArrowDown")pitch=clamp(pitch+0.06,-1.35,1.35);
      else if(event.key==="+"||event.key==="=")zoom=clamp(zoom/1.12,0.22,14);
      else if(event.key==="-")zoom=clamp(zoom*1.12,0.22,14);
      else used=false;
      if(used){event.preventDefault();redraw();}
    });
    listen(canvas,"webglcontextlost",(event)=>{event.preventDefault();contextLost=true;});
    // Recreating the page is preferable to accidentally retaining invalid GPU buffers.
    listen(canvas,"webglcontextrestored",()=>{contextLost=true;canvas.setAttribute("aria-label","WebGL の接続が再開しました。3DCG を再表示するにはページを再読み込みしてください。");});
    const resizeObserver=typeof ResizeObserver!=="undefined"?new ResizeObserver(redraw):null;
    if(resizeObserver)resizeObserver.observe(canvas);
    else listen(window,"resize",redraw);

    return {
      available:true,
      render,
      setCamera(nextMode){if(["orbit","ground","chase","payload","onboard"].includes(nextMode)){mode=nextMode;yaw=0.88;pitch=0.23;zoom=1;redraw();}},
      reset(){yaw=0.88;pitch=0.23;zoom=1;redraw();},
      dispose(){
        if(disposed)return;disposed=true;
        if(resizeObserver)resizeObserver.disconnect();
        listeners.forEach(([target,name,callback,options])=>target.removeEventListener(name,callback,options));
        resources.forEach((buffer)=>gl.deleteBuffer(buffer));gl.deleteProgram(meshProgram);gl.deleteProgram(starProgram);
      },
      getDiagnostics(){return {available:!disposed&&!contextLost,frameCount,drawCalls:drawing,plumeVisible,flameCount,bodyId:lastBody,stacked:lastStacked,camera:mode,time_s:lastFrame?lastFrame.time_s:null,coordinateFrame:"east_up_minus_north",independentMissionClock:false,physicalMeshValidation:false,cameraPoseValidation:false,integratedOrientationAccepted,actualEngineInputsAccepted,actualGimbalInputsAccepted:actualEngineInputsAccepted,actualFlapInputsAccepted,activeMainEngineIndices:engineActuation.filter(e=>e.lit).map(e=>e.index),recordedNonzeroMainEngineIndices:engineActuation.filter(e=>e.recordedNonzeroThrust).map(e=>e.index),flameCountMeaning:"visible_illustrative_flames_not_nonzero_recorded_force",flameVisibilityMinimumThrottle:FLAME_VISIBILITY_MIN_THROTTLE,flameVisibilityIsPhysicalThreshold:false,engineActuation,flapActuation,actuatorInputErrors,catchGeometryAccepted,catchGeometry:catchGeometryDiagnostics,hingeGeometry:"illustrative_proxy",plumeShape:"illustrative_not_flow_solution"};}
    };
  };
})();
