/* Read-only replay. All attitude comes from recorded scalar-first Hamilton q. */
(function(){"use strict";
const study=JSON.parse(document.getElementById('evidence').textContent), $=id=>document.getElementById(id);
const norm=v=>Math.hypot(...v), dot=(a,b)=>a.reduce((s,x,i)=>s+x*b[i],0), cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]], unit=v=>v.map(x=>x/(norm(v)||1));
const add=(a,b)=>a.map((x,i)=>x+b[i]), scale=(a,s)=>a.map(x=>x*s), sub=(a,b)=>add(a,scale(b,-1));
function rotate(q,v){const u=q.slice(1),uv=cross(u,v);return add(v,add(scale(uv,2*q[0]),scale(cross(u,uv),2)));}
function slerp(a,b,t){let d=dot(a,b);if(d<0){b=scale(b,-1);d=-d;}if(d>.9995)return unit(add(scale(a,1-t),scale(b,t)));const theta=Math.acos(Math.min(1,d));return add(scale(a,Math.sin((1-t)*theta)/Math.sin(theta)),scale(b,Math.sin(t*theta)/Math.sin(theta)));}
function sample(rows,t){let l=0,h=rows.length-1;while(l<h){const m=Math.ceil((l+h)/2);if(rows[m].time_s<=t)l=m;else h=m-1;}const a=rows[l],b=rows[Math.min(l+1,rows.length-1)],f=b.time_s>a.time_s?Math.max(0,Math.min(1,(t-a.time_s)/(b.time_s-a.time_s))):0;const out={...a,time_s:t};for(const k of ['v_eci_mps','omega_body_rad_s'])out[k]=a[k].map((x,i)=>x+(b[k][i]-x)*f);const dt=b.time_s-a.time_s;out.r_eci_m=a.r_eci_m.map((x,i)=>(2*f**3-3*f*f+1)*x+(f**3-2*f*f+f)*dt*a.v_eci_mps[i]+(-2*f**3+3*f*f)*b.r_eci_m[i]+(f**3-f*f)*dt*b.v_eci_mps[i]);out.q_body_to_eci=slerp(a.q_body_to_eci,b.q_body_to_eci,f);for(const k of ['altitude_m','ground_speed_mps','propellant_kg','applied_thrust_n','throttle','com_z_m'])out[k]=a[k]+(b[k]-a[k])*f;return out;}
function geodeticUp(r){const p=Math.hypot(r[0],r[1]),e2=6.6943799901413165e-3;let lat=Math.atan2(r[2],p*(1-e2));for(let i=0;i<8;i++){const ss=Math.sin(lat),n=6378137/Math.sqrt(1-e2*ss*ss);lat=Math.atan2(r[2]+e2*n*ss,p);}const lon=Math.atan2(r[1],r[0]);return [Math.cos(lat)*Math.cos(lon),Math.cos(lat)*Math.sin(lon),Math.sin(lat)];}
function recorded(rows,time){let l=0,h=rows.length-1;while(l<h){const m=Math.ceil((l+h)/2);if(rows[m].time_s<=time+1e-8)l=m;else h=m-1;}return rows[l];}
function catchView(record,frame,state,local){
  const lat=study.profile.launch.latitude_deg*Math.PI/180,lon=study.profile.launch.longitude_deg*Math.PI/180+7.292115e-5*frame.time_s;
  const n=6378137/Math.sqrt(1-6.6943799901413165e-3*Math.sin(lat)**2);
  const origin=[n*Math.cos(lat)*Math.cos(lon),n*Math.cos(lat)*Math.sin(lon),n*(1-6.6943799901413165e-3)*Math.sin(lat)];
  const up=[Math.cos(lat)*Math.cos(lon),Math.cos(lat)*Math.sin(lon),Math.sin(lat)],east=[-Math.sin(lon),Math.cos(lon),0],north=cross(up,east);
  const center=local(sub(origin,state.r_eci_m)),cols=[local(east),local(up),local(scale(north,-1))];
  const matrix=[...cols[0],0,...cols[1],0,...cols[2],0,...center,1];
  const point=p=>local(add(sub(origin,state.r_eci_m),add(scale(east,p[0]),add(scale(north,p[1]),scale(up,p[2])))));
  return {frame_time_s:frame.time_s,actuator_sample_time_s:recorded(run.samples,frame.time_s).time_s,
    configuration:record.configuration,site_matrix_local:matrix,arm_half_span_m:frame.arm_half_span_m,
    arm_rate_mps:frame.arm_rate_mps,authorized:record.eligibility.catch_authorized,missing_support:record.missing_support,
    settle_elapsed_s:frame.settle_elapsed_s,pins:frame.pins.map(p=>({...p,position_local_m:point(p.position_enu_m)}))};
}
const defaultRun=Number.isInteger(study.default_run_index)&&study.runs[study.default_run_index]?study.default_run_index:0;
let run=study.runs[defaultRun],playing=false,t=run.samples[0].time_s,previous=null;
const renderer=window.createMissionOSStarshipCG($('scene'));renderer.setCamera('chase');
const esc=x=>String(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function row(values){return '<tr>'+values.map(x=>'<td>'+esc(x)+'</td>').join('')+'</tr>';}
$('run').innerHTML=study.runs.map((r,i)=>'<option value="'+i+'">'+esc(r.scenario)+'</option>').join('');
$('run').value=String(defaultRun);
$('coverage').innerHTML=(study.coverage||[]).map(x=>row([x.component,x.implementation,x.status])).join('');
$('checks').innerHTML=(study.checks||[]).map(x=>row([x.name,x.passed===true?'PASS':x.passed===false?'FAIL':'NOT RUN',x.scope])).join('');
function choose(){run=study.runs[Number($('run').value)];t=run.samples[0].time_s;$('time').min=t;$('time').max=run.samples.at(-1).time_s;$('time').value=t;const o=run.outcome;$('cards').innerHTML=[['終了',o.termination],['最大高度',(o.max_altitude_m/1000).toFixed(1)+' km'],['最終対地速度',o.final_ground_speed_mps.toFixed(2)+' m/s']].map(([a,b])=>'<div class="card"><label>'+esc(a)+'</label><strong>'+esc(b)+'</strong><small>保存された計算結果</small></div>').join('');$('events').innerHTML=run.events.map(e=>row([e.time_s.toFixed(2),e.event,e.detail||''])).join('');$('details').textContent=JSON.stringify({configuration:study.profile,catch_configuration:run.catch_record?.configuration,outcome:run.outcome,provenance:study.provenance},null,2);$('scope').textContent=run.launch_continuation?'LAUNCH SEPARATION → RETURN → CONTACT · RECORDED STATES':run.catch_record?'TERMINAL TEST · INITIALIZED STATE · NOT LAUNCH TO CATCH':'INTEGRATED POSITION + QUATERNION';if(run.catch_record){$('rate').value='1';$('camera').value='orbit';renderer.setCamera('orbit');}paint();}
function paint(){let s=sample(run.samples,t);const frames=run.catch_record?.frames;const catchFrame=frames?.length&&t>=frames[0].time_s?recorded(frames,t):null;
if(catchFrame){for(const key of ['time_s','r_eci_m','v_eci_mps','q_body_to_eci','omega_body_rad_s','propellant_kg'])s[key]=catchFrame[key];s.com_z_m=catchFrame.com_body_m[2];s.applied_thrust_n=catchFrame.engine_thrust_n;s.ground_speed_mps=norm(add(s.v_eci_mps,[7.292115e-5*s.r_eci_m[1],-7.292115e-5*s.r_eci_m[0],0]));}
const up=geodeticUp(s.r_eci_m),east=unit(cross([0,0,1],up)),north=cross(up,east),local=v=>[dot(v,east),dot(v,up),-dot(v,north)];
const cols=[[1,0,0],[0,0,1],[0,-1,0]].map(v=>local(rotate(s.q_body_to_eci,v))),matrix=[...cols[0],0,...cols[1],0,...cols[2],0,0,0,0,1];
const catchGeometry=catchFrame?catchView(run.catch_record,catchFrame,s,local):null;
renderer.render({...s,bodyId:s.body_id||'ship',stacked:s.phase==='stack_ascent',attitude_matrix_local:matrix,attitude_source:'integrated_quaternion',com_height_m:s.com_z_m,afterContact:s.contact===true,separatedBodies:[],local_velocity_mps:local(s.v_eci_mps),heat_rate_w_m2:0,catch_geometry:catchGeometry});
$('phase').textContent=s.phase.toUpperCase().replaceAll('_',' ');$('speed').textContent=(s.ground_speed_mps*3.6).toFixed(0);$('altitude').textContent=(s.altitude_m/1000).toFixed(1);$('clock').textContent=catchFrame&&!run.launch_continuation?'TEST +'+(catchFrame.time_s-run.samples[0].time_s).toFixed(2)+'s':'T+'+Math.floor(t/3600).toString().padStart(2,'0')+':'+Math.floor(t/60%60).toString().padStart(2,'0')+':'+Math.floor(t%60).toString().padStart(2,'0');$('rates').textContent='ω XYZ '+s.omega_body_rad_s.map(x=>(x*180/Math.PI).toFixed(2)).join(' / ')+' °/s';$('fuel').textContent='FUEL '+(s.propellant_kg/1000).toFixed(1)+' t';$('time').value=t;
window.sixdofReplayDiagnostics={scenario:run.scenario,time_s:t,attitudeSource:'integrated_quaternion',quaternionNorm:norm(s.q_body_to_eci),catchFrameTime_s:catchFrame?.time_s??null,renderer:renderer.getDiagnostics()};$('scene').dataset.replayDiagnostics=JSON.stringify(window.sixdofReplayDiagnostics);
if(typeof window.dispatchEvent==='function'&&typeof CustomEvent==='function')window.dispatchEvent(new CustomEvent('missionos:saved-replay-paint',{detail:{run_index:Number($('run').value),time_s:t}}));}
$('run').onchange=choose;$('camera').onchange=()=>{renderer.setCamera($('camera').value);paint();};$('time').oninput=()=>{t=Number($('time').value);paint();};$('play').onclick=()=>{playing=!playing;$('play').textContent=playing?'Ⅱ 停止':'▶ 再生';};
// This API only selects/interpolates SAVED display data. It cannot advance a
// simulation, change a physical state, authorize a command or imply success.
window.sixdofSavedReplay={pause(){playing=false;$('play').textContent='▶ 再生';},seek(index,time){if(!Number.isInteger(index)||!study.runs[index]||!Number.isFinite(time))return false;if(Number($('run').value)!==index){$('run').value=String(index);choose();}t=Math.max(run.samples[0].time_s,Math.min(run.samples.at(-1).time_s,time));paint();return true;}};
function frame(now){if(playing&&previous!==null){t=Math.min(run.samples.at(-1).time_s,t+(now-previous)/1000*Number($('rate').value));if(t>=run.samples.at(-1).time_s){playing=false;$('play').textContent='▶ 再生';}paint();}previous=now;requestAnimationFrame(frame);}choose();requestAnimationFrame(frame);window.addEventListener('resize',paint);
})();
