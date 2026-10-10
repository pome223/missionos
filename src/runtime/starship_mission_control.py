"""Session-bound Starship simulation planning, approval, and process control.

Only the fixed local catalog can execute. Model output is a planning proposal;
an explicit operator command records a separate, expiring simulation grant.
No spacecraft, provider credential, or arbitrary command reaches the worker.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from hashlib import sha256
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
from threading import Lock, Thread
import time
from typing import Callable
import uuid

from .starship_replanning import SCENARIOS as M1_SCENARIOS, SCOPE as M1_SCOPE
from .starship_flight import SCENARIOS
from .starship_mission_director import SCENARIOS as MANAGED_SCENARIOS, MODE_ENV as DIRECTOR_MODE_ENV, SCOPE as DIRECTOR_SCOPE
from .starship_sixdof_catalog import (
    SIXDOF_PROFILE, SIXDOF_SCENARIOS, SIXDOF_SOURCES, SUPERVISED_SCENARIOS,
    SUPERVISION_SOURCES, FIXED_RETURN_POLICY, RETAINED_RETURN_POLICY, RETAINED_RETURN_SCENARIOS,
    CONTINUOUS_RETURN_SCENARIO, CONTINUOUS_RETURN_POLICY, CATCH_CATALOG, CATCH_PROFILE,
    LAUNCH_CATCH_SCENARIO, BOOSTER_RECOVERY_POLICY,
    catch_contract, recovery_contract, retained_return_contract, supervision_contract,
)

REPO = Path(__file__).resolve().parents[2]
CATALOG = (*SCENARIOS, "dispenser_comparison", "dispenser_jev_shadow", *SIXDOF_SCENARIOS, *MANAGED_SCENARIOS, *M1_SCENARIOS)
PLAN_TTL_S = 900
APPROVAL_TTL_S = 300
ARTIFACT_NAMES = frozenset({"report.html", "study.json", "verification.json", "manifest.json"})
BASE_SOURCES = ("src/runtime/starship_mission_control.py", "scripts/run_starship_mission_worker.py")
TOWER_CONTRACT_SCHEMA = "missionos.starship_tower_supervision_contract.v1"
TOWER_GRANT_SCOPE = "local_simulation_and_bounded_tower_supervision"
TOWER_SOURCES = (
    "src/runtime/starship_tower_supervision.py", "src/runtime/starship_tower_supervision_verifier.py",
    "src/runtime/starship_tower_broker.py", "src/runtime/starship_tower_actor.py",
    "src/runtime/starship_operator_resolution.py", "src/runtime/starship_return_sites.py",
    "src/runtime/starship_return_sites_verifier.py", "examples/spaceflight/starship-return-sites-model-test.json",
    "src/gateway/starship_chat.py", "src/gateway/server.py", "src/runtime/assets/starship_operator.html",
)
_TOWER_BROKERS = {}
_TOWER_REGISTRY_LOCK = Lock()


def tower_contract(mode="fixture", *, return_sites_sha256):
    """Future explicit-run authority; this does not add or enable a scenario."""
    from .starship_operator_resolution import SCOPE
    if (mode not in {"fixture", "live"} or type(return_sites_sha256) is not str
            or len(return_sites_sha256) != 64 or any(c not in "0123456789abcdef" for c in return_sites_sha256)):
        raise StarshipMissionError("invalid_tower_contract")
    return {"schema": TOWER_CONTRACT_SCHEMA, "scope": SCOPE, "mode": mode,
        "allowed_actions": ["continue_capture", "divert"], "maximum_routing_requests": 2,
        "maximum_observation_requests": 1, "maximum_human_requests": 1, "maximum_directives": 1,
        "maximum_jev_calls": 2 if mode == "live" else 0, "maximum_llm_calls": 0,
        "decision_expiry_s": 75., "maximum_observation_age_s": 2., "activation": "early_booster_return",
        "observation_collection_allowed": True, "human_resolution_allowed": True,
        "return_sites_sha256": return_sites_sha256, "physical_execution_authorized": False,
        "numeric_flight_authority": False}


def _plan_sources(plan):
    sources = _sources(plan["scenario"])
    if plan.get("tower_supervision") is not None:
        sources.update({name: sha256((REPO/name).read_bytes()).hexdigest() for name in TOWER_SOURCES})
    if plan.get("tower_runtime") is not None:
        from .starship_tower_runtime import runtime_sources
        sources.update(runtime_sources(REPO))
    return sources
FLIGHT_SOURCES = (
    "scripts/run_starship_3d.py", "src/runtime/starship_flight.py",
    "src/runtime/starship_physics.py", "src/runtime/starship_study.py",
    "src/runtime/starship_3d_report.py", "src/runtime/starship_entry_diagnostics.py",
    "src/runtime/runtime_claim_evidence.py",
    "examples/spaceflight/starship-flight14-observations.json",
    "src/runtime/assets/starship_cg_models.js", "src/runtime/assets/starship_cg_renderer.js",
    "src/runtime/assets/starship_cg_replay.js",
)
DISPENSER_SOURCES = (
    "scripts/run_starship_dispenser_experiment.py", "src/runtime/starship_dispenser_experiment.py",
    "src/runtime/starship_dispenser_verifier.py", "src/runtime/starship_dispenser_report.py",
)


class StarshipMissionError(ValueError):
    """An allowlisted failure reason suitable for the operator UI."""


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    temporary.replace(path)


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _sources(scenario: str) -> dict:
    if scenario in M1_SCENARIOS:
        from .starship_replanning import source_hashes
        return source_hashes()
    if scenario in MANAGED_SCENARIOS:
        from .starship_mission_director import source_hashes
        return source_hashes(REPO)
    if scenario == "dispenser_jev_shadow":
        from .starship_jev_shadow import source_hashes
        return source_hashes()
    if scenario in SIXDOF_SCENARIOS:
        return {name: sha256((REPO / name).read_bytes()).hexdigest()
                for name in BASE_SOURCES + SIXDOF_SOURCES + (SUPERVISION_SOURCES if scenario in SUPERVISED_SCENARIOS else ())}
    names = BASE_SOURCES + (DISPENSER_SOURCES if scenario == "dispenser_comparison" else FLIGHT_SOURCES)
    if scenario != "dispenser_comparison":
        # The flight verifier imports shared contract semantics via this package.
        # Bind its repository Python sources, including new files, before approval.
        names += tuple(sorted(str(path.relative_to(REPO)) for path in
                              (REPO / "packages/missionos-core/src/missionos_core").rglob("*.py")))
    return {name: sha256((REPO / name).read_bytes()).hexdigest() for name in names}


def worker_environment() -> dict[str, str]:
    """Explicit allowlist: no provider keys, cloud credentials, or user dotenv."""
    env = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
           if name in os.environ}
    env.update({"PYTHONPATH": str(REPO) + os.pathsep + str(REPO / "packages/missionos-core/src"),
                "PYTHONUNBUFFERED": "1", "MISSIONOS_LLM_BACKEND": "off"})
    return env


def process_environment(plan: dict) -> dict[str, str]:
    """Only the Jev observer broker may receive its own provider credential."""
    env = worker_environment()
    if plan["scenario"] in (*MANAGED_SCENARIOS, *M1_SCENARIOS):
        mode = plan["mission_envelope"]["mode"]
        if os.environ.get(DIRECTOR_MODE_ENV) != mode:
            raise StarshipMissionError("mission_director_configuration_changed")
        if mode == "live" and any(not os.environ.get(name, "").strip() for name in ("TYPESAFE_API_KEY", "DEEPSEEK_API_KEY")):
            raise StarshipMissionError("mission_director_credentials_required")
    if plan["scenario"] in SUPERVISED_SCENARIOS:
        mode = plan["flight_supervision"]["mode"]
        if os.environ.get("MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE") != mode:
            raise StarshipMissionError("flight_supervisor_configuration_changed")
        if mode == "live" and any(not os.environ.get(name, "").strip() for name in ("TYPESAFE_API_KEY", "DEEPSEEK_API_KEY")):
            raise StarshipMissionError("flight_supervisor_credentials_required")
    if plan["scenario"] == "dispenser_jev_shadow":
        mode = plan["jev_shadow"]["mode"]
        env["MISSIONOS_STARSHIP_JEV_SHADOW_MODE"] = mode
        if mode == "live":
            if (os.environ.get("MISSIONOS_STARSHIP_JEV_SHADOW_MODE") != "live"
                    or not os.environ.get("TYPESAFE_API_KEY", "").strip()):
                raise StarshipMissionError("jev_shadow_live_configuration_required")
            env["TYPESAFE_API_KEY"] = os.environ["TYPESAFE_API_KEY"]
    return env


def _run_simulator(args: list[str], timeout: float = 300) -> int:
    """Own a process group so a timeout also stops the flight CLI's worker."""
    process = subprocess.Popen(args, cwd=REPO, env=worker_environment(), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        _, stderr = process.communicate(timeout=timeout)
        if process.returncode:
            # Preserve bounded diagnostics in the local worker log. The child
            # receives the credential-free simulator environment; its output
            # is diagnostic data, never an instruction or a success receipt.
            sys.stderr.write("Simulator subprocess failed:\n" + stderr[-8192:].decode("utf-8", errors="replace") + "\n")
        return process.returncode
    finally:
        # The direct child may exit while a descendant still holds the pipes.
        # Always clean the owned group, including that timeout case.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()


class StarshipMissionService:
    def __init__(self, root: Path | str, *, planner: Callable | None = None,
                 clock: Callable[[], float] = time.time):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock = clock
        self.planner = planner
        with self._lock():
            key_path = self.root / "approval-signing.key"
            if not key_path.exists():
                with key_path.open("xb") as stream:
                    os.chmod(key_path, 0o600)
                    stream.write(secrets.token_bytes(32))
            self._key = key_path.read_bytes()
        if len(self._key) != 32:
            raise StarshipMissionError("invalid_local_approval_store")

    @contextmanager
    def _lock(self):
        with (self.root / ".lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _session_path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 256:
            raise StarshipMissionError("session_id_required")
        return self.root / ("session-" + sha256(session_id.encode()).hexdigest() + ".json")

    def _load(self, session_id: str) -> dict | None:
        path = self._session_path(session_id)
        return _read(path) if path.exists() else None

    def _save(self, session_id: str, state: dict) -> None:
        _write(self._session_path(session_id), state)

    def _signature(self, value: dict) -> str:
        return hmac.new(self._key, _digest(value).encode(), "sha256").hexdigest()

    def _plan_matches(self, state: dict | None, session_id: str, plan_id: str, plan_sha256: str):
        if not state:
            raise StarshipMissionError("no_current_starship_plan")
        plan = state["plan"]
        unsigned = {key: value for key, value in plan.items() if key != "sha256"}
        if (plan["id"] != plan_id or plan["sha256"] != plan_sha256
                or plan["session_id"] != session_id or _digest(unsigned) != plan_sha256):
            raise StarshipMissionError("plan_binding_mismatch")
        return plan

    def current(self, session_id: str) -> dict | None:
        with self._lock():
            state = self._load(session_id)
            return self._project(state) if state else None

    def _project(self, state: dict) -> dict:
        public = json.loads(json.dumps(state))
        if public.get("approval"):
            public["approval"].pop("signature", None)
        execution = public.get("execution")
        if execution:
            execution.pop("request_path", None)
        public["schema"] = "missionos.starship_chat_state.v1"
        public["physical_execution"] = False
        public["mission_completed"] = False
        public["starship_vehicle_validated"] = False
        public["starship_context"] = {"session_id": public["plan"]["session_id"],
                                      "plan_id": public["plan"]["id"],
                                      "plan_sha256": public["plan"]["sha256"]}
        return public

    def plan(self, session_id: str, utterance: str, *, expected_scenario: str | None = None) -> dict:
        if expected_scenario is not None and (type(expected_scenario) is not str or expected_scenario not in CATALOG):
            raise StarshipMissionError("invalid_expected_scenario")
        self._session_path(session_id)
        if not isinstance(utterance, str) or not 1 <= len(utterance) <= 2000:
            raise StarshipMissionError("invalid_operator_request")
        with self._lock():
            current = self._load(session_id)
            if current and current.get("execution", {}).get("status") == "running":
                raise StarshipMissionError("execution_in_progress")
            # Remember the revision before releasing the lock for a bounded provider call.
            previous = _digest(current) if current else None
        if self.planner is None:
            from src.intelligence.starship_mission_planner import plan_starship_request
            result = plan_starship_request(utterance, mode=os.environ.get("MISSIONOS_STARSHIP_PLANNER_MODE", "off"))
        else:
            result = self.planner(utterance)
        proposal = result.get("proposal")
        if (not isinstance(proposal, dict) or set(proposal) != {"scenario", "rationale", "uncertainties"}
                or proposal.get("scenario") not in CATALOG
                or not isinstance(proposal.get("rationale"), str) or not 1 <= len(proposal["rationale"]) <= 2000
                or not isinstance(proposal.get("uncertainties"), list)
                or any(not isinstance(x, str) or len(x) > 500 for x in proposal["uncertainties"])
                or len(proposal["uncertainties"]) > 10):
            raise StarshipMissionError("planner_proposal_rejected")
        scenario = proposal["scenario"]
        if expected_scenario is not None and (type(scenario) is not str or scenario != expected_scenario):
            # The UI's explicit selection is a restriction, not a hint that a
            # model may replace. No pending plan or approval exists for this
            # rejected proposal; an existing session revision is preserved.
            raise StarshipMissionError("planner_scenario_mismatch")
        shadow_mode = os.environ.get("MISSIONOS_STARSHIP_JEV_SHADOW_MODE", "off")
        if scenario == "dispenser_jev_shadow" and shadow_mode not in {"fixture", "live"}:
            raise StarshipMissionError("jev_shadow_not_configured")
        now = self.clock()
        plan = {"id": uuid.uuid4().hex, "session_id": session_id, "scenario": scenario,
                "backend": "synthetic_dispenser_comparison" if scenario == "dispenser_comparison" else "starship_3d_reference",
                "release_policy": "frozen_five_policy_comparison" if scenario == "dispenser_comparison" else "existing_deterministic_flight_guidance",
                "rationale": proposal["rationale"], "uncertainties": proposal["uncertainties"],
                "request_sha256": sha256(utterance.encode()).hexdigest(),
                "created_at_epoch_s": now, "expires_at_epoch_s": now + PLAN_TTL_S,
                "source_sha256": _sources(scenario), "simulation_only": True,
                "jev_authority": "none", "model_authority": "planning_only"}
        if scenario == "dispenser_jev_shadow":
            plan.update(backend="synthetic_dispenser_jev_shadow", release_policy="history_rule_unchanged",
                        jev_authority="shadow_routing_only", jev_shadow={"mode": shadow_mode,
                        "maximum_provider_calls": 22 if shadow_mode == "live" else 0,
                        "trigger": "first_decision_after_observed_retry_failure",
                        "evaluation_cases": 60, "execution_influenced": False})
        if scenario in SIXDOF_SCENARIOS:
            profile = _read(REPO / SIXDOF_PROFILE)
            plan.update(backend="starship_sixdof", release_policy="fixed_sixdof_guidance",
                        simulation={"scenario": SIXDOF_SCENARIOS[scenario],
                                    "profile": SIXDOF_PROFILE,
                                    "profile_id": profile["profile_id"],
                                    "profile_sha256": plan["source_sha256"][SIXDOF_PROFILE],
                                    "dt_scale": 1.0, "duration_override_s": None,
                                    "return_policy": CONTINUOUS_RETURN_POLICY if scenario == CONTINUOUS_RETURN_SCENARIO else RETAINED_RETURN_POLICY if scenario in RETAINED_RETURN_SCENARIOS else FIXED_RETURN_POLICY,
                                    "booster_policy": BOOSTER_RECOVERY_POLICY if scenario == LAUNCH_CATCH_SCENARIO else FIXED_RETURN_POLICY,
                                    "maximum_wall_time_s": 900 if scenario == LAUNCH_CATCH_SCENARIO else 300},
                        verification_scope="stored_sixdof_trajectory_and_outcome_consistency",
                        limitations=["開発用の6自由度モデル。SpaceX実機の精度は未検証。",
                                     "飛行誘導は固定。LLM/Jevによる飛行中の操作は行わない。",
                                     "保存記録の検証通過と帰還・回収の成功は別の事実。"])
        if scenario in SUPERVISED_SCENARIOS:
            try:
                contract = supervision_contract(os.environ.get("MISSIONOS_STARSHIP_FLIGHT_SUPERVISOR_MODE", "off"),
                    observation_collection=scenario == "sixdof_observation_supervised")
            except ValueError as exc:
                raise StarshipMissionError("flight_supervisor_not_configured") from exc
            plan.update(flight_supervision=contract, release_policy="approved_missing_effect_hold_or_skip",
                        jev_authority="routing_proposal_only", model_authority="bounded_proposal_only",
                        limitations=["6DOFを継続積分しながら、合成した放出不成立を監督します。",
                                     "許可する操作は残りの放出の見送り1回。再試行・誘導変更は対象外。",
                                     "実機の故障・復旧手順の再現やモデルの優位性は未検証。"])
        if scenario in RETAINED_RETURN_SCENARIOS:
            plan.update(return_policy=retained_return_contract(plan["simulation"]["return_policy"]),
                        limitations=["6DOFを継続積分しながら、合成した放出不成立を監督します。",
                                     "Jev/LLMの操作提案は放出見送り1回。帰還誘導は別途承認する固定アルゴリズムです。",
                                     "予定の帰還時刻に衛星保持を確認すると、現在の質量・推力・降下速度・姿勢・応答遅れから終端誘導を決めます。",
                                     "放出見送りの成否やモデル応答を帰還方針の発動条件にしません。帰還時刻・軌道離脱条件は変更しません。",
                                     "開発用の帰還方針であり、実機精度・安全な帰還・モデルの優位性は未検証。"])
        if scenario in CATCH_CATALOG:
            plan.update(booster_catch=catch_contract(), limitations=[
                "塔の近くで初期化した6自由度の捕捉試験です。打ち上げから塔への到達ではありません。",
                "支持点・アーム・剛性・摩擦・荷重限界は設定化した工学的仮定です。",
                "承認した健全性条件で有限のアームを動かし、接触力を積分して支持を検証します。",
                "タワーが使用不能なら捕捉を禁止し、あらかじめ決めた局所退避を実行します。",
                "実機キャッチ・構造強度・MissionOSによるAIの優位性は未検証です。"])
            plan["simulation"]["catch_profile_sha256"] = plan["source_sha256"][CATCH_PROFILE]
        if scenario == LAUNCH_CATCH_SCENARIO:
            plan.update(booster_recovery=recovery_contract(), limitations=[
                "打ち上げから分離したブースターの状態をそのまま帰還誘導へ渡します。",
                "位置・速度・姿勢・角速度・燃料・作動機構を初期化し直さずに積分します。",
                "実際の到達状態が条件を満たした場合だけ、有限のアームによる接触試験へ進みます。",
                "終端の接近・支持計算は最大30秒。実際の推力方向を姿勢目標に反映し、2秒間の連続支持を確認します。",
                "帰還経路の予測計算中はシミュレーション時刻を停止します。実時間の飛行制御は未検証です。",
                "機体・空力・燃料配分・誘導・支持機構には未同定の仮定があり、実機精度や到達・キャッチ成功は保証しません。",
                "Jev/LLMに誘導・点火・キャッチ指令の権限を追加しません。"])
            plan["simulation"]["catch_profile_sha256"] = plan["source_sha256"][CATCH_PROFILE]
        if scenario in MANAGED_SCENARIOS:
            from .starship_return_feasibility import readiness
            _, _, reason = readiness(_read(REPO / SIXDOF_PROFILE))
            if reason is not None:
                raise StarshipMissionError(reason)
            from .starship_mission_director import contract
            try:
                envelope = contract(os.environ.get(DIRECTOR_MODE_ENV, "off"), splashdown=scenario == "sixdof_managed_splashdown")
            except ValueError as exc:
                raise StarshipMissionError("mission_director_not_configured") from exc
            response_fault = "invalid_deployment_monitor" if scenario == "sixdof_managed_invalid_response" else None
            if scenario == "sixdof_managed_hold":
                response_fault = "hold_deployment_start"
            if response_fault and envelope["mode"] != "fixture":
                raise StarshipMissionError("fixture_response_fault_only")
            plan.update(backend="starship_mission_management", mission_envelope=envelope,
                release_policy="missionos_decides_within_approved_envelope",
                model_authority="bounded_mission_decision_candidate", jev_authority="bounded_mission_decision_candidate",
                simulation={"case": MANAGED_SCENARIOS[scenario], "profile": SIXDOF_PROFILE,
                    "response_fault": response_fault,
                    "profile_sha256": plan["source_sha256"][SIXDOF_PROFILE], "dt_scale": 1.,
                    "duration_override_s": None, "maximum_wall_time_s": 1200},
                verification_scope="decision_scope_execution_later_observation_and_same_start_comparison",
                limitations=["承認した範囲内でMissionOSが放出・帰還方式・ブースター退避を決めます。実行時に独立して範囲を検査します。",
                    "各計画は同じ開始状態の固定タイムラインと比較します。記録検証・比較合格・飛行成功は別判定です。",
                    "ShipとBoosterは独立した時計で順次積分する開発モデル。実時間の並行管制と実機精度は未検証です。",
                    "キャッチ到達条件が未認定のため退避します。退避先への安全な到達も保証しません。",
                    "fixtureは合成応答でありAI推論ではありません。人の作業量削減・AI優位性は未測定です。"])
        if scenario in M1_SCENARIOS:
            from .starship_return_feasibility import readiness
            from .starship_replanning import contract as m1_contract
            _, _, reason = readiness(_read(REPO / SIXDOF_PROFILE))
            if reason:
                raise StarshipMissionError(reason)
            try:
                envelope = m1_contract(os.environ.get(DIRECTOR_MODE_ENV, "off"))
            except ValueError as exc:
                raise StarshipMissionError("m1_not_configured") from exc
            if M1_SCENARIOS[scenario] == "decision_timeout" and envelope["mode"] != "fixture":
                raise StarshipMissionError("m1_timeout_fixture_only")
            plan.update(backend="starship_return_replanning", mission_envelope=envelope,
                release_policy="approved_26_payload_sequence_then_bounded_ai_return_control",
                model_authority="bounded_return_replanning", jev_authority="bounded_return_replanning",
                simulation={"case": M1_SCENARIOS[scenario], "profile": SIXDOF_PROFILE,
                    "profile_sha256": plan["source_sha256"][SIXDOF_PROFILE], "dt_scale": 1.,
                    "maximum_wall_time_s": 1800},
                verification_scope="launch_continuation_repeated_decisions_forecasts_fresh_dispatch_contact_and_area",
                limitations=["開発用の観測・回収区域・待機資源の仮定を承認します。実機の回収許可ではありません。",
                    "予測と実行は共有の6DOF。制約・観測不確かさ・実際の接触は別に確認します。",
                    "人は範囲を承認。Jevは帰還を判断。未応答時にも現在の状態と回収条件を検算します。"])
        plan["sha256"] = _digest(plan)
        state = {"status": "awaiting_approval", "plan": plan, "approval": None,
                 "execution": {}, "planner_invocation": result.get("invocation", {}),
                 "message": f"{scenario} のシミュレーション計画です。内容を確認して /approve、続いて /run で実行できます。実機への接続はありません。"}
        with self._lock():
            latest = self._load(session_id)
            if (_digest(latest) if latest else None) != previous:
                raise StarshipMissionError("plan_changed_during_planning")
            self._save(session_id, state)
        return self._project(state)

    def approve(self, session_id: str, plan_id: str, plan_sha256: str) -> dict:
        with self._lock():
            state = self._load(session_id)
            plan = self._plan_matches(state, session_id, plan_id, plan_sha256)
            if state["status"] != "awaiting_approval":
                raise StarshipMissionError("plan_not_awaiting_approval")
            now = self.clock()
            if now >= plan["expires_at_epoch_s"] or plan["source_sha256"] != _plan_sources(plan):
                raise StarshipMissionError("plan_expired_or_source_changed")
            if plan.get("tower_runtime") is not None:
                from .starship_tower_runtime import validate_runtime_contract
                validate_runtime_contract(plan["tower_runtime"], plan)
            grant = {"schema": "missionos.starship_simulation_approval.v1", "session_id": session_id,
                     "plan_id": plan_id, "plan_sha256": plan_sha256, "scope": "local_simulation_only",
                     "operator_command_received": True, "authenticated_operator_identity": False,
                     "created_at_epoch_s": now, "expires_at_epoch_s": min(now + APPROVAL_TTL_S, plan["expires_at_epoch_s"]),
                     "nonce": secrets.token_hex(16), "consumed_by_run": None,
                     "physical_execution_authorized": False}
            if plan["scenario"] == "dispenser_jev_shadow":
                grant["scope"] = "local_simulation_and_jev_shadow"
                grant["jev_shadow"] = plan["jev_shadow"]
            if plan["scenario"] in SUPERVISED_SCENARIOS:
                grant["scope"] = "local_simulation_and_bounded_flight_supervision"
                grant["flight_supervision"] = plan["flight_supervision"]
            if plan["scenario"] in RETAINED_RETURN_SCENARIOS:
                grant["scope"] = "local_simulation_and_bounded_flight_supervision_and_retained_return"
                grant["return_policy"] = plan["return_policy"]
            if plan["scenario"] in CATCH_CATALOG:
                grant["scope"] = "local_initialized_booster_catch_simulation"
                grant["booster_catch"] = plan["booster_catch"]
            if plan["scenario"] == LAUNCH_CATCH_SCENARIO:
                grant["scope"] = "local_launch_connected_booster_catch_simulation"
                grant["booster_recovery"] = plan["booster_recovery"]
            if plan["scenario"] in M1_SCENARIOS:
                grant["scope"] = M1_SCOPE
                grant["mission_envelope"] = plan["mission_envelope"]
            if plan["scenario"] in MANAGED_SCENARIOS:
                grant["scope"] = DIRECTOR_SCOPE
                grant["mission_envelope"] = plan["mission_envelope"]
            if plan.get("tower_supervision") is not None:
                contract = plan["tower_supervision"]
                if _digest(contract) != _digest(tower_contract(contract.get("mode"), return_sites_sha256=contract.get("return_sites_sha256"))):
                    raise StarshipMissionError("invalid_tower_contract")
                grant["scope"] = TOWER_GRANT_SCOPE
                grant["tower_supervision"] = contract
                if plan.get("tower_runtime") is not None:
                    from .starship_tower_runtime import validate_runtime_contract
                    grant["tower_runtime"] = validate_runtime_contract(plan["tower_runtime"], plan)
            grant["signature"] = self._signature(grant)
            state.update(status="approved", approval=grant,
                         message="表示した計画に対するシミュレーション承認を記録しました。まだ実行していません。/run で開始します。")
            self._save(session_id, state)
            return self._project(state)

    def reject(self, session_id: str, plan_id: str, plan_sha256: str) -> dict:
        with self._lock():
            state = self._load(session_id)
            self._plan_matches(state, session_id, plan_id, plan_sha256)
            if state["status"] not in {"awaiting_approval", "approved"}:
                raise StarshipMissionError("plan_cannot_be_rejected_in_this_state")
            state.update(status="rejected", approval=None, message="計画を却下しました。実行は開始しません。")
            self._save(session_id, state)
            return self._project(state)

    def _valid_grant(self, state: dict, *, consumed_by_run: str | None = None) -> None:
        grant = state.get("approval")
        if not isinstance(grant, dict):
            raise StarshipMissionError("explicit_plan_approval_required")
        unsigned = {key: value for key, value in grant.items() if key != "signature"}
        if not hmac.compare_digest(str(grant.get("signature", "")), self._signature(unsigned)):
            raise StarshipMissionError("approval_signature_invalid")
        plan = state["plan"]
        expiry_valid = self.clock() < grant["expires_at_epoch_s"]
        if plan.get("tower_runtime") is not None:
            from .starship_tower_runtime import validate_runtime_contract
            runtime = validate_runtime_contract(plan["tower_runtime"], plan)
            if consumed_by_run is None:
                expiry_valid = expiry_valid and not {"consumed_at_epoch_s", "execution_deadline_epoch_s"} & set(grant)
            else:
                consumed, deadline = grant.get("consumed_at_epoch_s"), grant.get("execution_deadline_epoch_s")
                now = self.clock()
                expiry_valid = (all(type(value) in (int, float) and math.isfinite(value) for value in (consumed, deadline, now))
                    and grant["created_at_epoch_s"] <= consumed < grant["expires_at_epoch_s"]
                    and deadline == consumed+runtime["maximum_wall_time_s"] and consumed <= now < deadline
                    and state.get("execution", {}).get("started_at_epoch_s") == consumed)
        if (grant["session_id"] != plan["session_id"] or grant["plan_id"] != plan["id"]
                or grant["plan_sha256"] != plan["sha256"] or grant["consumed_by_run"] != consumed_by_run
                or grant["scope"] != (M1_SCOPE if plan["scenario"] in M1_SCENARIOS else DIRECTOR_SCOPE if plan["scenario"] in MANAGED_SCENARIOS else TOWER_GRANT_SCOPE if plan.get("tower_supervision") is not None else "local_launch_connected_booster_catch_simulation" if plan["scenario"] == LAUNCH_CATCH_SCENARIO else "local_initialized_booster_catch_simulation" if plan["scenario"] in CATCH_CATALOG else "local_simulation_and_bounded_flight_supervision_and_retained_return" if plan["scenario"] in RETAINED_RETURN_SCENARIOS else "local_simulation_and_bounded_flight_supervision" if plan["scenario"] in SUPERVISED_SCENARIOS else "local_simulation_and_jev_shadow" if plan["scenario"] == "dispenser_jev_shadow" else "local_simulation_only")
                or grant.get("mission_envelope") != plan.get("mission_envelope")
                or grant.get("jev_shadow") != plan.get("jev_shadow")
                or grant.get("flight_supervision") != plan.get("flight_supervision")
                or grant.get("return_policy") != plan.get("return_policy")
                or grant.get("booster_catch") != plan.get("booster_catch")
                or grant.get("booster_recovery") != plan.get("booster_recovery")
                or grant.get("tower_supervision") != plan.get("tower_supervision")
                or grant.get("tower_runtime") != plan.get("tower_runtime")
                or grant["physical_execution_authorized"] is not False
                or not expiry_valid):
            raise StarshipMissionError("approval_binding_expired_or_consumed")
        if plan["source_sha256"] != _plan_sources(plan):
            raise StarshipMissionError("approved_source_changed")
        if plan["scenario"] in M1_SCENARIOS:
            from .starship_return_feasibility import readiness
            from .starship_replanning import contract as m1_contract
            _, _, reason = readiness(_read(REPO / SIXDOF_PROFILE))
            if reason or plan["mission_envelope"] != m1_contract(plan["mission_envelope"]["mode"]):
                raise StarshipMissionError(reason or "m1_envelope_not_current")
        if plan["scenario"] in MANAGED_SCENARIOS:
            from .starship_return_feasibility import readiness
            _, _, reason = readiness(_read(REPO / SIXDOF_PROFILE))
            if reason is not None:
                raise StarshipMissionError(reason)
            from .starship_mission_director import contract as director_contract
            try:
                current = director_contract(plan["mission_envelope"]["mode"], splashdown=plan["scenario"] == "sixdof_managed_splashdown")
            except (ValueError, KeyError, TypeError) as exc:
                raise StarshipMissionError("mission_envelope_not_current") from exc
            if plan["mission_envelope"] != current:
                raise StarshipMissionError("mission_envelope_not_current")

    def execute(self, session_id: str, plan_id: str, plan_sha256: str) -> dict:
        with self._lock():
            state = self._load(session_id)
            self._plan_matches(state, session_id, plan_id, plan_sha256)
            if state["status"] != "approved":
                raise StarshipMissionError("explicit_plan_approval_required")
            self._valid_grant(state)
            if state["plan"].get("tower_supervision") is not None:
                if state["plan"].get("tower_runtime") is not None:
                    return self._execute_tower_runtime(session_id, state)
                # A future explicit runtime must register its actual actor and
                # broker. Never send a broader contract to the legacy worker.
                raise StarshipMissionError("tower_runtime_not_connected")
            environment = process_environment(state["plan"])
            run_id = uuid.uuid4().hex
            run_dir = self.root / ("run-" + run_id)
            run_dir.mkdir(mode=0o700)
            if state["plan"]["scenario"] in SUPERVISED_SCENARIOS or state["plan"]["scenario"] in (*MANAGED_SCENARIOS, *M1_SCENARIOS):
                (run_dir / "supervision").mkdir(mode=0o700)
            state["approval"]["consumed_by_run"] = run_id
            unsigned = {key: value for key, value in state["approval"].items() if key != "signature"}
            state["approval"]["signature"] = self._signature(unsigned)
            request = {"schema": "missionos.starship_worker_request.v1", "session_id": session_id,
                       "plan_id": plan_id, "plan_sha256": plan_sha256, "run_id": run_id}
            _write(run_dir / "request.json", request)
            state.update(status="running", execution={"run_id": run_id, "status": "running",
                         "started_at_epoch_s": self.clock(), "subprocess_spawned": False,
                         "worker_receipt_verified": False, "physical_execution": False},
                         message="承認済み計画のシミュレーションを別プロセスで開始します。/status で確認できます。")
            self._save(session_id, state)
            try:
                with (run_dir / "worker.log").open("xb") as log:
                    child = subprocess.Popen(
                        [sys.executable, str(REPO / "scripts/run_starship_mission_worker.py"),
                         "--state-dir", str(self.root), "--run-id", run_id],
                        cwd=REPO, env=environment, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                state["execution"].update(worker_pid=child.pid, subprocess_spawned=True)
            except OSError:
                state.update(status="failed", message="ワーカープロセスを開始できませんでした。承認は再利用できません。")
                state["execution"].update(status="failed", failure_reason="worker_spawn_failed")
            self._save(session_id, state)
            if state["execution"].get("subprocess_spawned"):
                if state["plan"]["scenario"] in M1_SCENARIOS:
                    from src.intelligence.starship_replanning import serve
                    Thread(target=serve, args=(run_dir / "supervision", state["plan"]["mission_envelope"], child,
                        lambda: state["plan"]["source_sha256"] == _sources(state["plan"]["scenario"])), daemon=True).start()
                if state["plan"]["scenario"] in MANAGED_SCENARIOS:
                    from src.intelligence.starship_mission_director import serve_director
                    Thread(target=serve_director, args=(run_dir / "supervision", state["plan"]["mission_envelope"], child,
                        lambda: state["plan"]["source_sha256"] == _sources(state["plan"]["scenario"])), daemon=True).start()
                if state["plan"]["scenario"] in SUPERVISED_SCENARIOS:
                    from .starship_flight_broker import serve_flight_request
                    Thread(target=serve_flight_request, args=(run_dir / "supervision", run_id,
                           state["plan"]["flight_supervision"]["mode"], child,
                           lambda: state["plan"]["source_sha256"] == _sources(state["plan"]["scenario"])),
                           kwargs={"observation_collection": state["plan"]["scenario"] == "sixdof_observation_supervised"}, daemon=True).start()
                Thread(target=self._observe_exit, args=(session_id, run_id, child), daemon=True).start()
            return self._project(state)

    def _execute_tower_runtime(self, session_id, state):
        """Called under the store lock; only an explicitly approved new route."""
        from .starship_tower_runtime import validate_runtime_contract
        plan = state["plan"]
        contract = validate_runtime_contract(plan["tower_runtime"], plan)
        if os.environ.get("MISSIONOS_STARSHIP_TOWER_RUNTIME_MODE") != "fixture":
            raise StarshipMissionError("tower_runtime_explicit_fixture_configuration_required")
        lease_path = self.root/"tower-active-run.json"
        if lease_path.exists() or lease_path.is_symlink():
            raise StarshipMissionError("another_tower_run_lease_exists")
        run_id = uuid.uuid4().hex
        disable_driver = os.environ.get("MISSIONOS_STARSHIP_TOWER_RUNTIME_DISABLE_DRIVER") == "1"
        run_dir = self.root/("run-"+run_id)
        run_dir.mkdir(mode=0o700)
        (run_dir/"tower").mkdir(mode=0o700)
        with (run_dir/"tower-integrity.key").open("xb") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(secrets.token_bytes(32))
        context = {"session_id": session_id, "plan_id": plan["id"], "plan_sha256": plan["sha256"],
                   "run_id": run_id, "request_id": "tower-"+run_id}
        reservation = {"schema": "missionos.starship_tower_run_reservation.v1", "context": context,
            "source_sha256": _digest(plan["source_sha256"]),
            "return_sites_sha256": contract["return_sites_sha256"], "actor_pid": None,
            "runtime_driver_disabled": disable_driver}
        reservation["signature"] = self._signature(reservation)
        # The store flock makes this reservation exclusive across host service
        # instances BEFORE any actual actor process can be spawned.
        _write(lease_path, reservation)
        consumed_at = self.clock()
        state["approval"]["consumed_by_run"] = run_id
        state["approval"]["consumed_at_epoch_s"] = consumed_at
        state["approval"]["execution_deadline_epoch_s"] = consumed_at+contract["maximum_wall_time_s"]
        state["approval"]["signature"] = self._signature({k: v for k, v in state["approval"].items() if k != "signature"})
        reservation["approval_record_sha256"] = _digest(state["approval"])
        reservation["signature"] = self._signature({k: v for k, v in reservation.items() if k != "signature"})
        _write(lease_path, reservation)
        _write(run_dir/"request.json", {"schema": "missionos.starship_worker_request.v1", "session_id": session_id,
            "plan_id": plan["id"], "plan_sha256": plan["sha256"], "run_id": run_id,
            "runtime_driver_disabled": disable_driver})
        state.update(status="running", execution={"run_id": run_id, "status": "running",
            "started_at_epoch_s": consumed_at, "subprocess_spawned": False, "worker_receipt_verified": False,
            "process_role": "credential_free_tower_simulation_worker", "physical_execution": False,
            "tower_supervision_registered": False, "tower_runtime_policy": contract["policy_id"],
            "runtime_driver_disabled": disable_driver},
            message="専用ワーカーを開始します。実際の帰還制御・追加観測・退避の成立は、後続の保存記録で別々に確認します。")
        self._save(session_id, state)
        try:
            with (run_dir/"worker.log").open("xb") as log:
                args = [sys.executable, str(REPO/"scripts/run_starship_tower_mission_worker.py"),
                    "--state-dir", str(self.root), "--run-id", run_id]
                if disable_driver:
                    args.append("--disable-driver")
                child = subprocess.Popen(args, cwd=REPO, env=worker_environment(),
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            state["execution"].update(worker_pid=child.pid, subprocess_spawned=True)
            reservation["actor_pid"] = child.pid
            reservation["signature"] = self._signature({k: v for k, v in reservation.items() if k != "signature"})
            _write(lease_path, reservation)
        except OSError:
            state.update(status="failed", message="専用ワーカーを開始できませんでした。承認は再利用できません。")
            state["execution"].update(status="failed", failure_reason="worker_spawn_failed")
            lease_path.unlink()
        self._save(session_id, state)
        if state["execution"].get("subprocess_spawned"):
            Thread(target=self._connect_tower_runtime, args=(session_id, plan["id"], plan["sha256"], run_id, child), daemon=True).start()
            remaining = min(contract["maximum_wall_time_s"], max(0., state["approval"]["execution_deadline_epoch_s"]-self.clock()))
            Thread(target=self._limit_tower_runtime, args=(child, remaining), daemon=True).start()
            Thread(target=self._observe_exit, args=(session_id, run_id, child), daemon=True).start()
        return self._project(state)

    @staticmethod
    def _limit_tower_runtime(process, maximum_wall_time_s):
        try:
            process.wait(timeout=maximum_wall_time_s)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2.)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _connect_tower_runtime(self, session_id, plan_id, plan_sha256, run_id, process):
        """Host-only broker registration after fresh actual driver activation."""
        from .starship_tower_runtime import read_activation, read_local_key, FixtureTowerJudge, validate_runtime_contract
        from .starship_tower_broker import TowerBroker
        from .starship_operator_resolution import OperatorResolutionLedger
        run_dir = self.root/("run-"+run_id)
        try:
            key = read_local_key(run_dir)
            with self._lock():
                state, _ = self._tower_run(session_id, plan_id, plan_sha256, run_id)
                plan, approval = state["plan"], state["approval"]
                contract = validate_runtime_contract(plan["tower_runtime"], plan)
            remaining = min(contract["maximum_wall_time_s"], max(0., approval["execution_deadline_epoch_s"]-self.clock()))
            deadline = time.monotonic()+remaining
            while process.poll() is None and time.monotonic() < deadline:
                if plan["source_sha256"] != _plan_sources(plan):
                    return
                if (run_dir/"tower-activation.json").exists():
                    activation = read_activation(run_dir, plan=plan, approval=approval, run_id=run_id,
                        process_pid=process.pid, local_integrity_key=key)
                    broker = TowerBroker(run_dir/"tower", run_id, activation["scope"], process,
                        OperatorResolutionLedger(key, store_path=run_dir/"tower-resolution.sqlite3"),
                        FixtureTowerJudge(), lambda: plan["source_sha256"] == _plan_sources(plan))
                    self.register_tower_broker(session_id, plan_id, plan_sha256, run_id, broker=broker)
                    broker.serve()
                    return
                time.sleep(.05)
        except (OSError, ValueError, KeyError, TypeError):
            # No retry, second broker or provider-budget refill. A physical
            # driver must retain its original deadline/divert fallback.
            return

    def _valid_tower_reservation(self, state, run_id, process_pid):
        """The direct child must own the pre-spawn reservation before work."""
        path = self.root/"tower-active-run.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise StarshipMissionError("tower_worker_reservation_unavailable")
        lease = _read(path)
        fields = {"schema", "context", "source_sha256", "return_sites_sha256", "actor_pid", "approval_record_sha256",
                  "runtime_driver_disabled", "signature"}
        plan = state["plan"]
        context = {"session_id": plan["session_id"], "plan_id": plan["id"], "plan_sha256": plan["sha256"],
            "run_id": run_id, "request_id": "tower-"+run_id}
        if (type(lease) is not dict or set(lease) != fields
                or lease["schema"] != "missionos.starship_tower_run_reservation.v1"
                or lease["context"] != context or lease["actor_pid"] != process_pid
                or lease["source_sha256"] != _digest(plan["source_sha256"])
                or lease["return_sites_sha256"] != plan["tower_runtime"]["return_sites_sha256"]
                or lease["approval_record_sha256"] != _digest(state["approval"])
                or type(lease["runtime_driver_disabled"]) is not bool
                or lease["runtime_driver_disabled"] is not state["execution"].get("runtime_driver_disabled")
                or not hmac.compare_digest(str(lease["signature"]),
                    self._signature({key: value for key, value in lease.items() if key != "signature"}))):
            raise StarshipMissionError("tower_worker_reservation_binding_mismatch")

    def _tower_run(self, session_id, plan_id, plan_sha256, run_id):
        """Called under the existing store lock; no client context is inferred."""
        state = self._load(session_id)
        plan = self._plan_matches(state, session_id, plan_id, plan_sha256)
        execution = state.get("execution", {})
        if (type(run_id) is not str or len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id)
                or state["status"] != "running" or execution.get("status") != "running"
                or execution.get("run_id") != run_id or execution.get("subprocess_spawned") is not True):
            raise StarshipMissionError("tower_run_binding_not_active")
        contract = plan.get("tower_supervision")
        if (type(contract) is not dict or _digest(contract) != _digest(tower_contract(
                contract.get("mode"), return_sites_sha256=contract.get("return_sites_sha256")))):
            raise StarshipMissionError("approved_tower_supervision_scope_required")
        self._valid_grant(state, consumed_by_run=run_id)
        if plan.get("tower_runtime") is not None and execution.get("tower_supervision_registered") is not True:
            self._valid_tower_reservation(state, run_id, execution.get("worker_pid"))
        return state, contract

    def register_tower_broker(self, session_id, plan_id, plan_sha256, run_id, *, broker):
        """Future authorized launcher registration; never a browser/model action."""
        from .starship_tower_broker import TowerBroker
        from .starship_tower_actor import verify_actor_identity
        from .starship_tower_supervision import _scope
        if type(broker) is not TowerBroker:
            raise StarshipMissionError("typed_tower_broker_required")
        with self._lock():
            state, contract = self._tower_run(session_id, plan_id, plan_sha256, run_id)
            if state["execution"].get("runtime_driver_disabled") is True:
                raise StarshipMissionError("tower_runtime_driver_disabled")
            scope = json.loads(json.dumps(broker._scope))
            _scope(scope)
            context = scope["context"]
            run_dir = self.root/("run-"+run_id)
            if (context["session_id"] != session_id or context["plan_id"] != plan_id
                    or context["plan_sha256"] != plan_sha256 or context["run_id"] != run_id
                    or scope["source_sha256"] != _digest(state["plan"]["source_sha256"])
                    or scope["approval_record_sha256"] != _digest(state["approval"])
                    or scope["maximum_observation_age_s"] != contract["maximum_observation_age_s"]
                    or scope["observation_collection_allowed"] is not contract["observation_collection_allowed"]
                    or scope["human_resolution_allowed"] is not contract["human_resolution_allowed"]
                    or scope["original_simulation_deadline_s"] > scope["issued_simulation_time_s"]+contract["decision_expiry_s"]
                    or scope["original_wall_deadline_s"] > scope["issued_wall_time_s"]+contract["decision_expiry_s"]
                    or broker._router._mode != contract["mode"]
                    or broker._root.resolve() != run_dir/"tower"
                    or Path(broker._ledger._store_path).resolve() != run_dir/"tower-resolution.sqlite3"
                    or not broker._alive() or broker._clock() >= scope["original_wall_deadline_s"]):
                raise StarshipMissionError("tower_broker_scope_binding_mismatch")
            pid = getattr(broker._process, "pid", None)
            if type(pid) is not int or pid <= 0:
                raise StarshipMissionError("tower_actor_process_identity_required")
            identity_path = run_dir/"tower"/"actor-identity.json"
            if identity_path.is_symlink() or not identity_path.is_file() or identity_path.stat().st_size > 65536:
                raise StarshipMissionError("signed_tower_actor_identity_required")
            identity = _read(identity_path)
            if (identity.get("actor_pid") != pid or not verify_actor_identity(identity, expected_scope=scope,
                    expected_sites_sha256=contract["return_sites_sha256"],
                    local_integrity_key=broker._ledger._signing_key)["passed"]):
                raise StarshipMissionError("tower_actor_identity_binding_invalid")
            key = str(self.root), run_id
            with _TOWER_REGISTRY_LOCK:
                if key in _TOWER_BROKERS:
                    raise StarshipMissionError("tower_broker_already_registered")
                lease_path = self.root/"tower-active-run.json"
                if lease_path.exists() or lease_path.is_symlink():
                    reservation = _read(lease_path) if not lease_path.is_symlink() and lease_path.is_file() else {}
                    unsigned = {key: value for key, value in reservation.items() if key != "signature"}
                    if (state["plan"].get("tower_runtime") is None
                            or reservation.get("schema") != "missionos.starship_tower_run_reservation.v1"
                            or reservation.get("context") != context or reservation.get("actor_pid") != pid
                            or reservation.get("source_sha256") != scope["source_sha256"]
                            or reservation.get("return_sites_sha256") != contract["return_sites_sha256"]
                            or reservation.get("approval_record_sha256") != _digest(state["approval"])
                            or not hmac.compare_digest(str(reservation.get("signature", "")), self._signature(unsigned))):
                        # Unknown ownership after restart cannot safely reserve
                        # another flight or refill a provider budget.
                        raise StarshipMissionError("another_tower_run_lease_exists")
                lease = {"schema": "missionos.starship_tower_run_lease.v1", "context": context,
                    "scope_sha256": _digest(scope), "actor_pid": pid, "source_sha256": scope["source_sha256"],
                    "return_sites_sha256": contract["return_sites_sha256"], "actor_identity_sha256": _digest(identity)}
                lease["signature"] = self._signature(lease)
                _write(lease_path, lease)
                _TOWER_BROKERS[key] = {"broker": broker, "context": context, "scope_sha256": _digest(scope),
                                      "actor_pid": pid, "actor_identity_sha256": _digest(identity), "released": False}
            state["execution"]["tower_supervision_registered"] = True
            state["execution"]["tower_actor_pid"] = pid
            self._save(session_id, state)
        Thread(target=self._observe_tower_exit, args=(run_id, broker._process), daemon=True).start()
        return {"registered": True, "run_id": run_id, "physical_execution": False}

    def _observe_tower_exit(self, run_id, process):
        process.wait()
        with self._lock(), _TOWER_REGISTRY_LOCK:
            entry = _TOWER_BROKERS.get((str(self.root), run_id))
            if entry is None or entry["broker"]._process is not process:
                return
            path = self.root/"tower-active-run.json"
            if path.exists() and not path.is_symlink():
                lease = _read(path)
                unsigned = {key: value for key, value in lease.items() if key != "signature"}
                if (lease.get("context") == entry["context"] and lease.get("actor_pid") == entry["actor_pid"]
                        and hmac.compare_digest(str(lease.get("signature", "")), self._signature(unsigned))):
                    path.unlink()
            entry["released"] = True

    def _tower_entry(self, state, run_id):
        with _TOWER_REGISTRY_LOCK:
            entry = _TOWER_BROKERS.get((str(self.root), run_id))
        if entry is None or entry["released"]:
            raise StarshipMissionError("tower_broker_not_registered_or_ended")
        if (entry["context"]["session_id"] != state["plan"]["session_id"]
                or entry["context"]["plan_id"] != state["plan"]["id"]
                or entry["context"]["plan_sha256"] != state["plan"]["sha256"]
                or entry["scope_sha256"] != _digest(entry["broker"]._scope)
                or entry["broker"]._scope["approval_record_sha256"] != _digest(state["approval"])
                or entry["broker"]._process.pid != entry["actor_pid"]):
            raise StarshipMissionError("tower_registry_context_changed")
        lease_path = self.root/"tower-active-run.json"
        if lease_path.is_symlink() or not lease_path.is_file():
            raise StarshipMissionError("tower_run_lease_unavailable")
        lease = _read(lease_path)
        identity_path = self.root/("run-"+run_id)/"tower"/"actor-identity.json"
        if (identity_path.is_symlink() or not identity_path.is_file() or identity_path.stat().st_size > 65536
                or _digest(_read(identity_path)) != entry["actor_identity_sha256"]):
            raise StarshipMissionError("registered_tower_actor_identity_changed")
        if (lease.get("context") != entry["context"] or lease.get("scope_sha256") != entry["scope_sha256"]
                or lease.get("actor_pid") != entry["actor_pid"] or lease.get("actor_identity_sha256") != entry["actor_identity_sha256"]
                or not hmac.compare_digest(
                    str(lease.get("signature", "")), self._signature({key: value for key, value in lease.items() if key != "signature"}))):
            raise StarshipMissionError("tower_run_lease_binding_invalid")
        return entry

    def tower_status(self, session_id, plan_id, plan_sha256, run_id):
        with self._lock():
            state, _ = self._tower_run(session_id, plan_id, plan_sha256, run_id)
            entry = self._tower_entry(state, run_id)
            pending = entry["broker"].pending()
            pending["server_wall_time_s"] = entry["broker"]._clock()
            pending["maximum_observation_age_s"] = entry["broker"]._scope["maximum_observation_age_s"]
            # HMACs and private paths are unnecessary in the browser. The
            # broker consumes its own authoritative signed SQLite records.
            for name in ("request", "decision"):
                if pending.get(name):
                    pending[name].pop("signature", None)
            result = self._project(state)
            result["tower_pending"] = pending
            result["message"] = "現在のタワー観測と、承認済み範囲の判断待ちを表示します。指示の適用や飛行成功とは別です。"
            return result

    def tower_resolve(self, session_id, plan_id, plan_sha256, run_id, request_id, request_sha256, observed_evidence_sha256, choice):
        with self._lock():
            state, _ = self._tower_run(session_id, plan_id, plan_sha256, run_id)
            entry = self._tower_entry(state, run_id)
            if request_id != entry["context"]["request_id"]:
                raise StarshipMissionError("tower_request_context_mismatch")
            try:
                entry["broker"].resolve(context=entry["context"], request_sha256=request_sha256,
                    observed_evidence_sha256=observed_evidence_sha256, action=choice)
            except ValueError:
                raise StarshipMissionError("tower_resolution_rejected_refresh_required") from None
        return self.tower_status(session_id, plan_id, plan_sha256, run_id)

    def _observe_exit(self, session_id: str, run_id: str, child: subprocess.Popen) -> None:
        """Reap the child and record a process failure without inventing a worker receipt."""
        exit_code = child.wait()
        with self._lock():
            state = self._load(session_id)
            if not state or state.get("execution", {}).get("run_id") != run_id:
                return
            execution = state["execution"]
            execution.update(process_exit_observed=True, process_exit_code=exit_code)
            reservation_path = self.root/"tower-active-run.json"
            if execution.get("tower_runtime_policy") and reservation_path.is_file() and not reservation_path.is_symlink():
                reservation = _read(reservation_path)
                unsigned = {key: value for key, value in reservation.items() if key != "signature"}
                if (reservation.get("schema") == "missionos.starship_tower_run_reservation.v1"
                        and reservation.get("context", {}).get("run_id") == run_id
                        and reservation.get("actor_pid") == child.pid
                        and hmac.compare_digest(str(reservation.get("signature", "")), self._signature(unsigned))):
                    reservation_path.unlink()
            if execution.get("status") == "running" and not (self.root / ("run-" + run_id) / "result.json").exists():
                execution.update(status="failed", failure_reason="worker_exited_without_receipt")
                state.update(status="failed", message="ワーカーが検証記録を残さず終了しました。完了とは扱いません。")
            self._save(session_id, state)

    def status(self, session_id: str, plan_id: str | None = None) -> dict:
        with self._lock():
            state = self._load(session_id)
            if not state or (plan_id is not None and state["plan"]["id"] != plan_id):
                raise StarshipMissionError("no_matching_starship_plan")
            execution = state.get("execution", {})
            if execution.get("status") == "running":
                result_path = self.root / ("run-" + execution["run_id"]) / "result.json"
                if result_path.exists():
                    receipt = _read(result_path)
                    expected = self._signature({key: value for key, value in receipt.items() if key != "signature"})
                    if (not hmac.compare_digest(str(receipt.get("signature", "")), expected)
                            or receipt.get("run_id") != execution["run_id"]
                            or receipt.get("plan_sha256") != state["plan"]["sha256"]
                            or receipt.get("worker_pid") != execution.get("worker_pid")):
                        state.update(status="failed", message="ワーカー記録の結合検証に失敗しました。")
                        execution.update(status="failed", failure_reason="worker_receipt_binding_invalid")
                    else:
                        result_status = "verified" if receipt["verification_passed"] else "failed"
                        if (state["plan"]["scenario"] in M1_SCENARIOS
                                and result_status == "verified"
                                and not receipt["verification"].get("case_accepted", False)):
                            result_status = "unresolved"
                        execution.update(status=result_status, worker_receipt_verified=True,
                                         verification=receipt["verification"], artifact_sha256=receipt["artifact_sha256"],
                                         provider_credentials_present=receipt["provider_credentials_present"],
                                         ended_at_epoch_s=receipt["ended_at_epoch_s"], failure_reason=receipt.get("failure_reason"))
                        if "process_role" in receipt:
                            execution["process_role"] = receipt["process_role"]
                            execution["simulator_provider_credentials_present"] = receipt.get("simulator_provider_credentials_present")
                        state.update(status=result_status, message=(
                            "シミュレーションの保存記録を検証しました。実機の再現成功や衛星サービス開始を示すものではありません。"
                            if result_status == "verified" else
                            "保存記録の検証は通過しましたが、この便の帰還・判断の達成条件は未達です。未解決として記録します。"
                            if result_status == "unresolved" else "シミュレーションまたは保存記録の検証に失敗しました。"))
                        if result_status in {"verified", "unresolved"}:
                            execution["artifacts"] = sorted(receipt["artifact_sha256"])
                    self._save(session_id, state)
                elif self.clock() - execution["started_at_epoch_s"] > max(600, state["plan"].get("simulation", {}).get("maximum_wall_time_s", 300)+300):
                    execution.update(status="failed", failure_reason="worker_receipt_timeout")
                    state.update(status="failed", message="期限内にワーカーの結果が届きませんでした。実行完了とは扱いません。")
                    self._save(session_id, state)
            return self._project(state)

    def read_artifact(self, session_id: str, plan_id: str, name: str) -> Path:
        if name not in ARTIFACT_NAMES:
            raise StarshipMissionError("artifact_not_allowlisted")
        state = self.status(session_id, plan_id)
        execution = state["execution"]
        if (state["status"] not in {"verified", "unresolved"}
                or not execution.get("worker_receipt_verified")
                or name not in execution.get("artifact_sha256", {})):
            raise StarshipMissionError("verified_artifact_unavailable")
        path = self.root / ("run-" + execution["run_id"]) / "results" / name
        if (path.is_symlink() or not path.resolve().is_relative_to(self.root)
                or sha256(path.read_bytes()).hexdigest() != execution["artifact_sha256"][name]):
            raise StarshipMissionError("saved_artifact_changed")
        return path


def get_starship_service() -> StarshipMissionService:
    return StarshipMissionService(os.environ.get("MISSIONOS_STARSHIP_STATE_DIR", "output/starship-missions"))


def execute_worker(state_dir: Path, run_id: str) -> int:
    """Single-consumption worker. Only its signed receipt can finish a chat run."""
    if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise StarshipMissionError("invalid_run_id")
    service = StarshipMissionService(state_dir)
    run_dir = service.root / ("run-" + run_id)
    with (run_dir / "worker.lock").open("x"):
        request = _read(run_dir / "request.json")
        with service._lock():
            state = service._load(request["session_id"])
            plan = service._plan_matches(state, request["session_id"], request["plan_id"], request["plan_sha256"])
            service._valid_grant(state, consumed_by_run=run_id)
            if state["status"] != "running" or state["execution"]["run_id"] != run_id:
                raise StarshipMissionError("worker_not_authorized")
            if plan.get("tower_runtime") is not None:
                raise StarshipMissionError("tower_runtime_requires_direct_worker")
        result = {"schema": "missionos.starship_worker_receipt.v1", "run_id": run_id,
                  "plan_sha256": plan["sha256"], "worker_pid": os.getpid(),
                  "verification_passed": False, "verification": {}, "artifact_sha256": {},
                  "provider_credentials_present": any(name in os.environ for name in
                      ("DEEPSEEK_API_KEY", "TYPESAFE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "JEV_API_KEY")),
                  "physical_execution": False}
        try:
            shadow = plan["scenario"] == "dispenser_jev_shadow"
            allowed_keys = {"TYPESAFE_API_KEY"} if shadow and plan["jev_shadow"]["mode"] == "live" else set()
            if any(name in os.environ for name in
                   {"DEEPSEEK_API_KEY", "TYPESAFE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "JEV_API_KEY"} - allowed_keys):
                raise StarshipMissionError("provider_credentials_in_worker")
            output = run_dir / "results"
            if plan["scenario"] in M1_SCENARIOS:
                from .starship_replanning_verifier import verify
                args = [sys.executable, str(REPO/"scripts/run_starship_m1.py"), "--approve-simulation",
                    "--case", plan["simulation"]["case"], "--mode", plan["mission_envelope"]["mode"],
                    "--mailbox", str(run_dir/"supervision"), "--run-id", run_id, "--output-dir", str(output)]
                code = _run_simulator(args, timeout=plan["simulation"]["maximum_wall_time_s"])
                result["simulator_process_returncode"] = code
                if code:
                    raise StarshipMissionError("m1_simulator_process_failed")
                study = _read(output/"study.json")
                if (study["profile"] != _read(REPO/SIXDOF_PROFILE)
                        or plan["source_sha256"] != _sources(plan["scenario"])):
                    raise StarshipMissionError("m1_execution_input_binding_mismatch")
                verdict = verify(study, expected_envelope=plan["mission_envelope"],
                    expected_case=plan["simulation"]["case"], expected_sources=plan["source_sha256"])
                _write(output/"verification.json", verdict)
                manifest = _read(output/"manifest.json")
                manifest["files"]["verification.json"] = sha256((output/"verification.json").read_bytes()).hexdigest()
                _write(output/"manifest.json", manifest)
                result.update(verification=verdict, verification_passed=verdict["passed"],
                    artifact_sha256={name:sha256((output/name).read_bytes()).hexdigest() for name in ARTIFACT_NAMES},
                    ended_at_epoch_s=service.clock())
                result["signature"] = service._signature(result)
                _write(run_dir/"result.json", result)
                return 0 if verdict["passed"] else 2
            if plan["scenario"] in MANAGED_SCENARIOS:
                from .starship_mission_director_verifier import verify
                args = [sys.executable, str(REPO/"scripts/run_starship_managed_mission.py"), "--approve-simulation",
                    "--case", plan["simulation"]["case"], "--director-mode", plan["mission_envelope"]["mode"],
                    "--mailbox", str(run_dir/"supervision"), "--run-id", run_id, "--output-dir", str(output)]
                response_fault = plan["simulation"].get("response_fault")
                if response_fault:
                    args.extend(["--response-fault", response_fault])
                if plan["scenario"] == "sixdof_managed_splashdown":
                    args.append("--splashdown")
                code = _run_simulator(args, timeout=plan["simulation"]["maximum_wall_time_s"])
                result["simulator_process_returncode"] = code
                if code:
                    raise StarshipMissionError("managed_simulator_process_failed")
                study = _read(output/"study.json")
                if (study["provenance"]["source_sha256"] != plan["source_sha256"]
                        or plan["source_sha256"] != _sources(plan["scenario"])
                        or study["profile"] != _read(REPO/SIXDOF_PROFILE)
                        or study["return_sites"] != _read(REPO/"examples/spaceflight/starship-return-sites-model-test.json")
                        or study["provenance"]["dt_scale"] != 1.
                        or study["provenance"]["duration_override_s"] is not None):
                    raise StarshipMissionError("managed_execution_input_binding_mismatch")
                if study.get("response_fault") != response_fault:
                    raise StarshipMissionError("managed_response_fault_binding_mismatch")
                verdict = verify(study, expected_case=plan["simulation"]["case"],
                                 expected_envelope=plan["mission_envelope"], expected_run_id=run_id,
                                 expected_response_fault=response_fault)
                _write(output/"verification.json", verdict)
                manifest = _read(output/"manifest.json")
                manifest["files"]["verification.json"] = sha256((output/"verification.json").read_bytes()).hexdigest()
                _write(output/"manifest.json", manifest)
                result.update(verification=verdict, verification_passed=verdict["passed"],
                    artifact_sha256={name: sha256((output/name).read_bytes()).hexdigest() for name in ARTIFACT_NAMES},
                    ended_at_epoch_s=service.clock())
                result["signature"] = service._signature(result)
                _write(run_dir/"result.json", result)
                return 0 if verdict["passed"] else 2
            if shadow:
                from .starship_jev_shadow import run_shadow
                result["process_role"] = "jev_shadow_observer_broker"
                verdict = run_shadow(output, mode=plan["jev_shadow"]["mode"])
                result["verification"] = verdict
                result["verification_passed"] = verdict["verified"] is True
                bundle = _read(output / "study.json")
                result["simulator_provider_credentials_present"] = bundle["shadow"]["worker"]["provider_credentials_present"]
                if plan["source_sha256"] != _sources(plan["scenario"]):
                    raise StarshipMissionError("approved_source_changed_during_execution")
                result["artifact_sha256"] = {name: sha256((output / name).read_bytes()).hexdigest()
                                             for name in ARTIFACT_NAMES if (output / name).is_file()}
                result["ended_at_epoch_s"] = service.clock()
                result["signature"] = service._signature(result)
                _write(run_dir / "result.json", result)
                return 0 if result["verification_passed"] else 2
            dispenser = plan["scenario"] == "dispenser_comparison"
            sixdof = plan["scenario"] in SIXDOF_SCENARIOS
            script = ("run_starship_sixdof.py" if sixdof else
                      "run_starship_dispenser_experiment.py" if dispenser else "run_starship_3d.py")
            args = [sys.executable, str(REPO / "scripts" / script),
                    "--approve-synthetic" if dispenser else "--approve-simulation", "--output-dir", str(output)]
            if not dispenser:
                args += ["--scenario", SIXDOF_SCENARIOS[plan["scenario"]] if sixdof else plan["scenario"]]
            if sixdof:
                args += ["--profile", str(REPO / SIXDOF_PROFILE), "--return-policy", plan["simulation"]["return_policy"],
                         "--booster-policy", plan["simulation"]["booster_policy"]]
            if plan["scenario"] in SUPERVISED_SCENARIOS:
                args += ["--supervision-dir", str(run_dir / "supervision"), "--supervision-request-id", run_id]
                if plan["scenario"] == "sixdof_observation_supervised":
                    args += ["--collect-observation"]
            returncode = (_run_simulator(args, timeout=plan["simulation"]["maximum_wall_time_s"])
                          if plan["scenario"] == LAUNCH_CATCH_SCENARIO else _run_simulator(args))
            result["simulator_process_returncode"] = returncode
            if returncode != 0:
                raise StarshipMissionError("simulator_process_failed")
            if plan["source_sha256"] != _sources(plan["scenario"]):
                raise StarshipMissionError("approved_source_changed_during_execution")
            study = _read(output / "study.json")
            if sixdof:
                from .starship_sixdof_verifier import verify_study
                if not isinstance(study, dict) or not isinstance(study.get("provenance"), dict):
                    raise StarshipMissionError("sixdof_execution_input_binding_mismatch")
                provenance = study.get("provenance", {})
                recorded_sources = provenance.get("source_sha256", {})
                if (provenance.get("profile_sha256") != plan["simulation"]["profile_sha256"]
                        or study.get("profile") != _read(REPO / SIXDOF_PROFILE)
                        or provenance.get("dt_scale") != 1.0
                        or provenance.get("duration_override_s") is not None
                        or provenance.get("return_policy") != plan["simulation"]["return_policy"]
                        or provenance.get("booster_policy") != plan["simulation"]["booster_policy"]
                        or provenance.get("observation_collection_enabled", False) is not
                           (plan["scenario"] == "sixdof_observation_supervised")
                        or not isinstance(recorded_sources, dict)
                        or set(recorded_sources) != set(SIXDOF_SOURCES) - {
                            "src/runtime/starship_sixdof_catalog.py", SIXDOF_PROFILE,
                            "src/runtime/starship_sixdof_verifier.py", "src/runtime/starship_retained_return_verifier.py", "src/runtime/starship_booster_catch_verifier.py",
                            "src/runtime/starship_booster_recovery_verifier.py", "src/runtime/starship_wind_verifier.py"}
                        or any(plan["source_sha256"].get(name) != digest
                               for name, digest in recorded_sources.items())):
                    raise StarshipMissionError("sixdof_execution_input_binding_mismatch")
                if plan["scenario"] in CATCH_CATALOG:
                    from .starship_booster_catch_verifier import verify_catch
                    catch_config = _read(REPO / CATCH_PROFILE)
                    catch_runs = study.get("runs")
                    if (study.get("schema") != "missionos.starship_sixdof_study.v1"
                            or provenance.get("physical_execution_invoked") is not False
                            or provenance.get("starship_vehicle_validated") is not False
                            or provenance.get("catch_profile_sha256") != plan["simulation"]["catch_profile_sha256"]
                            or study.get("catch_profile") != catch_config or type(catch_runs) is not list or len(catch_runs) != 1
                            or type(catch_runs[0]) is not dict or catch_runs[0].get("scenario") != SIXDOF_SCENARIOS[plan["scenario"]]):
                        raise StarshipMissionError("catch_execution_input_binding_mismatch")
                    catch_record = catch_runs[0].get("catch_record")
                    if (not isinstance(catch_record, dict)
                            or catch_record.get("initialization") != {"kind": "terminal_initialized", "launch_connected": False}
                            or catch_record.get("control_policy", "fixed_v1") != "fixed_v1"
                            or catch_record.get("integration_dt_s") != catch_config["integration_dt_s"]
                            or catch_record.get("requested_duration_s") != catch_config["duration_s"]):
                        raise StarshipMissionError("catch_execution_input_binding_mismatch")
                    capture = verify_catch(catch_runs[0], study["profile"], catch_config)
                    observed = catch_runs[0].get("outcome")
                    observed = observed if isinstance(observed, dict) else {}
                    verdict = {"passed": capture["passed"], "issues": capture["issues"], "booster_catch": capture,
                               "mission_completed": False, "physical_execution": False,
                               "observed_outcomes": [{"scenario": catch_runs[0]["scenario"],
                                   "termination": observed.get("termination"), "orbit_gate_reached": False,
                                   "payload_released_count": 0, "booster": None,
                                   "simulated_catch_supported": capture.get("simulated_catch_supported", False),
                                   "duration_s": observed.get("duration_s"),
                                   "initialization": "near_tower_terminal", "simulation_success": None}]}
                else:
                    if plan["scenario"] == LAUNCH_CATCH_SCENARIO and (
                            provenance.get("catch_profile_sha256") != plan["simulation"]["catch_profile_sha256"]
                            or study.get("catch_profile") != _read(REPO / CATCH_PROFILE)):
                        raise StarshipMissionError("recovery_execution_input_binding_mismatch")
                    verdict = verify_study(study, expected_scenario=SIXDOF_SCENARIOS[plan["scenario"]])
                passed = verdict["passed"] is True
                # Preserve the primary verifier's evidence even if a later
                # contract check fails. A failed verdict need not contain the
                # success-only capture-planning summary.
                result["verification"] = verdict
                runs = study.get("runs")
                run = runs[0] if isinstance(runs, list) and len(runs) == 1 else None
                if plan["scenario"] == LAUNCH_CATCH_SCENARIO and isinstance(run, dict) and run.get("booster_run") is not None:
                    planning = verdict.get("booster_recovery", {}).get("capture_planning", {})
                    if (plan["booster_recovery"].get("capture_planning_contract") != "missionos.starship_capture_planning.v1"
                            or (passed and planning.get("assessed") is not True)):
                        raise StarshipMissionError("recovery_capture_planning_contract_missing")
                if plan["scenario"] in SUPERVISED_SCENARIOS:
                    from .starship_flight_supervision_verifier import verify_supervision
                    supervision = verify_supervision(run, study["profile"],
                                                     expected_request_id=run_id,
                                                     expected_contract=plan["flight_supervision"])
                    verdict["flight_supervision"] = supervision
                    passed = passed and supervision["passed"] is True
                    verdict["passed"] = passed
                from .starship_retained_return_verifier import verify_retained_return
                if plan["scenario"] not in CATCH_CATALOG:
                    retained_return = verify_retained_return(run, study["profile"],
                                                            expected_policy=plan["simulation"]["return_policy"])
                    verdict["retained_return"] = retained_return
                    passed = passed and retained_return["passed"] is True
                verdict["passed"] = passed
                _write(output / "verification.json", verdict)
                manifest = _read(output / "manifest.json")
                if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
                    raise StarshipMissionError("sixdof_manifest_invalid")
                manifest["files"]["verification.json"] = sha256((output / "verification.json").read_bytes()).hexdigest()
                _write(output / "manifest.json", manifest)
            elif dispenser:
                from .starship_dispenser_verifier import verify_dispenser_experiment
                verdict = verify_dispenser_experiment(study)
                passed = verdict["verified"] is True
            else:
                from .starship_study import verify_study
                verdict = verify_study(study)
                passed = verdict["study_verified"] is True
                _write(output / "verification.json", verdict)
            result["verification"] = verdict
            result["verification_passed"] = passed
            if plan["source_sha256"] != _sources(plan["scenario"]):
                raise StarshipMissionError("approved_source_changed_during_verification")
            result["artifact_sha256"] = {name: sha256((output / name).read_bytes()).hexdigest()
                                         for name in ARTIFACT_NAMES if (output / name).is_file()}
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            result["verification_passed"] = False
            result["failure_reason"] = str(exc) if isinstance(exc, StarshipMissionError) else "worker_execution_or_verification_failed"
        result["ended_at_epoch_s"] = service.clock()
        result["signature"] = service._signature(result)
        _write(run_dir / "result.json", result)
        return 0 if result["verification_passed"] else 2
