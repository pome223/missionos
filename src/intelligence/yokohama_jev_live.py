"""One explicitly authorized delivery budget; no automatic ledger recreation."""
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Publication carries no live grant and never imports a private ledger.
# Offline tests may monkeypatch this flag only with injected provider transport.
LIVE_ENABLED = False
BUDGET_ID = "closed-public-demo"
MODEL = "jev-1.13.0"
MAX_BYTES = 32768
# Conservative whole official 64k context, rounded UP to 65536 tokens.
MAX_REQUEST_USD = 65536 * 0.042 / 1_000_000
# Public fixtures have no default filesystem grant location. Tests explicitly
# inject an isolated absolute path; the consumed private grant is never resolved.


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LiveLedger:
    def __init__(self, path=None):
        if path is None:
            raise ValueError("Public demonstration has no default live ledger; explicit mock ledger required")
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("Explicit ledger path must be absolute")

    def connect(self):
        if (self.path.is_symlink() or not self.path.is_file()
                or not self.path.with_suffix(".initialized").is_file()):
            raise ValueError("Missing live ledger; never recreate or reset a consumed budget")
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
        conn.execute("PRAGMA synchronous=FULL")
        if conn.execute("SELECT budget_id FROM grant_record").fetchall() != [(BUDGET_ID,)]:
            conn.close()
            raise ValueError("Foreign budget ledger")
        return conn

    def claim(self, task_id, plan_sha256):
        # One attempt, consumed before runner creation; crash never refunds flight.
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT count(*) FROM delivery").fetchone()[0]:
                raise ValueError("This live delivery allowance was already consumed")
            conn.execute("INSERT INTO delivery VALUES (?,?)", (task_id, plan_sha256))

    def reserve(self, task_id, plan_sha256, request_id, observation_id, payload_sha256):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT task_id,plan_sha256 FROM delivery").fetchall() != [(task_id, plan_sha256)]:
                raise ValueError("Unbound live task or approved plan")
            if conn.execute("SELECT 1 FROM sends WHERE request_id=? OR observation_id=?",
                            (request_id, observation_id)).fetchone():
                raise ValueError("Replayed live request")
            count = conn.execute("SELECT count(*) FROM sends").fetchone()[0]
            if count >= 2 or (count + 1) * MAX_REQUEST_USD > 0.01:
                raise ValueError("Live budget exhausted")
            slot = count + 1
            conn.execute("INSERT INTO sends VALUES (?,?,?,?,?,?)",
                         (slot, request_id, observation_id, payload_sha256, MAX_REQUEST_USD, time.time()))
            return slot



class BoundedTransport:
    """No retries/redirects. Slot is durable before the sole HTTP attempt."""
    def __init__(self, *, ledger, task_id, plan_sha256, request, admission, opener=None):
        self.ledger, self.task_id, self.plan_sha256 = ledger, task_id, plan_sha256
        self.request, self.admission = request, admission
        self.opener = opener or build_opener(NoRedirect())
        self.calls, self.slot, self.size = 0, None, 0

    def __call__(self, payload):
        from src.runtime.yokohama_payload import digest
        if not LIVE_ENABLED:
            raise ValueError("Public demonstration has no authorized live grant; fixture only")
        body = json.dumps(payload, allow_nan=False, ensure_ascii=True).encode()
        self.size = len(body)
        if len(body) > MAX_BYTES or payload.get("model") != MODEL:
            raise ValueError("Payload cap or pinned model mismatch")
        key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not key:
            raise ValueError("Jev credential unavailable; no HTTP attempt")
        remaining = self.admission["host_deadline_monotonic_s"] - time.monotonic()
        if remaining <= 0:
            raise ValueError("Expired mailbox before send")
        if self.calls or self.slot is not None:
            raise ValueError("Transport cannot be replayed")
        self.slot = self.ledger.reserve(self.task_id, self.plan_sha256,
                                        self.request["judge_request_id"],
                                        self.request["observation_id"], digest(payload))
        # Recheck after lock wait; expired request still consumes its reserved slot.
        remaining = self.admission["host_deadline_monotonic_s"] - time.monotonic()
        if remaining <= 0:
            raise ValueError("Expired reserved mailbox")
        req = Request("https://api.typesafe.ai/v1/systemone", data=body,
                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        self.calls = 1
        with self.opener.open(req, timeout=min(15, remaining)) as response:
            result = response.read(1024 * 1024 + 1)
        if len(result) > 1024 * 1024:
            raise ValueError("Oversize provider response")
        raw = json.loads(result)
        if raw.get("model") != MODEL:
            raise ValueError("Provider model differs from pinned price/version")
        return raw

    def evidence(self):
        return dict(invocation_kind="decision_api", provider="typesafe",
                    inference_invoked=bool(self.calls), external_api_calls=self.calls,
                    budget_id=BUDGET_ID, reserved_slot=self.slot,
                    payload_bytes=self.size, maximum_request_usd=MAX_REQUEST_USD,
                    retries=0, redirects=0)
