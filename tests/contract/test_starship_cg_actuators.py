"""Production renderer accepts recorded 6DOF actuators without inferring engines.

A recording WebGL double checks submitted geometry transforms. Real shader and
browser verification remains a separate runtime smoke, not claimed by these tests.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required for renderer contract checks")

BOOTSTRAP = r"""
const assert=require('node:assert/strict'), fs=require('node:fs'), vm=require('node:vm');
global.window=globalThis;window.devicePixelRatio=1;window.addEventListener=()=>{};window.removeEventListener=()=>{};
const draws=[];let bound=null,currentModel=null,currentColor=null,currentEmissive=0,positionBuffer=null;
const gl=new Proxy({
 createShader:()=>({}),createProgram:()=>({}),createBuffer:()=>({}),
 getShaderParameter:()=>true,getProgramParameter:()=>true,
 getUniformLocation:(_,name)=>name,getAttribLocation:(_,name)=>name,
 bindBuffer:(_,buffer)=>{bound=buffer;},bufferData:(_,data)=>{bound.data=data;},
 vertexAttribPointer:name=>{if(name==='aPosition')positionBuffer=bound;},
 uniformMatrix4fv:(name,_,value)=>{if(name==='uModel')currentModel=[...value];},
 uniform3fv:(name,value)=>{if(name==='uColor')currentColor=[...value];},
 uniform1f:(name,value)=>{if(name==='uEmissive')currentEmissive=value;},
 drawArrays:()=>draws.push({matrix:currentModel,color:currentColor,emissive:currentEmissive,vertices:positionBuffer.data})
},{get:(target,key)=>key in target?target[key]:(/^[A-Z_]+$/.test(key)?key:()=>{})});
const canvas={style:{},getContext:()=>gl,getBoundingClientRect:()=>({width:900,height:600}),
 hasAttribute:()=>true,setAttribute:()=>{},addEventListener:()=>{},removeEventListener:()=>{}};
for(const path of process.argv.slice(1))vm.runInThisContext(fs.readFileSync(path,'utf8'));
const renderer=createMissionOSStarshipCG(canvas);
const id=[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1];
const shipNames=['hull_0','hull_1','hull_2','flap_aft_-1','flap_aft_1','flap_forward_-1','flap_forward_1'];
const gridNames=['hull_0','hull_1','hull_2','grid_fin_0','grid_fin_1','grid_fin_2'];
const frame=(kind='ship')=>{
 const count=kind==='booster'?33:6, names=kind==='booster'?gridNames:shipNames;
 return {bodyId:kind,altitude_m:100,time_s:10,com_height_m:26,attitude_source:'integrated_quaternion',attitude_matrix_local:id,
   engine_count:count,applied_thrust_n:count*1e6,throttle:1,propellant_kg:1e5,
   engine_states:Array.from({length:count+12},()=>({available:true,throttle:.5,gimbal_x_rad:0,gimbal_y_rad:0})),
   main_engine_anchors:Array.from({length:count},(_,i)=>[i+.3, i+.7, .2*i]),
   main_engine_thrust_n:Array.from({length:count},()=>1e6),flap_angles_rad:names.map(()=>0),aero_panel_names:[...names]};
};
const distance=(a,b)=>Math.hypot(...a.map((x,i)=>x-b[i]));
const point=(m,p)=>[0,1,2].map(i=>m[i]*p[0]+m[4+i]*p[1]+m[8+i]*p[2]+m[12+i]);
const direction=m=>m.slice(4,7).map(x=>x/Math.hypot(...m.slice(4,7)));
const nearly=(a,b,tol=1e-9)=>assert.ok(distance(a,b)<tol,`${a} != ${b}`);
const render=f=>{draws.length=0;renderer.render(f);return renderer.getDiagnostics();};
const plumeDraws=()=>draws.filter(d=>d.emissive===1&&distance(d.color,[.23,.42,1])<1e-9);
const bellDraws=()=>draws.filter(d=>distance(d.color,[.18,.23,.26])<1e-9);
"""


def js(source: str) -> None:
    result = subprocess.run(
        [NODE, "-e", BOOTSTRAP + "\n" + source,
         str(ROOT / "src/runtime/assets/starship_cg_models.js"),
         str(ROOT / "src/runtime/assets/starship_cg_renderer.js")],
        capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_engine_failure_preserves_identity_including_booster_last_engine():
    js(r"""
for(const kind of ['ship','booster']) {
 const f=frame(kind);f.engine_states[3].available=false;f.main_engine_thrust_n[3]=0;
 const d=render(f),count=kind==='ship'?6:33;
 assert.equal(d.actualEngineInputsAccepted,true);assert.equal(d.integratedOrientationAccepted,true);
 assert.equal(d.flameCount,count-1);assert.equal(d.activeMainEngineIndices.includes(3),false);
 assert.equal(d.activeMainEngineIndices.includes(count-1),true);
 assert.equal(plumeDraws().length,count-1);
 for(const i of d.activeMainEngineIndices) {
  const a=f.main_engine_anchors[i];
  assert.ok(plumeDraws().some(draw=>distance(draw.matrix.slice(12,15),[a[0],a[2]-26,-a[1]])<1e-9));
 }
}
""")


def test_residual_actuator_decay_is_recorded_without_visible_extra_flames():
    js(r"""
const f=frame('booster');
for(let i=0;i<33;i++) {f.engine_states[i].throttle=1e-10;f.main_engine_thrust_n[i]=1e-3;}
for(const i of [3,8]) {f.engine_states[i].throttle=.6;f.main_engine_thrust_n[i]=1.5e6;}
const before=JSON.stringify(f),d=render(f);
assert.equal(d.actualEngineInputsAccepted,true);assert.equal(d.flameCount,2);
assert.equal(plumeDraws().length,2);assert.deepEqual(d.activeMainEngineIndices,[3,8]);
assert.equal(d.recordedNonzeroMainEngineIndices.length,33);
assert.equal(d.flameVisibilityMinimumThrottle,1e-6);
assert.equal(d.flameVisibilityIsPhysicalThreshold,false);
assert.equal(d.flameCountMeaning,'visible_illustrative_flames_not_nonzero_recorded_force');
for(let i=0;i<33;i++) {
 assert.equal(d.engineActuation[i].throttle,f.engine_states[i].throttle);
 assert.equal(d.engineActuation[i].thrust_n,f.main_engine_thrust_n[i]);
 assert.equal(d.engineActuation[i].recordedNonzeroThrust,true);
}
assert.equal(JSON.stringify(f),before);
""")


def test_flame_visibility_boundary_and_legacy_cutoff_preserve_recorded_force():
    js(r"""
const f=frame();f.main_engine_thrust_n.fill(1);
f.engine_states.slice(0,6).forEach(e=>e.throttle=0);
f.engine_states[0].throttle=1e-6;f.engine_states[1].throttle=1e-6*(1-Number.EPSILON);
let d=render(f);assert.equal(d.flameCount,1);assert.deepEqual(d.activeMainEngineIndices,[0]);
assert.equal(d.engineActuation[1].thrust_n,1);assert.equal(d.engineActuation[1].lit,false);
f.afterContact=true;d=render(f);assert.equal(d.flameCount,0);
assert.equal(d.recordedNonzeroMainEngineIndices.length,6);
const legacy={bodyId:'ship',altitude_m:100,time_s:1,engine_count:6,throttle:1e-10,applied_thrust_n:1e-3};
d=render(legacy);assert.equal(d.flameCount,0);assert.equal(legacy.applied_thrust_n,1e-3);
""")


def test_catch_arms_and_support_points_use_recorded_geometry_not_mission_clock():
    js(r"""
const f=frame('booster'),site=[...id];site[12]=12;site[13]=-90;site[14]=17;
f.catch_geometry={frame_time_s:600,actuator_sample_time_s:599.9,configuration:{support_height_m:100,arm_half_width_m:.8,arm_half_length_m:4},
  site_matrix_local:site,arm_half_span_m:8,arm_rate_mps:-1,authorized:true,missing_support:false,settle_elapsed_s:0,
  pins:[{position_local_m:[6,10,17],normal_force_n:0},{position_local_m:[18,10,17],normal_force_n:0}]};
let d=render(f);assert.equal(d.catchGeometryAccepted,true);assert.equal(d.catchGeometry.source,'recorded_support_frames');
assert.equal(d.catchGeometry.visualCaptureStateOverride,false);assert.equal(d.catchGeometry.arm_half_span_m,8);
const pads=()=>draws.filter(x=>distance(x.color,[.68,.59,.38])<1e-9).map(x=>x.matrix.slice(12,15));
nearly(pads()[0],[4,9.98,17]);nearly(pads()[1],[20,9.98,17]);
const pins=draws.filter(x=>distance(x.color,[.97,.66,.32])<1e-9);
nearly(pins[0].matrix.slice(12,15),[6,10,17]);nearly(pins[1].matrix.slice(12,15),[18,10,17]);
f.time_s=9000;render(f);nearly(pads()[0],[4,9.98,17]); // No wall/mission-clock arm animation.
f.catch_geometry.arm_half_span_m=6;render(f);nearly(pads()[0],[6,9.98,17]);nearly(pads()[1],[18,9.98,17]);
f.catch_geometry.missing_support=true;render(f);assert.equal(pads().length,1);
delete f.catch_geometry;d=render(f);assert.equal(d.catchGeometryAccepted,false);assert.equal(d.catchGeometry,null);
""")


def test_invalid_catch_geometry_is_not_rendered_or_accepted():
    js(r"""
const f=frame('booster');f.catch_geometry={arm_half_span_m:NaN,site_matrix_local:id,pins:[]};
const d=render(f);assert.equal(d.catchGeometryAccepted,false);assert.equal(d.catchGeometry,null);
""")


def test_gimbal_moves_complete_bell_and_plume_with_body_axis_conversion():
    js(r"""
const f=frame();f.engine_states[1].gimbal_x_rad=.13;f.engine_states[1].gimbal_y_rad=-.21;
const d=render(f),e=d.engineActuation[1],expected=[Math.sin(-.21)*Math.cos(.13),Math.cos(-.21)*Math.cos(.13),Math.sin(.13)];
nearly(e.thrustDirectionCG,expected);
const origin=[1.3,.2-26,-1.7];
const bell=bellDraws().find(draw=>distance(draw.matrix.slice(12,15),origin)<1e-9);
assert.ok(bell);nearly(direction(bell.matrix),expected);
const flame=plumeDraws().find(draw=>distance(draw.matrix.slice(12,15),origin)<1e-9);
assert.ok(flame);nearly(direction(flame.matrix),expected);
// Three bell components (outer, inner, rim) all rotate about the same anchor.
const components=draws.filter(draw=>draw.emissive===0&&distance(draw.matrix.slice(12,15),origin)<1e-9);
assert.equal(components.length,3);components.forEach(draw=>nearly(direction(draw.matrix),expected));
const exhaust=point(flame.matrix,[0,-1,0]).map((x,i)=>x-origin[i]);
assert.ok(exhaust.reduce((s,x,i)=>s+x*expected[i],0)<0);
""")


def test_physics_engine_order_is_three_sl_then_three_vacuum():
    js(r"""
const f=frame();render(f);
for(const index of [1,3]) {
 const a=f.main_engine_anchors[index],origin=[a[0],a[2]-26,-a[1]];
 const bell=bellDraws().find(draw=>distance(draw.matrix.slice(12,15),origin)<1e-9);
 let radius=0;for(let i=0;i<bell.vertices.length;i+=3)radius=Math.max(radius,Math.hypot(bell.vertices[i],bell.vertices[i+2]));
 assert.ok(Math.abs(radius-(index<3?.73:1.13))<1e-6);
}
""")


def test_recorded_thrust_gates_flames_even_when_spool_throttle_is_nonzero():
    js(r"""
const f=frame();f.main_engine_thrust_n.fill(0);f.propellant_kg=0;
let d=render(f);assert.equal(d.actualEngineInputsAccepted,true);assert.equal(d.flameCount,0);assert.equal(d.plumeVisible,false);
f.main_engine_thrust_n.fill(1e6);f.afterContact=true;d=render(f);assert.equal(d.flameCount,0);
""")


@pytest.mark.parametrize("invalid", [
    "delete f.main_engine_thrust_n",
    "f.engine_states[2].gimbal_x_rad=null",
    "f.main_engine_anchors.pop()",
    "f.main_engine_thrust_n[4]=-1",
    "f.engine_states[0].available='true'",
])
def test_invalid_recorded_engine_inputs_never_fall_back_to_aggregate_count(invalid):
    js(f"const f=frame();{invalid};const d=render(f);" + r"""
assert.equal(d.actualEngineInputsAccepted,false);assert.equal(d.flameCount,0);
assert.ok(d.actuatorInputErrors.includes('main_engine_state_anchor_or_thrust_mismatch'));
""")


def test_actual_flaps_rotate_both_faces_about_fixed_hinge():
    js(r"""
const f=frame();f.flap_angles_rad[3]=.3;f.flap_angles_rad[6]=-.2;
const d=render(f);assert.equal(d.actualFlapInputsAccepted,true);assert.equal(d.flapActuation.length,4);
for(const name of ['flap_aft_-1','flap_forward_1']) {
 const flap=d.flapActuation.find(x=>x.name===name),h=flap.hingeMatrixCG;
 nearly(point(h,flap.pivotCG),flap.pivotCG);
 const tip=[flap.pivotCG[0]+3,flap.pivotCG[1],flap.pivotCG[2]];
 assert.ok(distance(point(h,tip),tip)>.5);
 // Submitted outer and shield matrices contain the recorded hinge rotation.
 const faces=draws.filter(x=>x.emissive===0&&distance([x.matrix[0],x.matrix[2],x.matrix[8]], [h[0],h[2],h[8]])<1e-9);
 assert.equal(faces.length,2);
}
""")


def test_panel_names_bind_angles_even_when_record_order_changes():
    js(r"""
const f=frame();f.flap_angles_rad=[.17,.23,-.11,.04,0,0,0];
f.aero_panel_names=['flap_forward_1','flap_aft_-1','flap_forward_-1','flap_aft_1','hull_0','hull_1','hull_2'];
const d=render(f);assert.equal(d.actualFlapInputsAccepted,true);
assert.equal(d.flapActuation.find(x=>x.name==='flap_aft_-1').angle_rad,.23);
assert.equal(d.flapActuation.find(x=>x.name==='flap_forward_1').angle_rad,.17);
""")


def test_grid_fin_axis_and_stack_upper_flaps_use_their_own_names():
    js(r"""
const f=frame('booster');f.stacked=true;f.aero_panel_names.push(...shipNames.map(n=>'upper_'+n));
f.flap_angles_rad.push(...shipNames.map(()=>0));f.flap_angles_rad[4]=.2;f.flap_angles_rad[9]=-.3;
const d=render(f);assert.equal(d.actualFlapInputsAccepted,true);assert.equal(d.flapActuation.length,7);
const fin=d.flapActuation.find(x=>x.name==='grid_fin_1');nearly(fin.axisCG,[Math.cos(2*Math.PI/3),0,-Math.sin(2*Math.PI/3)]);
assert.equal(fin.angle_rad,.2);assert.equal(d.flapActuation.find(x=>x.name==='upper_flap_aft_-1').angle_rad,-.3);
assert.equal(d.flameCount,33);assert.equal(d.engineActuation.length,33);
""")


def test_legacy_render_and_cleared_frame_do_not_retain_actuator_flags():
    js(r"""
render(frame());
const legacy={bodyId:'ship',altitude_m:100,time_s:1,engine_count:2,throttle:.7,applied_thrust_n:2e6};
let d=render(legacy);assert.equal(d.flameCount,2);assert.equal(d.actualEngineInputsAccepted,false);
assert.equal(d.actualFlapInputsAccepted,false);assert.equal(d.integratedOrientationAccepted,false);assert.deepEqual(d.flapActuation,[]);
d=render(null);assert.equal(d.flameCount,0);assert.deepEqual(d.actuatorInputErrors,[]);
""")


def test_invalid_orientation_is_not_reported_as_integrated_rotation():
    js(r"""
for(const invalid of [Array(16).fill(0),[...id.slice(0,8),0,0,-1,0,0,0,0,1]]) {
 const f=frame();f.attitude_matrix_local=invalid;assert.equal(render(f).integratedOrientationAccepted,false);
}
""")


def test_integrated_orientation_composes_with_gimbal_and_anchor_in_world_space():
    js(r"""
const f=frame();f.attitude_matrix_local=[0,1,0,0,-1,0,0,0,0,0,1,0,0,0,0,1];
f.engine_states[0].gimbal_x_rad=.1;f.engine_states[0].gimbal_y_rad=.2;
const d=render(f);assert.equal(d.integratedOrientationAccepted,true);
const engine=d.engineActuation[0],cg=engine.thrustDirectionCG;
const bell=bellDraws().find(draw=>distance(draw.matrix.slice(12,15),[26,.3,-.7])<1e-9);
assert.ok(bell);nearly(direction(bell.matrix),[-cg[1],cg[0],cg[2]]);
""")


def test_missing_duplicate_or_non_numeric_flap_inputs_leave_geometry_neutral():
    js(r"""
for(const mutation of [f=>delete f.aero_panel_names,f=>f.aero_panel_names[6]='flap_aft_-1',f=>f.flap_angles_rad[3]=null]) {
 const f=frame();mutation(f);const d=render(f);
 assert.equal(d.actualFlapInputsAccepted,false);assert.deepEqual(d.flapActuation,[]);
 assert.equal(d.actualEngineInputsAccepted,true);
}
""")
