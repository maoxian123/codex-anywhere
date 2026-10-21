import base64
import json
from uuid import uuid4

import pytest

from codex_anywhere.hub.history import INITIAL_POSITION, project_history
from codex_anywhere.hub.models import Command, Event, Item, Thread, db
from codex_anywhere.hub.transport import _put_item


class PageProjection:
    generation = "a" * 64
    fingerprint = "b" * 64

    def __init__(self, host):
        self.host = host
        self.thread = Thread(
            host_id=host["host"]["id"],
            native_id="native-page",
            title="Native",
            cwd=host["root"],
            status="importing",
            imported=True,
            history_loaded=False,
            native_updated_at=100,
        )
        db.session.add(self.thread)
        db.session.flush()
        self.start()

    def start(self, cursor=None):
        self.command = Command(
            host_id=self.thread.host_id,
            thread_id=self.thread.id,
            kind="thread.history",
            payload={
                "native_thread_id": self.thread.native_id,
                **({"cursor": cursor} if cursor else {}),
            },
            runtime_id=self.host["runtime_id"],
        )
        db.session.add(self.command)
        db.session.commit()

    def cursor(self, anchor):
        return (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "v": 1,
                        "threadId": self.thread.native_id,
                        "generation": self.generation,
                        "snapshotUpdatedAt": 100,
                        "snapshotFingerprint": self.fingerprint,
                        "nativeCursor": "native-next",
                        "anchorItemId": anchor,
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
        )

    def emit(self, suffix, values, *, live=True, put_item=_put_item):
        params = {
            "threadId": self.thread.native_id,
            "commandId": self.command.id,
            "generation": self.generation,
            **values,
        }
        method = "anywhere/history_" + suffix
        event = Event(
            host_id=self.thread.host_id,
            thread_id=self.thread.id,
            method=method,
            params=params,
            runtime_id=self.host["runtime_id"],
            event_key=uuid4().hex,
        )
        db.session.add(event)
        db.session.flush()
        assert project_history(self.thread, method, params, live, put_item) is True
        db.session.commit()

    def stage(self, ids):
        self.emit(
            "metadata",
            {
                "thread": {
                    "id": self.thread.native_id,
                    "cwd": self.thread.cwd,
                    "title": "Native title",
                    "_history_fingerprint": self.fingerprint,
                },
                "snapshotUpdatedAt": 100,
            },
        )
        for index, ident in enumerate(ids):
            self.emit(
                "item",
                {
                    "index": index,
                    "turnId": "turn-" + ident,
                    "createdAt": 50,
                    "item": {
                        "id": ident,
                        "type": "futureUnknownItem",
                        "native": {"preserve": True},
                    },
                },
            )

    def complete(self, count, next_cursor=None, **kwargs):
        self.emit(
            "complete",
            {
                "snapshotUpdatedAt": 100,
                "itemCount": count,
                "nextCursor": next_cursor,
                "hasMore": next_cursor is not None,
            },
            **kwargs,
        )

    def items(self):
        return Item.query.filter_by(thread_id=self.thread.id).order_by(Item.position).all()


@pytest.fixture
def page(app, paired_host):
    host = paired_host()
    with app.app_context():
        yield PageProjection(host)


def test_page_projects_atomically_and_preserves_original_items(page):
    page.stage(["latest", "older"])
    assert page.items() == []
    assert page.thread.history_loaded is False
    token = page.cursor("older")
    page.complete(2, token)
    assert [(item.native_id, item.position) for item in page.items()] == [
        ("older", INITIAL_POSITION),
        ("latest", INITIAL_POSITION + 1),
    ]
    assert page.items()[0].data == {
        "id": "older",
        "type": "futureUnknownItem",
        "native": {"preserve": True},
    }
    assert page.thread.history_loaded is True and page.thread.history_complete is False
    assert page.thread.history_cursor == token and page.thread.history_generation == page.generation
    assert page.thread.status == "idle" and page.thread.history_stale is False
    assert page.command.status == "completed" and page.command.result["historyProjected"] is True
    db.session.remove()
    assert db.session.get(Thread, page.thread.id).history_loaded is True


def test_older_page_preserves_positions_and_uncovered_cached_items(page):
    page.stage(["latest", "older"])
    token = page.cursor("older")
    page.complete(2, token)
    db.session.add(
        Item(
            thread_id=page.thread.id,
            native_id="outside-page",
            type="agentMessage",
            data={"text": "keep"},
            position=INITIAL_POSITION - 100,
        )
    )
    db.session.commit()
    positions = {item.native_id: item.position for item in page.items()}
    page.start(token)
    page.stage(["old-2", "old-3"])
    page.complete(2)
    assert [item.native_id for item in page.items()] == [
        "outside-page",
        "old-3",
        "old-2",
        "older",
        "latest",
    ]
    assert all(
        next(item.position for item in page.items() if item.native_id == ident) == position
        for ident, position in positions.items()
    )
    assert page.thread.history_complete is True and page.thread.history_cursor is None


def test_refresh_without_overlap_leaves_space_for_native_gap(page):
    page.stage(["known"])
    page.complete(1)
    old_position = page.items()[0].position
    page.start()
    page.generation = "c" * 64
    page.stage(["newest", "new-tail"])
    token = page.cursor("new-tail")
    page.complete(2, token)
    page.start(token)
    page.stage(["middle"])
    page.complete(1)
    assert [item.native_id for item in page.items()] == ["known", "middle", "new-tail", "newest"]
    assert page.items()[0].position == old_position


def test_conflicting_native_order_fails_and_keeps_raw_staging(page):
    page.stage(["latest", "older"])
    page.complete(2)
    page.start()
    page.thread.history_loaded = False
    page.thread.status = "importing"
    db.session.commit()
    page.stage(["latest", "inserted-with-no-gap", "older"])
    page.complete(3)
    assert page.command.status == "failed" and "stable position gap" in page.command.error
    assert [item.native_id for item in page.items()] == ["older", "latest"]
    assert page.thread.status == "error" and page.thread.last_error == page.command.error
    assert Event.query.filter_by(method="anywhere/history_item").count() == 5


def test_callback_failure_rolls_back_all_items_in_page(page):
    page.stage(["latest", "older"])
    calls = 0

    def fail_after_write(*args, **kwargs):
        nonlocal calls
        _put_item(*args, **kwargs)
        calls += 1
        if calls == 2:
            raise ValueError("projection rejected")

    page.complete(2, put_item=fail_after_write)
    assert page.command.status == "failed"
    assert page.items() == [] and page.thread.history_cursor is None


def test_incomplete_older_page_is_acknowledged_without_marking_loaded_thread_failed(page):
    page.stage(["known"])
    token = page.cursor("known")
    page.complete(1, token)
    page.start(token)
    page.stage(["older"])
    page.complete(2)
    assert page.command.status == "failed"
    assert page.thread.status == "idle" and page.thread.last_error is None
    assert page.thread.history_loaded is True and page.thread.history_cursor == token
    assert [item.native_id for item in page.items()] == ["known"]


@pytest.mark.parametrize("cause", ["old-runtime", "new-turn", "new-history", "new-import"])
def test_late_page_cannot_replace_newer_work(page, cause):
    page.stage(["late"])
    if cause == "new-turn":
        db.session.add(
            Command(
                host_id=page.thread.host_id,
                thread_id=page.thread.id,
                kind="turn.start",
                payload={},
                status="completed",
            )
        )
        db.session.commit()
    elif cause in {"new-history", "new-import"}:
        db.session.add(
            Command(
                host_id=page.thread.host_id,
                thread_id=page.thread.id,
                kind="thread.import" if cause == "new-import" else "thread.history",
                payload={},
                runtime_id=page.host["runtime_id"],
            )
        )
        db.session.commit()
    page.complete(1, live=cause != "old-runtime")
    assert page.items() == [] and page.thread.history_loaded is False


def test_same_second_newer_catalog_fingerprint_survives_completion(page):
    page.stage(["snapshot"])
    page.thread.native_fingerprint = "d" * 64
    db.session.commit()
    page.complete(1)
    assert page.thread.native_fingerprint == "d" * 64
    assert page.thread.history_fingerprint == page.fingerprint
    assert page.thread.history_stale is True


def test_duplicate_complete_is_idempotent(page):
    page.stage(["only"])
    page.complete(1)
    original = [(item.id, item.position) for item in page.items()]
    page.complete(1)
    assert [(item.id, item.position) for item in page.items()] == original


def test_mismatched_cursor_cannot_commit_page_or_coverage(page):
    page.stage(["only"])
    page.complete(1, page.cursor("different-item"))
    assert page.command.status == "failed" and "cursor" in page.command.error
    assert page.items() == [] and page.thread.history_cursor is None


def test_old_cursor_page_cannot_replace_advanced_coverage(page):
    page.stage(["first"])
    token = page.cursor("first")
    page.complete(1, token)
    page.start(token)
    page.stage(["late-older"])
    page.thread.history_cursor = "newer-checkpoint"
    db.session.commit()
    page.complete(1)
    assert [item.native_id for item in page.items()] == ["first"]
    assert page.thread.history_cursor == "newer-checkpoint"
