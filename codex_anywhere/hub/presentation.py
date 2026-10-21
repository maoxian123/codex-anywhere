"""Small browser projections; original native items remain in the journal/DB."""

from __future__ import annotations

MAX_TEXT = 16_000
MAX_ITEM_TEXT = 64_000


def compact_payload(original):
    budget = MAX_ITEM_TEXT
    nodes = 4096
    truncated = False

    def compact(value, depth=0):
        nonlocal budget, truncated, nodes
        nodes -= 1
        if nodes < 0:
            truncated = True
            return None
        if isinstance(value, str):
            if value.startswith("data:image/"):
                truncated = True
                return "[图片通过附件加载]"
            available = max(0, min(MAX_TEXT, budget))
            budget -= min(len(value), available)
            if len(value) > available:
                truncated = True
                return value[:available] + "\n… 点击查看完整内容"
            return value
        if isinstance(value, (dict, list)) and depth > 16:
            truncated = True
            return "[嵌套内容按需加载]"
        if isinstance(value, list):
            if len(value) > 100:
                truncated = True
            return [compact(child, depth + 1) for child in value[:100]]
        if isinstance(value, dict):
            # Metadata required for images/references gets its own small budget;
            # do not let a giant tool output consume their URLs and titles.
            entries = list(value.items())
            if len(entries) > 100:
                truncated = True
            result = {}
            for key, child in entries[:100]:
                if len(key) > 256 or len(key) > budget:
                    truncated = True
                    continue
                budget -= len(key)
                result[key] = compact(child, depth + 1)
            return result
        return value

    metadata = {key: original[key] for key in ("_assets", "_references") if key in original}
    data = compact({key: value for key, value in original.items() if key not in metadata})
    budget = 32_000
    nodes = 1024
    data.update(compact(metadata))
    if truncated:
        data["_truncated"] = True
    return data


def browser_item(item):
    result = item.as_dict()
    result["data"] = compact_payload(result["data"])
    return result


def browser_event(event):
    result = event.as_dict()
    result["params"] = compact_payload(result["params"])
    if result["params"].get("_truncated"):
        result["params"]["_raw_url"] = f"/api/events/{event.id}"
    return result
