"""Build a portable Japanese report from six reverified local onboard flights."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
from pathlib import Path

from src.runtime.ship_delivery_sitl import _read_jsonl
from src.runtime.ship_onboard_comparison import compare_onboard_runs


LABELS = {
    "short_clear": "すぐに通過する障害物",
    "long_block": "長く通路を塞ぐ障害物",
    "brake_stop": "途中で減速して止まる障害物",
}
ACTIONS = {"wait": "待機", "detour": "迂回"}


def build_report(directories, destination, attempt_directories=None):
    comparison = compare_onboard_runs(directories)
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    attempts = []
    for directory in attempt_directories or directories:
        source = Path(directory)
        result_bytes = (source / "result.json").read_bytes()
        result = json.loads(result_bytes)
        config = result["config"]
        attempts.append(
            {
                "directory_name": source.name,
                "run_id": config["run_id"],
                "recorded_status": result["status"],
                "case": config["urban"]["case"],
                "policy": config["urban"]["policy"],
                "included_in_completed_flight_comparison": config["run_id"]
                in {r["run_id"] for r in comparison["runs"]},
                "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                "worker_failures": [
                    e["reason"]
                    for e in _read_jsonl(source / "events.jsonl")
                    if e["event"] == "worker_failed"
                ],
            }
        )
    attempt_ids = [a["run_id"] for a in attempts]
    if len(set(attempt_ids)) != len(attempt_ids) or not {
        r["run_id"] for r in comparison["runs"]
    }.issubset(attempt_ids):
        raise ValueError("Attempt denominator must include every compared run exactly once")
    (root / "attempts.json").write_text(json.dumps(attempts, ensure_ascii=False, indent=2))
    total = len(attempts)
    replay, manifest = [], []
    for directory in directories:
        source = Path(directory)
        result = json.loads((source / "result.json").read_text())
        config = result["config"]
        events = _read_jsonl(source / "events.jsonl")
        entry = next(e["elapsed_s"] for e in events if e["event"] == "urban_entry_observed")
        records = _read_jsonl(source / "telemetry.jsonl")
        points = [
            {
                "t": r["elapsed_s"] - entry,
                "aircraft": r["vehicle"]["xyz"],
                "obstacle": r["urban_obstacle"]["xyz"],
                "phase": r["phase"],
            }
            for r in records
            if r["elapsed_s"] >= entry and r.get("vehicle") and r.get("urban_obstacle")
        ]
        request = json.loads((source / "vision-request-1.json").read_text())
        decision = next(e for e in events if e["event"] == "urban_decision")
        images = [
            {
                "t": r["elapsed_s"] - entry,
                "image": "data:image/png;base64,"
                + base64.b64encode((source / r["onboard_frame"]["file"]).read_bytes()).decode(),
                "sha256": r["onboard_frame"]["sha256"],
            }
            for r in request["frames"]
        ]
        metrics = next(r for r in comparison["runs"] if r["run_id"] == config["run_id"])
        replay.append(
            {
                "run_id": config["run_id"],
                "case": config["urban"]["case"],
                "policy": config["urban"]["policy"],
                "points": points,
                "images": images,
                "buildings": config["urban"]["buildings"],
                "coast": result["scenario_parameters"]["offshore_distance_m"],
                "entry": config["urban"]["entry_north_m"],
                "goal": config["goal_north_m"],
                "decision_at": decision["elapsed_s"] - entry,
                "action": decision["decision"]["action"],
                "duration": points[-1]["t"],
                "metrics": metrics,
            }
        )
        manifest.append(
            {
                "run_id": config["run_id"],
                "directory_name": source.name,
                "artifacts_sha256": {
                    str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(source.rglob("*"))
                    if path.is_file() and not path.is_symlink()
                },
            }
        )
    (root / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2))
    (root / "evidence-manifest.json").write_text(json.dumps(manifest, indent=2))
    (root / "replay-data.json").write_text(json.dumps(replay, ensure_ascii=False))
    mean = comparison["mean_model_minus_rule_s"]
    direction = "長い" if mean >= 0 else "短い"
    statement = (
        f"モデル側の市街地所要時間は、通常ルールより平均 {abs(mean):.1f} 秒{direction}結果でした。"
    )
    table = []
    for p in comparison["pairs"]:
        table.append(
            f"| {LABELS[p['case']]} | {ACTIONS[p['rule_action']]} / {p['rule_urban_elapsed_s']:.1f}s | {ACTIONS[p['model_action']]} / {p['model_urban_elapsed_s']:.1f}s | {p['model_minus_rule_s']:+.1f}s | {p['inference_elapsed_s']:.1f}s |"
        )
    disagreement = sum(p["model_differs_from_rule_on_same_images"] for p in comparison["pairs"])
    markdown = f"""# 船から配送へ：機上映像による判断と飛行の比較

固定した比較バッチのPX4/Gazebo {total} 試行中6試行で、機上RGBからの判断、荷物の接地・安定、船への帰還・安定を検証しました。
以下の時間比較は完了した6試行に条件付けたものです。失敗した試行も[試行索引](attempts.json)と[開発履歴](DEVELOPMENT-ja.md)に残しています。
{statement} 各条件・手法1回の探索的比較であり、一般的な優劣や統計的有意差は示しません。

**Step 2全体は未完了です。** 今回実行したモデルはローカルGemma4の汎用VLMです。
飛行専用VLA、行動条件付きWAMを接続したとは数えていません。

## 実測結果

| 条件 | 通常ルール：選択 / 時間 | VLM予測：選択 / 時間 | VLM − ルール | 推論時間 |
|---|---:|---:|---:|---:|
{chr(10).join(table)}

時間は市街地入口のホールド確認から配送地点の低高度ホバー確認までの壁時計です。
この小型シナリオの判断地点は海岸の30m手前です。表の「市街地時間」には、この進入区間も含みます。
モデルの待ち時間を含み、荷物接地・帰還の完了確認は別に検証しています。
同じ保存画像で通常ルールを再計算したとき、モデルと選択が異なった条件は {disagreement}/3 件でした。
各フライト自体の画像列は別々であり、同一画像を再生して飛行させた比較ではありません。
減速停止の条件では、通常ルールの飛行は迂回でしたが、モデル側の保存画像で通常ルールを再計算すると待機になりました。
観測列の違いで選択が変わる例であり、この再計算した待機経路を実際に飛ばした結果はありません。

## 観測から実行まで

前向き640×360 RGB、PX4の位置・姿勢、既知の奥行きと測量済みのシアン色マーカーから、赤い障害物の位置を推定します。
カメラは飛行中に撮影し、モデルへの問い合わせは上記の進入ホールド中に行います。全区間の飛行制御はPX4が担います。
通常ルールは速度と減速度から選びます。Gemma4は同じ画像・画像由来の位置列から通路が空くまでの秒数を提案します。
ルールが承認済みの待機／迂回に限定し、PX4が経路を実行します。直進開始には2枚の新しい画像による通路の空き確認が必要です。
モデルの提案、ミッション受理、飛行、荷物接地、帰還は別々の証拠として記録しています。
今回のVLM予測ポリシーは構造化された予測秒数を使います。別に返された動作提案や自然文の説明と矛盾していても、文章を読み替えて選択を修正していません。その矛盾もモデルの失敗として保持します。

Gazeboの障害物座標は独立した検証にだけ使用します。画像のハッシュ、モデルの入出力・重みID、実行コード、ワールド、計画を照合して再検証しました。
比較対象の完了6試行では、接触イベントと観測点間の幾何学検査に違反はありませんでした。連続時間の衝突安全性を証明したという意味ではありません。

## 範囲と残件

1機、停止した船、沖合100m＋内陸200m、50gの模擬荷物です。海上1km、移動船、実機、10機協調は今回の検証対象外です。
既知色・既知奥行き・マーカーを使う合成環境であり、一般の市街地認識ではありません。
推論はホストMac上で実行しました。機体搭載計算機の性能を示すものではありません。

専用VLA/WAMの接続と同条件の比較が残っています。候補の[WorldVLN配布重み](https://huggingface.co/EmbodiedCity/WorldVLN/tree/main)は36.9GBで、確認時点のローカル空き容量約7GBを超えていました。
既存のANWM実装も調査しましたが、[配布チェックポイント](https://huggingface.co/EmbodiedCity/ANWM/tree/main)は18.1GBです。16フレームのRGB・深度・候補軌道とCUDA実行環境が必要で、今回の5フレーム単眼入力をそのまま渡すことはできません。
利用できるモデル実行先と入力契約を確定してから、この比較基盤に接続します。

## 保存物

- [対話型リプレイと機上映像](report.html)
- [試行の分母・開発中の失敗と修正](DEVELOPMENT-ja.md)
- [再検証した比較値](comparison.json)
- [証拠ファイルのハッシュ一覧](evidence-manifest.json)
- [今回の比較バッチの全試行](attempts.json)
- [表示用の実測軌跡・画像](replay-data.json)

再生は入口時刻で揃え、全観測点の間を線形補間しています。未来予測映像ではありません。
各試験のrun ID、world/code hash、モデルdigestは比較JSONに保存しています。
"""
    (root / "REPORT-ja.md").write_text(markdown)
    cells = "".join(
        f"<tr><th>{LABELS[p['case']]}</th><td>{ACTIONS[p['rule_action']]} · {p['rule_urban_elapsed_s']:.1f}s</td><td>{ACTIONS[p['model_action']]} · {p['model_urban_elapsed_s']:.1f}s</td><td>{p['model_minus_rule_s']:+.1f}s</td></tr>"
        for p in comparison["pairs"]
    )
    document = (
        HTML.replace("__STATEMENT__", html.escape(statement))
        .replace("__ATTEMPTS__", str(total))
        .replace("__TABLE__", cells)
        .replace("__DATA__", json.dumps(replay, ensure_ascii=False).replace("</", "<\\/"))
    )
    (root / "report.html").write_text(document)
    return {
        "report": str(root / "report.html"),
        "runs_verified": len(replay),
        "mean_model_minus_rule_s": mean,
    }


HTML = r"""<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>船から配送へ｜機上RGBと飛行の比較</title><link rel="icon" href="data:,"><style>
:root{color-scheme:light;--ink:#182d3d;--muted:#526779;--line:#d9e4e9;--blue:#156ba2;--orange:#c56b2b}*{box-sizing:border-box}body{margin:0;background:#f2f6f8;color:var(--ink);font:16px/1.75 -apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif}main{max-width:1140px;margin:auto;padding:42px 28px}h1{font-size:clamp(27px,4vw,43px);line-height:1.35;margin:14px 0}h2{font-size:22px;margin:0 0 16px}.eyebrow{font-size:13px;letter-spacing:.1em;color:var(--blue)}p{margin:10px 0}.lead{font-size:19px;max-width:850px}.muted,small{color:var(--muted)}section{background:#fff;border:1px solid var(--line);border-radius:16px;padding:24px;margin:25px 0}.badge{display:inline-block;border-radius:5px;padding:4px 9px;background:#e3f2ed;color:#285b4b;font-size:13px}.pending{background:#fff2d7;color:#81561c}.cards,.visuals{display:grid;grid-template-columns:1fr 1fr;gap:20px}.panel{min-width:0}canvas{display:block;width:100%;aspect-ratio:1/1;background:#f7fafb;border:1px solid var(--line);border-radius:8px}.controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:12px 0 20px}select,button{font:inherit;padding:7px 12px;border:1px solid #b8cbd5;border-radius:7px;background:white;color:var(--ink)}button{cursor:pointer}input[type=range]{flex:1;min-width:130px;accent-color:var(--blue)}output{font-variant-numeric:tabular-nums;min-width:68px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{text-align:left;padding:13px 10px;border-bottom:1px solid var(--line)}.scroll{overflow-x:auto}th{font-weight:600}.camera{width:100%;display:block;border-radius:8px;aspect-ratio:16/9;object-fit:contain;background:#eef2f4}.metric{color:var(--blue);font-size:14px;min-height:45px}.model .metric{color:var(--orange)}.label{font-size:17px;font-weight:650;margin-bottom:6px}.foot{font-size:13px;overflow-wrap:anywhere}a{color:var(--blue)}@media(max-width:720px){main{padding:24px 12px}.cards,.visuals{grid-template-columns:1fr}section{padding:16px}.lead{font-size:16px}td,th{padding:9px 7px}h2{font-size:20px}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto}}
</style><main><div class="eyebrow">MISSIONOS · PX4 / GAZEBO · RECORDED EVIDENCE</div>
<h1>機上映像で判断し、<br>荷物を届けて船に戻る。</h1><p class="lead">機上RGBに基づく待機・迂回から配送・帰還まで、__ATTEMPTS__試行中6試行で検証。完了した試行で、通常ルールとローカル視覚モデルを比較しました。</p>
<span class="badge">6 / __ATTEMPTS__ 配送・帰還を検証</span> <span class="badge pending">専用VLA / WAM 接続は未完了</span>
<section><h2>モデルを呼んだことと、役に立ったことを分ける</h2><p>__STATEMENT__</p><p class="muted">各条件・手法1回の探索的比較です。Gemma4は汎用VLMとして実行しました。専用の飛行VLAや行動条件付きWAMの実行結果には数えていません。</p><div class="scroll"><table><thead><tr><th>条件</th><th>通常ルール</th><th>VLM予測</th><th>時間差</th></tr></thead><tbody>__TABLE__</tbody></table></div><small>市街地入口のホールド確認 → 配送地点の低高度ホバー確認。壁時計。推論待ち時間を含む。</small><p class="foot">同じコードで実行した__ATTEMPTS__試行のうち、完了6試行の所要時間を比較しています。今回の失敗・修正前のバッチ・開発試行を含む<a href="DEVELOPMENT-ja.md">全試行の記録</a>も保持しています。</p></section>
<section><h2>シミュレーターでの飛行を並べて見る</h2><div class="controls"><label for="case">条件</label><select id="case"><option value="short_clear">すぐに通過</option><option value="long_block">長く塞ぐ</option><option value="brake_stop">減速して停止</option></select><button id="play" type="button">▶ 再生</button><input id="time" aria-label="入口からの経過時間" type="range" min="0" max="240" value="0" step="0.1"><output id="clock">0.0 s</output></div><div class="visuals"><div class="panel"><div class="label">通常ルール</div><div class="metric" id="metric0"></div><canvas id="map0" width="560" height="560" aria-label="通常ルールの飛行記録"></canvas></div><div class="panel model"><div class="label">VLMの予測を使う</div><div class="metric" id="metric1"></div><canvas id="map1" width="560" height="560" aria-label="VLM予測での飛行記録"></canvas></div></div><p class="foot">上から見た実測軌跡。再生は8倍速で、スライダーは記録内の経過秒です。入口の時刻で揃え、観測点間を補間しています。薄い線は記録全体で、未来予測ではありません。丸は機体、赤は各試験で観測した障害物。高さは数値で表示します。</p></section>
<section><h2>判断に使った機上カメラ画像</h2><div class="controls"><label for="frame">画像</label><input id="frame" type="range" min="0" max="4" step="1" value="4"><output id="frame-label">5 / 5</output></div><div class="cards"><div><img class="camera" id="camera0" alt="通常ルールが判断に使った機上RGB"><p class="foot" id="camera-info0"></p></div><div><img class="camera" id="camera1" alt="VLMが判断に使った機上RGB"><p class="foot" id="camera-info1"></p></div></div><p class="muted" id="reason"></p><small>赤い箱は既知の障害物、シアン色は測量済みの方位補正マーカーです。各フライトの画像列は別々です。同じ保存画像でのルール再計算は比較JSONに記録しています。</small></section>
<section><h2>この結果の範囲</h2><p>1機・停止船・沖合100m＋内陸200m・50gの模擬荷物。既知色と既知奥行き、マーカーを使う合成環境です。一般の市街地認識、海上1km、実機、10機協調を検証した結果ではありません。</p><p>モデルはホストMacで実行。提案は承認済みの待機／迂回に限定し、直進前には新しい画像で通路を再確認します。荷物接地と帰還は別のVerifierが検証します。</p><p>専用VLA/WAMの接続と比較が残っています。Step 2全体を完了とはしていません。</p><p><a href="REPORT-ja.md">詳しいレポート</a> · <a href="comparison.json">比較値</a> · <a href="evidence-manifest.json">証拠ハッシュ</a></p></section><p class="foot">完了6試行では、接触記録と観測点間の幾何学検査に違反なし。連続時間の衝突安全性の証明ではありません。</p></main>
<script id="data" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent);const el=id=>document.getElementById(id);let selected=[],playing=false,last=0;
const labels={wait:'待機',detour:'迂回'};
const spatial=data.flatMap(r=>[...r.points.map(p=>p.aircraft),...r.buildings.flatMap(b=>[[b.xyz[0]-b.size[0]/2,b.xyz[1]-b.size[1]/2],[b.xyz[0]+b.size[0]/2,b.xyz[1]+b.size[1]/2]])]);
const bounds={minX:Math.min(...spatial.map(p=>p[0]),-12)-25,maxX:Math.max(...spatial.map(p=>p[0]),12)+25,minY:Math.min(...spatial.map(p=>p[1]),-18)-15,maxY:Math.max(...spatial.map(p=>p[1]))+20};
function pointAt(run,t){const p=run.points;let n=p.findIndex(x=>x.t>=t);if(n<0)return p[p.length-1];if(!n)return p[0];const a=p[n-1],b=p[n],u=(t-a.t)/(b.t-a.t);return {t,aircraft:a.aircraft.map((v,i)=>v+(b.aircraft[i]-v)*u),obstacle:a.obstacle.map((v,i)=>v+(b.obstacle[i]-v)*u),phase:a.phase};}
function draw(run,index,t){const canvas=el('map'+index),c=canvas.getContext('2d'),W=560,H=560;const typeScale=W/(canvas.clientWidth||W);const scale=Math.min((W-76)/(bounds.maxX-bounds.minX),(H-65)/(bounds.maxY-bounds.minY));const sx=x=>W/2+(x-(bounds.minX+bounds.maxX)/2)*scale,sy=y=>H-30-(y-bounds.minY)*scale;const box=(x,y,w,h,color)=>{c.fillStyle=color;c.fillRect(sx(x-w/2),sy(y+h/2),sx(x+w/2)-sx(x-w/2),sy(y-h/2)-sy(y+h/2));};c.clearRect(0,0,W,H);c.fillStyle='#e5f1f8';c.fillRect(0,0,W,H);c.fillStyle='#edf1e8';c.fillRect(0,0,W,sy(run.coast));c.strokeStyle='#cbd9dd';c.lineWidth=1;for(let y=0;y<=350;y+=50){c.beginPath();c.moveTo(38,sy(y));c.lineTo(W-38,sy(y));c.stroke();c.fillStyle='#657a85';c.font=11*typeScale+'px sans-serif';c.fillText(y+'m',4,sy(y)+3);}run.buildings.forEach(b=>box(b.xyz[0],b.xyz[1],b.size[0],b.size[1],'#bcc7cc'));box(0,0,24,36,'#6b8291');box(0,run.goal,12,12,'#56aa88');c.font=12*typeScale+'px sans-serif';c.fillStyle='#253d4b';c.fillText('船',sx(0)+30,sy(0)+4);c.fillText('配送地点',sx(0)+20,sy(run.goal));c.fillText('入口',sx(0)-38,sy(run.entry));c.beginPath();c.moveTo(42,H-18);c.lineTo(42+50*scale,H-18);c.strokeStyle='#294352';c.lineWidth=2;c.stroke();c.fillText('50 m',45,H-25);
function track(points,color,width){if(!points.length)return;c.beginPath();points.forEach((p,i)=>{const xy=[sx(p.aircraft[0]),sy(p.aircraft[1])];i?c.lineTo(...xy):c.moveTo(...xy)});c.strokeStyle=color;c.lineWidth=width;c.stroke();}track(run.points,'#c6d7df',2);const current=pointAt(run,t),trail=run.points.filter(p=>p.t<=t);track([...trail,current],index?'#c56b2b':'#156ba2',3);box(current.obstacle[0],current.obstacle[1],16,8,'#c9533b');c.beginPath();c.arc(sx(current.aircraft[0]),sy(current.aircraft[1]),6,0,Math.PI*2);c.fillStyle=index?'#c56b2b':'#156ba2';c.fill();c.strokeStyle='#fff';c.lineWidth=2;c.stroke();c.fillStyle='#294352';c.font=13*typeScale+'px sans-serif';c.textAlign='right';c.fillText('高度 '+current.aircraft[2].toFixed(1)+'m',W-12*typeScale,25);c.textAlign='left';c.fillText(t>=run.duration?'帰還・安定を検証済み':t<run.decision_at?'観測・判断中':labels[run.action]+'を選択',40,25);}
function render(){const t=+el('time').value;el('clock').textContent=t.toFixed(1)+' s';selected.forEach((r,i)=>{draw(r,i,t);el('metric'+i).textContent=labels[r.action]+' · 市街地 '+r.metrics.urban_elapsed_s.toFixed(1)+' s · 推論 '+r.metrics.inference_elapsed_s.toFixed(1)+' s';});}
function cameras(){let n=+el('frame').value;el('frame-label').textContent=(n+1)+' / '+selected[0].images.length;selected.forEach((r,i)=>{const im=r.images[Math.min(n,r.images.length-1)];el('camera'+i).src=im.image;el('camera-info'+i).textContent='入口から '+im.t.toFixed(2)+' s · run '+r.run_id.slice(0,12);});const proposal=selected[1].metrics.model_proposal;el('reason').textContent=proposal?'モデルの動作提案: '+labels[proposal.action]+' ／ 構造化された空き時間予測: '+proposal.predicted_clearance_seconds+' s ／ 実際の選択: '+labels[selected[1].action]+'。説明（原文）: '+proposal.reason:'—';}
function change(){playing=false;el('play').textContent='▶ 再生';selected=['onboard_stopping','onboard_vlm_forecast'].map(p=>data.find(r=>r.case===el('case').value&&r.policy===p));el('time').max=Math.ceil(10*Math.max(...selected.map(r=>r.duration)))/10;el('time').value=0;el('frame').max=Math.max(...selected.map(r=>r.images.length))-1;el('frame').value=el('frame').max;render();cameras();}
el('case').addEventListener('change',change);el('time').addEventListener('input',render);el('frame').addEventListener('input',cameras);el('play').addEventListener('click',()=>{playing=!playing;last=performance.now();el('play').textContent=playing?'Ⅱ 一時停止':'▶ 再生';if(playing&&+el('time').value>=+el('time').max)el('time').value=0;});function tick(now){if(playing){const next=Math.min(+el('time').max,+el('time').value+(now-last)/1000*8);el('time').value=next;render();if(next>=+el('time').max){playing=false;el('play').textContent='▶ 再生';}}last=now;requestAnimationFrame(tick);}window.addEventListener('resize',render);change();requestAnimationFrame(tick);
</script></html>"""


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", action="append", required=True, type=Path)
    parser.add_argument("--attempt-dir", action="append", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build_report(args.run_dir, args.output_dir, args.attempt_dir), indent=2))
