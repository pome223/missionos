#!/usr/bin/env python3
"""Export verified measured trajectories, native camera video and a local replay."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.verify_urban_reobserve import verify  # noqa: E402
from scripts.urban_reobserve_contract import MODES, BARRIER, CHECKPOINT  # noqa: E402
from scripts.urban_navigation_contract import scene_spec  # noqa: E402


HTML = """<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PX4：停止・再観測・迂回</title>
<style>
:root{color-scheme:light;font-family:system-ui,sans-serif;color:#182e40;background:#f3f6f8}*{box-sizing:border-box}body{margin:0;padding:24px;max-width:1260px;margin:auto}h1{font-size:clamp(23px,4vw,36px);margin:4px 0 12px}.eyebrow{font-size:12px;letter-spacing:.13em;color:#52717b}p{line-height:1.7}.lead{max-width:950px}.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:#f3f6f8f2;padding:14px 0;z-index:2}button,select{font:inherit;border:1px solid #a5b7c1;border-radius:6px;background:white;padding:8px 12px}input{flex:1;min-width:140px;accent-color:#007b7e}output{min-width:100px;font-variant-numeric:tabular-nums}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}article{background:white;border:1px solid #d4dfe5;border-radius:12px;overflow:hidden}article h2{font-size:20px;margin:18px 18px 6px}.detail{margin:0 18px 12px;font-size:14px;color:#526974;min-height:48px}canvas{width:100%;display:block;background:#f8fafb}video{width:100%;display:block;background:#182e40}.status{padding:12px 18px;min-height:64px;font-size:14px;font-variant-numeric:tabular-nums}.legend{font-size:13px;color:#526974}.note{background:#e7eff3;padding:16px;border-radius:8px;margin-top:22px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:10px 8px;text-align:left;border-bottom:1px solid #d7e1e7}.tablewrap{overflow-x:auto}.limit{font-size:13px;color:#526974}a{color:#056875}@media(max-width:736px){body{padding:16px}.grid{grid-template-columns:1fr}.controls{gap:8px}.detail{min-height:0}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto}}
</style>
<main><div class="eyebrow">MISSIONOS · PX4 / GAZEBO · RECORDED DEVELOPMENT TRIAL</div>
<h1>停止して見直し、通れる経路へ。</h1>
<p class="lead" id="finding"></p>
<p class="legend">地図：灰色＝建物、橙色＝飛行開始後に出現した障害物、線＝記録された飛行軌跡。下段は実際のシミュレーターカメラ映像です。</p>
<div class="controls"><button id="play" type="button">再生</button><label for="speed">速度</label><select id="speed"><option value="1">1×</option><option value="4" selected>4×</option></select><input id="time" type="range" min="0" step="0.1" value="0" aria-label="出発からのシミュレーション経過秒"><output id="clock"></output></div>
<div class="grid" id="cases"></div>
<div class="note"><strong>何を確かめたか</strong><p>停止地点で取り直した深度画像によって、直進候補がふさがれたことを検出し、供給済みの迂回候補を選び直せるかを比較しました。安全判定は両方式に共通です。障害物は出発後に挿入され、その後は動きません。</p></div>
<div class="tablewrap"><table><thead><tr><th>観測指標</th><th>経路を維持</th><th>再観測・再選択</th></tr></thead><tbody id="metrics"></tbody></table></div>
<p class="limit">各方式1試行の開発比較。停止は予定したチェックポイントで行っています。連続飛行中の緊急回避、任意経路の生成、動く障害物の予測、実機飛行、WAMの効果は検証していません。WAM・Jev・GPU呼出しは0です。録画はシミュレーション時刻に合わせて再生し、推論映像や未来の予測を含みません。</p>
<p class="limit">距離と経過時間は出発から着陸・disarmまで。停止中に16フレームを取得しているため、最新観測の古さと停止・判断に要した時間は異なります。未到達の短い経路を効率向上とは解釈しません。位置表示は間引いた観測点で、滑らかな補間や連続接触保証ではありません。</p>
<p class="limit">Apartment assets: OSRF gazebo_models, CC BY 3.0, Nathan Koenig / Cole Biesemeyer. <a href="https://github.com/osrf/gazebo_models">Source</a> · <a href="summary.json">測定値</a> · <a href="manifest.json">証拠ハッシュ</a></p></main>
<script>
const D=__DATA__, slider=document.getElementById('time'), clock=document.getElementById('clock'), play=document.getElementById('play');
slider.max=Math.max(...D.cases.map(c=>c.result.elapsed_departure_through_disarm_sim_s));
document.getElementById('finding').textContent=D.finding;
const labels=['元の経路を維持','停止・再観測して選び直す'];
const views=D.cases.map((c,i)=>{const el=document.createElement('article');el.innerHTML=`<h2>${labels[i]}</h2><p class="detail">${c.result.destination_reached?'目的地に到達し、着陸・disarmを確認':'安全制約で経路を退け、停止地点で着陸・disarmを確認'}</p><canvas width="580" height="400" aria-label="観測された飛行軌跡"></canvas><video preload="auto" muted playsinline poster="${c.mode}-checkpoint.png" aria-label="シミュレーターカメラ映像"><source src="${c.mode}.mp4" type="video/mp4"></video><div class="status"></div>`;document.getElementById('cases').appendChild(el);return{el,canvas:el.querySelector('canvas'),video:el.querySelector('video'),status:el.querySelector('.status')};});
const fmt=(n,u='')=>n.toFixed(2)+u;
const metrics=[['到達',r=>r.destination_reached?'確認':'未到達'],['経路変更回数',r=>r.replan_count],['飛行距離（着陸を含む）',r=>fmt(r.path_from_departure_through_disarm_m,' m')],['経過（sim）',r=>fmt(r.elapsed_departure_through_disarm_sim_s,' s')],['機体包絡の最小余裕',r=>fmt(r.minimum_observed_envelope_clearance_m,' m')],['観測された障害物接触通知',r=>r.building_contact_messages],['停止から再判断まで（sim）',r=>fmt(r.checkpoint_to_resume_sim_s,' s')],['停止から再判断まで（wall）',r=>fmt(r.checkpoint_to_resume_wall_s,' s')],['最新観測の採用時の古さ（wall）',r=>fmt(r.input_age_wall_s.resume,' s')]];
document.getElementById('metrics').innerHTML=metrics.map(([label,f])=>`<tr><th>${label}</th>${D.cases.map(c=>`<td>${f(c.result)}</td>`).join('')}</tr>`).join('');
function draw(t){clock.value=fmt(t,' sim s');D.cases.forEach((c,i)=>{const v=views[i],ctx=v.canvas.getContext('2d'),W=580,H=400,s=14,X=x=>55+x*s,Y=y=>210-y*s;ctx.clearRect(0,0,W,H);ctx.fillStyle='#f8fafb';ctx.fillRect(0,0,W,H);ctx.font='12px system-ui';ctx.fillStyle='#687f8c';for(let x=0;x<=30;x+=5){ctx.fillText(x+' m',X(x)-8,388);ctx.strokeStyle='#e4ebef';ctx.beginPath();ctx.moveTo(X(x),18);ctx.lineTo(X(x),372);ctx.stroke()}for(let y=-10;y<=10;y+=5){ctx.fillText(y+' m',4,Y(y));}function box(b,color){ctx.fillStyle=color;ctx.fillRect(X(b.lower_enu_m[0]),Y(b.upper_enu_m[1]),(b.upper_enu_m[0]-b.lower_enu_m[0])*s,(b.upper_enu_m[1]-b.lower_enu_m[1])*s)}D.buildings.forEach(b=>box(b,'#c8d3da'));if(t>=c.barrier_s)box(D.barrier,'#e9813c');ctx.strokeStyle='#78909c';ctx.setLineDash([4,4]);ctx.beginPath();ctx.arc(X(D.checkpoint[0]),Y(D.checkpoint[1]),8,0,Math.PI*2);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='#607684';ctx.fillText('停止点',X(3)-12,Y(0)+25);ctx.fillText('目的地',X(14)-16,Y(0)+25);ctx.strokeRect(X(14)-5,Y(0)-5,10,10);const selected=c.trace.filter(p=>p.t<=t),p=selected[selected.length-1]||c.trace[0];ctx.strokeStyle=i?'#007f80':'#527cc0';ctx.lineWidth=3;ctx.beginPath();selected.forEach((p,j)=>{j?ctx.lineTo(X(p.p[0]),Y(p.p[1])):ctx.moveTo(X(p.p[0]),Y(p.p[1]))});ctx.stroke();ctx.fillStyle=i?'#007f80':'#527cc0';ctx.beginPath();ctx.arc(X(p.p[0]),Y(p.p[1]),6,0,Math.PI*2);ctx.fill();let state=t<c.barrier_s?'通路へ移動':t<c.stop_s?'障害物出現 → 停止点へ':t<c.resume_s?'停止・新しいRGB-Dを取得':c.result.destination_reached?'迂回経路を実行':'安全制約で中止 → 着陸';if(t>=c.result.elapsed_departure_through_disarm_sim_s)state=c.result.destination_reached?'到達・着陸・disarm確認':'未到達・安全中止・disarm確認';v.status.textContent=`${state} ｜ 高度 ${fmt(p.p[2],' m')} ｜ 位置の観測時刻 ${fmt(p.t,' s')}`;let target=Math.max(0,Math.min(t-c.video_start_s,c.video_duration_s-.05));if(Number.isFinite(v.video.duration)&&Math.abs(v.video.currentTime-target)>.12)v.video.currentTime=target;});}
let playing=false,last=0;play.onclick=()=>{playing=!playing;play.textContent=playing?'一時停止':'再生';last=performance.now();if(playing&&+slider.value>=+slider.max)slider.value=0;};slider.oninput=()=>draw(+slider.value);views.forEach(v=>v.video.addEventListener('loadedmetadata',()=>draw(+slider.value)));function tick(now){if(playing){slider.value=Math.min(+slider.max,+slider.value+(now-last)/1000*+document.getElementById('speed').value);draw(+slider.value);if(+slider.value>=+slider.max){playing=false;play.textContent='再生';}}last=now;requestAnimationFrame(tick)}draw(0);requestAnimationFrame(tick);
</script></html>"""


def render(cohort, output):
    results = [verify(cohort / mode) for mode in MODES]
    output.mkdir(parents=True, exist_ok=False)
    cases, manifest = [], {}
    for mode, result in zip(MODES, results):
        session = cohort / mode / "session"
        origin = result["departure_sim_ns"]
        trace = [
            json.loads(line) for line in (session / "telemetry.jsonl").read_text().splitlines()
        ]
        reduced, last = [], -1e9
        for sample in trace:
            stamp = sample["pose_simulation_time_ns"]
            if stamp is None or not origin <= stamp <= result["terminal_sim_ns"]:
                continue
            t = (stamp - origin) / 1e9
            if t - last >= 0.24:
                reduced.append({"t": t, "p": sample["gazebo_pose_enu_m"]})
                last = t
        events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
        for e in events:
            obs = e.get("observed")
            if obs and origin <= obs["pose_simulation_time_ns"] <= result["terminal_sim_ns"]:
                reduced.append(
                    {
                        "t": (obs["pose_simulation_time_ns"] - origin) / 1e9,
                        "p": obs["gazebo_pose_enu_m"],
                    }
                )
        reduced = sorted({p["t"]: p for p in reduced}.values(), key=lambda p: p["t"])
        frames = json.loads((session / "route-images/frames.json").read_text())["frames"]
        frames = [
            f for f in frames if origin <= f["simulation_time_ns"] <= result["terminal_sim_ns"]
        ]
        checkpoint = min(
            frames, key=lambda f: abs(f["simulation_time_ns"] - result["checkpoint_sim_ns"])
        )
        shutil.copyfile(
            session / "route-images" / checkpoint["file"], output / (mode + "-checkpoint.png")
        )
        with tempfile.TemporaryDirectory(prefix="reobserve-video-") as temporary:
            concat = Path(temporary) / "frames.txt"
            lines = ["ffconcat version 1.0"]
            for i, frame in enumerate(frames):
                path = session / "route-images" / frame["file"]
                if "'" in str(path):
                    raise ValueError("unsupported concat path")
                duration = (
                    (frames[i + 1]["simulation_time_ns"] - frame["simulation_time_ns"]) / 1e9
                    if i + 1 < len(frames)
                    else 0.25
                )
                lines += ["file '" + str(path) + "'", "duration " + str(duration)]
            lines += ["file '" + str(session / "route-images" / frames[-1]["file"]) + "'"]
            concat.write_text("\n".join(lines) + "\n")
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat),
                    "-an",
                    "-fps_mode",
                    "vfr",
                    "-c:v",
                    "libx264",
                    "-crf",
                    "25",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(output / (mode + ".mp4")),
                ],
                check=True,
            )
        gate = next(e for e in events if e["event"] == "reobserve_safety_gate")
        cases.append(
            {
                "mode": mode,
                "result": result,
                "trace": reduced,
                "barrier_s": (result["barrier_appearance_sim_ns"] - origin) / 1e9,
                "stop_s": (result["checkpoint_sim_ns"] - origin) / 1e9,
                "resume_s": (gate["observed"]["pose_simulation_time_ns"] - origin) / 1e9,
                "video_start_s": (frames[0]["simulation_time_ns"] - origin) / 1e9,
                "video_duration_s": (
                    frames[-1]["simulation_time_ns"] - frames[0]["simulation_time_ns"]
                )
                / 1e9
                + 0.25,
            }
        )
        for relative in (
            "events.jsonl",
            "telemetry.jsonl",
            "flight-result.json",
            "route-images/frames.json",
            "history-approach/capture.json",
            "history-resume/capture.json",
            "barrier.sdf",
        ):
            manifest[mode + "/" + relative] = hashlib.sha256(
                (session / relative).read_bytes()
            ).hexdigest()
    finding = (
        "元の直進経路を維持した試行は安全に中止。再観測した試行は左へ迂回し、目的地への到達と着陸を確認しました。"
        if results[0]["safe_abort"] and results[1]["destination_reached"]
        else "結果は下の観測値を参照してください。予定した到達差が成立したとは限りません。"
    )
    data = {
        "finding": finding,
        "cases": cases,
        "buildings": scene_spec("gap")["buildings"],
        "barrier": BARRIER,
        "checkpoint": CHECKPOINT,
        "render_downsample_minimum_sim_s": 0.24,
        "trace_uses_observed_samples_only": True,
    }
    summary = {
        "schema_version": "urban_reobserve_report.v1",
        "runs": results,
        "cohort_size": 2,
        "learned_navigation_benefit_established": False,
        "wam_arrival_headroom_in_this_case": 0 if results[1]["destination_reached"] else None,
        "limitations": [
            "one development trial per method",
            "scripted static insertion",
            "planned checkpoint stop",
            "supplied route candidates",
            "no WAM/GPU",
            "not a Gateway closed-loop task",
        ],
    }
    for name, value in (
        ("summary.json", summary),
        ("replay-data.json", data),
        (
            "manifest.json",
            {
                "raw_sha256": manifest,
                "protocol_sha256": hashlib.sha256(
                    (cohort / "protocol.json").read_bytes()
                ).hexdigest(),
            },
        ),
    ):
        (output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    (output / "index.html").write_text(
        HTML.replace(
            "__DATA__", json.dumps(data, ensure_ascii=False, allow_nan=False).replace("</", "<\\/")
        )
    )
    manifest_path = output / "manifest.json"
    manifest_record = json.loads(manifest_path.read_text())
    manifest_record["artifact_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.iterdir())
        if path.name != "manifest.json"
    }
    manifest_path.write_text(json.dumps(manifest_record, indent=2) + "\n")
    print(json.dumps({"report": str(output / "index.html"), "runs_verified": 2}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cohort", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    render(args.cohort.resolve(), args.output.resolve())
