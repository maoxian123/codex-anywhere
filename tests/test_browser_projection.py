import json

from codex_anywhere.hub.models import Item, db


def test_paged_history_is_compact_but_raw_item_remains_retrievable(
    app, api, paired_host, create_thread
):
    host = paired_host()
    thread = create_thread(host)
    long_output = "tool output " * 100_000
    data = {
        "id": "native-large",
        "type": "commandExecution",
        "aggregatedOutput": long_output,
        "unknownFutureField": {"nested": ["preserved"]},
        "_assets": [{"id": "image", "url": "/api/assets/image", "name": "plot.png"}],
    }
    with app.app_context():
        item = Item(
            thread_id=thread["id"],
            native_id="native-large",
            type="commandExecution",
            position=1,
            data=data,
        )
        db.session.add(item)
        db.session.commit()
        item_id = item.id
    path = f"/api/threads/{thread['id']}"
    compact = api("GET", path + "?item_limit=100").get_json()["items"][0]
    assert compact["data"]["_truncated"] is True
    assert len(json.dumps(compact)) < 100_000
    assert compact["data"]["_assets"] == data["_assets"]
    full = api("GET", path + f"/items/{item_id}").get_json()["item"]
    assert full["data"] == data
    assert api("GET", path).get_json()["items"][0]["data"] == data


def test_raw_item_endpoint_cannot_cross_thread_boundaries(app, api, paired_host, create_thread):
    host = paired_host()
    first, second = create_thread(host), create_thread(host)
    with app.app_context():
        item = Item(
            thread_id=first["id"],
            native_id="private",
            type="agentMessage",
            position=1,
            data={"text": "original"},
        )
        db.session.add(item)
        db.session.commit()
        item_id = item.id
    assert api("GET", f"/api/threads/{second['id']}/items/{item_id}").status_code == 404


def test_native_inline_images_are_not_resent_in_each_browser_page(
    app, api, paired_host, create_thread
):
    thread = create_thread(paired_host())
    data = {
        "id": "image",
        "type": "userMessage",
        "content": [{"type": "image", "url": "data:image/png;base64," + "A" * 200_000}],
        "_assets": [{"id": "asset", "url": "/api/assets/asset"}],
    }
    with app.app_context():
        db.session.add(
            Item(
                thread_id=thread["id"], native_id="image", type="userMessage", position=1, data=data
            )
        )
        db.session.commit()
    page = api("GET", f"/api/threads/{thread['id']}?item_limit=100").get_json()
    assert len(json.dumps(page)) < 5000
    assert page["items"][0]["data"]["_assets"] == data["_assets"]


def test_large_structured_output_keeps_attachment_metadata(app, api, paired_host, create_thread):
    thread = create_thread(paired_host())
    data = {
        "id": "structured",
        "type": "futureToolResult",
        "rows": [list(range(100)) for _ in range(100)],
        "_assets": [{"id": "asset", "url": "/api/assets/asset"}],
    }
    with app.app_context():
        db.session.add(
            Item(
                thread_id=thread["id"],
                native_id="structured",
                type="futureToolResult",
                position=1,
                data=data,
            )
        )
        db.session.commit()
    response = api("GET", f"/api/threads/{thread['id']}?item_limit=100")
    assert response.status_code == 200
    displayed = response.get_json()["items"][0]["data"]
    assert displayed["_truncated"] is True
    assert displayed["_assets"] == data["_assets"]
