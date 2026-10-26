"""Native question validation and small, global attention summaries."""

from __future__ import annotations

from .models import Host, Interaction, Thread, db, iso

USER_INPUT_METHODS = {"item/tool/requestUserInput", "tool/requestUserInput"}


def is_blocking(interaction):
    """Only native question requests can explicitly permit work to continue."""
    return not (
        interaction.method in USER_INPUT_METHODS and interaction.params.get("isBlocking") is False
    )


def validate_user_input(interaction, response):
    if interaction.method not in USER_INPUT_METHODS:
        return
    if set(response) != {"answers"} or not isinstance(response["answers"], dict):
        raise ValueError("请按问题提交 answers 对象")
    questions = interaction.params.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("该请求没有可回答的问题")
    expected = {}
    for question in questions:
        if (
            not isinstance(question, dict)
            or not isinstance(question.get("id"), str)
            or not question["id"]
            or question["id"] in expected
        ):
            raise ValueError("该请求的问题 ID 无效")
        expected[question["id"]] = question
    answers = response["answers"]
    if set(answers) != set(expected):
        raise ValueError("请回答全部问题，且不要提交额外的问题 ID")
    for question_id, question in expected.items():
        value = answers[question_id]
        if not isinstance(value, dict) or set(value) != {"answers"}:
            raise ValueError("每个问题需要一个 answers 数组")
        supplied = value["answers"]
        if (
            not isinstance(supplied, list)
            or not supplied
            or any(not isinstance(answer, str) or not answer.strip() for answer in supplied)
        ):
            raise ValueError("每个问题至少需要一个非空回答")
        try:
            for answer in supplied:
                answer.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("回答包含无效字符") from exc
        options = question.get("options")
        if options and question.get("isOther") is not True:
            if not isinstance(options, list) or any(
                not isinstance(option, dict) or not isinstance(option.get("label"), str)
                for option in options
            ):
                raise ValueError("该请求的选项格式无效")
            allowed = {option["label"] for option in options}
            if any(answer not in allowed for answer in supplied):
                raise ValueError("请选择该问题提供的选项")


def pending_interactions(limit, offset):
    """Return bounded metadata without exposing questions, secrets or RPC IDs."""
    query = (
        db.session.query(Interaction, Host.name, Thread.display_title, Thread.title)
        .join(Host, Interaction.host_id == Host.id)
        .outerjoin(Thread, Interaction.thread_id == Thread.id)
        .filter(Interaction.status == "pending", Interaction.runtime_id == Host.runtime_id)
    )
    total = query.count()
    rows = query.order_by(Interaction.created_at, Interaction.id).offset(offset).limit(limit).all()
    result = []
    for interaction, host_name, display_title, title in rows:
        questions = interaction.params.get("questions")
        result.append(
            dict(
                id=interaction.id,
                thread_id=interaction.thread_id,
                host_id=interaction.host_id,
                thread_title=display_title or title or "未关联会话",
                host_name=host_name,
                method=interaction.method,
                created_at=iso(interaction.created_at),
                question_count=len(questions)
                if interaction.method in USER_INPUT_METHODS and isinstance(questions, list)
                else 0,
                blocking=is_blocking(interaction),
            )
        )
    next_offset = offset + len(result)
    return dict(
        interactions=result,
        total=total,
        has_more=next_offset < total,
        next_offset=next_offset if next_offset < total else None,
    )
