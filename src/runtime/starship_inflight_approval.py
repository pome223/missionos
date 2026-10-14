"""One-use local-operator scope amendments for opt-in simulation workers.

The host owns the bearer token. Models and workers must not receive it. A
signed amendment is not a command: the executor must independently check the
current physical state before using the resulting scope.
"""
from contextlib import contextmanager
from hashlib import sha256
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time


class InflightApprovalError(ValueError):
    """Fixed reason codes never include caller input or credentials."""


def _require(condition, reason):
    if not condition:
        raise InflightApprovalError(reason)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _copy(value):
    return json.loads(_json(value))


def _id(value):
    return type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value) is not None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12


def _scope(value):
    _require(type(value) is dict and set(value) == {"commands", "resource_limits"}, "invalid_scope")
    commands, limits = value["commands"], value["resource_limits"]
    _require(type(commands) is list and 0 < len(commands) <= 64 and all(_id(c) for c in commands)
             and len(commands) == len(set(commands)), "invalid_scope_commands")
    _require(type(limits) is dict and len(limits) <= 64
             and all(_id(k) and _number(v) for k, v in limits.items()), "invalid_scope_limits")


def _observation(value, simulation, wall, maximum_age):
    _require(type(value) is dict and set(value) == {"state_sha256", "simulation_time_s", "wall_time_s"},
             "invalid_observation")
    _require(type(value["state_sha256"]) is str and re.fullmatch(r"[a-f0-9]{64}", value["state_sha256"])
             and all(_number(value[k]) for k in ("simulation_time_s", "wall_time_s")), "invalid_observation")
    _require(0 <= simulation-value["simulation_time_s"] <= maximum_age
             and 0 <= wall-value["wall_time_s"] <= maximum_age, "observation_not_fresh")


class InflightApprovalLedger:
    """SQLite transactions serialize responses and grants across processes.

    ``operator_token=None`` creates a consumer-only handle. Token possession
    authenticates the configured local operator principal, not an SSO identity.
    Clocks must be from one host/boot and one mission's simulation timeline.
    """

    def __init__(self, path, *, signing_key, operator_token, operator_id, registry, clock=time.monotonic):
        _require(type(signing_key) is bytes and len(signing_key) >= 32, "invalid_signing_key")
        _require(operator_token is None or type(operator_token) is str and 32 <= len(operator_token) <= 512
                 and operator_token.isascii(),
                 "invalid_operator_token")
        _require(_id(operator_id), "invalid_operator_id")
        _scope(registry)
        self._key, self._token, self._clock = signing_key, operator_token, clock
        self._operator, self._registry = operator_id, _copy(registry)
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _require(not self._path.is_symlink(), "unsafe_ledger_path")
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self._path, 0o600)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS config (singleton INTEGER PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, request TEXT NOT NULL, receipt TEXT, consumed TEXT)")
            config = self._sign({"operator_id": operator_id, "registry": self._registry})
            old = db.execute("SELECT value FROM config WHERE singleton=1").fetchone()
            if old is None:
                db.execute("INSERT INTO config VALUES (1, ?)", (_json(config),))
            else:
                _require(self._read_signed(old[0]) == config, "ledger_configuration_mismatch")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self._path, timeout=5., isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def _sign(self, value):
        body = _copy(value)
        body["sha256"] = digest(body)
        body["signature"] = hmac.new(self._key, _json(body).encode(), "sha256").hexdigest()
        return body

    def _read_signed(self, raw):
        value = json.loads(raw)
        _require(type(value) is dict and type(value.get("signature")) is str, "ledger_integrity_failure")
        body = {k: v for k, v in value.items() if k != "signature"}
        _require(hmac.compare_digest(value["signature"], hmac.new(self._key, _json(body).encode(), "sha256").hexdigest())
                 and body.get("sha256") == digest({k: v for k, v in body.items() if k != "sha256"}),
                 "ledger_integrity_failure")
        return value

    def _now(self):
        now = self._clock()
        _require(_number(now), "invalid_wall_clock")
        return now

    def _expanded(self, scope, delta):
        _scope(scope)
        caps = self._registry
        _require(set(scope["commands"]) <= set(caps["commands"])
                 and set(scope["resource_limits"]) <= set(caps["resource_limits"])
                 and all(v <= caps["resource_limits"][k] for k, v in scope["resource_limits"].items()),
                 "current_scope_outside_registry")
        _require(type(delta) is dict and set(delta) == {"add_commands", "resource_limits"}, "invalid_scope_delta")
        additions, limits = delta["add_commands"], delta["resource_limits"]
        _require(type(additions) is list and all(_id(c) for c in additions)
                 and len(set(additions)) == len(additions)
                 and set(additions) <= set(caps["commands"])-set(scope["commands"]), "unregistered_or_existing_command")
        _require(type(limits) is dict and set(limits) <= set(scope["resource_limits"])
                 and all(_number(v) and scope["resource_limits"][k] < v <= caps["resource_limits"][k]
                         for k, v in limits.items()) and bool(additions or limits), "invalid_resource_expansion")
        return {"commands": scope["commands"]+additions, "resource_limits": {**scope["resource_limits"], **limits}}

    def create(self, *, request_id, mission_id, vehicle_id, revision, scope, delta, observation,
               simulation_time_s, simulation_deadline_s, wall_deadline_s, fallback, maximum_observation_age_s=2., candidate_sha256=None):
        wall = self._now()
        _require(candidate_sha256 is None or type(candidate_sha256) is str
                 and re.fullmatch(r"[a-f0-9]{64}", candidate_sha256), "invalid_candidate_binding")
        candidate_binding = {} if candidate_sha256 is None else {"candidate_sha256": candidate_sha256}
        _require(all(_id(v) for v in (request_id, mission_id, vehicle_id))
                 and type(revision) is int and revision >= 0, "invalid_request_context")
        self._expanded(scope, delta)
        _require(_id(fallback) and fallback in scope["commands"], "fallback_not_preapproved")
        _require(all(_number(v) for v in (simulation_time_s, simulation_deadline_s, wall_deadline_s,
                                         maximum_observation_age_s)) and 0 < maximum_observation_age_s <= 30
                 and simulation_time_s < simulation_deadline_s <= simulation_time_s+1200
                 and wall < wall_deadline_s <= wall+1200, "invalid_fixed_deadline")
        _observation(observation, simulation_time_s, wall, maximum_observation_age_s)
        request = self._sign({"schema": "missionos.starship_inflight_approval_request.v1",
            "request_id": request_id, "mission_id": mission_id, "vehicle_id": vehicle_id,
            "current_revision": revision, "current_scope": scope, "current_scope_sha256": digest(scope),
            "registry_sha256": digest(self._registry), "requested_scope_delta": delta,
            "observed_state_sha256": observation["state_sha256"], "initial_observation": observation,
            "issued_simulation_time_s": simulation_time_s, "issued_wall_time_s": wall,
            "simulation_deadline_s": simulation_deadline_s, "wall_deadline_s": wall_deadline_s,
            "maximum_observation_age_s": maximum_observation_age_s, "fallback": fallback,
            "simulation_only": True, "physical_execution_authorized": False, **candidate_binding})
        with self._transaction() as db:
            _require(db.execute("SELECT 1 FROM requests WHERE id=?", (request_id,)).fetchone() is None,
                     "request_id_already_used")
            db.execute("INSERT INTO requests (id, request) VALUES (?, ?)", (request_id, _json(request)))
        return request

    def _row(self, db, request_id):
        row = db.execute("SELECT request, receipt, consumed FROM requests WHERE id=?", (request_id,)).fetchone()
        _require(row is not None, "request_not_found")
        request, receipt, consumed = [self._read_signed(raw) if raw else None for raw in row]
        _require(request["request_id"] == request_id
                 and (receipt is None or receipt["request_sha256"] == request["sha256"])
                 and (consumed is None or receipt is not None and consumed["request_sha256"] == request["sha256"]
                      and consumed["receipt_sha256"] == receipt["sha256"]), "ledger_record_binding_failure")
        for linked in (receipt, consumed):
            if linked is not None:
                _require(linked.get("candidate_sha256") == request.get("candidate_sha256"),
                         "candidate_record_binding_failure")
        return request, receipt, consumed

    def status(self, request_id):
        with self._transaction() as db:
            request, receipt, consumed = self._row(db, request_id)
        return {"request": request, "receipt": receipt, "consumption": consumed}

    @staticmethod
    def _expired(request, simulation, wall):
        return simulation >= request["simulation_deadline_s"] or wall >= request["wall_deadline_s"]

    @staticmethod
    def _clocks(request, simulation, wall):
        _require(_number(simulation) and simulation >= request["issued_simulation_time_s"]
                 and wall >= request["issued_wall_time_s"], "clock_regressed")

    def _receipt(self, request, choice, simulation, wall):
        return self._sign({"schema": "missionos.starship_inflight_approval_receipt.v1",
            "request_id": request["request_id"], "request_sha256": request["sha256"],
            "mission_id": request["mission_id"], "vehicle_id": request["vehicle_id"],
            "choice": choice, "operator_id": self._operator if choice != "expire" else None,
            "local_operator_authenticated": choice != "expire", "sso_identity_verified": False,
            "simulation_time_s": simulation, "wall_time_s": wall, "executor_command_issued": False,
            **({"candidate_sha256": request["candidate_sha256"]} if "candidate_sha256" in request else {})})

    def resolve(self, *, request_id, request_sha256, observed_state_sha256, choice, bearer_token, simulation_time_s, candidate_sha256=None):
        self.authenticate_operator(bearer_token)
        _require(choice in ("approve", "reject"), "invalid_operator_choice")
        wall = self._now()
        with self._transaction() as db:
            request, receipt, consumed = self._row(db, request_id)
            _require(receipt is None and consumed is None, "request_already_resolved")
            _require(request["sha256"] == request_sha256 and request["observed_state_sha256"] == observed_state_sha256,
                     "displayed_request_mismatch")
            _require(candidate_sha256 == request.get("candidate_sha256"), "displayed_candidate_mismatch")
            self._clocks(request, simulation_time_s, wall)
            _require(not self._expired(request, simulation_time_s, wall), "approval_deadline_expired")
            receipt = self._receipt(request, choice, simulation_time_s, wall)
            db.execute("UPDATE requests SET receipt=? WHERE id=?", (_json(receipt), request_id))
        return receipt

    def authenticate_operator(self, bearer_token):
        """Authenticate status/notification HTTP requests without changing state."""
        _require(self._token is not None and type(bearer_token) is str and bearer_token.isascii()
                 and len(bearer_token) <= 512
                 and hmac.compare_digest(bearer_token, self._token), "operator_authentication_failed")
        return {"operator_id": self._operator, "local_operator_authenticated": True, "sso_identity_verified": False}

    def expire(self, *, request_id, simulation_time_s):
        wall = self._now()
        with self._transaction() as db:
            request, receipt, consumed = self._row(db, request_id)
            self._clocks(request, simulation_time_s, wall)
            _require(self._expired(request, simulation_time_s, wall), "deadline_not_reached")
            if receipt is None and consumed is None:
                receipt = self._receipt(request, "expire", simulation_time_s, wall)
                db.execute("UPDATE requests SET receipt=? WHERE id=?", (_json(receipt), request_id))
        return receipt

    def consume(self, *, request_id, mission_id, vehicle_id, revision, scope, observation, simulation_time_s, run_active):
        wall = self._now()
        _require(run_active is True, "mission_not_active")
        with self._transaction() as db:
            request, receipt, consumed = self._row(db, request_id)
            _require(consumed is None, "request_already_consumed")
            _require(type(revision) is int and (mission_id, vehicle_id, revision, digest(scope)) ==
                     (request["mission_id"], request["vehicle_id"], request["current_revision"], request["current_scope_sha256"]),
                     "current_context_changed")
            self._clocks(request, simulation_time_s, wall)
            _observation(observation, simulation_time_s, wall, request["maximum_observation_age_s"])
            _require(observation["simulation_time_s"] >= request["initial_observation"]["simulation_time_s"]
                     and observation["wall_time_s"] >= request["initial_observation"]["wall_time_s"], "observation_regressed")
            expired = self._expired(request, simulation_time_s, wall)
            _require(receipt is not None or expired, "operator_response_pending")
            if receipt is None:
                receipt = self._receipt(request, "expire", simulation_time_s, wall)
                db.execute("UPDATE requests SET receipt=? WHERE id=?", (_json(receipt), request_id))
            self._clocks(request, receipt["simulation_time_s"], receipt["wall_time_s"])
            _require(receipt["request_sha256"] == request["sha256"] and simulation_time_s >= receipt["simulation_time_s"]
                     and wall >= receipt["wall_time_s"], "receipt_binding_or_clock_invalid")
            approved = receipt["choice"] == "approve" and not expired
            new_scope = self._expanded(scope, request["requested_scope_delta"]) if approved else _copy(scope)
            consumed = self._sign({"schema": "missionos.starship_inflight_approval_consumption.v1",
                "request_id": request_id, "request_sha256": request["sha256"], "receipt_sha256": receipt["sha256"],
                "mission_id": mission_id, "vehicle_id": vehicle_id, "previous_revision": revision,
                "revision": revision+1 if approved else revision, "scope": new_scope, "scope_sha256": digest(new_scope),
                "approved": approved, "reason": "approved" if approved else "expired" if expired else "rejected",
                "approval_disposition": "approved_pending_executor" if approved else "not_approved",
                "scope_applied": False, "revision_semantics": "authorized_revision_not_applied_revision",
                "fallback": request["fallback"] if not approved else None, "latest_observation": observation,
                "simulation_time_s": simulation_time_s, "wall_time_s": wall, "simulation_only": True,
                "requires_executor_check": True, "executor_command_issued": False, "physical_execution_authorized": False,
                **({"candidate_sha256": request["candidate_sha256"]} if "candidate_sha256" in request else {})})
            db.execute("UPDATE requests SET consumed=? WHERE id=?", (_json(consumed), request_id))
        return consumed
