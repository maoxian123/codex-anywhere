"""Opt-in real Codex settings/history acceptance in an isolated loopback hub.

Run: python -m scripts.live_runtime_smoke --codex-bin /path/to/codex
Makes three short inference requests; never modifies global Codex configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import time
from pathlib import Path
from uuid import uuid4

from codex_anywhere.hub.models import Item, Thread, db
from scripts.live_smoke import LiveSystem, wait_for


def run(system: LiveSystem):
    host_id = system.host["id"]
    catalog = {}
    thread_id = ""
    nonce = "SETTINGS_CONTEXT_" + secrets.token_hex(4)

    def capabilities():
        nonlocal catalog
        accepted = system.post(f"/api/hosts/{host_id}/capabilities", {"cwd": str(system.workspace)})
        catalog = system.command(accepted["command"])["result"]
        assert catalog["models"]
        assert catalog["runtime_id"]
        return {"model_count": len(catalog["models"]), "capabilities": catalog["supported"]}

    system.record("real_dynamic_runtime_capabilities", capabilities)
    model = next(
        (entry for entry in catalog["models"] if entry.get("isDefault")), catalog["models"][0]
    )
    efforts = [entry["reasoningEffort"] for entry in model["supportedReasoningEfforts"]]
    effort = "low" if "low" in efforts else efforts[0]
    settings = {
        "model": model["model"],
        "effort": effort,
        "approval_policy": "on-request",
        "sandbox_mode": "read-only",
        "network_access": False,
    }

    def create():
        nonlocal thread_id
        result = system.post(
            "/api/threads",
            {
                "host_id": host_id,
                "cwd": str(system.workspace),
                "title": "Runtime settings acceptance",
                "execution_settings": settings,
            },
        )
        thread_id = result["thread"]["id"]
        completed = system.command(result["command"])
        effective = completed["result"]["effective_settings"]
        for key, value in settings.items():
            assert effective[key] == value, (key, effective)
        system.send(
            thread_id, f"Do not use tools. Remember this test nonce: {nonce}. Reply only {nonce}."
        )
        _, text = system.answer(thread_id)
        assert nonce in text, text
        return {
            "thread_id": thread_id,
            "model": effective["model"],
            "effort": effective["effort"],
            "approval_policy": effective["approval_policy"],
            "sandbox_mode": effective["sandbox_mode"],
        }

    system.record("real_selected_model_effort_and_readonly_turn", create)

    def next_settings():
        alternate = next((value for value in efforts if value != effort), effort)
        requested = dict(settings, effort=alternate)
        response = system.client.patch(
            f"/api/threads/{thread_id}/settings",
            json={
                "execution_settings": requested,
                "settings_revision": 0,
            },
            headers={"X-CSRF-Token": system.csrf},
        )
        assert response.status_code == 200, response.text
        current = response.json()["thread"]
        assert current["settings_revision"] == 1
        assert current["effective_settings_revision"] == 0
        system.send(thread_id, "Do not use tools. Reply only SETTINGS_NEXT_TURN_OK.")
        history, text = system.answer(thread_id)
        assert "SETTINGS_NEXT_TURN_OK" in text, text
        assert history["thread"]["effective_settings"]["effort"] == alternate
        return {
            "desired_revision": 1,
            "effective_revision": history["thread"]["effective_settings_revision"],
            "effort": alternate,
        }

    system.record("real_next_turn_settings_application", next_settings)

    def partial_history():
        # Evict only this test's own query projection. Native history is unchanged.
        with system.app.app_context(), system.app.extensions["broker"].command_lock:
            Item.query.filter_by(thread_id=thread_id).delete()
            thread = db.session.get(Thread, thread_id)
            thread.history_loaded = False
            thread.history_complete = False
            thread.history_cursor = None
            thread.history_generation = None
            thread.history_stale = True
            db.session.commit()
        started = system.post(f"/api/threads/{thread_id}/history", {"limit": 1})
        assert started["command"]["kind"] == "thread.history"
        system.command(started["command"])
        detail = wait_for(
            lambda: (
                value if (value := system.history(thread_id))["thread"]["history_loaded"] else None
            ),
            "native finite page",
            system.timeout,
        )
        assert len(detail["items"]) == 1
        assert detail["thread"]["history_cursor"] and not detail["thread"]["history_complete"]
        original_position = detail["items"][0]["position"]
        older = system.post(
            f"/api/threads/{thread_id}/history",
            {
                "cursor": detail["thread"]["history_cursor"],
                "limit": 1,
            },
        )
        system.command(older["command"])
        detail = system.history(thread_id)
        assert len(detail["items"]) == 2
        assert detail["items"][-1]["position"] == original_position
        assert detail["thread"]["history_complete"] is False
        return {"cached_items": 2, "complete": False, "stable_positions": True}

    system.record("real_native_partial_history_and_older_cursor", partial_history)

    def restart_and_continue():
        service, loop = system.holder["service"], system.holder["loop"]
        previous = service.runtime.runtime_id
        asyncio.run_coroutine_threadsafe(service.runtime.close(), loop).result(timeout=25)
        wait_for(
            lambda: service.runtime.runtime_id != previous and system.online(),
            "isolated runtime restart",
            45,
        )
        native_id = system.history(thread_id)["thread"]["native_id"]
        system.send(
            thread_id,
            "Do not use tools. Recall the SETTINGS_CONTEXT_ nonce from our first message and reply only that exact nonce.",
        )
        detail, text = system.answer(thread_id)
        assert nonce in text, text
        assert detail["thread"]["native_id"] == native_id
        assert detail["thread"]["effective_settings_revision"] == 1
        assert detail["thread"]["effective_settings"]["sandbox_mode"] == "read-only"
        return {
            "original_native_thread": True,
            "recalled_unloaded_history": True,
            "settings_survived_restart": True,
        }

    system.record("real_resume_original_thread_with_partial_browser_history", restart_and_continue)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5013)
    parser.add_argument("--codex-bin", default="codex")
    args = parser.parse_args()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    directory = (
        Path("instance/live-tests")
        / (time.strftime("%Y%m%d-%H%M%S") + "-runtime-" + uuid4().hex[:6])
    ).resolve()
    directory.mkdir(parents=True)
    system = LiveSystem(directory, args.port, args.codex_bin, 180)
    try:
        run(system)
        print(f"REPORT={directory / 'report.json'}", flush=True)
    finally:
        system.close()


if __name__ == "__main__":
    main()
