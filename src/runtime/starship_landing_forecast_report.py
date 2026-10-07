"""Saved finite six-DOF forecasts, separated from recovery execution/support."""
from html import escape
import json
import math

LABELS = {"legacy": "従来の開始判定", "burn_now": "今すぐ燃焼", "wait_2": "2秒待って燃焼", "wait_4": "4秒待って燃焼"}


def report(bundle):
    rows = []
    for case in bundle["cases"]:
        for name, method in case["methods"].items():
            terminal = method["terminal"]
            first = method["landing_request_time_s"]
            cells = [f'{case["cutoff_time_s"]:.1f}', LABELS[name], f'{case["origin_time_s"]:.2f}',
                f'{first:.2f}' if first is not None else "未要求", f'{method["duration_s"]:.2f}',
                f'{method["terminal_ground_speed_mps"]:.2f}',
                f'{math.hypot(*terminal["arrival"]["midpoint_enu_m"][:2]):,.1f}',
                f'{terminal["arrival"]["tilt_deg"]:.2f}', f'{terminal["margins"]["fuel_reserve"]/1000:+.2f}',
                "予測で範囲内" if method["verification"]["predicted_handoff_eligible"] else "予測で未達"]
            rows.append("<tr>"+"".join(f"<td>{escape(value)}</td>" for value in cells)+"</tr>")
    data = json.dumps(bundle, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")
    return '''<!doctype html><html lang="ja"><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Starship · 6DOF着陸開始予測</title><style>*{box-sizing:border-box}body{margin:0;background:#0c121a;color:#dce7f3;font:15px/1.7 system-ui}main{max-width:1480px;padding:28px;margin:auto}h1{font-size:27px}h2{font-size:21px}p{color:#b9cadb;max-width:1200px}.note{border-left:3px solid #e6b875;padding:8px 15px}.scroll{overflow:auto}table{border-collapse:collapse;white-space:nowrap;width:100%;font-size:13px}th,td{text-align:left;padding:9px;border-bottom:1px solid #304557}button,select{font:inherit;padding:6px 12px;background:#182c3e;color:inherit;border:1px solid #536c82;border-radius:5px}button{cursor:pointer}.controls{display:flex;align-items:center;gap:14px;flex-wrap:wrap}input{flex:1;min-width:130px}.panels{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}article{background:#101e2b;border:1px solid #32495d;padding:12px;border-radius:8px;min-width:0}h3{font-size:15px}canvas{width:100%;height:250px}article p{font-size:12px}a{color:#8dcaf6}@media(max-width:950px){.panels{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:736px){main{padding:12px}.panels{grid-template-columns:1fr}h1{font-size:22px}}</style>
<main><h1>実姿勢と有限応答を含む、着陸開始の予測</h1>
<p class=note>これは予測の開発比較です。機体の位置・速度・姿勢・角速度、全エンジン／fin状態、制御器の参照姿勢と記憶を引き継いで、同じ6DOFを積分しています。
支持点の位置・速度・姿勢・燃料条件を同時に検査します。予測された到達はキャッチ支持や実行、ミッション成功ではありません。</p>
<p>従来5条件を各1回再現し、各状態から4候補を予測しました。従来経路と全保存状態・指令が一致し、従来の予測もその後の保存軌跡を一致させることを要求しています。
待機中も時間・位置・姿勢・燃料を積分します。機体・空力・制御係数とキャッチ窓は固定です。これは同じモデルの決定的な継続検査で、独立した物理モデルによる妥当性検証ではありません。</p>
<p>予測到達：''' + str(bundle["predicted_arrival_count"]) + '/' + str(bundle["forecast_count"]) + '''。成立候補がなければ「選択なし」とします。本番方針への採用やMissionOSからの飛行中指令は行っていません。
比較の開始点は過去の従来燃焼要求から4秒以上前の保存点です。この選び方には過去の結果を使っており、オンラインの開始判定や未使用条件での評価ではありません。
予測計算のCPU時間は呼出元の飛行時計を進めません。実時間の期限内に計算できることは未検証です。</p>
<p>比較範囲は、従来の燃焼要求の約4秒前から従来の要求時刻までです。従来より遅い要求は今回試していません。候補は要求時刻の指定であり、実際の点火・推力の立上がりは有限なエンジン応答で別に計算します。13/5/3は段階の最大基数で、実際の要求基数は配分器が選びます。</p><h2>同じ開始状態からの4予測</h2><div class=scroll><table><thead><tr><th>boostback停止 T+秒</th><th>候補</th><th>開始 T+秒</th><th>燃焼要求 T+秒</th><th>予測区間 秒</th><th>予測終端CG速度 m/s</th><th>予測終端pin水平誤差 m</th><th>予測終端傾斜 °</th><th>予測支持燃料余裕 t</th><th>予測到達</th></tr></thead><tbody>''' + "".join(rows) + '''</tbody></table></div>
<h2>保存した予測の再生</h2><div class=controls><label for=case>停止条件</label><select id=case></select><button id=play>再生</button><output id=clock></output><input id=time type=range step=any min=0 aria-label="予測開始からの経過秒"></div><p id=selection></p><div class=panels>''' + "".join(f'<article><h3>{label}</h3><canvas id="{name}" aria-label="{label}の保存予測軌跡"></canvas><p id="{name}-state"></p></article>' for name, label in LABELS.items()) + '''</div>
<p>図は回転するタワーENUのCG東位置と高度（km）、北位置は数値で表示します。全保存予測を薄線、選択時刻以前の部分を実線で示します。
補間せず直前の保存点を表示し、各予測は自身の終端で止めます。4本の尺度と開始状態は共通です。
到達の診断はCGではなく両支持点で行い、重心移動と角速度の寄与も含めています。</p>
<p><a href=study.json>比較・検査結果・圧縮／展開後hash</a> · <a href=inputs.json>固定入力とソースhash</a> · <a href=attempt.json>実行前の計算予算</a></p></main>
<script>const study=''' + data + ''';const methods=['legacy','burn_now','wait_2','wait_4'],colors=['#7dbbef','#deb56e','#96d6a7','#cf9be8'],choice=document.getElementById('case'),slider=document.getElementById('time'),play=document.getElementById('play');let timer=null,bounds;
study.cases.forEach((c,i)=>{const o=document.createElement('option');o.value=i;o.textContent=`T+${c.cutoff_time_s.toFixed(1)}秒`;choice.append(o)});
function stop(){if(timer)clearInterval(timer);timer=null;play.textContent='再生'}
function setup(){stop();const c=study.cases[+choice.value],all=Object.values(c.methods).flatMap(m=>m.states);slider.max=Math.max(...all.map(p=>p.time_s-c.origin_time_s));slider.value=0;bounds={lo:Math.min(0,...all.map(p=>p.cg_position_enu_m[0]/1000))-1,hi:Math.max(0,...all.map(p=>p.cg_position_enu_m[0]/1000))+1,top:Math.max(1,...all.map(p=>p.cg_position_enu_m[2]/1000))+1};document.getElementById('selection').textContent='予測の選択：'+(c.selection==='no_capture_candidate'?'成立候補なし（no_capture_candidate）':c.selection)+'。飛行への指令・採用ではありません。';draw()}
function draw(){const c=study.cases[+choice.value],t=+slider.value;document.getElementById('clock').textContent=`予測 +${t.toFixed(2)}s`;methods.forEach((m,i)=>{const rows=c.methods[m].states,done=rows.filter(p=>p.time_s-c.origin_time_s<=t+1e-9),p=done[done.length-1]||rows[0],canvas=document.getElementById(m),ctx=canvas.getContext('2d'),w=canvas.clientWidth,h=250;canvas.width=w*devicePixelRatio;canvas.height=h*devicePixelRatio;ctx.scale(devicePixelRatio,devicePixelRatio);ctx.fillStyle='#101e2b';ctx.fillRect(0,0,w,h);const xy=p=>[35+(p.cg_position_enu_m[0]/1000-bounds.lo)/(bounds.hi-bounds.lo)*(w-50),h-30-p.cg_position_enu_m[2]/1000/bounds.top*(h-50)];ctx.font='11px system-ui';ctx.fillStyle='#bbccde';ctx.fillText(`高度0〜${bounds.top.toFixed(1)}km / 東${bounds.lo.toFixed(1)}〜${bounds.hi.toFixed(1)}km`,8,16);function line(items,color){ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath();items.forEach((p,j)=>{const [x,y]=xy(p);j?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke()}line(rows,'#374b5e');line(done,colors[i]);const [x,y]=xy(p);ctx.fillStyle=colors[i];ctx.beginPath();ctx.arc(x,y,5,0,7);ctx.fill();document.getElementById(m+'-state').textContent=`保存予測T+${p.time_s.toFixed(2)}s / +${(p.time_s-c.origin_time_s).toFixed(2)}s${t>=rows[rows.length-1].time_s-c.origin_time_s-1e-9?'（終端）':''} / 北${(p.cg_position_enu_m[1]/1000).toFixed(3)}km / 傾斜${p.arrival.tilt_deg.toFixed(2)}° / 主推力上向き寄与${p.main_thrust.upward_acceleration_mps2.toFixed(2)}m/s² / 燃料${(p.arrival.propellant_kg/1000).toFixed(2)}t / 支持燃料余裕${(p.margins.fuel_reserve/1000).toFixed(2)}t`})}
choice.addEventListener('change',setup);slider.addEventListener('input',()=>{stop();draw()});play.addEventListener('click',()=>{if(timer){stop();return}if(+slider.value>=+slider.max)slider.value=0;play.textContent='停止';timer=setInterval(()=>{slider.value=Math.min(+slider.max,+slider.value+0.5);draw();if(+slider.value>=+slider.max)stop()},100)});window.addEventListener('resize',draw);setup();</script></html>'''
