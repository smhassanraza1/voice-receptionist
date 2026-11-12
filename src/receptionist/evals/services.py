"""Runs the real MCP server and billing agent as subprocesses for an eval run.

Evals talk to the same processes, over the same HTTP + auth, that production uses. Each
scenario gets a freshly seeded database and its own token subject, so rate limits and
identity lockouts from one scenario can't leak into the next.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx2
import jwt
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from receptionist.common import clock
from receptionist.mcp_server.db import reset_database

TOKEN_ISSUER = "receptionist-auth"
ALL_SCOPES = "kb:read appointments:read appointments:write"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"service exited early with code {proc.returncode}")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"service on port {port} did not start")


class EvalServices:
    def __init__(self, workdir: Path, *, now: str = clock.EVAL_NOW) -> None:
        self.workdir = workdir
        self.now = now
        self.db_path = workdir / "clinic.db"
        self.jwt_secret = secrets.token_urlsafe(48)
        self.billing_token = secrets.token_urlsafe(24)
        self.internal_token = secrets.token_urlsafe(24)
        self.operator_passcode = secrets.token_urlsafe(12)
        self.mcp_port = free_port()
        self.billing_port = free_port()
        self.web_port = free_port()
        self.mcp_url = f"http://127.0.0.1:{self.mcp_port}/mcp"
        self.billing_url = f"http://127.0.0.1:{self.billing_port}"
        self.web_url = f"http://127.0.0.1:{self.web_port}"
        self._procs: list[subprocess.Popen] = []

    def __enter__(self) -> EvalServices:
        self.workdir.mkdir(parents=True, exist_ok=True)
        os.environ["CLINIC_NOW"] = self.now  # the agent prompt and fixtures read the same clock
        reset_database(self.db_path)
        base_env = {**os.environ, "CLINIC_NOW": self.now}
        self._spawn(
            "receptionist.mcp_server",
            {
                "MCP_PORT": str(self.mcp_port),
                "MCP_SERVER_URL": self.mcp_url,
                "MCP_DB_PATH": str(self.db_path),
                "MCP_JWT_SECRET": self.jwt_secret,
                "MCP_LOG_LEVEL": "WARNING",
            },
            base_env,
            self.mcp_port,
        )
        self._spawn(
            "receptionist.billing_agent",
            {
                "BILLING_AGENT_PORT": str(self.billing_port),
                "BILLING_AGENT_URL": self.billing_url,  # advertised in the agent card
                "BILLING_AGENT_TOKEN": self.billing_token,
                "BILLING_AGENT_LLM": os.getenv("BILLING_AGENT_LLM", "auto"),
            },
            base_env,
            self.billing_port,
        )
        # receives human-handoff escalations, exactly as the operator console would
        self._spawn(
            "receptionist.web",
            {
                "WEB_PORT": str(self.web_port),
                "INTERNAL_API_TOKEN": self.internal_token,
                "OPERATOR_PASSCODE": self.operator_passcode,
            },
            base_env,
            self.web_port,
        )
        return self

    def _spawn(self, module: str, extra: dict[str, str], env: dict[str, str], port: int) -> None:
        log = open(self.workdir / f"{module.rsplit('.', 1)[-1]}.log", "ab")  # noqa: SIM115
        proc = subprocess.Popen(
            [sys.executable, "-m", module], env={**env, **extra}, stdout=log, stderr=log
        )
        self._procs.append(proc)
        _wait_for_port(port, proc)

    def __exit__(self, *exc: object) -> None:
        for proc in self._procs:
            proc.terminate()
        for proc in self._procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def reset(self) -> None:
        reset_database(self.db_path)

    def mint_token(self, subject: str, scopes: str = ALL_SCOPES, ttl_s: int = 3600) -> str:
        now = int(time.time())
        claims = {
            "iss": TOKEN_ISSUER,
            "aud": self.mcp_url,
            "sub": subject,
            "scope": scopes,
            "iat": now,
            "nbf": now,
            "exp": now + ttl_s,
        }
        return jwt.encode(claims, self.jwt_secret, algorithm="HS256")

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, subject: str
    ) -> tuple[bool, Any]:
        """Call an MCP tool directly (used by graders). Returns (ok, payload)."""
        headers = {"Authorization": f"Bearer {self.mint_token(subject)}"}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=60.0)) as http,
            Client(streamable_http_client(self.mcp_url, http_client=http)) as client,
        ):
            result = await client.call_tool(name, arguments)
        if result.is_error:
            return False, " ".join(getattr(p, "text", "") for p in result.content)
        if result.structured_content is not None:
            return True, result.structured_content
        text = " ".join(getattr(p, "text", "") for p in result.content)
        try:
            return True, json.loads(text)
        except json.JSONDecodeError:
            return True, text
