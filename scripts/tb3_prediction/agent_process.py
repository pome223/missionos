"""Bounded parent-side IPC to a preloaded navigation Agent runtime."""

import json
import selectors
import subprocess
import time


class AgentProcess:
    def __init__(
        self, python, folder, env_file=None, detour_authorized=False, continue_authorized=False
    ):
        command = [
            str(python),
            "scripts/tb3_prediction/agent_service.py",
            "--folder",
            str(folder),
            "--operator-authorized-simulation",
        ]
        if detour_authorized:
            command += ["--approve-bounded-detour"]
        if continue_authorized:
            command += ["--approve-observed-continue"]
        if env_file:
            command += ["--env-file", str(env_file)]
        self.log = (folder / "agent-service.log").open("w")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            bufsize=1,
        )
        try:
            if self.receive(60).get("ready") is not True:
                raise RuntimeError("agent_service_not_ready")
        except BaseException:
            self.close()
            raise

    def receive(self, timeout):
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(timeout=min(1, max(0, deadline - time.monotonic()))):
                    continue
                line = self.process.stdout.readline()
                if not line:
                    raise RuntimeError("agent_service_exited")
                if line.startswith("RPC:"):
                    return json.loads(line[4:])
                self.log.write(line)
                self.log.flush()
        raise TimeoutError("agent_service_deadline")

    def request(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()
        return self.receive(145)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log.close()
