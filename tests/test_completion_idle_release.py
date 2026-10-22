"""One release clock: confirmed last turn completion plus 120 monotonic seconds."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from test_thread_ownership_node import cleanup, command, notify, writer_request
from test_thread_ownership_node import make_service as writer_service

from codex_anywhere.node import service as service_module
from codex_anywhere.node.runtime import RuntimeGone


def clock_for(monkeypatch):
    clock = {"wall": time.time(), "monotonic": 1000.0}
    monkeypatch.setattr(
        service_module,
        "time",
        SimpleNamespace(time=lambda: clock["wall"], monotonic=lambda: clock["monotonic"]),
    )
    return clock


async def make_service(tmp_path):
    service, runtime = await writer_service(tmp_path)
    service.store.save_thread("native", str(tmp_path))
    return service, runtime


async def start(service, text="work"):
    return await service.handle_command(
        command("turn.start", input=[{"type": "text", "text": text}])
    )


async def complete(runtime, turn_id="turn-1"):
    await notify(
        runtime,
        "turn/completed",
        {"threadId": "native", "turn": {"id": turn_id, "status": "completed"}},
    )


async def wait_event(service, method):
    async with asyncio.timeout(1):
        while not any(event["method"] == method for event in service.store.pending_events()):
            await asyncio.sleep(0)


def test_manual_unload_and_new_message_share_the_native_thread_lock(tmp_path):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        entered, proceed = asyncio.Event(), asyncio.Event()
        tasks = []
        original = runtime.request
        calls = []

        async def request(method, params):
            if method == "thread/unsubscribe":
                entered.set()
                await proceed.wait()
            calls.append(method)
            return await original(method, params)

        try:
            await start(service)
            await complete(runtime)
            runtime.request = request
            release = asyncio.create_task(service.handle_command(
                command("thread.release", manual=True, **writer_request())
            ))
            tasks.append(release)
            async with asyncio.timeout(2):
                await entered.wait()
                message = asyncio.create_task(start(service, "next"))
                tasks.append(message)
                while not service.pending_thread_commands.get("native"):
                    await asyncio.sleep(0)
                assert not message.done()
                assert "turn/start" not in calls
                proceed.set()
                assert (await release)["result"]["released"] is True
                assert "error" not in await message
            assert "native" in runtime.loaded_threads
            assert service.active_turns["native"] == "turn-2"
            assert calls.index("thread/unsubscribe") < calls.index("turn/start")
        finally:
            proceed.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await cleanup(service, runtime)

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["completed", "failed", "interrupted"])
def test_native_terminal_result_without_event_starts_exact_120_second_window(
    tmp_path, monkeypatch, terminal
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        try:
            result = await start(service, terminal)
            assert result["result"]["turn"]["status"] == terminal
            assert service.completion_idle_since["native"] == 1000
            clock["monotonic"] = 1119.999
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            clock["monotonic"] = 1120
            await service._sweep_threads_once()
            assert "native" not in runtime.loaded_threads
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_running_turn_never_becomes_idle_from_wall_clock_or_browser_silence(tmp_path, monkeypatch):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        try:
            await start(service)
            clock.update(wall=clock["wall"] + 1000000, monotonic=1000000)
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            assert "native" not in service.completion_idle_since
            await complete(runtime)
            ended = clock["monotonic"]
            clock.update(wall=1, monotonic=ended + 119)
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            clock["monotonic"] = ended + 120
            await service._sweep_threads_once()
            assert "native" not in runtime.loaded_threads
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_new_accepted_message_cancels_old_deadline_and_next_completion_restarts_it(
    tmp_path, monkeypatch
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        try:
            first = command("turn.start", input=[{"type": "text", "text": "work"}])
            await service.handle_command(first)
            await complete(runtime)
            clock["monotonic"] = 1119
            await start(service)
            assert "native" not in service.completion_idle_since
            clock["monotonic"] = 1300
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            await complete(runtime, "turn-2")
            assert service.completion_idle_since["native"] == 1300
            # A redelivery of an older accepted message is only a journal read.
            await service.handle_command(first)
            assert service.completion_idle_since["native"] == 1300
            clock["monotonic"] = 1419
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            clock["monotonic"] = 1420
            await service._sweep_threads_once()
            assert "native" not in runtime.loaded_threads
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_legacy_presence_and_release_neither_shorten_nor_extend_completion_window(
    tmp_path, monkeypatch
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        try:
            await start(service)
            await complete(runtime)
            clock["monotonic"] = 1119
            for kind in ["thread.presence", "thread.release"]:
                result = await service.handle_command(
                    command(kind, **writer_request(expires_at=clock["wall"] + 10000))
                )
                assert result["result"]["ignored"] is True
            assert service.completion_idle_since["native"] == 1000
            assert not service.writer_request_versions
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            clock["monotonic"] = 1120
            await service._sweep_threads_once()
            assert "native" not in runtime.loaded_threads
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_takeover_or_resume_without_a_completed_turn_never_invents_a_deadline(
    tmp_path, monkeypatch
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        try:
            await service.handle_command(command("thread.takeover", **writer_request()))
            clock["monotonic"] += 1000000
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            assert not service.completion_idle_since
            assert not hasattr(service, "thread_leases")
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_late_or_duplicate_completion_cannot_reset_or_replace_the_latest_turn(
    tmp_path, monkeypatch
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        try:
            await start(service)
            await complete(runtime)
            clock["monotonic"] = 1050
            await complete(runtime)
            assert service.completion_idle_since["native"] == 1000
            await start(service)
            await complete(runtime, "turn-1")
            await complete(runtime, "unknown-older-turn")
            assert "native" not in service.completion_idle_since
            assert service.active_turns["native"] == "turn-2"
            clock["monotonic"] = 1100
            await complete(runtime, "turn-2")
            clock["monotonic"] = 1150
            await complete(runtime, "turn-1")
            await complete(runtime, "turn-2")
            assert service.completion_idle_since["native"] == 1100
            assert service.latest_turns["native"] == "turn-2"
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_unknown_old_completion_while_start_rpc_pending_cannot_release_new_running_turn(
    tmp_path, monkeypatch
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        task = None
        try:
            task = asyncio.create_task(start(service, "defer"))
            await wait_event(service, "test/turnPending")
            await complete(runtime, "unknown-older-turn")
            assert "native" not in service.completion_idle_since
            clock["monotonic"] += 500
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            await runtime.request("finishTurnRpc", {})
            await task
            assert service.active_turns["native"] == "turn-1"
            assert "native" not in service.completion_idle_since
            clock["monotonic"] += 500
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await cleanup(service, runtime)

    asyncio.run(scenario())


@pytest.mark.parametrize("started_first", [False, True])
def test_completion_before_start_response_keeps_original_monotonic_deadline(
    tmp_path, monkeypatch, started_first
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        task = None
        try:
            task = asyncio.create_task(start(service, "fast-defer" if started_first else "defer"))
            await wait_event(service, "test/turnPending")
            if not started_first:
                await complete(runtime, "turn-1")
                assert "native" not in service.completion_idle_since
            clock["monotonic"] = 1100
            await runtime.request("finishTurnRpc", {})
            await task
            assert service.completion_idle_since["native"] == 1000
            clock["monotonic"] = 1119
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            clock["monotonic"] = 1120
            await service._sweep_threads_once()
            assert "native" not in runtime.loaded_threads
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_definite_native_rejection_restores_original_completion_without_renewing_it(
    tmp_path, monkeypatch
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        try:
            await start(service)
            await complete(runtime)
            clock["monotonic"] = 1119
            result = await start(service, "reject")
            assert "Native start rejected" in result["error"]
            assert service.completion_idle_since["native"] == 1000
            clock["monotonic"] = 1120
            await service._sweep_threads_once()
            assert "native" not in runtime.loaded_threads
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_unknown_start_outcome_does_not_restore_or_invent_idle_completion(tmp_path, monkeypatch):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        try:
            await start(service)
            await complete(runtime)
            original = runtime.request
            calls = []

            async def unknown(method, params):
                if method == "turn/start":
                    calls.append(method)
                    raise RuntimeGone("Native outcome unknown")
                return await original(method, params)

            runtime.request = unknown
            message = command("turn.start", input=[{"type": "text", "text": "new"}])
            result = await service.handle_command(message)
            assert result["unknown"] is True
            assert "native" not in service.completion_idle_since
            clock["monotonic"] += 10000
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            assert await service.handle_command(message) == result
            assert calls == ["turn/start"]
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_manual_release_during_running_turn_has_no_release_on_completion_queue(
    tmp_path, monkeypatch
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        try:
            await start(service)
            released = await service.handle_command(
                command("thread.release", manual=True, **writer_request())
            )
            assert released["result"]["deferred"] is True
            assert "native" not in service.completion_idle_since
            clock["monotonic"] += 600
            await complete(runtime)
            ended = clock["monotonic"]
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            clock["monotonic"] = ended + 119
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            clock["monotonic"] = ended + 120
            await service._sweep_threads_once()
            assert "native" not in runtime.loaded_threads
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


@pytest.mark.parametrize("pending_kind", ["steer", "approval"])
def test_pending_control_rpc_blocks_unsubscribe_even_after_a_confirmed_end(
    tmp_path, monkeypatch, pending_kind
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        task = None
        answer_gate = asyncio.Event()
        try:
            await start(service)
            if pending_kind == "steer":
                task = asyncio.create_task(
                    service.handle_command(
                        command(
                            "turn.steer",
                            input=[{"type": "text", "text": "defer"}],
                            expected_turn_id="turn-1",
                        )
                    )
                )
                await wait_event(service, "test/steerPending")
            else:
                await notify(
                    runtime,
                    "item/commandExecution/requestApproval",
                    {"threadId": "native"},
                    rpc_id=123,
                )
                begun = asyncio.Event()

                async def delayed_answer(rpc_id, response):
                    runtime.server_requests.pop(json.dumps(rpc_id), None)
                    begun.set()
                    await answer_gate.wait()
                    return {"answered": True}

                runtime.answer = delayed_answer
                task = asyncio.create_task(
                    service.handle_command(
                        command(
                            "interaction.answer",
                            rpc_id=123,
                            response={"decision": "accept"},
                            runtime_id=runtime.runtime_id,
                        )
                    )
                )
                await begun.wait()
            await complete(runtime)
            clock["monotonic"] += 600
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            released = await service.handle_command(
                command("thread.release", manual=True, **writer_request())
            )
            assert released["result"]["deferred"] is True
            if pending_kind == "steer":
                await runtime.request("finishSteerRpc", {})
            else:
                answer_gate.set()
            await task
            await service._sweep_threads_once()
            assert "native" not in runtime.loaded_threads
        finally:
            answer_gate.set()
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_manual_release_version_blocks_takeover_before_external_process_action(tmp_path):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        entered = asyncio.Event()
        gate = threading.Event()
        loop = asyncio.get_running_loop()
        original = service.ownership.inspect
        task = None
        try:
            service.ownership.state = "external"

            def inspect(thread_id, *, managed=False):
                loop.call_soon_threadsafe(entered.set)
                gate.wait(1)
                return original(thread_id, managed=managed)

            service.ownership.inspect = inspect
            task = asyncio.create_task(
                service.handle_command(
                    command(
                        "thread.takeover",
                        confirmed=True,
                        owner_token="token",
                        **writer_request(issued_at=10),
                    )
                )
            )
            await entered.wait()
            released = await service.handle_command(
                command("thread.release", manual=True, **writer_request(issued_at=20))
            )
            assert released["result"]["deferred"] is True
            gate.set()
            result = await task
            assert "stale" in result["error"]
            assert service.ownership.terminated == []
            assert "native" not in runtime.loaded_threads
        finally:
            gate.set()
            if task and not task.done():
                await asyncio.gather(task, return_exceptions=True)
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_completion_cache_is_bounded_and_keeps_latest_timer_on_old_duplicates(
    tmp_path, monkeypatch
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock_for(monkeypatch)
        try:
            await start(service)
            await complete(runtime)
            for number in range(150):
                service._confirm_turn_end("native", f"old-{number}")
            assert len(service.completed_turns["native"]) <= 100
            assert "turn-1" in service.completed_turns["native"]
            await complete(runtime)
            assert service.completion_idle_since["native"] == 1000
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_refused_new_start_restores_older_turn_completion_observed_during_pending_rpc(
    tmp_path, monkeypatch
):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        task = None
        try:
            await start(service)
            task = asyncio.create_task(start(service, "defer"))
            await wait_event(service, "test/turnPending")
            clock["monotonic"] = 1050
            await complete(runtime, "turn-1")
            assert "native" not in service.completion_idle_since
            clock["monotonic"] = 1100
            await runtime.request("finishTurnRpc", {"reject": True})
            rejected = await task
            assert "Native start rejected" in rejected["error"]
            assert service.completion_idle_since["native"] == 1050
            assert "native" not in service.active_turns
            clock["monotonic"] = 1169
            await service._sweep_threads_once()
            assert "native" in runtime.loaded_threads
            clock["monotonic"] = 1170
            await service._sweep_threads_once()
            assert "native" not in runtime.loaded_threads
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await cleanup(service, runtime)

    asyncio.run(scenario())


def test_failed_automatic_unsubscribe_is_not_retried_by_later_sweeps(tmp_path, monkeypatch):
    async def scenario():
        service, runtime = await make_service(tmp_path)
        clock = clock_for(monkeypatch)
        try:
            await service.handle_command(command("thread.takeover", "unsupported"))
            await notify(
                runtime, "turn/started", {"threadId": "unsupported", "turn": {"id": "done"}}
            )
            await notify(
                runtime,
                "turn/completed",
                {"threadId": "unsupported", "turn": {"id": "done", "status": "completed"}},
            )
            clock["monotonic"] = 1120
            await service._sweep_threads_once()
            assert "unsupported" in service.release_errors
            clock["monotonic"] += 600
            await service._sweep_threads_once()
            calls = (await runtime.request("inspect", {}))["calls"]
            assert sum(method == "thread/unsubscribe" for method, _ in calls) == 1
            assert "unsupported" in runtime.loaded_threads
        finally:
            await cleanup(service, runtime)

    asyncio.run(scenario())
