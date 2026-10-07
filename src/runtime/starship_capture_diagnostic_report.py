"""Standalone replay of saved landing intervals and separate gate deficits."""
from __future__ import annotations

from html import escape
import json
import math

LABELS = {"original": "従来配分", "all_phase": "候補：coast＋着陸", "coast_only": "候補：coast限定"}


def report(bundle):
    table = []
    for condition in bundle["conditions"]:
        for method, item in condition["methods"].items():
            first = item["landing_request"]["arrival"]
            surplus = item["ideal_preview_fuel_surplus_kg"]
            stop = item["ideal_stop_location"]
            cells = [f'{condition["cutoff_time_s"]:.1f}', LABELS[method],
                     f'{math.hypot(*first["midpoint_enu_m"][:2])/1000:.3f}', f'{first["tilt_deg"]:.2f}',
                     f'{first["propellant_kg"]/1000:.2f}',
                     "可（理想化）" if item["preview"]["burn_fuel_feasible"] else "未停止",
                     f'{surplus/1000:+.2f}' if surplus is not None else "算出不可",
                     f'{stop["horizontal_error_m"]/1000:.3f}' if stop is not None else "算出不可",
                     f'{item["preview"]["estimated_burn_time_s"]:.2f} / {item["elapsed_to_terminal_s"]:.2f}',
                     f'{item["terminal_ground_speed_mps"]:.2f}',
                     f'{item["terminal"]["arrival"]["propellant_kg"]/1000:.2f}']
            table.append("<tr>"+"".join(f"<td>{escape(x)}</td>" for x in cells)+"</tr>")
    data = json.dumps(bundle, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    return '''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Starship · キャッチ未達の診断</title><style>
*{box-sizing:border-box}body{margin:0;background:#0b1118;color:#dbe5f0;font:15px/1.7 system-ui,sans-serif}main{max-width:1440px;margin:auto;padding:28px}h1{font-size:27px}h2{font-size:20px;margin-top:32px}p{max-width:1120px;color:#b9c8d8}a{color:#8ecafa}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:9px;text-align:left;border-bottom:1px solid #304050;white-space:nowrap}th{color:#acbfd1}.scroll{overflow:auto}.note{border-left:3px solid #e4b861;padding:8px 15px}label,button,select{font:inherit}button,select{background:#192b3b;color:inherit;border:1px solid #536b81;border-radius:5px;padding:6px 12px}button{cursor:pointer}.controls{display:flex;gap:15px;flex-wrap:wrap;align-items:center}input{flex:1;min-width:120px}.panels{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}article{background:#101d2a;border:1px solid #324a60;border-radius:8px;padding:14px;min-width:0}article h3{font-size:16px;margin:0}canvas{width:100%;height:260px}article p{font-size:12px}.gate{font-size:12px;white-space:normal}.bad{color:#f3a8ad}.good{color:#8cdfb0}#clock{min-width:75px;font-variant-numeric:tabular-nums}small{color:#aebfd2}@media(max-width:736px){main{padding:12px}.panels{grid-template-columns:1fr}h1{font-size:22px}}
</style><main><h1>キャッチ未達を、状態と時刻で切り分ける</h1>
<p class="note">既存の5停止条件 × 3配分の15記録を再検査しました。全記録が整合しても、キャッチ到達・支持は未成立です。
今回の処理は保存状態の診断です。追加の飛行積分、実モデル推論、本番方針の変更は行っていません。</p>
<p>停止予測は推力を速度の反対へ理想的に向ける近似です。実際には、姿勢・ジンバル・エンジンの有限応答を持つ6DOFで飛んでいます。
「予測で停止できる」と「支持点が許容窓に届く」は別の条件です。残量があっても推力が下を向く場合と、姿勢を保てても残量を失う場合を分けて読んでください。</p>
<h2>着陸要求時と終端</h2><div class="scroll"><table><thead><tr><th>停止 T+秒</th><th>配分</th><th>要求時水平誤差 km</th><th>要求時傾斜 °</th><th>要求時燃料 t</th><th>停止予測</th><th>停止だけの燃料余裕 t（位置修正なし）</th><th>概算停止点・CG水平誤差 km</th><th>予測燃焼 / 要求→終端 s</th><th>接地CG速度 m/s</th><th>接地燃料 t</th></tr></thead><tbody>''' + "".join(table) + '''</tbody></table></div>
<p>「停止だけの燃料余裕」は理想予測後の残量から、その質量での支持reserveを差し引いた値です。タワーまでの位置修正や、実際の着陸則の長い降下に使う燃料は含みません。
概算停止点はCGでのENU変位を要求時のタワーENUへ変換した値で、支持点の到達ではありません。飛行中の座標系回転・曲率も凍結した近似です。未停止予測の消費量を必要量と呼ばず算出不可とします。
要求→終端の時間は接地までの観測時間で、実際に停止した時間ではありません。実際の着陸則は高度に応じた下降速度と水平修正を要求し、最短時間で止める予測とは異なります。予測消費量を実則の必要燃料や、その厳密な下限とは扱いません。支持点速度はCG速度と区別します。</p>
<h2>着陸要求からの保存軌跡と許容窓</h2><div class="controls"><label for="condition">停止条件</label><select id="condition"></select><button id="play">再生</button><output id="clock"></output><input id="time" aria-label="着陸要求からの経過秒" type="range" min="0" step="any"></div>
<p>各runの着陸要求を経過0秒にそろえています。従来配分の開始状態は候補と同一ではありません。
候補2本だけが全く同じ着陸開始状態です。異なる開始状態をそろえた因果実験にはしません。</p>
<div class="panels">''' + "".join(f'<article><h3>{label}</h3><canvas id="{method}" aria-label="{label}の保存された東位置と高度"></canvas><p id="{method}-state"></p><div id="{method}-gates"></div></article>' for method, label in LABELS.items()) + '''</div>
<p>図は回転タワーENUでのCG位置（km）で、薄線は全保存軌跡です。北位置は数値で表示します。表示は補間せず選択時刻以前の最後の保存状態を使います。
終端を過ぎたrunはその終端で止めます。全条件の同時成立を別時刻の最良値の組合せで作りません。</p>
<p>各余裕は正なら範囲内、負なら不足です。高度帯と速度帯は両支持点の悪い側、姿勢は鉛直傾きとbody Xの東方向誤差を別々に検査します。
約0.5秒間隔の保存点と正確な段階変更eventを読みます。細い高さ窓の連続通過時刻、到達不能性、空力・熱・構造の妥当性を証明しません。
主推力の上向き成分には空力・RCS・重力を含めず、正味の減速と呼びません。</p>
<h2>次に検証する仮説</h2><p>理想的な停止予測だけでは、支持燃料と有限時間の姿勢追従を満たす着陸開始状態を選べていない可能性があります。
次の候補は、上流の位置修正と、実姿勢・角速度・有限推力応答・支持reserveを含む開始予測を合わせて検証するものです。
原因の単独同定や成功の保証はなく、今回の記録だけで新方針を採用しません。</p>
<p>公開手法の参考：<a href="https://ntrs.nasa.gov/citations/20230017074">NASA：質量特性が変化する6DOF powered descent</a>。
月面の仮想着陸機を対象にした研究で、Super Heavyの非公開制御則や係数ではありません。</p>
<p><a href="diagnostics.json">全診断・15入力hash・検査結果</a></p></main>
<script>const data=''' + data + ''';
const methods=['original','all_phase','coast_only'],colors=['#78bef5','#e9b879','#8bdaa3'],units={horizontal_position:'m',pin_height:'m',pin_vertical_speed:'m/s',pin_horizontal_speed:'m/s',tilt:'°',clocking:'°',body_rate:'rad/s',fuel_reserve:'kg'},names={horizontal_position:'水平位置',pin_height:'両pin高さ帯',pin_vertical_speed:'両pin下降速度',pin_horizontal_speed:'両pin水平速度',tilt:'鉛直傾き',clocking:'body Xの東方向誤差',body_rate:'角速度',fuel_reserve:'支持燃料'};
const choice=document.getElementById('condition'),slider=document.getElementById('time'),play=document.getElementById('play'),clock=document.getElementById('clock');let timer=null,bounds;
for(const [i,c] of data.conditions.entries()){const o=document.createElement('option');o.value=i;o.textContent=`T+${c.cutoff_time_s.toFixed(1)}秒`;choice.append(o)}
function stop(){if(timer)clearInterval(timer);timer=null;play.textContent='再生'}
function setup(){stop();const all=Object.values(data.conditions[+choice.value].methods).flatMap(m=>m.rows);slider.max=Math.max(...all.map(r=>r.elapsed_s));slider.value=0;bounds={lo:Math.min(0,...all.map(r=>r.cg_position_enu_m[0]/1000))-1,hi:Math.max(0,...all.map(r=>r.cg_position_enu_m[0]/1000))+1,top:Math.max(1,...all.map(r=>r.cg_position_enu_m[2]/1000))+0.2};draw()}
function draw(){const c=data.conditions[+choice.value],t=+slider.value;clock.textContent=`+${t.toFixed(2)}s`;methods.forEach((m,k)=>{const item=c.methods[m],rows=item.rows,done=rows.filter(r=>r.elapsed_s<=t+1e-9),r=done[done.length-1]||rows[0],a=r.arrival,canvas=document.getElementById(m),ctx=canvas.getContext('2d'),w=canvas.clientWidth,h=260;canvas.width=w*devicePixelRatio;canvas.height=h*devicePixelRatio;ctx.scale(devicePixelRatio,devicePixelRatio);ctx.fillStyle='#101d2a';ctx.fillRect(0,0,w,h);const xy=row=>[40+(row.cg_position_enu_m[0]/1000-bounds.lo)/(bounds.hi-bounds.lo)*(w-54),h-32-row.cg_position_enu_m[2]/1000/bounds.top*(h-55)];ctx.font='11px system-ui';ctx.fillStyle='#c2d1df';ctx.fillText(`高度0〜${bounds.top.toFixed(1)}km / 東${bounds.lo.toFixed(1)}〜${bounds.hi.toFixed(1)}km`,8,15);function path(points,color){ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();points.forEach((row,i)=>{const [x,y]=xy(row);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke()}path(rows,'#3b5165');path(done,colors[k]);const [x,y]=xy(r);ctx.fillStyle=colors[k];ctx.beginPath();ctx.arc(x,y,5,0,7);ctx.fill();const [tx,ty]=xy({cg_position_enu_m:[0,0,a.pins[0].position_enu_m[2]-a.pins[0].height_above_support_m]});ctx.fillStyle='#e89aa4';ctx.fillRect(tx-3,ty-5,6,6);ctx.fillStyle='#c2d1df';ctx.fillText('支持面',tx+6,ty);document.getElementById(m+'-state').textContent=`保存T+${r.time_s.toFixed(2)}s / +${r.elapsed_s.toFixed(2)}s${t>=rows[rows.length-1].elapsed_s-1e-9?'（終端）':''} / ${r.phase} / 北${(r.cg_position_enu_m[1]/1000).toFixed(3)}km / 傾斜${a.tilt_deg.toFixed(2)}° / 主推力上向き加速度${r.main_thrust.upward_acceleration_mps2.toFixed(2)}m/s² / 燃料${(a.propellant_kg/1000).toFixed(2)}t`;
const target=document.getElementById(m+'-gates');target.replaceChildren();for(const [name,margin] of Object.entries(r.margins)){const p=document.createElement('div');p.className='gate '+(margin<0?'bad':'good');p.textContent=`${names[name]}：余裕 ${margin.toFixed(name==='body_rate'?4:2)} ${units[name]} ${margin<0?'範囲外':'範囲内'}`;target.append(p)}})}
choice.addEventListener('change',setup);slider.addEventListener('input',()=>{stop();draw()});play.addEventListener('click',()=>{if(timer){stop();return}if(+slider.value>=+slider.max)slider.value=0;play.textContent='停止';timer=setInterval(()=>{slider.value=Math.min(+slider.max,+slider.value+0.25);draw();if(+slider.value>=+slider.max)stop()},100)});window.addEventListener('resize',draw);setup();</script></html>'''
