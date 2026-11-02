"""Durable native message questions and confirmed desktop reply sources."""

import copy
import hashlib
import json

import pytest
from sqlalchemy import event

from codex_anywhere.hub.async_questions import (
    ASYNC_QUESTION_METHOD,
    encode_reply,
    expire_inactive_async_questions,
    is_current_async_question,
    project_async_questions,
    reply_display_text,
)
from codex_anywhere.hub.models import Host, Interaction, Item, Thread, db


@pytest.fixture
def source_thread(app, paired_host, create_thread):
    host = paired_host()
    thread = create_thread(host)
    with app.app_context():
        current = db.session.get(Thread, thread["id"])
        current.status = "active"
        current.active_turn_id = "turn"
        db.session.commit()
        yield current


def project_current(thread, data, turn_id=None, **kwargs):
    return project_async_questions(
        thread, data, thread.active_turn_id if turn_id is None else turn_id, live=True, **kwargs
    )


def source(identifier="agent-question", questions=None):
    return {
        "id": identifier,
        "type": "agentMessage",
        "text": "模型原始内容",
        "questions": questions
        if questions is not None
        else [
            {"title": "采用哪个方案？", "options": ["方案一", "方案二"]},
        ],
        "futureNativeField": {"preserve": True},
    }


def question_id(item="agent-question", index=0):
    return json.dumps(
        ["request_user_input_async", item, index], ensure_ascii=False, separators=(",", ":")
    )


def reply(entries, *, kind="userMessage", status="accepted"):
    text = (
        "<send_user_message_question_reply>\n"
        + json.dumps(entries, ensure_ascii=False)
        + "\n</send_user_message_question_reply>"
    )
    part = {"type": "text", "text": text}
    return (
        {"type": kind, "content": [part]}
        if kind == "userMessage"
        else {
            "type": kind,
            "status": status,
            "input": [part],
        }
    )


def answer(identifier=None, text="自定义方案", title="采用哪个方案？"):
    return {"questionItemId": identifier or question_id(), "question": title, "answer": text}


def persist_item(thread, data, *, identifier=None, position=1, created_at=100):
    value = copy.deepcopy(data)
    value.setdefault("id", identifier or f"native-{position}")
    item = Item(
        id=identifier,
        thread_id=thread.id,
        native_id=value["id"],
        turn_id="history-turn",
        type=value["type"],
        data=value,
        position=position,
        created_at=created_at,
    )
    db.session.add(item)
    return item


def test_question_projection_normalizes_without_mutating_native_payload(source_thread):
    data = source(
        questions=[
            {"title": "  保留原文\n问题 👋  ", "options": ["选项 A", "选项 B"]},
            "unknown malformed question",
            {"title": "自由输入", "options": None},
            {
                "id": "native-question",
                "question": "Unknown native representation",
                "options": [{"label": "preserve"}],
            },
        ]
    )
    original = copy.deepcopy(data)
    interaction = project_current(source_thread, data, "turn", created_at=123.5)
    assert data == original
    assert interaction.method == ASYNC_QUESTION_METHOD
    assert interaction.status == "pending"
    assert interaction.rpc_id == {"source": "agent_message", "item_id": "agent-question"}
    assert interaction.created_at == 123.5
    expected_id = hashlib.sha256(
        json.dumps(
            [source_thread.host_id, source_thread.id, "agent-question"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:32]
    assert interaction.id == expected_id
    assert interaction.params["sourceItemId"] == "agent-question"
    assert interaction.params["sourceTurnId"] == "turn"
    assert interaction.params["isBlocking"] is False
    assert interaction.params["questions"] == [
        {
            "id": question_id(index=0),
            "header": data["questions"][0]["title"],
            "question": data["questions"][0]["title"],
            "isOther": True,
            "options": [
                {"label": "选项 A", "description": ""},
                {"label": "选项 B", "description": ""},
            ],
        },
        {
            "id": question_id(index=2),
            "header": "自由输入",
            "question": "自由输入",
            "isOther": True,
            "options": [],
        },
    ]


@pytest.mark.parametrize(
    "questions",
    [
        None,
        {},
        [None],
        [{"title": ""}],
        [{"title": "   "}],
        [{"title": 1}],
        [{"title": "Question", "options": "bad"}],
        [{"title": "Question", "options": ["option", None]}],
    ],
)
def test_invalid_questions_are_left_as_native_data_without_actionable_interaction(
    source_thread, questions
):
    data = source()
    data["questions"] = questions
    original = copy.deepcopy(data)
    assert project_current(source_thread, data) is None
    assert data == original
    assert Interaction.query.count() == 0


def test_streaming_and_history_deduplicate_and_preserve_answer_metadata(source_thread):
    first = project_current(source_thread, source(), "turn")
    first.params = dict(
        first.params, answeredQuestionIds=[question_id()], answerCommandStatus="failed"
    )
    first.status = "answer_unknown"
    data = source(
        questions=[
            {"title": "更新后的标题", "options": ["新选项"]},
            {"title": "另一个问题"},
        ]
    )
    repeated = project_current(source_thread, data, "turn")
    assert repeated.id == first.id
    assert repeated.status == "answer_unknown"
    assert repeated.params["answerCommandStatus"] == "failed"
    assert repeated.params["answeredQuestionIds"] == [question_id()]
    assert repeated.params["questions"][0]["question"] == "更新后的标题"
    assert len(repeated.params["questions"]) == 2
    repeated.status = "resolved"
    project_current(source_thread, data, "turn")
    assert Interaction.query.count() == 1
    assert repeated.status == "resolved"


def test_completed_snapshot_restores_original_order_after_partial_questions(source_thread):
    data = source(questions=[None, {"title": "Second?"}])
    interaction = project_current(source_thread, data)
    assert [value["id"] for value in interaction.params["questions"]] == [question_id(index=1)]
    project_current(
        source_thread,
        source(
            questions=[
                {"title": "First?"},
                {"title": "Second?"},
            ]
        ),
    )
    assert [value["id"] for value in interaction.params["questions"]] == [
        question_id(index=0),
        question_id(index=1),
    ]


def test_reply_encoding_preserves_original_title_and_exact_single_answer(source_thread):
    title = "问题\n原始标题"
    interaction = project_current(source_thread, source(questions=[{"title": title}]))
    text = '  自定义 <tag>\n"answer"  '
    envelope, display = encode_reply(interaction, {"answers": {question_id(): {"answers": [text]}}})
    assert envelope.startswith("<send_user_message_question_reply>\n[")
    assert envelope.endswith("]\n</send_user_message_question_reply>")
    assert json.loads(envelope.split("\n", 1)[1].rsplit("\n", 1)[0]) == [
        {"questionItemId": question_id(), "question": title, "answer": text},
    ]
    assert title in display and text in display


@pytest.mark.parametrize("values", [[], ["one", "two"], "not-array", ["invalid\ud800"]])
def test_reply_encoder_rejects_ambiguous_or_invalid_answer_strings(source_thread, values):
    interaction = project_current(source_thread, source())
    with pytest.raises(ValueError):
        encode_reply(interaction, {"answers": {question_id(): {"answers": values}}})


@pytest.mark.parametrize("kind", ["userMessage", "steeringUserMessage"])
def test_confirmed_native_reply_resolves_only_known_question_ids(source_thread, kind):
    interaction = project_current(source_thread, source(), "turn")
    interaction.status = "answer_unknown"
    data = reply([answer(), answer(question_id("unknown-item"))], kind=kind)
    original = copy.deepcopy(data)
    updated = project_current(source_thread, data, "turn")
    assert updated.id == interaction.id
    assert interaction.status == "resolved"
    assert interaction.params["answeredQuestionIds"] == [question_id()]
    assert data == original
    assert Interaction.query.count() == 1


def test_partial_replies_accumulate_until_all_questions_are_confirmed(source_thread):
    interaction = project_current(
        source_thread,
        source(
            questions=[
                {"title": "First?"},
                {"title": "Second?"},
            ]
        ),
    )
    project_current(source_thread, reply([answer(question_id(index=0))]))
    assert interaction.status == "pending"
    assert interaction.params["answeredQuestionIds"] == [question_id(index=0)]
    project_current(source_thread, source(questions=[{"title": "First?"}]))
    assert len(interaction.params["questions"]) == 2
    project_current(source_thread, reply([answer(question_id(index=1))]))
    assert interaction.status == "resolved"
    assert interaction.params["answeredQuestionIds"] == [question_id(index=0), question_id(index=1)]
    project_current(source_thread, source())
    assert interaction.status == "resolved"


@pytest.mark.parametrize(
    "kind,status",
    [
        ("steeringUserMessage", "pending"),
        ("steeringUserMessage", "rejected"),
        ("agentMessage", "accepted"),
        ("commandExecution", "accepted"),
    ],
)
def test_nonaccepted_or_nonuser_sources_cannot_resolve_questions(source_thread, kind, status):
    interaction = project_current(source_thread, source())
    project_current(source_thread, reply([answer()], kind=kind, status=status))
    assert interaction.status == "pending"
    assert "answeredQuestionIds" not in interaction.params


@pytest.mark.parametrize(
    "transform",
    [
        lambda text: "Quoted reply:\n" + text,
        lambda text: "```xml\n" + text + "\n```",
        lambda text: json.dumps({"quoted": text}),
        lambda text: text.replace('"answer": "自定义方案"', '"answer": null'),
        lambda text: text.replace("[", "{", 1),
    ],
)
def test_quoted_or_malformed_reply_text_does_not_answer(source_thread, transform):
    interaction = project_current(source_thread, source())
    data = reply([answer()])
    data["content"][0]["text"] = transform(data["content"][0]["text"])
    project_current(source_thread, data)
    assert interaction.status == "pending"


def test_reply_ids_are_scoped_to_their_original_thread(source_thread):
    interaction = project_current(source_thread, source())
    other = Thread(
        host_id=source_thread.host_id,
        cwd=source_thread.cwd,
        title="Other",
        status="active",
        active_turn_id="turn",
    )
    db.session.add(other)
    db.session.flush()
    project_current(other, reply([answer()]))
    assert interaction.status == "pending"
    other_interaction = project_current(other, source())
    assert interaction.id != other_interaction.id


@pytest.mark.parametrize("kind", ["userMessage", "steeringUserMessage"])
def test_native_reply_display_text_is_readable_and_keeps_original_payload(kind):
    data = reply(
        [
            answer(text="  Exact answer\n第二行  "),
            answer(question_id(index=1), text="Other", title="第二个问题"),
        ],
        kind=kind,
    )
    original = copy.deepcopy(data)
    assert (
        reply_display_text(data) == "采用哪个方案？\n  Exact answer\n第二行  \n\n第二个问题\nOther"
    )
    assert data == original


def test_reply_display_text_ignores_unaccepted_or_quoted_native_payloads():
    assert (
        reply_display_text(reply([answer()], kind="steeringUserMessage", status="rejected")) is None
    )
    quoted = reply([answer()])
    quoted["content"][0]["text"] = "Quote: " + quoted["content"][0]["text"]
    assert reply_display_text(quoted) is None
    assert (
        reply_display_text({"type": "agentMessage", "content": reply([answer()])["content"]})
        is None
    )


@pytest.mark.parametrize(
    "live,turn_id,status,active_turn",
    [
        (False, "turn", "active", "turn"),
        (True, "old-turn", "active", "turn"),
        (True, "", "active", "turn"),
        (True, None, "active", "turn"),
        (True, "turn", "idle", None),
        (True, "turn", "error", "turn"),
    ],
)
def test_only_current_live_turn_can_create_actionable_questions(
    source_thread, live, turn_id, status, active_turn
):
    source_thread.status = status
    source_thread.active_turn_id = active_turn
    data = source()
    original = copy.deepcopy(data)
    assert project_async_questions(source_thread, data, turn_id, live=live) is None
    assert Interaction.query.count() == 0
    assert data == original


def test_history_item_never_creates_a_question_even_when_its_turn_is_active(source_thread):
    data = source()
    persist_item(source_thread, data)
    assert project_async_questions(source_thread, data, "turn") is None
    assert expire_inactive_async_questions(source_thread.id) == 0
    assert Interaction.query.count() == 0
    assert Item.query.one().data == data


@pytest.mark.parametrize("status", ["pending", "answering", "answer_unknown"])
@pytest.mark.parametrize("change", ["ended", "different_turn", "epoch", "error"])
def test_inactive_turn_or_runtime_expires_outstanding_source_questions(
    source_thread, status, change
):
    data = source()
    persist_item(source_thread, data)
    interaction = project_current(source_thread, data)
    interaction.status = status
    assert is_current_async_question(interaction, source_thread)
    if change == "ended":
        source_thread.status, source_thread.active_turn_id = "idle", None
    elif change == "different_turn":
        source_thread.active_turn_id = "next-turn"
    elif change == "epoch":
        db.session.get(Host, source_thread.host_id).runtime_id = "new-runtime"
    else:
        source_thread.status = "error"
    assert not is_current_async_question(interaction, source_thread)
    assert expire_inactive_async_questions(source_thread.id) == 1
    assert interaction.status == "expired"
    assert expire_inactive_async_questions(source_thread.id) == 0
    assert Item.query.one().data == data


@pytest.mark.parametrize("status", ["expired", "resolved", "answer_unknown"])
def test_live_or_history_replay_never_reopens_prior_question(source_thread, status):
    interaction = project_current(source_thread, source())
    interaction.status = status
    project_async_questions(source_thread, source(), "turn")
    assert interaction.status == status
    project_current(source_thread, source(), "turn")
    assert interaction.status == status
    assert Interaction.query.count() == 1


def test_current_questions_survive_cleanup_and_rpc_requests_are_not_touched(source_thread):
    interaction = project_current(source_thread, source())
    interaction.status = "answer_unknown"
    native = Interaction(
        host_id=source_thread.host_id,
        thread_id=source_thread.id,
        runtime_id="old-runtime",
        rpc_id=17,
        method="item/tool/requestUserInput",
        params={"questions": []},
        status="pending",
    )
    db.session.add(native)
    assert expire_inactive_async_questions() == 0
    assert interaction.status == "answer_unknown"
    assert native.status == "pending"


def test_cleanup_is_scoped_and_does_not_query_history_payloads(source_thread):
    other = Thread(
        host_id=source_thread.host_id,
        cwd=source_thread.cwd,
        title="Other",
        status="active",
        active_turn_id="turn",
    )
    db.session.add(other)
    db.session.flush()
    first = project_current(source_thread, source())
    second = project_current(other, source())
    persist_item(source_thread, {**source(), "text": "large historical payload " * 30000})
    source_thread.status = other.status = "idle"
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        assert expire_inactive_async_questions(source_thread.id) == 1
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    assert first.status == "expired"
    assert second.status == "pending"
    assert not any("FROM item" in statement or "JOIN item" in statement for statement in statements)
    assert expire_inactive_async_questions() == 1
    assert second.status == "expired"


def test_confirmed_history_reply_can_resolve_existing_prompt_without_creating_history_prompts(
    source_thread,
):
    interaction = project_current(source_thread, source())
    source_thread.status, source_thread.active_turn_id = "idle", None
    expire_inactive_async_questions(source_thread.id)
    assert interaction.status == "expired"
    project_async_questions(source_thread, reply([answer()]), "old-turn")
    assert interaction.status == "resolved"
    assert project_async_questions(source_thread, source("historical-question"), "old-turn") is None
    assert Interaction.query.count() == 1
