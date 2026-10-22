"""Hub-only conversation organization, independent of the native runtime."""

import unicodedata


def thread_preferences(data):
    if not data or set(data) - {"display_title", "pinned", "archived"}:
        raise ValueError("会话偏好必须包含显示名称、置顶或归档设置，不能包含未知字段")
    changes = dict(data)
    if "display_title" in changes and changes["display_title"] is not None:
        title = changes["display_title"]
        if not isinstance(title, str):
            raise ValueError("显示名称必须是字符串或 null")
        if any(unicodedata.category(char).startswith("C") for char in title):
            raise ValueError("显示名称不能包含控制字符")
        title = title.strip()
        if not 1 <= len(title) <= 240:
            raise ValueError("显示名称必须是 1–240 个字符")
        changes["display_title"] = title
    for key in ("pinned", "archived"):
        if key in changes and not isinstance(changes[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    return changes
