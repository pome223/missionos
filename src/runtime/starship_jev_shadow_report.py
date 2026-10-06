"""Standalone Japanese report for recorded, non-controlling Jev routing."""

from __future__ import annotations

from html import escape
import json
from typing import Any


_ROUTE_LABELS = {
    "bounded": "既存の履歴ルール",
    "need_observation": "追加観測の提案",
    "human_review": "人による確認の提案",
    "deep_reasoning": "DeepSeekによる検討の提案",
    "unavailable": "判断を取得できず",
}


def _text(value: object) -> str:
    return escape(str(value), quote=True)


def _json(value: object) -> str:
    return escape(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _table(headers: list[str], rows: list[list[object]]) -> str:
    heading = "".join(f"<th>{_text(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_text(item)}</td>" for item in row) + "</tr>" for row in rows
    )
    return f'<div class="scroll"><table><thead><tr>{heading}</tr></thead><tbody>{body}</tbody></table></div>'


def build_report(bundle: dict[str, Any], verdict: dict[str, Any]) -> str:
    """Display saved records and verification limits; no network or runtime logic."""
    shadow = bundle.get("shadow")
    shadow = shadow if isinstance(shadow, dict) else {}
    summary = verdict.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    verified = verdict.get("verified") is True
    mode = {
        "fixture": "固定fixture（外部モデル呼出しなし）",
        "live": "live設定（呼出しの有無・成否は記録で確認）",
    }.get(shadow.get("mode"), "未確認")

    def metric(key):
        return summary.get(key, "未検証")

    status = "保存記録の照合：合格" if verified else "保存記録の照合：不合格・未確認"
    metrics = [
        ("履歴ルールで実行した評価系列", metric("evaluation_count"), "元の870実行記録も独立に走査"),
        (
            "故障後に振分けを照会した観測点",
            metric("trigger_count"),
            f"該当しない系列：{metric('no_trigger_count')}",
        ),
        (
            "元の終端記録と一致した系列",
            metric("terminal_match_count"),
            "放出数・時刻・試行数・効用を含む全記録を照合",
        ),
    ]
    metric_html = "".join(
        f'<div class="metric"><small>{_text(label)}</small><strong>{_text(value)}</strong><span>{_text(note)}</span></div>'
        for label, value, note in metrics
    )
    route_counts = summary.get("route_counts", {})
    route_rows = [[_ROUTE_LABELS.get(route, route), count] for route, count in route_counts.items()]
    status_rows = [[key, value] for key, value in summary.get("status_counts", {}).items()]
    records = shadow.get("records", [])
    case_rows = []
    detail_rows = []
    for index, record in enumerate(records if isinstance(records, list) else []):
        if not isinstance(record, dict):
            continue
        frame = record.get("frame") if isinstance(record.get("frame"), dict) else {}
        budget = frame.get("public_budget") if isinstance(frame.get("public_budget"), dict) else {}
        routing = record.get("routing") if isinstance(record.get("routing"), dict) else {}
        invocation = (
            routing.get("invocation") if isinstance(routing.get("invocation"), dict) else {}
        )
        route = routing.get("route") or "unavailable"
        case_ref = str(frame.get("case_ref", "未確認"))
        tick, scale = budget.get("time_ticks"), budget.get("tick_s")
        seconds = tick * scale if type(tick) is int and type(scale) is int else "未確認"
        case_rows.append(
            [
                index + 1,
                case_ref[:12],
                seconds,
                budget.get("released", "未確認"),
                _ROUTE_LABELS.get(route, route),
                invocation.get("status", "未確認"),
            ]
        )
        detail_rows.append(
            f"<details><summary>観測点 {index + 1} — {_text(case_ref[:12])}</summary>"
            f"<pre>{_json(record)}</pre></details>"
        )
    material = {
        "mode": shadow.get("mode"),
        "trigger": shadow.get("trigger"),
        "maximum_calls": shadow.get("maximum_calls"),
        "worker": shadow.get("worker"),
        "worker_exit_code": shadow.get("worker_exit_code"),
        "source_sha256": shadow.get("source_sha256"),
        "verification": verdict,
    }
    return (
        _HTML.replace("__MODE__", _text(mode))
        .replace("__STATUS__", _text(status))
        .replace("__METRICS__", metric_html)
        .replace("__ROUTES__", _table(["振分け先の提案", "記録数"], route_rows))
        .replace("__STATUSES__", _table(["通信・検証の状態", "記録数"], status_rows))
        .replace("__ATTEMPTS__", _text(metric("recorded_calls_attempted")))
        .replace("__RESPONSES__", _text(metric("recorded_responses_observed")))
        .replace(
            "__CASES__",
            _table(
                ["#", "系列の匿名参照", "模擬時刻（秒）", "放出数", "振分け先の提案", "状態"],
                case_rows,
            ),
        )
        .replace("__DETAILS__", "".join(detail_rows))
        .replace("__MATERIAL__", _json(material))
    )


_HTML = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,"><title>MissionOS | 故障観測へのJevシャドー判断</title>
<style>
:root{color-scheme:dark;--bg:#080e13;--panel:#101b24;--line:#293a47;--muted:#a5b5c1;--text:#f0f4f7;--cyan:#8bdfec;--amber:#e4c080}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.8 system-ui,-apple-system,"Hiragino Kaku Gothic ProN",sans-serif}main{max-width:1220px;margin:auto;padding:42px 28px 70px}header{padding-bottom:25px;border-bottom:1px solid var(--line)}.eyebrow{font:11px ui-monospace,monospace;letter-spacing:.16em;color:var(--cyan)}h1{font-size:clamp(26px,4vw,43px);font-weight:500;letter-spacing:-.03em}h2{font-size:20px;font-weight:500;margin-top:0}p{margin:9px 0}.lead,.caption,small{color:var(--muted)}.lead{max-width:930px}.notice{border-left:2px solid var(--amber);padding:13px 17px;background:#211f19;color:#e4d9c3;font-size:13px;margin-top:23px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;margin:28px 0}.metric{border-bottom:1px solid var(--line);padding-bottom:16px}.metric strong{display:block;font:500 35px/1.5 ui-monospace,monospace}.metric span{font-size:12px;color:var(--muted)}.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:24px;margin:23px 0}.twocol{display:grid;grid-template-columns:1fr 1fr;gap:24px}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--muted);font-weight:400}#cases table{min-width:870px}details{margin:13px 0}summary{cursor:pointer;color:var(--cyan);font-size:13px}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:500px;overflow:auto;font:12px/1.6 ui-monospace,monospace}footer{font-size:12px;border-top:1px solid var(--line);padding-top:20px;color:var(--muted)}a{color:var(--cyan)}
@media(max-width:720px){main{padding:27px 12px}.metrics,.twocol{grid-template-columns:1fr}.panel{padding:17px}.metrics{gap:14px}}
</style></head><body><main>
<header><div class="eyebrow">MISSIONOS / SYNTHETIC FAULT OBSERVATION / SHADOW</div>
<h1>故障の観測を、次の相談先へ。</h1><p class="lead">放出装置の合成試験を既存の履歴ルールで動かし、再試行の失敗が観測された後の最初の判断点だけをJevへ照会します。Jevの答えは相談先の提案として記録し、実行には戻しません。</p>
<p class="caption">モード：__MODE__</p><div class="notice">試験専用ペイロード3個・期限24秒の合成試験です。振分けは提案だけで、追加観測・人への送信・DeepSeek呼出しは実行しません。実時間での応答を保証しません。実機精度、衛星サービス開始、Jevの有用性は評価していません。</div></header>
<section class="metrics">__METRICS__</section>
<section class="panel"><h2>__STATUS__</h2><p>元の比較試験870件を検証し、そのうち評価用の履歴ルール60件と今回の再実行を照合します。公開された履歴と予算から、故障後の最初の判断点22件を独立に抽出します。38件には該当する判断点がありません。</p><p class="caption">検証の対象は保存記録の整合性です。PIDやAPI呼出しの記載だけから、実プロセスや外部推論の実行を独立に認証したとは扱いません。通信失敗は結果から除かず、判断未取得として残します。</p></section>
<section class="panel"><h2>振分けと通信の記録</h2><p class="caption">記録されたAPI試行：__ATTEMPTS__件。応答の観測：__RESPONSES__件。fixtureの固定判断はモデル推論に数えません。Jevのconfidenceは未校正で、承認や放出可否の条件には使いません。</p><div class="twocol"><div>__ROUTES__</div><div>__STATUSES__</div></div></section>
<section class="panel" id="cases"><h2>故障後の観測点</h2><p class="caption">模擬時刻は試験内の経過時間です。APIの待ち時間とは異なります。系列の参照は検証用hashで、回復時刻や将来の観測はJevへの入力に含めません。</p>__CASES__</section>
<section class="panel"><h2>この記録から言える範囲</h2><p>履歴ルールへの作用がなく、元の実行記録と一致したかを確認します。一致はJevによる改善を示しません。振分けが適切だったか、追加の推論が必要だったか、実時間で間に合うかは次の評価課題です。</p><p class="caption">LLM judges. Human approves. Rules constrain. Executor acts. Verifier checks. Repair loops.</p></section>
<details><summary>公開観測・振分け・呼出し記録</summary>__DETAILS__</details><details><summary>検証範囲・プロセス記録・ソースhash</summary><pre>__MATERIAL__</pre></details>
<footer><a href="study.json">全記録JSON</a> · <a href="verification.json">検証結果</a> · <a href="manifest.json">ファイルhash</a><p>保存記録のレポート。実機ミッションの完了判定ではありません。</p></footer>
</main></body></html>"""
