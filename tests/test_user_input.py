"""Native model questions, bounded global attention and exact answer delivery."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from codex_anywhere.hub.models import Command, Interaction, Thread, db
from codex_anywhere.hub.transport import handle_node_message

METHOD = "item/tool/requestUserInput"


def native(app, host, method, params, *, rpc_id=None, runtime_id=None):
    handle_node_message(
        app,
        host["host"]["id"],
        {
            "type": "event",
            "event_id": uuid4().hex,
            "runtime_id": runtime_id or host["runtime_id"],
            "method": method,
            "params": params,
            **({"rpc_id": rpc_id} if rpc_id is not None else {}),
        },
    )


def question(identifier="choice", *, options=True, **kwargs):
    return {
        "id": identifier,
        "header": "选方案",
        "question": "你希望如何继续？",
        "options": [
            {"label": "保留原样", "description": "不修改"},
            {"label": "执行修改", "description": "应用方案"},
        ]
        if options
        else None,
        **kwargs,
    }


def ask(
    app,
    api,
    host,
    thread=None,
    *,
    questions=None,
    method=METHOD,
    blocking=True,
    rpc_id="original-native-rpc",
    runtime_id=None,
):
    native(
        app,
        host,
        method,
        {
            **({"threadId": thread["native_id"]} if thread else {}),
            "turnId": "question-turn",
            "itemId": "question-item",
            "isBlocking": blocking,
            "questions": questions if questions is not None else [question()],
        },
        rpc_id=rpc_id,
        runtime_id=runtime_id,
    )
    with app.app_context():
        return next(
            row.id
            for row in Interaction.query.filter_by(host_id=host["host"]["id"])
            if row.rpc_id == rpc_id
        )


def answer(api, identifier, response):
    return api("POST", f"/api/interactions/{identifier}/answer", json={"response": response})


@pytest.mark.parametrize("method", [METHOD, "tool/requestUserInput"])
def test_native_choices_preserve_rpc_id_and_exact_strings(
    app, api, paired_host, create_thread, method
):
    host = paired_host()
    thread = create_thread(host)
    identifier = ask(app, api, host, thread, method=method, rpc_id=271)
    response = {"answers": {"choice": {"answers": ["执行修改", "保留原样"]}}}
    accepted = answer(api, identifier, response)
    assert accepted.status_code == 202
    wire = json.loads(host["socket"].messages[-1])["command"]
    assert wire["kind"] == "interaction.answer"
    assert wire["payload"]["rpc_id"] == 271
    assert wire["payload"]["runtime_id"] == host["runtime_id"]
    assert wire["payload"]["response"] == response
    assert api("GET", "/api/interactions").get_json()["total"] == 0
    assert answer(api, identifier, response).status_code == 409


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"answers": []},
        {"answers": {}},
        {"answers": {"choice": {"answers": ["执行修改"]}, "extra": {"answers": ["x"]}}},
        {"answers": {"choice": "执行修改"}},
        {"answers": {"choice": {"answers": "执行修改"}}},
        {"answers": {"choice": {"answers": []}}},
        {"answers": {"choice": {"answers": [""]}}},
        {"answers": {"choice": {"answers": ["   "]}}},
        {"answers": {"choice": {"answers": [1]}}},
        {"answers": {"choice": {"answers": ["执行修改", None]}}},
        {"answers": {"choice": {"answers": ["unadvertised"]}}},
        {"answers": {"choice": {"answers": ["执行修改"], "extra": True}}},
        {"answers": {"choice": {"answers": ["执行修改"]}}, "extra": True},
    ],
)
def test_invalid_answer_does_not_consume_native_request(app, api, paired_host, response):
    host = paired_host()
    identifier = ask(app, api, host)
    assert answer(api, identifier, response).status_code == 400
    with app.app_context():
        assert db.session.get(Interaction, identifier).status == "pending"
        assert Command.query.filter_by(kind="interaction.answer").count() == 0


@pytest.mark.parametrize("options,is_other", [(None, False), ([], False), (True, True)])
def test_free_text_and_other_preserve_whitespace(app, api, paired_host, options, is_other):
    host = paired_host()
    q = question(isOther=is_other)
    if options is not True:
        q["options"] = options
    identifier = ask(
        app, api, host, questions=[q, question("password", options=False, isSecret=True)]
    )
    response = {
        "answers": {
            "choice": {"answers": ["  我的方案\n继续  "]},
            "password": {"answers": ["  private answer  "]},
        }
    }
    assert answer(api, identifier, response).status_code == 202
    wire = json.loads(host["socket"].messages[-1])["command"]
    assert wire["payload"]["response"] == response


def test_all_questions_must_be_answered(app, api, paired_host):
    host = paired_host()
    identifier = ask(app, api, host, questions=[question("one"), question("two")])
    assert (
        answer(api, identifier, {"answers": {"one": {"answers": ["执行修改"]}}}).status_code == 400
    )


def test_invalid_unicode_answer_is_rejected_before_journaling(app, api, paired_host):
    host = paired_host()
    identifier = ask(app, api, host, questions=[question(options=False)])
    response = {"answers": {"choice": {"answers": ["invalid \ud800 answer"]}}}
    assert answer(api, identifier, response).status_code == 400
    with app.app_context():
        assert db.session.get(Interaction, identifier).status == "pending"
        assert Command.query.filter_by(kind="interaction.answer").count() == 0


@pytest.mark.parametrize("questions", [[], [question("same"), question("same")], [{"id": 1}]])
def test_malformed_native_questions_cannot_be_answered(app, api, paired_host, questions):
    host = paired_host()
    identifier = ask(app, api, host, questions=questions)
    assert answer(api, identifier, {"answers": {}}).status_code == 400


def test_global_list_includes_archived_unloaded_and_unlinked_without_question_content(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    with app.app_context():
        stored = db.session.get(Thread, thread["id"])
        stored.archived = True
        stored.history_loaded = False
        stored.display_title = "自定义标题"
        db.session.commit()
    linked = ask(app, api, host, thread, questions=[question(isSecret=True)])
    unlinked = ask(app, api, host, rpc_id="unlinked", blocking=False)
    listing = api("GET", "/api/interactions").get_json()
    assert listing["total"] == 2
    rows = {row["id"]: row for row in listing["interactions"]}
    assert rows[linked]["thread_title"] == "自定义标题"
    assert rows[linked]["thread_id"] == thread["id"]
    assert rows[linked]["host_name"] == "workstation"
    assert rows[linked]["question_count"] == 1
    assert rows[linked]["blocking"] is True
    assert rows[unlinked]["thread_title"] == "未关联会话"
    assert rows[unlinked]["thread_id"] is None
    assert rows[unlinked]["blocking"] is False
    for row in rows.values():
        assert set(row) == {
            "id",
            "thread_id",
            "host_id",
            "thread_title",
            "host_name",
            "method",
            "created_at",
            "question_count",
            "blocking",
        }
    detail = api("GET", f"/api/interactions/{linked}").get_json()["interaction"]
    assert detail["params"]["questions"][0]["isSecret"] is True
    assert "rpc_id" not in detail and "runtime_id" not in detail


def test_pending_list_filters_status_runtime_and_missing_host_and_pages_stably(
    app, api, paired_host
):
    host = paired_host()
    with app.app_context():
        for identifier, status, runtime_id in [
            ("a", "pending", host["runtime_id"]),
            ("b", "pending", host["runtime_id"]),
            ("c", "pending", host["runtime_id"]),
            ("d", "answering", host["runtime_id"]),
            ("e", "expired", host["runtime_id"]),
            ("f", "resolved", host["runtime_id"]),
            ("g", "pending", "old-runtime"),
        ]:
            db.session.add(
                Interaction(
                    id=identifier,
                    host_id=host["host"]["id"],
                    runtime_id=runtime_id,
                    rpc_id=identifier,
                    method=METHOD,
                    params={"questions": []},
                    status=status,
                    created_at=123.0,
                )
            )
        db.session.add(
            Interaction(
                id="orphan",
                host_id="missing-host",
                runtime_id="runtime",
                rpc_id="orphan",
                method=METHOD,
                params={},
                status="pending",
            )
        )
        db.session.commit()
    first = api("GET", "/api/interactions?limit=2").get_json()
    assert [row["id"] for row in first["interactions"]] == ["a", "b"]
    assert (first["total"], first["has_more"], first["next_offset"]) == (3, True, 2)
    second = api("GET", "/api/interactions?limit=2&offset=2").get_json()
    assert [row["id"] for row in second["interactions"]] == ["c"]
    assert (second["total"], second["has_more"], second["next_offset"]) == (3, False, None)


def test_native_approval_is_always_blocking_and_has_no_question_count(app, api, paired_host):
    host = paired_host()
    identifier = ask(app, api, host, method="item/commandExecution/requestApproval", blocking=False)
    row = api("GET", "/api/interactions").get_json()["interactions"][0]
    assert row["id"] == identifier
    assert row["blocking"] is True
    assert row["question_count"] == 0


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=bad", "limit=1.5", "offset=-1"])
def test_attention_pagination_is_bounded(api, query):
    assert api("GET", "/api/interactions?" + query).status_code == 400


def test_attention_requires_login_and_unknown_ids_are_not_found(app, api):
    client = app.test_client()
    assert client.get("/api/interactions").status_code == 401
    assert client.get("/api/interactions/absent").status_code == 401
    assert api("GET", "/api/interactions/absent").status_code == 404


def test_nonblocking_questions_do_not_mask_active_work_or_other_blocking_requests(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    native(
        app,
        host,
        "turn/started",
        {"threadId": thread["native_id"], "turn": {"id": "question-turn"}},
    )
    nonblocking = ask(app, api, host, thread, blocking=False)
    assert api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]["status"] == "active"
    blocking = ask(app, api, host, thread, rpc_id="blocking", blocking=True)
    assert api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]["status"] == "waiting"
    assert (
        answer(api, blocking, {"answers": {"choice": {"answers": ["执行修改"]}}}).status_code == 202
    )
    with app.app_context():
        command = Command.query.filter_by(kind="interaction.answer").one()
        command_id = command.id
    handle_node_message(
        app,
        host["host"]["id"],
        {"type": "command_result", "id": command_id, "result": {"answered": True}},
    )
    assert api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]["status"] == "active"
    assert [row["id"] for row in api("GET", "/api/interactions").get_json()["interactions"]] == [
        nonblocking
    ]
    # Pending nonblocking input is still native work: no fabricated answer or expiration.
    with app.app_context():
        assert db.session.get(Interaction, nonblocking).status == "pending"


def test_native_resolution_without_thread_id_clears_global_reminder(app, api, paired_host):
    host = paired_host()
    identifier = ask(app, api, host, rpc_id="native-rpc")
    native(app, host, "serverRequest/resolved", {"requestId": "native-rpc"}, runtime_id="old")
    assert api("GET", "/api/interactions").get_json()["total"] == 1
    native(app, host, "serverRequest/resolved", {"requestId": "native-rpc"})
    assert api("GET", "/api/interactions").get_json()["total"] == 0
    assert (
        api("GET", f"/api/interactions/{identifier}").get_json()["interaction"]["status"]
        == "resolved"
    )


def test_resolution_without_thread_id_refreshes_linked_waiting_thread(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    native(
        app,
        host,
        "turn/started",
        {"threadId": thread["native_id"], "turn": {"id": "question-turn"}},
    )
    ask(app, api, host, thread, rpc_id="native-rpc")
    native(app, host, "serverRequest/resolved", {"requestId": "native-rpc"})
    assert api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]["status"] == "active"


def test_answering_nonblocking_question_does_not_keep_thread_waiting(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    native(
        app,
        host,
        "turn/started",
        {"threadId": thread["native_id"], "turn": {"id": "question-turn"}},
    )
    nonblocking = ask(app, api, host, thread, blocking=False, rpc_id="nonblocking")
    blocking = ask(app, api, host, thread, blocking=True, rpc_id="blocking")
    response = {"answers": {"choice": {"answers": ["执行修改"]}}}
    assert answer(api, nonblocking, response).status_code == 202
    native(app, host, "serverRequest/resolved", {"requestId": "blocking"})
    assert api("GET", f"/api/threads/{thread['id']}").get_json()["thread"]["status"] == "active"
    with app.app_context():
        assert db.session.get(Interaction, nonblocking).status == "answering"
        assert db.session.get(Interaction, blocking).status == "resolved"


def test_turn_completion_expires_nonblocking_question_without_auto_answer(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    native(
        app,
        host,
        "turn/started",
        {"threadId": thread["native_id"], "turn": {"id": "question-turn"}},
    )
    identifier = ask(app, api, host, thread, blocking=False)
    native(
        app,
        host,
        "turn/completed",
        {"threadId": thread["native_id"], "turn": {"id": "question-turn", "status": "completed"}},
    )
    assert api("GET", "/api/interactions").get_json()["total"] == 0
    with app.app_context():
        assert db.session.get(Interaction, identifier).status == "expired"
        assert Command.query.filter_by(kind="interaction.answer").count() == 0


def test_runtime_change_expires_question_and_prevents_answer(app, api, paired_host):
    host = paired_host()
    identifier = ask(app, api, host)
    handle_node_message(app, host["host"]["id"], {"type": "hello", "runtime_id": uuid4().hex})
    assert api("GET", "/api/interactions").get_json()["total"] == 0
    assert (
        answer(api, identifier, {"answers": {"choice": {"answers": ["执行修改"]}}}).status_code
        == 409
    )


def test_concurrent_answers_journal_only_one_native_reply(app, api, paired_host):
    host = paired_host()
    identifier = ask(app, api, host)
    barrier = Barrier(2)

    def submit():
        client = app.test_client()
        token = client.get("/api/auth/me").get_json()["csrf_token"]
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "test-password"},
            headers={"X-CSRF-Token": token},
        )
        assert login.status_code == 200
        token = client.get("/api/auth/me").get_json()["csrf_token"]
        barrier.wait(timeout=10)
        return client.post(
            f"/api/interactions/{identifier}/answer",
            json={"response": {"answers": {"choice": {"answers": ["执行修改"]}}}},
            headers={"X-CSRF-Token": token},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: submit(), range(2)))
    assert sorted(outcomes) == [202, 409]
    with app.app_context():
        assert Command.query.filter_by(kind="interaction.answer").count() == 1
        assert db.session.get(Interaction, identifier).status == "answering"


def test_orphaned_host_answer_returns_conflict(app, api, paired_host):
    host = paired_host()
    identifier = ask(app, api, host)
    with app.app_context():
        db.session.get(Interaction, identifier).host_id = "deleted-host"
        db.session.commit()
    assert (
        answer(api, identifier, {"answers": {"choice": {"answers": ["执行修改"]}}}).status_code
        == 409
    )
