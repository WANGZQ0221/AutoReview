"""Feishu event parsing helpers."""

from __future__ import annotations

import json
from typing import Any


JsonDict = dict[str, Any]


def verify_token(payload: JsonDict, expected_token: str) -> bool:
    if not expected_token:
        return True
    token = payload.get("token") or payload.get("header", {}).get("token")
    return token == expected_token


def get_challenge_response(payload: JsonDict) -> JsonDict | None:
    challenge = payload.get("challenge")
    if challenge:
        return {"challenge": challenge}
    return None


def extract_message_event(payload: JsonDict) -> JsonDict | None:
    schema = payload.get("schema")
    if schema == "2.0":
        header = payload.get("header") or {}
        event = payload.get("event") or {}
        if header.get("event_type") != "im.message.receive_v1":
            return None
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        content = message.get("content", "")
        content_data = parse_content(content)
        message_type = message.get("message_type", "")
        return {
            "message_id": message.get("message_id", ""),
            "chat_id": message.get("chat_id", ""),
            "sender_id": (
                sender.get("sender_id", {}).get("open_id")
                or sender.get("sender_id", {}).get("union_id")
                or ""
            ),
            "message_type": message_type,
            "text": extract_text_content(content),
            "image_key": content_data.get("image_key") or "",
            "file_key": content_data.get("file_key") or "",
            "file_name": content_data.get("file_name") or content_data.get("name") or "",
            "content": content_data,
        }

    event = payload.get("event") or {}
    if event.get("type") == "message":
        content = event.get("content", "")
        content_data = parse_content(content)
        message_type = event.get("message_type") or event.get("msg_type") or ""
        return {
            "message_id": event.get("message_id", ""),
            "chat_id": event.get("open_chat_id") or event.get("chat_id") or "",
            "sender_id": event.get("open_id") or "",
            "message_type": message_type,
            "text": event.get("text") or extract_text_content(content),
            "image_key": event.get("image_key") or content_data.get("image_key") or "",
            "file_key": event.get("file_key") or content_data.get("file_key") or "",
            "file_name": event.get("file_name") or content_data.get("file_name") or content_data.get("name") or "",
            "content": content_data,
        }
    return None


def parse_content(content: str | JsonDict) -> JsonDict:
    if isinstance(content, dict):
        return content
    if not content:
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def extract_text_content(content: str | JsonDict) -> str:
    parsed = parse_content(content)
    if parsed:
        return str(parsed.get("text") or "")
    return str(content)
