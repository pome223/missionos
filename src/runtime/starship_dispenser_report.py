"""Portable Japanese evidence viewer for the synthetic dispenser experiment."""

from __future__ import annotations

import json
from typing import Any


def build_report(study: dict[str, Any], verification: dict[str, Any]) -> str:
    """Render recorded outcomes; never infer physical mission success from them."""
    data = json.dumps({"study": study, "verification": verification},
                      ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    data = data.replace("<", "\\u003c").replace("&", "\\u0026")
    return _HTML.replace("__DATA__", data)


_HTML = r'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>MissionOS | 放出詰まりの方針比較</title>
<style>
:root{color-scheme:dark;--bg:#080e13;--panel:#101b24;--line:#293a47;--muted:#a5b5c1;--text:#f0f4f7;--cyan:#8bdfec;--amber:#e4c080}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.8 system-ui,-apple-system,"Hiragino Kaku Gothic ProN",sans-serif}
main{max-width:1260px;margin:auto;padding:42px 30px 70px}header{padding:0 0 26px;border-bottom:1px solid var(--line)}
.eyebrow{font:11px/1.6 ui-monospace,monospace;letter-spacing:.16em;color:var(--cyan)}h1{font-weight:500;letter-spacing:-.03em;font-size:clamp(27px,4vw,45px);margin:10px 0}h2{font-size:19px;font-weight:500;margin:0 0 15px}p{margin:9px 0}.lead{max-width:860px;color:var(--muted)}
.notice{border-left:2px solid var(--amber);padding:10px 18px;background:#211f19;color:#e4d9c3;font-size:13px;margin-top:22px}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:22px;padding:28px 0}.metric{border-bottom:1px solid var(--line);padding-bottom:16px}.metric small,.caption{font-size:12px;color:var(--muted)}.metric strong{display:block;font:500 34px/1.5 ui-monospace,monospace}.panel{padding:25px;background:var(--panel);border:1px solid var(--line);border-radius:8px;margin:22px 0}
.scroll{overflow-x:auto}#expected table{min-width:0}table{width:100%;border-collapse:collapse;min-width:800px;font-size:13px}th,td{padding:12px 13px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:11px;font-weight:400;color:var(--muted)}td.num{font-family:ui-monospace,monospace;font-variant-numeric:tabular-nums}.muted{color:var(--muted)}
select,button{background:#162733;color:var(--text);border:1px solid #425968;padding:8px 10px;border-radius:4px;font:inherit;max-width:100%}#world-note{overflow-wrap:anywhere}.controls{display:flex;flex-wrap:wrap;gap:15px;align-items:center;margin:18px 0}input[type=range]{flex:1;min-width:120px;accent-color:var(--cyan)}a{color:var(--cyan)}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:500px;overflow:auto;font:12px/1.6 ui-monospace,monospace}details{margin-top:14px}summary{cursor:pointer;color:var(--muted);font-size:12px}.twocol{display:grid;grid-template-columns:1fr 1fr;gap:25px}.track{margin:14px 0}.trackhead{display:flex;justify-content:space-between;gap:15px;font-size:12px}.rail{height:12px;position:relative;background:#071017;margin:7px 0;border-radius:2px;overflow:hidden}.segment{position:absolute;height:100%;background:#506b7d}.segment.retry{background:var(--cyan)}.cursor{position:absolute;top:0;height:100%;width:2px;background:white}.legend{display:flex;gap:20px;font-size:12px;color:var(--muted)}.legend b{display:inline-block;width:10px;height:10px;margin-right:6px;background:var(--cyan)}.legend b.wait{background:#506b7d}footer{font-size:11px;color:var(--muted);border-top:1px solid var(--line);padding-top:20px;margin-top:32px}
@media(max-width:700px){main{padding:25px 12px}.panel{padding:17px}.metrics{grid-template-columns:1fr;gap:13px}.metric strong{font-size:27px}.twocol{grid-template-columns:1fr}.controls{align-items:stretch}.controls label{width:100%}select{width:100%}}
</style></head><body><main>
<header><div class="eyebrow">MISSIONOS / SYNTHETIC DISPENSER STUDY</div><h1>待つ、再試行する、中止する。</h1><p class="lead">同じ故障系列で5つの方針を動かし、放出数と残り時間を比べる。観測できる情報だけで判断する方針と、未来を知る参考値を分けて表示します。</p><div class="notice">試験専用ペイロード3個・作業期限24秒の合成サブシステム試験です。Flight 14の実機再現、26基の衛星放出、帰還成功を検証するものではありません。燃料・軌道・通信サービスは未モデル化。LLM呼出しはありません。</div></header>
<section class="metrics"><div class="metric"><small>未使用の評価系列 × 方針</small><strong id="counts">—</strong><span class="caption">短時間回復・遅い回復・回復なし</span></div><div class="metric"><small>同じ観測を使う有限モデルの最適期待効用</small><strong id="optimal">—</strong><span class="caption">既知の分布・行動・目的関数の範囲</span></div><div class="metric"><small>実現回復時刻を知る参考上限</small><strong id="clairvoyant">—</strong><span class="caption">実行時に達成可能な改善量ではありません</span></div></section>
<section class="panel"><h2>評価用系列での結果</h2><p class="caption">各群を1/3で集計した標本平均です。上の最適期待値とは別の数値であり、有限の評価系列では分布既知方針が毎回最良になるとは限りません。方針の調整は別の30系列だけで行っています。</p><div class="scroll"><table><thead><tr><th>方針</th><th>平均放出数 / 3</th><th>全数放出の割合</th><th>平均経過秒</th><th>平均試行数</th><th>平均効用</th></tr></thead><tbody id="summary"></tbody></table></div><p class="caption">効用 = 10 × 放出数 − 0.15 × 経過秒 − 試行数。実際の燃料量や事業価値を表す尺度ではありません。</p><p id="comparison-finding" class="caption"></p><p id="verification" class="caption"></p></section>
<section class="panel"><h2>固定した方針の期待値</h2><p class="caption">同じ既知分布と観測の全分岐を評価した値です。評価系列の偶然による差と、有限モデル内の方針の差を分けます。</p><div id="expected"></div><p id="gap" class="caption"></p></section>
<section class="panel"><h2>同じ故障系列を比べる</h2><div class="controls"><label>評価系列 <select id="world" aria-label="評価系列"></select></label><label for="clock">記録時刻</label><input id="clock" type="range" min="0" max="24" step="2" value="24" aria-label="記録時刻"><output id="clock-value">24 s</output></div><p id="world-note" class="caption"></p><div class="legend"><span><b></b>再試行</span><span><b class="wait"></b>待機</span><span>白線：表示時刻</span></div><div id="tracks"></div><div class="scroll"><table><thead><tr><th>方針</th><th>終端放出数</th><th>終端時刻</th><th>期限までの余裕</th><th>試行数</th><th>効用</th><th>終了理由</th></tr></thead><tbody id="outcomes"></tbody></table></div><details><summary>選択した系列の観測・提案・Rules・実行receipt</summary><pre id="records"></pre></details><p class="caption">系列名と回復時刻は検証者用の事後情報です。方針へは渡していません。時間バーは保存された実行記録を表示し、未実行の動作を補間しません。</p></section>
<section class="panel twocol"><div><h2>比較を成立させる条件</h2><p class="caption">乱数は絶対時刻に結び付けて先に生成します。全方針で同じ系列を共有し、再試行回数で故障の世界が変わることを防ぎます。センサーは開始時と行動終了時だけ取得します。</p><p class="caption">retryは開始時に回復済みなら成功し、4秒後に結果を記録します。waitは2秒。期限を超える行動はRulesが拒否します。simulation scopeは実運航者の承認ではありません。</p></div><div><h2>ここから言えること</h2><p class="caption">この試験は、方針を実行して結果を比較する仕組みを検査します。ルールで十分なら、その結果を残します。上限との差があってもLLMの価値を示したことにはなりません。</p><p class="caption">分布やセンサーの性質は仮定です。テキスト情報の追加がLLMを必要にするという点も未検証です。物理の再現精度と監督方針の比較を混ぜません。</p></div></section>
<details><summary>調整用系列の候補と採用パラメータ</summary><pre id="tuning"></pre></details><details><summary>設定・出典hash・検証範囲</summary><pre id="provenance"></pre></details>
<footer><a href="study.json">全実行記録 JSON</a> · <a href="verification.json">保存記録の検証</a> · <a href="manifest.json">ファイルのSHA-256</a><p>LLM judges. Human approves. Rules constrain. Executor acts. Verifier checks. Repair loops.</p></footer>
</main><script id="data" type="application/json">__DATA__</script><script>
'use strict';
const {study:S,verification:V}=JSON.parse(document.getElementById('data').textContent);
const names={abort:'すぐ中止',immediate_abort:'すぐ中止',fixed_retry:'決まった回数の再試行',periodic_retry:'一定間隔で待って再試行',history_rule:'履歴を使うルール',finite_model_optimal:'分布既知の最良方針',distribution_optimal:'分布既知の最良方針',distribution_known:'分布既知の最良方針',optimal:'分布既知の最良方針'};
const scalar=x=>typeof x==='number'?x:x?.value;
const fmt=(x,d=2)=>Number.isFinite(scalar(x))?scalar(x).toFixed(d):'未取得';
const label=x=>names[x]||x;
function text(id,value){document.getElementById(id).textContent=value;}
function row(parent,values){const tr=document.createElement('tr');values.forEach((v,i)=>{const td=document.createElement('td');td.textContent=v;if(i)td.className='num';tr.appendChild(td)});parent.appendChild(tr);}
const evaluation=S.worlds.filter(w=>w.split==='eval');
const policies=Object.keys(S.summary.eval);
text('counts',`${evaluation.length} × ${policies.length}`);
text('optimal',fmt(S.model_bounds.finite_model_optimal_expected_utility,6));
text('clairvoyant',fmt(S.model_bounds.clairvoyant_expected_utility,6));
for(const p of policies){const a=S.summary.eval[p];row(document.getElementById('summary'),[label(p),fmt(a.mean_release_count),fmt(a.full_release_fraction*100,1)+'%',fmt(a.mean_elapsed_s),fmt(a.mean_attempt_count),fmt(a.mean_utility,4)]);}
const evalRuns=S.policy_runs.filter(r=>r.split==='eval');
const histories=new Map(evalRuns.filter(r=>r.policy==='history_rule').map(r=>[r.world_id,r]));
const optimalRuns=evalRuns.filter(r=>r.policy==='finite_model_optimal');
const equalReleases=optimalRuns.filter(r=>histories.get(r.world_id)?.terminal.release_count===r.terminal.release_count).length;
const total=p=>evalRuns.filter(r=>r.policy===p).reduce((n,r)=>n+r.terminal.release_count,0);
text('comparison-finding',`履歴ルールと分布既知方針の放出数が同じ系列：${equalReleases}/${optimalRuns.length}。放出総数は一定間隔の方針${total('periodic_retry')}個、履歴ルール${total('history_rule')}個、分布既知方針${total('finite_model_optimal')}個。最良方針は固定した効用の期待値を最大化し、放出数だけを最大化するものではありません。`);
text('verification',V.verified?'保存記録の独立走査：合格。実機の再現精度・物理的放出・ミッション完了の判定ではありません。':'保存記録の検証に失敗しました。詳細のverification.jsonを確認してください。');
const expected=S.model_bounds.policy_expected_utilities;
if(expected){const table=document.createElement('table');const wrap=document.createElement('div');wrap.className='scroll';wrap.appendChild(table);document.getElementById('expected').appendChild(wrap);for(const [p,value] of Object.entries(expected))row(table,[label(p),fmt(value,6)]);const baselines=Object.entries(expected).filter(([p])=>!p.includes('optimal')&&!p.includes('distribution'));if(baselines.length){const best=baselines.reduce((a,b)=>scalar(a[1])>=scalar(b[1])?a:b);text('gap',`単純方針の最大期待効用（${label(best[0])}）との差：${fmt(scalar(S.model_bounds.finite_model_optimal_expected_utility)-scalar(best[1]),6)}。宣言した有限モデル内の差であり、LLMの改善実績ではありません。`);}}else{text('expected','方針別の厳密期待値は未取得です。評価標本の差を、到達可能な改善量とは呼びません。');}
const select=document.getElementById('world');
for(const w of evaluation){const o=document.createElement('option');o.value=w.world_id;o.textContent=`${w.group} / ${w.world_id}`;select.appendChild(o);}
function action(step){return step.proposal.action||step.proposal.action_type||'unknown';}
function times(step,previous){const r=step.receipt||{};return [r.start_time_s??r.start_s??previous,r.end_time_s??r.end_s??step.observation?.time_s??previous];}
function render(){const world=evaluation.find(w=>w.world_id===select.value);if(!world)return;const t=Number(document.getElementById('clock').value);text('clock-value',`${t} s`);text('world-note',`事後の検証用情報：${world.group}、回復${world.recovery_time_s===null?'なし':world.recovery_time_s+'秒'}。全方針の系列hash：${world.tape_sha256}`);const runs=S.policy_runs.filter(r=>r.world_id===world.world_id);const tracks=document.getElementById('tracks');tracks.replaceChildren();const outcomes=document.getElementById('outcomes');outcomes.replaceChildren();for(const r of runs){const terminal=r.terminal;row(outcomes,[label(r.policy),terminal.release_count,fmt(terminal.elapsed_s,0)+' s',fmt(terminal.margin_s,0)+' s',terminal.attempt_count,fmt(terminal.utility,2),terminal.reason]);const track=document.createElement('div');track.className='track';const head=document.createElement('div');head.className='trackhead';const title=document.createElement('span');title.textContent=label(r.policy);head.appendChild(title);const status=document.createElement('span');let previous=0;let completed=0;const rail=document.createElement('div');rail.className='rail';for(const step of r.steps){const [start,end]=times(step,previous);previous=end;if(end<=t)completed++;if(end<=start)continue;const segment=document.createElement('div');segment.className='segment '+action(step);segment.style.left=(100*start/24)+'%';segment.style.width=(100*(end-start)/24)+'%';segment.style.opacity=end<=t?'1':'.28';segment.title=`${action(step)} ${start}–${end} s`;rail.appendChild(segment);}status.textContent=t>=terminal.elapsed_s?'終端に到達':`完了した記録 ${completed} 件`;head.appendChild(status);const cursor=document.createElement('div');cursor.className='cursor';cursor.style.left=`min(calc(100% - 2px),${100*t/24}%)`;rail.appendChild(cursor);track.append(head,rail);tracks.appendChild(track);}text('records',JSON.stringify(runs,null,2));}
select.addEventListener('change',render);document.getElementById('clock').addEventListener('input',render);render();
text('tuning',JSON.stringify(S.tuning,null,2));text('provenance',JSON.stringify({claim_boundary:S.claim_boundary,config:S.config,config_sha256:S.config_sha256,model_bounds:S.model_bounds,provenance:S.provenance,verification:V},null,2));
</script></body></html>'''
