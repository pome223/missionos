"""Portable recorded-trajectory comparison, with no simulator or API calls."""
from __future__ import annotations

from html import escape
import json
import math

from .starship_booster_recovery_verifier import _arrival


def replay_points(run, profile, config):
    points, previous = [], -math.inf
    for sample in run["samples"]:
        time = sample["time_s"]
        arrival = _arrival(sample, profile, config)
        item = {"t": time, "east": arrival["midpoint_enu_m"][0], "north": arrival["midpoint_enu_m"][1],
            "alt": sample["altitude_m"], "tilt": arrival["tilt_deg"],
            "error": sample.get("controller", {}).get("attitude_error_deg"), "q": sample["dynamic_pressure_pa"],
            "fins": [math.degrees(x) for x in sample["flap_angles_rad"][3:]], "fuel": sample["propellant_kg"]}
        if not time > previous or any(not math.isfinite(x) for x in
                (time, item["east"], item["north"], item["alt"], item["tilt"], item["q"], item["fuel"], *item["fins"])):
            raise ValueError("invalid_recorded_replay_sample")
        previous = time
        points.append(item)
    return points


def report(bundle):
    rows = []
    coast_only = bundle.get("candidate_scope") == "coast_only"
    labels = {"baseline": bundle.get("baseline_label", "従来"),
              "candidate": bundle.get("candidate_label", "有限応答・正則化")}
    scope_note = ("前回候補（coast＋着陸）と、動圧100Pa超のcoastだけに適用する候補を比較します。着陸では従来の配分へ戻し、残差を受け取るTVC/RCSの指令も変わります。"
                  if coast_only else "fin配分はcoastと着陸13/5/3基の両区間で変更し、残差を受け取るTVC/RCSの指令も変わります。")
    boundary_note = (f'着陸要求時の全物理状態が一致した条件は{bundle["landing_request_states_equal_count"]}/5。着陸開始前の保存状態と指令、前回候補の再現も比較記録で確認します。差の帰属はこの固定5条件と現在の代理モデルの範囲です。'
                     if coast_only else "この比較だけではcoastと着陸の効果や悪化の原因を分離できません。")
    for pair in bundle["conditions"]:
        for method, item in pair["methods"].items():
            o, m = item["outcome"], item["metrics"]
            error, saturation = m["max_sampled_attitude_error_deg"], m["sample_weighted_actual_angle_saturation_fraction"]
            cells = [f'{pair["cutoff_time_s"]:.1f}', labels[method],
                f"{error:.2f}" if error is not None else "区間なし", f"{100*saturation:.1f}%" if saturation is not None else "—",
                f'{o["final_ground_speed_mps"]:.2f}', f'{o["return_site_distance_m"]/1000:.2f}',
                f'{item["final_propellant_kg"]/1000:.2f}', "到達" if item["verification"]["handoff_reached"] else "未達",
                "成立" if item["candidate_supported"] else "未成立", "整合" if item["verification"]["passed"] else "不整合"]
            rows.append("<tr>"+"".join("<td>"+escape(c)+"</td>" for c in cells)+"</tr>")
    payload = json.dumps(bundle["conditions"], ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")
    outcome = f'候補の到達・支持成立は{bundle["candidate_supported_count"]}/5条件。'
    return '''<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>6DOF フィン配分比較</title>
<style>body{background:#071019;color:#e5edf5;font:16px system-ui;margin:30px auto;max-width:1250px;padding:18px}p{line-height:1.8}a{color:#8ecbff}table{border-collapse:collapse;min-width:1000px;width:100%}th,td{padding:10px;border-bottom:1px solid #344454;text-align:right}.scroll{overflow:auto}.views{display:grid;grid-template-columns:1fr 1fr;gap:18px}canvas{width:100%;height:300px;background:#101e2b;border-radius:8px}label,button,select{font:inherit;margin:8px 10px 8px 0}input{width:100%}.info{min-height:5em;font-size:14px}.note{color:#b8c8d9}h2{font-size:21px}@media(max-width:736px){.views{grid-template-columns:1fr}body{padding:12px;margin:12px auto}h1{font-size:26px}}</style>
<h1>6DOF フィン制御配分の比較</h1><p>'''+outcome+''' 本番制御への採用は別判定です。</p>
<p>同じ分離状態・5停止時刻で、双方を各1回、終端まで実行。機体係数、entry参照の選択、着陸誘導・点火・段数・throttleの判定則、燃料初期値、到達・接触条件を固定しています。'''+scope_note+'''</p>
<div class="scroll"><table><thead><tr>'''+''.join('<th>'+x+'</th>' for x in ("停止 T+秒","配分","coast誤差最大 °","実角度飽和割合","終端速度 m/s","タワー距離 km","残量 t","到達","接触・支持","記録検証"))+'''</tr></thead><tbody>'''+''.join(rows)+'''</tbody></table></div>
<p class="note">coast指標は動圧100〜70,000Paにある保存点だけの値。飽和割合は保存点の実角度が上限の99.5%以上だった割合を、次の保存点まで左端値で重み付けしたものです。連続時間の最大値・飽和率は検証していません。記録が整合してもキャッチ成立とは限りません。</p>
<h2>保存された軌跡と姿勢の再生</h2><label for="condition">停止条件</label><select id="condition"></select><button id="play" type="button">再生</button><span id="clock"></span><input id="time" aria-label="ミッション時刻" type="range" step="any">
<div class="views"><section><h3>'''+escape(labels["baseline"])+'''</h3><canvas id="baseline" aria-label="比較元の東方向位置と高度の保存軌跡"></canvas><p class="info" id="baseline-info"></p></section><section><h3>'''+escape(labels["candidate"])+'''</h3><canvas id="candidate" aria-label="候補の東方向位置と高度の保存軌跡"></canvas><p class="info" id="candidate-info"></p></section></div>
<p class="note">横軸はタワーからの支持点中間の東方向位置、縦軸はCG高度（km）。両図は同じ尺度です。北方向位置は数値で表示します。全保存軌跡を薄く表示し、選択時刻までを強く表示します。オンライン予測、実写、運動方程式の独立再積分ではありません。各runは固有の終端で止まります。</p>
<p>静的trimは要求entry軸の参照で、現在姿勢のtrimではありません。finの次周期予測とTVCの現在推力による配分は時間軸が異なります。予測残差は後続actuatorへの要求で、達成モーメントではありません。'''+boundary_note+'''</p>
<p>CLIのopt-inはローカル開発計算の実行指定で、MissionOSの計画承認ではありません。SpaceXの空力・制御の同定、実機動作、LLM/Jevの価値、本番方針への採用は確認していません。</p><p><a href="comparison.json">比較記録 JSON</a> · <a href="inputs.json">固定入力・source hash</a></p>
<script>const pairs='''+payload+''';
const choice=document.querySelector('#condition'),slider=document.querySelector('#time'),play=document.querySelector('#play'),clock=document.querySelector('#clock');
for(const [i,p] of pairs.entries()){const o=document.createElement('option');o.value=i;o.textContent=`T+${p.cutoff_time_s.toFixed(1)}秒`;choice.append(o)}
let timer=null,bounds;
function stop(){if(timer)clearInterval(timer);timer=null;play.textContent='再生'}
function setup(){stop();const p=pairs[+choice.value],all=Object.values(p.methods).flatMap(x=>x.replay);slider.min=Math.min(...all.map(x=>x.t));slider.max=Math.max(...all.map(x=>x.t));slider.value=slider.min;bounds={lo:Math.min(0,...all.map(x=>x.east))/1000-3,hi:Math.max(0,...all.map(x=>x.east))/1000+3,top:Math.max(1,...all.map(x=>x.alt))/1000+10};draw()}
function draw(){const t=+slider.value,p=pairs[+choice.value];clock.textContent=`T+${t.toFixed(1)}秒`;for(const method of ['baseline','candidate']){const points=p.methods[method].replay,canvas=document.getElementById(method),ctx=canvas.getContext('2d');canvas.width=Math.max(300,canvas.clientWidth)*devicePixelRatio;canvas.height=300*devicePixelRatio;ctx.scale(devicePixelRatio,devicePixelRatio);const w=canvas.clientWidth,h=300,xy=s=>[46+(s.east/1000-bounds.lo)/(bounds.hi-bounds.lo)*(w-65),h-35-s.alt/1000/bounds.top*(h-60)];ctx.fillStyle='#101e2b';ctx.fillRect(0,0,w,h);ctx.strokeStyle='#40566b';ctx.beginPath();ctx.moveTo(46,20);ctx.lineTo(46,h-35);ctx.lineTo(w-15,h-35);ctx.stroke();ctx.fillStyle='#b8c8d9';ctx.font='12px system-ui';ctx.fillText(`高度 0–${bounds.top.toFixed(0)} km`,52,18);ctx.fillText(`東位置 ${bounds.lo.toFixed(0)}〜${bounds.hi.toFixed(0)} km`,52,h-10);function line(items,color){ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();items.forEach((s,i)=>{let [x,y]=xy(s);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke()}line(points,'#304456');const done=points.filter(x=>x.t<=t+1e-9),s=done[done.length-1]||points[0];line(done,method==='baseline'?'#66b9ef':'#ecb86e');const [x,y]=xy(s);ctx.fillStyle=method==='baseline'?'#66b9ef':'#ecb86e';ctx.beginPath();ctx.arc(x,y,5,0,7);ctx.fill();const tower=xy({east:0,alt:0});ctx.fillStyle='#eb7181';ctx.fillRect(tower[0]-3,tower[1]-6,6,6);document.getElementById(method+'-info').textContent=`保存 T+${s.t.toFixed(2)}秒${t>=points[points.length-1].t-1e-9?'（終端）':''} / 高度 ${(s.alt/1000).toFixed(2)}km / 北 ${(s.north/1000).toFixed(2)}km / 傾斜 ${s.tilt.toFixed(2)}° / 姿勢誤差 ${s.error===null?'—':s.error.toFixed(2)+'°'} / 動圧 ${s.q.toFixed(1)}Pa / 実fin [${s.fins.map(x=>x.toFixed(1)).join(', ')}]° / 残量 ${(s.fuel/1000).toFixed(2)}t`}}
choice.addEventListener('change',setup);slider.addEventListener('input',()=>{stop();draw()});play.addEventListener('click',()=>{if(timer){stop();return}if(+slider.value>=+slider.max)slider.value=slider.min;play.textContent='停止';timer=setInterval(()=>{slider.value=Math.min(+slider.max,+slider.value+1);draw();if(+slider.value>=+slider.max)stop()},100)});window.addEventListener('resize',draw);setup();</script></html>'''
