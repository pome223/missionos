"""Replay actual saved development recoveries, with no outcome promotion."""
from html import escape
import json
import math


def report(bundle):
    rows = []
    guards = "、".join(f'{c["label"]} はT+{c["cutoff_time_s"]:.2f}秒'
                      for c in bundle["cases"] if c["cutoff_basis"] == "fuel_or_time_guard" and c["cutoff_time_s"] is not None)
    guard_text = (f'予定停止は共通のT+{bundle["cutoff_time_s"]:.2f}秒ですが、既存の燃料・時間保護は残ります。'
                  +(guards+"で保護による停止になりました。" if guards else "今回の記録に保護による早期停止はありません。")
                  +"表の停止時刻は実際の値です。")
    for case in bundle["cases"]:
        request, terminal, verdict = case["landing_request"], case["terminal"], case["verification"]
        sequence = " → ".join(str(p["plan"]["candidates"].index(p["plan"]["selected"])) for p in case["plans"])
        values = [case["label"], sequence, f'{case["cutoff_time_s"]:.2f}' if case["cutoff_time_s"] is not None else "未実行",
            f'{request["time_s"]:.2f}' if request else "未要求",
            f'{math.hypot(*request["arrival"]["midpoint_enu_m"][:2]):,.1f}' if request else "—",
            f'{request["arrival"]["tilt_deg"]:.2f}' if request else "—",
            f'{request["arrival"]["propellant_kg"]/1000:.2f}' if request else "—",
            f'{case["outcome"]["final_ground_speed_mps"]:.2f}',
            f'{math.hypot(*terminal["arrival"]["midpoint_enu_m"][:2]):,.1f}',
            f'{terminal["margins"]["fuel_reserve"]/1000:+.2f}',
            "到達" if verdict["handoff_reached"] else "未達", "支持" if verdict["catch_supported_after_handoff"] else "未成立"]
        rows.append("<tr>"+"".join(f"<td>{escape(x)}</td>" for x in values)+"</tr>")
    data = json.dumps(bundle, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")
    return '''<!doctype html><html lang=ja><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Starship · 帰還指令の6DOF実行</title><style>*{box-sizing:border-box}body{margin:0;background:#0b1119;color:#dde8f4;font:15px/1.7 system-ui}main{max-width:1320px;margin:auto;padding:26px}h1{font-size:27px}p{color:#bdcfe0}.note{border-left:3px solid #e7b575;padding-left:15px}.scroll{overflow:auto}table{border-collapse:collapse;white-space:nowrap;font-size:13px;width:100%}td,th{padding:9px;border-bottom:1px solid #344655;text-align:left}.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap}input{flex:1;min-width:150px}button,select{background:#182a3b;color:inherit;padding:7px;font:inherit;border:1px solid #58758d}canvas{width:100%;height:350px;background:#10202e}a{color:#8ac7fa}.panels{display:grid;grid-template-columns:1fr 1fr;gap:16px}article{min-width:0}article p{font-size:13px}@media(max-width:700px){main{padding:12px}.panels{grid-template-columns:1fr}h1{font-size:22px}}</style>
<main><h1>帰還の指令候補を、同じ6DOFで実行</h1><p class=note>同じ打ち上げ由来の保存分離状態から、従来方針と5種類の帰還速度目標を実行した開発比較です。
機体・空力・有限エンジン／fin応答・着陸制御・キャッチ条件は固定です。発射からの再実行、SpaceX実機の精度、MissionOS本番方針の採用ではありません。</p>
<p>候補0〜4は既存の計画器の上向き速度目標200／400／600／800／1000 m/sです。各候補は初回と姿勢整列時の再計画で同じ番号を選び、横方向の目標はその時点の状態から再計算します。
番号が同じでも目標ベクトルは固定ではありません。従来方針は元の選択規則を保ち、全保存物理状態・指令と終端が過去の記録に完全一致することを検査しています。
'''+escape(guard_text)+'''</p>
<p>到達 '''+str(bundle["handoff_count"])+"/"+str(bundle["recovery_count"])+'''、支持 '''+str(bundle["support_count"])+"/"+str(bundle["recovery_count"])+'''。帰還指令の効果と、着陸要求時の位置・実姿勢・燃料を測定します。
到達は両pinの位置・速度・機体姿勢／角速度・支持燃料の8条件を同時に満たす場合だけです。終端のCG速度が低いだけではキャッチ成功になりません。
独立検証は保存記録の連続性と到達／接触の計算で、別の運動方程式による再積分ではありません。1つの開発開始状態であり、未使用条件・故障・風への頑健性は未検証です。</p>
<div class=scroll><table><thead><tr>'''+"".join("<th>"+x+"</th>" for x in ("方針", "初回→再計画の番号", "停止 T+秒", "着陸要求 T+秒", "要求時pin誤差 m", "要求時傾斜 °", "要求時燃料 t", "終端CG速度 m/s", "終端pin誤差 m", "支持燃料余裕 t", "同時到達", "接触支持"))+'''</tr></thead><tbody>'''+"".join(rows)+'''</tbody></table></div>
<h2>保存状態の比較再生</h2><div class=controls><label for=choice>候補</label><select id=choice></select><button id=play>再生</button><output id=clock></output><input id=time type=range min=0 step=any aria-label="分離からの経過秒"></div>
<p>共通尺度のタワーENU東位置・CG高度（km）。薄線は保存全経路、実線は選択時刻まで。補間せず直前の保存点を表示し、それぞれ自身の終端で止めます。</p>
<div class=panels><article><h3>従来の選択規則</h3><canvas id=base></canvas><p id=base-state></p></article><article><h3 id=title></h3><canvas id=candidate></canvas><p id=candidate-state></p></article></div>
<p><a href=study.json>全結果とhash</a> · <a href=inputs.json>共通開始状態と固定設定</a> · <a href=attempt.json>実行前の予算</a></p></main><script>const study='''+data+''',choice=document.getElementById('choice'),slider=document.getElementById('time'),play=document.getElementById('play'),origin=study.cases[0].states[0].time_s;let timer=null,bounds;
study.cases.slice(1).forEach((c,i)=>{const o=document.createElement('option');o.value=i+1;o.textContent=c.label+'（上向き目標'+(200*(i+1))+'m/s）';choice.append(o)});
function stop(){if(timer)clearInterval(timer);timer=null;play.textContent='再生'}
function setup(){stop();const cases=[study.cases[0],study.cases[+choice.value]],all=cases.flatMap(c=>c.states);slider.max=Math.max(...all.map(p=>p.time_s-origin));slider.value=0;bounds={lo:Math.min(0,...all.map(p=>p.cg_position_enu_m[0]/1000))-1,hi:Math.max(0,...all.map(p=>p.cg_position_enu_m[0]/1000))+1,top:Math.max(1,...all.map(p=>p.cg_position_enu_m[2]/1000))+1};document.getElementById('title').textContent=cases[1].label;draw()}
function draw(){const elapsed=+slider.value;document.getElementById('clock').textContent='分離 +'+elapsed.toFixed(2)+'s';[study.cases[0],study.cases[+choice.value]].forEach((c,i)=>{const id=i?'candidate':'base',canvas=document.getElementById(id),ctx=canvas.getContext('2d'),w=canvas.clientWidth,h=350,done=c.states.filter(p=>p.time_s-origin<=elapsed+1e-9),p=done.at(-1)||c.states[0];canvas.width=w*devicePixelRatio;canvas.height=h*devicePixelRatio;ctx.scale(devicePixelRatio,devicePixelRatio);ctx.fillStyle='#10202e';ctx.fillRect(0,0,w,h);const xy=p=>[35+(p.cg_position_enu_m[0]/1000-bounds.lo)/(bounds.hi-bounds.lo)*(w-50),h-30-p.cg_position_enu_m[2]/1000/bounds.top*(h-55)];ctx.font='11px system-ui';ctx.fillStyle='#bdcfe0';ctx.fillText('高度0〜'+bounds.top.toFixed(1)+'km / 東'+bounds.lo.toFixed(1)+'〜'+bounds.hi.toFixed(1)+'km',8,16);function line(rows,color){ctx.beginPath();ctx.strokeStyle=color;ctx.lineWidth=2;rows.forEach((p,j)=>{const [x,y]=xy(p);j?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke()}line(c.states,'#3a5367');line(done,i?'#e4ba76':'#8fc8f4');const [x,y]=xy(p);ctx.fillStyle=i?'#e4ba76':'#8fc8f4';ctx.beginPath();ctx.arc(x,y,5,0,7);ctx.fill();const v=p.cg_velocity_enu_mps;document.getElementById(id+'-state').textContent='T+'+p.time_s.toFixed(2)+'s'+(elapsed>=c.states.at(-1).time_s-origin-1e-9?'（終端）':'')+' / CG速度'+Math.hypot(...v).toFixed(2)+'m/s / 北'+(p.cg_position_enu_m[1]/1000).toFixed(3)+'km / 傾斜'+p.arrival.tilt_deg.toFixed(2)+'° / 燃料'+(p.arrival.propellant_kg/1000).toFixed(2)+'t / 失敗条件 '+(p.failed_gates.join(', ')||'なし')})}
choice.addEventListener('change',setup);slider.addEventListener('input',()=>{stop();draw()});play.addEventListener('click',()=>{if(timer){stop();return}if(+slider.value>=+slider.max)slider.value=0;play.textContent='停止';timer=setInterval(()=>{slider.value=Math.min(+slider.max,+slider.value+1);draw();if(+slider.value>=+slider.max)stop()},100)});window.addEventListener('resize',draw);setup();</script></html>'''
