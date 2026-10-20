"""Exercise large command delivery over the real Flask and node WebSockets."""

import base64
import io
import json
import random
import time
from uuid import uuid4

from PIL import Image
from test_e2e import eventually
from test_e2e import live_system as live_system


def test_large_image_command_reaches_native_peer_without_heartbeat_reconnect(live_system):
    system = live_system
    client, post = system["client"], system["post"]
    created = post(
        "/api/threads",
        json={"host_id": system["host"]["id"], "cwd": str(system["root"]), "title": "Large image"},
    ).json()
    thread_id, command_id = created["thread"]["id"], created["command"]["id"]
    eventually(
        lambda: (
            client.get(f"/api/commands/{command_id}").json()["command"]["status"] == "completed"
        ),
        "thread creation before image command",
    )
    output = io.BytesIO()
    # Incompressible pixels make the actual PNG exceed 1 MiB without a huge canvas.
    pixels = random.Random(42).randbytes(768 * 768 * 3)
    Image.frombytes("RGB", (768, 768), pixels).save(output, format="PNG")
    image = output.getvalue()
    assert len(image) > 1024 * 1024
    uploaded = post("/api/assets", files={"file": ("large.png", image, "image/png")})
    assert uploaded.status_code == 200
    runtime_id = system["node"]["service"].runtime.runtime_id
    connection = system["node"]["service"].ws
    started = time.monotonic()
    accepted = post(
        f"/api/threads/{thread_id}/messages",
        json={
            "text": "Receive the complete image",
            "client_request_id": uuid4().hex,
            "asset_ids": [uploaded.json()["asset"]["id"]],
        },
    )
    assert accepted.status_code == 202, accepted.text

    def received_image():
        for line in system["audit"].read_text(encoding="utf-8").splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # The child may currently be flushing its final audit line.
            if message.get("method") == "turn/start":
                return next(
                    (
                        value["url"]
                        for value in message["params"]["input"]
                        if value["type"] == "image"
                    ),
                    None,
                )
        return None

    received = eventually(received_image, "large image over the active node WebSocket", timeout=5)
    assert time.monotonic() - started < 5
    assert base64.b64decode(received.split(",", 1)[1]) == image
    assert system["node"]["service"].runtime.runtime_id == runtime_id
    assert system["node"]["service"].ws is connection
