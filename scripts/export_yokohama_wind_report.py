#!/usr/bin/env python3
"""Publish portable observations of finished wind trials, including failures."""

from __future__ import annotations
import argparse
import hashlib
import html
import json
import math
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.runtime.yokohama_wind import verify_wind  # noqa: E402
from scripts.build_yokohama_battery_video import build  # noqa: E402


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


def export(runs, output):
    output.mkdir(parents=True, exist_ok=True)
    cases = []
    for run in runs:
        result, config = read(run / "result.json"), read(run / "config.json")
        worker = read(run / "worker-result.json")
        rows = [json.loads(x) for x in (run / "flight-trajectory.jsonl").read_text().splitlines()]
        wind = verify_wind(run, config, rows)
        folder = output / run.name
        folder.mkdir(exist_ok=True)
        video = folder / "video"
        if not video.exists():
            build(run, video)
        assert read(video / "battery-metadata.json")["source_trajectory_sha256"] == sha(
            run / "flight-trajectory.jsonl"
        )
        checks = {}
        for filename in [
            "verification.json",
            "decision-verification.json",
            "payload-verification.json",
        ]:
            if (run / filename).exists():
                value = read(run / filename)
                checks[filename] = value.get("status")
                write(folder / filename, value)
        holds = read(run / "hold-results.json") if (run / "hold-results.json").exists() else []
        trajectory = []
        for row in rows:
            if trajectory and row["sim_s"] <= trajectory[-1]["sim_s"]:
                continue
            trajectory.append(
                dict(
                    sim_s=row["sim_s"],
                    xyz=row["vehicle"]["xyz"],
                    phase=row["phase"],
                    battery_fraction=row["battery_fraction"],
                )
            )
        final = rows[-1]
        w, x, y, z = final["vehicle"]["quat_wxyz"]
        pitch = math.degrees(math.asin(max(-1, min(1, 2 * (w * y - z * x)))))
        receipt = (
            read(run / "payload-receipt.json") if (run / "payload-receipt.json").exists() else None
        )
        cargo_link = (
            ET.parse(run / "models/worlds/default.sdf")
            .getroot()
            .find("world/model[@name='delivery_payload']/link")
        )
        cargo_metrics = None
        if receipt:
            stable = receipt["assessment"]["evidence"]["stable_observations"]
            cargo_metrics = dict(
                observed_stable_sim_s=stable[-1]["sim_s"] - stable[0]["sim_s"],
                pad_center_error_m=math.dist(
                    stable[-1]["payload"]["xyz"][:2],
                    config["world"]["payload_delivery"]["pad_world_xyz_m"][:2],
                ),
                receipt_id=receipt["receipt_id"],
            )
        final_stage = next(
            (s for s in config["flight_stages"] if s["name"] == final["phase"]), None
        )
        safe = dict(
            case=run.name,
            run_id=config["run_id"],
            world_sha256=config["world"]["world_sha256"],
            wind=config["world"]["wind"],
            observed_cargo_link=cargo_link.attrib["name"] if cargo_link is not None else None,
            metadata_note="Early run affected_links labels cargo as link; actual SDF name is payload_link. Physics flag and verification use the actual element.",
            status=result["status"],
            reason=worker.get("reason"),
            last_phase=final["phase"],
            final_target_error_m=math.dist(
                final["vehicle"]["xyz"], final_stage["target_world_xyz_m"]
            )
            if final_stage
            else None,
            arrival_timeout_wall_s=final_stage["arrival_timeout_s"] if final_stage else None,
            duration_sim_s=final["sim_s"] - rows[0]["sim_s"],
            decision_backend=result["decision_backend"],
            native_vla_invoked=result["vla_invoked"],
            native_wam_invoked=result["wam_invoked"],
            gpu_requested=result["gpu_requested"],
            cleanup=result["cleanup"],
            holds_passed=sum(h["passed"] for h in holds),
            holds_recorded=len(holds),
            holds_required=13,
            max_hold_horizontal_error_m=max(
                [h["max_horizontal_error_m"] for h in holds], default=None
            ),
            max_hold_vertical_error_m=max([h["max_vertical_error_m"] for h in holds], default=None),
            final_observed_pitch_deg=pitch,
            receipt_present=receipt is not None,
            cargo=cargo_metrics,
            final_observed_tilt_deg=math.degrees(
                math.acos(max(-1, min(1, 1 - 2 * (x * x + y * y))))
            ),
            completion_verified=result["status"] == "passed"
            and len(checks) == 3
            and all(v == "passed" for v in checks.values()),
            verifiers=checks,
            evidence_hashes={
                p.name: sha(p)
                for p in [
                    run / "config.json",
                    run / "result.json",
                    run / "worker-result.json",
                    run / "flight-trajectory.jsonl",
                    run / "flight-events.jsonl",
                ]
            },
            source_sha256=result["source_sha256"],
            publication_verifier_sha256={
                str(p.relative_to(REPO)): sha(p)
                for p in [
                    REPO / "src/runtime/yokohama_wind.py",
                    REPO / "scripts/verify_yokohama_sitl.py",
                    REPO / "scripts/verify_yokohama_decisions.py",
                    REPO / "scripts/verify_yokohama_payload.py",
                ]
            },
            native_wind_flight_verified=False,
        )
        write(folder / "summary.json", safe)
        write(folder / "trajectory.json", trajectory)
        write(folder / "wind-verification.json", wind)
        write(folder / "hold-results.json", holds)
        cases.append(
            dict(
                **safe,
                trajectory=trajectory,
                plan=[s["target_world_xyz_m"] for s in config["flight_stages"]],
            )
        )
    write(
        output / "summary.json",
        [{k: v for k, v in c.items() if k not in ("trajectory", "plan")} for c in cases],
    )
    options = "".join(
        f'<option value="{i}" {"selected" if i == len(cases) - 1 else ""}>{html.escape(c["case"])}</option>'
        for i, c in enumerate(cases)
    )
    payload = json.dumps(cases, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    template = """<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>風とバッテリー | MissionOS</title>
<style>*{box-sizing:border-box}body{margin:0;background:#102832;color:#eaf3ef;font:16px/1.7 system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:32px}h1{font-size:30px;margin:8px 0}h2{font-size:20px}p{color:#bdd1d1}a{color:#a6e5ce}button,select{font:inherit;background:#244852;color:white;border:1px solid #57747c;border-radius:7px;padding:8px}select{max-width:100%}.grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.card{padding:20px;border:1px solid #385864;border-radius:10px}.note{color:#f1c590}canvas{display:block;width:100%;height:340px;background:#1b3944;border-radius:8px}video{width:100%;border-radius:8px}input{width:100%;accent-color:#82dbb7}#status{font-weight:600;color:#e2bb88}#stamp{font:13px/1.7 monospace;overflow-wrap:anywhere}.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.small{font-size:13px}@media(max-width:720px){main{padding:18px}.grid{grid-template-columns:1fr}h1{font-size:25px}.card{padding:14px}}</style>
<main><p class="small">MISSIONOS / CPU WIND STRESS + RECORDED BATTERY</p><h1>風を加え、観測で確かめる。</h1><p>海上はAP、市街地の判断はCPUの模擬モデル。今回の風あり試験で実VLA＋WAMは動かしていません。基準を緩めず、失敗も残します。</p><div class="controls"><label>試験 <select id="case" aria-label="試験を選ぶ">OPTIONS</select></label><button id="play">軌跡を再生</button></div><p id="status"></p><p id="condition"></p><div class="grid"><section class="card"><h2>観測した軌跡</h2><canvas id="map" aria-label="記録した機体位置の平面図"></canvas><input id="time" type="range" min="0" step="any" value="0" aria-label="記録時刻"><div id="stamp"></div><p class="small">破線：計画、緑：観測した経路、黄：現在の観測点。全工程表示は計画範囲を基準に縮尺を固定。移動を生成した映像ではありません。</p></section><section class="card"><h2>実カメラ＋模擬バッテリー</h2><video id="movie" controls playsinline preload="metadata" aria-label="風試験の残量付き映像"></video><p class="small">Gazeboで記録したRGB。8倍速。軌跡の時刻とは独立した再生です。SITL残量は時間ベースの模擬値。電流・Wh・風や推論の消費電力は未計測。欠測はUNAVAILABLE。</p><p id="links"></p></section></div><section><h2>今回分かった範囲</h2><p>一様風の力は、飛行経路外に置いた推進力のない物体の移動と、風を受けない対照物体で確認します。機体と荷物にも風を適用しています。</p><p class="note">Gazeboの近似的な力のモデルです。風速設定は実機の運用可能風速を意味しません。突風、建物による乱流、波、船の動揺は含みません。</p><p><a href="REPORT-ja.md">日本語レポート</a> · <a href="summary.json">全試験結果</a> · <a href="../yokohama-cargo-flight/index.html#battery-video">実VLA＋WAMの無風飛行と残量付き動画</a></p></section><p class="small">街区原典：横浜市・Project PLATEAU（2024年度公開カタログ）を加工 / CC BY 4.0。<a href="../yokohama-urban-scene/ATTRIBUTION.md">出典とライセンス</a>。</p></main>
<script>const cases=DATA;const choose=document.getElementById('case'),slider=document.getElementById('time'),canvas=document.getElementById('map'),ctx=canvas.getContext('2d'),button=document.getElementById('play');let c,playing=false,tick;
function draw(){const rows=c.trajectory,t=rows[0].sim_s+Number(slider.value);let i=0;while(i+1<rows.length&&rows[i+1].sim_s<=t)i++;const row=rows[i];document.getElementById('stamp').textContent=`SIM ${row.sim_s.toFixed(1)} s · ${row.phase} · SITL battery ${(row.battery_fraction*100).toFixed(1)}% · ${i+1}/${rows.length}`;const w=canvas.clientWidth,h=340;canvas.width=w*devicePixelRatio;canvas.height=h*devicePixelRatio;ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);const points=c.plan.concat(rows.map(r=>r.xyz)),xs=points.map(p=>p[0]),ys=points.map(p=>p[1]),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys),scale=Math.min((w-50)/(maxX-minX||1),(h-50)/(maxY-minY||1));const xy=p=>[25+(p[0]-minX)*scale,h-25-(p[1]-minY)*scale];function path(ps,color,dash){ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash(dash);ctx.beginPath();ps.forEach((p,j)=>{const [x,y]=xy(p);j?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke()}path(c.plan,'#7896a0',[5,5]);path(rows.slice(0,i+1).map(r=>r.xyz),'#8be8bf',[]);let [x,y]=xy(row.xyz);ctx.fillStyle='#ffd18b';ctx.beginPath();ctx.arc(x,y,5,0,Math.PI*2);ctx.fill();ctx.fillStyle='#d7e6e8';ctx.font='12px system-ui';ctx.fillText('ENU / north ↑',16,20)}
function load(){c=cases[Number(choose.value)];playing=false;button.textContent='軌跡を再生';slider.max=c.duration_sim_s;slider.value=0;document.getElementById('status').textContent=(c.completion_verified?'配送・帰船の検証に合格':'全行程は未達')+` · AP保持 ${c.holds_passed}/${c.holds_required} · ${c.last_phase}`;document.getElementById('condition').textContent=`東向き ${c.wind.velocity_enu_mps[0]} m/s設定 / `+(c.wind.after_takeoff?'離陸・保持後に風を開始':'生成時から風あり')+(c.reason?' / '+c.reason:'');const video=document.getElementById('movie');video.src=c.case+'/video/battery-flight.mp4';video.poster=c.case+'/video/battery-preview.png';document.getElementById('links').innerHTML=`<a href="${c.case}/summary.json">試験結果</a> · <a href="${c.case}/wind-verification.json">風の作用確認</a>`;draw()}
choose.onchange=load;slider.oninput=draw;button.onclick=()=>{playing=!playing;button.textContent=playing?'一時停止':'軌跡を再生'};function animate(now){if(playing&&tick){slider.value=Math.min(Number(slider.max),Number(slider.value)+(now-tick)/1000*8);draw();if(Number(slider.value)>=Number(slider.max)){playing=false;button.textContent='軌跡を再生'}}tick=now;requestAnimationFrame(animate)}window.onresize=draw;load();requestAnimationFrame(animate);</script></html>"""
    (output / "index.html").write_text(
        template.replace("OPTIONS", options).replace("DATA", payload)
    )
    return cases


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, action="append", required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    cases = export(a.run, a.output)
    print(
        json.dumps(
            [
                {k: c[k] for k in ("case", "status", "completion_verified", "holds_passed")}
                for c in cases
            ]
        )
    )


if __name__ == "__main__":
    main()
