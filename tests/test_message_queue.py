"""Queue guarantees against a fake node, without browser or inference lifetimes."""

import copy
import json
import time
from threading import Event as ThreadEvent
from threading import Thread as WorkerThread
from uuid import uuid4

import pytest
from werkzeug.exceptions import Conflict, NotFound

from codex_anywhere.hub.message_queue import (
    cancel_queued,
    dispatch_queue,
    dispatch_ready_queues,
    enqueue_prepared,
    mark_queue_paused,
    queue_state,
    resume_queue,
    should_enqueue,
    update_queued,
)
from codex_anywhere.hub.models import Command, Event, Host, Interaction, Thread, db
from codex_anywhere.hub.transport import handle_node_message


@pytest.fixture
def queued_thread(app, monkeypatch):
    delivered = []
    broker = app.extensions["broker"]
    monkeypatch.setattr(broker, "is_online", lambda host_id: True)
    monkeypatch.setattr(broker, "send_command", lambda command: delivered.append(command) or True)
    # These unit tests drive dispatch explicitly. HTTP/transport integration tests
    # independently cover the automatic, post-commit batch entry point.
    monkeypatch.setattr(
        "codex_anywhere.hub.message_queue.dispatch_ready_queues", lambda *args, **kwargs: []
    )
    with app.app_context():
        host = Host(
            name="queue-node",
            platform="test",
            token_hash=uuid4().hex,
            runtime_id="queue-runtime",
            capabilities=["runtimeSettings", "serviceTierWrite"],
        )
        db.session.add(host)
        db.session.flush()
        thread = Thread(
            host_id=host.id,
            native_id="native-queue-thread",
            title="Queue test",
            cwd="/workspace",
            status="idle",
            history_loaded=True,
            execution_settings={"model": "model-a", "service_tier": "default"},
        )
        db.session.add(thread)
        db.session.commit()
        identity = host.id, thread.id
    return {"host_id": identity[0], "thread_id": identity[1], "delivered": delivered}


def prepared(text="next message", **extra):
    return {
        "native_thread_id": "native-queue-thread",
        "input": [{"type": "text", "text": text}],
        "_display_text": text,
        "_assets": [],
        "_references": [],
        "_signature": "signature:" + text,
        **extra,
    }


def add_queued(app, identity, text="next message", **extra):
    with app.app_context():
        host = db.session.get(Host, identity["host_id"])
        thread = db.session.get(Thread, identity["thread_id"])
        command = enqueue_prepared(host, thread, prepared(text, **extra), uuid4().hex)
        return command.id


def set_thread(app, identity, **fields):
    with app.app_context():
        thread = db.session.get(Thread, identity["thread_id"])
        for name, value in fields.items():
            setattr(thread, name, value)
        db.session.commit()


def finish_turn(app, identity, command, status="completed", event_id=None):
    for method, turn in (
        ("turn/started", {"id": "turn-" + command.id}),
        ("turn/completed", {"id": "turn-" + command.id, "status": status}),
    ):
        handle_node_message(
            app,
            identity["host_id"],
            {
                "type": "event",
                "runtime_id": "queue-runtime",
                "event_id": event_id or uuid4().hex,
                "method": method,
                "params": {"threadId": "native-queue-thread", "turn": turn},
            },
        )
    handle_node_message(
        app,
        identity["host_id"],
        {"type": "command_result", "id": command.id, "result": {"turn": turn}},
    )


def test_enqueue_is_durable_private_and_idempotent_without_delivery(app, queued_thread):
    identity = queued_thread
    image = "data:image/png;base64,PRIVATE_IMAGE"
    payload = prepared(
        input=[{"type": "text", "text": "next message"}, {"type": "image", "url": image}],
        _assets=[{"id": "image-id", "name": "photo.png", "path": "PRIVATE_PATH"}],
        _references=[{"title": "selected", "excerpt": "quoted", "secret": "PRIVATE_SECRET"}],
        execution_settings={"model": "old-model"},
        settings_revision=1,
    )
    with app.app_context():
        host = db.session.get(Host, identity["host_id"])
        thread = db.session.get(Thread, identity["thread_id"])
        first = enqueue_prepared(host, thread, payload, "same-request")
        repeated = enqueue_prepared(host, thread, payload, "same-request")
        assert repeated.id == first.id
        assert Command.query.count() == 1
        assert first.kind == "turn.queue" and first.status == "pending"
        assert "execution_settings" not in first.payload
        assert first.runtime_id is None
        state = queue_state(thread)
        assert state["messages"][0]["revision"] == 0
        assert "PRIVATE" not in json.dumps(state)
        assert Event.query.filter_by(method="anywhere/message_queue").count() == 1
        assert not identity["delivered"]
        with pytest.raises(Conflict):
            enqueue_prepared(host, thread, prepared("changed"), "same-request")


def test_fifo_runs_once_after_completion_and_browser_can_be_absent(app, queued_thread):
    first = add_queued(app, queued_thread, "first")
    second = add_queued(app, queued_thread, "second")
    set_thread(app, queued_thread, status="active", active_turn_id="prior-running-turn")
    assert dispatch_queue(app) is None
    set_thread(app, queued_thread, status="idle", active_turn_id=None)
    command = dispatch_queue(app)
    assert command.id == first
    assert dispatch_queue(app) is None
    finish_turn(app, queued_thread, command)
    following = dispatch_queue(app)
    assert following.id == second
    assert len(queued_thread["delivered"]) == 2
    assert dispatch_queue(app) is None
    with app.app_context():
        assert Command.query.filter_by(id=first).one().status == "completed"
        assert Command.query.filter_by(id=second).one().status == "sent"


def test_completion_before_start_result_waits_for_definitive_command(app, queued_thread):
    first = add_queued(app, queued_thread, "first")
    second = add_queued(app, queued_thread, "second")
    command = dispatch_queue(app)
    assert command.id == first
    set_thread(app, queued_thread, status="idle", active_turn_id=None)
    assert dispatch_queue(app) is None  # The original start RPC has not settled.
    handle_node_message(
        app,
        queued_thread["host_id"],
        {"type": "command_result", "id": first, "result": {"turn": {"id": "finished"}}},
    )
    assert dispatch_queue(app).id == second
    assert len(queued_thread["delivered"]) == 2


def test_fast_result_is_not_overwritten_as_sent(app, queued_thread, monkeypatch):
    first = add_queued(app, queued_thread)

    def instant(command):
        with app.app_context():
            saved = db.session.get(Command, command["wire"]["id"])
            assert saved.kind == "turn.start" and saved.status == "queued"
            assert Event.query.filter_by(method="anywhere/message_queue").count() == 2
        handle_node_message(
            app,
            queued_thread["host_id"],
            {"type": "command_result", "id": first, "result": {"turn": {"id": "fast"}}},
        )
        return True

    monkeypatch.setattr(app.extensions["broker"], "send_command", instant)
    command = dispatch_queue(app)
    assert command.status == "completed"


@pytest.mark.parametrize("interaction_status", ["pending", "answering"])
def test_approval_and_user_input_guard_even_when_projection_is_idle(
    app, queued_thread, interaction_status
):
    add_queued(app, queued_thread)
    with app.app_context():
        db.session.add(
            Interaction(
                host_id=queued_thread["host_id"],
                thread_id=queued_thread["thread_id"],
                runtime_id="queue-runtime",
                rpc_id="approval-1",
                method="item/commandExecution/requestApproval",
                params={},
                status=interaction_status,
            )
        )
        db.session.commit()
        state = queue_state(db.session.get(Thread, queued_thread["thread_id"]))
        assert not state["paused"]
        assert "审批" in state["reason"]
    assert dispatch_queue(app) is None
    with pytest.raises(Conflict):
        resume_queue(app, queued_thread["thread_id"])


def test_settings_are_selected_at_dispatch_and_explicit_override_is_kept(app, queued_thread):
    add_queued(app, queued_thread, _queue_overrides={"service_tier": "fast"})
    set_thread(
        app,
        queued_thread,
        execution_settings={"model": "new-model", "service_tier": "default", "effort": "high"},
        settings_revision=9,
    )
    command = dispatch_queue(app)
    assert command.payload["execution_settings"] == {
        "model": "new-model",
        "service_tier": "fast",
        "effort": "high",
    }
    assert command.payload["settings_revision"] == 9


@pytest.mark.parametrize("status", ["error", "interrupted"])
def test_failed_or_interrupted_turn_requires_explicit_resume(app, queued_thread, status):
    command_id = add_queued(app, queued_thread)
    set_thread(app, queued_thread, status=status, last_error="prior failure")
    assert dispatch_queue(app) is None
    with app.app_context():
        assert queue_state(db.session.get(Thread, queued_thread["thread_id"]))["paused"]
    assert resume_queue(app, queued_thread["thread_id"]).id == command_id
    with app.app_context():
        thread = db.session.get(Thread, queued_thread["thread_id"])
        assert thread.status == "active" and thread.last_error is None


def test_unknown_never_retries_and_resume_acknowledges_without_losing_journal(app, queued_thread):
    first = add_queued(app, queued_thread, "first")
    second = add_queued(app, queued_thread, "second")
    with app.app_context():
        unknown = Command(
            host_id=queued_thread["host_id"],
            thread_id=queued_thread["thread_id"],
            kind="turn.start",
            payload={"_signature": "original"},
            runtime_id="queue-runtime",
            status="unknown",
            result={"preserved": True},
        )
        db.session.add(unknown)
        db.session.commit()
        unknown_id = unknown.id
    assert dispatch_queue(app) is None
    assert resume_queue(app, queued_thread["thread_id"]).id == first
    with app.app_context():
        saved = db.session.get(Command, unknown_id)
        assert saved.status == "unknown"
        assert saved.result == {"preserved": True}
        assert saved.payload["_signature"] == "original"
        assert saved.payload["_queue_resume_ack"] > 0
    finish_turn(app, queued_thread, dispatch_result(app, first))
    assert dispatch_queue(app).id == second
    assert [entry["wire"]["id"] for entry in queued_thread["delivered"]] == [first, second]


def dispatch_result(app, command_id):
    with app.app_context():
        command = db.session.get(Command, command_id)
        db.session.expunge(command)
        return command


@pytest.mark.parametrize("kind", ["thread.history", "thread.ownership", "runtime.capabilities"])
def test_readonly_unknown_does_not_block_a_new_turn(app, queued_thread, kind):
    next_id = add_queued(app, queued_thread)
    with app.app_context():
        db.session.add(
            Command(
                host_id=queued_thread["host_id"],
                thread_id=queued_thread["thread_id"],
                kind=kind,
                payload={},
                runtime_id="queue-runtime",
                status="unknown",
            )
        )
        db.session.commit()
    assert dispatch_queue(app).id == next_id


def test_offline_queue_waits_and_recovers_without_resume(app, queued_thread, monkeypatch):
    command_id = add_queued(app, queued_thread)
    broker = app.extensions["broker"]
    monkeypatch.setattr(broker, "is_online", lambda host_id: False)
    assert dispatch_queue(app) is None
    with app.app_context():
        state = queue_state(db.session.get(Thread, queued_thread["thread_id"]))
        assert not state["paused"]
        assert "重新连接" in state["reason"]
    with pytest.raises(Conflict):
        resume_queue(app, queued_thread["thread_id"])
    monkeypatch.setattr(broker, "is_online", lambda host_id: True)
    assert dispatch_queue(app).id == command_id


def test_edit_keeps_original_attachments_references_and_signature(app, queued_thread):
    asset = {"id": "immutable-asset", "name": "photo.png"}
    reference = {"kind": "web", "title": "snapshot", "url": "https://example.test", "excerpt": "old"}
    image = {"type": "image", "url": "data:image/png;base64,ORIGINAL"}
    command_id = add_queued(
        app,
        queued_thread,
        input=[{"type": "text", "text": "next message"}, image],
        _assets=[asset],
        _references=[reference],
    )
    with app.app_context():
        thread = db.session.get(Thread, queued_thread["thread_id"])
        command = update_queued(thread, command_id, "updated", 0)
        assert command.payload["_assets"] == [asset]
        assert command.payload["_references"] == [reference]
        assert command.payload["input"][1] == image
        assert command.payload["_signature"] == "signature:next message"
        assert command.payload["_queue_revision"] == 1
        assert command.payload["_display_text"] == "updated"
        assert "old" in command.payload["input"][0]["text"]
        with pytest.raises(Conflict):
            update_queued(thread, command_id, "stale edit", 0)
        # Text may be removed while attachments or immutable references remain.
        assert update_queued(thread, command_id, "", 1).payload["_queue_revision"] == 2


def test_edit_and_cancel_lose_to_dispatch_and_cannot_touch_other_threads(app, queued_thread):
    command_id = add_queued(app, queued_thread)
    dispatch_queue(app)
    with app.app_context():
        thread = db.session.get(Thread, queued_thread["thread_id"])
        with pytest.raises(Conflict):
            update_queued(thread, command_id, "too late", 0)
        with pytest.raises(Conflict):
            cancel_queued(thread, command_id, 0)
        with pytest.raises(NotFound):
            cancel_queued(thread, "not-here", 0)


def test_cancel_preserves_journal_and_moves_to_next_fifo(app, queued_thread):
    first = add_queued(app, queued_thread, "first")
    second = add_queued(app, queued_thread, "second")
    with app.app_context():
        thread = db.session.get(Thread, queued_thread["thread_id"])
        command = cancel_queued(thread, first, 0)
        assert command.status == "cancelled" and command.payload["_queue_revision"] == 1
        assert Command.query.count() == 2
        assert [message["id"] for message in queue_state(thread)["messages"]] == [second]
        with pytest.raises(Conflict):
            cancel_queued(thread, first, 0)
    assert dispatch_queue(app).id == second


def test_empty_oversize_and_invalid_unicode_edits_are_rejected(app, queued_thread):
    command_id = add_queued(app, queued_thread)
    with app.app_context():
        thread = db.session.get(Thread, queued_thread["thread_id"])
        for text in (" ", "x" * 100_001, "\ud800"):
            with pytest.raises(ValueError):
                update_queued(thread, command_id, text, 0)


def test_duplicate_completed_event_cannot_run_an_extra_message(app, queued_thread):
    first = add_queued(app, queued_thread, "first")
    second = add_queued(app, queued_thread, "second")
    command = dispatch_queue(app)
    finish_turn(app, queued_thread, command)
    following = dispatch_queue(app)
    assert following.id == second
    for _ in range(2):
        handle_node_message(
            app,
            queued_thread["host_id"],
            {
                "type": "event",
                "runtime_id": "queue-runtime",
                "event_id": "duplicate-completion",
                "method": "turn/completed",
                "params": {
                    "threadId": "native-queue-thread",
                    "turn": {"id": "turn-" + first, "status": "completed"},
                },
            },
        )
        assert dispatch_queue(app) is None
    assert len(queued_thread["delivered"]) == 2


def test_delivery_failure_keeps_only_the_committed_command_without_resending(
    app, queued_thread, monkeypatch
):
    command_id = add_queued(app, queued_thread)
    attempts = []

    def unavailable(command):
        attempts.append(copy.deepcopy(command))
        return False

    monkeypatch.setattr(app.extensions["broker"], "send_command", unavailable)
    command = dispatch_queue(app)
    assert command.id == command_id and command.status == "queued"
    assert dispatch_queue(app) is None
    assert len(attempts) == 1


def test_fifo_tie_is_stable_and_invalid_head_is_not_overtaken(app, queued_thread):
    first = add_queued(app, queued_thread, "first", _queue_overrides={"invalid-setting": "invalid"})
    second = add_queued(app, queued_thread, "second")
    with app.app_context():
        commands = Command.query.order_by(Command.created_at).all()
        commands[0].created_at = time.time() - 5
        commands[1].created_at = commands[0].created_at + 1
        db.session.commit()
    assert dispatch_queue(app) is None
    with app.app_context():
        assert db.session.get(Command, first).kind == "turn.queue"
        assert db.session.get(Command, second).kind == "turn.queue"
        state = queue_state(db.session.get(Thread, queued_thread["thread_id"]))
        assert state["paused"] and "运行设置" in state["reason"]


def test_reconnect_batch_recovers_each_idle_thread_once(app, queued_thread):
    first = add_queued(app, queued_thread, "first")
    add_queued(app, queued_thread, "later on first thread")
    with app.app_context():
        other = Thread(
            host_id=queued_thread["host_id"],
            native_id="second-native",
            title="Second queued task",
            cwd="/workspace",
            status="idle",
            history_loaded=True,
        )
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    second = add_queued(app, dict(queued_thread, thread_id=other_id), "second thread")
    commands = dispatch_ready_queues(app, queued_thread["host_id"])
    assert {command.id for command in commands} == {first, second}
    assert dispatch_ready_queues(app, queued_thread["host_id"]) == []
    assert len(queued_thread["delivered"]) == 2


@pytest.mark.parametrize("kind", ["turn.start", "turn.steer", "thread.takeover", "interaction.answer"])
@pytest.mark.parametrize("status", ["queued", "sent", "running"])
def test_delivered_control_prevents_overlapping_turns(app, queued_thread, kind, status):
    add_queued(app, queued_thread)
    with app.app_context():
        db.session.add(
            Command(
                host_id=queued_thread["host_id"],
                thread_id=queued_thread["thread_id"],
                kind=kind,
                payload={},
                runtime_id="queue-runtime",
                status=status,
            )
        )
        db.session.commit()
    assert dispatch_queue(app) is None


def test_prior_runtime_approval_does_not_block_current_runtime(app, queued_thread):
    command_id = add_queued(app, queued_thread)
    with app.app_context():
        db.session.add(
            Interaction(
                host_id=queued_thread["host_id"],
                thread_id=queued_thread["thread_id"],
                runtime_id="old-runtime",
                rpc_id="old-approval",
                method="item/commandExecution/requestApproval",
                params={},
                status="pending",
            )
        )
        db.session.commit()
    assert dispatch_queue(app).id == command_id


@pytest.mark.parametrize(
    "fields",
    [{"history_loaded": False}, {"native_id": None}, {"status": "importing"}],
)
def test_unready_thread_does_not_dispatch_even_on_resume(app, queued_thread, fields):
    add_queued(app, queued_thread)
    set_thread(app, queued_thread, **fields)
    assert dispatch_queue(app) is None
    with pytest.raises(Conflict):
        resume_queue(app, queued_thread["thread_id"])


def test_delivery_exception_cannot_erase_the_durable_decision(app, queued_thread, monkeypatch):
    command_id = add_queued(app, queued_thread)

    def fail(command):
        raise OSError("socket unavailable")

    monkeypatch.setattr(app.extensions["broker"], "send_command", fail)
    with pytest.raises(OSError):
        dispatch_queue(app)
    with app.app_context():
        command = db.session.get(Command, command_id)
        assert command.kind == "turn.start" and command.status == "queued"
        assert db.session.get(Thread, queued_thread["thread_id"]).status == "active"
    assert dispatch_queue(app) is None


@pytest.mark.parametrize("action", ["edit", "cancel"])
def test_concurrent_edit_or_cancel_cannot_mutate_a_dispatch_in_flight(
    app, queued_thread, monkeypatch, action
):
    command_id = add_queued(app, queued_thread)
    delivering = ThreadEvent()
    may_deliver = ThreadEvent()
    editing = ThreadEvent()
    outcomes = []

    def delivery(command):
        delivering.set()
        assert may_deliver.wait(5)
        return True

    def dispatch_worker():
        try:
            outcomes.append(dispatch_queue(app).id)
        except BaseException as exc:
            outcomes.append(exc)

    def edit_worker():
        try:
            with app.app_context():
                thread = db.session.get(Thread, queued_thread["thread_id"])
                editing.set()
                if action == "edit":
                    update_queued(thread, command_id, "racing edit", 0)
                else:
                    cancel_queued(thread, command_id, 0)
        except BaseException as exc:
            outcomes.append(exc)

    monkeypatch.setattr(app.extensions["broker"], "send_command", delivery)
    dispatch = WorkerThread(target=dispatch_worker)
    mutation = WorkerThread(target=edit_worker)
    dispatch.start()
    try:
        assert delivering.wait(5)
        mutation.start()
        assert editing.wait(5)
    finally:
        may_deliver.set()
        dispatch.join(timeout=5)
        if mutation.ident is not None:
            mutation.join(timeout=5)
    assert not dispatch.is_alive() and not mutation.is_alive()
    assert command_id in outcomes
    assert sum(isinstance(outcome, Conflict) for outcome in outcomes) == 1
    with app.app_context():
        saved = db.session.get(Command, command_id)
        assert saved.kind == "turn.start" and saved.status == "sent"
        assert saved.payload["_display_text"] == "next message"


def test_pause_survives_projection_changes_cancellation_new_messages_and_contexts(app, queued_thread):
    first = add_queued(app, queued_thread, "first")
    second = add_queued(app, queued_thread, "second")
    set_thread(app, queued_thread, status="interrupted", last_error=None)
    assert dispatch_queue(app) is None
    with app.app_context():
        assert all(command.payload.get("_queue_pause") for command in Command.query.all())
    # Native desktop activity can legitimately replace this status projection.
    set_thread(app, queued_thread, status="idle", last_error=None)
    with app.app_context():
        thread = db.session.get(Thread, queued_thread["thread_id"])
        cancel_queued(thread, first, 0)
    third = add_queued(app, queued_thread, "new message after interruption")
    with app.app_context():
        assert db.session.get(Command, third).payload.get("_queue_pause")
        thread = db.session.get(Thread, queued_thread["thread_id"])
        assert queue_state(thread)["paused"]
        assert not db.session.dirty
        events = Event.query.count()
    for _ in range(3):
        assert dispatch_queue(app) is None
    with app.app_context():
        assert Event.query.count() == events  # Heartbeats do not rewrite the same pause.
    resumed = resume_queue(app, queued_thread["thread_id"])
    assert resumed.id == second
    with app.app_context():
        assert "_queue_pause" not in db.session.get(Command, third).payload
        assert "_queue_pause" not in db.session.get(Command, second).payload
    finish_turn(app, queued_thread, resumed)
    assert dispatch_queue(app).id == third


def test_current_unknown_pause_remains_durable_after_definitive_late_result(app, queued_thread):
    queued_id = add_queued(app, queued_thread)
    with app.app_context():
        unknown = Command(
            host_id=queued_thread["host_id"],
            thread_id=queued_thread["thread_id"],
            kind="turn.start",
            payload={},
            runtime_id="queue-runtime",
            status="unknown",
        )
        db.session.add(unknown)
        db.session.commit()
        unknown_id = unknown.id
    assert dispatch_queue(app) is None
    with app.app_context():
        db.session.get(Command, unknown_id).status = "completed"
        db.session.commit()
    assert dispatch_queue(app) is None
    assert resume_queue(app, queued_thread["thread_id"]).id == queued_id


def test_batch_rotation_cannot_starve_a_thread_after_one_hundred_blocked_threads(app, queued_thread):
    with app.app_context():
        for index in range(100):
            blocked = Thread(
                host_id=queued_thread["host_id"],
                native_id=f"blocked-native-{index}",
                title="Running task",
                cwd="/workspace",
                status="active",
                active_turn_id=f"blocked-turn-{index}",
                history_loaded=True,
            )
            db.session.add(blocked)
            db.session.flush()
            db.session.add(
                Command(
                    host_id=queued_thread["host_id"],
                    thread_id=blocked.id,
                    kind="turn.queue",
                    payload=prepared("blocked pending"),
                    status="pending",
                    created_at=time.time() - 1000 + index,
                )
            )
        db.session.commit()
    eligible = add_queued(app, queued_thread, "ready after blocked tasks")
    assert dispatch_ready_queues(app, queued_thread["host_id"]) == []
    assert [command.id for command in dispatch_ready_queues(app, queued_thread["host_id"])] == [
        eligible
    ]
    assert len(queued_thread["delivered"]) == 1


def test_new_messages_share_runtime_command_guards_without_existing_queue(app, queued_thread):
    with app.app_context():
        host = db.session.get(Host, queued_thread["host_id"])
        thread = db.session.get(Thread, queued_thread["thread_id"])
        assert not should_enqueue(thread, host)
        unsettled = Command(
            host_id=host.id,
            thread_id=thread.id,
            runtime_id=host.runtime_id,
            kind="turn.start",
            status="running",
            payload={},
        )
        db.session.add(unsettled)
        db.session.commit()
        assert should_enqueue(thread, host)
        unsettled.status = "unknown"
        db.session.commit()
        assert should_enqueue(thread, host)
        unsettled.payload = {"_queue_resume_ack": time.time()}
        db.session.commit()
        assert not should_enqueue(thread, host)


def test_terminal_pause_helper_is_atomic_with_projection_and_never_commits(app, queued_thread):
    command_id = add_queued(app, queued_thread)
    with app.app_context():
        thread = db.session.get(Thread, queued_thread["thread_id"])
        original_events = Event.query.count()
        thread.status = "interrupted"
        # Read-only projection reports a pause without performing this mutation.
        assert queue_state(thread)["paused"]
        assert "_queue_pause" not in db.session.get(Command, command_id).payload
        assert mark_queue_paused(thread)
        assert not mark_queue_paused(thread)
        assert Event.query.count() == original_events + 1
        db.session.rollback()
    with app.app_context():
        assert "_queue_pause" not in db.session.get(Command, command_id).payload
        assert db.session.get(Thread, queued_thread["thread_id"]).status == "idle"
        assert Event.query.count() == original_events
        thread = db.session.get(Thread, queued_thread["thread_id"])
        thread.status = "interrupted"
        assert mark_queue_paused(thread)
        db.session.commit()
    set_thread(app, queued_thread, status="idle")
    assert dispatch_queue(app) is None
