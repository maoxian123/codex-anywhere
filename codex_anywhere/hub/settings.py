"""Persistent hub settings and their public, credential-free representation."""

from __future__ import annotations

import unicodedata

from flask import current_app

from .models import HubSetting, db


def administrator_credentials():
    # Refresh after acquiring the authentication lock, even if the request's
    # session previously read this record before another request changed it.
    record = db.session.get(HubSetting, "administrator", populate_existing=True)
    if record is not None:
        return dict(record.value)
    return {
        "username": current_app.config["ADMIN_USERNAME"],
        "password_hash": current_app.config["ADMIN_PASSWORD_HASH"],
    }


def public_settings(credentials):
    return {"administrator": {"username": credentials["username"]}}


def administrator_changes(data):
    if set(data) - {"username", "current_password", "new_password"}:
        raise ValueError("管理员设置包含未知字段")
    username = data.get("username")
    if not isinstance(username, str):
        raise ValueError("用户名必须是字符串")
    if any(unicodedata.category(char).startswith("C") for char in username):
        raise ValueError("用户名不能包含控制字符")
    username = username.strip()
    if not 1 <= len(username) <= 120:
        raise ValueError("用户名必须是 1–120 个字符")
    current_password = data.get("current_password")
    if not isinstance(current_password, str) or not 1 <= len(current_password) <= 1024:
        raise ValueError("请输入有效的当前密码")
    if "new_password" in data:
        password = data["new_password"]
        if not isinstance(password, str) or not 8 <= len(password) <= 1024:
            raise ValueError("新密码必须是 8–1024 个字符")
    return username, current_password, data.get("new_password")
