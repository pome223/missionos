"""Starship planning and scoped execution through the existing MissionOS chat.

This is a deterministic domain route, not an authority decision by an LLM.
Only exact operator commands can request approval or execution. The runtime
service remains responsible for plan binding, expiry, one-time consumption,
worker invocation and independent verification.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
import re
from typing import Any
from urllib.parse import quote

from src.runtime.starship_sixdof_catalog import SIXDOF_SCENARIOS
from src.runtime.starship_mission_director import SCENARIOS as MANAGED_SCENARIOS


AMBIGUOUS_CONFIRMATIONS = {
    "yes",
    "y",
    "ok",
    "okay",
    "go",
    "proceed",
    "はい",
    "了解",
    "承認",
    "実行",
}


COMMANDS = {
    "/approve": "approve",
    "Approve the current MissionOS plan.": "approve",
    "/run": "execute",
    "Run the current bounded action through the MissionOS execution gate.": "execute",
    "/status": "status",
    "/reject": "reject",
    "Reject the current MissionOS plan.": "reject",
}


class StarshipChatError(ValueError):
    """Fixed reasons generated solely by this domain router."""


def get_starship_service():
    # Import lazily: unrelated MissionOS conversations do not initialize this runtime.
    from src.runtime.starship_mission_control import get_starship_service as get_service

    return get_service()


def _response(text: str, action: str, result: dict, session_id: str) -> dict:
    response = {
        "schema_version": "missionos_autonomy_conversation_response.v1",
        "operator_instruction": {"text": text, "source": "missionos_chat"},
        "routed_action": action,
        "routing_source": "starship_scoped_mission_control",
        "message": str(result.get("message") or "Starship simulation status updated."),
        "operation_result": {**result, "summary_status": result.get("status")},
        "progress_counted": False,
        "conversation_route_bypassed_guardrails": False,
    }
    plan = result.get("plan")
    if (
        isinstance(plan, Mapping)
        and isinstance(plan.get("id"), str)
        and isinstance(plan.get("sha256"), str)
    ):
        response["starship_context"] = {
            "plan_id": plan["id"],
            "plan_sha256": plan["sha256"],
            "session_id": session_id,
        }
        if action == "plan":
            response["message"] += (
                f"\nScenario: {plan.get('scenario')}; backend: {plan.get('backend')}"
                f"\nRelease policy: {plan.get('release_policy')}"
                f"\nReason: {plan.get('rationale')}"
                f"\nUncertainties: {', '.join(plan.get('uncertainties', []))}"
                f"\nPlan: {plan['id']}; SHA-256: {plan['sha256']}"
                f"\nExpires (Unix seconds): {plan.get('expires_at_epoch_s')}"
            )
            if plan.get("jev_shadow"):
                shadow = plan["jev_shadow"]
                response["message"] += (
                    f"\nJev shadow: {shadow['mode']}; 最大API呼出し {shadow['maximum_provider_calls']}回。"
                    "60系列の最初の観測失敗後だけを分類し、履歴ルールの実行は変更しません。"
                    "この呼出し範囲も /approve の対象です。"
                )
            if plan.get("mission_envelope"):
                scope = plan["mission_envelope"]
                response["message"] += (
                    f"\nミッション管制: {scope['mode']}。/approve は各操作ではなく、表示した判断範囲を承認します。"
                    f"\nJev最大{scope['maximum_jev_calls']}回、DeepSeek最大{scope['maximum_llm_calls']}回。"
                    "放出の継続・保留・見送り、機構状態診断1回、帰還方式、キャッチか退避かを範囲内で決めます。"
                    "保留は合計30秒。帰還時刻は変更不可。応答期限は75秒。独立した実行チェックを通し、後続観測を検証します。"
                    "範囲外は引継ぎ要求を記録し、今回は事前承認済みの代替動作を実行します。飛行中の承認受付は未実装です。"
                    "\n"+"\n".join(plan["limitations"])
                )
            if plan.get("flight_supervision"):
                scope = plan["flight_supervision"]
                response["message"] += (
                    f"\n飛行中監督: {scope['mode']}; Jev最大{scope['maximum_jev_calls']}回、"
                    f"DeepSeek最大{scope['maximum_deepseek_calls']}回（Jevが追加推論へ回した場合）。"
                    "\n/approve は残りの放出を見送る操作1回と、このモデル呼出し範囲を含みます。"
                    "指令の受付を分離成功として扱いません。"
                )
                if scope.get("maximum_observation_requests") == 1:
                    response["message"] += "\n追加の放出状態報告を最大1回要求し、取得後に再判断します。期限は最初の依頼から75秒のままで、観測要求も /approve に含みます。"
            if plan.get("return_policy"):
                policy = plan["return_policy"]
                response["message"] += (
                    f"\n衛星保持時の帰還方針: {policy['policy_id']}。"
                    "\n/approve は固定の終端誘導アルゴリズムの使用も含みます。"
                    "予定の帰還時刻に実際の衛星保持を確認して発動し、質量・使用可能推力・降下速度・姿勢・応答遅れから反転開始条件を計算します。"
                    "モデルの応答や放出見送り操作の成否には依存しません。帰還時刻・軌道離脱条件は変更しません。"
                    "安全な帰還を保証する承認ではありません。"
                )
            if plan.get("booster_catch"):
                response["message"] += (
                    "\n/approve はタワー付近に初期化したキャッチ実験へのGOです。"
                    "支持点・アームの寸法と荷重特性は仮定値です。タワーが利用不可ならアームを閉じず、ローカルの退避を実行します。"
                    "打ち上げから帰還してきた状態ではなく、実機のキャッチ承認でもありません。"
                )
            if plan.get("booster_recovery"):
                policy = plan["booster_recovery"]
                response["message"] += (
                    f"\n打ち上げからの連続帰還方針: {policy['policy_id']}。"
                    "\n/approve は打ち上げ・実際の段分離状態からのブースター帰還・条件を満たした場合のキャッチ機構への引継ぎを承認します。"
                    "タワー付近への状態の初期化や位置・速度の補正は行いません。"
                    "引継ぎ条件を満たさない場合や衝突した場合も、その失敗を保存します。"
                    "終端の接近・支持計算は最大30秒で、2秒間の連続支持を確認します。"
                    "キャッチの成功や実機の安全性を保証する承認ではありません。"
                )
            if plan.get("backend") == "starship_sixdof":
                simulation = plan["simulation"]
                response["message"] += (
                    f"\n6DOF profile: {simulation['profile_id']}"
                    f"\nProfile SHA-256: {simulation['profile_sha256']}"
                    f"\n計算条件: {simulation['scenario']}; dt倍率 {simulation['dt_scale']}; "
                    f"実行上限 {simulation['maximum_wall_time_s']}秒"
                    f"\n検証範囲: {plan['verification_scope']}"
                    "\n" + "\n".join(plan["limitations"])
                )
        execution = result.get("execution")
        if result.get("status") == "verified" and isinstance(execution, dict):
            if plan.get("backend") == "starship_sixdof":
                for outcome in execution.get("verification", {}).get("observed_outcomes", []):
                    response["message"] += (
                        f"\n6DOF結果: {outcome['scenario']} / {outcome['termination']}"
                    )
                    if outcome["scenario"] in {"launch", "engine_out", "deployment_no_effect"}:
                        response["message"] += (
                            f"; 軌道条件 {'到達' if outcome['orbit_gate_reached'] else '未到達'}"
                            f"; 衛星放出 {outcome['payload_released_count']}基"
                        )
                    elif "simulated_catch_supported" in outcome:
                        response["message"] += (
                            f"; アームによる連続支持 {'確認' if outcome['simulated_catch_supported'] else '未達'}"
                            f"; 終端実験 {outcome['duration_s']:.2f}秒（タワー付近に初期化）"
                        )
                    else:
                        response["message"] += f"; 応答区間 {outcome['duration_s']:.1f}秒（フルミッションではありません）"
                    speed = outcome.get("final_contact_point_speed_mps")
                    if isinstance(speed, (int, float)):
                        response["message"] += f"; 接触点速度 {speed:.3f} m/s"
                    envelope = outcome.get("booster_recovery_envelope_satisfied")
                    if envelope is not None:
                        response["message"] += f"; ブースター回収条件 {'到達' if envelope else '未達'}（キャッチ未検証）"
                response["message"] += "\n記録の整合性を確認した結果です。ミッション成功の認定ではありません。"
                supervision = execution.get("verification", {}).get("flight_supervision")
                if isinstance(supervision, dict):
                    response["message"] += (
                        f"\n飛行中監督: 操作{supervision.get('command_count', 0)}回; "
                        f"放出見送りの後続観測 {'確認済み' if supervision.get('observed_effect') is True else '未確認'}。"
                        "モデルの優位性や実機での安全性を示す結果ではありません。"
                    )
                if plan.get("return_policy"):
                    retained = execution.get("verification", {}).get("retained_return", {})
                    response["message"] += (
                        f"\n衛星保持時の帰還: {plan['return_policy']['policy_id']}; "
                        f"保存記録の検証 {'通過' if retained.get('passed') is True else '未確認'}。"
                        "接触速度・帰還結果は上の6DOF結果で確認してください。"
                    )
                if plan.get("booster_recovery"):
                    verification = execution.get("verification", {})
                    recovery = verification.get("booster_recovery", {})
                    response["message"] += (
                        "\n打ち上げからのブースター帰還: キャッチへの引継ぎ "
                        + ("到達" if recovery.get("handoff_reached") is True else "未達")
                        + "; 引継ぎ後の支持・静定 "
                        + ("記録を確認" if verification.get("launch_connected_catch_supported") is True else "未達")
                        + "。実機のキャッチを確認した結果ではありません。"
                    )
                    planning = recovery.get("capture_planning")
                    if isinstance(planning, dict) and planning.get("assessed") is True:
                        response["message"] += ("\n帰還予測で到達条件と接触用燃料を満たした計画: "
                            f"{planning['admissible_prediction_count']} / {planning['forecast_count']}件。")
                        if planning.get("best_effort_cutoff_used") is True:
                            response["message"] += " 成立計画を得られず、位置を狙う継続試行として記録しています。"
                    outcomes = verification.get("observed_outcomes", [])
                    booster = outcomes[0].get("booster", {}) if outcomes else {}
                    booster = booster if isinstance(booster, dict) else {}
                    for key, label, unit in (("return_site_distance_m", "最終目標距離", "m"),
                                              ("final_ground_speed_mps", "最終対地速度", "m/s")):
                        value = recovery.get(key, booster.get(key))
                        if type(value) in (int, float):
                            response["message"] += f" {label} {value:,.3f} {unit}。"
            names = execution.get("artifacts", [])
            if isinstance(names, list):
                prefix = (
                    f"/missionos/starship/sessions/{quote(session_id, safe='')}"
                    f"/plans/{quote(plan['id'], safe='')}/artifacts/"
                )
                response["artifact_links"] = {
                    name: prefix + name
                    for name in names
                    if name in {"report.html", "study.json", "verification.json", "manifest.json"}
                }
                if "report.html" in response["artifact_links"]:
                    response["message"] += "\nReport: " + response["artifact_links"]["report.html"]
    return response


def _blocked(text: str, action: str, session_id: str, reason: str, message: str) -> dict:
    return _response(
        text,
        action,
        {
            "schema": "missionos.starship_chat_rejection.v1",
            "status": "blocked",
            "reason": reason,
            "message": message,
            "approval": None,
            "execution": None,
            "physical_execution": False,
        },
        session_id,
    )


def maybe_handle_starship_chat(payload: Mapping[str, Any], *, expected_scenario: str | None = None) -> dict | None:
    """Handle Starship turns; return None to preserve all unrelated routing."""
    raw = payload.get(
        "operator_instruction", payload.get("instruction", payload.get("message", ""))
    )
    if isinstance(raw, Mapping):
        raw = raw.get("text", raw.get("instruction", ""))
    text = raw.strip() if isinstance(raw, str) else ""
    explicit = (expected_scenario is not None or "starship" in text.lower() or "スターシップ" in text
                or any(scenario in text.lower() for scenario in (*SIXDOF_SCENARIOS, *MANAGED_SCENARIOS))
                or (bool(payload.get("starship_context")) and any(
                    marker in text.lower() for marker in ("6dof", "sixdof", "6自由度", "六自由度", "六軸", "6軸", "キャッチ", "catch", "姿勢連続"))))
    command = COMMANDS.get(text)
    context = payload.get("starship_context")
    has_context = "starship_context" in payload
    ambiguous = bool(context) and text.lower() in AMBIGUOUS_CONFIRMATIONS
    if not explicit and command is None and not ambiguous:
        return None
    if not explicit and not has_context and payload.get("mission_designer_context"):
        # Another domain has chat focus; its commands keep their existing route.
        return None
    session_id = payload.get("session_id")
    session_id = session_id.strip() if isinstance(session_id, str) else ""
    mode = os.environ.get("MISSIONOS_STARSHIP_PLANNER_MODE", "off").strip().lower()
    if mode not in {"fixture", "deepseek"}:
        if not explicit and not has_context:
            return None
        return _blocked(
            text,
            command or "plan",
            session_id,
            "starship_planning_disabled",
            "Starship planning is disabled in this Gateway. A configured fixture or DeepSeek planner is required; no simulation was started.",
        )
    if not session_id:
        if not explicit and not has_context:
            return None
        return _blocked(
            text,
            command or "plan",
            session_id,
            "session_id_required",
            "A MissionOS chat session is required for a Starship plan and its approval.",
        )
    action = command or "plan"
    from src.intelligence.starship_mission_planner import StarshipPlannerError
    from src.runtime.starship_mission_control import StarshipMissionError

    try:
        if expected_scenario is not None and action != "plan":
            raise StarshipChatError("expected_scenario_requires_planning")
        service = get_starship_service()
        current = service.current(session_id)
        if not explicit and current is None and not has_context:
            return None
        if ambiguous:
            _validate_context(context, session_id)
            return _blocked(
                text,
                "clarification_required",
                session_id,
                "explicit_starship_command_required",
                "Starship の承認には /approve、実行には /run、状態確認には /status を入力してください。まだ承認・実行していません。",
            )
        if action == "plan":
            result = (service.plan(session_id, text, expected_scenario=expected_scenario)
                      if expected_scenario is not None else service.plan(session_id, text))
        elif action == "status":
            if context is not None:
                _validate_context(context, session_id)
            result = service.status(
                session_id, context.get("plan_id") if isinstance(context, dict) else None
            )
        else:
            _validate_context(context, session_id)
            # Never fill these references from current(): approval must identify
            # the exact plan the client displayed, not a replacement from another tab.
            result = getattr(
                service, {"approve": "approve", "execute": "execute", "reject": "reject"}[action]
            )(session_id, context["plan_id"], context["plan_sha256"])
        if not isinstance(result, dict):
            raise StarshipChatError("invalid_starship_service_response")
        return _response(text, action, result, session_id)
    except StarshipPlannerError as exc:
        response = _blocked(
            text,
            action,
            session_id,
            exc.reason,
            "Starship の計画を作成できませんでした。承認・実行は行っていません。",
        )
        response["operation_result"]["planner_invocation"] = exc.invocation
        return response
    except (StarshipChatError, StarshipMissionError) as exc:
        return _blocked(
            text,
            action,
            session_id,
            str(exc),
            "The Starship request was blocked. Review the current plan with /status, then approve that exact plan before /run.",
        )
    except Exception:
        # Provider, I/O and unexpected implementation failures can contain secret
        # request data or local paths. Only typed, sanitized failures cross chat.
        return _blocked(
            text,
            action,
            session_id,
            "starship_service_error",
            "Starship の要求を処理できませんでした。/status で状態を確認してください。",
        )


def _validate_context(context: object, session_id: str) -> None:
    if not isinstance(context, dict) or set(context) != {"plan_id", "plan_sha256", "session_id"}:
        raise StarshipChatError("displayed_starship_plan_reference_required")
    if any(not isinstance(value, str) or not value for value in context.values()):
        raise StarshipChatError("invalid_starship_plan_reference")
    if context["session_id"] != session_id:
        raise StarshipChatError("starship_context_session_mismatch")
    checksum = context["plan_sha256"]
    if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
        raise StarshipChatError("invalid_starship_plan_hash")


def handle_starship_tower_operator(payload: dict) -> dict:
    """Typed in-flight simulation choices; never enter the generic chat route."""
    action, session = payload.get("action"), payload.get("session_id")
    mutation_attempted = False
    try:
        if type(session) is not str or re.fullmatch(r"starship-operator-[a-f0-9]{24}", session) is None:
            raise StarshipChatError("invalid_starship_operator_session")
        context = payload.get("starship_context")
        _validate_context(context, session)
        base = {"action", "session_id", "starship_context", "run_id"}
        expected = base if action == "tower_status" else base | {"request_id", "request_sha256", "observed_evidence_sha256", "choice"}
        if (type(payload) is not dict or set(payload) != expected or action not in {"tower_status", "tower_resolve"}
                or type(payload["run_id"]) is not str or re.fullmatch(r"[a-f0-9]{32}", payload["run_id"]) is None):
            raise StarshipChatError("invalid_tower_operator_action")
        service = get_starship_service()
        if action == "tower_status":
            result = service.tower_status(session, context["plan_id"], context["plan_sha256"], payload["run_id"])
        else:
            if (type(payload["request_id"]) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", payload["request_id"]) is None
                    or any(type(payload[name]) is not str or re.fullmatch(r"[a-f0-9]{64}", payload[name]) is None
                           for name in ("request_sha256", "observed_evidence_sha256"))
                    or payload["choice"] not in {"continue_capture", "divert"}):
                raise StarshipChatError("invalid_tower_operator_choice")
            mutation_attempted = True
            result = service.tower_resolve(session, context["plan_id"], context["plan_sha256"], payload["run_id"],
                payload["request_id"], payload["request_sha256"], payload["observed_evidence_sha256"], payload["choice"])
        return _response("Typed tower operator action", action, result, session)
    except (StarshipChatError, ValueError):
        response = _blocked("Typed tower operator action", action, session or "", "tower_operator_refresh_required",
            "受付の確定状況は未確認です。判断を再送せず、状態を照会してください。" if mutation_attempted else
            "タワー判断を受け付けられませんでした。元の期限・最新観測・実行状態を照会してください。実行済みとは扱いません。")
    except Exception:
        response = _blocked("Typed tower operator action", action, session or "", "tower_operator_unavailable",
            "受付の確定状況は未確認です。判断を再送せず、状態を照会してください。" if mutation_attempted else
            "タワー状態を確認できませんでした。実行状態を照会してください。")
    if mutation_attempted:
        # A committed one-use ledger decision can precede IPC publication or
        # status failure. Do not assert that the mutation did not occur.
        response["operation_result"].update(mutation_outcome_uncertain=True, resend_authorized=False)
    return response
