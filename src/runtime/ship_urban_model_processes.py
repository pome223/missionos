"""Own local model-worker lifetimes; no cloud allocation and no shell commands.

The worker protocol is deliberately separate from the existing one-shot native
services. A native adapter must preserve their image/model evidence contracts.
"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
from uuid import uuid4


class ManagedUrbanModels:
    def __init__(self, commands, output_dir, *, execution_scope, timeout_s=80):
        if set(commands) != {"vla", "wam"} or execution_scope not in {"fixture", "sim"}:
            raise ValueError("two_explicit_urban_model_commands_required")
        self.commands, self.root = commands, Path(output_dir)
        self.execution_scope, self.timeout_s = execution_scope, timeout_s
        self.processes, self.ports, self.nonces, self.logs = {}, {}, {}, {}
        self.lock = threading.Lock()
        self.closed, self.started = False, False

    def exchange(self, model, path, payload=None):
        with self.lock:
            if self.closed or model not in self.processes:
                raise RuntimeError("urban_model_process_inactive")
            if self.processes[model].poll() is not None:
                raise RuntimeError("urban_model_process_exited")
            port = self.ports[model]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=self.timeout_s)
        try:
            body = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
            connection.request(
                "POST" if payload is not None else "GET",
                path,
                body,
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read(2_000_001)
            if response.status != 200 or len(raw) > 2_000_000:
                raise ValueError("urban_model_http_response_rejected")
            value = json.loads(raw)
            if value.get("process_nonce") != self.nonces[model]:
                raise ValueError("urban_model_process_identity_mismatch")
            return value
        finally:
            connection.close()

    def start(self):
        with self.lock:
            if self.started or self.closed:
                raise RuntimeError("urban_model_process_session_is_single_use")
            self.started = True
            self.root.mkdir(parents=True, exist_ok=False)
        identities = {}
        for model in ("vla", "wam"):
            with self.lock:
                if self.closed:
                    raise RuntimeError("urban_model_start_cancelled")
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                nonce = uuid4().hex
                arguments = self.commands[model](port, nonce)
                if not isinstance(arguments, list) or not arguments:
                    raise ValueError("urban_model_requires_explicit_argv")
                log = (self.root / f"{model}.log").open("wb")
                self.logs[model] = log
                self.ports[model], self.nonces[model] = port, nonce
                self.processes[model] = subprocess.Popen(
                    arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            deadline = time.monotonic() + self.timeout_s
            while True:
                with self.lock:
                    if self.closed or self.processes[model].poll() is not None:
                        raise RuntimeError("urban_model_start_failed_or_cancelled")
                try:
                    identity = self.exchange(model, "/health")
                except (ConnectionError, OSError, http.client.HTTPException):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("urban_model_start_timeout")
                    time.sleep(0.02)
                    continue
                if (
                    identity.get("model") != model
                    or identity.get("execution_scope") != self.execution_scope
                    or identity.get("ready") is not True
                ):
                    raise ValueError("urban_model_worker_contract_mismatch")
                identities[model] = identity
                break
        return {"execution_scope": self.execution_scope, "workers": identities}

    def infer(self, request):
        return self.exchange(request["model"], "/infer", request)

    @staticmethod
    def group_alive(process):
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def stop(self):
        # The same lock covers process creation and revocation. A delayed
        # startup thread cannot create a second worker after this snapshot.
        with self.lock:
            self.closed = True
            processes = dict(self.processes)
        for process in processes.values():
            if self.group_alive(process):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for process in processes.values():
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            if self.group_alive(process):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=2)
        for log in self.logs.values():
            log.close()
        return {
            "owned_processes": {
                model: {"pid": process.pid, "exit_code": process.returncode}
                for model, process in processes.items()
            },
            "accelerator_power_measured": False,
        }

    def stopped(self):
        with self.lock:
            return self.closed and all(
                p.poll() is not None and not self.group_alive(p) for p in self.processes.values()
            )
