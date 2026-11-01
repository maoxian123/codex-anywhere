"""Project desktop message questions and their confirmed native replies."""

from __future__ import annotations

import hashlib
import json
import math

from sqlalchemy import Text, and_, cast, or_

from .models import Host, Interaction, Item, Thread, db

ASYNC_QUESTION_METHOD = "anywhere/message_questions"
QUESTION_SOURCE = "request_user_input_async"
REPLY_OPEN = "<send_user_message_question_reply>"
REPLY_CLOSE = "</send_user_message_question_reply>"
BACKFILL_BATCH_SIZE = 200


def _interaction_id(thread, item_id):
    key = json.dumps(
        [thread.host_id, thread.id, item_id], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def _text(value):
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _questions(data):
    item_id, source = data.get("id"), data.get("questions")
    if not _text(item_id) or not item_id or not isinstance(source, list):
        return []
    result = []
    for index, question in enumerate(source):
        if not isinstance(question, dict):
            continue
        title, options = question.get("title"), question.get("options")
        if not _text(title) or not title.strip():
            continue
        if options is not None and (
            not isinstance(options, list) or not all(_text(option) for option in options)
        ):
            continue
        result.append(
            {
                "id": json.dumps(
                    [QUESTION_SOURCE, item_id, index], ensure_ascii=False, separators=(",", ":")
                ),
                "header": title,
                "question": title,
                "isOther": True,
                "options": [{"label": option, "description": ""} for option in options]
                if options
                else [],
            }
        )
    return result


def _question_index(question):
    try:
        parts = json.loads(question["id"])
        if isinstance(parts, list) and len(parts) == 3 and type(parts[2]) is int:
            return parts[2]
    except (ValueError, TypeError, RecursionError):
        pass
    return math.inf


def _upsert_questions(thread, data, turn_id, created_at=None):
    questions = _questions(data)
    if not questions:
        return None, False
    item_id = data["id"]
    identifier = _interaction_id(thread, item_id)
    interaction = db.session.get(Interaction, identifier)
    if interaction is None:
        host = db.session.get(Host, thread.host_id)
        interaction = Interaction(
            id=identifier,
            host_id=thread.host_id,
            thread_id=thread.id,
            runtime_id=host.runtime_id if host and host.runtime_id else "",
            rpc_id={"source": "agent_message", "item_id": item_id},
            method=ASYNC_QUESTION_METHOD,
            params={
                "isBlocking": False,
                "sourceItemId": item_id,
                "sourceTurnId": turn_id,
                "questions": questions,
            },
            status="pending",
        )
        if type(created_at) in {int, float} and math.isfinite(created_at) and created_at > 0:
            interaction.created_at = created_at
        db.session.add(interaction)
        return interaction, True
    params = dict(interaction.params)
    existing = params.get("questions", [])
    merged = {
        question["id"]: question
        for question in existing
        if isinstance(question, dict) and isinstance(question.get("id"), str)
    }
    merged.update((question["id"], question) for question in questions)
    params.update(
        isBlocking=False,
        sourceItemId=item_id,
        questions=sorted(merged.values(), key=_question_index),
    )
    if turn_id is not None:
        params["sourceTurnId"] = turn_id
    changed = params != interaction.params
    if changed:
        interaction.params = params
    # Replayed history and later streaming snapshots never reopen a request.
    return interaction, changed


def encode_reply(interaction, response):
    """Encode a fully validated single-text answer for each message question."""
    if interaction.method != ASYNC_QUESTION_METHOD:
        raise ValueError("该交互不是消息提问")
    entries = []
    display = []
    answers = response["answers"]
    for question in interaction.params["questions"]:
        supplied = answers[question["id"]]["answers"]
        if not isinstance(supplied, list) or len(supplied) != 1 or not _text(supplied[0]):
            raise ValueError("每个问题需要一个文本回答")
        answer = supplied[0]
        entries.append(
            {
                "questionItemId": question["id"],
                "question": question["question"],
                "answer": answer,
            }
        )
        display.append(f"{question['question']}\n{answer}")
    encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return f"{REPLY_OPEN}\n{encoded}\n{REPLY_CLOSE}", "\n\n".join(display)


def _reply_entries(data):
    kind = data.get("type")
    if kind == "userMessage":
        content = data.get("content")
    elif kind == "steeringUserMessage" and data.get("status") == "accepted":
        content = data.get("input")
    else:
        return []
    if not isinstance(content, list):
        return []
    result = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text" or not _text(part.get("text")):
            continue
        text = part["text"].strip()
        if not text.startswith(REPLY_OPEN) or not text.endswith(REPLY_CLOSE):
            continue
        try:
            entries = json.loads(text[len(REPLY_OPEN) : -len(REPLY_CLOSE)].strip())
        except (ValueError, RecursionError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if (
                isinstance(entry, dict)
                and _text(entry.get("questionItemId"))
                and _text(entry.get("question"))
                and _text(entry.get("answer"))
                and entry["answer"].strip()
            ):
                result.append(entry)
    return result


def _reconcile_replies(thread, data):
    entries = _reply_entries(data)
    candidates = set()
    for entry in entries:
        try:
            parts = json.loads(entry["questionItemId"])
        except (ValueError, RecursionError):
            continue
        if (
            isinstance(parts, list)
            and len(parts) == 3
            and parts[0] == QUESTION_SOURCE
            and _text(parts[1])
            and parts[1]
            and type(parts[2]) is int
            and parts[2] >= 0
        ):
            candidates.add(_interaction_id(thread, parts[1]))
    if not candidates:
        return []
    answered = {entry["questionItemId"] for entry in entries}
    changed = []
    for interaction in Interaction.query.filter(
        Interaction.id.in_(candidates),
        Interaction.thread_id == thread.id,
        Interaction.method == ASYNC_QUESTION_METHOD,
    ):
        params = dict(interaction.params)
        ids = [
            question["id"]
            for question in params.get("questions", [])
            if isinstance(question, dict) and isinstance(question.get("id"), str)
        ]
        if not ids or not answered.intersection(ids):
            continue
        previous = params.get("answeredQuestionIds", [])
        previous = previous if isinstance(previous, list) else []
        accumulated = {value for value in previous if isinstance(value, str) and value in ids}
        accumulated.update(answered.intersection(ids))
        params["answeredQuestionIds"] = [value for value in ids if value in accumulated]
        resolved = len(accumulated) == len(ids)
        if params != interaction.params or (resolved and interaction.status != "resolved"):
            interaction.params = params
            if resolved:
                interaction.status = "resolved"
            changed.append(interaction)
    return changed


def reply_display_text(data):
    """Render a confirmed native reply envelope without altering its payload."""
    if not isinstance(data, dict):
        return None
    entries = _reply_entries(data)
    if not entries:
        return None
    return "\n\n".join(f"{entry['question']}\n{entry['answer']}" for entry in entries)


def project_async_questions(thread, data, turn_id=None, *, created_at=None):
    """Project native items without changing their raw payload or committing."""
    if not thread or not isinstance(data, dict):
        return None
    if data.get("type") == "agentMessage":
        return _upsert_questions(thread, data, turn_id, created_at)[0]
    changed = _reconcile_replies(thread, data)
    return changed[-1] if changed else None


def _batches(query):
    cursor = None
    while True:
        page = query.filter(Item.id > cursor) if cursor is not None else query
        rows = page.order_by(Item.id).limit(BACKFILL_BATCH_SIZE).all()
        if not rows:
            return
        yield rows
        cursor = rows[-1].id


def backfill_async_questions(thread_id=None):
    """Scan only question/reply JSON sources, with bounded rows and no Item cache."""
    questions = db.session.query(
        Item.id,
        Item.thread_id,
        Item.native_id,
        Item.turn_id,
        Item.created_at,
        Item.data["questions"].label("questions"),
    ).filter(Item.type == "agentMessage", Item.data["questions"].as_string().is_not(None))
    replies = db.session.query(
        Item.id,
        Item.thread_id,
        Item.type,
        Item.data["content"].label("content"),
        Item.data["input"].label("input"),
        Item.data["status"].label("status"),
    ).filter(
        or_(
            and_(
                Item.type == "userMessage",
                cast(Item.data["content"], Text).contains(REPLY_OPEN, autoescape=True),
            ),
            and_(
                Item.type == "steeringUserMessage",
                Item.data["status"].as_string() == "accepted",
                cast(Item.data["input"], Text).contains(REPLY_OPEN, autoescape=True),
            ),
        )
    )
    if thread_id is not None:
        questions = questions.filter(Item.thread_id == thread_id)
        replies = replies.filter(Item.thread_id == thread_id)
    changed = 0
    for page in _batches(questions):
        for row in page:
            thread = db.session.get(Thread, row.thread_id)
            if thread:
                _, updated = _upsert_questions(
                    thread,
                    {
                        "id": row.native_id,
                        "type": "agentMessage",
                        "questions": row.questions,
                    },
                    row.turn_id,
                    row.created_at,
                )
                changed += int(updated)
    for page in _batches(replies):
        for row in page:
            thread = db.session.get(Thread, row.thread_id)
            if thread:
                changed += len(
                    _reconcile_replies(
                        thread,
                        {
                            "type": row.type,
                            "content": row.content,
                            "input": row.input,
                            "status": row.status,
                        },
                    )
                )
    return changed
