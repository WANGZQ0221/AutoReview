"""Minimal Feishu Open Platform client."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


JsonDict = dict[str, Any]


class FeishuApiError(RuntimeError):
    pass


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str, base_url: str = "https://open.feishu.cn"):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = base_url.rstrip("/")
        self._tenant_access_token = ""
        self._tenant_access_token_expires_at = 0

    def reply_text(self, message_id: str, text: str) -> JsonDict:
        return self._request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            body={
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            auth=True,
        )

    def get_message_resource(self, message_id: str, file_key: str, resource_type: str = "image") -> tuple[bytes, str]:
        query = urlencode({"type": resource_type})
        path = (
            "/open-apis/im/v1/messages/"
            f"{quote(message_id, safe='')}/resources/{quote(file_key, safe='')}?{query}"
        )
        return self._request_bytes("GET", path, auth=True)

    def get_tenant_access_token(self) -> str:
        now = int(time.time())
        if self._tenant_access_token and self._tenant_access_token_expires_at > now + 60:
            return self._tenant_access_token
        payload = self._request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            body={
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            },
            auth=False,
        )
        token = payload.get("tenant_access_token")
        if not token:
            raise FeishuApiError(f"Feishu token response missing tenant_access_token: {payload}")
        self._tenant_access_token = str(token)
        self._tenant_access_token_expires_at = now + int(payload.get("expire", 7200))
        return self._tenant_access_token

    def _request(self, method: str, path: str, *, body: JsonDict | None, auth: bool) -> JsonDict:
        response_body, _ = self._request_bytes(method, path, body=body, auth=auth)
        payload = json.loads(response_body.decode("utf-8"))
        if payload.get("code") not in (0, None):
            raise FeishuApiError(str(payload))
        return payload

    def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        body: JsonDict | None = None,
        auth: bool,
    ) -> tuple[bytes, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            headers["Authorization"] = f"Bearer {self.get_tenant_access_token()}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:
                return response.read(), response.headers.get("Content-Type", "")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise FeishuApiError(f"Feishu API HTTP {exc.code}: {error_body}") from exc
