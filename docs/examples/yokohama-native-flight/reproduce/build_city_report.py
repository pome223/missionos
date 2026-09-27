"""Export reviewed city model/trajectory receipts; never invokes a model or simulator."""

import argparse
import base64
import hashlib
import json
import math
import shutil
import sys
from pathlib import Path


def read(p):
    return json.loads(p.read_text())


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    sys.path.insert(0, str(a.repo))
    from src.runtime.yokohama_scene import to_source

    r = a.run
    o = a.output
    o.mkdir(parents=True, exist_ok=True)
    (o / "images").mkdir(exist_ok=True)
    cfg = read(r / "config.json")
    result = read(r / "result.json")
    assert cfg.get("decisions", {}).get("backend") == "native", (
        "Native report requires an explicitly native attempt"
    )
    events = [json.loads(s) for s in (r / "flight-events.jsonl").read_text().splitlines()]
    rows = [json.loads(s) for s in (r / "flight-trajectory.jsonl").read_text().splitlines()]
    if not rows:
        raise ValueError(
            "A recorded flight is required; infrastructure failures have no trajectory"
        )
    start = rows[0]["sim_s"]
    trajectory = []
    for row in rows:
        t = round(row["sim_s"] - start, 6)
        assert math.isfinite(t) and all(math.isfinite(x) for x in row["vehicle"]["xyz"])
        if trajectory and t < trajectory[-1]["t"]:
            raise ValueError("Simulator clock reversed")
        if trajectory and t == trajectory[-1]["t"]:
            continue
        trajectory.append(
            {
                "t": t,
                "xyz": to_source(row["vehicle"]["xyz"], cfg["world"]["frame"]).round(6).tolist(),
                "phase": row["phase"],
                "nav_state": int(row["nav_state"]),
            }
        )
    holds = []
    for h in read(r / "hold-results.json") if (r / "hold-results.json").exists() else []:
        path = r / "images" / (h["point"] + "-onboard_rgb.png")
        if path.exists():
            shutil.copy2(path, o / "images" / path.name)
            holds.append(
                dict(
                    h,
                    image="data:image/png;base64," + base64.b64encode(path.read_bytes()).decode(),
                )
            )
    data = {
        "schema": "missionos.yokohama-city-model-replay.v1",
        "run_id": cfg["run_id"],
        "clock": "simulator seconds since first telemetry observation",
        "sample_reduction": "No decimation; duplicate stats ticks collapsed keeping first observation",
        "trajectory": trajectory,
        "points": cfg["world"]["points"],
        "holds": holds,
    }
    (o / "trajectory.json").write_text(
        json.dumps({k: v for k, v in data.items() if k != "holds"}, indent=2) + "\n"
    )
    count = sum(e["event"] == "city_segment_arrived" for e in events)
    native = bool(result.get("vla_invoked") and result.get("wam_invoked"))
    dv = (
        read(r / "decision-verification.json")
        if (r / "decision-verification.json").exists()
        else {"status": "not_run"}
    )
    fv = (
        read(r / "verification.json")
        if (r / "verification.json").exists()
        else {"status": "not_run"}
    )
    passed = dv.get("native_model_flight_verified") is True and fv.get("status") == "passed"
    title = "実VLA＋WAMで、短い移動を更新。" if passed else "市街地での実モデル試行。"
    badge = (
        ("実モデルの移動反映 " + str(count) + " / 2")
        if passed
        else ("検証未達 / 到達イベント " + str(count) + " / 2")
    )
    template = (a.repo / "docs/examples/yokohama-px4-sitl/reproduce/replay.html").read_text()
    template = (
        template.replace("横浜街区でのAP飛行 | MissionOS", "横浜街区でのモデル判断 | MissionOS")
        .replace("MISSIONOS / YOKOHAMA CPU SITL", "MISSIONOS / YOKOHAMA NATIVE MODEL TRIAL")
        .replace("街区を飛び、止まる。", title)
        .replace(
            "横浜の実都市形状で、PX4の移動・保持・帰還を観測。",
            "D1・D2で再観測し、モデル提案を確認してAPへ渡す試行。",
        )
        .replace("モデル推論なし / GPU追加費用 $0", badge)
    )
    template = template.replace(
        "<strong>3 / 3</strong><span>接触の正・負対照</span>",
        "<strong>" + str(count) + " / 2</strong><span>モデル区間の到達イベント</span>",
    )
    template = template.replace(
        "固定のAP経路で離陸、街区往復、保持、着陸。無風・静止建物のシミュレーションです。",
        "AP経路のD1・D2にモデル判断を挿入。無風・静止建物のシミュレーションです。経路全体をVLAが作る試験ではありません。",
    )
    template = template.replace(
        "実VLA＋WAM：未実施", "2回の判断反映を検証" if passed else "実モデル検証：未達"
    )
    template = template.replace(
        "次は一般の建物に対応する予測画像の読み取りと、市街地での観測更新へ接続します。海上区間・荷物投下は今回含みません。",
        "予測画像は既知の形状との整合性を確認。一般的な障害物認識・未観測領域の安全性は未検証です。海上区間・荷物投下・実機飛行は含みません。",
    )
    template = template.replace(
        'poster="images/01-D2-onboard_rgb.png"',
        'poster="images/'
        + (holds[min(1, len(holds) - 1)]["point"] if holds else "00-D1")
        + '-onboard_rgb.png"',
    )
    template = template.replace("今回確認した範囲", "試験の設定")
    template = template.replace(
        "<span>最大水平ずれ</span>", "<span>30秒測定内の最大水平ずれ</span>"
    )
    observed_label = (
        "実VLA："
        + ("実行" if result.get("vla_invoked") else "未実行")
        + " / 実WAM："
        + ("実行" if result.get("wam_invoked") else "未実行")
        + f"。モデル区間の到達イベント {count} / 2。"
    )
    template = template.replace(
        '<div class="note"><strong>試験の設定</strong>',
        '<div class="note"><strong>この記録の結果</strong><p>'
        + observed_label
        + "判定と中断理由は実測レポートに記録しています。</p></div>"
        + '<div class="note"><strong>試験の設定</strong>',
    )
    # Keep the 3D replay observed-data-only. Predictions appear separately below it.
    blocks = []
    cycles = []
    vla_calls = []
    for response_path in sorted((r / "decisions").glob("*/native-response.json")):
        response = read(response_path)
        if "vla_inference_invoked" not in response:
            continue
        vla_calls.append(
            {
                key: response.get(key)
                for key in [
                    "vla_inference_invoked",
                    "generated_text",
                    "generated_token_ids",
                    "request_sha256",
                    "input_images_sha256",
                    "elapsed_s",
                    "inference_s",
                    "cuda_allocated_after_request_bytes",
                ]
            }
        )
    for wp in sorted((r / "decisions").glob("*-request.json")):
        request = read(wp)
        if request["operation"] != "wam":
            continue
        folder = wp.with_name(wp.name.removesuffix("-request.json"))
        response_path = wp.with_name(wp.name.replace("-request", "-response"))
        response = (
            read(response_path)
            if response_path.exists()
            else {"error": "No host response preserved"}
        )
        panels = []
        cap = r / request["capture"]["file"]
        last = read(cap)["frames"][-1]
        source = cap.parent / last["assets"]["onboard_rgb_png"]["file"]
        dest = f"cycle-{request['cycle']}-observed.png"
        shutil.copy2(source, o / "images" / dest)
        panels.append(
            f'<figure><img src="images/{dest}" alt="推論前の実観測"><figcaption>推論前の実観測 / RGB</figcaption></figure>'
        )
        for kind in ["hold", "vla"]:
            image = folder / (kind + "-prediction.png")
            if image.exists():
                dest = f"cycle-{request['cycle']}-{kind}-prediction.png"
                shutil.copy2(image, o / "images" / dest)
                panels.append(
                    f'<figure><img src="images/{dest}" alt="WAMの予測画像"><figcaption>WAM予測 / {kind}</figcaption></figure>'
                )
        blocks.append(
            '<section class="model-cycle"><h2>判断 '
            + str(request["cycle"])
            + '：予測と実観測</h2><div class="pred-grid">'
            + "".join(panels)
            + '</div><p class="caption">予測は224×224。実観測・予測・再現用の3D表示は別の記録です。</p></section>'
        )
        cycles.append(
            {
                "cycle": request["cycle"],
                "request_sha256": sha(wp),
                "response_sha256": sha(response_path) if response_path.exists() else None,
                "assessment": response.get("value"),
                "error": response.get("error"),
            }
        )
    template = template.replace(
        "(100*Math.max(...DATA.holds.map(h=>h.max_horizontal_error_m))).toFixed(0)+' cm'",
        "(DATA.holds.length?(100*Math.max(...DATA.holds.map(h=>h.max_horizontal_error_m))).toFixed(0)+' cm':'未取得')",
    )
    template = template.replace(
        "function showImage(){const h=DATA.holds[Number(select.value)];",
        "function showImage(){if(!DATA.holds.length){select.disabled=true;document.getElementById('hold-image').hidden=true;document.getElementById('hold-caption').textContent='保持終了時の画像は未取得';return}const h=DATA.holds[Number(select.value)];",
    )
    template = template.replace(
        "</style>",
        ".model-cycle{padding:0 32px 24px}.pred-grid{display:grid;grid-template-columns:2fr 1fr 1fr;gap:18px}.pred-grid figure{margin:0;min-width:0}.pred-grid figcaption{font-size:12px;color:#b8cacd;margin-top:8px}@media(max-width:600px){.pred-grid{grid-template-columns:1fr}.model-cycle{padding:0 12px 20px}}</style>",
    )
    template = template.replace("</main><footer>", "</main>" + "".join(blocks) + "<footer>")
    scene = a.repo / "docs/examples/yokohama-urban-scene"
    template = (
        template.replace("__THREE__", (scene / "vendor/three.min.js").read_text())
        .replace("__DATA__", json.dumps(data, separators=(",", ":")))
        .replace("__SCENE__", (scene / "scene.json").read_text())
    )
    (o / "index.html").write_text(template)
    for source, name in [
        ("verification.json", "verification-flight.json"),
        ("decision-verification.json", "verification-decisions.json"),
    ]:
        if (r / source).exists():
            shutil.copy2(r / source, o / name)
    summary = {
        "run_id": cfg["run_id"],
        "status": "passed" if passed else "not_qualified",
        "model_updates_observed": count,
        "native_vla_invoked": result.get("vla_invoked", False),
        "native_wam_invoked": result.get("wam_invoked", False),
        "world_sha256": cfg["world"]["world_sha256"],
        "source_sha256": result.get("source_sha256", {}),
        "error": result.get("error") or result.get("observed", {}).get("reason"),
        "holds_completed": len(holds),
        "cycles": cycles,
        "vla_calls": vla_calls,
        "duration_sim_s": trajectory[-1]["t"],
        "duration_wall_s": rows[-1]["wall_s"] - rows[0]["wall_s"],
        "payload_delivery_verified": False,
        "generic_obstacle_recognition_verified": False,
        "energy_savings_verified": False,
    }
    (o / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    manifest = {
        "schema": "missionos.yokohama-native-evidence.v1",
        "run_id": cfg["run_id"],
        "raw_evidence_sha256": {
            str(p.relative_to(r)): sha(p)
            for p in sorted(r.rglob("*"))
            if p.is_file()
            and not p.is_symlink()
            and p.suffix in {".json", ".jsonl", ".npz", ".png", ".py"}
        },
        "source_scene_manifest_sha256": sha(scene / "files.sha256.json"),
    }
    (o / "evidence-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(o),
                "status": summary["status"],
                "updates": count,
                "native_vla_and_wam_invoked": native,
            }
        )
    )


if __name__ == "__main__":
    main()
