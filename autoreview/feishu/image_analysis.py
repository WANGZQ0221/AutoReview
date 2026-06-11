"""Image analysis clients for Feishu image messages."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import FeishuConfig


JsonDict = dict[str, Any]


class ImageAnalysisError(RuntimeError):
    pass


class ImageAnalysisClient:
    def __init__(self, config: FeishuConfig):
        self.image2_url = config.image2_url
        self.ocr_url = config.ocr_url
        self.ocr_api_key = config.ocr_api_key
        self.timeout_seconds = config.image_analysis_timeout_seconds

    @property
    def enabled(self) -> bool:
        return bool(self.image2_url or self.ocr_url)

    def analyze(self, image_bytes: bytes, image_name: str = "feishu_image.jpg") -> JsonDict:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        result: JsonDict = {}
        if self.image2_url:
            result["image2"] = self._call_provider(
                self.image2_url,
                {"imageBase64": image_base64, "function": 0},
            )
        if self.ocr_url:
            headers = {"x-api-key": self.ocr_api_key} if self.ocr_api_key else {}
            result["ocr"] = self._call_multipart_provider(
                self.ocr_url,
                image_bytes,
                image_name,
                headers=headers,
            )
        return result

    def _call_provider(self, url: str, body: JsonDict, headers: JsonDict | None = None) -> JsonDict:
        request_headers = {"Content-Type": "application/json; charset=utf-8"}
        request_headers.update(headers or {})
        request = Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw_body = response.read().decode("utf-8", errors="replace")
                payload = json.loads(raw_body) if raw_body else {}
                return {
                    "ok": 200 <= response.status < 300,
                    "status": response.status,
                    "data": payload,
                }
        except HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            return {
                "ok": False,
                "status": exc.code,
                "error": raw_body,
            }
        except (URLError, TimeoutError) as exc:
            return {
                "ok": False,
                "status": 0,
                "error": str(exc),
            }
        except json.JSONDecodeError as exc:
            raise ImageAnalysisError(f"Image analysis returned non-JSON response: {exc}") from exc

    def _call_multipart_provider(
        self,
        url: str,
        image_bytes: bytes,
        image_name: str,
        headers: JsonDict | None = None,
    ) -> JsonDict:
        boundary = "autoreview-feishu-image-boundary"
        body = self._build_multipart_body(boundary, image_bytes, image_name)
        request_headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        request_headers.update(headers or {})
        request = Request(
            url,
            data=body,
            headers=request_headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw_body = response.read().decode("utf-8", errors="replace")
                payload = json.loads(raw_body) if raw_body else {}
                return {
                    "ok": 200 <= response.status < 300,
                    "status": response.status,
                    "data": payload,
                }
        except HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            return {
                "ok": False,
                "status": exc.code,
                "error": raw_body,
            }
        except (URLError, TimeoutError) as exc:
            return {
                "ok": False,
                "status": 0,
                "error": str(exc),
            }
        except json.JSONDecodeError as exc:
            raise ImageAnalysisError(f"Image analysis returned non-JSON response: {exc}") from exc

    @staticmethod
    def _build_multipart_body(boundary: str, image_bytes: bytes, image_name: str) -> bytes:
        filename = image_name or "feishu_image.jpg"
        mime_type = _guess_image_mime_type(filename)
        parts = [
            f"--{boundary}\r\n"
            "Content-Disposition: form-data; name=\"image_name\"\r\n\r\n"
            f"{filename}\r\n",
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime_type}\r\n\r\n",
        ]
        return (
            "".join(parts).encode("utf-8")
            + image_bytes
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )


def extract_ocr_text(analysis: JsonDict) -> str:
    result = analysis.get("ocr") or {}
    if not result.get("ok"):
        return ""
    payload = result.get("data") or {}
    return _pick_text(payload) or _pick_rows_text(payload)


def format_image_analysis(analysis: JsonDict, *, include_auxiliary: bool = False) -> str:
    if not analysis:
        return "图片已下载，但 image2/OCR 未配置。"

    lines = []
    if "ocr" in analysis:
        lines.append(_format_provider_result("OCR", analysis["ocr"]))
    if "image2" in analysis and (include_auxiliary or "ocr" not in analysis):
        lines.append(_format_provider_result("image2 辅助", analysis["image2"]))
    return "\n".join(line for line in lines if line)


def _format_provider_result(name: str, result: JsonDict) -> str:
    if not result.get("ok"):
        return f"- {name}：失败（HTTP {result.get('status')}）{_shorten(result.get('error', ''))}"

    payload = result.get("data") or {}
    text = _pick_text(payload) or _pick_rows_text(payload)
    total = _pick_number(payload, ["data.total", "total"])
    parts = [f"- {name}：成功"]
    if text:
        parts.append(f"文本：{_shorten(text)}")
    if total is not None:
        parts.append(f"数量：{total}")
    return "，".join(parts)


def _pick_text(payload: JsonDict) -> str:
    for path in (
        "data.image_txt",
        "data.text",
        "data.ocr_text",
        "full_text",
        "ocr_result",
        "image_txt",
        "text",
        "ocr_text",
        "result.text",
    ):
        value = _dig(payload, path)
        if value:
            return str(value)
    return ""


def _pick_rows_text(payload: JsonDict) -> str:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return ""
    pieces = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = row.get("Content") or row.get("OcrText") or row.get("text")
        if text:
            pieces.append(str(text).strip())
    return "\n".join(piece for piece in pieces if piece)


def _pick_number(payload: JsonDict, paths: list[str]) -> int | None:
    for path in paths:
        value = _dig(payload, path)
        if isinstance(value, int):
            return value
    return None


def _dig(payload: JsonDict, path: str) -> Any:
    current: Any = payload
    for key in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _shorten(value: Any, limit: int = 120) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _guess_image_mime_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"
