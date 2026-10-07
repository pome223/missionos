"""Offline replay of integrated attitude and position, never flight animation inputs."""
from __future__ import annotations

import html
import json
from hashlib import sha256
from pathlib import Path


def _supervision_panel(study):
    sections = []
    for run in study.get("runs", []):
        log = run.get("supervision")
        if not isinstance(log, dict):
            continue
        response = log.get("response") or {}
        jev, llm = response.get("jev_invocation", {}), response.get("llm_invocation", {})
        rows = [
            ("観測", "放出指令の受付後も分離を観測できず、安全インターロックで保留" if log.get("request") else "判断点への到達なし"),
            ("Jev", f"経路: {response.get('route', '応答なし')} / {jev.get('status', '未呼出し')}"),
            ("DeepSeek", llm.get("status", "未呼出し")),
            ("Rules / 操作", f"許可された見送り操作 {len(log.get('commands', []))}回 / {log.get('status')}"),
            ("結果", f"最終シーケンサー: {log.get('final_sequencer_state')} / 衛星分離 {run['outcome']['payload_released_count']}基"),
        ]
        if log.get("observation_collection_allowed"):
            collection = log.get("collection") or {}
            following = log.get("followup_response") or {}
            rows += [("追加観測", f"最大1回 / {collection.get('status', '要求なし')} / 要求 T+{collection.get('issued_time_s')}s → 取得 T+{collection.get('completed_time_s')}s"),
                     ("取得後の再判断", f"経路: {following.get('route', '応答なし')} / Jev: {following.get('jev_invocation', {}).get('status', '未呼出し')} / DeepSeek: {following.get('llm_invocation', {}).get('status', '未呼出し')}"),
                     ("期限", "最初の依頼から75秒。追加観測で延長しません。機器再試行・誘導変更は許可しません。")]
        table = "".join("<tr><th>"+html.escape(label)+"</th><td>"+html.escape(str(value))+"</td></tr>" for label, value in rows)
        observations = "".join(
            "<tr><td>"+f"{row['time_s']:.2f}"+"</td><td>"+html.escape(row["sequencer_state"])+
            "</td><td>"+str(row["payload_released_count"])+"</td></tr>"
            for row in log.get("observations", [])
        )
        sections.append('<section class="content" id="flight-supervision"><h2>MISSIONOS / 飛行中の監督</h2>'
            '<p class="notice">合成した放出不成立に対し、承認済みの保留・見送りだけを扱います。'
            'モデル待機中も6自由度を積分。指令受付と後続観測を分けて保存しています。'
            '検証結果は <a href="verification.json">verification.json</a> に記録します。</p>'
            '<table>'+table+'</table><details><summary>監督に使った観測時刻と操作後の状態</summary>'
            '<table><thead><tr><th>T+ (s)</th><th>シーケンサー</th><th>分離数</th></tr></thead><tbody>'+
            observations+'</tbody></table></details><p class="notice">保留は既存インターロックでも維持されます。'
            'この結果からAIによる救済、実機の安全性、ミッション成功は主張しません。</p></section>')
    return "".join(sections)


def _retained_return_panel(study):
    sections = []
    for run in study.get("runs", []):
        record = run.get("retained_return", {})
        if record.get("policy_id") not in ("mass_state_terminal_v1", "mass_state_terminal_v2", "mass_state_terminal_v3"):
            continue
        activation, trigger = record.get("activation") or {}, record.get("trigger") or {}
        budget, state = trigger.get("budget", {}), trigger.get("state", {})
        outcome = run.get("outcome", {})
        contact = outcome.get("contact_receipt") or {}
        def quantity(value, unit, digits=2):
            return f"{value:,.{digits}f} {unit}" if isinstance(value, (float, int)) else "未観測"
        rows = [
            ("承認された方針", f"{record['policy_id']} / {record.get('status')}"),
            ("帰還時の保持衛星", f"{activation.get('payload_retained_count', '未観測')}基 / "+quantity(activation.get("payload_retained_mass_kg"), "kg", 0)),
            ("帰還方針の有効化", "T+ "+quantity(activation.get("time_s"), "s")),
            ("反転開始", quantity(state.get("altitude_m"), "m")+" / T+ "+quantity(trigger.get("time_s"), "s")),
            ("準備時間の見積り", quantity(budget.get("preparation_time_s"), "s")),
            ("必要高度の見積り", quantity(budget.get("required_altitude_m"), "m")),
            ("実行結果", outcome.get("termination", "未観測")),
            ("接触点の実測速度", quantity(contact.get("surface_relative_speed_mps"), "m/s", 3)),
            ("接触時の残燃料", quantity(contact.get("propellant_kg"), "kg", 0)),
            ("姿勢目標の参照", {"mass_state_terminal_v1": "従来の地理方向参照",
                             "mass_state_terminal_v2": "全帰還区間の平行移動参照・26基条件で悪化した不採用候補",
                             "mass_state_terminal_v3": "参照方向が特異になる区間だけ連続化し、角速度を制限して地理方向に復帰"}[record["policy_id"]]),
        ]
        table = "".join("<tr><th>"+html.escape(label)+"</th><td>"+html.escape(str(value))+"</td></tr>" for label, value in rows)
        sections.append('<section class="content" id="retained-return"><h2>MISSIONOS / 衛星を保持した帰還</h2>'
            '<p class="notice">事前承認した固定の方針が、保持質量・慣性・姿勢・降下速度・使用可能な推力から反転の準備距離を算出します。'
            'JevやLLMが点火高度・姿勢・推力を指定することはありません。帰還時刻・軌道離脱目標・機体係数は従来と同じです。</p>'
            '<table>'+table+'</table><details><summary>反転開始前の見積りと条件</summary><pre>'+
            html.escape(json.dumps(budget, ensure_ascii=False, indent=2))+'</pre></details>'
            '<p class="notice">見積りは姿勢変更中の空力や推力を省略した近似です。達成できる姿勢変更や燃料余裕を保証しません。'
            '結果は実際に積分した接触速度で判定します。5 m/s以下でも水面・構造・搭載物の生存は未検証です。'
            'ブースターの回収、衛星の展開、ミッション全体の成功とは分けています。独立検証は '
            '<a href="verification.json">verification.json</a> を参照してください。</p></section>')
    return "".join(sections)


def _catch_panel(study):
    sections = []
    cases = [(run, False) for run in study.get("runs", [])]
    cases += [(run["booster_catch_run"], True) for run in study.get("runs", []) if run.get("booster_catch_run")]
    for run, continued in cases:
        record = run.get("catch_record")
        if not isinstance(record, dict):
            continue
        configuration, outcome = record.get("configuration", {}), run.get("outcome", {})
        settling, eligibility = record.get("settling", {}), record.get("eligibility", {})
        final = (record.get("frames") or [{}])[-1]
        rows = [
            ("試験条件", run.get("scenario", "未観測")),
            ("開始状態", "帰還で積分した状態の継続 / 接続の独立検証は verification.json" if continued else "終端状態から初期化 / 打ち上げからの接続なし"),
            ("Rules / キャッチ許可", "許可" if eligibility.get("catch_authorized") else "許可なし・代替経路"),
            ("接近制御 / 計算上限", f"{record.get('control_policy', 'fixed_v1')} / {record.get('requested_duration_s', '未記録')} s"),
            ("実行結果", outcome.get("termination", "未観測")),
            ("支持点の最大荷重", f"{record.get('peak_support_force_n', 0)/1e6:.3f} MN"),
            ("最大圧縮", f"{record.get('peak_compression_m', 0):.4f} m"),
            ("静定時間 / 必要時間", f"{settling.get('observed_s', 0):.3f} / {settling.get('required_s', 0):.3f} s"),
            ("最終エンジン推力", f"{final.get('engine_thrust_n', 0):.1f} N"),
            ("支持点高度 / アーム速度", f"{configuration.get('support_height_m', 0):.1f} m / {configuration.get('arm_speed_mps', 0):.2f} m/s"),
            ("過荷重 / ストローク超過", f"{record.get('support_overload')} / {record.get('support_stroke_exceeded')}"),
        ]
        table = "".join("<tr><th>"+html.escape(label)+"</th><td>"+html.escape(str(value))+"</td></tr>" for label, value in rows)
        start_notice = ('帰還の最終状態を変更せず、支持機構の積分へ受け渡した記録です。' if continued else
                        '終端状態から開始した独立試験です。打ち上げ・帰還からキャッチまでを通した成功ではありません。')
        sections.append('<section class="content" id="booster-catch"><h2>MISSIONOS / ブースター終端キャッチ試験</h2>'
            '<p class="notice"><b>'+start_notice+'</b>'
            '二つの支持点、片側接触、弾性・減衰荷重、摩擦、有限のエンジン停止応答を積分しています。'
            'アーム位置と支持点は記録値から描画し、機体を捕獲位置に固定する演出は行いません。</p>'
            '<table>'+table+'</table><p class="notice">支持機構の寸法・剛性・許容荷重は未同定の仮定です。'
            'タワーとアーム全体の変形、機体全体との連続衝突判定、実機のキャッチ安全性は未検証です。'
            '支持点の色は記録された接触荷重を表し、成功判定ではありません。姿勢・アームは支持フレーム、'
            'エンジンとフィンの形状は直近の保存済み作動状態を表示します。'
            '独立した検証結果は <a href="verification.json">verification.json</a> を参照してください。</p></section>')
    return "".join(sections)


def _recovery_panel(study):
    sections = []
    for run in study.get("runs", []):
        record = run.get("booster_recovery")
        if not isinstance(record, dict):
            continue
        booster = run.get("booster_run") or {}
        guidance = booster.get("recovery_record", {})
        planning = guidance.get("capture_planning")
        outcome = booster.get("outcome", {})
        contact = booster.get("contact") or {}
        def quantity(value, unit):
            return f"{value:,.3f} {unit}" if isinstance(value, (float, int)) else "未観測"
        rows = [
            ("帰還方針", record.get("policy_id")),
            ("分離状態の引継ぎ", "保存された分離状態から継続" if record.get("separation_state_preserved") else "分離未到達"),
            ("帰還の終了", outcome.get("termination", "未実行")),
            ("タワーまでの水平距離", quantity(outcome.get("return_site_distance_m"), "m")),
            ("接触点の速度", quantity(contact.get("surface_relative_speed_mps"), "m/s")),
            ("キャッチ進入条件", "到達" if record.get("handoff_reached") else "未達"),
            ("接触機構の実行", "実行" if record.get("catch_invoked") else "未実行"),
            ("連続支持の計算結果", "支持あり・独立検証を参照" if record.get("launch_connected_catch_supported") else "未達"),
        ]
        if isinstance(planning, dict):
            count = planning.get("admissible_prediction_count", 0)
            total = guidance.get("full_coast_prediction_count", 0)
            rows.insert(2, ("予測で到達条件と接触用燃料を満たした計画", f"{count} / {total} 件（予測の物理計算の独立再実行は未実施）"))
            rows.insert(3, ("位置を狙う継続試行", "成立計画を得られず継続" if planning.get("best_effort_cutoff_used") else "未使用"))
        table = "".join("<tr><th>"+html.escape(label)+"</th><td>"+html.escape(str(value))+"</td></tr>" for label, value in rows)
        assumptions = {"guidance_configuration": guidance.get("guidance_configuration"),
                       "capture_planning": planning,
                       "boostback_prediction": guidance.get("boostback_plan"),
                       "limitations": booster.get("limitations", [])}
        sections.append('<section class="content" id="booster-recovery"><h2>MISSIONOS / 打ち上げからブースター帰還・キャッチへ</h2>'
            '<p class="notice">同じ打ち上げで積分した段分離状態から、予測を使う固定の帰還誘導へ進みます。'
            'タワーへの実際の到達条件を満たした場合だけ、位置・速度・姿勢・角速度・燃料・作動機構をそのまま接触計算へ渡します。</p>'
            '<table>'+table+'</table><details><summary>誘導の仮定と予測値（実行結果とは別）</summary><pre>'+
            html.escape(json.dumps(assumptions, ensure_ascii=False, indent=2))+'</pre></details>'
            '<p class="notice">この帰還方針は公開された一般的な誘導手法を参考にした開発用実装です。'
            'SpaceXの非公開制御則や機体係数の再現ではありません。記録の整合性、到達、支持の成立は別々に判定します。'
            'LLM/Jevは操舵・点火・捕捉を指令しません。独立検証は <a href="verification.json">verification.json</a> を参照してください。</p></section>')
    return "".join(sections)


def build_report(study: dict) -> str:
    assets = Path(__file__).parent / "assets"
    # Full evidence stays in study.json; inline only replay fields to keep the
    # offline view responsive. Every draw value is selected from saved states.
    sample_fields = ("time_s", "phase", "body_id", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "propellant_kg",
                     "altitude_m", "ground_speed_mps", "com_z_m", "applied_thrust_n", "engine_count", "throttle", "contact",
                     "engine_states", "flap_angles_rad", "aero_panel_names", "main_engine_anchors", "main_engine_thrust_n")
    display_runs = []
    def display(run, name):
        item = {"scenario": name, "outcome": run["outcome"],
                             "launch_continuation": run.get("launch_continuation", False),
                             "samples": [{k: s[k] for k in sample_fields if k in s} for s in run["samples"]],
                             "events": [{k: e[k] for k in ("time_s", "event", "detail") if k in e} for e in run["events"]]}
        if isinstance(run.get("catch_record"), dict):
            record = run["catch_record"]
            item["catch_record"] = {key: record[key] for key in ("configuration", "initialization", "eligibility", "settling", "missing_support") if key in record}
            fields = ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "propellant_kg", "com_body_m",
                      "arm_half_span_m", "arm_rate_mps", "engine_thrust_n", "pins", "settle_elapsed_s", "settle_eligible")
            item["catch_record"]["frames"] = [{key: frame[key] for key in fields if key in frame} for frame in record.get("frames", [])]
        display_runs.append(item)
    for run in study["runs"]:
        body = run.get("body_id", (run.get("samples") or [{}])[0].get("body_id", "ship"))
        display(run, run["scenario"]+" / "+body.upper())
        if run.get("booster_run"):
            booster = run["booster_run"]
            caught = run.get("booster_catch_run")
            if caught:
                booster = {**booster, "samples": booster["samples"]+caught["samples"],
                           "events": booster["events"]+caught["events"],
                           "catch_record": caught["catch_record"], "launch_continuation": True,
                           "outcome": {**caught["outcome"], "max_altitude_m": booster["outcome"]["max_altitude_m"]}}
            if run.get("booster_recovery"):
                separation_time = booster["samples"][0]["time_s"]
                # Before separation the saved stack reference drives the CG;
                # afterward the independently integrated booster reference does.
                stack = [s for s in run["samples"] if s["phase"] == "stack_ascent" and s["time_s"] <= separation_time]
                booster = {**booster, "samples": stack+booster["samples"], "launch_continuation": True,
                           "events": [e for e in run["events"] if e["time_s"] <= separation_time]+booster["events"]}
            display(booster, run["scenario"]+(" / STACK → BOOSTER" if run.get("booster_recovery") else " / BOOSTER"))
        for satellite in run.get("satellites", []):
            samples = satellite["samples"]
            display({"samples": samples, "events": [{"time_s": samples[0]["time_s"], "event": "rigid_body_release", "detail": "independent orbital propagation; no service commissioning"}],
                     "outcome": {"termination": "orbital_propagation_horizon", "max_altitude_m": max(s["altitude_m"] for s in samples),
                                 "final_ground_speed_mps": samples[-1]["ground_speed_mps"], "final_orbit": satellite["final_orbit"],
                                 "starlink_service_verified": False}}, run["scenario"]+" / "+satellite["id"])
    view = {k: study[k] for k in ("title", "profile", "coverage", "checks", "provenance") if k in study}
    view["runs"] = display_runs
    view["default_run_index"] = next((i for i, item in enumerate(display_runs) if item.get("launch_continuation")), 0)
    view["checks"] = [dict(check) for check in study.get("checks", [])]
    for check in view["checks"]:
        if check["name"] == "nesc_case02_rotational_rates_only":
            check["scope"] = "NASA NESC 2015 Case 02の公開角速度301点。回転部分のみを照合。全ケースの認定ではありません。"
    asset_names = ("starship_cg_models.js", "starship_cg_renderer.js", "starship_sixdof_replay.js")
    source_texts = {name: (assets/name).read_text() for name in asset_names}
    view["provenance"] = {**study.get("provenance", {}), "replay_render_source_sha256": {
        **{name: sha256(text.encode()).hexdigest() for name, text in source_texts.items()},
        "starship_sixdof_report.py": sha256(Path(__file__).read_bytes()).hexdigest()}}
    payload = json.dumps(view, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")
    scripts = "\n".join(source_texts.values())
    title = html.escape(study.get("title", "STARSHIP / SIX DEGREES OF FREEDOM"))
    return """<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Starship · 6DOF evidence</title><style>
:root{color-scheme:dark;font-family:Arial,'Hiragino Kaku Gothic ProN',sans-serif;background:#080b10;color:#e7edf4}*{box-sizing:border-box}body{margin:0}header{padding:24px 4vw;display:flex;justify-content:space-between;gap:15px;align-items:center;border-bottom:1px solid #26303c}h1{font-size:18px;letter-spacing:.18em;margin:0}header span{font-size:11px;letter-spacing:.13em;color:#8aa1b7}.hero{position:relative;height:65vh;min-height:450px;background:#02050a}canvas{display:block;width:100%;height:100%;touch-action:none}.ribbon{position:absolute;top:22px;left:4vw;font-size:11px;letter-spacing:.15em;color:#c5d6e7}.phase{font-size:18px;letter-spacing:.12em;margin-top:10px}.hud{position:absolute;bottom:24px;left:4vw;right:4vw;display:flex;gap:28px;align-items:flex-end;pointer-events:none}.gauge{border:1px solid #708294;border-radius:50%;height:96px;width:96px;text-align:center;display:flex;flex-direction:column;justify-content:center;background:#02060955}.gauge label,.clock label{font-size:9px;letter-spacing:.13em;color:#a6b5c5}.gauge strong{font-size:24px;font-weight:400;margin:4px}.clock{margin:auto;text-align:center}.clock strong{display:block;font:30px monospace;margin:5px}.rates{font:12px/1.9 monospace;background:#02060977;padding:8px}.controls{padding:17px 4vw;border-bottom:1px solid #26303c;display:flex;gap:12px;flex-wrap:wrap;align-items:center}input[type=range]{flex:1;min-width:180px;accent-color:#ccd9e4}button,select{background:#121b27;border:1px solid #364759;border-radius:4px;padding:8px;color:#e4edf7;font:12px inherit}.content{max-width:1240px;margin:auto;padding:28px 4vw 60px}.notice{line-height:1.8;color:#b7c6d5;font-size:13px;border-left:2px solid #ddad65;padding-left:16px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:24px 0}.card{border:1px solid #293748;padding:18px;line-height:1.7}.card label{color:#8fa9c1;font-size:11px}.card strong{display:block;font-size:19px;font-weight:400}.card small{color:#9daec0}h2{font-size:15px;font-weight:400;letter-spacing:.07em;margin-top:32px}table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:11px 8px;border-bottom:1px solid #253140;vertical-align:top;line-height:1.6}th{color:#8fa9c1;font-weight:400}td.good{color:#83d9b2}td.warn{color:#e6b56a}.scroll{overflow:auto}a{color:#a6c8e9}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;line-height:1.7;color:#abc0d3}details{margin-top:20px}summary{cursor:pointer;font-size:13px}.error{padding:24px;color:#ffac90}
.hero:after{content:"";position:absolute;left:0;right:0;bottom:0;height:145px;background:linear-gradient(transparent,rgba(1,4,9,.94));pointer-events:none}.hud{z-index:1}.clock{text-shadow:0 1px 4px #000}
@media(max-width:600px){header{padding:18px}h1{font-size:13px;letter-spacing:.08em}header span{font-size:9px}.hero{height:58vh;min-height:420px}.hero:after{height:235px}.hud{gap:10px;bottom:20px;flex-wrap:wrap}.gauge{width:70px;height:70px}.gauge strong{font-size:19px}.clock{margin-left:auto;margin-right:0}.clock strong{font-size:23px}.rates{width:100%;display:flex;gap:14px;font-size:10px}.cards{grid-template-columns:1fr}.content{padding:22px 18px}.controls{padding:14px 18px}.controls input{flex-basis:100%}td{min-width:120px}}
</style><header><h1>""" + title + """</h1><span>MISSIONOS / DEVELOPMENT</span></header>
<div class="hero"><canvas id="scene" aria-label="Recorded six degree of freedom simulation replay"></canvas><div class="ribbon"><span id="scope">INTEGRATED POSITION + QUATERNION</span><div class="phase" id="phase">LOADING</div></div><div class="hud"><div class="gauge"><label>SPEED</label><strong id="speed">—</strong><label>KM/H</label></div><div class="gauge"><label>ALTITUDE</label><strong id="altitude">—</strong><label>KM</label></div><div class="clock"><strong id="clock">T+00:00</strong><label>6DOF SIMULATION · NOT FLIGHT TELEMETRY</label></div><div class="rates"><div id="rates">BODY ω</div><div id="fuel">PROPELLANT</div></div></div></div>
<div class="controls"><button id="play">▶ 再生</button><select id="run" aria-label="Simulation case"></select><select id="camera" aria-label="Camera"><option value="chase">CHASE</option><option value="orbit">ORBIT</option><option value="onboard">ONBOARD</option><option value="ground">GROUND</option></select><select id="rate" aria-label="Playback speed"><option value="1">×1</option><option value="10">×10</option><option value="60" selected>×60</option><option value="300">×300</option></select><input id="time" aria-label="Replay time" type="range" min="0" max="1" step="any" value="0"></div>
<main class="content"><p class="notice">位置・速度・クォータニオン・機体角速度を同時に積分しています。CGは記録した姿勢を補間して表示します。機体の寸法・色・カメラ・プルームは表示用です。<b>SpaceX実機の精度を検証したシミュレーターではありません。</b></p><div class="cards" id="cards"></div><h2>検証の範囲</h2><div class="scroll"><table><thead><tr><th>検査</th><th>結果</th><th>意味 / 限界</th></tr></thead><tbody id="checks"></tbody></table></div><h2>搭載したモデルと残る課題</h2><div class="scroll"><table><thead><tr><th>要素</th><th>この実装</th><th>扱い</th></tr></thead><tbody id="coverage"></tbody></table></div><h2>実行イベント</h2><div class="scroll"><table><thead><tr><th>T+ (s)</th><th>イベント</th><th>観測された状態</th></tr></thead><tbody id="events"></tbody></table></div><details><summary>実行条件・結果・未同定パラメーター</summary><pre id="details"></pre></details><p class="notice">数値解の検証、採用モデルの妥当性、SpaceX実機との一致度は別の評価です。軌道到達や着水の結果を成功に補正せず、未達・時間切れも記録します。</p></main>
""" + _supervision_panel(study) + _retained_return_panel(study) + _recovery_panel(study) + _catch_panel(study) + """
<script id="evidence" type="application/json">""" + payload + """</script><script>""" + scripts + """</script></html>"""
