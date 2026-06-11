"""OPPO Open Platform API client.

The public interface follows OPPO's API upload flow:
token -> signed business requests -> pre-upload config -> file upload.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import http.client
import json
import mimetypes
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import OppoApiSettings
from .errors import OppoApiError


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class Token:
    access_token: str
    expire_in: int | None = None

    @property
    def is_valid(self) -> bool:
        if self.expire_in is None:
            return True
        return self.expire_in > int(time.time()) + 300


def normalize_param_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def normalize_params(params: JsonDict) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in params.items():
        string_value = normalize_param_value(value)
        if string_value is not None:
            normalized[key] = string_value
    return normalized


def build_api_sign(params: JsonDict, client_secret: str) -> str:
    """Build OPPO api_sign using ASCII key order and HMAC-SHA256."""

    normalized = normalize_params(params)
    canonical = "&".join(
        f"{key}={normalized[key]}"
        for key in sorted(normalized.keys())
        if key != "api_sign"
    )
    digest = hmac.new(
        client_secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest.lower()


def _extract_message(payload: Any) -> str:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("message", "msg", "error_description", "err_msg"):
                if data.get(key):
                    return str(data[key])
        for key in ("message", "msg", "error_description", "err_msg"):
            if payload.get(key):
                return str(payload[key])
        if payload.get("errno") is not None:
            return f"OPPO API errno={payload.get('errno')}"
    return "OPPO API request failed"


def extract_upload_url(data: JsonDict) -> str | None:
    for key in ("url", "file_url", "uri_path", "path"):
        if data.get(key):
            return str(data[key])
    return None


class OppoApiClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        settings: OppoApiSettings | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.settings = settings or OppoApiSettings()
        self._token: Token | None = None

    def get_access_token(self, *, force_refresh: bool = False) -> Token:
        if self._token and self._token.is_valid and not force_refresh:
            return self._token

        payload = self._request_json(
            "GET",
            self.settings.token_path,
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            signed=False,
        )
        data = self._require_success(payload)
        token_value = data.get("access_token") if isinstance(data, dict) else None
        if not token_value:
            raise OppoApiError("OPPO token response did not include access_token", payload=payload)
        expire_in = data.get("expire_in") if isinstance(data, dict) else None
        expires_at = self._normalize_expire_in(expire_in)
        self._token = Token(str(token_value), expires_at)
        return self._token

    def get_upload_config(self) -> JsonDict:
        payload = self._signed_request("GET", self.settings.pre_upload_path)
        data = self._require_success(payload)
        if not isinstance(data, dict) or not data.get("upload_url") or not data.get("sign"):
            raise OppoApiError("OPPO upload config response is missing upload_url or sign", payload=payload)
        return data

    def upload_file(self, path: str | Path, file_type: str) -> JsonDict:
        upload_config = self.get_upload_config()
        payload = self._multipart_upload(
            upload_url=str(upload_config["upload_url"]),
            fields={"type": file_type, "sign": str(upload_config["sign"])},
            file_path=Path(path),
        )
        data = self._require_success(payload)
        upload_url = extract_upload_url(data) if isinstance(data, dict) else None
        if not isinstance(data, dict) or not upload_url:
            raise OppoApiError("OPPO upload response did not include file url", payload=payload)
        data.setdefault("url", upload_url)
        return data

    def release_version(self, params: JsonDict) -> JsonDict:
        payload = self._signed_request(
            "POST",
            self.settings.release_path,
            params,
            timeout=self.settings.submit_timeout_seconds,
        )
        data = self._require_success(payload)
        return data if isinstance(data, dict) else {"data": data}

    def update_material(self, params: JsonDict) -> JsonDict:
        payload = self._signed_request("POST", self.settings.update_material_path, params)
        data = self._require_success(payload)
        return data if isinstance(data, dict) else {"data": data}

    def get_task_state(self, pkg_name: str, version_code: str) -> JsonDict:
        payload = self._signed_request(
            "POST",
            self.settings.task_state_path,
            {"pkg_name": pkg_name, "version_code": version_code},
        )
        data = self._require_success(payload)
        return data if isinstance(data, dict) else {"data": data}

    def get_app_info(self, pkg_name: str, version_code: str | None = None) -> JsonDict:
        params: JsonDict = {"pkg_name": pkg_name}
        if version_code:
            params["version_code"] = version_code
        payload = self._signed_request("GET", self.settings.detail_path, params)
        data = self._require_success(payload)
        return data if isinstance(data, dict) else {"data": data}

    def _signed_request(
        self,
        method: str,
        path: str,
        params: JsonDict | None = None,
        *,
        timeout: int | None = None,
    ) -> JsonDict:
        token = self.get_access_token()
        signed_params: JsonDict = {
            **(params or {}),
            "access_token": token.access_token,
            "timestamp": str(int(time.time())),
        }
        signed_params["api_sign"] = build_api_sign(signed_params, self.client_secret)
        return self._request_json(
            method,
            path,
            params=normalize_params(signed_params),
            signed=True,
            timeout=timeout,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: JsonDict | None = None,
        signed: bool,
        timeout: int | None = None,
    ) -> JsonDict:
        timeout = timeout or self.settings.timeout_seconds
        url = self._build_url(path)
        headers = {"Accept": "application/json"}
        data: bytes | None = None
        method = method.upper()

        encoded = urlencode(normalize_params(params or {}))
        if method == "GET" and encoded:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{encoded}"
        elif method == "POST":
            data = encoded.encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                status_code = response.status
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = body
            raise OppoApiError(
                _extract_message(payload),
                status_code=exc.code,
                payload=payload,
            ) from exc
        except Exception as exc:  # urllib raises several URL/network error subclasses.
            raise OppoApiError(f"OPPO {method} request failed: {exc}") from exc

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OppoApiError(
                f"OPPO {method} response is not JSON",
                status_code=status_code,
                payload=body,
            ) from exc

        if status_code < 200 or status_code >= 300:
            raise OppoApiError(
                _extract_message(payload),
                status_code=status_code,
                payload=payload,
            )

        return payload

    def _build_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        base = self.settings.base_url.rstrip("/")
        path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
        return f"{base}{path}"

    def _require_success(self, payload: JsonDict) -> Any:
        if not isinstance(payload, dict):
            raise OppoApiError("OPPO response is not an object", payload=payload)
        errno = payload.get("errno")
        if errno not in (0, "0", None):
            raise OppoApiError(_extract_message(payload), payload=payload)
        if errno is None and payload.get("code") not in (0, "0", None):
            raise OppoApiError(_extract_message(payload), payload=payload)
        return payload.get("data", payload)

    def _multipart_upload(
        self,
        *,
        upload_url: str,
        fields: dict[str, str],
        file_path: Path,
    ) -> JsonDict:
        if not file_path.exists() or not file_path.is_file():
            raise OppoApiError(f"Upload file does not exist: {file_path}")

        boundary = f"----AutoReviewOppo{int(time.time() * 1000)}"
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        file_name = file_path.name.replace('"', "")
        file_size = file_path.stat().st_size

        text_parts = [
            self._multipart_text_part(boundary, key, value) for key, value in fields.items()
        ]
        file_header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        file_tail = b"\r\n"
        closing = f"--{boundary}--\r\n".encode("utf-8")
        content_length = (
            sum(len(part) for part in text_parts)
            + len(file_header)
            + file_size
            + len(file_tail)
            + len(closing)
        )

        split_url = urlsplit(upload_url)
        if split_url.scheme not in ("http", "https") or not split_url.netloc:
            raise OppoApiError(f"Invalid OPPO upload_url: {upload_url}")
        connection_cls = http.client.HTTPSConnection if split_url.scheme == "https" else http.client.HTTPConnection
        path = split_url.path or "/"
        if split_url.query:
            path = f"{path}?{split_url.query}"
        connection = connection_cls(split_url.netloc, timeout=self.settings.timeout_seconds)

        try:
            connection.putrequest("POST", path)
            connection.putheader("Host", split_url.netloc)
            connection.putheader("Accept", "application/json")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(content_length))
            connection.endheaders()
            for part in text_parts:
                connection.send(part)
            connection.send(file_header)
            with file_path.open("rb") as file_obj:
                while True:
                    chunk = file_obj.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
            connection.send(file_tail)
            connection.send(closing)
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            status_code = response.status
        except Exception as exc:
            raise OppoApiError(f"OPPO file upload failed: {exc}") from exc
        finally:
            connection.close()

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OppoApiError(
                "OPPO upload response is not JSON",
                status_code=status_code,
                payload=body,
            ) from exc

        if status_code < 200 or status_code >= 300:
            raise OppoApiError(_extract_message(payload), status_code=status_code, payload=payload)
        return payload

    @staticmethod
    def _multipart_text_part(boundary: str, name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{value}\r\n"
        ).encode("utf-8")

    @staticmethod
    def _normalize_expire_in(expire_in: Any) -> int | None:
        if not expire_in:
            return None
        expire_value = int(expire_in)
        now = int(time.time())
        if expire_value < now:
            return now + expire_value
        return expire_value
