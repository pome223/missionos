#!/usr/bin/env python3
"""Export reviewed native forecast evidence; exclude weights and private cloud logs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
from PIL import Image

try:
    from scripts.yokohama_pad_focus_data import sha, write
except ModuleNotFoundError:
    from yokohama_pad_focus_data import sha, write


def export(root, dest):
    dest.mkdir(parents=True, exist_ok=False)
    shutil.copytree(root / "prepared", dest / "prepared")
    for name in [
        "focus-protocol.json",
        "evaluation.json",
        "budget-authorization.json",
        "overlap.json",
        "source-maintenance.json",
        "qualitative-review.json",
        "adoption.json",
    ]:
        shutil.copy2(root / name, dest / name)
    shutil.copy2(root / "comparison.png", dest / "comparison.png")
    shutil.copy2(root / "controller-completion.json", dest / "cost.json")
    shutil.copytree(
        root / "remote/results", dest / "results", ignore=shutil.ignore_patterns("*.pt")
    )
    (dest / "runtime").mkdir()
    for name in ["gpu.csv", "gpu-processes-after.csv", "checkpoint-download.json"]:
        shutil.copy2(root / "remote" / name, dest / "runtime" / name)
    executed = dest / "executed-source"
    executed.mkdir()
    for p in (root / "outbound").glob("*.py"):
        shutil.copy2(p, executed / p.name)
    capture = dest / "capture"
    capture.mkdir()
    receipt = json.loads((root / "capture/capture-result.json").read_text())
    for entry in receipt["cases"]:
        shutil.copy2(
            root / "capture" / entry["id"] / "capture.json", capture / (entry["id"] + ".json")
        )
    shutil.copy2(root / "capture/capture-result.json", capture / "capture-result.json")
    shutil.copy2(root / "capture/cleanup.json", capture / "cleanup.json")
    shutil.copy2(root / "capture/capture_yokohama_pad_motion.py", capture / "executed_capture.py")
    data = json.loads((dest / "prepared/dataset/dataset.json").read_text())
    (dest / "images").mkdir()
    for s in data["samples"]:
        if s["split"] == "test":
            with np.load(dest / "prepared/dataset" / s["history"], allow_pickle=False) as a:
                Image.fromarray(a["rgb"][-1]).save(dest / "images" / (s["id"] + ".png"))
    (dest / "videos").mkdir()
    for entry in receipt["cases"]:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-framerate",
                "4",
                "-i",
                str(root / "capture" / entry["id"] / "%04d-rgb.png"),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "22",
                "-movflags",
                "+faststart",
                str(dest / "videos" / (entry["id"] + ".mp4")),
            ],
            check=True,
        )
    evaluation = json.loads((dest / "evaluation.json").read_text())
    cost = json.loads((dest / "cost.json").read_text())
    model = json.loads((dest / "results/summary.json").read_text())
    overlap = json.loads((dest / "overlap.json").read_text())
    counts = evaluation["counts"]
    latency = evaluation["latency_reference"].get(
        "after", evaluation["latency_reference"]["before"]
    )
    labels = {"persistence": "現在画像を維持", "before": "実WAM・学習前", "after": "実WAM・学習後"}
    table = "\n".join(
        f"| {labels[k]} | {v['matches']}/{v['known_targets']} | {v['false_clear']} | {v['unknown']} | {v['useful_future_occupied']} |"
        for k, v in counts.items()
    )
    text = f"""# 実WAM追加学習：戻る先行機の予測は改善せず

**今回の追加学習は飛行へ採用しません。** 占有・空きの一致は13/26から12/26、判別不能は13件から14件になりました。今は空いていても4秒後に先行機が戻る条件は、学習前後とも判別不能です。90%の精度は求めていませんが、今回の変更で待機判断に役立つ改善は確認できませんでした。

実ANWMの入力を先行機・パッド付近へ切り出し、1秒先と4秒先を予測しました。これは学習済みCPU状態モデルではありません。**今回は固定カメラの収録映像による実WAMの検証で、VLA・自機飛行・配送・帰船は実行していません。**

[画像と記録映像](index.html) · [数値結果](evaluation.json) · [条件](focus-protocol.json)

一部の静止場面では先行機の形が残りました。一方、戻る場面や退出後の画像では、実観測にない強い模様が現れています。画像全体の平均誤差も8.24から14.81へ増えました。誤った空き判定が0件でも、判別不能や崩れた画像を含むため、安全な予測を得たとは扱いません。[画像の目視確認](qualitative-review.json)

![観測・学習前・学習後・実際の未来画像](comparison.png)

| 方法 | 占有／空きの一致 | 誤った空き | 判別不能 | 今は空きだが未来の占有を予測 |
|---|---:|---:|---:|---:|
{table}

新しい3つの動作時刻、14観測地点×2予測時間の28条件です。位置の境界にある2条件は正解が不明なため、一致数から除いています。重なる履歴と時間違いの予測を独立した飛行数とは扱いません。固定カメラ・機体・経路は学習と共通です。学習と画素単位で同じ16枚履歴は28条件中{evaluation["exact_training_history_overlap"]}条件あり、未知環境への汎化の証拠ではありません。未来画像が学習側の画像と完全一致する条件も28条件中{overlap["exact_future_pixel_overlap"]}条件あります。[重複確認](overlap.json)。

数値は開発用画像で作った簡易読取りによります。生成画像に対する万能な認識器ではありません。実際の未来画像でも、移行中の先行機を判別できない条件があるため、画像も併せて確認します。

## 実モデルで変更したこと

元の640×360画像の固定領域を224×224に変換し、過去16枚・4 Hzだけを入力します。入力範囲は学習済みの既知場面を見て先に固定しました。評価時のモデル入力には、占有の正解ラベル、先行機の将来位置、予定表を含めません。評価用の未来画像はGPUへ渡していません。学習用の未来画像と、簡易読取り器の学習ラベルは学習資材としてGPU側にも配置しています。

自機が保持する候補だけを予測します。固定カメラで移動量ゼロなので、最新の実観測の切り出しを同じ視点の条件画像に使っています。従来の全画面・複数視点の飛行サービスとは異なる実験用入力です。飛行サービスの検証条件や登録済み重みは変更していません。

以前の実ANWM追加学習重みから開始し、開発用3例で必要性を判定しました。追加学習の有無は評価用正解を見て決めていません。今回は{model.get("steps", 0):,}更新、学習用208組。対象は最後の2つのTransformerブロックと既存出力部分で、他の重みとVAEは固定です。各学習ターゲットにある動く部分の誤差と、潜在画像の復元誤差を重くしました。保存した重みを読み込み直してから評価しています。

切り出し、予測時間、条件画像、学習範囲・損失が変わっています。この組合せの検証であり、各変更の効果を独立に特定した実験ではありません。前回の失敗を成功へ書き換えていません。[前回の実WAM追加学習](../yokohama-pad-learning/REPORT-ja.md)

## 境界・費用

完璧な画質や90%の精度、理想的Rulesへの勝利は条件にしていません。一方、予測画像の改善だけで飛行に有効とは認定しません。未来の一点の占有予測は、その間の進入経路の安全を保証しません。学習後の温まった推論は中央値{latency["warm_wall_seconds_median"]:.2f}壁時計秒です。起動・転送は含みません。実時間と同速と仮定すると、28条件中{latency["endpoint_already_past_at_one_times_speed"]}条件で予測対象時刻が計算完了までに過ぎます。飛行中の実時計との対応は未検証で、そのまま実時間の進入判断には使えません。VLAの提案、MissionOSの判断、Rules、AP実行、配送結果は次の接続検証で分けて確認する必要があります。

映像は今回のCPU Gazeboの実記録を4 Hz・等速で再生しています。先行機は台本で動く物体で、2台目の自律PX4ではありません。自機飛行も電力測定もないため、模擬バッテリー表示を付けていません。

累計上限を$20から**$23**へ更新しました。今回の追加概算は**${cost["new_estimated_usd"]:.3f}**、累計概算は**${cost["cumulative_estimated_usd"]:.3f}**。請求確定額ではありません。VM・ディスクの削除とCPU収録コンテナの終了を確認しました。[費用・終了記録](cost.json)。時間単価は[公式料金表](https://cloud.google.com/products/compute/pricing/accelerator-optimized)を参照し、余裕を含む$1.80/hと転送分$0.40で見積もっています。

## E2E / Runtime Verification

CPU Gazeboで736枚を記録し、公開済みの学習用記録と分離しました。GPUでは固定したANWM基底モデル・以前の追加学習重みを実際に読み込み、推論・学習・保存後の再読み込みを実行しています。評価用正解はGPU外で照合しました。

```sh
python scripts/capture_yokohama_pad_motion.py --approve-sitl \\
  --source-rig "$RIG" --case-set native-focus-v1 --output-dir "$CAPTURE"
python train_yokohama_pad_focus.py --root "$STAGING" --execute-training
python scripts/check_yokohama_pad_focus.py --bundle docs/examples/yokohama-pad-native-focus
```

最後のコマンドは保存された推論出力・時刻・重み識別子・画像判定をCPUで再検査します。GPU推論の再実行ではありません。実行時ソースと公開用に整形した保守ソースを分け、原記録を保存しています。[証拠一覧](evidence-manifest.json)

原典：横浜市・Project PLATEAUの加工データ。[出典とライセンス](../yokohama-urban-scene/ATTRIBUTION.md)
"""
    (dest / "REPORT-ja.md").write_text(text)
    payload = json.dumps(evaluation, ensure_ascii=False).replace("</", "<\\/")
    (dest / "index.html").write_text(
        HTML.replace("__DATA__", payload).replace(
            "__COST__", f"${cost['cumulative_estimated_usd']:.2f} / $23"
        )
    )
    manifest(dest)


def manifest(dest):
    write(
        dest / "evidence-manifest.json",
        dict(
            schema="native_pad_focus_public_evidence.v1",
            files={
                str(p.relative_to(dest)): sha(p)
                for p in sorted(dest.rglob("*"))
                if p.is_file() and p.name != "evidence-manifest.json"
            },
        ),
    )


HTML = """<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>先行機を残す実WAM追加学習</title><style>
:root{color-scheme:dark}body{margin:0;background:#10242c;color:#e6eff1;font:16px/1.7 system-ui,sans-serif}main{max-width:1150px;margin:auto;padding:28px}h1{font-size:27px;line-height:1.5}.note{background:#24414b;padding:16px;border-radius:12px}a{color:#95dcee}select,button{font:inherit;padding:8px;margin:12px 4px;background:#284954;color:#fff;border:1px solid #65838e;border-radius:8px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}figure{margin:0}img{width:100%;border-radius:8px;image-rendering:auto}figcaption{font-size:14px}video{max-width:640px;width:100%;display:block;border-radius:10px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:9px;border-bottom:1px solid #45616c;text-align:left}.small{color:#b0c4ca;font-size:13px}.source{margin:24px 0;max-width:640px}@media(max-width:700px){main{padding:16px}.grid{grid-template-columns:repeat(2,1fr)}h1{font-size:23px}}</style>
<main><p class="small">MissionOS / 横浜街区 / 実ANWM・GPU事後学習</p><h1>実WAM追加学習：戻る先行機の予測は改善せず</h1>
<p>一致は13/26 → 12/26。戻る場面は判別不能のままで、今回の重みは飛行へ採用しません。90%は求めず、判断に役立つ改善の有無で確認しています。</p>
<p class="note">固定カメラの収録映像から、実WAMが1秒先・4秒先を予測した記録です。<b>自機飛行・実VLA・配送・帰船は今回未実施です。</b> 学習済みCPU状態モデルの結果とは分けています。</p>
<p>GPU累計概算 <b>__COST__</b> · <a href="REPORT-ja.md">レポート</a> · <a href="evaluation.json">数値と全条件</a></p>
<table><thead><tr><th>方法</th><th>一致</th><th>誤った空き</th><th>不明</th><th>未来の占有を先読み</th></tr></thead><tbody id="counts"></tbody></table>
<p class="small">3場面・14観測×2時間。正解が境界で不明な2条件を一致数から除外。相関する条件で、独立した28飛行ではありません。</p>
<label for="case">比較する条件</label><select id="case"></select><button id="previous">前</button><button id="next">次</button><p id="status"></p>
<div class="grid"><figure><img id="current" alt="推論前の実観測の切り出し"><figcaption>推論前の実観測 / 入力領域</figcaption></figure><figure><img id="before" alt="実ANWMの学習前予測"><figcaption id="before-caption"></figcaption></figure><figure><img id="after" alt="実ANWMの学習後予測"><figcaption id="after-caption"></figcaption></figure><figure><img id="actual" alt="同じ未来時刻の実観測"><figcaption>指定した未来時刻の実観測</figcaption></figure></div>
<div class="source"><p>元の全画面（推論前）</p><img id="full" alt="切り出し前の全画面観測"></div>
<p>先行機の動き / 今回のGazeboカメラ記録・等速</p><video id="video" controls preload="metadata"></video>
<p class="small">予測は「自機が保持する」候補の未来一点です。区間全体の占有・衝突や、飛行中の推論遅延は検証していません。原典：横浜市・Project PLATEAU、加工。<a href="../yokohama-urban-scene/ATTRIBUTION.md">帰属</a></p></main>
<script>const data=__DATA__;const label={occupied:'占有',clear:'空き',unknown:'不明'};const names={persistence:'現在画像を維持',before:'実WAM 学習前',after:'実WAM 学習後'};
document.getElementById('counts').innerHTML=Object.entries(data.counts).map(([k,v])=>`<tr><td>${names[k]}</td><td>${v.matches}/${v.known_targets}</td><td>${v.false_clear}</td><td>${v.unknown}</td><td>${v.useful_future_occupied}</td></tr>`).join('');const select=document.getElementById('case');data.rows.forEach((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=r.id;select.append(o)});
function show(){const r=data.rows[+select.value],site=r.id.replace(/-t\\d+$/,'');document.getElementById('status').textContent=`${r.horizon_s}秒先：実際は${label[r.truth]} ／ 現在の位置記録は${label[r.current_pose_state]}`;document.getElementById('current').src=`images/${r.id}.png`;document.getElementById('actual').src=`prepared/targets/${r.id}.png`;document.getElementById('full').src=`prepared/observed/${site}.png`;for(const s of ['before','after']){document.getElementById(s).src=`results/${s}/${r.id}/prediction.png`;document.getElementById(s+'-caption').textContent=`${names[s]}：${label[r.methods[s]?.state]||'未実施'}`};const scene=r.id.split('-').slice(0,2).join('-');const v=document.getElementById('video');if(v.dataset.scene!==scene){v.src=`videos/${scene}.mp4`;v.dataset.scene=scene}}select.onchange=show;document.getElementById('previous').onclick=()=>{select.value=(+select.value+data.rows.length-1)%data.rows.length;show()};document.getElementById('next').onclick=()=>{select.value=(+select.value+1)%data.rows.length;show()};show();</script></html>"""


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    export(a.root, a.output)
