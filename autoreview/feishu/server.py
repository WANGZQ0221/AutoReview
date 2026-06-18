"""HTTP webhook server for Feishu events."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from typing import Any

from autoreview.agent import ReviewAgent
from autoreview.agent.llm import LlmConfig, OpenAICompatibleLlmClient
from autoreview.agent.state import JsonStateStore

from .client import FeishuApiError, FeishuClient
from .config import FeishuConfig
from .events import extract_message_event, get_challenge_response, verify_token as verify_event_token
from .image_analysis import ImageAnalysisClient, extract_ocr_text, format_image_analysis


JsonDict = dict[str, Any]
BOT_MENTION_NAMES = ("应用发布agent", "提交助手", "AutoReview", "autoreview")


class FeishuWebhookApp:
    def __init__(self, config: FeishuConfig):
        self.config = config
        llm_client = OpenAICompatibleLlmClient(LlmConfig.from_mapping(config.llm))
        self.agent = ReviewAgent(
            JsonStateStore(config.state_path),
            oppo_config_path=config.config_path,
            market_data_config_path=config.market_data_config_path,
            llm_client=llm_client if llm_client.enabled else None,
        )
        self.client = FeishuClient(config.app_id, config.app_secret)
        self.image_analyzer = ImageAnalysisClient(config)

    def handle_payload(self, payload: JsonDict, *, verify_token: bool = True) -> JsonDict:
        challenge = get_challenge_response(payload)
        if challenge:
            return challenge
        if verify_token and not verify_event_token(payload, self.config.verification_token):
            return {"code": 403, "message": "invalid verification token"}

        event = extract_message_event(payload)
        if not event:
            return {"code": 0, "message": "ignored"}

        return self.handle_message_event(event)

    def handle_message_event(self, event: JsonDict) -> JsonDict:
        if self._should_ignore_group_message(event):
            return {"code": 0, "message": "ignored: group message without bot mention"}

        if event.get("file_key") or event.get("message_type") == "file":
            return self.handle_file_event(event)

        if event.get("image_key") or event.get("message_type") == "image":
            return self.handle_image_event(event)

        session_id = event.get("chat_id") or event.get("sender_id") or "default"
        response = self.agent.handle_message(
            session_id=session_id,
            text=event.get("text", ""),
            sender_id=event.get("sender_id", ""),
        )
        message_id = event.get("message_id")
        if message_id:
            try:
                self.client.reply_text(message_id, response.text)
            except FeishuApiError as exc:
                return {"code": 1, "message": str(exc), "agent_response": response.text}
        return {"code": 0, "message": "ok", "agent_response": response.text}

    def _should_ignore_group_message(self, event: JsonDict) -> bool:
        if not _is_group_chat_event(event):
            return False
        return not _event_addresses_bot(event, app_id=self.config.app_id)

    def handle_image_event(self, event: JsonDict) -> JsonDict:
        message_id = event.get("message_id", "")
        image_key = event.get("image_key", "")
        session_id = event.get("chat_id") or event.get("sender_id") or "default"
        if not image_key:
            response_text = "收到图片消息，但没有拿到 image_key，暂时不能下载图片。"
            return self._reply(message_id, response_text)

        try:
            image_bytes, content_type = self.client.get_message_resource(message_id, image_key, "image")
        except FeishuApiError as exc:
            response_text = f"收到图片了，但从飞书下载图片失败：{exc}"
            return self._reply(message_id, response_text, code=1)

        image_name = _safe_image_name(image_key or message_id)
        upload = self._save_upload(
            session_id=session_id,
            message_id=message_id,
            resource_key=image_key,
            resource_type="image",
            file_name=image_name,
            content_type=content_type,
            data=image_bytes,
        )
        analysis: JsonDict = {}
        ocr_text = ""
        summary = ""
        if self.image_analyzer.enabled:
            analysis = self.image_analyzer.analyze(image_bytes, image_name=image_name)
            ocr_text = extract_ocr_text(analysis)
            summary = format_image_analysis(analysis)
        image_patch = {
            "last_upload": upload,
            "last_image_analysis": {
                "message_id": message_id,
                "image_key": image_key,
                "image_name": image_name,
                "content_type": content_type,
                "summary": summary,
                "ocr_text": ocr_text,
                "analysis": analysis,
            },
            "sender_id": event.get("sender_id", ""),
        }
        self.agent.state_store.update_session(
            session_id,
            image_patch,
        )
        if _looks_like_oppo_rejection(ocr_text):
            response = self.agent.analyze_rejection_text(
                session_id,
                ocr_text,
                sender_id=event.get("sender_id", ""),
                source="image_ocr",
            )
            response_text = response.text
        elif not self.image_analyzer.enabled:
            response_text = "收到图片，已保存为最近上传。可发送“绑定材料：图标/截图1/版权证明”。"
        else:
            response_text = "收到图片，OCR 已记录。发送“分析这张图”可分析文本；也可发送“绑定材料：图标/截图1/版权证明”。"
        return self._reply(message_id, response_text, agent_response=response_text)

    def handle_file_event(self, event: JsonDict) -> JsonDict:
        message_id = event.get("message_id", "")
        file_key = event.get("file_key", "")
        session_id = event.get("chat_id") or event.get("sender_id") or "default"
        if not file_key:
            response_text = "收到文件消息，但没有拿到 file_key，暂时不能下载文件。"
            return self._reply(message_id, response_text)

        try:
            file_bytes, content_type = self.client.get_message_resource(message_id, file_key, "file")
        except FeishuApiError as exc:
            response_text = f"收到文件了，但从飞书下载文件失败：{exc}"
            return self._reply(message_id, response_text, code=1)

        file_name = _safe_file_name(event.get("file_name") or file_key or message_id)
        upload = self._save_upload(
            session_id=session_id,
            message_id=message_id,
            resource_key=file_key,
            resource_type="file",
            file_name=file_name,
            content_type=content_type,
            data=file_bytes,
        )
        self.agent.state_store.update_session(
            session_id,
            {
                "last_upload": upload,
                "sender_id": event.get("sender_id", ""),
            },
        )
        response_text = (
            "收到文件，已保存为最近上传。"
            "可发送“绑定材料：APK/版权证明/ICP证明”。"
        )
        return self._reply(message_id, response_text, agent_response=response_text)

    def _save_upload(
        self,
        *,
        session_id: str,
        message_id: str,
        resource_key: str,
        resource_type: str,
        file_name: str,
        content_type: str,
        data: bytes,
    ) -> JsonDict:
        upload_dir = self.config.state_path.parent / "feishu_uploads" / _safe_slug(session_id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_file_name(file_name)
        target = upload_dir / _safe_file_name(f"{message_id}_{safe_name}")
        target.write_bytes(data)
        return {
            "message_id": message_id,
            "resource_key": resource_key,
            "resource_type": resource_type,
            "file_name": safe_name,
            "content_type": content_type,
            "path": str(target),
            "size": len(data),
        }

    def _reply(
        self,
        message_id: str,
        text: str,
        *,
        code: int = 0,
        agent_response: str | None = None,
    ) -> JsonDict:
        if message_id:
            try:
                self.client.reply_text(message_id, text)
            except FeishuApiError as exc:
                return {"code": 1, "message": str(exc), "agent_response": text}
        return {"code": code, "message": "ok" if code == 0 else "failed", "agent_response": agent_response or text}


def make_handler(app: FeishuWebhookApp):
    class FeishuWebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(raw_body)
                response = app.handle_payload(payload)
                status = 200 if response.get("code") in (0, None) else int(response.get("code", 500))
            except Exception as exc:
                response = {"code": 500, "message": str(exc)}
                status = 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))

        def log_message(self, format, *args):
            return

    return FeishuWebhookHandler


def run_server(config_path: str | Path, host: str = "0.0.0.0", port: int = 8080) -> None:
    config = FeishuConfig.from_file(config_path)
    app = FeishuWebhookApp(config)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"Feishu webhook server listening on http://{host}:{port}")
    server.serve_forever()


def _safe_image_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "feishu_image").strip("._")
    if not name:
        name = "feishu_image"
    if "." not in name:
        name += ".jpg"
    return name


def _safe_file_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "_", value or "feishu_upload").strip("._")
    return name or "feishu_upload"


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "default").strip("._")
    return slug or "default"


def _looks_like_oppo_rejection(text: str) -> bool:
    if not text:
        return False
    markers = (
        "未通过",
        "驳回",
        "审核不通过",
        "请勿重复提交",
        "APK相似度",
        "APK 相似度",
        "ICP备案",
        "版权证明",
        "特殊类证书",
        "马甲",
    )
    return any(marker in text for marker in markers)


def _is_group_chat_event(event: JsonDict) -> bool:
    chat_type = str(event.get("chat_type") or "").lower()
    return chat_type in {"group", "chat"}


def _event_addresses_bot(event: JsonDict, *, app_id: str = "") -> bool:
    text = str(event.get("text") or "")
    if _text_mentions_bot(text):
        return True

    content = event.get("content") if isinstance(event.get("content"), dict) else {}
    mentions = event.get("mentions") or content.get("mentions") or []
    if not isinstance(mentions, list):
        return False
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        values = " ".join(str(value or "") for value in mention.values())
        if app_id and app_id in values:
            return True
        if _text_mentions_bot(values):
            return True
    return False


def _text_mentions_bot(text: str) -> bool:
    clean = str(text or "")
    return any(name in clean for name in BOT_MENTION_NAMES)


def _shorten(value: Any, limit: int = 180) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."
