"""Deterministic JSONL peer for integration tests; never calls a model or network."""

import argparse
import base64
import copy
import json
import sys
from pathlib import Path


def send(message):
    print(json.dumps(message), flush=True)


def notify(method, params):
    send({"method": method, "params": params})


def main():
    # Codex's JSONL transport is UTF-8 even when Windows defaults to a legacy code page.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--audit", required=True)
    args = parser.parse_args()
    root = Path(args.root)
    threads = {}
    execution = {}
    default_execution = {
        "model": "test-default",
        "reasoningEffort": "medium",
        "approvalPolicy": "on-request",
        "approvalsReviewer": "user",
        "sandbox": {"type": "workspaceWrite", "networkAccess": False, "writableRoots": []},
    }

    def apply_settings(params, previous=None):
        result = copy.deepcopy(previous or default_execution)
        for key in ("model", "approvalPolicy", "approvalsReviewer"):
            if key in params:
                result[key] = params[key]
        config = params.get("config") or {}
        if "model_reasoning_effort" in config:
            result["reasoningEffort"] = config["model_reasoning_effort"]
        if "effort" in params:
            result["reasoningEffort"] = params["effort"]
        if "sandbox" in params:
            result["sandbox"] = {
                "type": {
                    "workspace-write": "workspaceWrite",
                    "read-only": "readOnly",
                    "danger-full-access": "dangerFullAccess",
                }[params["sandbox"]]
            }
        if "sandbox_workspace_write.network_access" in config:
            result["sandbox"]["networkAccess"] = config["sandbox_workspace_write.network_access"]
        if "sandboxPolicy" in params:
            result["sandbox"] = copy.deepcopy(params["sandboxPolicy"])
        return result

    history_cwd = root.parent / "previous-project"
    history_cwd.mkdir(exist_ok=True)
    (history_cwd / "history.png").write_bytes((root / "output.png").read_bytes())
    historical = {
        "id": "native-history",
        "cwd": str(history_cwd),
        "name": "Existing desktop conversation",
        "preview": "Historical question",
        "createdAt": 1700000000,
        "updatedAt": 1700000100,
        "source": "appServer",
        "turns": [],
    }
    image_url = (
        "data:image/png;base64,"
        + base64.b64encode((history_cwd / "history.png").read_bytes()).decode()
    )
    history_turns = [
        {
            "id": "old-turn-1",
            "startedAt": 1700000000,
            "items": [
                {
                    "id": "old-user-1",
                    "type": "userMessage",
                    "content": [
                        {"type": "text", "text": "Historical question"},
                        {"type": "image", "url": image_url},
                    ],
                },
                {"id": "old-answer-1", "type": "agentMessage", "text": "Historical answer"},
            ],
        },
        {
            "id": "old-turn-2",
            "startedAt": 1700000100,
            "items": [
                {"id": "old-image", "type": "imageView", "path": str(history_cwd / "history.png")},
                {
                    "id": "old-search",
                    "type": "webSearch",
                    "status": "completed",
                    "action": {"type": "openPage", "url": "https://example.com/history"},
                },
            ],
        },
    ]
    pending = None
    sequence = 0
    with Path(args.audit).open("a", encoding="utf-8", buffering=1) as audit:
        for raw in sys.stdin:
            message = json.loads(raw)
            audit.write(json.dumps(message) + "\n")
            method = message.get("method")
            rpc_id = message.get("id")
            params = message.get("params", {})
            if method == "initialize":
                send({"id": rpc_id, "result": {"userAgent": "codex-test-peer/1"}})
            elif method == "initialized":
                pass
            elif method == "model/list":
                models = [
                    {
                        "id": "test-default-id",
                        "model": "test-default",
                        "displayName": "Test default",
                        "isDefault": True,
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
                        "futureCatalogField": {"preserve": True},
                    },
                    {
                        "id": "test-special-id",
                        "model": "test-special",
                        "displayName": "Test special",
                        "isDefault": False,
                        "defaultReasoningEffort": "future-effort",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": "future-effort"},
                        ],
                    },
                ]
                offset = int(params.get("cursor", "0"))
                send(
                    {
                        "id": rpc_id,
                        "result": {
                            "data": models[offset : offset + 1],
                            "nextCursor": str(offset + 1) if offset + 1 < len(models) else None,
                        },
                    }
                )
            elif method == "config/read":
                send(
                    {
                        "id": rpc_id,
                        "result": {
                            "config": {
                                "model": "test-default",
                                "model_reasoning_effort": "medium",
                                "approval_policy": "on-request",
                                "sandbox_mode": "workspace-write",
                                "approvals_reviewer": "user",
                                "sandbox_workspace_write": {"network_access": False},
                                "developer_instructions": "PRIVATE-TEST-CONFIG",
                            },
                            "origins": {"private": {"token": "PRIVATE-TEST-ORIGIN"}},
                        },
                    }
                )
            elif method == "configRequirements/read":
                send(
                    {
                        "id": rpc_id,
                        "result": {
                            "requirements": {
                                "allowedApprovalPolicies": ["on-request", "untrusted"],
                                "allowedSandboxModes": ["workspace-write", "read-only"],
                            }
                        },
                    }
                )
            elif method == "thread/start":
                native_id = f"native-thread-{len(threads) + 1}"
                threads[native_id] = params["cwd"]
                execution[native_id] = apply_settings(params)
                send(
                    {
                        "id": rpc_id,
                        "result": {
                            "thread": {"id": native_id, "cwd": params["cwd"]},
                            **execution[native_id],
                        },
                    }
                )
            elif method == "thread/list":
                send({"id": rpc_id, "result": {"data": [historical], "nextCursor": None}})
            elif method == "thread/read":
                thread = {key: value for key, value in historical.items() if key != "turns"}
                if params.get("includeTurns"):
                    thread["turns"] = history_turns
                send({"id": rpc_id, "result": {"thread": thread}})
            elif method == "thread/turns/list":
                second = bool(params.get("cursor"))
                turn = history_turns[1 if second else 0]
                send(
                    {
                        "id": rpc_id,
                        "result": {
                            "data": [{**turn, "items": []}]
                            if params.get("itemsView") == "notLoaded"
                            else [turn],
                            "nextCursor": None if second else "older-page-2",
                        },
                    }
                )
            elif method == "thread/items/list":
                entries = [
                    {
                        "turnId": turn["id"],
                        "item": item,
                        "startedAtMs": turn["startedAt"] * 1000,
                        "completedAtMs": None,
                    }
                    for turn in history_turns
                    if params.get("turnId") is None or turn["id"] == params["turnId"]
                    for item in turn["items"]
                ]
                if params.get("sortDirection") == "desc":
                    entries.reverse()
                index = int(params.get("cursor", "0"))
                limit = params.get("limit", 100)
                page = entries[index : index + limit]
                send(
                    {
                        "id": rpc_id,
                        "result": {
                            "data": page,
                            "nextCursor": str(index + len(page))
                            if index + len(page) < len(entries)
                            else None,
                        },
                    }
                )
            elif method == "thread/resume":
                native_id = params["threadId"]
                execution[native_id] = apply_settings(params, execution.get(native_id))
                send(
                    {"id": rpc_id, "result": {"thread": {"id": native_id}, **execution[native_id]}}
                )
            elif method == "turn/start":
                sequence += 1
                native_id = params["threadId"]
                execution[native_id] = apply_settings(params, execution.get(native_id))
                turn_id = f"native-turn-{sequence}"
                base = {"threadId": native_id, "turnId": turn_id}
                send({"id": rpc_id, "result": {"turn": {"id": turn_id}}})
                notify("turn/started", {"threadId": native_id, "turn": {"id": turn_id}})
                notify(
                    "item/completed",
                    {
                        **base,
                        "item": {
                            "id": f"user-{sequence}",
                            "type": "userMessage",
                            "content": params["input"],
                        },
                    },
                )
                if sequence == 1 and native_id != historical["id"]:
                    notify(
                        "item/agentMessage/delta",
                        {**base, "itemId": "answer-1", "delta": "Connected "},
                    )
                    notify(
                        "item/agentMessage/delta",
                        {**base, "itemId": "answer-1", "delta": "from fake Codex."},
                    )
                    pending = base
                    send(
                        {
                            "id": 101,
                            "method": "item/commandExecution/requestApproval",
                            "params": {
                                **base,
                                "itemId": "approval-1",
                                "command": "read test image",
                                "availableDecisions": ["accept", "decline"],
                            },
                        }
                    )
                else:
                    text = next(
                        value["text"] for value in params["input"] if value["type"] == "text"
                    )
                    item = {"id": f"answer-{sequence}", "type": "agentMessage", "text": text}
                    notify("item/completed", {**base, "item": item})
                    notify(
                        "turn/completed",
                        {
                            "threadId": native_id,
                            "turn": {"id": turn_id, "status": "completed", "items": [item]},
                        },
                    )
            elif method is None and rpc_id == 101 and pending:
                if not isinstance(rpc_id, int) or message.get("result") != {"decision": "accept"}:
                    raise RuntimeError("approval response lost its original native RPC shape")
                base = pending
                items = [
                    {
                        "id": "answer-1",
                        "type": "agentMessage",
                        "text": "Connected from fake Codex.",
                    },
                    {"id": "image-1", "type": "imageView", "path": str(root / "output.png")},
                    {
                        "id": "search-1",
                        "type": "webSearch",
                        "status": "completed",
                        "action": {
                            "type": "openPage",
                            "url": "https://example.com/codex",
                            "title": "Test source",
                        },
                    },
                ]
                for item in items:
                    notify("item/completed", {**base, "item": item})
                notify(
                    "turn/completed",
                    {
                        "threadId": base["threadId"],
                        "turn": {"id": base["turnId"], "status": "completed", "items": items},
                    },
                )
                pending = None
            else:
                send(
                    {"id": rpc_id, "error": {"code": -32601, "message": "Unsupported test method"}}
                )


if __name__ == "__main__":
    main()
