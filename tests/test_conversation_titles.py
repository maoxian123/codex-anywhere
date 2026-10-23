"""Conversation names follow accepted input and native title notifications."""

import io
from uuid import uuid4

import pytest
from PIL import Image

from codex_anywhere.hub.models import Command, Thread, db
from codex_anywhere.hub.transport import handle_node_message

OMITTED = object()


def new_conversation(app, api, host, title=OMITTED):
    payload = {"host_id": host["host"]["id"], "cwd": host["root"]}
    if title is not OMITTED:
        payload["title"] = title
    response = api("POST", "/api/threads", json=payload)
    assert response.status_code == 202, response.get_json()
    created = response.get_json()
    native_id = "native-" + uuid4().hex
    with app.app_context():
        handle_node_message(
            app,
            host["host"]["id"],
            {
                "type": "command_result",
                "id": created["command"]["id"],
                "result": {"thread": {"id": native_id, "cwd": host["root"]}},
            },
        )
    thread = api("GET", f"/api/threads/{created['thread']['id']}").get_json()["thread"]
    return thread, created


def state(api, thread):
    return api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]


def message(api, thread, text="", **extra):
    return api(
        "POST",
        f"/api/threads/{thread['id']}/messages",
        json={"text": text, "client_request_id": uuid4().hex, **extra},
    )


def native_event(app, host, thread, method, params, runtime_id=None):
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": uuid4().hex,
            "runtime_id": runtime_id or host["runtime_id"],
            "method": method,
            "params": {"threadId": thread["native_id"], **params},
        },
    )


def finish_turn(app, host, thread):
    native_event(app, host, thread, "turn/started", {"turn": {"id": "turn-first"}})
    native_event(
        app,
        host,
        thread,
        "turn/completed",
        {"turn": {"id": "turn-first", "status": "completed"}},
    )


@pytest.mark.parametrize("title", [OMITTED, "", " \t\n  "])
def test_creation_without_name_keeps_default_out_of_native_start_payload(
    app, api, paired_host, title
):
    thread, created = new_conversation(app, api, paired_host(), title)
    assert created["thread"]["title"] == "新对话"
    assert thread["title"] == "新对话"
    with app.app_context():
        start = db.session.get(Command, created["command"]["id"])
        assert start.kind == "thread.start"
        assert "title" not in start.payload
        assert Command.query.count() == 1


def test_explicit_creation_name_remains_compatible(app, api, paired_host):
    thread, created = new_conversation(app, api, paired_host(), "  明确指定的标题  ")
    assert created["thread"]["title"] == "明确指定的标题"
    assert thread["title"] == "明确指定的标题"
    with app.app_context():
        start = db.session.get(Command, created["command"]["id"])
        assert start.payload["title"] == "明确指定的标题"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\n  \t\n  修复    侧栏\t布局  \n第二行不属于标题", "修复 侧栏 布局"),
        (" \r\n  First\t accepted   message \r\nOther line", "First accepted message"),
        ("\n" + "很长的会话标题" * 20 + "\n第二行", ("很长的会话标题" * 20)[:80]),
    ],
)
def test_first_accepted_message_uses_first_nonempty_line_with_bounded_whitespace(
    app, api, paired_host, text, expected
):
    host = paired_host()
    thread, _ = new_conversation(app, api, host)
    response = message(api, thread, text)
    assert response.status_code == 202, response.get_json()
    assert state(api, thread)["title"] == expected
    with app.app_context():
        command = db.session.get(Command, response.get_json()["command"]["id"])
        # Naming must not rewrite the model-visible message or journal fake work.
        assert command.kind == "turn.start"
        assert command.payload["_display_text"] == text
        assert command.payload["input"][0]["text"] == text
        assert {row.kind for row in Command.query.all()} == {"thread.start", "turn.start"}


def test_later_messages_do_not_replace_first_message_title(app, api, paired_host):
    host = paired_host()
    thread, _ = new_conversation(app, api, host)
    assert message(api, thread, "First accepted task").status_code == 202
    assert state(api, thread)["title"] == "First accepted task"
    finish_turn(app, host, thread)
    assert message(api, thread, "A later requirement").status_code == 202
    assert state(api, thread)["title"] == "First accepted task"


@pytest.mark.parametrize("title", ["User supplied title", "新任务"])
def test_explicit_or_legacy_title_is_not_replaced_by_input(app, api, paired_host, title):
    thread, _ = new_conversation(app, api, paired_host(), title)
    assert message(api, thread, "This should not rename the conversation").status_code == 202
    assert state(api, thread)["title"] == title


def test_explicit_title_matching_default_is_not_replaced(app, api, paired_host):
    thread, created = new_conversation(app, api, paired_host(), "新对话")
    with app.app_context():
        assert db.session.get(Command, created["command"]["id"]).payload["title"] == "新对话"
    assert message(api, thread, "The explicitly chosen name must remain").status_code == 202
    assert state(api, thread)["title"] == "新对话"


@pytest.mark.parametrize("imported", [True, False])
def test_native_conversation_matching_default_without_creation_command_is_not_renamed(
    app, api, paired_host, imported
):
    host = paired_host()
    with app.app_context():
        native = Thread(
            host_id=host["host"]["id"],
            native_id="native-existing-" + uuid4().hex,
            title="新对话",
            cwd=host["root"],
            status="idle",
            imported=imported,
            history_loaded=True,
        )
        db.session.add(native)
        db.session.commit()
        thread = native.as_dict()
        assert Command.query.filter_by(thread_id=thread["id"], kind="thread.start").count() == 0
    assert message(api, thread, "Continue the existing native conversation").status_code == 202
    assert state(api, thread)["title"] == "新对话"


def test_first_message_matching_default_does_not_allow_later_auto_rename(app, api, paired_host):
    host = paired_host()
    thread, _ = new_conversation(app, api, host)
    assert message(api, thread, "新对话").status_code == 202
    assert state(api, thread)["title"] == "新对话"
    finish_turn(app, host, thread)
    assert message(api, thread, "A different second message").status_code == 202
    assert state(api, thread)["title"] == "新对话"


def test_auto_title_preserves_custom_display_name(app, api, paired_host):
    thread, _ = new_conversation(app, api, paired_host())
    assert (
        api(
            "PATCH",
            f"/api/threads/{thread['id']}/preferences",
            json={"display_title": "My stable display label"},
        ).status_code
        == 200
    )
    assert message(api, thread, "Title from the real first message").status_code == 202
    current = state(api, thread)
    assert current["title"] == "Title from the real first message"
    assert current["display_title"] == "My stable display label"


@pytest.mark.parametrize(
    "failure", ["empty", "missing_asset", "invalid_reference", "offline"]
)
def test_rejected_first_message_leaves_default_title_untouched(app, api, paired_host, failure):
    host = paired_host()
    thread, _ = new_conversation(app, api, host)
    kwargs = {}
    text = "This rejected request must not supply the title"
    if failure == "empty":
        text = " \n\t "
    elif failure == "missing_asset":
        kwargs["asset_ids"] = ["missing-asset"]
    elif failure == "invalid_reference":
        kwargs["references"] = [{"kind": "web", "url": "javascript:invalid"}]
    elif failure == "offline":
        app.extensions["broker"].detach(host["host"]["id"], host["socket"])
    response = message(api, thread, text, **kwargs)
    assert response.status_code in {400, 409}, response.get_json()
    assert state(api, thread)["title"] == "新对话"
    with app.app_context():
        assert Command.query.filter_by(thread_id=thread["id"], kind="turn.start").count() == 0


@pytest.mark.parametrize("text", ["Name from queued message", "新对话"])
def test_first_queued_message_names_unnamed_conversation_once(app, api, paired_host, text):
    host = paired_host()
    thread, _ = new_conversation(app, api, host)
    native_event(app, host, thread, "turn/started", {"turn": {"id": "desktop-turn"}})
    response = message(api, thread, text)
    assert response.status_code == 202
    assert response.get_json()["command"]["kind"] == "turn.queue"
    assert state(api, thread)["title"] == text
    assert message(api, thread, "Later queued message").status_code == 202
    assert state(api, thread)["title"] == text


def test_image_only_first_message_has_useful_title(app, api, paired_host):
    thread, _ = new_conversation(app, api, paired_host())
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(image, format="PNG")
    image.seek(0)
    response = api(
        "POST",
        "/api/assets",
        data={"file": (image, "preview.png", "image/png")},
        content_type="multipart/form-data",
    )
    assert response.status_code in {200, 201}, response.get_json()
    asset = response.get_json()["asset"]
    assert message(api, thread, asset_ids=[asset["id"]]).status_code == 202
    assert state(api, thread)["title"] == "图片对话"


def test_reference_only_first_message_has_useful_title(app, api, paired_host):
    thread, _ = new_conversation(app, api, paired_host())
    response = message(
        api,
        thread,
        references=[
            {
                "kind": "web",
                "url": "https://example.com/context",
                "excerpt": "User supplied excerpt",
            }
        ],
    )
    assert response.status_code == 202, response.get_json()
    assert state(api, thread)["title"] == "引用对话"


def test_native_title_event_updates_projection_without_touching_display_name(app, api, paired_host):
    host = paired_host()
    thread, _ = new_conversation(app, api, host, "Initial native title")
    api(
        "PATCH",
        f"/api/threads/{thread['id']}/preferences",
        json={"display_title": "Personal label"},
    )
    native_event(app, host, thread, "thread/name/updated", {"threadName": "Updated by Codex"})
    current = state(api, thread)
    assert current["title"] == "Updated by Codex"
    assert current["display_title"] == "Personal label"


@pytest.mark.parametrize("title", [None, "", " \t\n "])
def test_empty_native_title_event_does_not_clear_existing_name(app, api, paired_host, title):
    host = paired_host()
    thread, _ = new_conversation(app, api, host, "Keep the current title")
    native_event(app, host, thread, "thread/name/updated", {"threadName": title})
    assert state(api, thread)["title"] == "Keep the current title"


def test_old_runtime_title_notification_is_not_projected(app, api, paired_host):
    host = paired_host()
    thread, _ = new_conversation(app, api, host, "Current runtime name")
    native_event(
        app,
        host,
        thread,
        "thread/name/updated",
        {"threadName": "Obsolete runtime name"},
        runtime_id="old-runtime-" + uuid4().hex,
    )
    assert state(api, thread)["title"] == "Current runtime name"
