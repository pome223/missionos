"""Portable saved-state report; rendering is never flight or mission evidence."""
from __future__ import annotations

from copy import deepcopy
from bisect import bisect_left, bisect_right
from hashlib import sha256
from html import escape
import json
import math

from . import starship_booster_recovery_verifier as geometry
from .starship_sixdof_report import build_report


def _safe(value):
    if type(value) is float and not math.isfinite(value):
        return {"invalid_numeric": str(value)}
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    return value


def _json(value):
    return json.dumps(_safe(value), ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c")


def _numeric(value):
    return type(value) in (float, int) and math.isfinite(value)


def _quantity(value, digits=2):
    return f"{value:,.{digits}f}" if _numeric(value) else "未観測"


def _start(run):
    if isinstance(run.get("initial_state"), dict):
        return run["initial_state"]
    rows = run.get("samples") or []
    return {k: rows[0][k] for k in ("time_s", "r_eci_m", "v_eci_mps", "q_body_to_eci", "omega_body_rad_s", "propellant_kg")
            if k in rows[0]} if rows else None


def _points(run, profile):
    """Reconstruct CG ENU only from finite ordered saved samples."""
    rows, previous = [], None
    samples = run.get("samples")
    if not isinstance(samples, list) or not samples:
        return [], "保存履歴なし"
    for sample in samples:
        time = sample.get("time_s")
        if not _numeric(time) or previous is not None and time <= previous:
            return [], "保存時刻が非有限、重複、または逆順"
        for key, size in (("r_eci_m", 3), ("v_eci_mps", 3), ("q_body_to_eci", 4)):
            vector = sample.get(key)
            if not isinstance(vector, (list, tuple)) or len(vector) != size or not all(_numeric(x) for x in vector):
                return [], "表示に必要な位置・速度・姿勢が不正"
        if abs(sum(x*x for x in sample["q_body_to_eci"])-1.) > 1e-6:
            return [], "保存姿勢が単位クォータニオンではない"
        origin, axes = geometry._tower(profile, time)
        position = geometry._sub(sample["r_eci_m"], origin)
        velocity = geometry._sub(sample["v_eci_mps"], geometry._cross([0., 0., geometry._ROTATION], sample["r_eci_m"]))
        enu = [geometry._dot(position, axis) for axis in axes]
        speed = geometry._norm(velocity)
        if not all(_numeric(x) for x in [*enu, speed]):
            return [], "ENU変換が非有限"
        rows.append({"time_s": time, "position_enu_m": enu, "cg_speed_mps": speed,
                     "fuel_kg": sample.get("propellant_kg"), "phase": sample.get("phase", "未記録")})
        previous = time
    return rows, None


def _arrival(run, case):
    receipt = case.get("source_receipt") or {}
    result = (run.get("recovery_record") or {}).get("handoff", {}).get("observation")
    return result if isinstance(result, dict) else receipt.get("arrival", {})


def _record_binding(run, verdict, receipt):
    """Bind the caller's verdict to exact saved producer JSON, not display JSON."""
    try:
        raw = json.dumps(run, allow_nan=False, separators=(",", ":")).encode()
        actual = sha256(raw).hexdigest()
        expected = receipt.get("return_json_sha256")
        if type(expected) is not str or expected != actual:
            return actual, "保存runのreturn_json_sha256が欠落または不一致"
        linked = receipt.get("verification")
        if type(linked) is not dict or type(verdict) is not dict:
            return actual, "保存runに結び付いた検証結果が欠落"
        first = json.dumps(linked, allow_nan=False, sort_keys=True, separators=(",", ":"))
        second = json.dumps(verdict, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if first != second:
            return actual, "source_receipt.verificationと表示する検証結果が不一致"
        return actual, None
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None, "保存runまたは検証結果を有限JSONとしてhashへ結び付けられません"


def _protected_indices(rows, events):
    times = [row["time_s"] for row in rows]
    protected, bindings = {0, len(rows)-1}, []
    for index, event in enumerate(events):
        time = event.get("time_s")
        if not _numeric(time):
            continue
        after = bisect_left(times, time)
        options = [i for i in (after-1, after) if 0 <= i < len(rows)]
        chosen = min(options, key=lambda i: abs(times[i]-time))
        protected.add(chosen)
        bindings.append({"event_index": index, "event_time_s": time,
                         "saved_sample_time_s": times[chosen], "exact": times[chosen] == time})
    return protected, bindings


def _max_gap_indices(rows, interval):
    """Select saved rows only; retain the last row before each gap bound."""
    times, chosen, current = [row["time_s"] for row in rows], {0}, 0
    while current < len(rows)-1:
        candidate = bisect_right(times, times[current]+interval+1e-9)-1
        current = max(current+1, candidate)
        chosen.add(current)
    return chosen


def _reduce_cg(displayed, catch_start=None):
    rows, events = displayed["samples"], displayed.get("events", [])
    protected, bindings = _protected_indices(rows, events)
    chosen, last = set(protected), rows[0]["time_s"]
    for index, row in enumerate(rows[1:], 1):
        if row["time_s"]-last >= .5-1e-9:
            chosen.add(index)
            last = row["time_s"]
    if catch_start is not None:
        offset = bisect_left([row["time_s"] for row in rows], catch_start)
        if offset < len(rows):
            chosen.update(offset+i for i in _max_gap_indices(rows[offset:], .1))
    displayed["samples"] = [rows[i] for i in sorted(chosen)]
    receipt = {"cg_raw_sample_count": len(rows), "cg_display_sample_count": len(chosen),
               "cg_display_rule": {"normal_min_interval_s": .5, "catch_max_gap_s": .1,
                                   "protected": "first,last,nearest_or_exact_each_actual_event"},
               "cg_event_sample_bindings": bindings, "cg_downsampled": len(chosen) < len(rows),
               "cg_reduction_creates_new_states": False,
               "full_metrics_use_reduced_cg": False}
    record = displayed.get("catch_record") or {}
    frames = record.get("frames") or []
    if frames:
        frame_indices, frame_bindings = _protected_indices(frames, events)
        frame_indices.update(_max_gap_indices(frames, .1))
        record["frames"] = [frames[i] for i in sorted(frame_indices)]
        receipt.update(cg_raw_catch_frame_count=len(frames), cg_display_catch_frame_count=len(frame_indices),
                       cg_catch_event_frame_bindings=frame_bindings,
                       cg_display_catch_max_gap_s=max((b["time_s"]-a["time_s"] for a, b in
                                                     zip(record["frames"], record["frames"][1:])), default=0.))
    return receipt


def _pack_cg(displayed):
    """Lossless transport encoding; shared arrays remain the same saved values."""
    rows, shared, packed = displayed["samples"], {}, 0
    for key in ("main_engine_anchors", "main_engine_thrust_n", "aero_panel_names"):
        if key in rows[0] and all(row.get(key) == rows[0][key] for row in rows):
            shared[key] = rows[0][key]
            for row in rows:
                row.pop(key, None)
    fields = ("throttle", "gimbal_x_rad", "gimbal_y_rad", "available")
    for row in rows:
        engines = row.get("engine_states")
        if isinstance(engines, list) and engines and all(type(e) is dict and set(e) == set(fields) for e in engines):
            row["engine_states"] = [[e[key] for key in fields] for e in engines]
            packed += 1
    return shared, {"engine_states": list(fields), "engine_state_samples_packed": packed,
                    "shared_fields": list(shared), "numeric_values_rounded": False,
                    "new_physical_states_created": False}


def build_completion_report(cases, profile):
    """Build a standalone report from passed or failed persisted records.

    ``verification.passed`` establishes stored-record consistency only. Source
    receipts are displayed verbatim after safe JSON conversion; no verifier,
    integration, hosted model, hardware or network operation is invoked here.
    """
    if not isinstance(cases, list) or not cases:
        raise ValueError("completion_report_requires_cases")
    profile = deepcopy(profile)
    views, display_runs, rows, manifests, cg_shared = [], [], [], [], []
    valid_count, arrived_count, supported_count = 0, 0, 0
    start_hashes = []
    for index, case in enumerate(cases):
        run = case.get("run") or {}
        label = str(case.get("label", f"表示対象の記録 {index+1}"))
        verdict = case.get("verification") or {}
        receipt = case.get("source_receipt") or {}
        points, display_error = _points(run, profile)
        raw_hash, binding_error = _record_binding(run, verdict, receipt)
        display_error = display_error or binding_error
        passed = verdict.get("passed") is True and display_error is None
        handoff = passed and verdict.get("handoff_reached") is True
        support = handoff and verdict.get("catch_supported_after_handoff") is True
        valid_count += passed
        arrived_count += handoff
        supported_count += support
        signature = sha256(_json(_start(run)).encode()).hexdigest()
        start_hashes.append(signature)
        arrival = _arrival(run, case)
        pin = arrival.get("position_error_enu_m") or arrival.get("midpoint_enu_m")
        pin_miss = math.hypot(*pin[:2]) if isinstance(pin, (list, tuple)) and len(pin) >= 2 and all(_numeric(x) for x in pin[:2]) else None
        terminal_speed = points[-1]["cg_speed_mps"] if points else None
        final_fuel = (run.get("final_state") or {}).get("propellant_kg")
        if final_fuel is None and points:
            final_fuel = points[-1]["fuel_kg"]
        wall = case.get("wall_s", receipt.get("wall_s"))
        status = "整合性未成立・集計除外" if not passed else "到達・支持成立（仮定モデル）" if support else "表示対象の有効な未達記録" if not handoff else "到達・支持未成立"
        declared_launch = receipt.get("full_launch_reexecuted") is True
        full_launch = False  # Unsigned flags and an asserted verdict are not enclosing launch execution evidence.
        scope = "発射からの再実行は呼出側の宣言・未検証" if declared_launch else "保存分離からの開発継続"
        values = [label, status, scope, run.get("outcome", {}).get("termination", "未観測"),
                  _quantity(terminal_speed, 3), _quantity(pin_miss, 2),
                  _quantity(final_fuel/1000., 3) if _numeric(final_fuel) else "未観測",
                  "到達" if handoff else "未判定" if not passed else "未達",
                  "支持" if support else "未判定" if not passed else "未成立", _quantity(wall, 3)]
        rows.append('<tr class="'+("valid" if passed else "invalid")+'">'+
                    "".join("<td>"+escape(str(x))+"</td>" for x in values)+"</tr>")
        views.append({"label": label, "status": status, "valid": passed, "display_error": display_error,
                      "origin_time_s": points[0]["time_s"] if points else None, "points": points,
                      "outcome": run.get("outcome", {}), "handoff": handoff, "support": support,
                      "initial_state_sha256": signature, "full_launch_proof": full_launch,
                      "caller_declared_full_launch": declared_launch})
        manifests.append({"label": label, "source_receipt": receipt, "verification": verdict,
                          "initial_state_sha256": signature, "display_error": display_error,
                          "return_json_sha256_reconstructed": raw_hash, "record_binding_error": binding_error,
                          "display_sample_count": len(points), "display_downsampled": False,
                          "success_denominator_included": passed})
        if points:
            views[-1]["cg_run_index"] = len(display_runs)
            displayed = deepcopy(run)
            displayed["scenario"] = label
            displayed["launch_continuation"] = False
            displayed.setdefault("outcome", {}).setdefault("max_altitude_m", max(
                sample["altitude_m"] for sample in displayed["samples"] if _numeric(sample.get("altitude_m"))))
            displayed["outcome"]["final_ground_speed_mps"] = terminal_speed
            displayed["outcome"] = _safe(displayed["outcome"])
            displayed["outcome"].setdefault("termination", "未観測")
            catch = case.get("catch_run")
            catch_start = None
            if isinstance(catch, dict):
                exact = catch.get("initial_state") == run.get("final_state")
                if handoff and exact:
                    catch_start = catch.get("samples", [{}])[0].get("time_s")
                    catch_samples = [s for s in catch.get("samples", []) if s.get("time_s", -1) > displayed["samples"][-1]["time_s"]]
                    displayed["samples"] += deepcopy(catch_samples)
                    displayed["events"] += deepcopy(catch.get("events", []))
                    if isinstance(catch.get("catch_record"), dict):
                        displayed["catch_record"] = deepcopy(catch["catch_record"])
                    maximum_altitude = displayed["outcome"]["max_altitude_m"]
                    displayed["outcome"].update(_safe(catch.get("outcome", {})))
                    displayed["outcome"]["max_altitude_m"] = maximum_altitude
                    manifests[-1]["exact_catch_state_continuation_rendered"] = True
                else:
                    manifests[-1]["exact_catch_state_continuation_rendered"] = False
            manifests[-1].update(_reduce_cg(displayed, catch_start))
            views[-1]["cg_terminal_time_s"] = displayed["samples"][-1]["time_s"]
            shared, encoding = _pack_cg(displayed)
            cg_shared.append(shared)
            manifests[-1]["cg_lossless_encoding"] = encoding
            display_runs.append(displayed)
    same_start = len(set(start_hashes)) == 1
    denominator = str(valid_count)
    manifest = {"schema": "missionos.starship_completion_report_manifest.v1", "cases": manifests,
                "profile_sha256": sha256(_json(profile).encode()).hexdigest(),
                "valid_record_count": valid_count, "invalid_record_count": len(cases)-valid_count,
                "handoff_count": arrived_count, "support_count": supported_count,
                "same_exact_initial_state": same_start, "full_launch_verified_count": sum(c["full_launch_proof"] for c in views),
                "campaign_complete_enumeration_established": False,
                "full_launch_flags_are_caller_declarations_only": True,
                "two_dimensional_replay_interpolated": False,
                "cg_interpolation_creates_physical_evidence": False,
                "source_conditions_frozen_across_cases": False,
                "physical_execution": False, "mission_completed": False,
                "model_advantage_established": False}
    dataset = {"cases": views, "same_exact_initial_state": same_start, "manifest": manifest}
    heading = "同じ保存分離状態からの表示対象の記録" if same_start else "開始状態が異なる表示対象の記録（同条件比較ではありません）"
    header_names = ("記録", "判定", "実行区間", "終了理由", "終端CG速度 m/s", "終端pin水平誤差 m", "終端燃料 t", "同時到達", "接触支持", "実時間 s")
    evidence = ('<main class="completion-evidence" id="completion-evidence-section"><h1>Starship / 完成までの検証記録</h1><p class="completion-note">'+escape(heading)+
        '。表示対象の有効記録 '+denominator+'件の到達 '+str(arrived_count)+'/'+denominator+'、支持 '+str(supported_count)+'/'+denominator+
        '。整合性未成立 '+str(len(cases)-valid_count)+'件を成功率の分母から除外し、記録は残します。終了理由や低いCG速度だけで成功とは扱いません。</p>'+
        '<p>方針・実装ソースが異なる開発段階の記録です。固定条件の頑健性試験、選択器の比較、LLM/Jevの改善効果ではありません。'
        '表示対象の記録の整合性、両pinの同時進入、接触後の支持を分けています。実機飛行、SpaceX精度、衛星サービス、ミッション全体の完了は未検証です。</p>'+
        '<p>保存runのhashと渡された検証結果の対応を確認します。検証器や物理を再実行せず、'
        '表示対象の一覧が試験全体を網羅することも確認しません。発射からの再実行という呼出側の宣言は、このレポートでは検証できません。</p>'+
        '<div class="completion-table-scroll" tabindex="0" role="region" aria-label="測定結果の横スクロール表"><table><thead><tr>'+"".join('<th scope="col">'+x+'</th>' for x in header_names)+
        '</tr></thead><tbody>'+"".join(rows)+'</tbody></table></div>'+
        '<details><summary>表示対象の記録のソース・hash・検証結果と表示範囲</summary><pre>'+escape(json.dumps(_safe(manifest), ensure_ascii=False, indent=2))+'</pre></details>'+
        '<h2>保存位置を共通の尺度で比較</h2><div class="completion-controls"><label for="completion-case">右の記録</label><select id="completion-case"></select>'+
        '<button type="button" id="completion-play">再生</button><button type="button" id="completion-reset">開始へ</button><output id="completion-clock"></output>'+
        '<label class="completion-time-label" for="completion-time">開始からの経過秒</label><input id="completion-time" type="range" min="0" max="1" step="any" value="0">'+
        '<button type="button" id="completion-export">表示データJSON</button></div>'+
        '<p>タワーENUの東・北・上、単位km。薄線は保存済みの全経路、色線は選択時刻まで。2D図は補間せず直前の保存点を表示し、それぞれ自身の終端で止めます。'
        '3DCGの記録選択・時刻もこの再生時計に連動します。開始状態が違う場合は各開始からの経過時間をそろえ、同じ物理時刻の比較とは扱いません。</p>'+
        '<div class="completion-parallel"><article><h3 id="completion-left-title"></h3><canvas id="completion-left" aria-label="参照記録の東位置と高度"></canvas><p id="completion-left-state"></p></article>'+
        '<article><h3 id="completion-right-title"></h3><canvas id="completion-right" aria-label="選択記録の東位置と高度"></canvas><p id="completion-right-state"></p></article></div>'+
        '<p class="completion-note">以下の3DCGは保存した位置・姿勢の表示用補間です。補間、色、カメラ、プルームは新しい物理観測や成功を生成しません。'
        'CG表示だけでは熱・構造・実航法・実機の安全性を検証できません。2D図の数値は補間していません。'
        'CG表示は通常0.5秒間隔以上の保存点と開始・終端・実イベントの最寄り点を残しています。'
        '接触区間の姿勢・支持フレームは保存点だけを選び、元記録が許す限り0.1秒以内の間隔を保ちます。'
        '2D座標と測定値は表示対象の各記録を削減する前の保存点から計算しています。削減規則・件数・ソースhashは上のmanifestに残します。'
        '表の終端数値は帰還runの終端です。接触支持は、別の継続runを含めた検証結果です。</p></main>')
    study = {"title": "STARSHIP / SAVED SIX-DOF DEVELOPMENT RECORDS", "profile": profile,
             "runs": display_runs, "coverage": [], "checks": [], "provenance": {"completion_report": manifest}}
    study["provenance"]["cg_shared_sample_fields"] = cg_shared
    if display_runs:
        document = build_report(study)
        # Legacy catch wording describes isolated fixtures. This report links
        # exact saved-state continuation independently; keep the visible scope
        # specific rather than inherit a launch or initialization claim.
        document = document.replace("終端状態から開始した独立試験です。打ち上げ・帰還からキャッチまでを通した成功ではありません。",
            "保存帰還の終端状態を変更せず継続した表示です。発射からの再実行・成功とは分けて評価します。")
        document = document.replace("終端状態から初期化 / 打ち上げからの接続なし", "保存帰還の終端から継続 / 発射の再実行とは別")
        document = document.replace('<a href="verification.json">verification.json</a>',
            '<a href="#completion-evidence-section">保存した検証結果</a>')
        document = document.replace('</header>', '</header>'+evidence, 1)
        marker = '<script id="evidence" type="application/json">'
        start = document.index(marker)+len(marker)
        end = document.index('</script>', start)+len('</script>')
        document = document[:end]+'<script>'+_CG_DECODE+'</script>'+document[end:]
        document = document.replace("const study=JSON.parse(document.getElementById('evidence').textContent)",
            "const study=window.completionCGStudy||JSON.parse(document.getElementById('evidence').textContent)")
    else:
        document = '<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Starship · invalid retained records</title>'+evidence+'</html>'
    document = document.replace('</html>', '<style>'+_STYLE+'</style><script id="completion-data" type="application/json">'+_json(dataset)+'</script><script>'+_REPLAY+'</script></html>')
    return document


_STYLE = """
.completion-evidence{max-width:1320px;margin:auto;padding:26px 4vw;color:#dfeaf6;font:15px/1.75 system-ui}.completion-evidence h1{font-size:25px;letter-spacing:.03em}.completion-note{border-left:3px solid #ddb276;padding-left:14px;color:#c5d3e0}.completion-table-scroll{max-width:100%;overflow:auto;border:1px solid #304357}.completion-table-scroll table{white-space:nowrap;font-size:12px}.completion-table-scroll th,.completion-table-scroll td{padding:10px}.completion-table-scroll tr.invalid{color:#ffbfad;background:#3b2128}.completion-controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.completion-controls select{max-width:100%;min-width:0}.completion-controls input{flex:1;min-width:150px}.completion-time-label{font-size:12px}.completion-parallel{display:grid;grid-template-columns:1fr 1fr;gap:18px}.completion-parallel article{min-width:0}.completion-parallel canvas{display:block;width:100%;height:300px;touch-action:auto;background:#10202e}.completion-parallel p{font-size:12px;min-height:62px;overflow-wrap:anywhere}.completion-evidence pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px}.completion-evidence :focus-visible{outline:3px solid #b9e4ff;outline-offset:3px}.completion-evidence a{color:#9acafa}body{background:#080b10;color:#e7edf4;margin:0}button,select,input{font:inherit}button{cursor:pointer}.completion-controls button{min-height:40px}.completion-controls output{font:14px monospace}#scene{max-width:100%}
@media(max-width:736px){.completion-parallel{grid-template-columns:1fr}.completion-evidence{padding:18px 12px}.completion-evidence h1{font-size:21px}.completion-controls input{flex-basis:100%;min-width:0}.completion-controls select{flex:1 0 65%;min-width:160px}.completion-parallel canvas{height:260px}header{flex-wrap:wrap;overflow-wrap:anywhere}.hero .hud{left:12px;right:12px;gap:8px}.hero .clock{min-width:110px}.controls{max-width:100%}.controls select{max-width:100%;min-width:0}.controls input{min-width:0}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;animation:none!important;transition:none!important}}
"""


_CG_DECODE = r"""
(function(){'use strict';const study=JSON.parse(document.getElementById('evidence').textContent),shared=study.provenance.cg_shared_sample_fields||[];
study.runs.forEach((run,index)=>{run.samples.forEach(sample=>{Object.entries(shared[index]||{}).forEach(([key,value])=>{sample[key]=value});if(Array.isArray(sample.engine_states)&&Array.isArray(sample.engine_states[0]))sample.engine_states=sample.engine_states.map(e=>({throttle:e[0],gimbal_x_rad:e[1],gimbal_y_rad:e[2],available:e[3]}))})});window.completionCGStudy=study;
})();
"""


_REPLAY = r"""
(function(){'use strict';
const dataset=JSON.parse(document.getElementById('completion-data').textContent),cases=dataset.cases;
const $=id=>document.getElementById(id),choice=$('completion-case'),slider=$('completion-time'),play=$('completion-play');
const reduced=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
let timer=null,bounds=null,synchronizingCG=false;
cases.forEach((c,i)=>{const option=document.createElement('option');option.value=String(i);option.textContent=c.label+(c.valid?'':' / 整合性未成立');choice.append(option)});
choice.value=String(cases.length-1);
function stop(){if(timer!==null)clearInterval(timer);timer=null;play.textContent=reduced?'1秒進める':'再生';play.setAttribute('aria-pressed','false')}
function sampled(c,elapsed){if(!c.points.length)return null;const target=c.origin_time_s+elapsed;let low=0,high=c.points.length-1;while(low<high){const mid=Math.ceil((low+high)/2);if(c.points[mid].time_s<=target+1e-9)low=mid;else high=mid-1}return {point:c.points[low],index:low,terminal:target>=c.points.at(-1).time_s-1e-9}}
function drawOne(c,id,color,elapsed){const canvas=$(id),ctx=canvas.getContext('2d'),w=Math.max(1,canvas.clientWidth),h=canvas.clientHeight||300,dpr=window.devicePixelRatio||1;
canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.scale(dpr,dpr);ctx.fillStyle='#10202e';ctx.fillRect(0,0,w,h);ctx.font='11px system-ui';ctx.fillStyle='#c1d5e5';
ctx.fillText('東 '+bounds.left.toFixed(1)+'〜'+bounds.right.toFixed(1)+' km / 上 0〜'+bounds.top.toFixed(1)+' km',8,18);
const xy=p=>[34+(p.position_enu_m[0]/1000-bounds.left)/(bounds.right-bounds.left)*(w-47),h-30-p.position_enu_m[2]/1000/bounds.top*(h-58)];
function line(points,stroke){ctx.beginPath();ctx.strokeStyle=stroke;ctx.lineWidth=2;points.forEach((p,i)=>{const [x,y]=xy(p);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke()}
line(c.points,'#3b5367');const at=sampled(c,elapsed);if(!at){$(id+'-state').textContent=c.status+' / '+(c.display_error||'表示不可');return null}
line(c.points.slice(0,at.index+1),color);const [x,y]=xy(at.point);ctx.fillStyle=color;ctx.beginPath();ctx.arc(x,y,5,0,2*Math.PI);ctx.fill();
const p=at.point,fuel=typeof p.fuel_kg==='number'?(p.fuel_kg/1000).toFixed(3)+'t':'未観測';
$(id+'-state').textContent=c.status+' / T+'+p.time_s.toFixed(2)+'s'+(at.terminal?'（自身の終端）':'')+' / CG '+p.cg_speed_mps.toFixed(3)+'m/s / 北 '+(p.position_enu_m[1]/1000).toFixed(3)+'km / 燃料 '+fuel+' / '+p.phase;
return {case_index:cases.indexOf(c),time_s:p.time_s,sample_index:at.index,position_enu_m:p.position_enu_m,terminal:at.terminal}}
function draw(){const elapsed=Number(slider.value);$('completion-clock').textContent='開始 +'+elapsed.toFixed(2)+'s';slider.setAttribute('aria-valuetext',elapsed.toFixed(2)+' 秒');
const left=drawOne(cases[0],'completion-left','#90cafd',elapsed),right=drawOne(cases[Number(choice.value)],'completion-right','#e7ba77',elapsed);
window.completionReplayDiagnostics={elapsed_s:elapsed,left,right,interpolated:false,reduced_motion:!!reduced};$('completion-evidence-section').dataset.replayDiagnostics=JSON.stringify(window.completionReplayDiagnostics);
const selected=cases[Number(choice.value)],cg=window.sixdofSavedReplay;if(cg&&!synchronizingCG&&Number.isInteger(selected.cg_run_index)){synchronizingCG=true;try{cg.pause();cg.seek(selected.cg_run_index,selected.origin_time_s+elapsed);cgScope();}finally{synchronizingCG=false;}}}
function setup(){stop();const selected=[cases[0],cases[Number(choice.value)]],points=selected.flatMap(c=>c.points);slider.max=Math.max(0,...selected.map(c=>c.points.length?Math.max(c.points.at(-1).time_s,c.cg_terminal_time_s||0)-c.origin_time_s:0));slider.value='0';
bounds={left:Math.min(0,...points.map(p=>p.position_enu_m[0]/1000))-1,right:Math.max(0,...points.map(p=>p.position_enu_m[0]/1000))+1,top:Math.max(1,...points.map(p=>p.position_enu_m[2]/1000))+1};
$('completion-left-title').textContent='参照 / '+cases[0].label;$('completion-right-title').textContent='選択 / '+cases[Number(choice.value)].label;draw()}
choice.addEventListener('change',setup);slider.addEventListener('input',()=>{stop();draw()});$('completion-reset').addEventListener('click',()=>{stop();slider.value='0';draw()});
play.addEventListener('click',()=>{if(reduced){slider.value=String(Math.min(Number(slider.max),Number(slider.value)+1));draw();return}if(timer!==null){stop();return}if(Number(slider.value)>=Number(slider.max))slider.value='0';play.textContent='停止';play.setAttribute('aria-pressed','true');timer=setInterval(()=>{slider.value=String(Math.min(Number(slider.max),Number(slider.value)+1));draw();if(Number(slider.value)>=Number(slider.max))stop()},100)});
$('completion-export').addEventListener('click',()=>{const blob=new Blob([JSON.stringify(dataset,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='starship-completion-display.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)});
if(reduced&&$('play')){$('play').disabled=true;$('play').title='動きを減らす設定のため、3DCGは時刻スライダーで操作してください'}
const cgScope=()=>{const scope=$('scope');if(scope&&$('run'))scope.textContent='SAVED RETURN STATE → REPLAY · FULL LAUNCH REQUIRES SEPARATE RECEIPT'};
window.addEventListener('missionos:saved-replay-paint',event=>{if(synchronizingCG||!event.detail)return;const selected=cases.findIndex(c=>c.cg_run_index===event.detail.run_index);if(selected<0)return;synchronizingCG=true;try{stop();if(Number(choice.value)!==selected){choice.value=String(selected);setup();}slider.value=String(Math.max(0,event.detail.time_s-cases[selected].origin_time_s));draw();cgScope();}finally{synchronizingCG=false;}});
if($('run'))$('run').addEventListener('change',cgScope);cgScope();window.addEventListener('resize',draw);setup();
})();
"""
