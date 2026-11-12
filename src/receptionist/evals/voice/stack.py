"""Local LiveKit server and agent worker processes for voice evals."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from livekit import api

from ..services import EvalServices, _wait_for_port, free_port

DEV_KEY, DEV_SECRET = "devkey", "local-eval-secret-not-for-production-use"
# The worker this stack launches registers under this name, whatever AGENT_NAME .env sets.
AGENT_NAME = "receptionist-eval"


def find_livekit_server() -> str | None:
    return os.getenv("LIVEKIT_SERVER_BIN") or shutil.which("livekit-server")


class LocalLiveKit:
    """`livekit-server --dev` bound to localhost, so calls never leave the machine."""

    def __init__(self, workdir: Path) -> None:
        binary = find_livekit_server()
        if binary is None:
            raise RuntimeError("livekit-server not found; set LIVEKIT_SERVER_BIN or add it to PATH")
        self._binary = binary
        self._workdir = workdir
        self.port = free_port()
        self.url = f"ws://127.0.0.1:{self.port}"
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> LocalLiveKit:
        self._workdir.mkdir(parents=True, exist_ok=True)
        config = (
            f"port: {self.port}\n"
            f"rtc:\n  tcp_port: {free_port()}\n  port_range_start: 50000\n  port_range_end: 50200\n"
            "  node_ip: 127.0.0.1\n  use_external_ip: false\n"
            f"keys:\n  {DEV_KEY}: {DEV_SECRET}\n"
        )
        log = open(self._workdir / "livekit-server.log", "ab")  # noqa: SIM115
        self._proc = subprocess.Popen(
            [self._binary, "--bind", "127.0.0.1", "--config-body", config], stdout=log, stderr=log
        )
        _wait_for_port(self.port, self._proc)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait(timeout=10)

    def token(self, identity: str, room: str, *, dispatch_metadata: dict | None = None) -> str:
        token = (
            api.AccessToken(DEV_KEY, DEV_SECRET)
            .with_identity(identity)
            .with_grants(api.VideoGrants(room_join=True, room=room))
        )
        if dispatch_metadata is not None:
            token = token.with_room_config(
                api.RoomConfiguration(
                    agents=[
                        api.RoomAgentDispatch(
                            agent_name=AGENT_NAME, metadata=json.dumps(dispatch_metadata)
                        )
                    ]
                )
            )
        return token.to_jwt()

    @staticmethod
    def new_room() -> str:
        return f"eval-{uuid.uuid4().hex[:8]}"


class AgentWorker:
    """Runs `python -m receptionist.agent start` against the local server and eval services."""

    def __init__(self, livekit: LocalLiveKit, services: EvalServices, workdir: Path) -> None:
        self._livekit = livekit
        self._services = services
        self._workdir = workdir
        self.call_log_dir = workdir / "calls"
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> AgentWorker:
        env = {
            **os.environ,
            "LIVEKIT_URL": self._livekit.url,
            "LIVEKIT_API_KEY": DEV_KEY,
            "LIVEKIT_API_SECRET": DEV_SECRET,
            "AGENT_NAME": AGENT_NAME,
            "MCP_SERVER_URL": self._services.mcp_url,
            "MCP_AUTH_TOKEN": self._services.mint_token("eval-voice-agent", ttl_s=6 * 3600),
            "BILLING_AGENT_URL": self._services.billing_url,
            "BILLING_AGENT_TOKEN": self._services.billing_token,
            "WEB_BASE_URL": self._services.web_url,
            "INTERNAL_API_TOKEN": self._services.internal_token,
            "CLINIC_NOW": self._services.now,
            "RECEPTIONIST_CALL_LOG_DIR": str(self.call_log_dir),
        }
        log = open(self._workdir / "agent-worker.log", "ab")  # noqa: SIM115
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "receptionist.agent", "start"], env=env, stdout=log, stderr=log
        )
        time.sleep(4.0)  # worker registration; dispatches queue on the server until it's up
        if self._proc.poll() is not None:
            raise RuntimeError("agent worker exited during startup; see agent-worker.log")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def call_record(self, room: str, timeout: float = 10.0) -> dict | None:
        path = self.call_log_dir / f"{room}.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return json.loads(path.read_text())
            time.sleep(0.2)
        return None
