"""Deterministic JSONL peer for integration tests; never calls a model or network."""

import argparse
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
            elif method == "thread/start":
                native_id = f"native-thread-{len(threads) + 1}"
                threads[native_id] = params["cwd"]
                send({"id": rpc_id, "result": {"thread": {"id": native_id, "cwd": params["cwd"]}}})
            elif method == "thread/resume":
                send({"id": rpc_id, "result": {"thread": {"id": params["threadId"]}}})
            elif method == "turn/start":
                sequence += 1
                native_id = params["threadId"]
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
                if sequence == 1:
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
