"""Live integration test: a real Agent Relay HTTP server against a real DB.

Unlike ``test_agent_relay.py`` (FastAPI's in-process ``TestClient``), this
starts an actual ``uvicorn`` process and talks to it over real HTTP, the same
way the included worker and dashboard do. It reproduces SPEC.md's acceptance
scenario 1: two agents register, one sends a task, the other claims and
completes it, and the sender reads back the result.

Respects ``RELAY_DATABASE_URL``/``DATABASE_URL`` if already set (CI points
this at PostgreSQL); otherwise falls back to a scratch SQLite file so this
never touches a developer's ``./agent-relay.db``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(tmp_path):
    port = _free_port()
    db_url = os.environ.get("RELAY_DATABASE_URL") or os.environ.get("DATABASE_URL") or f"sqlite:///{tmp_path}/integration.db"
    env = {**os.environ, "RELAY_DATABASE_URL": db_url}
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        healthy = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        if not healthy:
            output = process.stdout.read() if process.stdout else ""
            process.terminate()
            raise RuntimeError(f"live server did not become healthy in time:\n{output}")
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def register(client: httpx.Client, name: str) -> tuple[dict, dict[str, str]]:
    response = client.post("/api/v1/agents", json={"name": name})
    assert response.status_code == 201
    data = response.json()
    return data, {"Authorization": f"Bearer {data['token']}"}


def test_scenario_1_two_agents_exchange_task_and_result(live_server):
    with httpx.Client(base_url=live_server, timeout=10) as client:
        sender, sender_headers = register(client, "alice")
        recipient, recipient_headers = register(client, "uppercase")

        created = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient["agent_id"], "input": "hola mundo"},
        )
        assert created.status_code == 201
        task_id = created.json()["task_id"]
        assert created.json()["status"] == "queued"

        claim = client.post(
            "/api/v1/tasks/claim",
            headers=recipient_headers,
            json={"worker_id": "integration-worker", "wait_seconds": 5},
        )
        assert claim.status_code == 200
        claim_token = claim.json()["claim_token"]

        complete = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": claim_token, "output": "HOLA MUNDO"},
        )
        assert complete.status_code == 200
        assert complete.json()["status"] == "completed"

        # What the sender sees once the result is in: a terminal, completed
        # task with the recipient's output, no error, and a finish time.
        seen_by_sender = client.get(f"/api/v1/tasks/{task_id}", headers=sender_headers).json()
        assert seen_by_sender["status"] == "completed"
        assert seen_by_sender["output"] == "HOLA MUNDO"
        assert seen_by_sender["error"] is None
        assert seen_by_sender["finished_at"] is not None

        attempts = client.get(f"/api/v1/tasks/{task_id}/attempts", headers=sender_headers).json()
        assert attempts["items"][0]["outcome"] == "completed"
        assert "claim_token" not in attempts["items"][0]
